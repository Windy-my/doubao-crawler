"""开发用 Redis:用 fakeredis 在本机起一个 6379 端点。

适用场景:本机没有 redis-server、也没有可用的 Docker daemon 时,让项目能直接
跑起来(与真实 Redis 协议兼容,且支持 EVAL/cjson,满足队列的 Lua 脚本)。

**仅用于开发/试用**;生产环境请使用真实 Redis,把 config.REDIS_HOST 指向它即可,
项目代码无需改动。
"""

from __future__ import annotations

import sys

try:
    from fakeredis import TcpFakeServer
except ImportError:  # pragma: no cover
    print("[X] 缺少 fakeredis,请先安装: pip install \"fakeredis[lua]\"")
    print("    注意:必须带 [lua] 额外依赖,否则队列的 Lua 脚本无法执行。")
    raise SystemExit(1)

HOST = "127.0.0.1"
PORT = 6379


def main() -> int:
    try:
        server = TcpFakeServer((HOST, PORT), server_type="redis")
    except OSError as exc:
        print(f"[X] 无法在 {HOST}:{PORT} 启动(端口可能已被占用): {exc}")
        return 1

    print(f"[OK] 开发用 Redis 已启动: {HOST}:{PORT}(fakeredis)")
    print("     该窗口请保持开启;关闭窗口即停止。生产请改用真实 Redis。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[OK] 开发用 Redis 已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
