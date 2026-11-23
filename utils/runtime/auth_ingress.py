"""按来源 IP 或已签名的会话标识限流，每个 worker 的来源表受总额度约束。"""
from collections import Counter, deque
from threading import Lock
from time import monotonic


class AuthIngress:
    def __init__(self, per_source: int, total: int, seconds: int):
        if min(total, seconds) <= 0 or per_source < 0:
            raise ValueError("认证请求总额度和窗口须为正数，来源额度须为非负数")
        self.per_source = per_source
        self.total = total
        self.seconds = seconds
        self._accepted = deque()
        self._counts = Counter()
        self._denied = 0
        self._lock = Lock()

    def allow(self, source: str) -> bool:
        with self._lock:
            now = monotonic()
            while self._accepted and self._accepted[0][0] <= now - self.seconds:
                _, expired = self._accepted.popleft()
                self._counts[expired] -= 1
                if not self._counts[expired]:
                    del self._counts[expired]
            if len(self._accepted) >= self.total or self._counts[source] >= self.per_source:
                self._denied += 1
                return False
            self._accepted.append((now, source))
            self._counts[source] += 1
            return True

    def record_denied(self, count: int = 1) -> None:
        with self._lock:
            self._denied += count

    def take_denied(self) -> int:
        with self._lock:
            count, self._denied = self._denied, 0
            return count
