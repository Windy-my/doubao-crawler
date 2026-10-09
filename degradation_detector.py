"""降权检测与自动恢复。

豆包对账号的轻度风控表现为**静默降权**:接口仍然返回 200,正文也正常,
但不再附带参考链接。因此仅靠 HTTP 状态码无法判断,必须依据"参考链接返回率"
这一业务指标巡检。

巡检逻辑:
    1. 活跃账号总查询数 ≥ ``DEGRADATION_MIN_QUERIES`` 且参考链接返回率
       低于 ``DEGRADATION_REF_RATE``(默认 30%)→ 判定已降权,冷却 2 小时。
    2. 冷却池中冷却已结束的账号 → 重新激活,健康分重置为 60,给一次机会。
"""

from __future__ import annotations

import logging
import time
from typing import Any

import config
from account_pool import AccountPool

logger = logging.getLogger("doubao.degradation")


class DegradationDetector:
    """周期性巡检账号健康度,执行降权冷却与冷却到期恢复。"""

    def __init__(self, pool: AccountPool, redis_client: Any) -> None:
        self.pool = pool
        self.redis = redis_client

    async def check_and_rotate(self) -> dict[str, int]:
        """执行一轮巡检。

        :return: ``{"degraded": 本次判定降权数, "reactivated": 本次恢复数}``。
        """
        degraded = await self._cool_degraded_accounts()
        reactivated = await self._reactivate_expired_accounts()
        return {"degraded": degraded, "reactivated": reactivated}

    async def _cool_degraded_accounts(self) -> int:
        """巡检活跃账号,把参考链接返回率过低的账号移入冷却池。"""
        degraded = 0
        for account_id in await self.pool.list_active_ids():
            account = await self.pool.get_account(account_id)
            if not account:
                continue

            total = account["total"]
            # 样本不足时不判断,避免刚注册的账号被误伤
            if total < config.DEGRADATION_MIN_QUERIES:
                continue

            ref_rate = account["reference"] / total
            if ref_rate < config.DEGRADATION_REF_RATE:
                await self.pool.move_to_cooling(
                    account_id,
                    config.HEALTH_COOLING_TIME,
                    reason=f"降权巡检: 参考链接率 {ref_rate:.0%} ({account['reference']}/{total})",
                )
                degraded += 1

        if degraded:
            logger.info("降权巡检: %d 个账号被移入冷却池", degraded)
        return degraded

    async def _reactivate_expired_accounts(self) -> int:
        """把冷却期已结束的账号重新激活。"""
        reactivated = 0
        now = time.time()
        for account_id in await self.pool.list_cooling_ids():
            account = await self.pool.get_account(account_id)
            if not account:
                # 脏数据:冷却池中有但哈希不存在
                await self.redis.zrem(config.POOL_COOLING, account_id)
                continue

            if account["cooling_until"] <= now:
                await self.pool.reactivate(account_id, config.REACTIVATE_HEALTH)
                reactivated += 1

        if reactivated:
            logger.info("降权巡检: %d 个账号冷却结束并重新激活", reactivated)
        return reactivated
