"""检测当前出口 IP,并测试豆包连通性。

用途:更换网络后(手机热点、重启光猫、切换代理)确认出口 IP 是否真的变了 ——
豆包的限流是按请求来源(IP + 设备指纹)打的,换账号不换 IP 是没用的。

用法::

    # 查看当前出口 IP + 豆包连通性
    python scripts/check_ip.py

    # 顺便测试某个代理的出口 IP
    python scripts/check_ip.py --proxy http://127.0.0.1:18080
    python scripts/check_ip.py --proxy http://user:pass@1.2.3.4:8080
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import aiohttp  # noqa: E402

import config  # noqa: E402

# 多个查询服务,任一可用即可
IP_SERVICES = (
    "https://api.ipify.org?format=json",
    "https://ipinfo.io/json",
    "https://api.ip.sb/geoip",
    "https://ipapi.co/json/",
)

TIMEOUT = aiohttp.ClientTimeout(total=10)


def _extract_ip(payload: dict) -> str | None:
    for key in ("ip", "query", "origin"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value.split(",")[0].strip()
    return None


def _describe(payload: dict) -> str:
    """尽量拼出归属地信息(不同服务字段名不一样)。"""
    parts = [
        payload.get("city"),
        payload.get("region"),
        payload.get("country_name") or payload.get("country"),
    ]
    parts = [str(p) for p in parts if p]
    org = payload.get("org") or payload.get("isp") or payload.get("asn")
    text = " / ".join(parts)
    if org:
        text = f"{text} ({org})" if text else str(org)
    return text


async def query_exit_ip(proxy: dict | None) -> tuple[str | None, str]:
    """查询出口 IP,返回 (ip, 描述)。"""
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        for url in IP_SERVICES:
            try:
                async with session.get(url, proxy=proxy.get("server") if proxy else None) as resp:
                    if resp.status != 200:
                        continue
                    payload = await resp.json(content_type=None)
                    if not isinstance(payload, dict):
                        continue
                    ip = _extract_ip(payload)
                    if ip:
                        return ip, _describe(payload)
            except Exception:  # noqa: BLE001
                continue
    return None, ""


async def check_doubao(proxy: dict | None) -> tuple[bool, str]:
    """测试能否访问豆包(只看连通性,不发起对话)。"""
    proxy_url = proxy.get("server") if proxy else None
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            async with session.get(config.DOUBAO_CHAT_URL, proxy=proxy_url) as resp:
                return resp.status == 200, f"HTTP {resp.status}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def parse_proxy(raw: str) -> dict | None:
    """与 add_account.py 保持一致的代理解析。"""
    if not raw:
        return None
    from urllib.parse import urlparse

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


async def main_async(proxy: dict | None) -> int:
    label = "代理" if proxy else "本机"
    print("-" * 58)
    print(f"检测{label}出口 IP")
    print("-" * 58)
    if proxy:
        print(f"  代理地址: {proxy['server']}")

    ip, desc = await query_exit_ip(proxy)
    if ip:
        print(f"  出口 IP : {ip}")
        if desc:
            print(f"  归属地  : {desc}")
    else:
        print("  [X] 未能获取出口 IP(检查网络或该网络是否屏蔽了查询服务)")

    ok, detail = await check_doubao(proxy)
    print(f"  豆包连通: {'可访问' if ok else '不可访问'}  ({detail})")
    print("-" * 58)

    if not ip:
        return 1
    print()
    print("提示:换网络后重新运行本脚本,如果「出口 IP」变了,说明已经换到新 IP,")
    print("      再跑 run.bat 采集即可。若 IP 没变,说明该网络仍走原来的出口。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="check_ip",
        description="检测出口 IP 与豆包连通性(换网络后确认 IP 是否变化)",
    )
    parser.add_argument(
        "--proxy",
        default="",
        help="可选:顺便测试该代理的出口 IP,如 http://127.0.0.1:18080",
    )
    args = parser.parse_args()

    proxy = parse_proxy(args.proxy)
    if args.proxy and not proxy:
        return 1

    return asyncio.run(main_async(proxy))


if __name__ == "__main__":
    raise SystemExit(main())
