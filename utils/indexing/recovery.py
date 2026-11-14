"""覆盖上传的持久化恢复记录；阶段先于外部副作用提交。"""
from typing import Literal, TypedDict
import os
import uuid

from pydantic import BaseModel, ConfigDict, Field
import psycopg
from psycopg.types.json import Jsonb

from utils.core.project_paths import scoped_knowledge_file
from utils.documents.knowledge_files import compute_file_sha256
from utils.observability.audit import append_audit

RecoveryStage = Literal["prepared", "replacing", "indexing", "writing", "publishing", "published", "cleaned", "rolled_back", "unpublished"]
TERMINAL_STAGES = ("cleaned", "rolled_back", "unpublished")


class PublicationUncertain(RuntimeError):
    """发布或恢复状态尚未确认；必须保留源文件、备份与向量供核对。"""


class UploadRecoveryResult(TypedDict):
    ok: bool
    committed: bool
    overwritten: bool
    error: str | None


class UploadRecovery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: Literal[1] = 1
    stage: RecoveryStage
    destination: str
    staged_path: str
    backup_path: str | None
    old_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    new_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    index_id: str | None = None


class UploadJournal:
    def __init__(self, conninfo: str, task_id: str, item_id: int, user_id: int, project_id: str,
                 recovery: object = None):
        self.conninfo, self.task_id, self.item_id = conninfo, task_id, item_id
        self.user_id, self.project_id = user_id, project_id
        self.record = None if recovery is None else UploadRecovery.model_validate(recovery)

    def _save(self, record: UploadRecovery) -> None:
        with psycopg.connect(self.conninfo, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE upload_task_items SET recovery=%s,updated_at=now() WHERE id=%s AND task_id=%s",
                            (Jsonb(record.model_dump()), self.item_id, self.task_id))
                if cur.rowcount != 1:
                    raise RuntimeError("上传任务已不存在，停止修改文件")
                append_audit(cur, user_id=self.user_id, action="file.upload.stage", resource_type="upload_item",
                             resource_id=str(self.item_id), project_id=self.project_id, result="accepted",
                             operation_id=self.task_id, event_key=f"upload:{self.item_id}:{record.stage}",
                             details={"stage": record.stage, "content_hash": record.new_hash})
        self.record = record

    def prepare(self, directory: str, destination: str, staged_path: str) -> UploadRecovery:
        if self.record is not None:
            raise RuntimeError("已有恢复记录，必须先核对原操作")
        new_hash = compute_file_sha256(staged_path)
        if new_hash is None:
            raise OSError("无法读取上传暂存文件")
        existed = os.path.isfile(destination)
        old_hash = compute_file_sha256(destination) if existed else None
        if existed and old_hash is None:
            raise OSError("无法校验被覆盖的原文件")
        backup = scoped_knowledge_file(directory, os.path.join(directory, f".bak_{uuid.uuid4().hex}")) if existed else None
        record = UploadRecovery(stage="prepared", destination=destination, staged_path=staged_path,
                                backup_path=backup, old_hash=old_hash, new_hash=new_hash)
        self._save(record)
        return record

    def advance(self, stage: RecoveryStage, *, index_id: str | None = None) -> None:
        if self.record is None:
            raise RuntimeError("上传恢复记录尚未建立")
        record = self.record.model_copy(update={"stage": stage, **({"index_id": index_id} if index_id is not None else {})})
        self._save(record)

    def validated_record(self, directory: str) -> UploadRecovery:
        record = self.record
        if record is None:
            raise RuntimeError("缺少上传恢复记录，无法自动恢复")
        for path in (record.destination, record.staged_path, record.backup_path):
            if path is not None:
                scoped_knowledge_file(directory, path)
                if os.path.dirname(path) != directory:
                    raise ValueError("恢复文件必须位于当前项目目录")
        if not os.path.basename(record.staged_path).startswith(".upload_"):
            raise ValueError("无效的恢复暂存文件")
        if record.backup_path is not None and not os.path.basename(record.backup_path).startswith(".bak_"):
            raise ValueError("无效的恢复备份文件")
        return record

    @property
    def pending(self) -> bool:
        return self.record is not None and self.record.stage not in TERMINAL_STAGES
