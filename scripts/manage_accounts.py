"""豆包账号管理(CDP 模式):添加 / 删除 / 列表 / 启停实例 / 检查登录态。

不带参数运行即进入交互菜单::

    python scripts/manage_accounts.py

菜单:

    0) 添加账号(启动 Chrome 扫码登录)
    1) 删除账号
    2) 查看账号列表
    3) 启动所有账号的 Chrome 实例
    4) 停止所有 Chrome 实例
    5) 检查账号登录态与出口 IP
    q) 退出

CDP 模式的账号模型:
    每个账号 = 一个**独立 Chrome 实例**(独立 User Data Dir + 独立调试端口)。
    登录态保存在该 profile 里,由浏览器自己管理 —— 不再导出/注入 cookies。

也支持命令行方式(便于脚本化)::

    python scripts/manage_accounts.py --list
    python scripts/manage_accounts.py --add account_3 --proxy http://host:8080
    python scripts/manage_accounts.py --delete account_3 --purge-profile --yes
    python scripts/manage_accounts.py --check
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import account_store  # noqa: E402
import cdp  # noqa: E402
import config  # noqa: E402
import utils  # noqa: E402

SESSION_COOKIE_NAMES = ("sessionid", "sessionid_ss")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def parse_proxy(raw: str) -> dict | None:
    """把 ``http://user:pass@host:port`` 解析成 Playwright/Chrome 可用的代理字典。

    支持两种写法:
        ``http://host:port``               —— 直连型代理
        ``http://user:pass@host:port``     —— 带用户名密码的代理
    """
    if not raw:
        return None
    url = urlparse(raw)
    if not url.hostname:
        print(f"[!] 代理格式无法解析: {raw}")
        return None
    server = f"{url.scheme}://{url.hostname}"
    if url.port:
        server += f":{url.port}"
    proxy: dict[str, str] = {"server": server}
    if url.username:
        proxy["username"] = url.username
    if url.password:
        proxy["password"] = url.password
    return proxy


def is_valid_id(account_id: str) -> bool:
    """账号标识只允许字母、数字、下划线和短横线。"""
    return bool(account_id) and account_id.replace("-", "").replace("_", "").isalnum()


def describe_line(account: dict, *, check_state: bool = True) -> str:
    """把账号格式化成一行摘要。"""
    port = account["cdp_port"]
    proxy = (account.get("proxy") or {}).get("server") or "未绑定"
    state = ""
    if check_state:
        state = "实例=运行中" if cdp.is_port_open(port) else "实例=未运行"
    return (
        f"{account['account_id']:<16} 端口={port:<6} {state:<13} 代理={proxy}"
    )


# ---------------------------------------------------------------------------
# 0) 添加账号
# ---------------------------------------------------------------------------
async def wait_for_login(port: int, timeout: int) -> bool:
    """通过 CDP 连接轮询检测豆包登录态。"""
    deadline_seconds = timeout
    elapsed = 0
    step = 5
    while elapsed < deadline_seconds:
        await asyncio.sleep(step)
        elapsed += step
        try:
            async with cdp.cdp_browser(port) as (browser, context):
                info = await cdp.verify_cdp_connection(
                    browser, context, port, check_ip=False
                )
            if info.get("logged_in"):
                print(f"[+] 检测到登录态(用时约 {elapsed} 秒)")
                return True
        except Exception:  # noqa: BLE001
            # 连接失败通常是 Chrome 还没起好,继续等
            pass
        if elapsed % 15 == 0:
            print(f"    ... 仍在等待登录({elapsed}/{deadline_seconds} 秒)")
    return False


def add_account(account_id: str, raw_proxy: str = "", timeout: int = 300) -> int:
    """添加账号:分配端口 → 启动 Chrome → 等待扫码登录 → 写配置。"""
    if not is_valid_id(account_id):
        print("[X] 账号标识只能包含字母、数字、下划线和短横线")
        return 1

    accounts = account_store.load_accounts()
    if any(a["account_id"] == account_id for a in accounts):
        print(f"[!] 账号 {account_id} 已存在,将覆盖其配置(不影响已有 profile)")

    proxy = parse_proxy(raw_proxy)
    if raw_proxy and not proxy:
        return 1

    # 分配空闲端口
    existing = next((a for a in accounts if a["account_id"] == account_id), None)
    port = existing["cdp_port"] if existing else account_store.next_free_port(accounts)
    if existing and cdp.is_port_open(existing["cdp_port"]):
        print(f"[i] 端口 {port} 已在运行,复用该实例")

    profile = account_store.PROFILES_DIR / account_id

    print("-" * 62)
    print(f" 账号 : {account_id}")
    print(f" 端口 : {port}")
    print(f" Profile: {profile}")
    print(f" 代理 : {proxy['server'] if proxy else '未绑定'}")
    print("-" * 62)

    if not cdp.find_chrome_path():
        print("[X] 未找到 Chrome/Edge,请设置环境变量 DOUBAO_CHROME_PATH")
        return 1

    cdp.launch_chrome(
        port=port,
        user_data_dir=profile,
        proxy=proxy,
        start_url=config.DOUBAO_CHAT_URL,
    )
    print("[+] Chrome 已启动,请在弹出的窗口中登录豆包(扫码或账号密码)")
    print(f"    等待登录中(最多 {timeout} 秒)...")

    if not asyncio.run(wait_for_login(port, timeout)):
        print("[X] 未检测到登录态(超时)。可稍后在菜单选 5) 复查,或重新添加。")
        # 即便超时也保存配置,方便后续用菜单 5) 复查
        account_store.save_accounts(
            _upsert(account_id, port, str(Path(".profiles") / account_id), proxy)
        )
        return 1

    account_store.save_accounts(
        _upsert(
            account_id,
            port,
            str(Path(".profiles") / account_id),
            proxy,
        )
    )
    print(f"[+] 账号 {account_id} 已保存到 {account_store.ACCOUNTS_FILE}")
    return 0


def _upsert(
    account_id: str, port: int, user_data_dir: str, proxy: dict | None
) -> list[dict]:
    """把账号写入列表(存在则覆盖)。"""
    accounts = account_store.load_accounts()
    entry = {
        "account_id": account_id,
        "cdp_port": port,
        "user_data_dir": user_data_dir,
        "proxy": proxy,
        "status": account_store.STATUS_ACTIVE,
        "cookies": [],
    }
    for index, existing in enumerate(accounts):
        if existing["account_id"] == account_id:
            accounts[index] = entry
            return accounts
    accounts.append(entry)
    return accounts


def interactive_add() -> None:
    """菜单项 0。"""
    print("\n--- 添加账号 ---")
    accounts = account_store.load_accounts()
    default_id = f"account_{len(accounts) + 1}"
    account_id = input(f" 账号标识(回车使用 {default_id}): ").strip() or default_id

    raw_proxy = input(
        " 代理(可选,回车跳过)\n"
        "   格式 http://host:port  或  http://user:pass@host:port\n"
        "   > "
    ).strip()

    add_account(account_id, raw_proxy)


# ---------------------------------------------------------------------------
# 1) 删除账号
# ---------------------------------------------------------------------------
def delete_account(
    target: str, *, purge_profile: bool = False, assume_yes: bool = False
) -> int:
    """按编号或 account_id 删除账号(可选停止实例并删除 profile)。"""
    accounts = account_store.load_accounts()
    if not accounts:
        print("[i] 当前没有任何账号")
        return 1

    entry = None
    if target.isdigit() and 0 <= int(target) < len(accounts):
        entry = accounts[int(target)]
    else:
        entry = next((a for a in accounts if a["account_id"] == target), None)

    if entry is None:
        print(f"[X] 未找到账号:{target}")
        return 1

    account_id = entry["account_id"]
    if not assume_yes:
        answer = input(f" 确认删除账号 {account_id}?[y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            print(" 已取消")
            return 0

    remaining = [a for a in accounts if a["account_id"] != account_id]
    account_store.save_accounts(remaining)
    print(f"[+] 已删除账号:{account_id}(剩余 {len(remaining)} 个)")

    # 停止对应的 Chrome 实例
    profile = account_store.profile_dir(entry)
    if cdp.is_port_open(entry["cdp_port"]):
        cdp.stop_chrome_for_profile(profile)
        print(f"[+] 已停止该账号的 Chrome 实例(端口 {entry['cdp_port']})")

    if profile.exists():
        if purge_profile:
            shutil.rmtree(profile, ignore_errors=True)
            print(f"[+] 已删除浏览器 profile:{profile}")
        else:
            print(f"[i] 浏览器 profile 保留:{profile}")
    return 0


def interactive_delete() -> None:
    """菜单项 1。"""
    print("\n--- 删除账号 ---")
    accounts = account_store.load_accounts()
    if not accounts:
        print("[i] 当前没有任何账号")
        return
    for index, account in enumerate(accounts):
        print(f"    {index}) {describe_line(account)}")

    target = input(" 请输入要删除的编号或 account_id(回车取消): ").strip()
    if not target:
        print(" 已取消")
        return

    entry = None
    if target.isdigit() and 0 <= int(target) < len(accounts):
        entry = accounts[int(target)]
    else:
        entry = next((a for a in accounts if a["account_id"] == target), None)
    if entry is None:
        print(f"[X] 未找到账号:{target}")
        return

    purge = False
    profile = account_store.profile_dir(entry)
    if profile.exists():
        purge = input(
            f" 同时删除该账号的浏览器 profile({profile})?[y/N]: "
        ).strip().lower() in ("y", "yes")

    delete_account(entry["account_id"], purge_profile=purge, assume_yes=False)


# ---------------------------------------------------------------------------
# 2) 查看账号列表
# ---------------------------------------------------------------------------
def list_accounts(check_state: bool = True) -> int:
    """打印账号列表。"""
    accounts = account_store.load_accounts()
    if not accounts:
        print("[i] 还没有任何账号,请先添加(菜单选 0)")
        return 0

    print("-" * 74)
    for index, account in enumerate(accounts):
        print(f"  {index}) {describe_line(account, check_state=check_state)}")
    print("-" * 74)
    print(f" 共 {len(accounts)} 个账号")
    return 0


# ---------------------------------------------------------------------------
# 3) / 4) 启停全部实例
# ---------------------------------------------------------------------------
def start_all() -> int:
    """启动所有账号的 Chrome 实例。"""
    accounts = account_store.load_accounts()
    if not accounts:
        print("[i] 没有可启动的账号")
        return 1
    if not cdp.find_chrome_path():
        print("[X] 未找到 Chrome/Edge,请设置 DOUBAO_CHROME_PATH")
        return 1

    for account in accounts:
        port = account["cdp_port"]
        if cdp.is_port_open(port):
            print(f"[i] {account['account_id']}: 已在运行(端口 {port})")
            continue
        print(f"[+] 启动 {account['account_id']} (端口 {port}) ...")
        cdp.launch_chrome(
            port=port,
            user_data_dir=account_store.profile_dir(account),
            proxy=account.get("proxy"),
            start_url=config.DOUBAO_CHAT_URL,
        )
    return 0


def stop_all() -> int:
    """停止所有账号的 Chrome 实例。"""
    accounts = account_store.load_accounts()
    if not accounts:
        print("[i] 没有账号")
        return 1
    for account in accounts:
        if not cdp.is_port_open(account["cdp_port"]):
            print(f"[i] {account['account_id']}: 未在运行")
            continue
        cdp.stop_chrome_for_profile(account_store.profile_dir(account))
        print(f"[+] {account['account_id']}: 已停止")
    return 0


# ---------------------------------------------------------------------------
# 5) 检查登录态与出口 IP
# ---------------------------------------------------------------------------
async def _check_one(account: dict) -> bool:
    port = account["cdp_port"]
    account_id = account["account_id"]
    if not cdp.is_port_open(port):
        print(f"  [X] {account_id}: 实例未运行(端口 {port})")
        return False
    try:
        async with cdp.cdp_browser(port) as (browser, context):
            info = await cdp.verify_cdp_connection(browser, context, port)
    except Exception as exc:  # noqa: BLE001
        print(f"  [X] {account_id}: CDP 连接失败 {type(exc).__name__}: {exc}")
        return False

    logged = info.get("logged_in")
    print(
        f"  {'[OK]' if logged else '[!] '} {account_id}: "
        f"登录态={'已登录' if logged else '未登录'}  "
        f"退出IP={info.get('exit_ip') or '未知'}  "
        f"内核={info.get('browser_version')}"
    )
    return bool(logged)


async def _check_all_async() -> int:
    accounts = account_store.load_accounts()
    if not accounts:
        print("[i] 没有账号")
        return 1
    print("-" * 74)
    results = []
    for account in accounts:
        results.append(await _check_one(account))
    print("-" * 74)
    print(f" 已登录 {sum(results)} / {len(accounts)}")
    return 0


def check_all() -> int:
    """检查所有账号的登录态与出口 IP。"""
    return asyncio.run(_check_all_async())


# ---------------------------------------------------------------------------
# 交互菜单
# ---------------------------------------------------------------------------
MENU_TEXT = """
==========================================================
 豆包账号管理 (CDP 模式)
==========================================================
  0) 添加账号(启动 Chrome 扫码登录)
  1) 删除账号
  2) 查看账号列表
  3) 启动所有账号的 Chrome 实例
  4) 停止所有 Chrome 实例
  5) 检查账号登录态与出口 IP
  q) 退出
----------------------------------------------------------"""


def interactive_menu() -> int:
    """菜单主体。"""
    print(MENU_TEXT)
    while True:
        try:
            choice = input(" 请选择 [0/1/2/3/4/5/q]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n 已退出")
            return 0

        if choice in ("q", "quit", "exit", ""):
            print(" 已退出")
            return 0
        if choice == "0":
            interactive_add()
        elif choice == "1":
            interactive_delete()
        elif choice == "2":
            list_accounts()
        elif choice == "3":
            start_all()
        elif choice == "4":
            stop_all()
        elif choice == "5":
            check_all()
        else:
            print("[!] 无效选择,请输入 0-5 或 q")

        print(MENU_TEXT)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manage_accounts",
        description="豆包账号管理(CDP 模式,不带参数则进入交互菜单)",
    )
    parser.add_argument("--add", metavar="ACCOUNT_ID", help="添加账号")
    parser.add_argument(
        "--proxy",
        default="",
        help="固定代理:http://host:port 或 http://user:pass@host:port",
    )
    parser.add_argument("--timeout", type=int, default=300, help="等待登录的秒数")
    parser.add_argument("--delete", metavar="ACCOUNT_ID", help="删除账号")
    parser.add_argument(
        "--purge-profile", action="store_true", help="删除账号时一并删除 profile"
    )
    parser.add_argument("--yes", action="store_true", help="删除时跳过确认")
    parser.add_argument("--list", action="store_true", help="查看账号列表")
    parser.add_argument("--start", action="store_true", help="启动所有 Chrome 实例")
    parser.add_argument("--stop", action="store_true", help="停止所有 Chrome 实例")
    parser.add_argument("--check", action="store_true", help="检查登录态与出口 IP")
    return parser


def main() -> int:
    utils.setup_logging()
    args = build_parser().parse_args()

    if args.list:
        return list_accounts()
    if args.start:
        return start_all()
    if args.stop:
        return stop_all()
    if args.check:
        return check_all()
    if args.delete:
        return delete_account(
            args.delete, purge_profile=args.purge_profile, assume_yes=args.yes
        )
    if args.add:
        return add_account(args.add, args.proxy, args.timeout)

    # 不带参数 → 交互菜单
    return interactive_menu()


if __name__ == "__main__":
    raise SystemExit(main())
