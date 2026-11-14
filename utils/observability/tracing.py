"""有界、批量写入的关键 Span；诊断写入失败不改变业务结果。"""
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from functools import wraps
from queue import Empty, Full, Queue
from typing import ParamSpec, TypeVar
import hashlib
import logging
import threading
import time
import uuid

import psycopg
from psycopg.types.json import Jsonb

from utils.observability.context import ExecutionContext, bind_context, current_context, ExecutionStopped

Attribute = str | int | float | bool | list[str] | list[int]
logger = logging.getLogger("shallow_ask_deep_answer.tracing")
P = ParamSpec("P")
R = TypeVar("R")


@dataclass
class Span:
    name: str
    context: ExecutionContext
    parent_span_id: str | None
    attributes: dict[str, Attribute] = field(default_factory=dict)
    status: str = "ok"
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started: float = field(default_factory=time.monotonic)
    duration_ms: float = 0

    def fail(self, code: str) -> None:
        self.status = "error"
        self.attributes["error_code"] = code

    def end(self) -> None:
        self.duration_ms = max(0, (time.monotonic() - self.started) * 1000)
        writer = _writer
        if writer is not None and self.context.trace_id is not None and self.context.user_id is not None:
            writer.submit(self)


def new_span(name: str, attributes: dict[str, Attribute] | None = None,
             *, context: ExecutionContext | None = None) -> Span:
    parent = current_context.get() if context is None else context
    return Span(name, replace(parent, span_id=uuid.uuid4().hex[:16]), parent.span_id, attributes or {})


@contextmanager
def span(name: str, **attributes: Attribute) -> Iterator[Span]:
    record = new_span(name, attributes)
    with bind_context(record.context):
        try:
            yield record
        except (GeneratorExit, ExecutionStopped):
            record.status = "cancelled"
            raise
        except BaseException as exc:
            record.fail(type(exc).__name__)
            raise
        finally:
            record.end()


def traced(name: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        @wraps(function)
        def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            with span(name):
                return function(*args, **kwargs)
        return wrapped
    return decorate


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class TraceWriter:
    def __init__(self, conninfo: str, capacity: int = 2048):
        self.conninfo = conninfo
        self.queue: Queue[Span] = Queue(maxsize=capacity)
        self.stop = threading.Event()
        self.dropped = 0
        self.failed_batches = 0
        self.thread = threading.Thread(target=self._run, name="trace-writer", daemon=True)
        self.thread.start()

    def submit(self, record: Span) -> None:
        try:
            self.queue.put_nowait(record)
        except Full:
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 100 == 0:
                logger.warning("[Trace] 队列已满，累计丢弃 %s 条诊断记录", self.dropped)

    def write(self, batch: list[Span]) -> None:
        with psycopg.connect(self.conninfo, connect_timeout=3, options="-c statement_timeout=3000") as conn:
            with conn.cursor() as cur:
                for item in batch:
                    ctx = item.context
                    # 注销或删除会话后到达的线程尾部记录不会重新创建个人数据。
                    cur.execute("""
                        INSERT INTO trace_spans(span_id,trace_id,parent_span_id,user_id,project_id,
                          generation_id,attempt_id,name,started_at,duration_ms,status,attributes)
                        SELECT %s,%s,%s,id,%s,%s,%s,%s,%s,%s,%s,%s FROM users WHERE id=%s
                          AND (%s::bigint IS NULL OR EXISTS(SELECT 1 FROM generations WHERE id=%s))
                        ON CONFLICT(span_id) DO NOTHING
                    """, (ctx.span_id,ctx.trace_id,item.parent_span_id,ctx.project_id,ctx.generation_id,
                          ctx.attempt_id,item.name,item.started_at,item.duration_ms,item.status,
                          Jsonb(item.attributes),ctx.user_id,ctx.generation_id,ctx.generation_id))

    def _run(self) -> None:
        while not self.stop.is_set() or not self.queue.empty():
            try:
                first = self.queue.get(timeout=0.2)
            except Empty:
                continue
            batch = [first]
            for _ in range(63):
                try:
                    batch.append(self.queue.get_nowait())
                except Empty:
                    break
            try:
                self.write(batch)
            except Exception:
                self.failed_batches += 1
                logger.exception("[Trace] 批量写入失败，丢失 %s 条诊断记录，累计失败批次=%s",
                                 len(batch), self.failed_batches)
            finally:
                for _ in batch:
                    self.queue.task_done()

    def close(self) -> None:
        self.stop.set()
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            logger.error("[Trace] 关闭时未能完成诊断记录写入，剩余=%s", self.queue.qsize())


_writer: TraceWriter | None = None


def start_tracing(conninfo: str) -> None:
    global _writer
    if _writer is not None:
        raise RuntimeError("Trace writer 已启动")
    _writer = TraceWriter(conninfo)


def stop_tracing() -> None:
    global _writer
    if _writer is not None:
        writer, _writer = _writer, None
        writer.close()
