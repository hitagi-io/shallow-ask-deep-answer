"""在查询数据库之前限制匿名请求，每个 worker 的来源表受总请求额度约束。"""
from collections import Counter, deque
from threading import Lock
from time import monotonic


class AuthIngress:
    def __init__(self, per_ip: int, total: int, seconds: int):
        if min(total, seconds) <= 0 or per_ip < 0:
            raise ValueError("认证请求总额度和窗口须为正数，来源额度须为非负数")
        self.per_ip = per_ip
        self.total = total
        self.seconds = seconds
        self._accepted = deque()
        self._counts = Counter()
        self._denied = 0
        self._lock = Lock()

    def allow(self, ip: str) -> bool:
        with self._lock:
            now = monotonic()
            while self._accepted and self._accepted[0][0] <= now - self.seconds:
                _, source = self._accepted.popleft()
                self._counts[source] -= 1
                if not self._counts[source]:
                    del self._counts[source]
            if len(self._accepted) >= self.total or self._counts[ip] >= self.per_ip:
                self._denied += 1
                return False
            self._accepted.append((now, ip))
            self._counts[ip] += 1
            return True

    def record_denied(self, count: int = 1) -> None:
        with self._lock:
            self._denied += count

    def take_denied(self) -> int:
        with self._lock:
            count, self._denied = self._denied, 0
            return count
