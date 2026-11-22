"""按账号计算实际源文件占用。暂存文件、覆盖备份和旧版上传都计入总量。"""
from dataclasses import dataclass
import os
from pathlib import Path
import uuid
import psycopg

from utils.core.pg_lock import shared_section
from utils.core.project_paths import get_knowledge_dir


@dataclass(frozen=True)
class UploadLimits:
    storage_bytes: int
    daily_bytes: int
    pending_tasks: int

    def __post_init__(self):
        if min(self.storage_bytes, self.daily_bytes, self.pending_tasks) < 1:
            raise ValueError("账号存储、每日上传量和待处理任务上限必须为正数")


class UploadQuotaExceeded(ValueError):
    def __init__(self, detail: str, status: int = 429):
        super().__init__(detail)
        self.status = status


@dataclass(frozen=True)
class UploadReservation:
    token: str
    max_bytes: int
    limiting_error: str
    status: int

    def check_bytes(self, size: int) -> None:
        if size > self.max_bytes:
            raise UploadQuotaExceeded(self.limiting_error, self.status)


def storage_lock_key(user_id: int) -> int:
    return (0x75706C64 << 32) | user_id


def moving_upload_files(conninfo: str, user_id: int):
    # 只在重命名期间与用量统计互斥，解析和向量化可以与后续暂存并行。
    return shared_section(conninfo, storage_lock_key(user_id), timeout_seconds=30)


def _storage_available(account_dir: Path, remaining: int) -> int:
    if remaining <= 0 or not account_dir.exists():
        return remaining
    with os.scandir(account_dir) as projects:
        for project in projects:
            if not project.is_dir(follow_symlinks=False) or project.is_junction():
                continue
            try:
                with os.scandir(project.path) as files:
                    for file in files:
                        try:
                            if file.is_file(follow_symlinks=False):
                                remaining -= file.stat(follow_symlinks=False).st_size
                        except FileNotFoundError:
                            continue  # 并发删除释放容量。
                        if remaining <= 0:
                            return remaining
            except FileNotFoundError:
                continue
    return remaining


def _capacity(cur, user_id: int, directory: str, limits: UploadLimits) -> tuple[int, str, int]:
    cur.execute("SELECT count(*), COALESCE(sum(reserved_bytes),0)::bigint FROM upload_receivers "
                "WHERE user_id=%s AND expires_at>clock_timestamp()", (user_id,))
    receivers, reserved = cur.fetchone()
    cur.execute("SELECT count(*) FROM upload_tasks WHERE user_id=%s AND status IN ('queued','running')", (user_id,))
    if cur.fetchone()[0] + receivers >= limits.pending_tasks:
        raise UploadQuotaExceeded("账号待处理上传任务已满，请等待任务完成")
    cur.execute("SELECT bytes FROM upload_daily_usage WHERE user_id=%s AND usage_day=(now() AT TIME ZONE 'UTC')::date",
                (user_id,))
    row = cur.fetchone()
    daily = limits.daily_bytes - (row[0] if row else 0) - reserved
    daily_error = "账号今日可用上传量不足，请在 UTC 零点后重试"
    if daily <= 0:
        raise UploadQuotaExceeded(daily_error)
    storage = _storage_available(Path(directory).parent, limits.storage_bytes - reserved)
    if storage <= daily:
        return storage, "账号存储空间不足，请先删除不再使用的资料", 413
    return daily, daily_error, 429


def reserve_upload(cur, user_id: int, project_id: str, request_bytes: int, limits: UploadLimits,
                   lifetime: int) -> UploadReservation:
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (storage_lock_key(user_id),))
    size, error, status = _capacity(cur, user_id, get_knowledge_dir(user_id, project_id), limits)
    if size < 1:
        raise UploadQuotaExceeded(error, status)
    if request_bytes < size:
        size, error, status = request_bytes, "上传文件超过请求声明的大小上限", 413
    token = str(uuid.uuid4())
    cur.execute("INSERT INTO upload_receivers(token,user_id,project_id,reserved_bytes,expires_at) "
                "VALUES (%s,%s,%s,%s,clock_timestamp()+make_interval(secs => %s))",
                (token, user_id, project_id, size, lifetime))
    return UploadReservation(token, size, error, status)


def claim_upload(cur, user_id: int, directory: str, incoming: int, limits: UploadLimits, token: str) -> None:
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (storage_lock_key(user_id),))
    cur.execute("DELETE FROM upload_receivers WHERE token=%s AND user_id=%s AND expires_at>clock_timestamp() "
                "RETURNING reserved_bytes", (token, user_id))
    row = cur.fetchone()
    if row is None or incoming > row[0]:
        raise UploadQuotaExceeded("上传预留额度已失效，请重新上传", 408)
    # 和入队共用事务，再核对旧文件、当前日期及其他接收中的请求。
    size, error, status = _capacity(cur, user_id, directory, limits)
    if incoming > size:
        raise UploadQuotaExceeded(error, status)


def staged_upload_prefix(token: str) -> str:
    return f".upload_{token}_"


def discard_upload(cur, token: str) -> None:
    # 接收记录与入队互斥。即使提交回执丢失，已入队的文件也绝不会被此处清理。
    cur.execute("DELETE FROM upload_receivers WHERE token=%s RETURNING user_id, project_id", (token,))
    receipt = cur.fetchone()
    if receipt is None:
        return
    prefix = staged_upload_prefix(token)
    try:
        with os.scandir(get_knowledge_dir(*receipt)) as entries:
            for entry in entries:
                if entry.name.startswith(prefix) and entry.name[len(prefix):].isdecimal():
                    Path(entry.path).unlink(missing_ok=True)
    except FileNotFoundError:
        pass


def reserve_embedding_chars(conninfo: str, user_id: int, chars: int, limit: int) -> None:
    if chars > limit:
        raise UploadQuotaExceeded("单份资料的入库文本超过每日额度，请拆分资料")
    with psycopg.connect(conninfo, connect_timeout=5) as conn:
        accepted = conn.execute("""
            INSERT INTO upload_daily_usage(user_id,usage_day,bytes,embedding_chars)
            VALUES (%s,(now() AT TIME ZONE 'UTC')::date,0,%s)
            ON CONFLICT (user_id,usage_day) DO UPDATE
               SET embedding_chars=upload_daily_usage.embedding_chars+EXCLUDED.embedding_chars
             WHERE upload_daily_usage.embedding_chars+EXCLUDED.embedding_chars <= %s
            RETURNING 1
        """, (user_id, chars, limit)).fetchone()
        if accepted is None:
            raise UploadQuotaExceeded("账号今日入库文本额度已达上限，请在 UTC 零点后重试")
