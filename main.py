"""入口:初始化 Redis、账号池、任务队列,加载词表,连接已运行的 Chrome 采集。

**CDP 模式**:不再由 Playwright 启动浏览器,而是连接**已运行的真实 Chrome 实例**
(每个账号一个,独立 User Data Dir + 调试端口),从而避开自动化指纹检测 ——
Playwright 自启的 Chromium 会暴露 ``navigator.webdriver`` 与 CDP 特征,易被限流。

运行::

    # 1) 先启动各账号的 Chrome(也可让采集时自动拉起)
    python scripts/launch_chrome.py --all

    # 2) 开始采集
    python main.py
    python main.py --clear             # 先清空旧队列
    python main.py --words D:\\x.txt    # 指定词表
    python main.py --no-autolaunch     # 实例没运行时不自动拉起,直接报错
    python main.py --admin             # 附带启动 FastAPI 管理端
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis

import account_store
import cdp
import config
import utils
from account_pool import AccountPool
from degradation_detector import DegradationDetector
from task_queue import RedisTaskQueue
from worker import worker

logger = logging.getLogger("doubao.main")

# 降权巡检的周期间隔(秒)
DETECTOR_INTERVAL = 300


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="doubao-crawler",
        description="豆包网页版批量问答采集:正文 Markdown + 参考链接 + 截图",
    )
    parser.add_argument("--words", type=Path, default=config.WORDS_FILE, help="词表文件")
    parser.add_argument(
        "--accounts",
        type=Path,
        default=account_store.ACCOUNTS_FILE,
        help="账号配置文件(CDP 模式下为含 cdp_port 的 accounts.json)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=config.MAX_CONCURRENT_PAGES,
        help="Worker 数量(默认等于 MAX_CONCURRENT_PAGES)",
    )
    parser.add_argument("--clear", action="store_true", help="开始前清空所有队列")
    parser.add_argument(
        "--no-enqueue",
        action="store_true",
        help="不把词表推入队列(仅消费已有任务,用于断点续爬)",
    )
    parser.add_argument(
        "--no-autolaunch",
        action="store_true",
        help="Chrome 实例未运行时不要自动拉起(跳过该账号)",
    )
    parser.add_argument("--admin", action="store_true", help="启动 FastAPI 管理端")
    parser.add_argument("--admin-host", default="127.0.0.1", help="管理端监听地址")
    parser.add_argument("--admin-port", type=int, default=8000, help="管理端端口")
    parser.add_argument("--log-level", default=config.LOG_LEVEL, help="日志级别")
    return parser


# ---------------------------------------------------------------------------
# 初始化
# ---------------------------------------------------------------------------
async def create_redis() -> aioredis.Redis:
    """建立并测试 Redis 连接。"""
    client: aioredis.Redis = aioredis.Redis(
        host=config.REDIS_HOST,
        port=config.REDIS_PORT,
        db=config.REDIS_DB,
        password=config.REDIS_PASSWORD,
        decode_responses=False,
        socket_connect_timeout=10,
        health_check_interval=30,
    )
    try:
        await client.ping()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "无法连接 Redis %s:%s —— 请先启动 Redis。错误: %s",
            config.REDIS_HOST,
            config.REDIS_PORT,
            exc,
        )
        raise
    logger.info("Redis 已连接: %s:%s/%s", config.REDIS_HOST, config.REDIS_PORT, config.REDIS_DB)
    return client


# 冷却恢复后所需"温柔期"请求次数由 config.RECOVERY_REQUESTS 控制


async def prepare_inputs(
    queue: RedisTaskQueue,
    pool: AccountPool,
    args: argparse.Namespace,
) -> None:
    """加载账号(CDP 格式)并注册,加载词表并推入队列。"""
    # --- 账号:CDP 模式下从 data/accounts.json 读取(自动兼容旧格式)---
    # 只注册 status != disabled 的账号,便于把某个账号临时排除在测试外
    accounts = [
        a
        for a in account_store.load_accounts()
        if a.get("status") != account_store.STATUS_DISABLED
    ]
    if accounts:
        count = await pool.register_from_config(accounts)
        logger.info("已注册 %d 个账号(CDP 模式)", count)
        for account in accounts:
            logger.info(
                "   - %s  端口=%s  profile=%s  代理=%s",
                account["account_id"],
                account["cdp_port"],
                account["user_data_dir"],
                (account.get("proxy") or {}).get("server") or "未绑定",
            )
    else:
        logger.warning(
            "没有配置任何账号,请先运行 scripts/manage_accounts.py 添加账号"
        )

    # --- 词表 ---
    if not args.no_enqueue:
        words = utils.load_words(Path(args.words))
        if words:
            await queue.push_many(words)
        else:
            logger.warning("词表为空,没有任务入队")


# ---------------------------------------------------------------------------
# 后台任务
# ---------------------------------------------------------------------------
async def periodic_detector(
    detector: DegradationDetector, interval: int = DETECTOR_INTERVAL
) -> None:
    """周期执行降权巡检与冷却恢复,异常不中断循环。"""
    logger.info("降权巡检已启动,周期 %d 秒", interval)
    while True:
        try:
            await asyncio.sleep(interval)
            result = await detector.check_and_rotate()
            logger.info(
                "降权巡检: 降权 %d,恢复 %d", result["degraded"], result["reactivated"]
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("降权巡检异常(将继续运行): %s", exc)


async def start_admin_api(
    pool: AccountPool,
    queue: RedisTaskQueue,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> None:
    """可选的 FastAPI 管理端,提供账号池与队列状态查询。"""
    try:
        import uvicorn
        from fastapi import FastAPI
    except ImportError:
        logger.warning("未安装 fastapi/uvicorn,跳过管理端启动")
        return

    app = FastAPI(title="doubao-crawler admin", version="1.0.0")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/stats")
    async def stats() -> dict[str, Any]:
        return {"accounts": await pool.stats(), "tasks": await queue.size()}

    @app.get("/accounts")
    async def accounts() -> dict[str, list[str]]:
        return {
            "active": await pool.list_active_ids(),
            "cooling": await pool.list_cooling_ids(),
        }

    @app.get("/accounts/{account_id}")
    async def account_detail(account_id: str) -> dict[str, Any]:
        info = await pool.get_account(account_id)
        if not info:
            return {"error": "not found", "account_id": account_id}
        # cookies 体积大且敏感,不回传
        info.pop("cookies", None)
        return info

    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level="warning")
    )
    logger.info("管理端已启动: http://%s:%d", host, port)
    await server.serve()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
async def run(args: argparse.Namespace) -> None:
    """程序主入口。"""
    utils.setup_logging(args.log_level)
    redis_client = await create_redis()
    pool = AccountPool(redis_client)
    queue = RedisTaskQueue(redis_client)

    try:
        if args.clear:
            await queue.clear()

        await prepare_inputs(queue, pool, args)

        # 断点续爬:把上次崩溃遗留的超时任务重新入队
        await queue.recover()
        logger.info("队列状态: %s", await queue.size())

        # 后台降权巡检
        detector = DegradationDetector(pool, redis_client)
        background: list[asyncio.Task] = [
            asyncio.create_task(periodic_detector(detector))
        ]
        if args.admin:
            background.append(
                asyncio.create_task(
                    start_admin_api(pool, queue, args.admin_host, args.admin_port)
                )
            )

        worker_count = max(1, args.workers)
        auto_launch = not args.no_autolaunch

        logger.info(
            "CDP 模式 | 后端=%s | 浏览器=%s",
            cdp.BACKEND_NAME,
            cdp.find_chrome_path() or "(未找到,可设置 DOUBAO_CHROME_PATH)",
        )

        async with cdp.async_playwright() as playwright:
            # CDP 模式:**不启动浏览器**,只连接已运行的 Chrome 实例。
            # 实例由 scripts/launch_chrome.py 管理,也可在这里按需自动拉起。
            # 只使用 status != disabled 的账号(便于把某个账号临时排除在测试外)。
            accounts = [
                a
                for a in account_store.load_accounts()
                if a.get("status") != account_store.STATUS_DISABLED
            ]
            logger.info(
                "参与本次采集的账号: %s",
                [a["account_id"] for a in accounts] or "(无)",
            )

            if auto_launch:
                for account in accounts:
                    port = account["cdp_port"]
                    if cdp.is_port_open(port):
                        continue
                    logger.info(
                        "拉起 Chrome 实例: %s (端口 %d)", account["account_id"], port
                    )
                    try:
                        cdp.launch_chrome(
                            port=port,
                            user_data_dir=account_store.profile_dir(account),
                            proxy=account.get("proxy"),
                            start_url=config.DOUBAO_CHAT_URL,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.error("   启动失败: %s", exc)

            # CDP 连接验证门:确认实例连通、已登录豆包(出口 IP 检查略过以加快启动)
            usable = 0
            for account in accounts:
                port = account["cdp_port"]
                if not cdp.is_port_open(port):
                    logger.warning(
                        "账号 %s 的 Chrome 实例未运行(端口 %d),该账号暂不可用",
                        account["account_id"],
                        port,
                    )
                    continue
                try:
                    async with cdp.cdp_browser(port) as (browser, context):
                        info = await cdp.verify_cdp_connection(
                            browser, context, port, check_ip=False
                        )
                    if info.get("logged_in"):
                        usable += 1
                    else:
                        logger.warning(
                            "账号 %s 未登录豆包,请先在该 Chrome 窗口里登录后再采集",
                            account["account_id"],
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("账号 %s 的 CDP 验证失败: %s", account["account_id"], exc)

            logger.info("可用账号: %d / %d", usable, len(accounts))

            # 共享并发闸门:限制同时打开的 Page 总数
            semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_PAGES)
            workers = [
                asyncio.create_task(
                    worker(
                        queue,
                        pool,
                        playwright,
                        worker_id=index + 1,
                        semaphore=semaphore,
                    )
                )
                for index in range(worker_count)
            ]

            try:
                await asyncio.gather(*workers)
            finally:
                if config.CLOSE_CHROME_ON_EXIT:
                    logger.info("配置要求退出时关闭 Chrome 实例 ...")
                    for account in accounts:
                        cdp.stop_chrome_for_profile(
                            account_store.profile_dir(account)
                        )
                else:
                    logger.info("Chrome 实例保持运行(登录态已保留,下次可直接连接)")

        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)

        logger.info("全部任务处理完毕。队列状态: %s", await queue.size())
        logger.info("账号池状态: %s", await pool.stats())
    finally:
        await redis_client.aclose()


def main() -> int:
    """同步包装,处理 Ctrl+C 退出。"""
    args = build_parser().parse_args()

    # Windows 的 Proactor 事件循环在 CDP 连接断开时会抛出无害告警
    # (Exception in callback _ProactorBasePipeTransport._call_connection_lost),
    # 这里把它静音,避免干扰正常日志。
    def _silence_proactor_noise(loop, context) -> None:
        handle = str(context.get("handle") or "")
        if "_ProactorBasePipeTransport" in handle or "_call_connection_lost" in handle:
            return
        loop.default_exception_handler(context)

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.set_exception_handler(_silence_proactor_noise)
        try:
            loop.run_until_complete(run(args))
        finally:
            loop.close()
    except KeyboardInterrupt:
        print("\n已手动中断。running 中的任务将在下次启动时由 recover() 重新入队。")
        return 130
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("doubao.main").error("启动失败: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
