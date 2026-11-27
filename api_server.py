"""FastAPI 入口：账户、流式对话和知识库管理 API。"""
import os
import json
import time
import uuid
import asyncio
import shutil
import re
import math
import stat
from pathlib import Path
from uuid import UUID
from functools import partial
from collections.abc import Callable
from contextlib import ExitStack, asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, HTTPException, Depends, Header, Request, Query
from starlette.datastructures import UploadFile

import anyio
import psycopg
from psycopg_pool import ConnectionPool
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, field_validator

from chat_agent import ChatAgent, PG_CHECKPOINT_POOL_MAX
from utils.chat.checkpoints import AttemptCheckpoints
from rag.vector_store import get_vector_store, drop_project_vectors
from rag.index_health import IndexHealth
from rag.index_manifest import ManifestStore, current_index_version
from utils.documents.knowledge_files import document_format, list_supported_files
from utils.documents.upload_form import upload_form, upload_request_size
from utils.documents.file_listing import NAME_SORTS, FileFilter, FilePage, FileSort, KnowledgeFile, paginate_files, summarize_files
from utils.retrieval.source_evidence import SourceVersionChanged, read_source_passage
from utils.core.config_loader import vector_db_config
from utils.core.project_paths import get_account_knowledge_dir, get_knowledge_dir, get_knowledge_storage_root, scoped_knowledge_file
from utils.core.pg_lock import SingleInstanceGuard, exclusive_section, shared_section, project_index_lock_key
from utils.chat.generation_runner import run_generation
from utils.chat.subscriptions import POLL_SECONDS, SubscriptionResponse, watch_generation
from utils.observability.context import ExecutionContext, bind_context, current_context
from utils.observability.tracing import span, start_tracing, stop_tracing
from utils.observability.audit import AuditResult, audit_operation, resource_digest, configure_audit
from utils.observability.queries import generation_trace, audit_page, prune_observability
from utils.indexing.recovery import PublicationUncertain
from utils.indexing.health_report_cache import HealthReportCache, HealthReportInvalidated, ProjectHealthReports
from utils.indexing.health_scan import HealthScanRunner, ProjectGoneError
from utils.runtime.readiness import check_readiness
from utils.runtime.request_wait import wait_for_client
from utils.runtime.shutdown import ServiceStopping, check_shutdown, request_shutdown, reset_shutdown
from utils.core.logging_config import configure_logging, get_logger
from utils.indexing.upload_tasks import UploadTaskStore
from utils.indexing.deletions import begin_file_deletion, file_deletions, finish_file_deletion, pending_deletions
from utils.indexing.upload_quota import UploadLimits, UploadQuotaExceeded, daily_embedding_limit, moving_upload_files, staged_upload_prefix
from utils.indexing.progress import IndexProgress
from auth import (
    PASSWORD_MAX_LENGTH,
    PG_CONN,
    PG_POOL_MAX,
    init_db,
    close_db_pool,
    register,
    login,
    consume_account_change_limit,
    verify_token,
    get_session_user,
    revoke_session,
    auth_ingress,
    audit_login_result,
    maintain_access_limits,
    InvalidConversationCursor,
    list_conversations,
    list_messages,
    create_conversation,
    update_conversation_title,
    begin_generation,
    get_generation,
    acquire_subscription,
    release_subscription,
    cancel_generation,
    save_generation_progress,
    finish_generation,
    reap_stale_generations,
    delete_conversation_guarded,
    ChatLimits,
    release_chat_slot,
    list_projects,
    get_project,
    create_project,
    update_project,
    begin_project_deletion,
    finish_project_deletion,
    project_deletion_state,
    set_deletion_disk_stage,
    project_access,
    get_upload_usage,
    reserve_upload_request,
    release_upload_request,
    upload_admission,
    list_project_scopes,
)
from accounts import AccountError, update_account, prepare_deletion, begin_account_deletion, finish_account_deletion

logger = get_logger(__name__)
configure_logging()

# AnyIO 默认线程池并发额度：用于同步路由、依赖及 anyio.to_thread.run_sync。
# 上游流式生成使用独立线程，asyncio.to_thread 使用 asyncio 默认执行器，均不受此值限制。
THREADPOOL_SIZE = int(os.getenv("THREADPOOL_SIZE", "200"))
UVICORN_WORKERS = int(os.getenv("UVICORN_WORKERS", "1"))
ENABLE_API_DOCS = os.getenv("ENABLE_API_DOCS", "false").lower() in ("1", "true", "yes")

CHAT_MESSAGE_MAX_CHARS = int(os.getenv("CHAT_MESSAGE_MAX_CHARS", "8000"))
CHAT_THREAD_ID_MAX_CHARS = 128
CONV_TITLE_MAX_CHARS = 255   # 与 conversations.title VARCHAR(255) 对齐

CHAT_RATE_LIMIT = int(os.getenv("CHAT_RATE_LIMIT", "30"))
CHAT_RATE_WINDOW_SECONDS = int(os.getenv("CHAT_RATE_WINDOW_SECONDS", "60"))
# 0 表示不限制同一用户同时生成的会话数；正数可恢复并发上限。
MAX_CONCURRENT_CHAT_PER_USER = int(os.getenv("MAX_CONCURRENT_CHAT_PER_USER", "0"))
MAX_CHAT_SUBSCRIPTIONS_PER_USER = int(os.getenv("MAX_CHAT_SUBSCRIPTIONS_PER_USER", "4"))
if MAX_CHAT_SUBSCRIPTIONS_PER_USER < 1:
    raise ValueError("每账号聊天订阅数上限必须为正整数")
CHAT_SLOT_MAX_AGE_SECONDS = int(os.getenv("CHAT_SLOT_MAX_AGE_SECONDS", "1800"))

# 端到端生成截止时间：超过后服务端将该任务置为 failed，不再无限等待模型。
CHAT_STREAM_DEADLINE_SECONDS = int(os.getenv("CHAT_STREAM_DEADLINE_SECONDS", "300"))
# 生成任务存活检测：保存进度时同时更新 generations.heartbeat_at；超过
# CHAT_GENERATION_STALE_SECONDS 未更新的运行中任务，视为所属进程已异常退出，
# 由下一次请求在同一事务内置为 failed 并补写中断提示。
CHAT_GENERATION_SAVE_SECONDS = float(os.getenv("CHAT_GENERATION_SAVE_SECONDS", "0.5"))
CHAT_GENERATION_STALE_SECONDS = int(os.getenv("CHAT_GENERATION_STALE_SECONDS", "120"))
if not math.isfinite(CHAT_GENERATION_SAVE_SECONDS) or CHAT_GENERATION_SAVE_SECONDS < 0.1:
    raise ValueError("CHAT_GENERATION_SAVE_SECONDS 必须为不小于 0.1 的有限数值")
if CHAT_GENERATION_STALE_SECONDS < CHAT_GENERATION_SAVE_SECONDS * 3:
    raise ValueError("CHAT_GENERATION_STALE_SECONDS 必须不小于保存间隔的 3 倍")

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
UPLOAD_ACTIVE_DETAIL = "资料正在上传、入库、删除或等待恢复，请稍后重试"

# 就绪检查的总时限（秒），覆盖进程启动、建连、查询及 RPC。
READINESS_TIMEOUT_SECONDS = float(os.getenv("READINESS_TIMEOUT_SECONDS", "3"))
if not math.isfinite(READINESS_TIMEOUT_SECONDS) or READINESS_TIMEOUT_SECONDS < 0.5:
    raise ValueError("READINESS_TIMEOUT_SECONDS 必须为不小于 0.5 的有限数值")



def _warn_connection_budget() -> None:
    """启动时核算 PG 连接需求，超出上限时明确告警（多 worker 时最容易踩）。"""
    per_worker = PG_CHECKPOINT_POOL_MAX + PG_POOL_MAX + UPLOAD_WORKERS + 1
    need = UVICORN_WORKERS * per_worker
    try:
        with psycopg.connect(PG_CONN) as conn, conn.cursor() as cur:
            cur.execute("SHOW max_connections")
            max_conn = int(cur.fetchone()[0])
    except psycopg.Error as e:
        logger.warning(f"[启动] 无法读取 PostgreSQL 的 max_connections，跳过连接预算检查：{e}")
        return
    detail = (f"workers={UVICORN_WORKERS} × (检查点池 {PG_CHECKPOINT_POOL_MAX} + 业务池 {PG_POOL_MAX} "
              f"+ 上传队列池 {UPLOAD_WORKERS} + Trace 写入 1) = {need}")
    if need > max_conn - 5:
        state = "超出" if need > max_conn else "接近"
        logger.warning(f"[启动] PostgreSQL 预计连接占用约 {need}，已{state}连接上限（max_connections={max_conn}；{detail}）。"
              "建议提高 max_connections，或降低连接池上限。")
    else:
        logger.info(f"[启动] PostgreSQL 池与 Trace 预算 {need}，连接上限 {max_conn}（{detail}）；"
                    "还需为索引读写、审计与扫描连接预留容量")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global agent
    reset_shutdown()
    get_knowledge_storage_root()
    anyio.to_thread.current_default_thread_limiter().total_tokens = THREADPOOL_SIZE
    logger.info(f"[启动] AnyIO 默认线程池并发额度为 {THREADPOOL_SIZE}，工作进程数为 {UVICORN_WORKERS}")
    with ExitStack() as resources:
        # 导入模块时不建池；正常退出和启动中途失败都由生命周期释放资源。
        resources.callback(close_db_pool)
        init_db()
        configure_audit(PG_CONN)
        start_tracing(PG_CONN)
        resources.callback(stop_tracing)
        agent = ChatAgent()
        resources.callback(agent.close)
        _warn_connection_budget()
        background_stop = asyncio.Event()
        upload_pool = resources.enter_context(_upload_task_store.worker_pool(UPLOAD_WORKERS))
        upload_executor = resources.enter_context(
            ThreadPoolExecutor(max_workers=UPLOAD_WORKERS, thread_name_prefix="upload-index")
        )
        task = asyncio.create_task(_scheduled_health_loop(background_stop), name="scheduled-health")
        task.add_done_callback(_background_task_done)
        recovery_task = asyncio.create_task(_recovery_loop(background_stop), name="generation-recovery")
        recovery_task.add_done_callback(_background_task_done)
        deletion_task = asyncio.create_task(_deletion_recovery_loop(background_stop), name="deletion-recovery")
        upload_workers = [asyncio.create_task(_upload_worker_loop(i, background_stop, upload_executor, upload_pool),
                                             name=f"upload-index-{i}") for i in range(UPLOAD_WORKERS)]
        try:
            yield
        finally:
            request_shutdown()
            background_stop.set()
            logger.info("[关闭] 已通知后台入库和健康检查停止，等待当前步骤收尾")
            generation_tasks = tuple(_generation_tasks)
            for generation_task in generation_tasks:
                generation_task.cancel()
            # 不取消健康检查的 asyncio 等待，确保底层线程退出后再关闭连接池。
            await asyncio.gather(task, recovery_task, deletion_task, *generation_tasks, return_exceptions=True)
            # 线程在解析/嵌入边界响应停止；等待回滚、状态写回和已提交写入的收尾。
            await asyncio.gather(*upload_workers, return_exceptions=True)
    logger.info("[关闭] PostgreSQL 连接池已释放")


app = FastAPI(
    title="浅问深答",
    lifespan=lifespan,
    docs_url="/docs" if ENABLE_API_DOCS else None,
    redoc_url="/redoc" if ENABLE_API_DOCS else None,
    openapi_url="/openapi.json" if ENABLE_API_DOCS else None,
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = uuid.uuid4().hex
    with bind_context(ExecutionContext(http_request_id=request_id)):
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


async def _recovery_loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            _health_reports.prune()
            await asyncio.to_thread(maintain_access_limits)
            await asyncio.to_thread(reap_stale_generations, CHAT_GENERATION_STALE_SECONDS)
            await asyncio.to_thread(prune_observability, PG_CONN)
        except Exception:
            logger.exception("[恢复] 扫描失联生成任务失败")
        try:
            await asyncio.wait_for(stop.wait(), timeout=15)
        except TimeoutError:
            pass

def _recover_project_files(user_id: int, project_id: str) -> None:
    with _index_write_section(user_id, project_id), project_access(user_id, project_id) as current:
        if current is not None:
            _finish_file_deletions(user_id, project_id)


def _recover_deletions() -> None:
    users, projects, files = pending_deletions(PG_CONN)
    for callback, scopes in ((_complete_account_deletion, [(user,) for user in users]),
                             (_complete_project_deletion, projects), (_recover_project_files, files)):
        for scope in scopes:
            check_shutdown()
            try:
                callback(*scope)
            except ServiceStopping:
                raise
            except Exception:
                logger.exception("[恢复] 删除清理仍待重试 scope=%s", scope)


async def _deletion_recovery_loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.to_thread(_recover_deletions)
        except ServiceStopping:
            return
        except Exception:
            logger.exception("[恢复] 读取待删除资源失败")
        try:
            await asyncio.wait_for(stop.wait(), timeout=15)
        except TimeoutError:
            pass


# 仅在 lifespan 启动时创建，避免脚本入口和 Uvicorn 导入分别创建连接池。
agent: ChatAgent
allowed_types = vector_db_config["allowed_types"]

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "50"))
MAX_UPLOAD_REQUEST_MB = int(os.getenv("MAX_UPLOAD_REQUEST_MB", str(2 * MAX_UPLOAD_MB + 1)))
UPLOAD_RECEIVE_TIMEOUT_SECONDS = int(os.getenv("UPLOAD_RECEIVE_TIMEOUT_SECONDS", "1800"))
UPLOAD_LIMITS = UploadLimits(
    storage_bytes=int(os.getenv("ACCOUNT_STORAGE_MB", "1024")) * 1024**2,
    daily_bytes=int(os.getenv("ACCOUNT_DAILY_UPLOAD_MB", "250")) * 1024**2,
    pending_tasks=int(os.getenv("ACCOUNT_PENDING_UPLOAD_TASKS", "8")),
)
if min(MAX_UPLOAD_MB, MAX_UPLOAD_REQUEST_MB, UPLOAD_RECEIVE_TIMEOUT_SECONDS) < 1:
    raise ValueError("上传文件、请求大小和接收时限必须为正数")
UPLOAD_WORKERS = int(os.getenv("UPLOAD_WORKERS", "4"))
UPLOAD_TASK_POLL_SECONDS = float(os.getenv("UPLOAD_TASK_POLL_SECONDS", "0.5"))
if UPLOAD_WORKERS < 1:
    raise ValueError("UPLOAD_WORKERS 必须为正整数")
if not math.isfinite(UPLOAD_TASK_POLL_SECONDS) or UPLOAD_TASK_POLL_SECONDS <= 0:
    raise ValueError("UPLOAD_TASK_POLL_SECONDS 必须为正数")
_upload_task_store = UploadTaskStore(PG_CONN)

HEALTH_REPORT_TTL_SECONDS = int(os.getenv("HEALTH_REPORT_TTL_SECONDS", "120"))
HEALTH_CHECK_TIMEOUT_SECONDS = float(os.getenv("HEALTH_CHECK_TIMEOUT_SECONDS", "60"))
HEALTH_CHECK_MAX_CONCURRENT = int(os.getenv("HEALTH_CHECK_MAX_CONCURRENT", "2"))
if not math.isfinite(HEALTH_CHECK_TIMEOUT_SECONDS) or HEALTH_CHECK_TIMEOUT_SECONDS < 0.5:
    raise ValueError("HEALTH_CHECK_TIMEOUT_SECONDS 必须为不小于 0.5 的有限数值")
if HEALTH_CHECK_MAX_CONCURRENT < 1:
    raise ValueError("HEALTH_CHECK_MAX_CONCURRENT 必须为正整数")
_health_reports = ProjectHealthReports(HEALTH_REPORT_TTL_SECONDS)
_health_scanner = HealthScanRunner(HEALTH_CHECK_TIMEOUT_SECONDS, HEALTH_CHECK_MAX_CONCURRENT)
_readiness_report = HealthReportCache(1)


async def _refresh_health_report(request: Request, user_id: int, project_id: str) -> dict:
    async def check():
        return await _health_scanner.scan(user_id, project_id, PG_CONN, INDEX_READ_LOCK_TIMEOUT_SECONDS)

    try:
        # require_project 已完成请求鉴权；仅由共享扫描任务
        # 获取索引锁、项目锁及存储连接。
        return await wait_for_client(request, _health_reports.refresh((user_id, project_id), check))
    except ProjectGoneError:
        raise HTTPException(status_code=404, detail="项目不存在或已被删除")
    except HealthReportInvalidated:
        raise HTTPException(status_code=503, detail="项目状态已变更，请重新检查")
    except TimeoutError:
        raise HTTPException(status_code=503, detail="索引检查超时，请稍后重试")
    except HTTPException:
        raise
    except Exception:
        logger.exception("[索引健康检查] 检查失败 project_id=%s", project_id)
        raise HTTPException(status_code=503, detail="无法确认索引状态，请稍后重试")


_PG_LOCK_KEY_HEALTH = 0x64656570
_HEALTH_LEADER_POLL_SECONDS = 60

INDEX_WRITE_LOCK_TIMEOUT_SECONDS = int(os.getenv("INDEX_WRITE_LOCK_TIMEOUT_SECONDS", "300"))

INDEX_READ_LOCK_TIMEOUT_SECONDS = int(os.getenv("INDEX_READ_LOCK_TIMEOUT_SECONDS", "30"))


def _index_write_section(user_id: int, project_id: str):
    """索引写操作的跨进程互斥段（写入方使用，带等待上限）。"""
    return exclusive_section(PG_CONN, project_index_lock_key(user_id, project_id),
                             INDEX_WRITE_LOCK_TIMEOUT_SECONDS, cancel_check=check_shutdown)


def _format_repair_summary(repair: dict) -> str:
    """格式化修复摘要，供定时自动修复和手动修复共用同一套表述。"""
    record_state = "已更新" if repair["manifest_rebuilt"] else "未变更"
    return (
        f"清理残留向量 {len(repair['ghost_deleted'])} 个"
        f"（失败 {len(repair['ghost_errors'])} 个），"
        f"索引记录{record_state}，重索引 {len(repair['re_indexed'])} 个"
        f"（失败 {len(repair['re_index_failures'])} 个）"
    )


def _repair_audit_result(report: dict) -> AuditResult:
    repair = report["repair"]
    if repair["re_index_failures"] or repair["ghost_errors"] or not report["healthy"]:
        return "partial"
    return "succeeded"


def _auto_heal_report(user_id: int, project_id: str) -> dict | None:
    """索引自动修复闭环：健康检查发现问题 → 自动修复 → 复查，直至检查通过或达到轮次上限。

    停止条件：report['healthy'] 为 True。Milvus 不可达或修复异常时立即终止，
    等待下一轮健康检查再试。
    """
    with _index_write_section(user_id, project_id), project_access(user_id, project_id) as project:
        check_shutdown()
        if project is None:
            return None
        _resolve_pending_uploads(user_id, project_id)
        if _upload_task_store.has_active(user_id, project_id):
            return None
        index_health = IndexHealth(user_id, project_id)
        max_rounds = vector_db_config["health_auto_repair_max_rounds"]
        report = index_health.check(record_files=True)
        for _ in range(max_rounds):
            check_shutdown()
            if report["healthy"]:
                break
            if report["disk_error"] or report["milvus_error"] or report["manifest_error"]:
                logger.warning("[索引自动修复] 存储不可用，本轮暂不执行修复")
                break
            try:
                with audit_operation(user_id, "index.repair", "project", project_id,
                                     project_id=project_id, system=True) as operation:
                    report = index_health.repair()
                    operation.result = _repair_audit_result(report)
                repair = report["repair"]
                logger.info(f"[索引自动修复] 本轮修复完成：{_format_repair_summary(repair)}")
            except ServiceStopping:
                raise
            except Exception as e:
                logger.error(f"[索引自动修复] 执行失败：{e}")
                break
        _health_reports.set((user_id, project_id), report)
        return report


def _upload_progress_payload(value: object, status: str) -> dict | None:
    if value is None:
        return None
    progress = IndexProgress.model_validate(value)
    return {**progress.model_dump(), "percent": 100 if status == "success" else progress.percent}


def _uploaded_file_exists(directory: str, filename: str) -> bool:
    file_path = scoped_knowledge_file(directory, _safe_filename(filename))
    try:
        return stat.S_ISREG(os.stat(file_path).st_mode)
    except FileNotFoundError:
        return False


def _upload_task_payload(task: dict) -> dict:
    """保留历史任务结果，同时确认源文件当前是否仍在，不暴露磁盘路径。"""
    directory = get_knowledge_dir(task["user_id"], task["project_id"])
    return {
        "task_id": task["id"],
        "status": task["status"],
        "total": task["total"],
        "completed": task["completed"],
        "succeeded": task["succeeded"],
        "failed": task["failed"],
        "error": task["error"],
        "recovery_required": task["recovery_required"],
        "files": [
            {
                "name": item["name"],
                "status": item["status"],
                "overwritten": item["overwritten"],
                "indexed": item["indexed"],
                "error": item["error"],
                "progress": _upload_progress_payload(item["progress"], item["status"]),
                "file_exists": _uploaded_file_exists(directory, item["name"]) if item["status"] == "success" else None,
            }
            for item in task["files"]
        ],
    }


def _process_upload_task(task: dict, lease: psycopg.Connection, queued_items: list[dict]) -> bool:
    """执行一个上传任务的索引操作；数据库队列保证同一项目内按顺序执行。"""
    task_id = task["id"]
    user_id, project_id = task["user_id"], task["project_id"]
    succeeded = True
    try:
        for item in queued_items:
            check_shutdown()
            journal = _upload_task_store.journal(task, item)
            status = "error"
            overwritten = None
            indexed = False
            error = None
            last_progress: IndexProgress | None = None

            def report_progress(progress: IndexProgress) -> None:
                nonlocal last_progress
                if last_progress is not None:
                    if progress.percent < last_progress.percent:
                        raise ValueError("入库进度不能回退")
                    if (progress.stage == last_progress.stage and progress.percent == last_progress.percent
                            and progress.completed != progress.total):
                        return
                _upload_task_store.update_item_progress(task_id, item["id"], progress)
                last_progress = progress

            try:
                with _index_write_section(user_id, project_id), project_access(user_id, project_id) as current:
                    check_shutdown()
                    # 检查队列租约连接，数据库断连导致租约丢失时停止处理。
                    lease.execute("SELECT 1")
                    _ensure_project(current)
                    _resolve_pending_uploads(user_id, project_id)
                    knowledge_dir = get_knowledge_dir(user_id, project_id)
                    staged_path = _staged_upload_path(knowledge_dir, item["staged_path"])
                    destination = scoped_knowledge_file(
                        knowledge_dir, os.path.join(knowledge_dir, item["name"])
                    )
                    result = get_vector_store(user_id, project_id).replace_and_index_file(
                        destination, staged_path, on_progress=report_progress, journal=journal,
                    )
                    overwritten = result["overwritten"]
                    indexed = result["committed"]
                    error = result["error"]
                    status = "success" if indexed and not error else "error"
                _health_reports.invalidate((user_id, project_id))
            except (ServiceStopping, psycopg.OperationalError):
                raise
            except PublicationUncertain as exc:
                error = str(exc)
            except TimeoutError:
                error = "索引正被其他操作占用，请稍后重试"
            except HTTPException as exc:
                error = str(exc.detail)
            except Exception:
                logger.exception("[知识库] 后台上传任务失败 file=%s project_id=%s", item["name"], project_id)
                error = "后台入库失败，请稍后重试"
            _upload_task_store.finish_item(
                task_id, item["id"], status=status, overwritten=overwritten,
                indexed=indexed, error=error, lease=lease,
            )
            if error:
                succeeded = False
                check_shutdown()
                if journal.pending:
                    raise PublicationUncertain(error)
        _upload_task_store.finish(task_id, lease=lease)
        return succeeded
    except (ServiceStopping, psycopg.OperationalError):
        raise
    except Exception:
        logger.exception("[知识库] 后台上传任务异常 task_id=%s", task_id)
        try:
            _upload_task_store.fail(task_id, "后台任务异常中断，请重新上传", lease=lease)
        except Exception:
            logger.exception("[知识库] 后台上传任务状态写回失败 task_id=%s", task_id)
        return False


def _run_next_upload(pool: ConnectionPool) -> bool:
    check_shutdown()
    with pool.connection() as lease, _upload_task_store.claim_next(lease) as task:
        if task is None:
            return False
        return _run_claimed_upload(task, lease)


def _run_claimed_upload(task: dict, lease: psycopg.Connection) -> bool:
    try:
        with bind_context(ExecutionContext(trace_id=task["trace_id"],
                          user_id=task["user_id"], project_id=task["project_id"])), span("upload.execute") as execution:
            if task["status"] == "running":
                # 领取成功证明原队列会话锁已经释放；项目写锁防止仍在收尾的索引操作并发。
                with _index_write_section(task["user_id"], task["project_id"]), \
                        project_access(task["user_id"], task["project_id"]) as project:
                    _ensure_project(project)
                    _upload_task_store.requeue_unprepared_items(task["id"], lease)
                    for item in _upload_task_store.items(task["id"]):
                        if item["status"] == "running":
                            journal = _upload_task_store.journal(task, item)
                            result = get_vector_store(task["user_id"], task["project_id"]).recover_upload(journal) if journal.record is not None else {
                                "ok": False, "committed": False, "overwritten": None,
                                "error": "更新前任务缺少恢复记录，请检查文档状态或重新上传",
                            }
                            _upload_task_store.finish_item(task["id"], item["id"], status="success" if result["ok"] else "error",
                                indexed=result["committed"], overwritten=result["overwritten"], error=result["error"], lease=lease)
                            if not result["ok"]:
                                execution.fail("upload_interrupted")
            queued_items = [item for item in _upload_task_store.items(task["id"]) if item["status"] == "queued"]
            if not _process_upload_task(task, lease, queued_items):
                execution.fail("upload_failed")
            queued_ids = {item["id"] for item in queued_items}
            for item in _upload_task_store.items(task["id"]):
                if item["status"] not in ("success", "error"):
                    continue
                if item["recovery"] is None and item["id"] not in queued_ids:
                    continue  # 无法核实的旧任务仍需保留暂存文件。
                if _upload_task_store.journal(task, item).pending:
                    continue
                try:
                    _remove_staged_upload(task["user_id"], task["project_id"], item["staged_path"])
                except (OSError, ValueError):
                    logger.exception("[知识库] 临时上传文件清理失败 file=%s", item["staged_path"])
        return True
    except (ServiceStopping, psycopg.OperationalError, TimeoutError):
        # 关停、断连或接管时等锁超时，都留给下一次领取继续。
        raise
    except Exception:
        _upload_task_store.fail(task["id"], "上传恢复未完成，请执行索引修复", lease=lease)
        raise


def _resolve_pending_uploads(user_id: int, project_id: str) -> None:
    """调用方持项目写锁；仅核对已结束任务，运行中的队列由原 worker 处理。"""
    _finish_file_deletions(user_id, project_id)
    for item in _upload_task_store.pending_recoveries(user_id, project_id):
        task = {"id": item["task_id"], "user_id": user_id, "project_id": project_id}
        journal = _upload_task_store.journal(task, item)
        with audit_operation(user_id, "upload.recover", "upload_item", str(item["id"]),
                             project_id=project_id, system=True) as operation:
            result = get_vector_store(user_id, project_id).recover_upload(journal)
            _upload_task_store.finish_item(task["id"], item["id"], status="success" if result["ok"] else "error",
                indexed=result["committed"], overwritten=result["overwritten"], error=result["error"])
            operation.result = "succeeded" if result["ok"] else "partial"
        _upload_task_store.finish(task["id"])
        _remove_staged_upload(user_id, project_id, item["staged_path"])


def _staged_upload_path(directory: str, path: str) -> str:
    path = scoped_knowledge_file(directory, path)
    if not os.path.basename(path).startswith(".upload_"):
        raise ValueError("无效的上传暂存文件")
    return path


def _remove_staged_upload(user_id: int, project_id: str, path: str) -> None:
    path = _staged_upload_path(get_knowledge_dir(user_id, project_id), path)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


async def _upload_worker_loop(worker_id: int, stop: asyncio.Event, executor: ThreadPoolExecutor,
                              pool: ConnectionPool) -> None:
    while not stop.is_set():
        try:
            worked = await asyncio.get_running_loop().run_in_executor(executor, _run_next_upload, pool)
            if worked:
                continue
        except ServiceStopping:
            return
        except Exception:
            logger.exception("[知识库] 上传 worker=%s 轮询失败", worker_id)
        try:
            await asyncio.wait_for(stop.wait(), timeout=UPLOAD_TASK_POLL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def _scheduled_health_loop(stop: asyncio.Event):
    """定时健康检查和自动修复。

    多 worker/多实例下必须只由一个进程执行，否则会重复修复、重复触发嵌入调用；
    使用 PostgreSQL 会话级咨询锁（advisory lock）选出唯一执行进程；持有者退出后，其他进程自动接管。
    """
    interval = vector_db_config["health_check_interval_hours"] * 3600
    guard = SingleInstanceGuard(PG_CONN, _PG_LOCK_KEY_HEALTH)
    next_run = 0.0
    was_leader = False
    while not stop.is_set():
        is_leader = await asyncio.to_thread(guard.try_acquire)
        if stop.is_set():
            return
        if is_leader != was_leader:
            logger.info(
                "[索引健康检查] 当前进程已获得定时检查和修复的执行权" if is_leader
                else "[索引健康检查] 当前进程已失去执行权，暂停定时检查与修复"
            )
            was_leader = is_leader
        if not is_leader or time.monotonic() < next_run:
            try:
                await asyncio.wait_for(stop.wait(), timeout=_HEALTH_LEADER_POLL_SECONDS)
            except asyncio.TimeoutError:
                pass
            continue
        try:
            for scope in await asyncio.to_thread(list_project_scopes):
                check_shutdown()
                try:
                    report = await asyncio.to_thread(_auto_heal_report, scope["user_id"], scope["id"])
                    if report is not None:
                        logger.info("[索引健康检查] 项目 %s 检查完成 healthy=%s", scope["id"], report["healthy"])
                except ServiceStopping:
                    return
                except Exception:
                    logger.exception("[索引健康检查] 项目 %s 检查失败", scope["id"])
        except ServiceStopping:
            return
        except Exception as e:
            logger.error(f"[索引健康检查] 执行失败：{e}")
        next_run = time.monotonic() + interval



class ChatRequest(BaseModel):
    message: str = Field(max_length=CHAT_MESSAGE_MAX_CHARS)
    thread_id: str = Field(max_length=CHAT_THREAD_ID_MAX_CHARS)
    # 客户端生成的幂等键：同一会话内重复提交不会重复写消息或重复生成
    request_id: str = Field(pattern=r"^[0-9a-zA-Z_-]{8,64}$")
    kb_only: bool = True
    project_id: str | None = None
    retry_of: int | None = Field(default=None, gt=0)

    @field_validator("project_id")
    @classmethod
    def normalize_project_id(cls, value: str | None) -> str | None:
        return str(UUID(value)) if value is not None else None


class RegisterRequest(BaseModel):
    username: str
    password: str = Field(max_length=PASSWORD_MAX_LENGTH)


class LoginRequest(BaseModel):
    username: str
    password: str = Field(max_length=PASSWORD_MAX_LENGTH)


class UpdateAccountRequest(BaseModel):
    username: str | None = Field(default=None, min_length=2, max_length=50)
    current_password: str | None = Field(default=None, max_length=PASSWORD_MAX_LENGTH)
    new_password: str | None = Field(default=None, max_length=PASSWORD_MAX_LENGTH)


class DeletionVerificationRequest(BaseModel):
    password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)


class DeleteAccountRequest(BaseModel):
    confirmation_token: str = Field(min_length=1, max_length=2048)
    confirmation_text: str = Field(min_length=1, max_length=60)


class CreateConversationRequest(BaseModel):
    title: str = Field(default="新对话", max_length=CONV_TITLE_MAX_CHARS)


class UpdateTitleRequest(BaseModel):
    title: str = Field(max_length=CONV_TITLE_MAX_CHARS)


class ProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("项目名称不能为空")
        return value



def get_access_claims(authorization: str = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未登录")
    token = authorization[7:]
    payload = verify_token(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    return payload


def get_current_user(payload: dict = Depends(get_access_claims)) -> dict:
    """复核账号与凭证版本，让改密和注销在所有 worker 上立即生效。"""
    user = get_session_user(payload["user_id"], payload["jti"])
    if user is None:
        raise HTTPException(status_code=401, detail="登录已失效，请重新登录")
    if payload["auth_version"] != user["auth_version"]:
        raise HTTPException(status_code=401, detail="密码已更新，请重新登录")
    return {"user_id": user["id"], "username": user["username"], "auth_version": user["auth_version"],
            "session_id": payload["jti"], "expires_at": payload["exp"]}


def account_change_user(action: str):
    def admit(payload: dict = Depends(get_access_claims)) -> dict:
        limiter = auth_ingress[action]
        if not limiter.allow(f"{payload['user_id']}:{payload['jti']}"):
            raise HTTPException(429, "操作过于频繁，请稍后再试")
        user = get_current_user(payload)
        if not consume_account_change_limit(user["user_id"], user["session_id"]):
            limiter.record_denied()
            raise HTTPException(429, "操作过于频繁，请稍后再试")
        return user

    return admit


def require_project(project_id: UUID, user: dict = Depends(get_current_user)) -> dict:
    project = get_project(user["user_id"], str(project_id))
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在或已被删除")
    if project["deleted_at"] is not None:
        raise HTTPException(status_code=410, detail="项目已删除，历史对话仅供查看，原文已不可用")
    return {**project, "user_id": user["user_id"]}



@app.get("/api/health")
def health():
    """存活检查：只证明进程能响应请求，不探测任何外部依赖。"""
    return {"status": "ok"}


@app.get("/api/health/ready")
async def readiness(request: Request):
    """PG 与 Milvus 并行探测；超时结束检查并回收连接，返回暂未就绪。"""
    checks = await wait_for_client(request, _readiness_report.get(
        lambda: check_readiness(PG_CONN, vector_db_config["milvus_uri"], READINESS_TIMEOUT_SECONDS)))
    ready = all(item["ok"] for item in checks.values())
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ready" if ready else "not_ready", "checks": checks},
    )



def _auth_source(request: Request, action: str) -> str:
    ip = request.client.host if request.client else "unknown"
    if not auth_ingress[action].allow(ip):
        raise HTTPException(429, "尝试过于频繁，请稍后再试")
    return ip


def _auth_denied(action: str, username: str, result: dict, status: int) -> None:
    if result.get("rate_limited"):
        auth_ingress[action].record_denied()
        raise HTTPException(429, result["error"])
    audit_login_result(action, username, None, f"http_{status}")
    raise HTTPException(status, result["error"])


@app.post("/api/auth/register")
def api_register(req: RegisterRequest, request: Request):
    result = register(req.username, req.password, _auth_source(request, "account.register"))
    if not result["ok"]:
        _auth_denied("account.register", req.username, result, 400)
    return {"message": "注册成功"}


@app.post("/api/auth/login")
def api_login(req: LoginRequest, request: Request):
    result = login(req.username, req.password, _auth_source(request, "auth.login"))
    if not result["ok"]:
        _auth_denied("auth.login", req.username, result, 401)
    audit_login_result("auth.login", req.username, result["user"]["id"])
    return {"token": result["token"], "user": result["user"]}


@app.post("/api/auth/logout")
def api_logout(user: dict = Depends(get_current_user)):
    revoke_session(user)
    return {"message": "已退出登录"}


@app.get("/api/auth/me")
def api_me(user: dict = Depends(get_current_user)):
    return {"user": {"id": user["user_id"], "username": user["username"]}}


@app.patch("/api/auth/me")
def api_update_account(req: UpdateAccountRequest, user: dict = Depends(account_change_user("account.update"))):
    with audit_operation(user["user_id"], "account.update", "account", str(user["user_id"]), transactional=True):
        try:
            return update_account(user["user_id"], user["auth_version"], session_id=user["session_id"],
                                  expires_at=user["expires_at"], **req.model_dump())
        except AccountError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@app.post("/api/auth/deletion-verification")
def api_prepare_deletion(req: DeletionVerificationRequest, user: dict = Depends(account_change_user("account.verify_deletion"))):
    with audit_operation(user["user_id"], "account.verify_deletion", "account", str(user["user_id"])):
        try:
            return prepare_deletion(user["user_id"], user["auth_version"], req.password)
        except AccountError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


def _remove_account_directory(user_id: int) -> None:
    try:
        get_account_knowledge_dir(user_id).rmdir()
    except FileNotFoundError:
        pass


def _complete_account_deletion(user_id: int) -> None:
    finish_account_deletion(user_id, _complete_project_deletion, _remove_account_directory)


@app.delete("/api/auth/me")
def api_delete_account(req: DeleteAccountRequest, user: dict = Depends(account_change_user("account.delete"))):
    user_id = user["user_id"]
    with audit_operation(user_id, "account.delete", "account", str(user_id), transactional=True):
        try:
            begin_account_deletion(user_id, user["auth_version"], req.confirmation_token, req.confirmation_text,
                                   stale_seconds=CHAT_GENERATION_STALE_SECONDS, slot_max_age=CHAT_SLOT_MAX_AGE_SECONDS)
        except AccountError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        except (TimeoutError, psycopg.errors.LockNotAvailable) as exc:
            raise HTTPException(status_code=409, detail="账号资料正在处理其他操作，请稍后重试注销") from exc
        try:
            _complete_account_deletion(user_id)
        except Exception:
            logger.exception("[账号] 等待恢复注销清理 user_id=%s", user_id)
            return JSONResponse({"message": "注销已受理，账号已停用，后台将继续清理资料"}, status_code=202)
        return {"message": "账号及其项目、文档、索引和对话已注销删除"}


@app.get("/api/conversations")
def api_list_conversations(
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(None, max_length=512),
    user: dict = Depends(get_current_user),
):
    try:
        return list_conversations(user["user_id"], limit, cursor)
    except InvalidConversationCursor as exc:
        raise HTTPException(status_code=400, detail="无效的会话分页游标") from exc


@app.get("/api/conversations/{conv_id}/messages")
def api_list_messages(
    conv_id: str,
    limit: int = Query(50, ge=1, le=100),
    before: int | None = Query(None, ge=1, le=9223372036854775807),
    user: dict = Depends(get_current_user),
):
    result = list_messages(user["user_id"], conv_id, limit, before, CHAT_GENERATION_STALE_SECONDS)
    if result is None:
        raise HTTPException(status_code=404, detail="会话不存在或已被删除")
    return result


@app.post("/api/conversations")
def api_create_conversation(req: CreateConversationRequest, user: dict = Depends(get_current_user)):
    conv_id = str(uuid.uuid4())
    conversation = create_conversation(user["user_id"], conv_id, req.title)
    if conversation is None:
        raise HTTPException(status_code=500, detail="创建会话失败，请稍后重试")
    return {**conversation, "messages": []}


@app.patch("/api/conversations/{conv_id}")
def api_update_title(conv_id: str, req: UpdateTitleRequest, user: dict = Depends(get_current_user)):
    if not update_conversation_title(user["user_id"], conv_id, req.title):
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"message": "ok"}


@app.delete("/api/conversations/{conv_id}")
def api_delete_conversation(conv_id: str, user: dict = Depends(get_current_user)):
    """删除会话及检查点。运行中的回答会先收到取消信号，本次删除返回 409。"""
    try:
        result = delete_conversation_guarded(
            user["user_id"],
            conv_id,
            CHAT_GENERATION_STALE_SECONDS,
        )
    except Exception as e:
        logger.error(f"[会话] 删除失败 conversation_id={conv_id}：{e}")
        raise HTTPException(status_code=500, detail="清理会话上下文失败，请稍后重试")
    if result == "not_found":
        raise HTTPException(status_code=404, detail="会话不存在")
    if result == "busy":
        raise HTTPException(status_code=409, detail="回答正在生成，已请求取消，请稍后再试")
    return {"message": "ok"}



# 每个 worker 的事件循环登记和移除本地取消回调；跨进程计数由 chat_slots 表承担。
_chat_slot_cancels: dict[str, Callable[[], None]] = {}


def _bind_chat_slot_cancel(token: str, cancel) -> None:
    """把租约与生成任务的取消入口绑定，租约清理时可以停止本进程内的任务。"""
    _chat_slot_cancels[token] = cancel


async def _release_chat_slot(user_id: int, token: str) -> None:
    _chat_slot_cancels.pop(token, None)
    try:
        await asyncio.to_thread(release_chat_slot, user_id, token)
    except psycopg.Error:
        logger.exception("[对话流] 释放并发槽位失败，将由租约过期清理")


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# 强引用后台任务，生命周期只由生成结果、显式停止、超时或服务关闭决定。
_generation_tasks: set[asyncio.Task] = set()


def _background_task_done(task: asyncio.Task) -> None:
    _generation_tasks.discard(task)
    if not task.cancelled():
        error = task.exception()
        if error is not None:
            logger.error("[后台任务] %s 异常结束", task.get_name(), exc_info=error)


async def _run_generation(req: ChatRequest, user_id: int, generation_id: int, attempt_id: str, slot: str):
    try:
        with span("generation.execute", kb_only=req.kb_only) as execution:
            attempt = AttemptCheckpoints(f"{user_id}:{req.thread_id}", attempt_id)
            status = await run_generation(
                lambda should_stop: agent.stream_answer(
                    req.message, attempt, kb_only=req.kb_only, should_stop=should_stop,
                    user_id=user_id, project_id=req.project_id,
                ),
                partial(save_generation_progress, generation_id, attempt.attempt_id),
                partial(finish_generation, generation_id, attempt.attempt_id, publish_checkpoint=attempt.publish),
                deadline=CHAT_STREAM_DEADLINE_SECONDS,
                save_interval=CHAT_GENERATION_SAVE_SECONDS,
                on_bridge=lambda bridge: _bind_chat_slot_cancel(slot, bridge.stop),
            )
            if status == "failed":
                execution.fail("generation_failed")
            elif status == "cancelled":
                execution.status = "cancelled"
    finally:
        await _release_chat_slot(user_id, slot)


async def subscription_user(user: dict = Depends(get_current_user)):
    lifetime = max(60, CHAT_STREAM_DEADLINE_SECONDS + 30)
    deadline = time.monotonic() + min(lifetime, user["expires_at"] - time.time())
    token = await anyio.to_thread.run_sync(acquire_subscription, user, MAX_CHAT_SUBSCRIPTIONS_PER_USER, lifetime)
    if token is None:
        raise HTTPException(429, "同时查看的回答过多，请关闭其他订阅后重试")
    try:
        yield {**user, "subscription": token, "stream_deadline": deadline}
    finally:
        with anyio.CancelScope(shield=True):
            try:
                await anyio.to_thread.run_sync(release_subscription, token)
            except psycopg.Error:
                logger.exception("[聊天] 释放订阅额度失败，将在到期后回收")


def _generation_stream(user: dict, conv_id: str, request_id: str):
    key = (user["user_id"], conv_id, request_id)
    fetch = partial(get_generation, *key, CHAT_GENERATION_STALE_SECONDS, include_subscribers=True)

    async def events():
        previous = None
        first = True
        with watch_generation(key) as reader:
            while time.monotonic() < user["stream_deadline"]:
                generation = await reader.read(fetch)
                if generation is None:
                    yield _sse({"type": "error", "status": "error", "error": "未找到这次回答，请刷新页面后重试。"})
                    return
                if user["subscription"] in generation["subscriber_tokens"]:
                    payload = {k: v for k, v in generation.items() if k not in ("user_content", "subscriber_tokens")}
                    signature = {k: v for k, v in payload.items() if k != "elapsed_seconds"}
                    if signature != previous:
                        yield _sse({"type": "snapshot", **payload})
                        previous = signature
                    if payload["status"] != "running":
                        return
                    yield ": keep-alive\n\n"
                elif not first:
                    return
                # 刚加入时缓存可能早于本订阅；等第一次新快照验证后才发送正文。
                first = False
                await asyncio.sleep(POLL_SECONDS)

    return SubscriptionResponse(events(), deadline=user["stream_deadline"], headers=SSE_HEADERS)


@app.get("/api/conversations/{conv_id}/generations/{request_id}/stream")
async def resume_generation(conv_id: str, request_id: str, user: dict = Depends(subscription_user)):
    generation = await asyncio.to_thread(
        get_generation, user["user_id"], conv_id, request_id, CHAT_GENERATION_STALE_SECONDS,
    )
    if generation is None:
        raise HTTPException(status_code=404, detail="未找到这次回答，请刷新页面后重试。")
    return _generation_stream(user, conv_id, request_id)


@app.post("/api/conversations/{conv_id}/generations/{request_id}/cancel")
async def stop_generation(conv_id: str, request_id: str, user: dict = Depends(get_current_user), before_start: bool = False):
    if not re.fullmatch(r"[0-9a-zA-Z_-]{8,64}", request_id):
        raise HTTPException(status_code=404, detail="未找到这次回答")
    state = await asyncio.to_thread(cancel_generation, user["user_id"], conv_id, request_id,
                                    user["expires_at"] if before_start else None)
    if state is None:
        raise HTTPException(status_code=404, detail="未找到这次回答，请刷新页面后重试。")
    return {"message": "ok", "state": state}


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest, user: dict = Depends(subscription_user)):
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="消息不能为空")
    if req.kb_only and req.project_id is None:
        raise HTTPException(status_code=400, detail="请先选择知识库项目")
    if not req.kb_only and req.project_id is not None:
        raise HTTPException(status_code=400, detail="通用模式不使用项目")
    # 服务端统一登记生成任务并写入用户消息：会话必须存在且属于当前用户；
    # 同一会话只允许一个运行中任务，同一 request_id 重复提交按幂等重放处理。
    try:
        begun = await asyncio.to_thread(
            begin_generation,
            user["user_id"],
            req.thread_id,
            req.request_id,
            req.message,
            CHAT_GENERATION_STALE_SECONDS,
            req.kb_only,
            req.project_id,
            req.retry_of,
            limits=ChatLimits(send_count=CHAT_RATE_LIMIT, window_seconds=CHAT_RATE_WINDOW_SECONDS,
                              concurrent=MAX_CONCURRENT_CHAT_PER_USER, lease_seconds=CHAT_SLOT_MAX_AGE_SECONDS),
        )
    except Exception as e:
        logger.error(f"[对话流] 登记生成任务失败 thread_id={req.thread_id}：{e}")
        raise HTTPException(status_code=503, detail="服务暂时不可用，请稍后重试")

    for token in begun.get("reaped", ()):
        cancel = _chat_slot_cancels.pop(token, None)
        if cancel is not None:
            cancel()

    generation_errors = {
        "rate_limited": (429, "请求过于频繁，请稍后再试"),
        "concurrency_limited": (429, "并发回答数过多，请等待当前回答完成"),
        "not_found": (404, "会话不存在或已被删除"),
        "busy": (409, "当前会话已有回答正在生成，请稍后再试"),
        "mode_conflict": (409, "请求编号已用于其他问答模式，请重新发送"),
        "conversation_mode_conflict": (409, "此会话的问答模式已固定，请开启新对话后切换模式"),
        "content_conflict": (409, "请求编号已用于不同的问题，请使用新的请求编号"),
        "invalid_retry": (409, "只能重新生成当前会话中已失败或已取消的原问题"),
        "project_required": (400, "请先选择知识库项目"),
        "project_not_found": (404, "项目不存在或已被删除"),
        "project_deleted": (410, "项目已删除，历史对话仅供查看，无法继续提问"),
        "project_conflict": (409, "请求的项目与问答模式或原请求不一致"),
        "upload_active": (409, UPLOAD_ACTIVE_DETAIL),
        "conversation_project_conflict": (409, "此会话的项目已固定，请开启新对话后选择项目"),
    }
    if begun["status"] == "stopped_before_start":
        return Response(status_code=204)
    if begun["status"] in generation_errors:
        status, detail = generation_errors[begun["status"]]
        raise HTTPException(status_code=status, detail=detail)

    if begun["status"] == "started":
        # 在返回响应前启动独立任务；断开 SSE 只结束订阅。
        with bind_context(ExecutionContext(http_request_id=current_context.get().http_request_id,
                          trace_id=begun["trace_id"], user_id=user["user_id"], project_id=req.project_id,
                          generation_id=begun["generation_id"], attempt_id=begun["attempt_id"])):
            generation_task = asyncio.create_task(
                _run_generation(req, user["user_id"], begun["generation_id"], begun["attempt_id"], begun["slot"]),
                name=f"generation-{begun['generation_id']}",
            )
        _generation_tasks.add(generation_task)
        generation_task.add_done_callback(_background_task_done)

    return _generation_stream(user, req.thread_id, req.request_id)



@app.get("/api/projects")
def api_list_projects(user: dict = Depends(get_current_user)):
    return {"projects": list_projects(user["user_id"])}


@app.get("/api/account/upload-quota")
def api_upload_quota(user: dict = Depends(get_current_user)):
    return {
        "usage": get_upload_usage(user["user_id"]),
        "limits": {
            "storage_bytes": UPLOAD_LIMITS.storage_bytes,
            "daily_bytes": UPLOAD_LIMITS.daily_bytes,
            "pending_tasks": UPLOAD_LIMITS.pending_tasks,
            "embedding_chars": daily_embedding_limit(),
            "file_bytes": MAX_UPLOAD_MB * 1024**2,
            "request_bytes": MAX_UPLOAD_REQUEST_MB * 1024**2,
        },
    }


@app.get("/api/generations/{generation_id}/trace")
def get_generation_trace(generation_id: int, user: dict = Depends(get_current_user)):
    result = generation_trace(PG_CONN, user["user_id"], generation_id)
    if result is None:
        raise HTTPException(status_code=404, detail="生成任务不存在")
    return result


@app.get("/api/audit-events")
def get_audit_events(before: int | None = Query(None, gt=0), limit: int = Query(30, ge=1, le=100),
                     user: dict = Depends(get_current_user)):
    return audit_page(PG_CONN, user["user_id"], before, limit)


@app.post("/api/projects", status_code=201)
def api_create_project(req: ProjectRequest, user: dict = Depends(get_current_user)):
    with audit_operation(user["user_id"], "project.create", "project", "new", transactional=True):
        return {"project": create_project(user["user_id"], req.name)}


@app.patch("/api/projects/{project_id}")
def api_update_project(req: ProjectRequest, project: dict = Depends(require_project)):
    with audit_operation(project["user_id"], "project.rename", "project", project["id"], project_id=project["id"], transactional=True):
        updated = update_project(project["user_id"], project["id"], req.name)
        if updated is None:
            raise HTTPException(status_code=404, detail="项目不存在或已被删除")
        return {"project": updated}


def _remove_project_files(user_id: int, project_id: str, state: dict) -> None:
    target = Path(get_knowledge_dir(user_id, project_id))
    parent = get_account_knowledge_dir(user_id)
    staged = parent / f".deleting-{UUID(project_id)}"
    if target.parent != parent or target.name != str(UUID(project_id)) or staged.resolve() != staged:
        raise RuntimeError("无效的项目文件目录")
    stage = state["deletion_disk_stage"]
    try:
        target.stat()
    except FileNotFoundError:
        pass
    else:
        if staged.exists():
            raise RuntimeError("项目目录及其删除暂存目录同时存在，请核对存储")
        with moving_upload_files(PG_CONN, user_id):
            target.rename(staged)
    try:
        staged.stat()
    except FileNotFoundError:
        if stage == "pending" and state["expect_files"]:
            raise RuntimeError("无法确认项目源文件已删除，请恢复资料目录")
        if stage == "quarantined":
            parent.stat()  # 区分清理后崩溃和整个存储离线。
    else:
        if stage != "quarantined":
            set_deletion_disk_stage(user_id, project_id, "quarantined")
        stage = "quarantined"
        shutil.rmtree(staged)
    if stage != "removed":
        set_deletion_disk_stage(user_id, project_id, "removed")


def _complete_project_deletion(user_id: int, project_id: str) -> None:
    with _index_write_section(user_id, project_id):
        state = project_deletion_state(user_id, project_id)
        if state is None:
            return
        _remove_project_files(user_id, project_id, state)
        drop_project_vectors(user_id, project_id)
        agent.forget_project(user_id, project_id)
        finish_project_deletion(user_id, project_id)
        _health_reports.invalidate((user_id, project_id))


@app.delete("/api/projects/{project_id}")
def api_delete_project(project_id: UUID, user: dict = Depends(get_current_user)):
    user_id, pid = user["user_id"], str(project_id)
    with audit_operation(user_id, "project.delete", "project", pid, project_id=pid, transactional=True):
        try:
            with _index_write_section(user_id, pid):
                status = begin_project_deletion(user_id, pid, CHAT_GENERATION_STALE_SECONDS)
        except TimeoutError as exc:
            raise HTTPException(status_code=503, detail="项目正在处理其他操作，请稍后重试") from exc
        if status == "not_found":
            raise HTTPException(status_code=404, detail="项目不存在")
        if status == "busy":
            raise HTTPException(status_code=409, detail="项目中有回答正在生成，请停止或等待完成后再删除")
        if status == "pending":
            _health_reports.invalidate((user_id, pid))
            try:
                _complete_project_deletion(user_id, pid)
            except Exception:
                logger.exception("[项目] 等待恢复删除清理 project_id=%s", pid)
                return JSONResponse({"message": "删除已受理，历史对话仅供查看，后台将继续清理资料"}, status_code=202)
        return {"message": "项目文档和索引已删除，历史对话已保留，仅供查看"}


def _ensure_project(project):
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在或已被删除")


def _ensure_no_uploads(user_id: int, project_id: str) -> None:
    if _upload_task_store.has_active(user_id, project_id):
        raise HTTPException(status_code=409, detail=UPLOAD_ACTIVE_DETAIL)


def _safe_filename(filename: str) -> str:
    # 跨平台拒绝目录、Windows设备名、流名称及会发生归一化冲突的名字。
    if (not filename or len(filename) > 220 or filename.startswith(".")
            or filename.rstrip(" .") != filename
            or any(ord(c) < 32 or c in '<>:"/\\|?*' for c in filename)
            or re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", filename, re.I)):
        raise ValueError("无效的文件名")
    return filename


def _project_manifests(user_id: int, project_id: str) -> ManifestStore:
    return ManifestStore(vector_db_config, user_id, project_id)


def _project_file_statuses(user_id: int, project_id: str) -> list[KnowledgeFile]:
    directory = get_knowledge_dir(user_id, project_id)
    paths = list_supported_files(directory, allowed_types)
    return _project_manifests(user_id, project_id).file_statuses(paths, current_index_version(vector_db_config))


@app.get("/api/projects/{project_id}/files", response_model=FilePage)
def list_files(
    limit: int = Query(10, ge=1, le=100),
    page: int = Query(1, ge=1),
    search: str = Query("", max_length=220),
    status: FileFilter = Query("all"),
    sort: FileSort = Query("uploaded_desc"),
    project: dict = Depends(require_project),
):
    user_id, project_id = project["user_id"], project["id"]
    with project_access(user_id, project_id) as current:
        _ensure_project(current)
        files = _project_file_statuses(user_id, project_id)
        uploads = [] if sort in NAME_SORTS else _upload_task_store.file_upload_order(user_id, project_id)
        return paginate_files(files, uploads, limit=limit, page=page, search=search, status=status, sort=sort)


@app.get("/api/projects/{project_id}/files/summary")
def file_summary(project: dict = Depends(require_project)):
    user_id, project_id = project["user_id"], project["id"]
    with project_access(user_id, project_id) as current:
        _ensure_project(current)
        return summarize_files(_project_file_statuses(user_id, project_id))


@app.get("/api/projects/{project_id}/sources/{filename}")
def get_source_passage(
    filename: str,
    version: str = Query(pattern=r"^[0-9a-f]{64}$"),
    unit: int = Query(ge=0), start: int = Query(ge=0), end: int = Query(gt=0),
    digest: str = Query(pattern=r"^[0-9a-f]{64}$"),
    snapshot: str = Query(pattern=r"^[0-9a-f]{64}$"),
    project: dict = Depends(require_project),
):
    """按项目读取入库快照并核对原文件版本。"""
    with audit_operation(project["user_id"], "source.read", "file", resource_digest(filename), project_id=project["id"]):
        user_id, project_id = project["user_id"], project["id"]
        try:
            filename = _safe_filename(filename)
            file_path = scoped_knowledge_file(get_knowledge_dir(user_id, project_id), filename)
            with shared_section(PG_CONN, project_index_lock_key(user_id, project_id), timeout_seconds=10), \
                    project_access(user_id, project_id) as current:
                _ensure_project(current)
                if any(os.path.normcase(job["filename"]) == os.path.normcase(filename)
                       for job in file_deletions(PG_CONN, user_id, project_id)):
                    raise HTTPException(410, "文件正在删除")
                def lookup(key: str):
                    identity = (version, document_format(file_path))
                    return _project_manifests(user_id, project_id).source_snapshot(identity, key)

                passage = read_source_passage(file_path, version, unit, start, end, digest,
                                              snapshot=snapshot, lookup=lookup)
            return JSONResponse(passage, headers={"Cache-Control": "no-store"})
        except HTTPException:
            raise
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except SourceVersionChanged as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except TimeoutError:
            raise HTTPException(status_code=503, detail="原文件正在更新，请稍后重试")
        except Exception:
            logger.exception("[原文引用] 读取失败 project_id=%s file=%s", project_id, filename)
            raise HTTPException(status_code=503, detail="暂时无法读取原文，请稍后重试")


def _write_upload_to(file: UploadFile, tmp_path: str, expected_size: int) -> None:
    size = 0
    with open(tmp_path, "wb") as output:
        while chunk := file.file.read(1024 * 1024):
            size += len(chunk)
            if size > expected_size:
                raise ValueError("上传文件大小发生变化，请重新上传")
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    if size != expected_size:
        raise ValueError("上传文件大小发生变化，请重新上传")


@app.post("/api/projects/{project_id}/files/upload", status_code=202, openapi_extra={
    "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {
        "type": "object", "required": ["files"],
        "properties": {"files": {"type": "array", "items": {"type": "string", "format": "binary"}}},
    }}}},
})
async def upload_files(request: Request, project: dict = Depends(require_project)):
    request_limit = MAX_UPLOAD_REQUEST_MB * 1024**2
    request_bytes = upload_request_size(request, request_limit)
    user_id, project_id = project["user_id"], project["id"]
    reservation = None
    queued = False
    try:
        reservation = await anyio.to_thread.run_sync(reserve_upload_request, user_id, project_id, request_bytes,
                                                     UPLOAD_LIMITS, UPLOAD_RECEIVE_TIMEOUT_SECONDS)
        if reservation is None:
            raise HTTPException(404, "项目不存在或已被删除")
        async with upload_form(request, file_limit=MAX_UPLOAD_MB * 1024**2, request_limit=request_limit,
                               check_bytes=reservation.check_bytes,
                               receive_timeout=UPLOAD_RECEIVE_TIMEOUT_SECONDS) as (files, oversized):
            result = await anyio.to_thread.run_sync(_stage_uploads, files, oversized, project, reservation.token)
            queued = result["task_id"] is not None
            return result
    except UploadQuotaExceeded as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    finally:
        if reservation is not None and not queued:
            with anyio.CancelScope(shield=True):
                try:
                    await anyio.to_thread.run_sync(release_upload_request, reservation.token)
                except (OSError, psycopg.Error):
                    logger.exception("[上传] 释放接收额度失败，将在到期后回收")


def _stage_uploads(files: list[UploadFile], oversized: set[UploadFile], project: dict, reservation_token: str):
    with audit_operation(project["user_id"], "file.upload", "project", project["id"], project_id=project["id"], transactional=True) as operation:
        started = time.monotonic()
        user_id, project_id = project["user_id"], project["id"]
        staged, errors, candidates = [], [], []
        for file in files:
            name = file.filename or ""
            try:
                _safe_filename(name)
                if os.path.splitext(name)[1].lower() not in allowed_types:
                    raise ValueError("支持的文件类型：" + "、".join(ext.lstrip(".").upper() for ext in allowed_types))
                if file in oversized:
                    raise ValueError(f"文件超过大小上限 {MAX_UPLOAD_MB} MB")
                if not file.size:
                    raise ValueError("文件内容不能为空")
                candidates.append((file, name, file.size))
            except ValueError as exc:
                errors.append({"name": name or "(无名文件)", "error": str(exc)})
        if not candidates:
            operation.record("denied", "upload_rejected")
            return {"task_id": None, "errors": errors}

        directory = get_knowledge_dir(user_id, project_id)
        incoming = sum(size for _, _, size in candidates)
        try:
            with upload_admission(user_id, project_id, directory, incoming, UPLOAD_LIMITS, reservation_token) as cur:
                if cur is None:
                    raise HTTPException(404, "项目不存在或已被删除")
                os.makedirs(directory, exist_ok=True)
                for position, (file, name, size) in enumerate(candidates):
                    path = scoped_knowledge_file(directory, f"{staged_upload_prefix(reservation_token)}{position}")
                    try:
                        _write_upload_to(file, path, size)
                        staged.append({"name": name, "staged_path": path, "size": size})
                    except (OSError, ValueError):
                        _remove_staged_upload(user_id, project_id, path)
                        logger.exception("[知识库] 上传落盘失败 file=%s project_id=%s", name, project_id)
                        errors.append({"name": name, "error": "上传失败，请稍后重试"})
                if not staged:
                    operation.record("denied", "upload_rejected")
                    return {"task_id": None, "errors": errors}
                task_id = _upload_task_store.create(user_id, project_id, staged, cur=cur)
        except UploadQuotaExceeded as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        logger.info("[上传] 已保存并排队 task_id=%s 文件=%s 保存耗时=%.3fs",
                    task_id, len(staged), time.monotonic() - started)
        return {"task_id": task_id, "errors": errors}


@app.get("/api/projects/{project_id}/upload-tasks")
def list_upload_tasks(project: dict = Depends(require_project)):
    activity, tasks = _upload_task_store.list_tasks(project["user_id"], project["id"])
    return {"activity": activity, "tasks": [_upload_task_payload(task) for task in tasks]}


@app.get("/api/projects/{project_id}/upload-tasks/{task_id}")
def get_upload_task(task_id: UUID, project: dict = Depends(require_project)):
    task = _upload_task_store.get(project["user_id"], project["id"], str(task_id))
    if task is None:
        raise HTTPException(status_code=404, detail="上传任务不存在")
    return _upload_task_payload(task)


def _finish_file_deletions(user_id: int, project_id: str) -> None:
    for job in file_deletions(PG_CONN, user_id, project_id):
        directory = get_knowledge_dir(user_id, project_id)
        def delete_index(path):
            get_vector_store(user_id, project_id, location=job).delete_file_index(path)
        finish_file_deletion(PG_CONN, user_id, project_id, directory, job, delete_index)
        _health_reports.invalidate((user_id, project_id))


@app.delete("/api/projects/{project_id}/files/{filename}")
def delete_file(filename: str, project: dict = Depends(require_project)):
    user_id, project_id = project["user_id"], project["id"]
    with audit_operation(user_id, "file.delete", "file", resource_digest(filename), project_id=project_id, transactional=True):
        try:
            filename = _safe_filename(filename)
            with _index_write_section(user_id, project_id), project_access(user_id, project_id) as current:
                _ensure_project(current)
                jobs = file_deletions(PG_CONN, user_id, project_id)
                if not any(os.path.normcase(job["filename"]) == os.path.normcase(filename) for job in jobs):
                    _ensure_no_uploads(user_id, project_id)
                    source = Path(scoped_knowledge_file(get_knowledge_dir(user_id, project_id), filename))
                    if not source.is_file():
                        raise HTTPException(status_code=404, detail="文件不存在")
                    begin_file_deletion(_project_manifests(user_id, project_id), source.name)
                _health_reports.invalidate((user_id, project_id))
                try:
                    _finish_file_deletions(user_id, project_id)
                except Exception:
                    logger.exception("[知识库] 等待恢复文件删除 project_id=%s", project_id)
                    return JSONResponse({"message": "删除已受理，后台将继续清理文件及索引"}, status_code=202)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(status_code=503, detail="索引正在更新，请稍后重试") from exc
        return {"message": f"已删除 {filename}"}


@app.post("/api/projects/{project_id}/vector/build")
def build_knowledge_index(project: dict = Depends(require_project)):
    with audit_operation(project["user_id"], "index.build", "project", project["id"], project_id=project["id"]) as operation:
        user_id, project_id = project["user_id"], project["id"]
        _ensure_no_uploads(user_id, project_id)
        try:
            with _index_write_section(user_id, project_id), project_access(user_id, project_id) as current:
                _ensure_project(current)
                _ensure_no_uploads(user_id, project_id)
                failures = get_vector_store(user_id, project_id).index_pending_files()
                _health_reports.invalidate((user_id, project_id))
            # API只暴露用户可理解的文件名，绝对存储路径保留在后端。
            failures = [{**item, "file": os.path.basename(item["file"])} for item in failures]
            operation.result = "partial" if failures else "succeeded"
            return {"message": f"构建完成，但有 {len(failures)} 个文件失败" if failures else "构建完成", "failures": failures}
        except HTTPException:
            raise
        except TimeoutError:
            raise HTTPException(status_code=503, detail="索引正被其他操作占用，请稍后重试")
        except Exception:
            logger.exception("[知识库] 构建失败 project_id=%s", project_id)
            raise HTTPException(status_code=500, detail="构建失败，请稍后重试")


@app.get("/api/projects/{project_id}/index/health")
async def get_index_health(request: Request, refresh: bool = False, project: dict = Depends(require_project)):
    user_id, project_id = project["user_id"], project["id"]
    if not refresh:
        return _health_reports.peek((user_id, project_id))
    return await _refresh_health_report(request, user_id, project_id)


@app.post("/api/projects/{project_id}/index/health/repair")
def repair_index_health(project: dict = Depends(require_project)):
    with audit_operation(project["user_id"], "index.repair", "project", project["id"], project_id=project["id"]) as operation:
        user_id, project_id = project["user_id"], project["id"]
        try:
            with _index_write_section(user_id, project_id), project_access(user_id, project_id) as current:
                _ensure_project(current)
                _resolve_pending_uploads(user_id, project_id)
                _ensure_no_uploads(user_id, project_id)
                report = IndexHealth(user_id, project_id).repair(include_pending=True)
                _health_reports.set((user_id, project_id), report)
            operation.result = _repair_audit_result(report)
            return report
        except HTTPException:
            raise
        except TimeoutError:
            raise HTTPException(status_code=503, detail="索引正被其他操作占用，请稍后重试")
        except Exception:
            logger.exception("[索引健康检查] 修复与补建失败 project_id=%s", project_id)
            raise HTTPException(status_code=500, detail="修复与补建失败，请稍后重试")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "api_server:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        workers=UVICORN_WORKERS,
    )
