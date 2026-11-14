"""关键成功事件与业务共用事务；跨存储操作先持久化意图。"""
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Literal
from collections.abc import Iterator
import hashlib
import logging
import uuid

import psycopg
from psycopg.types.json import Jsonb
from starlette.exceptions import HTTPException

from utils.observability.context import current_context, bind_context

AuditResult = Literal["started", "accepted", "succeeded", "failed", "denied", "partial", "cancelled"]
AuditValue = str | int | bool | list[str]
logger = logging.getLogger("shallow_ask_deep_answer.audit")
_conninfo: str | None = None


def configure_audit(conninfo: str) -> None:
    global _conninfo
    _conninfo = conninfo


def resource_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def append_audit(cur: psycopg.Cursor, *, user_id: int | None, action: str,
                 resource_type: str, resource_id: str, result: AuditResult,
                 operation_id: str | None = None, event_key: str | None = None,
                 project_id: str | None = None, error_code: str | None = None,
                 system: bool = False, details: dict[str, AuditValue] | None = None,
                 trace_id: str | None = None) -> None:
    context = current_context.get()
    operation = operation_id or context.http_request_id or uuid.uuid4().hex
    cur.execute("""
        INSERT INTO audit_events(event_key, operation_id, user_id, actor_type, action,
          resource_type, resource_id, project_id, result, error_code, http_request_id, trace_id, details)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (event_key) DO NOTHING
    """, (event_key or uuid.uuid4().hex, operation, user_id,
          "system" if system else "anonymous" if user_id is None else "user", action,
          resource_type, resource_id, project_id, result, error_code,
          context.http_request_id, trace_id if trace_id is not None else context.trace_id, Jsonb(details or {})))


@dataclass
class AuditOperation:
    user_id: int | None
    action: str
    resource_type: str
    resource_id: str
    project_id: str | None = None
    system: bool = False
    operation_id: str = field(default_factory=lambda: current_context.get().http_request_id or uuid.uuid4().hex)
    result: AuditResult = "succeeded"
    details: dict[str, AuditValue] = field(default_factory=dict)

    def record(self, result: AuditResult, error_code: str | None = None) -> None:
        # 独立提交确保后续业务回滚不会删除操作意图或失败记录。
        if _conninfo is None:
            raise RuntimeError("审计数据库尚未初始化")
        with psycopg.connect(_conninfo, connect_timeout=5,
                             options="-c statement_timeout=5000") as conn:
            with conn.cursor() as cur:
                append_audit(cur, user_id=self.user_id, action=self.action,
                             resource_type=self.resource_type, resource_id=self.resource_id,
                             project_id=self.project_id, result=result, system=self.system,
                             operation_id=self.operation_id, error_code=error_code, details=self.details,
                             event_key=f"{self.operation_id}:{self.action}:{self.resource_id}:{result}")


@contextmanager
def audit_operation(user_id: int | None, action: str, resource_type: str, resource_id: str,
                    *, project_id: str | None = None, system: bool = False,
                    transactional: bool = False) -> Iterator[AuditOperation]:
    operation = AuditOperation(user_id, action, resource_type, resource_id, project_id, system)
    parent = current_context.get()
    with bind_context(replace(parent, user_id=user_id, project_id=project_id,
                              trace_id=parent.trace_id or uuid.uuid4().hex)):
        operation.record("started")
        try:
            yield operation
        except Exception as exc:
            try:
                operation.record("denied" if isinstance(exc, HTTPException) and 400 <= exc.status_code < 500 else "failed",
                                 f"http_{exc.status_code}" if isinstance(exc, HTTPException) else type(exc).__name__)
            except psycopg.Error:
                # 原始业务错误继续传播，审计故障单独明确告警。
                logger.exception("[审计] 失败事件写入失败 operation_id=%s", operation.operation_id)
            raise
        else:
            if not transactional:
                operation.record(operation.result)
