"""按服务端认证账号查询执行诊断与审计，校验存储数据后再返回。"""
from datetime import datetime
from typing import Literal

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field

from utils.observability.tracing import Attribute
from utils.observability.audit import AuditResult, AuditValue


class TraceSpanRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    span_id: str
    parent_span_id: str | None
    name: str
    started_at: datetime
    duration_ms: float = Field(ge=0, allow_inf_nan=False)
    status: Literal["ok", "error", "cancelled"]
    attributes: dict[str, Attribute]


class AuditRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: int
    operation_id: str
    actor_type: Literal["user", "system", "anonymous"]
    action: str
    resource_type: str
    resource_id: str
    project_id: str | None
    result: AuditResult
    error_code: str | None
    trace_id: str | None
    created_at: datetime
    details: dict[str, AuditValue]


def generation_trace(conninfo: str, user_id: int, generation_id: int) -> dict | None:
    with psycopg.connect(conninfo, row_factory=dict_row) as conn:
        owner = conn.execute("SELECT id,trace_id,status,result_kind,error_code,retry_of FROM generations WHERE id=%s AND user_id=%s",
                             (generation_id, user_id)).fetchone()
        if owner is None:
            return None
        rows = conn.execute("SELECT span_id,parent_span_id,name,started_at,duration_ms,status,attributes FROM trace_spans "
                            "WHERE trace_id=%s AND generation_id=%s AND user_id=%s ORDER BY started_at,span_id LIMIT 501",
                            (owner["trace_id"], generation_id, user_id)).fetchall()
    return {**owner, "spans": [TraceSpanRecord.model_validate(row).model_dump(mode="json") for row in rows[:500]],
            "truncated": len(rows) > 500}


def audit_page(conninfo: str, user_id: int, before: int | None, limit: int) -> dict:
    with psycopg.connect(conninfo, row_factory=dict_row) as conn:
        rows = conn.execute("SELECT id,operation_id,actor_type,action,resource_type,resource_id,project_id,result,"
                            "error_code,trace_id,created_at,details FROM audit_events "
                            "WHERE user_id=%s AND (%s::bigint IS NULL OR id<%s) ORDER BY id DESC LIMIT %s",
                            (user_id,before,before,limit+1)).fetchall()
    return {"events": [AuditRecord.model_validate(row).model_dump(mode="json") for row in rows[:limit]],
            "next_before": rows[limit-1]["id"] if len(rows) > limit else None}


def prune_observability(conninfo: str) -> None:
    """按固定保留期限分批清理；审计触发器只允许删除已过期事件。"""
    with psycopg.connect(conninfo, connect_timeout=5, options="-c statement_timeout=5000") as conn:
        conn.execute("DELETE FROM trace_spans WHERE span_id IN (SELECT span_id FROM trace_spans "
                     "WHERE started_at < now()-interval '14 days' LIMIT 1000)")
        conn.execute("DELETE FROM audit_events WHERE id IN (SELECT id FROM audit_events "
                     "WHERE created_at < now()-interval '90 days' ORDER BY created_at,id LIMIT 1000)")
