"""运行前环境自检(CDP 模式)。

被 ``run.bat`` / ``manage_accounts.py`` 调用,逐项检查并输出可执行的修复建议。

检查内容:
    1. Python 版本
    2. 依赖(playwright / patchright / redis / aiohttp)
    3. Redis 连通性
    4. 浏览器内核(Chrome / Edge)
    5. 账号配置(含 cdp_port / user_data_dir)
    6. 各账号 Chrome 实例是否运行、CDP 是否连通、是否已登录
    7. 词表

退出码:
    0 —— 关键项全部就绪
    1 —— 存在阻塞项
"""

from __future__ import annotations

import asyncio
import importlib.util
import socket
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import account_store  # noqa: E402
import cdp  # noqa: E402

OK = "[OK]  "
WARN = "[!]   "
BAD = "[X]   "

blocking: list[str] = []
warnings: list[str] = []


def check_python() -> None:
    version = sys.version_info
    label = f"Python {version.major}.{version.minor}.{version.micro}"
    if version >= (3, 10):
        print(f"{OK}{label}")
    else:
        print(f"{BAD}{label} —— 需要 3.10 及以上")
        blocking.append("升级 Python 到 3.10+")


def check_deps() -> None:
    import config

    required = ["playwright", "redis", "aiohttp"]
    missing = [m for m in required if importlib.util.find_spec(m) is None]
    if missing:
        print(f"{BAD}缺少依赖: {', '.join(missing)}")
        blocking.append("运行 pip install -r requirements.txt")
    else:
        print(f"{OK}核心依赖就绪(playwright / redis / aiohttp)")

    print(f"{OK}CDP 后端: {cdp.BACKEND_NAME}")
    if importlib.util.find_spec("patchright") is None:
        print(
            f"{WARN}未安装 patchright(可选):它修复了 Playwright 的 CDP Runtime.enable 泄露"
        )
        warnings.append('可选:pip install patchright(装了会自动启用)')
    if importlib.util.find_spec("fakeredis") is None:
        print(f"{WARN}未安装 fakeredis(仅在无可用的真实 Redis 时需要)")
        warnings.append('可选:pip install "fakeredis[lua]"')


def check_redis() -> None:
    import config

    sock = socket.socket()
    sock.settimeout(2)
    try:
        if sock.connect_ex((config.REDIS_HOST, config.REDIS_PORT)) == 0:
            print(f"{OK}Redis 可连接: {config.REDIS_HOST}:{config.REDIS_PORT}")
            return
    finally:
        sock.close()
    print(f"{BAD}Redis 不可连接: {config.REDIS_HOST}:{config.REDIS_PORT}")
    blocking.append("启动 Redis,或运行 scripts\\dev_redis.py 使用开发用 Redis")


def check_chrome() -> str | None:
    path = cdp.find_chrome_path()
    if path:
        print(f"{OK}浏览器内核: {path}")
        return path
    print(f"{BAD}未找到 Chrome/Edge")
    blocking.append("安装 Chrome,或设置环境变量 DOUBAO_CHROME_PATH 指向浏览器 exe")
    return None


def check_accounts() -> list[dict]:
    all_accounts = account_store.load_accounts()
    accounts = [
        a
        for a in all_accounts
        if a.get("status") != account_store.STATUS_DISABLED
    ]
    disabled = len(all_accounts) - len(accounts)

    if not accounts:
        print(f"{BAD}没有可用的账号配置({account_store.ACCOUNTS_FILE})")
        blocking.append("运行 scripts\\manage_accounts.py 添加账号(菜单选 0)")
        return []

    suffix = f"(另有 {disabled} 个已禁用,本次跳过)" if disabled else ""
    print(f"{OK}可用账号:{len(accounts)} 个{suffix}")
    for account in accounts:
        print(
            f"       - {account['account_id']:<16} 端口={account['cdp_port']:<6} "
            f"profile={account['user_data_dir']}"
        )
    return accounts


async def _probe_instance(account: dict) -> bool:
    """检查单个实例:是否运行、CDP 是否连通、是否登录。"""
    account_id = account["account_id"]
    port = account["cdp_port"]

    if not cdp.is_port_open(port):
        print(f"{BAD}{account_id}: Chrome 实例未运行(端口 {port})")
        return False

    try:
        async with cdp.cdp_browser(port) as (browser, context):
            info = await cdp.verify_cdp_connection(
                browser, context, port, check_ip=False
            )
    except Exception as exc:  # noqa: BLE001
        print(f"{BAD}{account_id}: CDP 连接失败 {type(exc).__name__}: {exc}")
        return False

    if info.get("logged_in"):
        print(
            f"{OK}{account_id}: 实例运行中 · CDP 正常 · 已登录 "
            f"(内核 {info.get('browser_version')})"
        )
        return True

    print(f"{BAD}{account_id}: 实例运行中但**未登录豆包**")
    print(f"       请在该 Chrome 窗口里登录 {account_id} 对应的豆包账号")
    return False


async def _probe_all(accounts: list[dict]) -> list[bool]:
    return [await _probe_instance(account) for account in accounts]


def check_instances(accounts: list[dict]) -> None:
    if not accounts:
        return
    results = asyncio.run(_probe_all(accounts))
    offline = len(accounts) - sum(results)
    if offline:
        blocking.append(
            f"{offline} 个账号实例不可用 —— 运行 scripts\\launch_chrome.py --all 启动,"
            "并在弹出的窗口里登录"
        )


def check_words() -> None:
    import config

    path = Path(config.WORDS_FILE)
    if not path.exists():
        print(f"{BAD}词表不存在: {path}")
        blocking.append("创建 data/words.txt,每行一个待查询的词")
        return

    words = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if words:
        print(f"{OK}词表就绪:{len(words)} 个词 ({path.name})")
    else:
        print(f"{BAD}词表为空: {path}")
        blocking.append("往 data/words.txt 里写入待查询的词")


def main() -> int:
    print("-" * 58)
    print("环境自检(CDP 模式)")
    print("-" * 58)

    check_python()
    check_deps()
    check_redis()
    check_chrome()
    accounts = check_accounts()
    check_instances(accounts)
    check_words()

    print("-" * 58)
    if blocking:
        print(f"{BAD}发现 {len(blocking)} 个阻塞项:")
        for i, item in enumerate(blocking, 1):
            print(f"      {i}. {item}")
    if warnings:
        print(f"{WARN}可选优化:")
        for item in warnings:
            print(f"      - {item}")
    if not blocking:
        print(f"{OK}全部就绪,可以开始采集。")

    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
