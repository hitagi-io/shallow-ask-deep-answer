"""进程内健康报告缓存：异步合并检查，等待者不持有线程或数据库连接。"""

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


class HealthReportInvalidated(RuntimeError):
    """检查期间项目发生变更，旧结果不能作为当前状态返回。"""


@dataclass
class _Refresh:
    task: asyncio.Task
    settled: asyncio.Future
    waiters: int = 0
    stopping: bool = False


class HealthReportCache:
    def __init__(self, ttl: float):
        self._ttl = ttl
        # set / invalidate 也会由同步写接口和后台修复线程调用。
        self._lock = threading.Lock()
        self._report: dict | None = None
        self._updated_at = 0.0
        self._revision = 0
        self._pending: _Refresh | None = None

    def set(self, report: dict) -> None:
        """发布修复后的报告，避免被更早启动的检查覆盖。"""
        with self._lock:
            self._report = report
            self._updated_at = time.monotonic()
            self._revision += 1

    def invalidate(self) -> None:
        """失效报告，但保留在途任务，直到它结束并释放扫描资源。"""
        with self._lock:
            self._report = None
            self._updated_at = 0.0
            self._revision += 1

    def peek(self) -> dict | None:
        """只读取有效缓存；过期或缺失时返回 None，不触发扫描。"""
        with self._lock:
            return self._report if time.monotonic() - self._updated_at < self._ttl else None

    async def _refresh(self, check: Callable[[], Awaitable[dict]], revision: int) -> dict:
        report = await check()
        # 即使下层在取消后返回，也不能发布已经放弃的检查结果。
        if asyncio.current_task().cancelling():
            raise asyncio.CancelledError
        with self._lock:
            if self._revision == revision:
                self._report = report
                self._updated_at = time.monotonic()
                self._revision += 1
            elif self._report is None:
                raise HealthReportInvalidated("项目状态已变更，请重新检查")
            else:
                report = self._report
        return report

    def _settle(self, refresh: _Refresh) -> None:
        # 只有任务真正退出（包括 check 的 finally 清理）才准许下一轮执行。
        with self._lock:
            if self._pending is refresh:
                self._pending = None
        if not refresh.task.cancelled():
            refresh.task.exception()  # 所有调用者已离开时也回收异常。
        refresh.settled.set_result(None)

    async def get(self, check: Callable[[], Awaitable[dict]], *, refresh: bool = False) -> dict:
        """调用方先鉴权；只有 check 内部获取项目锁和数据库连接。

        get 在同一个服务事件循环使用，set / invalidate 可跨线程调用。
        取消单个等待者不会取消其他请求；最后一个等待者取消后，先等待
        check 释放资源，再允许新的检查启动。
        """
        while True:
            with self._lock:
                if not refresh and self._report is not None and time.monotonic() - self._updated_at < self._ttl:
                    return self._report
                pending = self._pending
                if pending is None:
                    loop = asyncio.get_running_loop()
                    task = loop.create_task(self._refresh(check, self._revision))
                    pending = self._pending = _Refresh(task, loop.create_future())
                    task.add_done_callback(lambda _, flight=pending: self._settle(flight))
                stopping = pending.stopping
                if not stopping:
                    pending.waiters += 1
            if stopping:
                # 不能加入已取消的任务，更不能在它清理前启动第二次扫描。
                await asyncio.shield(pending.settled)
                continue
            try:
                return await asyncio.shield(pending.task)
            finally:
                with self._lock:
                    pending.waiters -= 1
                    stop = pending.waiters == 0 and not pending.task.done()
                    if stop:
                        pending.stopping = True
                if stop:
                    # 服务关闭时扫描也可能已被取消，不重复打断它的 finally。
                    if not pending.task.cancelling():
                        pending.task.cancel()
                    await asyncio.shield(pending.settled)
