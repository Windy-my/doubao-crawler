"""豆包账号管理:添加 / 删除 / 查看账号列表。

直接运行即可进入交互菜单::

    python scripts/add_account.py

菜单:

    0) 添加账号      —— 弹出浏览器扫码登录,导出 cookies 写入 data/accounts.json
    1) 删除账号      —— 从 accounts.json 移除(可选一并删除其浏览器 profile)
    2) 查看账号列表  —— 显示账号、cookie 数、登录态、绑定代理

也支持命令行方式(便于脚本化,与交互菜单等价)::

    python scripts/add_account.py --id account_2
    python scripts/add_account.py --id account_2 --proxy http://127.0.0.1:18080
    python scripts/add_account.py --id account_2 --proxy http://user:pass@1.2.3.4:8080
    python scripts/add_account.py --list
    python scripts/add_account.py --delete account_2

关于代理(保持原有写法):
    --proxy http://host:port                直连型代理
    --proxy http://user:pass@host:port      带用户名密码的代理

为什么不能直接导入 Edge 的登录态:
    实测 Edge 153 的 cookie 是 v20(App-Bound Encryption),外部脚本无法解密,
    且 Edge 禁止在默认数据目录上开启远程调试。所以必须让浏览器自己完成登录,
    再用 ``context.storage_state()`` 导出(由浏览器自身解密)。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import config  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

ACCOUNTS_FILE = BASE_DIR / "data" / "accounts.json"
PROFILES_DIR = BASE_DIR / ".profiles"
DOUBAO_URL = "https://www.doubao.com/chat/"

SESSION_COOKIE_NAMES = ("sessionid", "sessionid_ss")


# ---------------------------------------------------------------------------
# 账号文件读写
# ---------------------------------------------------------------------------
def load_accounts() -> list[dict]:
    """读取现有账号列表(文件不存在时返回空列表)。"""
    if not ACCOUNTS_FILE.exists():
        return []
    try:
        raw = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"[!] {ACCOUNTS_FILE} 不是合法 JSON,将视为空列表")
        return []
    accounts = raw.get("accounts", []) if isinstance(raw, dict) else raw
    return [a for a in accounts if isinstance(a, dict)]


def save_accounts(accounts: list[dict]) -> None:
    """写回账号列表。"""
    ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACCOUNTS_FILE.write_text(
        json.dumps({"accounts": accounts}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def parse_proxy(raw: str) -> dict | None:
    """把 ``http://user:pass@host:port`` 解析成 Playwright 的代理字典。

    支持两种写法(与既有行为一致):
        ``http://host:port``                —— 直连型代理
        ``http://user:pass@host:port``      —— 带用户名密码的代理
    """
    if not raw:
        return None
    url = urlparse(raw)
    if not url.hostname:
        print(f"[!] 代理格式无法解析,已忽略: {raw}")
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


def describe_account(account: dict) -> str:
    """把账号格式化成一行摘要,供列表展示。"""
    name = str(account.get("account_id") or "?")
    cookies = account.get("cookies") or []
    has_session = any(c.get("name") in SESSION_COOKIE_NAMES for c in cookies)
    proxy = account.get("proxy") or {}
    server = proxy.get("server") or "未绑定"
    return (
        f"{name:<18} cookie={len(cookies):<3} "
        f"登录态={'有' if has_session else '缺失':<4} 代理={server}"
    )


def is_valid_id(account_id: str) -> bool:
    """账号标识只允许字母、数字、下划线和短横线。"""
    return bool(account_id) and account_id.replace("-", "").replace("_", "").isalnum()


def upsert_account(account_id: str, cookies: list[dict], proxy: dict | None) -> int:
    """新增或覆盖账号,返回写入后的账号总数。"""
    accounts = load_accounts()
    entry = {"account_id": account_id, "cookies": cookies, "proxy": proxy}
    for index, existing in enumerate(accounts):
        if existing.get("account_id") == account_id:
            accounts[index] = entry
            print(f"[i] 已更新已存在的账号:{account_id}")
            break
    else:
        accounts.append(entry)
        print(f"[+] 已新增账号:{account_id}")

    save_accounts(accounts)
    print(f"[+] 写入 {ACCOUNTS_FILE}")
    return len(accounts)


# ---------------------------------------------------------------------------
# 登录并导出
# ---------------------------------------------------------------------------
async def login_and_export(
    account_id: str, proxy: dict | None, timeout: int
) -> list[dict] | None:
    """打开浏览器让用户扫码登录,返回 doubao 相关的 cookies。"""
    profile_dir = PROFILES_DIR / account_id
    profile_dir.mkdir(parents=True, exist_ok=True)

    launch_kwargs: dict = {
        "user_data_dir": str(profile_dir),
        "headless": False,
        "args": config.BROWSER_ARGS,
        "viewport": config.VIEWPORT,
        "locale": "zh-CN",
        "timezone_id": "Asia/Shanghai",
    }
    if proxy:
        launch_kwargs["proxy"] = proxy

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(**launch_kwargs)
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(DOUBAO_URL, wait_until="domcontentloaded", timeout=60000)

            print("=" * 58)
            print(f" 请在刚弹出的浏览器窗口中登录账号:{account_id}")
            if proxy:
                print(f" 已绑定代理:{proxy['server']}")
            print(f" 等待登录中(最多 {timeout} 秒)...")
            print("=" * 58)

            for elapsed in range(0, timeout, 3):
                await asyncio.sleep(3)
                cookies = await ctx.cookies("https://www.doubao.com")
                names = {c["name"] for c in cookies}
                if any(n in names for n in SESSION_COOKIE_NAMES):
                    print(f"[+] 检测到登录态(用时约 {elapsed + 3} 秒)")
                    await asyncio.sleep(2)  # 等其余 cookie 落盘
                    state = await ctx.storage_state()
                    doubao = [
                        c
                        for c in state.get("cookies", [])
                        if "doubao" in c.get("domain", "")
                    ]
                    print(f"[+] 导出 {len(doubao)} 条 doubao cookie")
                    return doubao

            print("[X] 登录超时,未检测到 sessionid")
            return None
        finally:
            await ctx.close()


# ---------------------------------------------------------------------------
# 各项功能
# ---------------------------------------------------------------------------
def cmd_list() -> int:
    """查看账号列表。"""
    accounts = load_accounts()
    if not accounts:
        print(f"[i] 还没有任何账号,请先添加({ACCOUNTS_FILE})")
        return 0
    print(f"[i] 当前共 {len(accounts)} 个账号:")
    for index, account in enumerate(accounts):
        print(f"    {index}) {describe_account(account)}")
    return 0


def add_account(
    account_id: str, raw_proxy: str = "", timeout: int = 420, *,
    confirm_overwrite: bool = False
) -> int:
    """添加或更新账号。"""
    if not is_valid_id(account_id):
        print("[X] 账号标识只能包含字母、数字、下划线和短横线")
        return 1

    existing = {a.get("account_id") for a in load_accounts()}
    if account_id in existing and not confirm_overwrite:
        print(f"[!] 账号 {account_id} 已存在,继续将覆盖它的 cookie")

    proxy = parse_proxy(raw_proxy)
    if raw_proxy and not proxy:
        print("[X] 代理格式无法解析,已取消")
        return 1

    cookies = asyncio.run(login_and_export(account_id, proxy, timeout))
    if not cookies:
        return 1

    total = upsert_account(account_id, cookies, proxy)
    print(f"[+] 当前账号列表:{[a.get('account_id') for a in load_accounts()]}")
    print(f"[+] 共 {total} 个账号")
    return 0


def delete_account(
    target: str, *, purge_profile: bool = False, assume_yes: bool = False
) -> int:
    """按编号或 account_id 删除账号,可选一并删除其浏览器 profile。"""
    accounts = load_accounts()
    if not accounts:
        print("[i] 当前没有任何账号")
        return 1

    entry = None
    if target.isdigit() and 0 <= int(target) < len(accounts):
        entry = accounts[int(target)]
    else:
        entry = next((a for a in accounts if a.get("account_id") == target), None)

    if entry is None:
        print(f"[X] 未找到账号:{target}")
        return 1

    account_id = str(entry.get("account_id"))

    if not assume_yes:
        answer = input(f" 确认删除账号 {account_id}?[y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            print(" 已取消")
            return 0

    remaining = [a for a in accounts if a.get("account_id") != account_id]
    save_accounts(remaining)
    print(f"[+] 已删除账号:{account_id}(剩余 {len(remaining)} 个)")

    profile_dir = PROFILES_DIR / account_id
    if profile_dir.exists():
        if purge_profile:
            shutil.rmtree(profile_dir, ignore_errors=True)
            print(f"[+] 已删除浏览器 profile:{profile_dir}")
        else:
            print(f"[i] 浏览器 profile 仍保留:{profile_dir}")
            print("    如需一并清理,可在菜单里选择,或手动删除该目录")

    return 0


# ---------------------------------------------------------------------------
# 交互菜单
# ---------------------------------------------------------------------------
MENU_TEXT = """
==========================================================
 豆包账号管理
==========================================================
  0) 添加账号
  1) 删除账号
  2) 查看账号列表
  q) 退出
----------------------------------------------------------"""


def interactive_add() -> None:
    """菜单项 0:添加账号。"""
    print("\n--- 添加账号 ---")
    accounts = load_accounts()
    default_id = f"account_{len(accounts) + 1}"
    account_id = input(f" 账号标识(回车使用 {default_id}): ").strip() or default_id

    if not is_valid_id(account_id):
        print("[X] 账号标识只能包含字母、数字、下划线和短横线")
        return

    if any(a.get("account_id") == account_id for a in accounts):
        if input(f"[!] {account_id} 已存在,覆盖它的 cookie?[y/N]: ").strip().lower() not in (
            "y",
            "yes",
        ):
            print(" 已取消")
            return

    raw_proxy = input(
        " 代理(可选,回车跳过)\n"
        "   格式 http://host:port  或  http://user:pass@host:port\n"
        "   > "
    ).strip()

    add_account(account_id, raw_proxy, confirm_overwrite=True)


def interactive_delete() -> None:
    """菜单项 1:删除账号。"""
    print("\n--- 删除账号 ---")
    accounts = load_accounts()
    if not accounts:
        print("[i] 当前没有任何账号")
        return

    for index, account in enumerate(accounts):
        print(f"    {index}) {describe_account(account)}")

    target = input(" 请输入要删除的编号或 account_id(回车取消): ").strip()
    if not target:
        print(" 已取消")
        return

    # 解析目标,先确认再决定是否清理 profile
    entry = None
    if target.isdigit() and 0 <= int(target) < len(accounts):
        entry = accounts[int(target)]
    else:
        entry = next((a for a in accounts if a.get("account_id") == target), None)

    if entry is None:
        print(f"[X] 未找到账号:{target}")
        return

    account_id = str(entry.get("account_id"))
    purge = False
    profile_dir = PROFILES_DIR / account_id
    if profile_dir.exists():
        purge = input(
            f" 同时删除该账号的浏览器 profile({profile_dir})?[y/N]: "
        ).strip().lower() in ("y", "yes")

    # delete_account 内部还会再确认一次,这里已经问过,直接放行
    delete_account(account_id, purge_profile=purge, assume_yes=False)


def interactive_menu() -> int:
    """无参数运行时进入的交互菜单。"""
    print(MENU_TEXT)
    while True:
        try:
            choice = input(" 请选择 [0/1/2/q]: ").strip().lower()
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
            cmd_list()
        else:
            print("[!] 无效选择,请输入 0、1、2 或 q")

        print(MENU_TEXT)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="add_account",
        description="豆包账号管理:添加 / 删除 / 查看(不带参数则进入交互菜单)",
    )
    parser.add_argument("--id", help="要添加(或覆盖)的账号标识,例如 account_2")
    parser.add_argument(
        "--proxy",
        default="",
        help="固定代理:http://host:port 或 http://user:pass@host:port",
    )
    parser.add_argument(
        "--timeout", type=int, default=420, help="等待扫码登录的秒数(默认 420)"
    )
    parser.add_argument("--list", action="store_true", help="查看账号列表")
    parser.add_argument("--delete", metavar="ACCOUNT_ID", help="删除指定账号")
    parser.add_argument(
        "--purge-profile",
        action="store_true",
        help="删除账号时一并删除其浏览器 profile",
    )
    parser.add_argument("--yes", action="store_true", help="删除时跳过确认")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    # 带参数 → 命令行模式(与交互菜单等价,便于脚本化)
    if args.list:
        return cmd_list()
    if args.delete:
        return delete_account(
            args.delete, purge_profile=args.purge_profile, assume_yes=args.yes
        )
    if args.id:
        return add_account(args.id, args.proxy, args.timeout, confirm_overwrite=True)

    # 不带参数 → 交互菜单
    return interactive_menu()


if __name__ == "__main__":
    raise SystemExit(main())
