"""手机 ↔ 电脑端 relay：手机投递任务，电脑端（jarvis-bridge）长轮询领取，回传结果。

为什么长轮询而不是 WebSocket：公网入口是手写的 nanmount-server.py 反代，只转发
/api 与 /v1 的普通 HTTP，WebSocket 过不去。长轮询在同样的延迟量级下不需要改反代。

单进程语义：core 以 uvicorn 单 worker 运行（见 Dockerfile），所以队列放内存即可。
进程重启后未完成任务直接丢弃——电脑没连着的任务本来也不该留。
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Optional

# 电脑端多久没来 pull 就算离线
ONLINE_WINDOW = 45.0
# 任务在队列里最多活多久（秒），过期由 GC 清掉
JOB_TTL = 600.0
# 已被领取但迟迟没回传的任务，多久后放回队列重投（桥进程被杀时会出现这种孤儿）
STALE_CLAIM = 70.0


class RelayJob:
    __slots__ = ("id", "payload", "created_at", "claimed_at", "result", "done")

    def __init__(self, payload: dict[str, Any]) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.payload = payload
        self.created_at = time.time()
        self.claimed_at: Optional[float] = None
        self.result: Optional[dict[str, Any]] = None
        self.done = asyncio.Event()


class RelayHub:
    def __init__(self) -> None:
        self._jobs: dict[str, RelayJob] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self.last_pull = 0.0

    # ------------------------------------------------------------ 状态
    def pc_online(self) -> bool:
        return (time.time() - self.last_pull) < ONLINE_WINDOW

    def pc_last_seen(self) -> Optional[float]:
        return self.last_pull or None

    # ------------------------------------------------------------ 手机侧
    async def submit(self, payload: dict[str, Any]) -> RelayJob:
        self._gc()
        job = RelayJob(payload)
        self._jobs[job.id] = job
        await self._queue.put(job.id)
        return job

    # ------------------------------------------------------------ 电脑侧
    async def pull(self, wait: float) -> Optional[RelayJob]:
        """等最多 wait 秒拿一个任务；被领取太久没回传的会自动重投。"""
        self.last_pull = time.time()
        self._requeue_stale()
        try:
            job_id = await asyncio.wait_for(self._queue.get(), timeout=max(wait, 0.01))
        except asyncio.TimeoutError:
            return None
        self.last_pull = time.time()
        job = self._jobs.get(job_id)
        if job is None:      # 已被 GC
            return None
        job.claimed_at = time.time()
        return job

    async def complete(self, job_id: str, result: dict[str, Any]) -> bool:
        job = self._jobs.get(job_id)
        if job is None:
            return False
        job.result = result
        job.done.set()
        return True

    # ------------------------------------------------------------ 内部
    def _requeue_stale(self) -> None:
        now = time.time()
        for job in list(self._jobs.values()):
            if (
                job.claimed_at is not None
                and job.result is None
                and now - job.claimed_at > STALE_CLAIM
            ):
                job.claimed_at = None
                self._queue.put_nowait(job.id)

    def _gc(self) -> None:
        now = time.time()
        for job_id, job in list(self._jobs.items()):
            if job.result is not None:
                if now - job.created_at > 60:
                    self._jobs.pop(job_id, None)
            elif now - job.created_at > JOB_TTL:
                self._jobs.pop(job_id, None)

    def stats(self) -> dict[str, Any]:
        return {
            "online": self.pc_online(),
            "pending": sum(1 for j in self._jobs.values() if j.result is None),
            "last_pull": self.last_pull,
        }
