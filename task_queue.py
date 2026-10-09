"""基于 Redis 的任务队列,支持断点续爬与死信。

队列结构:
    * ``queue:pending``      —— List,待处理任务(LPUSH 入队,RPOP/LMOVE 出队,FIFO)。
    * ``queue:running``      —— List,处理中任务(元素带 ``started_at`` 便于超时判定)。
    * ``queue:done``         —— List,已完成任务。
    * ``queue:dead_letter``  —— List,重试超过 ``MAX_RETRY`` 次的任务。

任务状态流转:``PENDING → RUNNING → COMPLETED``(失败重试则回到 PENDING,
超过重试上限进入 dead letter)。

出队与确认通过 Lua 脚本在 Redis 端原子完成,避免 Worker 崩溃时任务丢失。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import config
import utils

logger = logging.getLogger("doubao.task_queue")

# 原子出队:从 pending 右端弹出,写入 running 左端并补上 started_at。
# 时间戳由客户端经 ARGV 传入,使脚本保持确定性——避免在脚本内调用 TIME
# (旧版 Redis 在非确定性命令之后禁止写命令),同时与 recover() 共用同一时间基准。
_POP_LUA = """
local task = redis.call('RPOP', KEYS[1])
if not task then
    return nil
end
local now = tonumber(ARGV[1])
local ok, decoded = pcall(cjson.decode, task)
if ok then
    decoded['started_at'] = now
    decoded['status'] = 'RUNNING'
    redis.call('LPUSH', KEYS[2], cjson.encode(decoded))
else
    redis.call('LPUSH', KEYS[2], task)
end
return task
"""

# 原子确认:按 task_id 在 running 中定位并删除(元素值因补过 started_at 而不同)
_ACK_LUA = """
local items = redis.call('LRANGE', KEYS[1], 0, -1)
for i = 1, #items do
    local ok, decoded = pcall(cjson.decode, items[i])
    if ok and tostring(decoded['task_id']) == ARGV[1] then
        redis.call('LREM', KEYS[1], 1, items[i])
        return 1
    end
end
return 0
"""

# done 队列只保留最近 N 条,避免长期运行撑爆内存
_DONE_KEEP = 10000


class RedisTaskQueue:
    """Redis 任务队列。"""

    def __init__(self, redis_client: Any) -> None:
        self.redis = redis_client
        self._pop_script = redis_client.register_script(_POP_LUA)
        self._ack_script = redis_client.register_script(_ACK_LUA)

    # ------------------------------------------------------------------
    # 入队 / 出队
    # ------------------------------------------------------------------
    async def push(
        self, word: str, task_id: str | None = None, retry: int = 0
    ) -> dict[str, Any]:
        """把任务推入 ``queue:pending``,返回任务字典。"""
        task = {
            "task_id": task_id or utils.task_id_for(word),
            "word": word,
            "retry": retry,
            "status": "PENDING",
            "created_at": time.time(),
        }
        await self.redis.lpush(config.QUEUE_PENDING, json.dumps(task, ensure_ascii=False))
        return task

    async def push_many(self, words: list[str]) -> int:
        """批量入队,返回入队数量(使用 pipeline 降低往返开销)。"""
        count = 0
        async with self.redis.pipeline(transaction=False) as pipe:
            for word in words:
                task = {
                    "task_id": utils.task_id_for(word),
                    "word": word,
                    "retry": 0,
                    "status": "PENDING",
                    "created_at": time.time(),
                }
                pipe.lpush(config.QUEUE_PENDING, json.dumps(task, ensure_ascii=False))
                count += 1
            await pipe.execute()
        logger.info("已入队 %d 个任务", count)
        return count

    async def pop(self) -> dict[str, Any] | None:
        """从 ``queue:pending`` 右端弹出一个任务并放入 ``queue:running``。

        :return: 任务字典;队列为空返回 None。
        """
        raw = await self._pop_script(
            keys=[config.QUEUE_PENDING, config.QUEUE_RUNNING],
            args=[f"{time.time():.6f}"],
        )
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.error("任务反序列化失败: %s", str(raw)[:200])
            return None

    # ------------------------------------------------------------------
    # 完成 / 失败
    # ------------------------------------------------------------------
    async def ack(self, task: dict[str, Any]) -> bool:
        """标记任务完成:从 running 移除并推入 done。"""
        task = dict(task)
        task.pop("started_at", None)
        task["status"] = "COMPLETED"
        task["finished_at"] = time.time()
        payload = json.dumps(task, ensure_ascii=False)

        removed = await self._ack_script(
            keys=[config.QUEUE_RUNNING], args=[str(task["task_id"])]
        )
        await self.redis.lpush(config.QUEUE_DONE, payload)
        await self.redis.ltrim(config.QUEUE_DONE, 0, _DONE_KEEP - 1)
        return bool(removed)

    async def requeue(self, task: dict[str, Any]) -> None:
        """把任务放回 ``queue:pending`` 且**不**增加重试次数。

        用于"暂时无可用账号"等外部原因导致的延后处理——这类情况并非任务本身
        失败,不应消耗重试额度。
        """
        task = dict(task)
        task.pop("started_at", None)
        task["status"] = "PENDING"
        payload = json.dumps(task, ensure_ascii=False)
        # 先按 task_id 从 running 摘除,再放回 pending
        await self._ack_script(
            keys=[config.QUEUE_RUNNING], args=[str(task["task_id"])]
        )
        await self.redis.lpush(config.QUEUE_PENDING, payload)

    async def retry(self, task: dict[str, Any], reason: str = "") -> bool:
        """任务失败后重新入队;超过 ``MAX_RETRY`` 则移入死信队列。

        :return: True 表示已重新入队,False 表示已进入死信队列。
        """
        task = dict(task)
        task.pop("started_at", None)
        retry_count = int(task.get("retry") or 0) + 1
        task["retry"] = retry_count
        task["status"] = "PENDING"
        task["last_error"] = reason
        payload = json.dumps(task, ensure_ascii=False)

        # 无论重试还是进死信,都要先从 running 中摘除
        await self._ack_script(keys=[config.QUEUE_RUNNING], args=[str(task["task_id"])])

        if retry_count > config.MAX_RETRY:
            await self.redis.lpush(config.QUEUE_DEAD_LETTER, payload)
            logger.error(
                "任务 %s 重试 %d 次仍失败,已进入死信队列: %s",
                task["task_id"],
                retry_count,
                reason,
            )
            return False

        await self.redis.lpush(config.QUEUE_PENDING, payload)
        logger.warning(
            "任务 %s 第 %d 次重试入队: %s", task["task_id"], retry_count, reason
        )
        return True

    # ------------------------------------------------------------------
    # 断点续爬
    # ------------------------------------------------------------------
    async def recover(self) -> dict[str, int]:
        """把 ``queue:running`` 中执行超时的任务重新入队。

        超时判定依据元素内的 ``started_at``(由 ``pop`` 写入的时间戳),与进行中
        任务使用同一时间基准。

        :return: ``{"requeued": n, "dead": m}``。
        """
        now = time.time()
        raw_items = await self.redis.lrange(config.QUEUE_RUNNING, 0, -1)
        requeued = 0
        dead = 0

        for raw in raw_items:
            text = raw.decode() if isinstance(raw, bytes) else raw
            try:
                task = json.loads(text)
            except json.JSONDecodeError:
                # 脏数据直接清理
                await self.redis.lrem(config.QUEUE_RUNNING, 1, raw)
                continue

            started_at = float(task.get("started_at") or 0)
            if started_at and now - started_at <= config.RUNNING_TIMEOUT:
                # 仍在执行中,保留
                continue

            # 从 running 摘除
            await self.redis.lrem(config.QUEUE_RUNNING, 1, raw)

            task.pop("started_at", None)
            retry_count = int(task.get("retry") or 0) + 1
            task["retry"] = retry_count
            task["status"] = "PENDING"
            payload = json.dumps(task, ensure_ascii=False)

            if retry_count > config.MAX_RETRY:
                await self.redis.lpush(config.QUEUE_DEAD_LETTER, payload)
                dead += 1
            else:
                await self.redis.lpush(config.QUEUE_PENDING, payload)
                requeued += 1

        if requeued or dead:
            logger.info("断点恢复完成: 重新入队 %d,死信 %d", requeued, dead)
        else:
            logger.info("断点恢复完成: 无超时任务")
        return {"requeued": requeued, "dead": dead}

    # ------------------------------------------------------------------
    # 统计与维护
    # ------------------------------------------------------------------
    async def size(self) -> dict[str, int]:
        """各队列长度概览。"""
        return {
            "pending": int(await self.redis.llen(config.QUEUE_PENDING)),
            "running": int(await self.redis.llen(config.QUEUE_RUNNING)),
            "done": int(await self.redis.llen(config.QUEUE_DONE)),
            "dead_letter": int(await self.redis.llen(config.QUEUE_DEAD_LETTER)),
        }

    async def clear(self) -> None:
        """清空全部队列(谨慎使用,主要用于重置)。"""
        await self.redis.delete(
            config.QUEUE_PENDING,
            config.QUEUE_RUNNING,
            config.QUEUE_DONE,
            config.QUEUE_DEAD_LETTER,
        )
        logger.warning("已清空所有任务队列")
