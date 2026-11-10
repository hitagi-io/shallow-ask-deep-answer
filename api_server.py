"""FastAPI 入口：账户、流式对话和知识库管理 API。"""
import os
import json
import time
import uuid
import asyncio
import threading
import shutil
import re
import math
from pathlib import Path
from uuid import UUID
from functools import partial
from collections.abc import Callable
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Header, Request, Query

import anyio
import psycopg
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from chat_agent import ChatAgent, PG_CHECKPOINT_POOL_MAX
from rag.vector_store import get_vector_store
from rag.index_health import IndexHealth
from rag.index_manifest import ManifestStore, current_index_version, project_collection_name
from utils.knowledge_files import list_supported_files
from utils.config_loader import vector_db_config
from utils.project_paths import get_knowledge_dir, get_knowledge_storage_root, scoped_knowledge_file
from utils.pg_lock import SingleInstanceGuard, exclusive_section, project_index_lock_key
from utils.generation_runner import run_generation
from utils.health_report_cache import HealthReportCache, HealthReportInvalidated
from utils.health_scan import HealthScanRunner, ProjectGoneError
from utils.readiness import check_readiness
from utils.request_wait import wait_for_client
from utils.logging_config import configure_logging, get_logger
from utils.upload_tasks import UploadTaskStore
from auth import (
    PASSWORD_MAX_LENGTH,
    PG_CONN,
    PG_POOL_MAX,
    init_db,
    register,
    login,
    consume_rate_limit,
    verify_token,
    get_user_by_id,
    list_conversations,
    list_messages,
    create_conversation,
    update_conversation_title,
    begin_generation,
    get_generation,
    cancel_generation,
    save_generation_progress,
    finish_generation,
    delete_conversation_guarded,
    acquire_chat_slot,
    bind_chat_slot_generation,
    release_chat_slot,
    list_projects,
    get_project,
    create_project,
    update_project,
    delete_project,
    project_access,
    list_project_scopes,
)
from accounts import AccountError, update_account, prepare_deletion, delete_account

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

# 就绪检查的总时限（秒），覆盖进程启动、建连、查询及 RPC。
READINESS_TIMEOUT_SECONDS = float(os.getenv("READINESS_TIMEOUT_SECONDS", "3"))
if not math.isfinite(READINESS_TIMEOUT_SECONDS) or READINESS_TIMEOUT_SECONDS < 0.5:
    raise ValueError("READINESS_TIMEOUT_SECONDS 必须为不小于 0.5 的有限数值")



def _warn_connection_budget() -> None:
    """启动时核算 PG 连接需求，超出上限时明确告警（多 worker 时最容易踩）。"""
    per_worker = PG_CHECKPOINT_POOL_MAX + PG_POOL_MAX
    need = UVICORN_WORKERS * per_worker
    try:
        with psycopg.connect(PG_CONN) as conn, conn.cursor() as cur:
            cur.execute("SHOW max_connections")
            max_conn = int(cur.fetchone()[0])
    except Exception as e:
        logger.warning(f"[启动] 无法读取 PostgreSQL 的 max_connections，跳过连接预算检查：{e}")
        return
    detail = f"workers={UVICORN_WORKERS} × (检查点池 {PG_CHECKPOINT_POOL_MAX} + 业务池 {PG_POOL_MAX}) = {need}"
    if need > max_conn - 5:
        state = "超出" if need > max_conn else "接近"
        logger.warning(f"[启动] PostgreSQL 预计连接占用约 {need}，已{state}连接上限（max_connections={max_conn}；{detail}）。"
              "建议提高 max_connections，或降低连接池上限。")
    else:
        logger.info(f"[启动] PostgreSQL 连接容量充足：预计占用 {need}，连接上限 {max_conn}（{detail}）")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    get_knowledge_storage_root()
    anyio.to_thread.current_default_thread_limiter().total_tokens = THREADPOOL_SIZE
    logger.info(f"[启动] AnyIO 默认线程池并发额度为 {THREADPOOL_SIZE}，工作进程数为 {UVICORN_WORKERS}")
    _warn_connection_budget()
    task = asyncio.create_task(_scheduled_health_loop(), name="scheduled-health")
    task.add_done_callback(_background_task_done)
    upload_stop = asyncio.Event()
    upload_executor = ThreadPoolExecutor(max_workers=UPLOAD_WORKERS, thread_name_prefix="upload-index")
    upload_workers = [asyncio.create_task(_upload_worker_loop(i, upload_stop, upload_executor),
                                         name=f"upload-index-{i}") for i in range(UPLOAD_WORKERS)]
    try:
        yield
    finally:
        upload_stop.set()
        task.cancel()
        for generation_task in list(_generation_tasks):
            generation_task.cancel()
        await asyncio.gather(task, *_generation_tasks, return_exceptions=True)
        # Drain in-flight work; cancellation of to_thread cannot stop an index write.
        await asyncio.gather(*upload_workers, return_exceptions=True)
        upload_executor.shutdown(wait=True)


app = FastAPI(
    title="浅问深答",
    lifespan=lifespan,
    docs_url="/docs" if ENABLE_API_DOCS else None,
    redoc_url="/redoc" if ENABLE_API_DOCS else None,
    openapi_url="/openapi.json" if ENABLE_API_DOCS else None,
)

init_db()

agent = ChatAgent()
allowed_types = vector_db_config["allowed_types"]

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "50"))
UPLOAD_WORKERS = int(os.getenv("UPLOAD_WORKERS", "4"))
UPLOAD_TASK_POLL_SECONDS = float(os.getenv("UPLOAD_TASK_POLL_SECONDS", "0.5"))
if UPLOAD_WORKERS < 1:
    raise ValueError("UPLOAD_WORKERS 必须为正整数")
if not math.isfinite(UPLOAD_TASK_POLL_SECONDS) or UPLOAD_TASK_POLL_SECONDS <= 0:
    raise ValueError("UPLOAD_TASK_POLL_SECONDS 必须为正数")
_upload_task_store = UploadTaskStore(PG_CONN)

HEALTH_REPORT_TTL_SECONDS = int(os.getenv("HEALTH_REPORT_TTL_SECONDS", "120"))
HEALTH_CHECK_TIMEOUT_SECONDS = float(os.getenv("HEALTH_CHECK_TIMEOUT_SECONDS", "60"))
HEALTH_CHECK_MAX_CONCURRENT = int(os.getenv("HEALTH_CHECK_MAX_CONCURRENT", "4"))
if not math.isfinite(HEALTH_CHECK_TIMEOUT_SECONDS) or HEALTH_CHECK_TIMEOUT_SECONDS < 0.5:
    raise ValueError("HEALTH_CHECK_TIMEOUT_SECONDS 必须为不小于 0.5 的有限数值")
if HEALTH_CHECK_MAX_CONCURRENT < 1:
    raise ValueError("HEALTH_CHECK_MAX_CONCURRENT 必须为正整数")
_health_reports: dict[tuple[int, str], HealthReportCache] = {}
_health_reports_lock = threading.Lock()
_health_scanner = HealthScanRunner(HEALTH_CHECK_TIMEOUT_SECONDS, HEALTH_CHECK_MAX_CONCURRENT)
_readiness_report = HealthReportCache(1)


def _health_cache(user_id: int, project_id: str) -> HealthReportCache:
    with _health_reports_lock:
        return _health_reports.setdefault((user_id, project_id), HealthReportCache(HEALTH_REPORT_TTL_SECONDS))


def _invalidate_health(user_id: int, project_id: str) -> None:
    with _health_reports_lock:
        cache = _health_reports.get((user_id, project_id))
        if cache is not None:
            # Keep any in-flight task registered until it has released its resources.
            cache.invalidate()


async def _request_health_snapshot(request: Request, user_id: int, project_id: str,
                                   refresh: bool = False) -> dict:
    async def check():
        return await _health_scanner.scan(user_id, project_id, PG_CONN, INDEX_READ_LOCK_TIMEOUT_SECONDS)

    try:
        # require_project has already authenticated/authorized this request. Only
        # the shared scan task acquires index/project locks and storage connections.
        return await wait_for_client(request, _health_cache(user_id, project_id).get(check, refresh=refresh))
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
    return exclusive_section(PG_CONN, project_index_lock_key(user_id, project_id), INDEX_WRITE_LOCK_TIMEOUT_SECONDS)


def _format_repair_summary(repair: dict) -> str:
    """格式化修复摘要，供定时自动修复和手动修复共用同一套表述。"""
    record_state = "已更新" if repair["manifest_rebuilt"] else "未变更"
    return (
        f"清理残留向量 {len(repair['ghost_deleted'])} 个"
        f"（失败 {len(repair['ghost_errors'])} 个），"
        f"索引记录{record_state}，重索引 {len(repair['re_indexed'])} 个"
        f"（失败 {len(repair['re_index_failures'])} 个）"
    )


def _auto_heal_report(user_id: int, project_id: str) -> dict | None:
    """索引自动修复闭环：健康检查发现问题 → 自动修复 → 复查，直至检查通过或达到轮次上限。

    停止条件：report['healthy'] 为 True。Milvus 不可达或修复异常时立即终止，
    等待下一轮健康检查再试。
    """
    with _index_write_section(user_id, project_id), project_access(user_id, project_id) as project:
        if project is None:
            return None
        if _upload_task_store.has_active(user_id, project_id):
            return None
        index_health = IndexHealth(user_id, project_id)
        max_rounds = vector_db_config["health_auto_repair_max_rounds"]
        report = index_health.check(record_files=True)
        for _ in range(max_rounds):
            if report["healthy"]:
                break
            if report["milvus_error"] or report["manifest_error"]:
                logger.warning("[索引自动修复] 存储不可用，本轮暂不执行修复")
                break
            try:
                report = index_health.repair()
                repair = report["repair"]
                logger.info(f"[索引自动修复] 本轮修复完成：{_format_repair_summary(repair)}")
            except Exception as e:
                logger.error(f"[索引自动修复] 执行失败：{e}")
                break
        _health_cache(user_id, project_id).set({"report": report})
        return report


def _upload_task_payload(task: dict) -> dict:
    """Return task state without exposing staged filesystem paths."""
    return {
        "task_id": task["id"],
        "status": task["status"],
        "total": task["total"],
        "completed": task["completed"],
        "succeeded": task["succeeded"],
        "failed": task["failed"],
        "error": task["error"],
        "files": [
            {
                "name": item["name"],
                "status": item["status"],
                "overwritten": item["overwritten"],
                "indexed": item["indexed"],
                "error": item["error"],
            }
            for item in task["files"]
        ],
    }


def _process_upload_task(task: dict, lease: psycopg.Connection) -> None:
    """Index one task; the database queue guarantees project ordering."""
    task_id = task["id"]
    user_id, project_id = task["user_id"], task["project_id"]
    try:
        for item in _upload_task_store.items(task_id):
            if item["status"] != "queued":
                continue
            _upload_task_store.mark_item_running(task_id, item["id"])
            status = "error"
            overwritten = None
            indexed = False
            error = None
            staged_path = item["staged_path"]
            try:
                with _index_write_section(user_id, project_id), project_access(user_id, project_id) as current:
                    # Stop using a queue lease lost to a database disconnect.
                    lease.execute("SELECT 1")
                    _ensure_project(current)
                    knowledge_dir = get_knowledge_dir(user_id, project_id)
                    staged_path = scoped_knowledge_file(knowledge_dir, staged_path)
                    destination = scoped_knowledge_file(
                        knowledge_dir, os.path.join(knowledge_dir, item["name"])
                    )
                    result = get_vector_store(user_id, project_id).replace_and_index_file(
                        destination, staged_path
                    )
                    overwritten = bool(result.get("overwritten"))
                    indexed = bool(result.get("ok"))
                    error = result.get("error")
                    status = "success" if indexed and not error else "error"
                _invalidate_health(user_id, project_id)
            except TimeoutError:
                error = "索引正被其他操作占用，请稍后重试"
            except HTTPException as exc:
                error = str(exc.detail)
            except Exception:
                logger.exception("[知识库] 后台上传任务失败 file=%s project_id=%s", item["name"], project_id)
                error = "后台入库失败，请稍后重试"
            finally:
                try:
                    _remove_staged_upload(user_id, project_id, staged_path)
                except (OSError, ValueError):
                    logger.exception("[知识库] 临时上传文件清理失败 file=%s", staged_path)
            _upload_task_store.finish_item(
                task_id, item["id"], status=status, overwritten=overwritten,
                indexed=indexed, error=error,
            )
        _upload_task_store.finish(task_id)
    except Exception:
        logger.exception("[知识库] 后台上传任务异常 task_id=%s", task_id)
        try:
            _upload_task_store.fail(task_id, "后台任务异常中断，请重新上传")
        except Exception:
            logger.exception("[知识库] 后台上传任务状态写回失败 task_id=%s", task_id)


def _run_next_upload() -> bool:
    claimed = _upload_task_store.claim_next()
    if claimed is None:
        return False
    task, lease = claimed
    try:
        if task["status"] == "running":
            # Only a worker whose session lease disappeared can reach here.
            # A crash may have moved the staged bytes already: do not replay a
            # replacement blindly or report that it was successfully indexed.
            with _index_write_section(task["user_id"], task["project_id"]):
                _upload_task_store.fail(task["id"], "后台任务中断，请检查文档状态或重新上传")
                for item in _upload_task_store.items(task["id"]):
                    _remove_staged_upload(task["user_id"], task["project_id"], item["staged_path"])
                _invalidate_health(task["user_id"], task["project_id"])
        else:
            _process_upload_task(task, lease)
            for item in _upload_task_store.items(task["id"]):
                _remove_staged_upload(task["user_id"], task["project_id"], item["staged_path"])
        return True
    finally:
        lease.close()


def _remove_staged_upload(user_id: int, project_id: str, path: str) -> None:
    path = scoped_knowledge_file(get_knowledge_dir(user_id, project_id), path)
    if not os.path.basename(path).startswith(".upload_"):
        raise ValueError("无效的上传暂存文件")
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


async def _upload_worker_loop(worker_id: int, stop: asyncio.Event, executor: ThreadPoolExecutor) -> None:
    while not stop.is_set():
        try:
            worked = await asyncio.get_running_loop().run_in_executor(executor, _run_next_upload)
            if worked:
                continue
        except Exception:
            logger.exception("[知识库] 上传 worker=%s 轮询失败", worker_id)
        try:
            await asyncio.wait_for(stop.wait(), timeout=UPLOAD_TASK_POLL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def _scheduled_health_loop():
    """定时健康检查和自动修复。

    多 worker/多实例下必须只由一个进程执行，否则会重复修复、重复触发嵌入调用；
    使用 PostgreSQL 会话级咨询锁（advisory lock）选出唯一执行进程；持有者退出后，其他进程自动接管。
    """
    interval = vector_db_config["health_check_interval_hours"] * 3600
    guard = SingleInstanceGuard(PG_CONN, _PG_LOCK_KEY_HEALTH)
    next_run = 0.0
    was_leader = False
    while True:
        is_leader = await asyncio.to_thread(guard.try_acquire)
        if is_leader != was_leader:
            logger.info(
                "[索引健康检查] 当前进程已获得定时检查和修复的执行权" if is_leader
                else "[索引健康检查] 当前进程已失去执行权，暂停定时检查与修复"
            )
            was_leader = is_leader
        if not is_leader or time.monotonic() < next_run:
            await asyncio.sleep(_HEALTH_LEADER_POLL_SECONDS)
            continue
        try:
            for scope in await asyncio.to_thread(list_project_scopes):
                try:
                    report = await asyncio.to_thread(_auto_heal_report, scope["user_id"], scope["id"])
                    if report is not None:
                        logger.info("[索引健康检查] 项目 %s 检查完成 healthy=%s", scope["id"], report["healthy"])
                except Exception:
                    logger.exception("[索引健康检查] 项目 %s 检查失败", scope["id"])
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



def get_current_user(authorization: str = Header(None)) -> dict:
    """复核账号与凭证版本，让改密和注销在所有 worker 上立即生效。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未登录")
    token = authorization[7:]
    payload = verify_token(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    user_id = payload.get("user_id")
    if user_id is None:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    user = get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="账号不存在或已被注销")
    if payload.get("auth_version", 0) != user["auth_version"]:
        raise HTTPException(status_code=401, detail="密码已更新，请重新登录")
    return {"user_id": user["id"], "username": user["username"], "auth_version": user["auth_version"]}


def require_project(project_id: UUID, user: dict = Depends(get_current_user)) -> dict:
    project = get_project(user["user_id"], str(project_id))
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在或已被删除")
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



@app.post("/api/auth/register")
def api_register(req: RegisterRequest, request: Request):
    client_ip = request.client.host if request.client else None
    result = register(req.username, req.password, client_ip)
    if not result["ok"]:
        raise HTTPException(
            status_code=429 if result.get("rate_limited") else 400, detail=result["error"]
        )
    return {"message": "注册成功"}


@app.post("/api/auth/login")
def api_login(req: LoginRequest, request: Request):
    client_ip = request.client.host if request.client else None
    result = login(req.username, req.password, client_ip)
    if not result["ok"]:
        raise HTTPException(
            status_code=429 if result.get("rate_limited") else 401, detail=result["error"]
        )
    return {"token": result["token"], "user": result["user"]}


@app.get("/api/auth/me")
def api_me(user: dict = Depends(get_current_user)):
    return {"user": {"id": user["user_id"], "username": user["username"]}}


@app.patch("/api/auth/me")
def api_update_account(req: UpdateAccountRequest, user: dict = Depends(get_current_user)):
    try:
        return update_account(user["user_id"], user["auth_version"], **req.model_dump())
    except AccountError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@app.post("/api/auth/deletion-verification")
def api_prepare_deletion(req: DeletionVerificationRequest, user: dict = Depends(get_current_user)):
    try:
        return prepare_deletion(user["user_id"], user["auth_version"], req.password)
    except AccountError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@app.delete("/api/auth/me")
def api_delete_account(req: DeleteAccountRequest, user: dict = Depends(get_current_user)):
    user_id = user["user_id"]

    def cleanup(project_ids, conversation_ids):
        for conv_id in conversation_ids:
            _purge_thread_checkpoints(f"{user_id}:{conv_id}")
        for project_id in project_ids:
            try:
                get_vector_store(user_id, project_id).drop_project_index()
                _remove_project_files(user_id, project_id)
                agent.forget_project(user_id, project_id)
            finally:
                _invalidate_health(user_id, project_id)
        # Only remove the now-empty account directory, never an unchecked subtree.
        root = Path(get_knowledge_storage_root()).resolve()
        account_dir = root / str(user_id)
        if account_dir.resolve() != account_dir or account_dir.parent != root:
            raise RuntimeError("无效的账号文件目录")
        if account_dir.exists():
            account_dir.rmdir()

    try:
        delete_account(user_id, user["auth_version"], req.confirmation_token, req.confirmation_text,
                       cleanup=cleanup,
                       stale_seconds=CHAT_GENERATION_STALE_SECONDS, slot_max_age=CHAT_SLOT_MAX_AGE_SECONDS)
        return {"message": "账号及其项目、文档、索引和对话已注销删除"}
    except AccountError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    except (TimeoutError, psycopg.errors.LockNotAvailable) as exc:
        raise HTTPException(status_code=409, detail="账号资料正在处理其他操作，请稍后重试注销") from exc
    except Exception as exc:
        logger.exception("[账号] 注销清理未完成 user_id=%s", user_id)
        raise HTTPException(status_code=503, detail="注销清理未完成，部分数据可能已删除。账号仍保留，请重试注销。") from exc



@app.get("/api/conversations")
def api_list_conversations(
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(None, max_length=512),
    user: dict = Depends(get_current_user),
):
    try:
        return list_conversations(user["user_id"], limit, cursor)
    except ValueError as exc:
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


def _purge_thread_checkpoints(thread_id: str) -> None:
    """删除会话对应的 LangGraph 检查点；仅数据库运行故障最多尝试 3 次。"""
    for attempt in range(3):
        try:
            agent.checkpointer.delete_thread(thread_id)
            return
        except psycopg.OperationalError:
            logger.exception("[会话] 清理检查点失败 thread_id=%s（第 %s 次尝试）", thread_id, attempt + 1)
            if attempt == 2:
                raise
            time.sleep(0.5)


@app.delete("/api/conversations/{conv_id}")
def api_delete_conversation(conv_id: str, user: dict = Depends(get_current_user)):
    """删除会话：与生成任务互斥，运行中的回答会先收到取消信号，本次删除返回 409。

    检查点清理与会话删除放在同一个持锁事务里：清理失败时整体回滚，
    不会出现「上下文已删、业务会话还在」的状态。
    """
    thread_id = f"{user['user_id']}:{conv_id}"
    try:
        result = delete_conversation_guarded(
            user["user_id"],
            conv_id,
            CHAT_GENERATION_STALE_SECONDS,
            lambda: _purge_thread_checkpoints(thread_id),
        )
    except Exception as e:
        logger.error(f"[会话] 删除失败 conversation_id={conv_id}：{e}")
        raise HTTPException(status_code=500, detail="清理会话上下文失败，请稍后重试")
    if result == "not_found":
        raise HTTPException(status_code=404, detail="会话不存在")
    if result == "busy":
        raise HTTPException(status_code=409, detail="回答正在生成，已请求取消，请稍后再试")
    return {"message": "ok"}



# 跨进程计数由 chat_slots 租约表承担；本映射只保存本进程内的取消回调，用于在
# 租约被清理（可能由其他 worker 触发）时及时停止仍然存活的上游生成。
_chat_slot_cancels: dict[str, Callable[[], None]] = {}
_chat_slot_cancels_lock = threading.Lock()


async def _acquire_chat_slot(user_id: int) -> dict:
    """占用一个跨进程对话并发槽位，返回 {"token", "reaped"}；token 为 None 表示已达上限。

    达到 CHAT_SLOT_MAX_AGE_SECONDS 或绑定任务已结束/心跳超时的租约会被清理；
    跨进程取消已由数据库写入 cancel_requested，本进程内的对应任务还需通过
    本地回调立即停止，否则会继续占用模型请求与线程资源。
    """
    lease = await asyncio.to_thread(
        acquire_chat_slot,
        user_id,
        MAX_CONCURRENT_CHAT_PER_USER,
        CHAT_SLOT_MAX_AGE_SECONDS,
        CHAT_GENERATION_STALE_SECONDS,
    )
    for token in lease["reaped"]:
        with _chat_slot_cancels_lock:
            cancel = _chat_slot_cancels.pop(token, None)
        if cancel is not None:
            try:
                cancel()
            except Exception as e:
                logger.error(f"[对话流] 取消超时槽位任务失败（已忽略）：{e}")
    return lease


def _bind_chat_slot_cancel(token: str, cancel) -> None:
    """把租约与生成任务的取消入口绑定，租约清理时可以停止本进程内的任务。"""
    with _chat_slot_cancels_lock:
        _chat_slot_cancels[token] = cancel


async def _release_chat_slot(user_id: int, token: str) -> None:
    with _chat_slot_cancels_lock:
        _chat_slot_cancels.pop(token, None)
    try:
        await asyncio.to_thread(release_chat_slot, user_id, token)
    except Exception as e:
        logger.error(f"[对话流] 释放并发槽位失败（将由租约过期清理）：{e}")


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


async def _run_generation(req: ChatRequest, user_id: int, generation_id: int, slot: str):
    try:
        await run_generation(
            lambda should_stop: agent.stream_answer(
                req.message, f"{user_id}:{req.thread_id}", kb_only=req.kb_only, should_stop=should_stop,
                user_id=user_id, project_id=req.project_id,
            ),
            partial(save_generation_progress, generation_id),
            partial(finish_generation, generation_id),
            deadline=CHAT_STREAM_DEADLINE_SECONDS,
            save_interval=CHAT_GENERATION_SAVE_SECONDS,
            on_bridge=lambda bridge: _bind_chat_slot_cancel(slot, bridge.stop),
        )
    finally:
        await _release_chat_slot(user_id, slot)


async def _generation_events(user_id: int, conv_id: str, request_id: str, auth_version: int):
    previous = None
    while True:
        account = await asyncio.to_thread(get_user_by_id, user_id)
        if account is None or account["auth_version"] != auth_version:
            # Reconnecting clients receive 401; old streams stop exposing snapshots.
            return
        generation = await asyncio.to_thread(
            get_generation, user_id, conv_id, request_id, CHAT_GENERATION_STALE_SECONDS,
        )
        if generation is None:
            yield _sse({"type": "error", "status": "error", "error": "未找到这次回答，请刷新页面后重试。"})
            return
        # elapsed_seconds 每次读取都会变；仅内容、阶段或状态改变时推送完整快照。
        signature = {k: v for k, v in generation.items() if k != "elapsed_seconds"}
        if signature != previous:
            yield _sse({"type": "snapshot", **generation})
            previous = signature
        if generation["status"] != "running":
            return
        # 持续输出注释心跳，避免代理把思考阶段的连接视为空闲。
        yield ": keep-alive\n\n"
        await asyncio.sleep(0.5)


@app.get("/api/conversations/{conv_id}/generations/{request_id}/stream")
async def resume_generation(conv_id: str, request_id: str, user: dict = Depends(get_current_user)):
    generation = await asyncio.to_thread(
        get_generation, user["user_id"], conv_id, request_id, CHAT_GENERATION_STALE_SECONDS,
    )
    if generation is None:
        raise HTTPException(status_code=404, detail="未找到这次回答，请刷新页面后重试。")
    return StreamingResponse(_generation_events(user["user_id"], conv_id, request_id, user["auth_version"]),
                             media_type="text/event-stream", headers=SSE_HEADERS)


@app.post("/api/conversations/{conv_id}/generations/{request_id}/cancel")
async def stop_generation(conv_id: str, request_id: str, user: dict = Depends(get_current_user)):
    if not await asyncio.to_thread(cancel_generation, user["user_id"], conv_id, request_id):
        raise HTTPException(status_code=404, detail="未找到这次回答，请刷新页面后重试。")
    return {"message": "ok"}


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest, user: dict = Depends(get_current_user)):
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="消息不能为空")
    if req.kb_only and req.project_id is None:
        raise HTTPException(status_code=400, detail="请先选择知识库项目")
    if not req.kb_only and req.project_id is not None:
        raise HTTPException(status_code=400, detail="通用模式不使用项目")
    existing = await asyncio.to_thread(
        get_generation, user["user_id"], req.thread_id, req.request_id, CHAT_GENERATION_STALE_SECONDS,
    )
    if existing is not None:
        if existing["kb_only"] != req.kb_only or existing["project_id"] != req.project_id:
            raise HTTPException(status_code=409, detail="请求编号已用于其他模式或项目，请重新发送")
        # 幂等重放和恢复只订阅已有任务，不消耗新的并发槽位或发送额度。
        return StreamingResponse(_generation_events(user["user_id"], req.thread_id, req.request_id, user["auth_version"]),
                                 media_type="text/event-stream", headers=SSE_HEADERS)
    if not await asyncio.to_thread(
        consume_rate_limit, "chat", str(user["user_id"]), CHAT_RATE_LIMIT, CHAT_RATE_WINDOW_SECONDS
    ):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")

    try:
        lease = await _acquire_chat_slot(user["user_id"])
    except Exception as e:
        logger.error(f"[对话流] 获取并发槽位失败 user_id={user['user_id']}：{e}")
        raise HTTPException(status_code=503, detail="服务暂时不可用，请稍后重试")
    slot = lease["token"]
    if slot is None:
        raise HTTPException(status_code=429, detail="并发回答数过多，请等待当前回答完成")

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
        )
    except Exception as e:
        await _release_chat_slot(user["user_id"], slot)
        logger.error(f"[对话流] 登记生成任务失败 thread_id={req.thread_id}：{e}")
        raise HTTPException(status_code=503, detail="服务暂时不可用，请稍后重试")

    if begun["status"] == "not_found":
        await _release_chat_slot(user["user_id"], slot)
        raise HTTPException(status_code=404, detail="会话不存在或已被删除")
    if begun["status"] == "busy":
        await _release_chat_slot(user["user_id"], slot)
        raise HTTPException(status_code=409, detail="当前会话已有回答正在生成，请稍后再试")

    if begun["status"] == "mode_conflict":
        await _release_chat_slot(user["user_id"], slot)
        raise HTTPException(status_code=409, detail="请求编号已用于其他问答模式，请重新发送")

    if begun["status"] == "conversation_mode_conflict":
        await _release_chat_slot(user["user_id"], slot)
        raise HTTPException(status_code=409, detail="此会话的问答模式已固定，请开启新对话后切换模式")

    project_errors = {
        "project_required": (400, "请先选择知识库项目"),
        "project_not_found": (404, "项目不存在或已被删除"),
        "project_conflict": (409, "请求的项目与问答模式或原请求不一致"),
        "conversation_project_conflict": (409, "此会话的项目已固定，请开启新对话后选择项目"),
    }
    if begun["status"] in project_errors:
        await _release_chat_slot(user["user_id"], slot)
        status, detail = project_errors[begun["status"]]
        raise HTTPException(status_code=status, detail=detail)

    if begun["status"] == "replay":
        await _release_chat_slot(user["user_id"], slot)
    else:
        # 在返回响应前启动独立任务；断开 SSE 只结束订阅。
        generation_task = asyncio.create_task(
            _run_generation(req, user["user_id"], begun["generation_id"], slot),
            name=f"generation-{begun['generation_id']}",
        )
        _generation_tasks.add(generation_task)
        generation_task.add_done_callback(_background_task_done)
        try:
            await asyncio.to_thread(bind_chat_slot_generation, user["user_id"], slot, begun["generation_id"])
        except Exception:
            logger.exception("[对话流] 关联并发槽位与生成任务失败")

    return StreamingResponse(_generation_events(user["user_id"], req.thread_id, req.request_id, user["auth_version"]),
                             media_type="text/event-stream", headers=SSE_HEADERS)



@app.get("/api/projects")
def api_list_projects(user: dict = Depends(get_current_user)):
    return {"projects": list_projects(user["user_id"])}


@app.post("/api/projects", status_code=201)
def api_create_project(req: ProjectRequest, user: dict = Depends(get_current_user)):
    return {"project": create_project(user["user_id"], req.name)}


@app.patch("/api/projects/{project_id}")
def api_update_project(req: ProjectRequest, project: dict = Depends(require_project)):
    updated = update_project(project["user_id"], project["id"], req.name)
    if updated is None:
        raise HTTPException(status_code=404, detail="项目不存在或已被删除")
    return {"project": updated}


def _remove_project_files(user_id: int, project_id: str) -> None:
    # 目录只由经过规范化的账号与UUID构造；删除前再次确认完整路径边界。
    target = Path(get_knowledge_dir(user_id, project_id)).resolve()
    account_dir = Path(get_knowledge_dir(user_id, project_id)).parent.resolve()
    if target.parent != account_dir or target.name != str(UUID(project_id)):
        raise RuntimeError("无效的项目文件目录")
    if target.exists():
        shutil.rmtree(target)


@app.delete("/api/projects/{project_id}")
def api_delete_project(project: dict = Depends(require_project)):
    user_id, project_id = project["user_id"], project["id"]

    def cleanup(conversation_ids):
        for conv_id in conversation_ids:
            _purge_thread_checkpoints(f"{user_id}:{conv_id}")
        store = get_vector_store(user_id, project_id)
        store.drop_project_index()
        _remove_project_files(user_id, project_id)
        agent.forget_project(user_id, project_id)

    try:
        with _index_write_section(user_id, project_id):
            result = delete_project(user_id, project_id, CHAT_GENERATION_STALE_SECONDS, cleanup)
        if result["status"] == "not_found":
            raise HTTPException(status_code=404, detail="项目不存在或已被删除")
        if result["status"] == "busy":
            raise HTTPException(status_code=409, detail="项目中有回答正在生成，请停止或等待完成后再删除")
        _invalidate_health(user_id, project_id)
        return {"message": "项目及其文档、相关对话已删除"}
    except HTTPException:
        raise
    except TimeoutError:
        raise HTTPException(status_code=503, detail="项目正在处理其他操作，请稍后重试")
    except Exception:
        logger.exception("[项目] 删除失败 project_id=%s", project_id)
        raise HTTPException(status_code=503, detail="项目清理未完成，请重试删除")
    finally:
        _invalidate_health(user_id, project_id)


def _ensure_project(project):
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在或已被删除")


def _ensure_no_uploads(user_id: int, project_id: str) -> None:
    if _upload_task_store.has_active(user_id, project_id):
        raise HTTPException(status_code=409, detail="项目有上传任务等待或正在入库，请完成后重试")


def _safe_filename(filename: str) -> str:
    # 跨平台拒绝目录、Windows设备名、流名称及会发生归一化冲突的名字。
    if (not filename or len(filename) > 220 or filename.startswith(".")
            or filename.rstrip(" .") != filename
            or any(ord(c) < 32 or c in '<>:"/\\|?*' for c in filename)
            or re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", filename, re.I)):
        raise ValueError("无效的文件名")
    return filename


@app.get("/api/projects/{project_id}/files")
def list_files(project: dict = Depends(require_project)):
    user_id, project_id = project["user_id"], project["id"]
    with project_access(user_id, project_id) as current:
        _ensure_project(current)
        directory = get_knowledge_dir(user_id, project_id)
        paths = list_supported_files(directory, allowed_types)
        manifests = ManifestStore({**vector_db_config, "collection_name":
                                   project_collection_name(vector_db_config, user_id, project_id)})
        return {"files": manifests.file_statuses(paths, current_index_version(vector_db_config))}


def _write_upload_to(file: UploadFile, tmp_path: str) -> None:
    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    size = 0
    with open(tmp_path, "wb") as output:
        while chunk := file.file.read(1024 * 1024):
            size += len(chunk)
            if size > max_bytes:
                raise ValueError(f"文件超过大小上限 {MAX_UPLOAD_MB} MB")
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    if size == 0:
        raise ValueError("文件内容不能为空")


@app.post("/api/projects/{project_id}/files/upload", status_code=202)
def upload_files(files: list[UploadFile] = File(...), project: dict = Depends(require_project)):
    user_id, project_id = project["user_id"], project["id"]
    staged, errors = [], []
    # Shared project access prevents deletion during staging; there is no
    # index lock here, so even a slow embedding request cannot delay upload.
    with project_access(user_id, project_id) as current:
        _ensure_project(current)
        knowledge_dir = get_knowledge_dir(user_id, project_id)
        os.makedirs(knowledge_dir, exist_ok=True)
        try:
            for file in files:
                name = file.filename or ""
                staged_path = scoped_knowledge_file(knowledge_dir, os.path.join(knowledge_dir, f".upload_{uuid.uuid4().hex}"))
                accepted = False
                try:
                    name = _safe_filename(name)
                    if os.path.splitext(name)[1].lower() not in allowed_types:
                        raise ValueError("仅支持 TXT、PDF、DOCX")
                    _write_upload_to(file, staged_path)
                    staged.append({"name": name, "staged_path": staged_path})
                    accepted = True
                except ValueError as exc:
                    errors.append({"name": name or "(无名文件)", "error": str(exc)})
                except Exception:
                    logger.exception("[知识库] 上传落盘失败 file=%s project_id=%s", name, project_id)
                    errors.append({"name": name, "error": "上传失败，请稍后重试"})
                finally:
                    if not accepted:
                        _remove_staged_upload(user_id, project_id, staged_path)
            if not staged:
                return {"task_id": None, "errors": errors}
            task_id = _upload_task_store.create(user_id, project_id, staged)
        except BaseException:
            for item in staged:
                _remove_staged_upload(user_id, project_id, item["staged_path"])
            raise
    return {"task_id": task_id, "errors": errors}


@app.get("/api/projects/{project_id}/upload-tasks")
def list_upload_tasks(project: dict = Depends(require_project)):
    return {"tasks": [_upload_task_payload(task) for task in
                      _upload_task_store.list_tasks(project["user_id"], project["id"])]}


@app.get("/api/projects/{project_id}/upload-tasks/{task_id}")
def get_upload_task(task_id: UUID, project: dict = Depends(require_project)):
    task = _upload_task_store.get(project["user_id"], project["id"], str(task_id))
    if task is None:
        raise HTTPException(status_code=404, detail="上传任务不存在")
    return _upload_task_payload(task)


@app.delete("/api/projects/{project_id}/files/{filename}")
def delete_file(filename: str, project: dict = Depends(require_project)):
    user_id, project_id = project["user_id"], project["id"]
    _ensure_no_uploads(user_id, project_id)
    try:
        filename = _safe_filename(filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    directory = get_knowledge_dir(user_id, project_id)
    file_path = scoped_knowledge_file(directory, os.path.join(directory, filename))
    try:
        with _index_write_section(user_id, project_id), project_access(user_id, project_id) as current:
            _ensure_project(current)
            _ensure_no_uploads(user_id, project_id)
            if not os.path.isfile(file_path):
                raise HTTPException(status_code=404, detail="文件不存在")
            os.remove(file_path)
            _invalidate_health(user_id, project_id)
            get_vector_store(user_id, project_id).delete_file_index(file_path)
        return {"message": f"已删除: {filename}"}
    except HTTPException:
        raise
    except TimeoutError:
        raise HTTPException(status_code=503, detail="索引正被其他操作占用，请稍后重试")
    except Exception:
        logger.exception("[知识库] 删除失败 file=%s project_id=%s", filename, project_id)
        detail = "删除文件失败，请稍后重试" if os.path.exists(file_path) else "文件已删除，但索引清理失败，请执行索引修复"
        raise HTTPException(status_code=500, detail=detail)


@app.post("/api/projects/{project_id}/vector/build")
def build_knowledge_index(project: dict = Depends(require_project)):
    user_id, project_id = project["user_id"], project["id"]
    _ensure_no_uploads(user_id, project_id)
    try:
        with _index_write_section(user_id, project_id), project_access(user_id, project_id) as current:
            _ensure_project(current)
            _ensure_no_uploads(user_id, project_id)
            failures = get_vector_store(user_id, project_id).index_pending_files()
            _invalidate_health(user_id, project_id)
        # API只暴露用户可理解的文件名，绝对存储路径保留在后端。
        failures = [{**item, "file": os.path.basename(item["file"])} for item in failures]
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
        snapshot = _health_cache(user_id, project_id).peek()
        return snapshot["report"] if snapshot is not None else None
    snapshot = await _request_health_snapshot(request, user_id, project_id, refresh)
    return snapshot["report"]


@app.post("/api/projects/{project_id}/index/health/repair")
def repair_index_health(project: dict = Depends(require_project)):
    user_id, project_id = project["user_id"], project["id"]
    _ensure_no_uploads(user_id, project_id)
    try:
        with _index_write_section(user_id, project_id), project_access(user_id, project_id) as current:
            _ensure_project(current)
            _ensure_no_uploads(user_id, project_id)
            report = IndexHealth(user_id, project_id).repair()
            _health_cache(user_id, project_id).set({"report": report})
        return report
    except HTTPException:
        raise
    except TimeoutError:
        raise HTTPException(status_code=503, detail="索引正被其他操作占用，请稍后重试")
    except Exception:
        logger.exception("[索引健康检查] 修复失败 project_id=%s", project_id)
        raise HTTPException(status_code=500, detail="修复失败，请稍后重试")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "api_server:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        workers=UVICORN_WORKERS,
    )
