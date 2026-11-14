"""供知识库索引工作进程使用的持久化上传任务队列。"""
from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
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

    def create(self, user_id: int, project_id: str, items: list[dict]) -> str:
        task_id = str(uuid.uuid4())
        trace_id = uuid.uuid4().hex
        with psycopg.connect(self.conninfo) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO upload_tasks (id, user_id, project_id, total, trace_id)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (task_id, user_id, project_id, len(items), trace_id),
                )
                cur.executemany(
                    """
                    INSERT INTO upload_task_items
                        (task_id, position, name, staged_path)
                    VALUES (%s, %s, %s, %s)
                    """,
                    [(task_id, index, item["name"], item["staged_path"])
                     for index, item in enumerate(items)],
                )
                append_audit(cur, user_id=user_id, action="file.upload", resource_type="upload_task",
                             resource_id=task_id, project_id=project_id, result="accepted", operation_id=task_id,
                             event_key=f"upload:{task_id}:accepted", details={"files": len(items)}, trace_id=trace_id)
            conn.commit()
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
        return {**dict(task), "files": [dict(item) for item in items],
                "recovery_required": task["status"] in ("failed", "completed") and any(item["recovery_pending"] for item in items)}

    def claim_next(self) -> tuple[dict, psycopg.Connection] | None:
        """持有会话锁，直到调用方处理完任务。

        任务仍标记为运行中但锁已可获取，说明原工作进程已失去租约。
        返回该任务以执行明确的中断恢复，避免与仍在运行的长任务竞争。
        调用方必须关闭租约连接，即使处理失败也不例外。
        """
        lease = psycopg.connect(self.conninfo, autocommit=True, row_factory=dict_row)
        try:
            candidates = lease.execute("""
                SELECT user_id, project_id, MIN(sequence) AS first_sequence
                FROM upload_tasks WHERE status IN ('queued', 'running')
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
                return dict(row), lease
            lease.close()
            return None
        except BaseException:
            lease.close()
            raise

    def list_tasks(self, user_id: int, project_id: str) -> list[dict]:
        # 历史条数限制不隐藏未结束任务；在同一快照中读取全部所需记录，
        # 无论任务数量多少，都只执行两次查询。
        with psycopg.connect(self.conninfo, row_factory=dict_row) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
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
                return []
            grouped = {task["id"]: [] for task in tasks}
            for item in conn.execute("""
                SELECT task_id, position, name, status, overwritten, indexed, error, progress,
                       recovery IS NOT NULL AND recovery->>'stage' NOT IN ('cleaned','rolled_back','unpublished') AS recovery_pending
                FROM upload_task_items WHERE task_id = ANY(%s) ORDER BY position
            """, (list(grouped),)).fetchall():
                grouped[item.pop("task_id")].append(item)
        return [{**task, "files": grouped[task["id"]], "recovery_required": task["status"] in ("failed", "completed")
                 and any(item["recovery_pending"] for item in grouped[task["id"]])} for task in tasks]

    def items(self, task_id: str) -> list[dict]:
        with psycopg.connect(self.conninfo, row_factory=dict_row) as conn:
            rows = conn.execute(
                """
                SELECT id, position, name, staged_path, status, recovery
                FROM upload_task_items
                WHERE task_id = %s
                ORDER BY position
                """,
                (task_id,),
            ).fetchall()
        return [dict(row) for row in rows]

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

    def has_active(self, user_id: int, project_id: str) -> bool:
        with psycopg.connect(self.conninfo) as conn:
            return conn.execute("""
                SELECT 1 FROM upload_tasks WHERE user_id = %s AND project_id = %s
                  AND (status IN ('queued', 'running') OR EXISTS (
                    SELECT 1 FROM upload_task_items i WHERE i.task_id=upload_tasks.id
                      AND i.recovery IS NOT NULL AND i.recovery->>'stage' NOT IN ('cleaned','rolled_back','unpublished')
                  )) LIMIT 1
            """, (user_id, project_id)).fetchone() is not None

    def pending_recoveries(self, user_id: int, project_id: str) -> list[dict]:
        with psycopg.connect(self.conninfo, row_factory=dict_row) as conn:
            return conn.execute("SELECT i.*, t.user_id,t.project_id FROM upload_task_items i "
                                "JOIN upload_tasks t ON t.id=i.task_id WHERE t.user_id=%s AND t.project_id=%s "
                                "AND t.status IN ('failed','completed') AND i.recovery IS NOT NULL "
                                "AND i.recovery->>'stage' NOT IN ('cleaned','rolled_back','unpublished') ORDER BY t.sequence,i.position",
                                (user_id,project_id)).fetchall()

    def journal(self, task: dict, item: dict) -> UploadJournal:
        return UploadJournal(self.conninfo, task["id"], item["id"], task["user_id"], task["project_id"], item["recovery"])

    def mark_item_running(self, task_id: str, item_id: int) -> None:
        with psycopg.connect(self.conninfo) as conn:
            conn.execute(
                """
                UPDATE upload_task_items
                SET status = 'running', updated_at = CURRENT_TIMESTAMP
                WHERE id = %s AND task_id = %s AND status = 'queued'
                """,
                (item_id, task_id),
            )
            conn.execute(
                "UPDATE upload_tasks SET updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                (task_id,),
            )
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT user_id,project_id FROM upload_tasks WHERE id=%s", (task_id,))
                owner = cur.fetchone()
                if owner is None:
                    raise RuntimeError("上传任务不存在")
                append_audit(cur, user_id=owner["user_id"], action="file.upload.start", resource_type="upload_item",
                             resource_id=str(item_id), project_id=owner["project_id"], operation_id=task_id,
                             result="started", event_key=f"upload:{item_id}:start")
            conn.commit()

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
            conn.commit()

    def finish_item(self, task_id: str, item_id: int, *, status: str,
                    overwritten: bool | None, indexed: bool | None,
                    error: str | None) -> None:
        with psycopg.connect(self.conninfo) as conn:
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
            conn.commit()

    def finish(self, task_id: str) -> None:
        with psycopg.connect(self.conninfo) as conn:
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
            conn.commit()

    def fail(self, task_id: str, error: str) -> None:
        with psycopg.connect(self.conninfo, row_factory=dict_row) as conn:
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
            conn.commit()

