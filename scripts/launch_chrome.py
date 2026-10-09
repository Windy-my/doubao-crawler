"""Chrome 实例启动 / 停止工具(CDP 模式)。

每个账号对应一个**独立的 Chrome 实例**:独立 User Data Dir + 独立调试端口。
Chrome 可以独立于本脚本长期运行,采集时只通过 CDP 连上去。

用法::

    # 启动所有账号的 Chrome(有头模式,会弹窗口)
    python scripts/launch_chrome.py --all

    # 只启动某个账号
    python scripts/launch_chrome.py --id account_1

    # 查看各实例运行状态
    python scripts/launch_chrome.py --status

    # 停止所有实例(只结束本项目 profile 对应的进程,不影响其它 Chrome 窗口)
    python scripts/launch_chrome.py --stop --all
    python scripts/launch_chrome.py --stop --id account_1
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import account_store  # noqa: E402
import cdp  # noqa: E402
import config  # noqa: E402
import utils  # noqa: E402


def show_status() -> int:
    """打印各账号 Chrome 实例的运行状态。"""
    accounts = account_store.load_accounts()
    if not accounts:
        print("[i] 还没有任何账号,请先运行 scripts/manage_accounts.py 添加")
        return 0

    print("-" * 72)
    print(f" 浏览器内核: {cdp.find_chrome_path() or '(未找到)'}")
    print(f" CDP 后端  : {cdp.BACKEND_NAME}")
    print("-" * 72)
    running = 0
    for account in accounts:
        port = account["cdp_port"]
        profile = account_store.profile_dir(account)
        alive = cdp.is_port_open(port)
        running += 1 if alive else 0
        flag = "运行中" if alive else "未运行"
        proxy = (account.get("proxy") or {}).get("server") or "未绑定"
        print(
            f"  {account['account_id']:<16} 端口={port:<6} {flag:<6} "
            f"代理={proxy}"
        )
        print(f"       profile: {profile}")
    print("-" * 72)
    print(f" 共 {len(accounts)} 个账号,{running} 个实例在运行")
    return 0


def start_accounts(accounts: list[dict], wait_seconds: float = 8.0) -> int:
    """启动给定账号的 Chrome 实例。"""
    if not accounts:
        print("[X] 没有可启动的账号")
        return 1

    if not cdp.find_chrome_path():
        print(
            "[X] 未找到 Chrome/Edge,请设置环境变量 DOUBAO_CHROME_PATH 指向浏览器 exe"
        )
        return 1

    failed = 0
    for account in accounts:
        account_id = account["account_id"]
        port = account["cdp_port"]
        profile = account_store.profile_dir(account)
        proxy = account.get("proxy")

        if cdp.is_port_open(port):
            print(f"[i] {account_id}: 端口 {port} 已在运行,跳过")
            continue

        try:
            cdp.launch_chrome(
                port=port,
                user_data_dir=profile,
                proxy=proxy,
                lang="zh-CN",
                start_url=config.DOUBAO_CHAT_URL,
                wait_seconds=wait_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[X] {account_id}: 启动失败 {type(exc).__name__}: {exc}")
            failed += 1
            continue

        if cdp.is_port_open(port):
            print(f"[+] {account_id}: 已启动(端口 {port})")
            print(f"      profile: {profile}")
        else:
            print(f"[!] {account_id}: 已发出启动命令,但端口 {port} 还没就绪")
            failed += 1

    if failed:
        print(f"\n[!] {failed} 个实例未正常启动,可稍后用 --status 复查")
        return 1
    return 0


def stop_accounts(accounts: list[dict]) -> int:
    """停止给定账号的 Chrome 实例。"""
    if not accounts:
        print("[X] 没有要停止的账号")
        return 1

    for account in accounts:
        account_id = account["account_id"]
        profile = account_store.profile_dir(account)
        if not cdp.is_port_open(account["cdp_port"]):
            print(f"[i] {account_id}: 未在运行,跳过")
            continue
        cdp.stop_chrome_for_profile(profile)
        print(f"[+] {account_id}: 已停止")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="launch_chrome",
        description="Chrome 实例启动/停止工具(CDP 模式)",
    )
    parser.add_argument("--id", help="只操作指定账号,例如 account_1")
    parser.add_argument("--all", action="store_true", help="操作所有账号")
    parser.add_argument("--stop", action="store_true", help="停止实例(默认是启动)")
    parser.add_argument("--status", action="store_true", help="查看实例运行状态")
    return parser


def main() -> int:
    utils.setup_logging()
    args = build_parser().parse_args()

    if args.status:
        return show_status()

    accounts = account_store.load_accounts()
    if args.id:
        target = [a for a in accounts if a["account_id"] == args.id]
        if not target:
            print(f"[X] 未找到账号:{args.id}")
            return 1
        accounts = target
    elif not args.all:
        print("请指定 --all 或 --id <account_id>,或用 --status 查看状态")
        return 1
    else:
        accounts = [a for a in accounts if a.get("status") != account_store.STATUS_DISABLED]

    if args.stop:
        return stop_accounts(accounts)
    return start_accounts(accounts)


if __name__ == "__main__":
    raise SystemExit(main())
