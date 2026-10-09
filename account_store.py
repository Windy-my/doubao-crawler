"""账号存储:统一读写 ``data/accounts.json``(CDP 模式)。

CDP 模式下账号结构::

    {
      "account_id": "account_1",
      "cdp_port": 9222,                     # 该账号 Chrome 实例的调试端口
      "user_data_dir": ".profiles/account_1",  # 独立 User Data Dir
      "proxy": {"server": ..., "username": ..., "password": ...} | null,
      "status": "active" | "disabled",
      "health_score": 100
    }

**登录态不再存 cookies**,而是留在各自的 Chrome profile 里(真实浏览器自己管理)。

向后兼容:
    旧格式只有 ``account_id`` / ``cookies`` / ``proxy``。加载时会自动补上
    ``cdp_port`` 与 ``user_data_dir``(端口从 ``CDP_PORT_BASE`` 递增分配);
    ``cookies`` 字段保留但 CDP 模式不再使用,避免误删用户已有数据。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import config

logger = logging.getLogger("doubao.account_store")

BASE_DIR = Path(__file__).resolve().parent
ACCOUNTS_FILE = BASE_DIR / "data" / "accounts.json"
PROFILES_DIR = BASE_DIR / ".profiles"

STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"


def _normalize(account: dict[str, Any], index: int, used_ports: set[int]) -> dict[str, Any]:
    """归一化单条账号:补全 CDP 字段,并解决端口冲突。"""
    account_id = str(account.get("account_id") or f"account_{index + 1}")

    port = account.get("cdp_port")
    if not isinstance(port, int) or port in used_ports:
        port = config.CDP_PORT_BASE
        while port in used_ports:
            port += 1
    used_ports.add(port)

    raw_dir = account.get("user_data_dir") or str(Path(".profiles") / account_id)

    return {
        "account_id": account_id,
        "cdp_port": port,
        "user_data_dir": raw_dir,
        "proxy": account.get("proxy") or None,
        "status": account.get("status") or STATUS_ACTIVE,
        # 兼容旧字段:CDP 模式不再使用,但保留以免丢数据
        "cookies": account.get("cookies") or [],
        "health_score": account.get("health_score", 100),
    }


def load_accounts() -> list[dict[str, Any]]:
    """读取并归一化账号列表。"""
    if not ACCOUNTS_FILE.exists():
        return []
    try:
        raw = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("%s 不是合法 JSON,视为空列表", ACCOUNTS_FILE)
        return []

    items = raw.get("accounts", []) if isinstance(raw, dict) else raw
    used_ports: set[int] = set()
    accounts: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if isinstance(item, dict):
            accounts.append(_normalize(item, index, used_ports))
    return accounts


def save_accounts(accounts: list[dict[str, Any]]) -> None:
    """写回账号列表(自动去掉运行时字段)。"""
    ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACCOUNTS_FILE.write_text(
        json.dumps({"accounts": accounts}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def find_account(account_id: str) -> dict[str, Any] | None:
    """按 account_id 查找账号。"""
    return next((a for a in load_accounts() if a.get("account_id") == account_id), None)


def profile_dir(account: dict[str, Any]) -> Path:
    """返回账号的 User Data Dir 绝对路径。"""
    raw = account.get("user_data_dir") or str(Path(".profiles") / account["account_id"])
    path = Path(raw)
    return path if path.is_absolute() else BASE_DIR / path


def next_free_port(accounts: list[dict[str, Any]]) -> int:
    """给新账号分配一个未被占用的调试端口。"""
    used = {a.get("cdp_port") for a in accounts}
    port = config.CDP_PORT_BASE
    while port in used or port >= 65535:
        port += 1
    return port


def active_accounts() -> list[dict[str, Any]]:
    """只返回状态为 active 的账号。"""
    return [a for a in load_accounts() if a.get("status") != STATUS_DISABLED]
