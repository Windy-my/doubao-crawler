"""账号池管理:健康分、冷却、固定代理绑定。

存储结构(Redis):
    * ``account:{id}``  —— Hash,保存账号的 cookies、代理、各项计数与健康分。
    * ``pool:active``   —— Sorted Set,score 为健康分,成员为可用账号 ID。
    * ``pool:cooling``  —— Sorted Set,score 为冷却结束时间戳,成员为冷却中账号 ID。

设计要点:
    * **固定代理**:代理在注册时写入账号哈希,运行期永不更换。频繁切换 IP
      会直接触发豆包降权。
    * **健康分** = 成功率 × 40 + 参考链接率 × 60。参考链接权重更高,因为
      豆包轻度风控时表现为"静默降权"(能回答但不返回参考链接)。
    * **冷却**:连续 ``NO_REF_STREAK_LIMIT`` 次无参考链接 → 冷却 1 小时;
      健康分低于 ``LOW_HEALTH_THRESHOLD`` → 冷却 2 小时。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import config

logger = logging.getLogger("doubao.account_pool")


class AccountPool:
    """基于 Redis 的账号池,负责账号的注册、领取、健康分与冷却调度。"""

    def __init__(self, redis_client: Any) -> None:
        self.redis = redis_client
        # 单进程内串行化"领取账号",避免多个 Worker 抢到同一账号
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    @staticmethod
    def _key(account_id: str) -> str:
        """账号哈希键。"""
        return f"{config.ACCOUNT_KEY_PREFIX}{account_id}"

    @staticmethod
    def _decode_hash(raw: dict[Any, Any]) -> dict[str, Any]:
        """把 Redis 返回的 bytes 哈希解码为普通 Python 字典。"""
        decoded: dict[str, Any] = {}
        for key, value in raw.items():
            k = key.decode() if isinstance(key, bytes) else key
            v = value.decode() if isinstance(value, bytes) else value
            decoded[k] = v

        # cookies / proxy 存的是 JSON 字符串,这里还原为对象
        decoded["cookies"] = json.loads(decoded.get("cookies") or "[]")
        proxy_raw = decoded.get("proxy") or ""
        decoded["proxy"] = json.loads(proxy_raw) if proxy_raw else None

        # CDP 模式:定位该账号对应的 Chrome 实例
        port_raw = decoded.get("cdp_port") or ""
        decoded["cdp_port"] = int(float(port_raw)) if port_raw else None
        decoded["user_data_dir"] = decoded.get("user_data_dir") or ""

        # 数值字段统一转换,便于调用方直接运算
        for field in ("health", "last_used", "cooling_until", "created_at"):
            decoded[field] = float(decoded.get(field) or 0)
        for field in (
            "total",
            "success",
            "reference",
            "no_ref_streak",
            "recovery_remaining",
        ):
            decoded[field] = int(float(decoded.get(field) or 0))
        return decoded

    async def _load_account(self, account_id: str) -> dict[str, Any] | None:
        """从 Redis 读取单个账号信息,不存在返回 None。"""
        raw = await self.redis.hgetall(self._key(account_id))
        if not raw:
            return None
        return self._decode_hash(raw)

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------
    async def add_account(
        self,
        account_id: str,
        cookies: list[dict[str, Any]] | None = None,
        proxy: dict[str, Any] | None = None,
        health: float = 100.0,
        cdp_port: int | None = None,
        user_data_dir: str | None = None,
    ) -> None:
        """注册账号,初始健康分 100。

        CDP 模式下登录态保存在各自的 Chrome profile 里,``cookies`` 不再必需;
        ``cdp_port`` 与 ``user_data_dir`` 用于定位该账号的 Chrome 实例。

        :param account_id: 账号唯一标识。
        :param cookies: 兼容旧模式保留,CDP 模式下可为空。
        :param proxy: ``{"server": ..., "username": ..., "password": ...}``,可为 None。
        :param health: 初始健康分。
        :param cdp_port: 该账号 Chrome 实例的 CDP 调试端口。
        :param user_data_dir: 该账号的 User Data Dir。
        """
        key = self._key(account_id)
        mapping = {
            "account_id": account_id,
            "cookies": json.dumps(cookies or [], ensure_ascii=False),
            "proxy": json.dumps(proxy, ensure_ascii=False) if proxy else "",
            "cdp_port": str(cdp_port) if cdp_port else "",
            "user_data_dir": user_data_dir or "",
            "health": str(health),
            "total": "0",
            "success": "0",
            "reference": "0",
            "no_ref_streak": "0",
            # 冷却恢复后需要"温柔"处理的剩余次数(渐进式恢复)
            "recovery_remaining": "0",
            "last_used": "0",
            "cooling_until": "0",
            "created_at": str(time.time()),
        }
        await self.redis.hset(key, mapping=mapping)
        # 注册即进入活跃池
        await self.redis.zrem(config.POOL_COOLING, account_id)
        await self.redis.zadd(config.POOL_ACTIVE, {account_id: health})
        logger.info(
            "注册账号 %s (health=%.0f, cdp_port=%s, proxy=%s)",
            account_id,
            health,
            cdp_port or "-",
            "已绑定" if proxy else "无",
        )

    async def register_from_config(self, accounts: list[dict[str, Any]]) -> int:
        """从配置批量注册账号,返回注册数量。"""
        count = 0
        for item in accounts:
            await self.add_account(
                account_id=item["account_id"],
                cookies=item.get("cookies") or [],
                proxy=item.get("proxy"),
                cdp_port=item.get("cdp_port"),
                user_data_dir=item.get("user_data_dir"),
            )
            count += 1
        return count

    # ------------------------------------------------------------------
    # 领取
    # ------------------------------------------------------------------
    async def acquire_account(
        self, min_health: float | None = None
    ) -> dict[str, Any] | None:
        """按健康分从高到低领取一个可用账号。

        过滤条件:
            1. 健康分 ≥ ``min_health``(默认 ``ACCOUNT_MIN_HEALTH``)。
            2. 不在冷却期(``cooling_until`` 已过)。
            3. 距上次使用 ≥ ``MIN_ACCOUNT_INTERVAL`` 秒。

        :return: 账号信息字典(含解析后的 cookies 与 proxy);无可用账号返回 None。
        """
        threshold = config.ACCOUNT_MIN_HEALTH if min_health is None else min_health

        async with self._lock:
            now = time.time()
            # 健康分降序排列
            candidates = await self.redis.zrevrange(
                config.POOL_ACTIVE, 0, -1, withscores=True
            )
            for account_id, score in candidates:
                if isinstance(account_id, bytes):
                    account_id = account_id.decode()
                if float(score) < threshold:
                    continue

                account = await self._load_account(account_id)
                if not account:
                    # 有序集合中存在但哈希已丢失,清理脏数据
                    await self.redis.zrem(config.POOL_ACTIVE, account_id)
                    continue

                # 冷却检查
                if account["cooling_until"] > now:
                    continue

                # 使用间隔检查:随机区间 + 恢复期加倍。
                # 固定间隔本身就是自动化特征,所以改成随机;
                # 刚从冷却里恢复的账号前若干次请求额外放慢(渐进式恢复)。
                min_interval = config.random_account_interval()
                if account.get("recovery_remaining", 0) > 0:
                    min_interval *= config.RECOVERY_INTERVAL_FACTOR
                if now - account["last_used"] < min_interval:
                    continue

                # 命中:立即标记使用时间(在锁内完成,防止并发重复领取)
                await self.redis.hset(self._key(account_id), "last_used", now)
                account["last_used"] = now
                return account

            return None

    # ------------------------------------------------------------------
    # 结果上报
    # ------------------------------------------------------------------
    async def report_result(
        self,
        account_id: str,
        has_reference: bool,
        success: bool,
    ) -> None:
        """上报一次查询结果,更新统计、健康分与冷却状态。

        :param has_reference: 本次是否返回参考链接(豆包降权时表现为 False)。
        :param success: 本次查询是否整体成功(网络/解析均正常)。
        """
        key = self._key(account_id)
        async with self._lock:
            raw = await self.redis.hgetall(key)
            if not raw:
                logger.warning("上报结果失败,账号不存在: %s", account_id)
                return
            account = self._decode_hash(raw)

            total = account["total"] + 1
            success_count = account["success"] + (1 if success else 0)
            reference_count = account["reference"] + (1 if has_reference else 0)

            # 连续无参考链接计数:
            #   成功且无参考链接 → 累加(这是豆包静默降权的信号)
            #   成功且有参考链接 → 清零
            #   技术性失败(网络/超时/解析异常)→ 保持不变,不作为降权依据
            if has_reference:
                no_ref_streak = 0
            elif success:
                no_ref_streak = account["no_ref_streak"] + 1
            else:
                no_ref_streak = account["no_ref_streak"]

            # 健康分 = 成功率 × 40 + 参考链接率 × 60
            success_rate = success_count / total
            reference_rate = reference_count / total
            health = (
                success_rate * 100 * config.SUCCESS_WEIGHT
                + reference_rate * 100 * config.REFERENCE_WEIGHT
            )

            updates = {
                "total": str(total),
                "success": str(success_count),
                "reference": str(reference_count),
                # 渐进式冷却恢复:每完成一次请求就消耗一次"温柔期"额度
                "recovery_remaining": str(
                    max(0, account.get("recovery_remaining", 0) - 1)
                ),
                "no_ref_streak": str(no_ref_streak),
                "health": f"{health:.4f}",
            }
            await self.redis.hset(key, mapping=updates)

            logger.info(
                "账号 %s 上报: success=%s ref=%s total=%d 健康分=%.1f 连续无链接=%d",
                account_id,
                success,
                has_reference,
                total,
                health,
                no_ref_streak,
            )

            # 1) 连续无参考链接 → 冷却 1 小时,并清零连续计数
            if no_ref_streak >= config.NO_REF_STREAK_LIMIT:
                await self.move_to_cooling(
                    account_id,
                    config.COOLING_TIME,
                    reason=f"连续 {no_ref_streak} 次无参考链接",
                )
                await self.redis.hset(key, "no_ref_streak", "0")
                return

            # 2) 健康分过低 → 冷却 2 小时
            #    需要足够样本才判定,避免一次网络抖动/超时就把账号打入长冷却
            if (
                health < config.LOW_HEALTH_THRESHOLD
                and total >= config.MIN_SAMPLES_FOR_COOLING
            ):
                await self.move_to_cooling(
                    account_id,
                    config.HEALTH_COOLING_TIME,
                    reason=f"健康分过低 ({health:.1f})",
                )
                return

            # 3) 正常 → 更新活跃池中的健康分
            await self.redis.zadd(config.POOL_ACTIVE, {account_id: health})

    # ------------------------------------------------------------------
    # 冷却与恢复
    # ------------------------------------------------------------------
    async def move_to_cooling(
        self, account_id: str, seconds: int, reason: str = ""
    ) -> None:
        """把账号移入冷却池,``seconds`` 秒后可被重新激活。"""
        until = time.time() + seconds
        key = self._key(account_id)
        await self.redis.hset(
            key,
            mapping={"cooling_until": f"{until:.4f}", "cooling_reason": reason},
        )
        await self.redis.zrem(config.POOL_ACTIVE, account_id)
        await self.redis.zadd(config.POOL_COOLING, {account_id: until})
        logger.warning(
            "账号 %s 进入冷却 %.0f 分钟: %s",
            account_id,
            seconds / 60,
            reason,
        )

    async def reactivate(
        self, account_id: str, health: float | None = None
    ) -> None:
        """把账号从冷却池重新激活,健康分重置为指定值(默认 60)。

        同时清零该轮次的统计计数。否则"冷却结束 → 巡检按旧的低参考链接率立刻
        再次判定降权"会形成死循环,账号永远无法真正恢复。
        """
        score = config.REACTIVATE_HEALTH if health is None else health
        key = self._key(account_id)
        await self.redis.hset(
            key,
            mapping={
                "health": f"{score:.4f}",
                "cooling_until": "0",
                "cooling_reason": "",
                "no_ref_streak": "0",
                # 渐进式恢复:重新激活后前 N 次请求使用加倍间隔,避免立刻又被限流
                "recovery_remaining": str(config.RECOVERY_REQUESTS),
                # 开启新的评估窗口
                "total": "0",
                "success": "0",
                "reference": "0",
            },
        )
        await self.redis.zrem(config.POOL_COOLING, account_id)
        await self.redis.zadd(config.POOL_ACTIVE, {account_id: score})
        logger.info(
            "账号 %s 已重新激活 (health=%.0f, 温柔期 %d 次请求)",
            account_id,
            score,
            config.RECOVERY_REQUESTS,
        )

    # ------------------------------------------------------------------
    # 查询辅助
    # ------------------------------------------------------------------
    async def get_account(self, account_id: str) -> dict[str, Any] | None:
        """读取账号信息(不改变任何状态)。"""
        return await self._load_account(account_id)

    async def list_active_ids(self) -> list[str]:
        """返回活跃池中的账号 ID(健康分降序)。"""
        ids = await self.redis.zrevrange(config.POOL_ACTIVE, 0, -1)
        return [i.decode() if isinstance(i, bytes) else i for i in ids]

    async def list_cooling_ids(self) -> list[str]:
        """返回冷却池中的账号 ID(冷却结束时间升序,即最早解冻在前)。"""
        ids = await self.redis.zrange(config.POOL_COOLING, 0, -1)
        return [i.decode() if isinstance(i, bytes) else i for i in ids]

    async def stats(self) -> dict[str, int]:
        """账号池概览:活跃数、冷却数。"""
        return {
            "active": int(await self.redis.zcard(config.POOL_ACTIVE)),
            "cooling": int(await self.redis.zcard(config.POOL_COOLING)),
        }
