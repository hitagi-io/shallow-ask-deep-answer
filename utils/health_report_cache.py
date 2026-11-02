"""进程内健康报告缓存：合并并发刷新，包括同一轮刷新的异常。"""

import threading
import time
from collections.abc import Callable
from concurrent.futures import Future


class HealthReportCache:
    def __init__(self, ttl: float):
        self._ttl = ttl
        self._lock = threading.Lock()
        self._report: dict | None = None
        self._updated_at = 0.0
        self._revision = 0
        self._pending: Future | None = None

    def set(self, report: dict) -> None:
        """定时检查或手动修复发布新报告，避免被更早启动的检查覆盖。"""
        with self._lock:
            self._report = report
            self._updated_at = time.monotonic()
            self._revision += 1

    def get(self, check: Callable[[], dict], *, refresh: bool = False) -> dict:
        with self._lock:
            # TTL 判断与登记刷新在同一把锁内，排队请求不会重复启动检查。
            if not refresh and self._report is not None and time.monotonic() - self._updated_at < self._ttl:
                return self._report
            pending = self._pending
            leader = pending is None
            if leader:
                pending = self._pending = Future()
            revision = self._revision

        if not leader:
            return pending.result()

        try:
            report = check()
            with self._lock:
                if self._revision == revision:
                    self._report = report
                    self._updated_at = time.monotonic()
                    self._revision += 1
                else:
                    report = self._report
            pending.set_result(report)
            return report
        except BaseException as exc:
            pending.set_exception(exc)
            raise
        finally:
            with self._lock:
                self._pending = None
