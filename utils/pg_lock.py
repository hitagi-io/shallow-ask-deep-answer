"""PostgreSQL 咨询锁（advisory lock）工具：多进程/多实例部署下的跨进程互斥。

- `exclusive_section`：排他锁，用于初始化和索引写入。
- `shared_section`：共享锁，用于需要与索引写入隔离的读取。
- `SingleInstanceGuard`：会话级锁，让定时任务在多实例中只由一个执行。

进程退出或连接断开时会话级锁自动释放，其他实例会在下一次尝试时接管。
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib

import psycopg

from utils.logging_config import get_logger

logger = get_logger(__name__)

PG_LOCK_KEY_SETUP = 0x64656571


def project_index_lock_key(user_id: int, project_id: str) -> int:
    """每个账号项目独立的稳定咨询锁，不阻塞其他项目的索引操作。"""
    from uuid import UUID
    scope = f"knowledge-index:{int(user_id)}:{UUID(str(project_id))}"
    return int.from_bytes(hashlib.sha256(scope.encode()).digest()[:8], "big", signed=True)


@contextmanager
def _lock_section(conninfo: str, lock_key: int, timeout_seconds: float | None, *, shared: bool):
    acquire_sql = "SELECT pg_advisory_lock_shared(%s)" if shared else "SELECT pg_advisory_lock(%s)"
    release_sql = "SELECT pg_advisory_unlock_shared(%s)" if shared else "SELECT pg_advisory_unlock(%s)"
    label = "共享锁" if shared else "锁"
    conn = psycopg.connect(conninfo, autocommit=True)
    try:
        with conn.cursor() as cur:
            if timeout_seconds is None:
                cur.execute(acquire_sql, (lock_key,))
            else:
                cur.execute(
                    "SELECT set_config('lock_timeout', %s, false)",
                    (str(int(timeout_seconds * 1000)),),
                )
                try:
                    cur.execute(acquire_sql, (lock_key,))
                except psycopg.errors.LockNotAvailable as exc:
                    raise TimeoutError(
                        f"等待{label} {hex(lock_key)} 超过 {timeout_seconds:g} 秒"
                    ) from exc
        try:
            yield
        finally:
            with conn.cursor() as cur:
                cur.execute(release_sql, (lock_key,))
    finally:
        conn.close()


def exclusive_section(conninfo: str, lock_key: int, timeout_seconds: float | None = None):
    """排他锁：用于初始化和索引写入，同一时刻仅允许一个持有者进入。

    timeout_seconds 限制等待锁的时长，超时抛 TimeoutError。
    写操作可能包含外部 embedding 调用，应设置等待上限，避免长期占用线程与连接。
    """
    return _lock_section(conninfo, lock_key, timeout_seconds, shared=False)


def shared_section(conninfo: str, lock_key: int, timeout_seconds: float | None = None):
    """共享锁：允许并发读取，但与 exclusive_section 互斥，避免读取中间状态。

    timeout_seconds 限制等待锁的时长，超时抛 TimeoutError。
    """
    return _lock_section(conninfo, lock_key, timeout_seconds, shared=True)


class SingleInstanceGuard:
    """跨进程单例执行锁，基于 PG 会话级咨询锁（advisory lock）。

    持有者崩溃或连接中断时锁自动释放，因此不需要额外组件来决定由哪个进程执行。
    """

    def __init__(self, conninfo: str, lock_key: int):
        self._conninfo = conninfo
        self._lock_key = lock_key
        self._conn: psycopg.Connection | None = None
        self._held = False

    def try_acquire(self) -> bool:
        """尝试成为执行者；已持有时先用一次轻量往返确认会话仍然有效。"""
        if self._held and self._conn is not None and not self._conn.closed:
            try:
                with self._conn.cursor() as cur:
                    cur.execute("SELECT 1")
                return True
            except psycopg.OperationalError:
                logger.warning("[定时任务] 单例锁连接失效，尝试重新获取", exc_info=True)
                self._conn.close()
                self._conn = None
        self._held = False
        try:
            if self._conn is None or self._conn.closed:
                self._conn = psycopg.connect(self._conninfo, autocommit=True)
            with self._conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (self._lock_key,))
                self._held = bool(cur.fetchone()[0])
        except psycopg.OperationalError:
            logger.warning("[定时任务] 无法获取单例锁，等待下一轮重试", exc_info=True)
            self._held = False
            if self._conn is not None:
                self._conn.close()
                self._conn = None
        return self._held
