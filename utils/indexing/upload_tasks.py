"""供知识库索引工作进程使用的持久化上传任务队列。"""
from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict, Field, field_validator

from utils.indexing.progress import IndexProgress
from utils.indexing.recovery import UploadJournal
from utils.observability.audit import append_audit


_TASK_PROJECT_LOCK_NAMESPACE = "knowledge-upload-queue"


class FileUploadOrder(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    name: str = Field(min_length=1, max_length=220)
    created_at: datetime
    sequence: int = Field(gt=0)
    position: int = Field(ge=0)

    @field_validator("created_at")
    @classmethod
    def check_database_time(cls, value: datetime) -> datetime:
        if value.tzinfo is not None:
            raise ValueError("上传时间须与数据库 TIMESTAMP 格式一致")
        return value


def _project_queue_lock_key(user_id: int, project_id: str) -> int:
    scope = f"{_TASK_PROJECT_LOCK_NAMESPACE}:{int(user_id)}:{project_id}"
    return int.from_bytes(hashlib.sha256(scope.encode()).digest()[:8], "big", signed=True)


class UploadTaskStore:
    """基于 PostgreSQL 的轻量任务存储。

    队列持久化保存，实际索引工作仍在 API 工作进程中执行。
    运行中的任务不会被重复领取，索引变更仍受现有项目咨询锁保护。
    """

    def __init__(self, conninfo: str | None = None):
        self.conninfo = conninfo or os.getenv(
            "PG_CONNECTION_STRING",
            "postgresql://postgres:postgres@localhost:5432/shallow_ask_deep_answer",
        )

    @staticmethod
    def setup(cur) -> None:
        # 源文件暂存完成后即确认接收；索引任务持久化到队列，
        # 由后台工作进程按同一项目的提交顺序执行。
        cur.execute("""
            CREATE TABLE IF NOT EXISTS upload_tasks (
                id          VARCHAR(36) PRIMARY KEY,
                sequence    BIGSERIAL UNIQUE,
                user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                project_id  VARCHAR(36) NOT NULL,
                status      VARCHAR(16) NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'running', 'completed', 'failed')),
                total       INTEGER NOT NULL CHECK (total >= 0),
                completed   INTEGER NOT NULL DEFAULT 0 CHECK (completed >= 0),
                succeeded   INTEGER NOT NULL DEFAULT 0 CHECK (succeeded >= 0),
                failed      INTEGER NOT NULL DEFAULT 0 CHECK (failed >= 0),
                error       TEXT,
                created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                started_at  TIMESTAMP,
                finished_at TIMESTAMP,
                updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (user_id, id),
                FOREIGN KEY (user_id, project_id) REFERENCES projects(user_id, id)
                    ON DELETE CASCADE
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS upload_task_items (
                id           BIGSERIAL PRIMARY KEY,
                task_id      VARCHAR(36) NOT NULL REFERENCES upload_tasks(id)
                                 ON DELETE CASCADE,
                position     INTEGER NOT NULL CHECK (position >= 0),
                name         VARCHAR(220) NOT NULL,
                staged_path  TEXT NOT NULL,
                status       VARCHAR(16) NOT NULL DEFAULT 'queued'
                                 CHECK (status IN ('queued', 'running', 'success', 'error')),
                overwritten  BOOLEAN,
                indexed      BOOLEAN,
                error        TEXT,
                progress     JSONB,
                updated_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (task_id, position)
            );
        """)
        # 已有任务没有阶段记录时保留 NULL，不推测其历史进度。
        cur.execute("ALTER TABLE upload_task_items ADD COLUMN IF NOT EXISTS progress JSONB")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_upload_tasks_queue "
            "ON upload_tasks (sequence) WHERE status IN ('queued', 'running')"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_upload_tasks_project "
            "ON upload_tasks (user_id, project_id, sequence DESC)"
        )

        cur.execute("""
            CREATE TABLE IF NOT EXISTS upload_daily_usage (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                usage_day DATE NOT NULL, bytes BIGINT NOT NULL CHECK (bytes >= 0),
                embedding_chars BIGINT NOT NULL DEFAULT 0 CHECK (embedding_chars >= 0),
                PRIMARY KEY (user_id,usage_day)
            );
            CREATE INDEX IF NOT EXISTS idx_upload_tasks_account_pending ON upload_tasks(user_id)
                WHERE status IN ('queued','running');
            CREATE TABLE IF NOT EXISTS upload_receivers (
                token UUID PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                project_id VARCHAR(36) NOT NULL,
                reserved_bytes BIGINT NOT NULL CHECK (reserved_bytes >= 0),
                expires_at TIMESTAMPTZ NOT NULL,
                FOREIGN KEY (user_id,project_id) REFERENCES projects(user_id,id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_upload_receivers_user ON upload_receivers(user_id, expires_at);
            CREATE INDEX IF NOT EXISTS idx_upload_receivers_expiry ON upload_receivers(expires_at);
        """)

    def create(self, user_id: int, project_id: str, items: list[dict], *, cur) -> str:
        task_id = str(uuid.uuid4())
        trace_id = uuid.uuid4().hex
        cur.execute("INSERT INTO upload_tasks (id,user_id,project_id,total,trace_id) VALUES (%s,%s,%s,%s,%s)",
                    (task_id, user_id, project_id, len(items), trace_id))
        cur.executemany("INSERT INTO upload_task_items (task_id,position,name,staged_path) VALUES (%s,%s,%s,%s)",
                        [(task_id, i, item["name"], item["staged_path"]) for i, item in enumerate(items)])
        cur.execute("""
            INSERT INTO upload_daily_usage(user_id,usage_day,bytes) VALUES (%s,(now() AT TIME ZONE 'UTC')::date,%s)
            ON CONFLICT (user_id,usage_day) DO UPDATE SET bytes=upload_daily_usage.bytes + EXCLUDED.bytes
        """, (user_id, sum(item["size"] for item in items)))
        append_audit(cur, user_id=user_id, action="file.upload", resource_type="upload_task",
                     resource_id=task_id, project_id=project_id, result="accepted", operation_id=task_id,
                     event_key=f"upload:{task_id}:accepted", details={"files": len(items)}, trace_id=trace_id)
        return task_id

    def get(self, user_id: int, project_id: str, task_id: str) -> dict | None:
        with psycopg.connect(self.conninfo, row_factory=dict_row) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            task = conn.execute(
                """
                SELECT id, user_id, project_id, status, total, completed,
                       succeeded, failed, error, created_at, started_at,
                       finished_at, updated_at
                FROM upload_tasks
                WHERE id = %s AND user_id = %s AND project_id = %s
                """,
                (task_id, user_id, project_id),
            ).fetchone()
            if task is None:
                return None
            items = conn.execute(
                """
                SELECT position, name, status, overwritten, indexed, error, progress,
                       recovery IS NOT NULL AND recovery->>'stage' NOT IN ('cleaned','rolled_back','unpublished') AS recovery_pending
                FROM upload_task_items
                WHERE task_id = %s
                ORDER BY position
                """,
                (task_id,),
            ).fetchall()
        return {**task, "files": items,
                "recovery_required": task["status"] in ("failed", "completed") and any(item["recovery_pending"] for item in items)}

    def worker_pool(self, max_workers: int) -> ConnectionPool:
        return ConnectionPool(self.conninfo, min_size=0, max_size=max_workers, open=False,
                              kwargs={"autocommit": True, "row_factory": dict_row}, name="upload-queue")

    @contextmanager
    def claim_next(self, lease: psycopg.Connection) -> Iterator[dict | None]:
        """领取期间独占会话和项目队列锁；取得 running 的任务时由调用方恢复。"""
        try:
            candidates = lease.execute("""
                SELECT user_id, project_id, MIN(sequence) AS first_sequence
                FROM upload_tasks WHERE status IN ('queued', 'running')
                  AND EXISTS (SELECT 1 FROM projects p WHERE p.id=upload_tasks.project_id AND p.deleted_at IS NULL)
                GROUP BY user_id, project_id ORDER BY first_sequence
            """).fetchall()
            for candidate in candidates:
                key = _project_queue_lock_key(candidate["user_id"], candidate["project_id"])
                if not lease.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (key,)).fetchone()["acquired"]:
                    continue
                row = lease.execute("""
                    SELECT id, user_id, project_id, status, trace_id FROM upload_tasks
                    WHERE user_id = %s AND project_id = %s
                      AND status IN ('queued', 'running')
                      AND EXISTS (SELECT 1 FROM projects p WHERE p.id=upload_tasks.project_id AND p.deleted_at IS NULL)
                    ORDER BY sequence LIMIT 1
                """, (candidate["user_id"], candidate["project_id"])).fetchone()
                if row is None:
                    lease.execute("SELECT pg_advisory_unlock(%s)", (key,))
                    continue
                if row["trace_id"] is None:
                    # 迁移前排队的任务在第一次领取时获得稳定追踪编号。
                    row["trace_id"] = uuid.uuid4().hex
                    lease.execute("UPDATE upload_tasks SET trace_id=%s WHERE id=%s", (row["trace_id"], row["id"]))
                if row["status"] == "queued":
                    lease.execute("""
                        UPDATE upload_tasks SET status = 'running', started_at = CURRENT_TIMESTAMP,
                            updated_at = CURRENT_TIMESTAMP WHERE id = %s
                    """, (row["id"],))
                yield row
                lease.execute("SELECT pg_advisory_unlock(%s)", (key,))
                return
        except BaseException:
            # 查询或处理失败时丢弃会话，避免将仍持锁的连接归池。
            lease.close()
            raise
        yield None

    def list_tasks(self, user_id: int, project_id: str) -> tuple[str, list[dict]]:
        # 活动状态包含尚未入队的接收租约，和任务列表读取同一个事务快照。
        with psycopg.connect(self.conninfo, row_factory=dict_row) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            with conn.cursor() as cur:
                activity = self.project_activity(cur, user_id, project_id)
            tasks = conn.execute("""
                SELECT * FROM upload_tasks WHERE user_id = %s AND project_id = %s
                  AND (status IN ('queued', 'running') OR EXISTS (
                      SELECT 1 FROM upload_task_items i WHERE i.task_id=upload_tasks.id
                        AND i.recovery IS NOT NULL AND i.recovery->>'stage' NOT IN ('cleaned','rolled_back','unpublished')
                  ) OR id IN (
                      SELECT id FROM upload_tasks WHERE user_id = %s AND project_id = %s
                      ORDER BY created_at DESC, sequence DESC LIMIT 7))
                ORDER BY created_at DESC, sequence DESC
            """, (user_id, project_id, user_id, project_id)).fetchall()
            if not tasks:
                return activity, []
            grouped = {task["id"]: [] for task in tasks}
            for item in conn.execute("""
                SELECT task_id, position, name, status, overwritten, indexed, error, progress,
                       recovery IS NOT NULL AND recovery->>'stage' NOT IN ('cleaned','rolled_back','unpublished') AS recovery_pending
                FROM upload_task_items WHERE task_id = ANY(%s) ORDER BY position
            """, (list(grouped),)).fetchall():
                grouped[item.pop("task_id")].append(item)
        return activity, [{**task, "files": grouped[task["id"]], "recovery_required": task["status"] in ("failed", "completed")
                 and any(item["recovery_pending"] for item in grouped[task["id"]])} for task in tasks]

    def items(self, task_id: str) -> list[dict]:
        with psycopg.connect(self.conninfo, row_factory=dict_row) as conn:
            return conn.execute(
                """
                SELECT id, position, name, staged_path, status, recovery
                FROM upload_task_items
                WHERE task_id = %s
                ORDER BY position
                """,
                (task_id,),
            ).fetchall()

    def file_upload_order(self, user_id: int, project_id: str) -> list[FileUploadOrder]:
        """查询每个文件最近一次上传，覆盖全部资料，不受最近七条展示限制。"""
        with psycopg.connect(self.conninfo, row_factory=dict_row) as conn:
            rows = conn.execute("""
                SELECT name, created_at, sequence, position FROM (
                    SELECT DISTINCT ON (i.name) i.name, t.created_at, t.sequence, i.position
                    FROM upload_tasks t JOIN upload_task_items i ON i.task_id = t.id
                    WHERE t.user_id = %s AND t.project_id = %s
                    ORDER BY i.name, t.created_at DESC, t.sequence DESC, i.position DESC
                ) latest
                ORDER BY created_at DESC, sequence DESC, position
            """, (user_id, project_id)).fetchall()
        return [FileUploadOrder.model_validate(row) for row in rows]

    @staticmethod
    def project_activity(cur, user_id: int, project_id: str) -> str:
        cur.execute("""
            SELECT CASE
              WHEN EXISTS (SELECT 1 FROM upload_receivers WHERE user_id=%(user)s AND project_id=%(project)s
                           AND expires_at > clock_timestamp())
                OR EXISTS (SELECT 1 FROM upload_tasks WHERE user_id=%(user)s AND project_id=%(project)s
                           AND status IN ('queued','running')) THEN 'updating'
              WHEN EXISTS (SELECT 1 FROM pending_file_deletions WHERE user_id=%(user)s AND project_id=%(project)s)
                OR EXISTS (SELECT 1 FROM upload_tasks t JOIN upload_task_items i ON i.task_id=t.id
                           WHERE t.user_id=%(user)s AND t.project_id=%(project)s AND i.recovery IS NOT NULL
                           AND i.recovery->>'stage' NOT IN ('cleaned','rolled_back','unpublished')) THEN 'recovery'
              ELSE 'idle' END AS activity
        """, {"user": user_id, "project": project_id})
        return cur.fetchone()["activity"]

    @classmethod
    def project_has_active(cls, cur, user_id: int, project_id: str) -> bool:
        return cls.project_activity(cur, user_id, project_id) != "idle"

    def has_active(self, user_id: int, project_id: str) -> bool:
        with psycopg.connect(self.conninfo, row_factory=dict_row) as conn, conn.cursor() as cur:
            return self.project_has_active(cur, user_id, project_id)

    def pending_recoveries(self, user_id: int, project_id: str) -> list[dict]:
        with psycopg.connect(self.conninfo, row_factory=dict_row) as conn:
            return conn.execute("SELECT i.*, t.user_id,t.project_id FROM upload_task_items i "
                                "JOIN upload_tasks t ON t.id=i.task_id WHERE t.user_id=%s AND t.project_id=%s "
                                "AND t.status IN ('failed','completed') AND i.recovery IS NOT NULL "
                                "AND i.recovery->>'stage' NOT IN ('cleaned','rolled_back','unpublished') ORDER BY t.sequence,i.position",
                                (user_id,project_id)).fetchall()

    def journal(self, task: dict, item: dict) -> UploadJournal:
        return UploadJournal(self.conninfo, task["id"], item["id"], task["user_id"], task["project_id"], item["recovery"])

    @contextmanager
    def _write_transaction(self, lease: psycopg.Connection | None):
        # Worker 用持租约的连接写回结果。丢失租约后，旧 worker 无法覆盖接管者的状态。
        if lease is not None:
            with lease.transaction():
                yield lease
        else:
            with psycopg.connect(self.conninfo, row_factory=dict_row) as conn:
                yield conn

    def requeue_unprepared_items(self, task_id: str, lease: psycopg.Connection) -> None:
        """仅在接管时调用。旧 worker 的开始事件证明它在移动文件前必写恢复日志。"""
        with self._write_transaction(lease) as conn:
            conn.execute(
                """
                UPDATE upload_task_items i SET status = 'queued', updated_at = now()
                WHERE i.task_id = %s AND i.status = 'running' AND i.recovery IS NULL
                  AND EXISTS (SELECT 1 FROM audit_events a
                              WHERE a.event_key = 'upload:' || i.id || ':start' AND a.operation_id = i.task_id)
                """,
                (task_id,),
            )

    def update_item_progress(self, task_id: str, item_id: int, progress: IndexProgress) -> None:
        with psycopg.connect(self.conninfo) as conn:
            result = conn.execute(
                """
                UPDATE upload_task_items SET progress = %s, updated_at = CURRENT_TIMESTAMP
                WHERE task_id = %s AND id = %s AND status = 'running'
                """,
                (Jsonb(progress.model_dump()), task_id, item_id),
            )
            if result.rowcount != 1:
                raise RuntimeError("上传任务已不在运行，无法更新入库进度")

    def finish_item(self, task_id: str, item_id: int, *, status: str,
                    overwritten: bool | None, indexed: bool | None,
                    error: str | None, lease: psycopg.Connection | None = None) -> None:
        with self._write_transaction(lease) as conn:
            conn.execute(
                """
                UPDATE upload_task_items
                SET status = %s, overwritten = %s, indexed = %s,
                    error = %s, progress = CASE WHEN %s = 'success' THEN %s ELSE progress END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s AND task_id = %s
                """,
                (status, overwritten, indexed, error, status,
                 Jsonb(IndexProgress(stage="finalizing", completed=1, total=1).model_dump()), item_id, task_id),
            )
            conn.execute(
                """
                UPDATE upload_tasks t
                SET completed = q.completed, succeeded = q.succeeded,
                    failed = q.failed, updated_at = CURRENT_TIMESTAMP
                FROM (
                    SELECT task_id,
                           COUNT(*) FILTER (WHERE status IN ('success', 'error')) AS completed,
                           COUNT(*) FILTER (WHERE status = 'success') AS succeeded,
                           COUNT(*) FILTER (WHERE status = 'error') AS failed
                    FROM upload_task_items
                    WHERE task_id = %s
                    GROUP BY task_id
                ) q
                WHERE t.id = q.task_id
                """,
                (task_id,),
            )
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT user_id,project_id FROM upload_tasks WHERE id=%s", (task_id,))
                owner = cur.fetchone()
                if owner is None:
                    raise RuntimeError("上传任务不存在")
                append_audit(cur, user_id=owner["user_id"], action="file.upload.finish", resource_type="upload_item",
                             resource_id=str(item_id), project_id=owner["project_id"], operation_id=task_id,
                             result="succeeded" if status == "success" else "partial" if indexed else "failed",
                             event_key=f"upload:{item_id}:finish:{status}:{indexed}")

    def finish(self, task_id: str, *, lease: psycopg.Connection | None = None) -> None:
        with self._write_transaction(lease) as conn:
            conn.execute(
                """
                UPDATE upload_tasks t
                SET status = 'completed', error = CASE WHEN q.failed = 0 THEN NULL ELSE t.error END,
                    completed = q.completed, succeeded = q.succeeded,
                    failed = q.failed, finished_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                FROM (
                    SELECT task_id,
                           COUNT(*) FILTER (WHERE status IN ('success', 'error')) AS completed,
                           COUNT(*) FILTER (WHERE status = 'success') AS succeeded,
                           COUNT(*) FILTER (WHERE status = 'error') AS failed
                    FROM upload_task_items
                    WHERE task_id = %s
                    GROUP BY task_id
                ) q
                WHERE t.id = q.task_id AND q.completed = t.total
                """,
                (task_id,),
            )

    def fail(self, task_id: str, error: str, *, lease: psycopg.Connection | None = None) -> None:
        with self._write_transaction(lease) as conn:
            conn.execute(
                """
                UPDATE upload_task_items
                SET status = 'error', error = COALESCE(error, %s),
                    updated_at = CURRENT_TIMESTAMP
                WHERE task_id = %s AND status IN ('queued', 'running')
                """,
                (error, task_id),
            )
            result = conn.execute(
                """
                UPDATE upload_tasks t
                SET status = 'failed', error = %s,
                    completed = q.completed, succeeded = q.succeeded,
                    failed = q.failed, finished_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                FROM (
                    SELECT task_id,
                           COUNT(*) FILTER (WHERE status IN ('success', 'error')) AS completed,
                           COUNT(*) FILTER (WHERE status = 'success') AS succeeded,
                           COUNT(*) FILTER (WHERE status = 'error') AS failed
                    FROM upload_task_items
                    WHERE task_id = %s
                    GROUP BY task_id
                ) q
                WHERE t.id = q.task_id AND t.status = 'running'
                RETURNING t.user_id,t.project_id,t.trace_id
                """,
                (error, task_id),
            )
            owner = result.fetchone()
            if owner is not None:
                with conn.cursor() as cur:
                    append_audit(cur, user_id=owner["user_id"], action="file.upload.fail",
                                 resource_type="upload_task", resource_id=task_id, project_id=owner["project_id"],
                                 result="failed", operation_id=task_id, event_key=f"upload:{task_id}:failed",
                                 error_code="upload_interrupted", trace_id=owner["trace_id"], system=True)

