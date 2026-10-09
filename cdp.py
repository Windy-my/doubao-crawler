"""CDP 模式核心:连接**已运行的真实 Chrome 实例**,并提供启动 / 停止 / 验证能力。

为什么改用 CDP:
    Playwright 自己 ``launch()`` 出来的 Chromium 带有明显的自动化特征 ——
    ``navigator.webdriver``、CDP ``Runtime.enable`` 泄露、无头指纹、缺少真实浏览器
    的扩展与 WebGL/Canvas 指纹。豆包风控能直接识别,表现为"同一账号在 Edge 里
    正常,一换成 Playwright 就被限流"。

    改法:让 Chrome 以真实方式启动(独立 User Data Dir + 调试端口),Playwright 只
    通过 ``connect_over_cdp`` 连上去复用它的上下文,指纹与真人浏览器一致。

浏览器后端:
    优先使用 **patchright**(Playwright 的 drop-in 替代,修复了 CDP 上的
    Runtime.enable 泄露);未安装时自动回退到 playwright,代码无需改动。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import config

logger = logging.getLogger("doubao.cdp")

BASE_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# 浏览器后端:patchright → playwright 回退
# ---------------------------------------------------------------------------
BACKEND_NAME = "playwright"
if config.PREFER_PATCHRIGHT:
    try:
        from patchright.async_api import async_playwright  # type: ignore

        BACKEND_NAME = "patchright"
    except ImportError:
        BACKEND_NAME = "playwright"

if BACKEND_NAME == "playwright":
    from playwright.async_api import async_playwright  # type: ignore  # noqa: F811

__all__ = [
    "async_playwright",
    "BACKEND_NAME",
    "find_chrome_path",
    "is_port_open",
    "pick_free_port",
    "build_chrome_args",
    "launch_chrome",
    "stop_chrome_for_profile",
    "connect_over_cdp",
    "verify_cdp_connection",
    "CDPVerificationError",
]


class CDPVerificationError(RuntimeError):
    """CDP 验证门未通过。"""


# ---------------------------------------------------------------------------
# Chrome 可执行文件探测
# ---------------------------------------------------------------------------
CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def find_chrome_path() -> str | None:
    """定位浏览器可执行文件:配置优先,其次自动探测 Chrome → Edge。"""
    if config.CHROME_PATH and Path(config.CHROME_PATH).exists():
        return config.CHROME_PATH

    for candidate in CHROME_CANDIDATES:
        if candidate and Path(candidate).exists():
            return candidate
    return None


# ---------------------------------------------------------------------------
# 端口工具
# ---------------------------------------------------------------------------
def is_port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    """检测端口是否已在监听。"""
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((host, port)) == 0
    finally:
        sock.close()


def pick_free_port(start: int | None = None) -> int:
    """从起始端口向后找一个空闲端口。"""
    port = start or config.CDP_PORT_BASE
    while port < 65535:
        if not is_port_open(port):
            return port
        port += 1
    raise RuntimeError("找不到可用的调试端口")


# ---------------------------------------------------------------------------
# Chrome 启动 / 停止
# ---------------------------------------------------------------------------
def build_chrome_args(
    chrome_path: str,
    port: int,
    user_data_dir: str,
    proxy: dict[str, Any] | None = None,
    lang: str = "zh-CN",
    start_url: str | None = None,
) -> list[str]:
    """构造 Chrome 启动参数。

    .. note::
       **代理必须在启动时设置**(``--proxy-server``)。CDP 连接的是已运行的
       Chrome,连上之后无法再修改代理 —— 这一点与 Playwright 的 ``proxy``
       参数不同。需要认证的代理,Chrome 命令行不支持直接传凭据,请改用
       免认证代理、PAC 脚本或代理认证扩展。
    """
    args = [
        chrome_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        # 去掉"Chrome 正受到自动化软件控制"的提示条
        "--disable-blink-features=AutomationControlled",
        # 首次运行 / 默认浏览器检查等噪音
        "--no-first-run",
        "--no-default-browser-check",
        "--no-service-autorun",
        "--disable-search-engine-choice-screen",
        # 与代理 IP 的地理位置保持一致(geoip 匹配)
        f"--lang={lang}",
    ]

    if proxy and proxy.get("server"):
        args.append(f"--proxy-server={proxy['server']}")
        if proxy.get("username") or proxy.get("password"):
            logger.warning(
                "代理 %s 配置了账号密码,但 Chrome 命令行不支持内联凭据,"
                "请改用免认证代理 / PAC / 代理认证扩展(否则会认证失败)",
                proxy["server"],
            )
        if config.CHROME_PROXY_BYPASS:
            args.append(f"--proxy-bypass-list={config.CHROME_PROXY_BYPASS}")

    if start_url:
        args.append(start_url)
    return args


def _spawn_detached(args: list[str]) -> None:
    """以**完全脱离父进程**的方式启动浏览器。

    直接用 ``subprocess.Popen`` 启动的进程仍属于当前进程组,脚本或终端退出时
    可能被连带清理(实测:通过后台任务启动时,任务一结束 Chrome 就被杀掉了)。
    这里改用 ``Start-Process``(Windows)/ ``start_new_session``(POSIX),
    让浏览器独立存活,登录态才能长期保留。
    """
    if sys.platform == "win32":
        exe = args[0].replace("'", "''")
        quoted = ",".join("'" + a.replace("'", "''") + "'" for a in args[1:])
        script = (
            f"Start-Process -FilePath '{exe}' -ArgumentList @({quoted})"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            timeout=60,
        )
        return

    subprocess.Popen(  # noqa: S603
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def launch_chrome(
    port: int,
    user_data_dir: str | Path,
    proxy: dict[str, Any] | None = None,
    lang: str = "zh-CN",
    start_url: str = "https://www.doubao.com/chat/",
    wait_seconds: float = 10.0,
) -> bool:
    """以调试端口启动一个 Chrome 实例(独立 User Data Dir)。

    :return: True 表示调试端口已就绪(含"已运行直接复用"的情况)。
    """
    if is_port_open(port):
        logger.info("端口 %d 已在监听,复用现有 Chrome 实例", port)
        return True

    chrome_path = find_chrome_path()
    if not chrome_path:
        raise RuntimeError(
            "未找到 Chrome/Edge 可执行文件,请设置环境变量 DOUBAO_CHROME_PATH"
        )

    profile = Path(user_data_dir)
    profile.mkdir(parents=True, exist_ok=True)

    args = build_chrome_args(chrome_path, port, str(profile), proxy, lang, start_url)
    logger.info("启动 Chrome: 端口=%d profile=%s", port, profile)
    logger.debug("启动参数: %s", " ".join(args))

    _spawn_detached(args)

    # 等待调试端口就绪
    import time

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if is_port_open(port):
            logger.info("Chrome 已就绪(端口 %d)", port)
            return True
        time.sleep(0.5)

    logger.warning("等待 Chrome 调试端口 %d 超时(可稍后用 --status 复查)", port)
    return False


def stop_chrome_for_profile(user_data_dir: str | Path) -> bool:
    """按 User Data Dir 结束对应 Chrome 进程(不会影响其它 Chrome 窗口)。"""
    profile = str(Path(user_data_dir).resolve())
    if sys.platform != "win32":
        return False

    escaped = profile.replace("'", "''")
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' or Name='msedge.exe'\" "
        "| Where-Object { $_.CommandLine -like '*--user-data-dir=*' } "
        f"| Where-Object {{ $_.CommandLine -like '*{escaped}*' }} "
        "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            timeout=30,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("停止 Chrome 失败: %s", exc)
        return False


# ---------------------------------------------------------------------------
# CDP 连接与验证门
# ---------------------------------------------------------------------------
async def connect_over_cdp(playwright, port: int):
    """连接到指定端口的 Chrome,返回 (browser, context)。

    **不创建新的 context** —— 复用浏览器里已有的那个,才能带上真实登录态与指纹。
    """
    url = f"http://127.0.0.1:{port}"
    browser = await playwright.chromium.connect_over_cdp(
        url, timeout=config.CDP_CONNECT_TIMEOUT * 1000
    )
    if not browser.contexts:
        raise CDPVerificationError(f"CDP 连接成功但没有可用上下文: {url}")
    return browser, browser.contexts[0]


async def find_or_open_doubao_page(context, timeout_ms: int = 30000):
    """在上下文中找到豆包页面;没有就打开一个。"""
    for page in context.pages:
        if "doubao.com" in page.url:
            return page

    page = await context.new_page()
    await page.goto(
        config.DOUBAO_CHAT_URL, wait_until="domcontentloaded", timeout=timeout_ms
    )
    # 给页面 JS 一点初始化时间(签名逻辑挂载)
    await asyncio.sleep(config.random_action_delay() + 2)
    return page


async def verify_cdp_connection(
    browser,
    context,
    expected_port: int,
    *,
    page: Any = None,
    check_login: bool = True,
    check_ip: bool = True,
) -> dict[str, Any]:
    """CDP 连接验证门:确认连到的是正确、已登录、可用出口的浏览器实例。

    Gate 1  端点与上下文存在
    Gate 2  能拿到页面,并定位到豆包页面
    Gate 3  登录态有效(检查 sessionid,HttpOnly 也能读到)
    Gate 4  出口 IP(在页面内 fetch,拿到的是浏览器真实出口)

    :return: 验证结果摘要字典。
    """
    result: dict[str, Any] = {"port": expected_port, "backend": BACKEND_NAME}

    # ---- Gate 1:端点与上下文 ----
    try:
        result["browser_version"] = browser.version
    except Exception:  # noqa: BLE001
        result["browser_version"] = "unknown"
    context_count = len(browser.contexts)
    result["context_count"] = context_count
    logger.info(
        "[CDP] 端口 %d | 内核 %s | 上下文 %d",
        expected_port,
        result["browser_version"],
        context_count,
    )
    if context_count == 0:
        raise CDPVerificationError("CDP 连接成功但无可用上下文")

    # ---- Gate 2:定位豆包页面 ----
    doubao_page = page or await find_or_open_doubao_page(context)
    result["page_url"] = doubao_page.url
    logger.info("[CDP] 页面: %s", doubao_page.url)

    # ---- Gate 3:登录态(sessionid 是 HttpOnly,必须用 cookies() 读)----
    is_logged_in = False
    if check_login:
        try:
            cookies = await context.cookies("https://www.doubao.com")
            names = {c.get("name") for c in cookies}
            is_logged_in = bool({"sessionid", "sessionid_ss"} & names)
            result["cookie_count"] = len(cookies)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[CDP] 读取 cookie 失败: %s", exc)
    result["logged_in"] = is_logged_in
    logger.info("[CDP] 登录态: %s", "已登录" if is_logged_in else "未登录")

    # ---- Gate 4:出口 IP ----
    # 在页面内 fetch,拿到的才是**浏览器真实的出口**(有代理时也准确)。
    # 多服务轮询:api.ipify.org 在中国大陆常被墙,所以补了其它来源。
    if check_ip:
        try:
            ip_info = await doubao_page.evaluate(
                """async () => {
                    const services = [
                        'https://api.ipify.org?format=json',
                        'https://ipinfo.io/json',
                        'https://api.ip.sb/geoip',
                        'https://api.myip.com',
                    ];
                    for (const url of services) {
                        try {
                            const r = await fetch(url);
                            if (!r.ok) continue;
                            const d = await r.json();
                            if (d && d.ip) return d;
                        } catch (e) { /* 换下一个服务 */ }
                    }
                    return { ip: null };
                }"""
            )
            result["exit_ip"] = (ip_info or {}).get("ip")
        except Exception as exc:  # noqa: BLE001
            result["exit_ip"] = None
            logger.warning("[CDP] 出口 IP 检查失败: %s", exc)
        logger.info("[CDP] 出口 IP: %s", result.get("exit_ip") or "未知(可忽略)")

    return result


@contextlib.asynccontextmanager
async def cdp_browser(port: int):
    """便捷上下文:连接 CDP,退出时**只断开连接、不关闭 Chrome**。

    .. warning::
       CDP 模式下调用 ``browser.close()`` 会**关闭整个 Chrome 实例**,并且
       Playwright 没有提供 ``disconnect()``。因此这里刻意不调用 ``close()`` ——
       直接退出 ``async_playwright()`` 上下文即可,Playwright 会释放连接,
       而 Chrome 进程继续运行,登录态得以保留。
    """
    async with async_playwright() as playwright:
        browser, context = await connect_over_cdp(playwright, port)
        yield browser, context
        # 故意不调用 browser.close():
        # 那会连带关闭 Chrome 实例,丢掉登录态。让 playwright 上下文退出即可。
