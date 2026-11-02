"""FastAPI 入口：账户、流式对话和知识库管理 API。"""
import os
import json
import time
import uuid
import asyncio
import threading
from functools import partial
from collections.abc import Callable
from contextlib import asynccontextmanager
from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Header, Request, Query

import anyio
import psycopg
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from chat_agent import ChatAgent, PG_CHECKPOINT_POOL_MAX
from rag.vector_store import get_vector_store
from rag.index_health import IndexHealth
from utils.config_loader import vector_db_config
from utils.project_paths import resolve_project_path
from utils.pg_lock import SingleInstanceGuard, exclusive_section, shared_section
from utils.generation_runner import run_generation
from utils.health_report_cache import HealthReportCache
from utils.logging_config import configure_logging, get_logger
from auth import (
    PASSWORD_MAX_LENGTH,
    PG_CONN,
    PG_POOL_MAX,
    init_db,
    register,
    login,
    consume_rate_limit,
    verify_token,
    get_user_cached,
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
)

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
CHAT_GENERATION_SAVE_SECONDS = max(float(os.getenv("CHAT_GENERATION_SAVE_SECONDS", "0.5")), 0.1)
CHAT_GENERATION_STALE_SECONDS = max(
    int(os.getenv("CHAT_GENERATION_STALE_SECONDS", "120")),
    CHAT_GENERATION_SAVE_SECONDS * 3,
)

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

# 就绪检查的依赖探测超时（秒）：PG 建连与 Milvus RPC 都受此限制，避免探针长期占用线程池。
READINESS_TIMEOUT_SECONDS = max(float(os.getenv("READINESS_TIMEOUT_SECONDS", "3")), 0.5)



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
    anyio.to_thread.current_default_thread_limiter().total_tokens = THREADPOOL_SIZE
    logger.info(f"[启动] AnyIO 默认线程池并发额度为 {THREADPOOL_SIZE}，工作进程数为 {UVICORN_WORKERS}")
    _warn_connection_budget()
    task = asyncio.create_task(_scheduled_health_loop())
    try:
        yield
    finally:
        task.cancel()
        for generation_task in list(_generation_tasks):
            generation_task.cancel()
        await asyncio.gather(task, *_generation_tasks, return_exceptions=True)


app = FastAPI(
    title="浅问深答",
    lifespan=lifespan,
    docs_url="/docs" if ENABLE_API_DOCS else None,
    redoc_url="/redoc" if ENABLE_API_DOCS else None,
    openapi_url="/openapi.json" if ENABLE_API_DOCS else None,
)

init_db()

agent = ChatAgent()
vector_store = get_vector_store()
index_health = IndexHealth()

knowledge_dir = resolve_project_path(vector_db_config["knowledge_dir"])
allowed_types = vector_db_config["allowed_types"]

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "50"))

HEALTH_REPORT_TTL_SECONDS = int(os.getenv("HEALTH_REPORT_TTL_SECONDS", "120"))
_health_reports = HealthReportCache(HEALTH_REPORT_TTL_SECONDS)


def _set_health_report(report: dict) -> None:
    """更新进程内报告缓存并刷新时间戳。"""
    _health_reports.set(report)


_PG_LOCK_KEY_HEALTH = 0x64656570
_HEALTH_LEADER_POLL_SECONDS = 60

# 进程内锁无法协调多 worker，所有索引写操作还要持有这把 PG 锁。
_PG_LOCK_KEY_INDEX_WRITE = 0x64656572

INDEX_WRITE_LOCK_TIMEOUT_SECONDS = int(os.getenv("INDEX_WRITE_LOCK_TIMEOUT_SECONDS", "300"))

INDEX_READ_LOCK_TIMEOUT_SECONDS = int(os.getenv("INDEX_READ_LOCK_TIMEOUT_SECONDS", "30"))


def _index_write_section():
    """索引写操作的跨进程互斥段（写入方使用，带等待上限）。"""
    return exclusive_section(PG_CONN, _PG_LOCK_KEY_INDEX_WRITE, INDEX_WRITE_LOCK_TIMEOUT_SECONDS)


def _replace_and_index(tmp_path: str, dest: str) -> dict:
    """原子替换 + 入库，整段持有索引写锁。

    "是否存在同名文件"与替换、入库在同一互斥段内判定，并发上传同名文件时
    后进入者能看到前一个请求的落地结果，不会把覆盖误判成新建。
    新版本入库并校验通过后才清理旧向量；失败则回滚磁盘文件，旧索引保持可用。
    """
    with _index_write_section():
        return vector_store.replace_and_index_file(dest, tmp_path)


def _format_repair_summary(repair: dict) -> str:
    """格式化修复摘要，供定时自动修复和手动修复共用同一套表述。"""
    record_state = "已更新" if repair.get("manifest_rebuilt") else "未变更"
    return (
        f"清理残留向量 {len(repair.get('ghost_deleted', []))} 个"
        f"（失败 {len(repair.get('ghost_errors', []))} 个），"
        f"索引记录{record_state}，重索引 {len(repair.get('re_indexed', []))} 个"
        f"（失败 {len(repair.get('re_index_failures', []))} 个）"
    )


def _auto_heal_report() -> dict:
    """索引自动修复闭环：健康检查发现问题 → 自动修复 → 复查，直至检查通过或达到轮次上限。

    停止条件：report['healthy'] 为 True。Milvus 不可达或修复异常时立即终止，
    等待下一轮健康检查再试。
    """
    with _index_write_section():
        max_rounds = int(vector_db_config.get("health_auto_repair_max_rounds", 3))
        report = index_health.check()
        for _ in range(max_rounds):
            if report.get("healthy"):
                break
            if report.get("milvus_error") or report.get("manifest_error"):
                logger.warning("[索引自动修复] 存储不可用，本轮暂不执行修复")
                break
            try:
                report = index_health.repair()
                repair = report.get("repair") or {}
                logger.info(f"[索引自动修复] 本轮修复完成：{_format_repair_summary(repair)}")
            except Exception as e:
                logger.error(f"[索引自动修复] 执行失败：{e}")
                break
        return report


async def _scheduled_health_loop():
    """定时健康检查和自动修复。

    多 worker/多实例下必须只由一个进程执行，否则会重复修复、重复触发嵌入调用；
    使用 PostgreSQL 会话级咨询锁（advisory lock）选出唯一执行进程；持有者退出后，其他进程自动接管。
    """
    interval = max(int(vector_db_config.get("health_check_interval_hours", 6) * 3600), 600)
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
            report = await asyncio.to_thread(_auto_heal_report)
            _set_health_report(report)
            state = "健康" if report["healthy"] else f"发现 {len(report['issues'])} 类问题"
            logger.info(f"[索引健康检查] 检查完成：{state}（文件 {report['stats']['files']} 个，向量块 {report['stats']['chunks']} 个）")
        except Exception as e:
            logger.error(f"[索引健康检查] 执行失败：{e}")
        next_run = time.monotonic() + interval



class ChatRequest(BaseModel):
    message: str = Field(max_length=CHAT_MESSAGE_MAX_CHARS)
    thread_id: str = Field(max_length=CHAT_THREAD_ID_MAX_CHARS)
    # 客户端生成的幂等键：同一会话内重复提交不会重复写消息或重复生成
    request_id: str = Field(pattern=r"^[0-9a-zA-Z_-]{8,64}$")
    kb_only: bool = True


class RegisterRequest(BaseModel):
    username: str
    password: str = Field(max_length=PASSWORD_MAX_LENGTH)


class LoginRequest(BaseModel):
    username: str
    password: str = Field(max_length=PASSWORD_MAX_LENGTH)


class CreateConversationRequest(BaseModel):
    title: str = Field(default="新对话", max_length=CONV_TITLE_MAX_CHARS)


class UpdateTitleRequest(BaseModel):
    title: str = Field(max_length=CONV_TITLE_MAX_CHARS)



def get_current_user(authorization: str = Header(None)) -> dict:
    """从 Authorization header 中解析 JWT，验签后复核账号存续与角色。

    复核结果带短 TTL 缓存（USER_CACHE_TTL_SECONDS），避免每个请求都查库；
    代价是删号/降权的最长生效延迟等于该 TTL。
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未登录")
    token = authorization[7:]
    payload = verify_token(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    user_id = payload.get("user_id")
    if user_id is None:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    user = get_user_cached(user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="账号不存在或已被注销")
    return {"user_id": user["id"], "username": user["username"], "role": user["role"]}


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """仅允许管理员访问。"""
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可操作")
    return user



def _probe_postgres() -> dict:
    """独立建连执行 SELECT 1，不占用业务连接池。"""
    started = time.perf_counter()
    try:
        with psycopg.connect(
            PG_CONN, connect_timeout=max(int(READINESS_TIMEOUT_SECONDS), 1)
        ) as conn:
            conn.execute("SELECT 1")
        return {"ok": True, "latency_ms": round((time.perf_counter() - started) * 1000, 1)}
    except Exception as e:
        logger.error(f"[就绪检查] PostgreSQL 不可用：{e}")
        return {
            "ok": False,
            "error": "数据库不可用",
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }


def _probe_milvus() -> dict:
    """复用检索实际使用的 Milvus 连接做一次轻量调用。"""
    started = time.perf_counter()
    try:
        vector_store.vector_store.client.list_collections(timeout=READINESS_TIMEOUT_SECONDS)
        return {"ok": True, "latency_ms": round((time.perf_counter() - started) * 1000, 1)}
    except Exception as e:
        logger.error(f"[就绪检查] Milvus 不可用：{e}")
        return {
            "ok": False,
            "error": "向量库不可用",
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }


@app.get("/api/health")
def health():
    """存活检查：只证明进程能响应请求，不探测任何外部依赖。"""
    return {"status": "ok"}


@app.get("/api/health/ready")
def readiness():
    """就绪检查：探测 PostgreSQL 与 Milvus，任一不可用返回 503。"""
    checks = {"postgres": _probe_postgres(), "milvus": _probe_milvus()}
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
    return {"user": {"id": user["user_id"], "username": user["username"], "role": user["role"]}}



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
    """删除会话对应的 LangGraph 检查点，最多重试 3 次；失败抛异常触发事务回滚。"""
    for attempt in range(3):
        try:
            agent.checkpointer.delete_thread(thread_id)
            return
        except Exception as e:
            logger.error(f"[会话] 清理检查点失败 thread_id={thread_id}（第 {attempt + 1} 次尝试）：{e}")
            if attempt < 2:
                time.sleep(0.5)
    raise RuntimeError("清理会话上下文失败")


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


async def _run_generation(req: ChatRequest, user_id: int, generation_id: int, slot: str):
    try:
        await run_generation(
            lambda should_stop: agent.stream_answer(
                req.message, f"{user_id}:{req.thread_id}", kb_only=req.kb_only, should_stop=should_stop,
            ),
            partial(save_generation_progress, generation_id),
            partial(finish_generation, generation_id),
            deadline=CHAT_STREAM_DEADLINE_SECONDS,
            save_interval=CHAT_GENERATION_SAVE_SECONDS,
            on_bridge=lambda bridge: _bind_chat_slot_cancel(slot, bridge.stop),
        )
    finally:
        await _release_chat_slot(user_id, slot)


async def _generation_events(user_id: int, conv_id: str, request_id: str):
    previous = None
    while True:
        generation = await asyncio.to_thread(
            get_generation, user_id, conv_id, request_id, CHAT_GENERATION_STALE_SECONDS,
        )
        if generation is None:
            yield _sse({"type": "error", "status": "error", "error": "生成任务不存在"})
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
        raise HTTPException(status_code=404, detail="生成任务不存在")
    return StreamingResponse(_generation_events(user["user_id"], conv_id, request_id),
                             media_type="text/event-stream", headers=SSE_HEADERS)


@app.post("/api/conversations/{conv_id}/generations/{request_id}/cancel")
async def stop_generation(conv_id: str, request_id: str, user: dict = Depends(get_current_user)):
    if not await asyncio.to_thread(cancel_generation, user["user_id"], conv_id, request_id):
        raise HTTPException(status_code=404, detail="生成任务不存在")
    return {"message": "ok"}


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest, user: dict = Depends(get_current_user)):
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="消息不能为空")
    existing = await asyncio.to_thread(
        get_generation, user["user_id"], req.thread_id, req.request_id, CHAT_GENERATION_STALE_SECONDS,
    )
    if existing is not None:
        if existing["kb_only"] != req.kb_only:
            raise HTTPException(status_code=409, detail="请求编号已用于其他问答模式，请重新发送")
        # 幂等重放和恢复只订阅已有任务，不消耗新的并发槽位或发送额度。
        return StreamingResponse(_generation_events(user["user_id"], req.thread_id, req.request_id),
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

    if begun["status"] == "replay":
        await _release_chat_slot(user["user_id"], slot)
    else:
        # 在返回响应前启动独立任务；断开 SSE 只结束订阅。
        generation_task = asyncio.create_task(_run_generation(req, user["user_id"], begun["generation_id"], slot))
        _generation_tasks.add(generation_task)
        generation_task.add_done_callback(_generation_tasks.discard)
        try:
            await asyncio.to_thread(bind_chat_slot_generation, user["user_id"], slot, begun["generation_id"])
        except Exception:
            logger.exception("[对话流] 关联并发槽位与生成任务失败")

    return StreamingResponse(_generation_events(user["user_id"], req.thread_id, req.request_id),
                             media_type="text/event-stream", headers=SSE_HEADERS)



@app.get("/api/files")
def list_files(user: dict = Depends(require_admin)):
    try:
        with shared_section(PG_CONN, _PG_LOCK_KEY_INDEX_WRITE, INDEX_READ_LOCK_TIMEOUT_SECONDS):
            return {"files": index_health.file_statuses()}
    except TimeoutError:
        raise HTTPException(status_code=503, detail="索引正被其他操作占用，请稍后重试")
    except Exception:
        logger.exception("[知识库] 无法读取文件索引状态")
        raise HTTPException(status_code=503, detail="无法确认索引状态，请稍后重试")


async def _write_upload_to(f: UploadFile, tmp_path: str) -> int:
    """分块写入上传内容并累计大小，超过上限立即中断，避免整文件读入内存。"""
    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    size = 0
    with open(tmp_path, "wb") as fp:
        while chunk := await f.read(1024 * 1024):
            size += len(chunk)
            if size > max_bytes:
                raise ValueError(f"文件超过大小上限 {MAX_UPLOAD_MB} MB")
            fp.write(chunk)
    return size


@app.post("/api/files/upload")
async def upload_files(files: list[UploadFile] = File(...), user: dict = Depends(require_admin)):
    os.makedirs(knowledge_dir, exist_ok=True)
    uploaded = []
    errors = []
    for f in files:
        name = os.path.basename(f.filename or "")
        if not name or name.startswith("."):
            errors.append({"name": name or "(无名文件)", "error": "无效的文件名"})
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext not in allowed_types:
            errors.append({"name": name, "error": f"不支持的文件类型: {ext}"})
            continue
        try:
            dest = os.path.join(knowledge_dir, name)
            tmp_path = os.path.join(knowledge_dir, f".tmp_{uuid.uuid4().hex}")
            try:
                await _write_upload_to(f, tmp_path)
                index_result = await asyncio.to_thread(_replace_and_index, tmp_path, dest)
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            if not index_result["ok"]:
                logger.error(f"[知识库] 入库失败 file={name}：{index_result['error']}")
            uploaded.append({
                "name": name,
                "overwritten": index_result["overwritten"],
                "indexed": index_result["ok"],
                "error": None if index_result["ok"] else index_result["error"],
            })
        except TimeoutError as e:
            logger.error(f"[知识库] 上传失败 file={name}：等待索引写锁超时（{e}）")
            errors.append({"name": name, "error": "索引正被其他操作占用，请稍后重试"})
        except Exception as e:
            logger.error(f"[知识库] 上传失败 file={name}：{e}")
            errors.append({"name": name, "error": "上传失败，详见服务端日志"})
    return {"uploaded": uploaded, "errors": errors}


@app.delete("/api/files/{filename}")
def delete_file(filename: str, user: dict = Depends(require_admin)):
    safe_name = os.path.basename(filename)
    file_path = os.path.join(knowledge_dir, safe_name)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="文件不存在")
    try:
        with _index_write_section():
            os.remove(file_path)
            vector_store.delete_file_index(file_path)
        return {"message": f"已删除: {safe_name}"}
    except TimeoutError as e:
        logger.error(f"[知识库] 删除失败 file={safe_name}：等待索引写锁超时（{e}）")
        raise HTTPException(status_code=503, detail="索引正被其他操作占用，请稍后重试")
    except Exception as e:
        if os.path.exists(file_path):
            logger.error(f"[知识库] 删除失败 file={safe_name}：磁盘文件未删除，向量与索引记录未改动（{e}）")
            raise HTTPException(status_code=500, detail="删除文件失败，请确认文件未被其他程序占用后重试")
        logger.error(f"[知识库] 删除后清理失败 file={safe_name}：磁盘文件已删除，向量与索引记录未清理（{e}），需执行索引修复")
        raise HTTPException(status_code=500, detail="文件已删除，但向量/索引记录清理失败，请稍后执行一次「修复」")


# 保留路由名，以维持原有反向路由名称和 OpenAPI operationId。
@app.post("/api/vector/build", name="build_vector")
def build_knowledge_index(user: dict = Depends(require_admin)):
    """为尚未入库、分块损坏或版本过期的知识库内容构建索引。"""
    try:
        with _index_write_section():
            failures = vector_store.index_pending_files()
    except TimeoutError as e:
        logger.error(f"[知识库] 构建失败：等待索引写锁超时（{e}）")
        raise HTTPException(status_code=503, detail="索引正被其他操作占用，请稍后重试")
    except Exception as e:
        logger.error(f"[知识库] 构建失败：{e}")
        raise HTTPException(status_code=500, detail="构建失败，请稍后重试")
    if failures:
        logger.error(f"[知识库] 构建完成：{len(failures)} 个文件入库失败")
        return {
            "message": f"构建完成，但有 {len(failures)} 个文件失败",
            "failures": failures,
        }
    logger.info("[知识库] 构建完成，所有文件均已成功入库")
    return {"message": "构建完成"}


@app.get("/api/index/health")
def get_index_health(refresh: bool = False, user: dict = Depends(require_admin)):
    """返回最近一次健康检查报告；refresh=1 时立即执行一次检查。"""
    def check():
        with shared_section(PG_CONN, _PG_LOCK_KEY_INDEX_WRITE, INDEX_READ_LOCK_TIMEOUT_SECONDS):
            return index_health.check()

    try:
        return _health_reports.get(check, refresh=refresh)
    except TimeoutError as e:
        logger.error(f"[索引健康检查] 检查失败：等待索引写锁超时（{e}）")
        raise HTTPException(status_code=503, detail="索引正被其他操作占用，请稍后重试")
    except Exception as e:
        logger.error(f"[索引健康检查] 检查失败：{e}")
        raise HTTPException(status_code=500, detail="健康检查失败，请稍后重试")


@app.post("/api/index/health/repair")
def repair_index_health(user: dict = Depends(require_admin)):
    """清理残留索引，并从源文件重建缺失、损坏或陈旧的分块与清单。"""
    try:
        with _index_write_section():
            report = index_health.repair()
            _set_health_report(report)
    except TimeoutError as e:
        logger.error(f"[索引健康检查] 修复失败：等待索引写锁超时（{e}）")
        raise HTTPException(status_code=503, detail="索引正被其他操作占用，请稍后重试")
    except Exception as e:
        logger.error(f"[索引健康检查] 修复失败：{e}")
        raise HTTPException(status_code=500, detail="修复失败，请稍后重试")
    logger.info(f"[索引健康检查] 修复完成：{_format_repair_summary(report.get('repair') or {})}")
    return report


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "api_server:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        workers=UVICORN_WORKERS,
    )
