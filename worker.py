"""Worker 调度器(CDP 模式):连接已运行的真实 Chrome 采集豆包回答。

与旧版(Playwright ``launch``)的区别:

    旧: ``browser.new_context(proxy=..., user_agent=...)`` + ``add_cookies``
        —— 每个任务都是全新的自动化指纹,豆包风控能识别并限流。

    新: ``connect_over_cdp(http://127.0.0.1:<port>)`` 连到**真实启动的 Chrome**,
        复用它的上下文与登录态,指纹与真人浏览器完全一致
        (``navigator.webdriver`` 为原生 ``false``)。

采集流程::

    1. 确保该账号的 Chrome 实例在运行(未运行则按需拉起)
    2. CDP 连接 → 复用 ``browser.contexts[0]``
    3. 定位/打开豆包页面,模拟真人行为(滚动、随机停顿)
    4. 驱动页面输入并发送,捕获豆包自己发出的 completion 响应
    5. 解析 SSE → 截图 → 落盘

**重要**:CDP 模式下 **绝不能调用 ``browser.close()``** —— 那会关闭整个 Chrome
实例、丢掉登录态。只让 playwright 上下文退出即可。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any
from urllib.parse import urlparse

import account_store
import cdp
import config
import utils
from account_pool import AccountPool
from screenshot import screenshot_conversation
from sse_parser import DoubaoSseParser
from task_queue import RedisTaskQueue

logger = logging.getLogger("doubao.worker")


class RateLimitedError(RuntimeError):
    """豆包返回限流/风控错误(STREAM_ERROR: rate limited)。

    这类失败不是任务本身的问题,而是账号(或出口 IP)被平台限流,
    需要更长的冷却时间。
    """


class StopTestSignal(Exception):
    """测试终止信号:用于"试探最小安全间隔"时,一旦触发限流就立即停下。"""


def is_completion_url(url: str) -> bool:
    """**精确**判断是否为 completion 接口。

    必须精确匹配路径:豆包还有 ``/chat/completion/action`` 等同前缀的接口,
    用子串判断会把它的响应当成目标响应(实测表现为"SSE 解析结果为空")。
    """
    try:
        return urlparse(url).path == config.DOUBAO_COMPLETION_API
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# 拟人化行为
# ---------------------------------------------------------------------------
async def simulate_human_behavior(page) -> None:
    """模拟真人浏览动作,降低被判定为自动化的概率。

    做三件低风险的事:轻微滚动、随机停顿、把鼠标移到页面上随机位置。
    不会点击任何业务按钮,避免误触。
    """
    try:
        # 轻微滚动(模拟人在看页面)
        await page.evaluate(
            f"window.scrollTo(0, {random.randint(80, 420)});"
        )
        await asyncio.sleep(config.random_action_delay())

        # 鼠标移动到随机位置
        viewport = config.VIEWPORT
        try:
            await page.mouse.move(
                random.randint(100, max(120, viewport["width"] - 100)),
                random.randint(100, max(120, viewport["height"] - 100)),
                steps=random.randint(5, 15),
            )
        except Exception:  # noqa: BLE001
            pass

        await asyncio.sleep(config.random_action_delay())
    except Exception as exc:  # noqa: BLE001
        logger.debug("拟人化行为执行失败(忽略): %s", exc)


# UI 交互模式下用于定位输入框的候选选择器(豆包当前用 contenteditable div)
EDITOR_SELECTORS = (
    "div[contenteditable='true']",
    "[contenteditable='true']",
    "textarea[data-testid='chat_input_input']",
    "textarea",
)


async def find_editor(page, timeout: int = 30000):
    """定位聊天输入框,返回元素;未找到返回 None。"""
    per_selector = max(timeout // len(EDITOR_SELECTORS), 3000)
    for selector in EDITOR_SELECTORS:
        try:
            element = await page.wait_for_selector(
                selector, timeout=per_selector, state="visible"
            )
            if element:
                return element
        except Exception:  # noqa: BLE001
            continue
    return None


# 可能遮挡输入框的弹窗:按钮文案候选
POPUP_CLOSE_TEXTS = (
    "下载豆包电脑版",
    "下载电脑版",
    "电脑版客户端",
    "稍后再说",
    "以后再说",
    "我知道了",
    "暂不",
    "取消",
)


async def dismiss_popups(page) -> int:
    """关闭可能遮挡输入框的弹窗(下载引导 / 新手引导等)。

    豆包在**首次访问或新会话**时可能弹出"下载豆包电脑版"之类的窗口,不关掉
    会导致输入框不可点击、提问发不出去。这里按"关闭按钮 → 文案按钮 → ESC"
    三级兜底清理,并返回关闭数量。
    """
    closed = 0
    try:
        # 1) dialog / modal 内的关闭按钮(右上角 X)
        for selector in (
            "[role='dialog'] button[aria-label*='关闭']",
            "[role='dialog'] [class*='close']",
            "[class*='modal'] [class*='close']",
            "[class*='Modal'] [class*='close']",
        ):
            try:
                for element in await page.query_selector_all(selector):
                    if await element.is_visible():
                        await element.click(timeout=1500)
                        closed += 1
                        await asyncio.sleep(0.3)
            except Exception:  # noqa: BLE001
                continue

        # 2) 按文案找"稍后/关闭/取消"类按钮
        for text in POPUP_CLOSE_TEXTS:
            try:
                locator = page.locator(
                    f"button:has-text('{text}'), [role='button']:has-text('{text}')"
                ).first
                if await locator.count() and await locator.is_visible():
                    await locator.click(timeout=1500)
                    closed += 1
                    await asyncio.sleep(0.3)
            except Exception:  # noqa: BLE001
                continue

        # 3) ESC 兜底
        if closed:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.3)
    except Exception as exc:  # noqa: BLE001
        logger.debug("关闭弹窗出错(忽略): %s", exc)

    if closed:
        logger.info("已关闭 %d 个引导弹窗", closed)
    return closed


async def interact_and_capture(page, word: str, timeout_seconds: int) -> str:
    """驱动页面输入并发送,捕获豆包自己发出的 completion 响应文本。

    为什么必须这样做:completion 接口 URL 上带 ``a_bogus`` / ``msToken`` /
    ``device_id`` 等由页面 JS 动态计算的签名参数,脚本无法构造 ——
    只有让豆包前端自己发请求才能拿到合法签名。

    :return: 完整的 SSE 响应文本。
    """
    captured: dict[str, Any] = {}
    pending: list[asyncio.Task] = []
    done = asyncio.Event()

    async def handle_response(response) -> None:
        # 必须精确匹配路径:否则 /chat/completion/action 等子路径会被误认,
        # 拿到空 body 后就提前结束等待(实测表现为"SSE 解析结果为空")。
        if not is_completion_url(response.url):
            return
        try:
            body = await response.body()
            # 豆包 SSE 存在 cp1252 双重编码,需先还原再解析
            text = utils.repair_mojibake(body.decode("utf-8", errors="replace"))
            if not text:
                # 空响应(可能是预检/子路径),不算命中,继续等真正的响应
                logger.debug("忽略空响应: %s", response.url[:120])
                return
            captured["text"] = text
            captured["status"] = response.status
            logger.debug(
                "捕获 completion 响应: status=%s len=%d", response.status, len(text)
            )
        except Exception as exc:  # noqa: BLE001
            captured["error"] = f"{type(exc).__name__}: {exc}"
            logger.warning("读取 completion 响应失败: %s", exc)
        finally:
            # 只有真正拿到文本才算命中,避免空响应提前结束等待
            if "text" in captured:
                done.set()

    def on_response(response) -> None:
        pending.append(asyncio.create_task(handle_response(response)))

    def on_request(request) -> None:
        if is_completion_url(request.url):
            logger.info("检测到 completion 请求: %s", request.url[:140])

    page.on("response", on_response)
    page.on("request", on_request)
    try:
        # 先清理可能遮挡输入框的引导弹窗(如"下载豆包电脑版")
        await dismiss_popups(page)

        # 定位输入框并输入。页面可能正在重渲染导致句柄失效
        # (实测 ElementHandle.fill: Element is not attached to the DOM),故重试一次。
        for attempt in range(2):
            editor = await find_editor(page)
            if editor is None:
                raise RuntimeError("未找到输入框,页面结构可能已变更")
            try:
                # 拟人化:先点输入框 → 停顿 → 输入
                await editor.click()
                await asyncio.sleep(config.random_action_delay())
                await editor.fill(word)
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == 0:
                    logger.warning("输入框句柄失效,重新定位后重试: %s", exc)
                    await asyncio.sleep(config.random_action_delay() + 1.5)
                    continue
                raise

        await asyncio.sleep(config.random_action_delay())
        await page.keyboard.press("Enter")
        logger.info("已发送查询词: %s", word)

        try:
            await asyncio.wait_for(done.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(f"等待 completion 响应超时({timeout_seconds}s)") from exc
    finally:
        for task in pending:
            task.cancel()
        for listener, name in ((on_response, "response"), (on_request, "request")):
            try:
                page.remove_listener(name, listener)
            except Exception:  # noqa: BLE001
                pass

    if "text" not in captured:
        raise RuntimeError(f"未捕获到响应文本: {captured.get('error', 'unknown')}")
    return captured["text"]


# ---------------------------------------------------------------------------
# 主处理流程(CDP)
# ---------------------------------------------------------------------------
async def process_task(
    playwright,
    task: dict[str, Any],
    account: dict[str, Any],
    pool: AccountPool,
    *,
    auto_launch: bool | None = None,
) -> dict[str, Any]:
    """处理单个任务:CDP 连接已运行的 Chrome → UI 交互采集 → 落盘。

    :param playwright: ``async_playwright()`` 上下文对象(不是 browser)。
    :param task: 任务字典,至少含 ``task_id`` / ``word``。
    :param account: 账号字典,需含 ``cdp_port`` 与 ``user_data_dir``。
    :param auto_launch: 实例未运行时是否自动拉起(默认取 config)。
    """
    task_id = task["task_id"]
    word = task["word"]
    account_id = account.get("account_id")
    port = account.get("cdp_port")
    started = time.monotonic()

    if not port:
        raise RuntimeError(f"账号 {account_id} 未配置 cdp_port,无法使用 CDP 模式")

    should_launch = config.AUTO_LAUNCH_CHROME if auto_launch is None else auto_launch

    # --- 1. 确保 Chrome 实例在运行 ---
    if not cdp.is_port_open(port):
        if not should_launch:
            raise RuntimeError(f"Chrome 实例未运行且未启用自动拉起(端口 {port})")

        profile = str(account_store.profile_dir(account))
        logger.info("端口 %d 无实例,尝试自动拉起 Chrome(%s)", port, account_id)
        ok = cdp.launch_chrome(
            port=port,
            user_data_dir=profile,
            proxy=account.get("proxy"),
            start_url=config.DOUBAO_CHAT_URL,
        )
        if not ok:
            raise RuntimeError(f"Chrome 实例启动失败(端口 {port})")

    # --- 2. CDP 连接并复用已有上下文 ---
    browser, context = await cdp.connect_over_cdp(playwright, port)
    logger.info("已连接 CDP(端口 %d,上下文 %d 个)", port, len(browser.contexts))

    page = None
    try:
        # --- 3. 定位豆包页面 ---
        page = await cdp.find_or_open_doubao_page(context)
        logger.info("使用页面: %s", page.url)

        # --- 4. 拟人化 + 采集 ---
        await simulate_human_behavior(page)
        sse_text = await interact_and_capture(page, word, config.UI_RESPONSE_TIMEOUT)

        if not sse_text:
            raise RuntimeError("SSE 响应为空(可能被风控拦截或页面结构变更)")

        if config.SAVE_RAW_SSE:
            raw_path = config.DATA_DIR / "raw_sse" / utils.safe_name(task_id, ".txt")
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(raw_path.write_text, sse_text, "utf-8")

        # --- 5. 解析 SSE ---
        parser = DoubaoSseParser()
        parsed = parser.parse_text(sse_text)

        if parsed.get("rate_limited"):
            raise RateLimitedError("豆包限流(rate limited),账号需较长冷却")
        if not parsed["markdown"] and not parsed["reference_links"]:
            raise RuntimeError("SSE 解析结果为空(可能被风控拦截或页面结构变更)")

        # --- 6. 截图(CDP 模式下页面由真实浏览器渲染,直接截即可)---
        screenshot_path = await screenshot_conversation(page, task_id, wait_timeout=8000)

        # --- 7. 落盘 ---
        markdown_path = await asyncio.to_thread(
            utils.save_markdown, task_id, parsed["markdown"]
        )
        elapsed = time.monotonic() - started
        record = {
            "task_id": task_id,
            "word": word,
            "account_id": account_id,
            "cdp_port": port,
            "success": True,
            "has_reference": parsed["has_reference"],
            "reference_count": len(parsed["reference_links"]),
            "reference_links": parsed["reference_links"],
            "image_urls": parsed["image_urls"],
            "event_counts": parsed["event_counts"],
            "markdown_file": str(markdown_path),
            "screenshot_file": str(screenshot_path) if screenshot_path else "",
            "elapsed": round(elapsed, 2),
            "created_at": time.time(),
        }
        await utils.append_result(record)
        logger.info(
            "任务完成 %s | 词=%s | 参考链接=%d | 耗时=%.1fs",
            task_id,
            word,
            record["reference_count"],
            elapsed,
        )
        return record
    finally:
        # 只关页面,不关 browser —— browser.close() 会连带关闭整个 Chrome 实例。
        # 另外:如果这是 Chrome 的**最后一个标签页**,关掉它会让实例直接退出
        # (实测每轮任务后实例都消失,下次得重新拉起),所以先补一个空白页兜底。
        if page is not None:
            try:
                if len(context.pages) <= 1:
                    await context.new_page()
                await page.close()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Worker 循环
# ---------------------------------------------------------------------------
async def _handle_task(
    playwright,
    task_queue: RedisTaskQueue,
    account_pool: AccountPool,
    task: dict[str, Any],
) -> bool:
    """领取账号并处理任务,统一处理重试与结果上报。

    :return: True 表示领取到账号并处理过;False 表示当前无可用账号。
    """
    account = await account_pool.acquire_account()
    if account is None:
        logger.warning("暂无可用账号,等待 %d 秒后重新入队", config.NO_ACCOUNT_WAIT)
        await asyncio.sleep(config.NO_ACCOUNT_WAIT)
        await task_queue.requeue(task)
        return False

    account_id = account.get("account_id")
    try:
        result = await asyncio.wait_for(
            process_task(playwright, task, account, account_pool),
            timeout=config.TASK_TIMEOUT,
        )
    except RateLimitedError as exc:
        logger.error("任务 %s 被豆包限流: %s", task.get("task_id"), exc)
        # 限流是账号/出口级状态,直接长冷却,不要靠连续失败累积
        await account_pool.move_to_cooling(
            account_id, config.RATE_LIMIT_COOLING_TIME, reason="豆包限流(rate limited)"
        )
        await task_queue.retry(task, reason=str(exc))
        if config.STOP_ON_RATE_LIMIT:
            raise StopTestSignal(str(exc)) from exc
        return True
    except Exception as exc:  # noqa: BLE001 - 网络/超时/解析异常统一处理
        logger.error("任务 %s 处理失败: %s", task.get("task_id"), exc)
        await account_pool.report_result(
            account_id, has_reference=False, success=False
        )
        await task_queue.retry(task, reason=str(exc))
        return True

    await account_pool.report_result(
        account_id,
        has_reference=bool(result.get("has_reference")),
        success=True,
    )
    await task_queue.ack(task)
    return True


async def worker(
    task_queue: RedisTaskQueue,
    account_pool: AccountPool,
    playwright,
    worker_id: int = 1,
    semaphore: asyncio.Semaphore | None = None,
) -> None:
    """Worker 主循环:持续从队列取任务并处理(CDP 模式)。"""
    sem = semaphore or asyncio.Semaphore(config.MAX_CONCURRENT_PAGES)
    empty_rounds = 0
    no_account_rounds = 0
    logger.info("Worker %d 启动(CDP 模式)", worker_id)

    while True:
        task = await task_queue.pop()
        if task is None:
            # 队列为空时不能立即退出:可能有其它 Worker 正在处理 running 中的任务
            sizes = await task_queue.size()
            if sizes["pending"] == 0 and sizes["running"] == 0:
                empty_rounds += 1
                if empty_rounds >= 3:
                    logger.info("Worker %d 队列已清空,退出", worker_id)
                    return
            else:
                empty_rounds = 0
            await asyncio.sleep(2)
            continue

        empty_rounds = 0
        async with sem:
            try:
                handled = await _handle_task(
                    playwright, task_queue, account_pool, task
                )
            except StopTestSignal as exc:
                logger.warning(
                    "Worker %d 收到停止信号(已触发限流),结束测试: %s", worker_id, exc
                )
                return

        if handled:
            no_account_rounds = 0
            # 任务之间的间隔:
            #   配了阶梯(DOUBAO_INTERVAL_LADDER)→ 按已完成任务数递减,用于试探最小安全间隔;
            #   否则用 TASK_INTERVAL_MIN/MAX 的随机区间。
            # 队列已空时不再等待。
            if config.TASK_INTERVAL_MAX > 0:
                remaining = await task_queue.size()
                if remaining["pending"] > 0:
                    done_count = remaining.get("done", 0)
                    ladder_wait = config.ladder_interval(done_count)
                    if ladder_wait is None:
                        wait = config.random_task_interval()
                        logger.info("任务间隔:等待 %.1f 秒后继续(随机)", wait)
                    else:
                        wait = ladder_wait
                        logger.info(
                            "任务间隔:等待 %.1f 秒后继续(阶梯档位,已完成 %d 个)",
                            wait,
                            done_count,
                        )
                    await asyncio.sleep(wait)
        else:
            no_account_rounds += 1
            if no_account_rounds >= config.MAX_NO_ACCOUNT_ROUNDS:
                logger.warning(
                    "Worker %d 连续 %d 轮无可用账号,退出(任务已回队,下次启动可续跑)",
                    worker_id,
                    no_account_rounds,
                )
                return
