"""FastAPI 后端，为 Vue 前端提供对话与知识库 API，含账户系统。"""
import os
import json
import time
import uuid
import asyncio
import itertools
import threading
from contextlib import asynccontextmanager
from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Header, Request

import anyio
import psycopg
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from main_deep_agent import MainDeepAgent, PG_CHECKPOINT_POOL_MAX
from rag.vector_store import get_vector_store
from rag.index_health import IndexHealth
from utils.file_tool import list_files_with_allowed_type, get_file_md5_hex, read_md5_set
from utils.config_loader import vector_db_config
from utils.path_tool import get_abs_path
from utils.pg_lock import SingleInstanceGuard, exclusive_section, shared_section
from auth import (
    PG_CONN,
    PG_POOL_MAX,
    init_db,
    register,
    login,
    check_rate,
    verify_token,
    get_user_cached,
    list_conversations,
    create_conversation,
    update_conversation_title,
    append_message,
    delete_conversation,
)

# 流式回答也占用线程池；该值应覆盖预期的同时在线流数。
THREADPOOL_SIZE = int(os.getenv("THREADPOOL_SIZE", "200"))
UVICORN_WORKERS = int(os.getenv("UVICORN_WORKERS", "1"))
ENABLE_API_DOCS = os.getenv("ENABLE_API_DOCS", "false").lower() in ("1", "true", "yes")

CHAT_MESSAGE_MAX_CHARS = int(os.getenv("CHAT_MESSAGE_MAX_CHARS", "8000"))
CHAT_THREAD_ID_MAX_CHARS = 128
CONV_TITLE_MAX_CHARS = 255   # 与 conversations.title VARCHAR(255) 对齐
MESSAGE_STATUS_VALUES = ("streaming", "completed", "error")

CHAT_RATE_LIMIT = int(os.getenv("CHAT_RATE_LIMIT", "30"))
CHAT_RATE_WINDOW_SECONDS = int(os.getenv("CHAT_RATE_WINDOW_SECONDS", "60"))
MAX_CONCURRENT_CHAT_PER_USER = int(os.getenv("MAX_CONCURRENT_CHAT_PER_USER", "3"))
CHAT_SLOT_MAX_AGE_SECONDS = int(os.getenv("CHAT_SLOT_MAX_AGE_SECONDS", "1800"))



def _warn_connection_budget() -> None:
    """启动时核算 PG 连接需求，超出上限时明确告警（多 worker 时最容易踩）。"""
    per_worker = PG_CHECKPOINT_POOL_MAX + PG_POOL_MAX
    need = UVICORN_WORKERS * per_worker
    try:
        with psycopg.connect(PG_CONN) as conn, conn.cursor() as cur:
            cur.execute("SHOW max_connections")
            max_conn = int(cur.fetchone()[0])
    except Exception as e:
        print(f"[启动] 警告：读取 PG max_connections 失败，跳过连接预算核算: {e}")
        return
    detail = f"workers={UVICORN_WORKERS} × (检查点池 {PG_CHECKPOINT_POOL_MAX} + 业务池 {PG_POOL_MAX}) = {need}"
    if need > max_conn - 5:
        state = "超出" if need > max_conn else "接近"
        print(f"[启动] 警告：PG 连接需求约 {need}，已{state} max_connections={max_conn}（{detail}）；"
              f"请调大 max_connections 或调小池上限，否则高并发时会等待连接")
    else:
        print(f"[启动] PG 连接预算充足：需求约 {need}，上限 {max_conn}（{detail}）")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    anyio.to_thread.current_default_thread_limiter().total_tokens = THREADPOOL_SIZE
    print(f"[启动] 线程池上限 {THREADPOOL_SIZE}，工作进程数 {UVICORN_WORKERS}")
    _warn_connection_budget()
    task = asyncio.create_task(_scheduled_health_loop())
    yield
    task.cancel()


app = FastAPI(
    title="浅问深答",
    lifespan=lifespan,
    docs_url="/docs" if ENABLE_API_DOCS else None,
    redoc_url="/redoc" if ENABLE_API_DOCS else None,
    openapi_url="/openapi.json" if ENABLE_API_DOCS else None,
)

agent = MainDeepAgent()
vector_store = get_vector_store()
index_health = IndexHealth()

knowledge_dir = get_abs_path(vector_db_config["knowledge_dir"])
allowed_types = vector_db_config["allowed_types"]

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "50"))

init_db()



_health_report: dict | None = None
_health_report_at: float = 0.0

HEALTH_REPORT_TTL_SECONDS = int(os.getenv("HEALTH_REPORT_TTL_SECONDS", "120"))


def _set_health_report(report: dict) -> None:
    """更新进程内报告缓存并刷新时间戳。"""
    global _health_report, _health_report_at
    _health_report = report
    _health_report_at = time.monotonic()


_PG_LOCK_KEY_HEALTH = 0x64656570
_HEALTH_LEADER_POLL_SECONDS = 60

# 进程内锁无法协调多 worker，所有索引写操作还要持有这把 PG 锁。
_PG_LOCK_KEY_INDEX_WRITE = 0x64656572

INDEX_WRITE_LOCK_TIMEOUT_SECONDS = int(os.getenv("INDEX_WRITE_LOCK_TIMEOUT_SECONDS", "300"))

INDEX_READ_LOCK_TIMEOUT_SECONDS = int(os.getenv("INDEX_READ_LOCK_TIMEOUT_SECONDS", "30"))


def _index_write_section():
    """索引写操作的跨进程互斥段（写入方使用，带等待上限）。"""
    return exclusive_section(PG_CONN, _PG_LOCK_KEY_INDEX_WRITE, INDEX_WRITE_LOCK_TIMEOUT_SECONDS)


def _replace_and_index(tmp_path: str, dest: str, overwritten: bool) -> dict:
    """原子替换 + 入库，整段持有索引写锁。"""
    with _index_write_section():
        if overwritten:
            vector_store.delete_file(dest)
        os.replace(tmp_path, dest)
        return vector_store.index_file(dest)


def _format_repair_summary(repair: dict) -> str:
    """格式化修复摘要，供定时自愈与手动修复共用同一套表述。"""
    record_state = "已重建" if repair.get("md5_rebuilt") else "未变更"
    return (
        f"清理残留向量 {len(repair.get('ghost_deleted', []))} 个"
        f"（失败 {len(repair.get('ghost_errors', []))} 个），"
        f"入库记录{record_state}，重索引 {len(repair.get('re_indexed', []))} 个"
        f"（失败 {len(repair.get('re_index_failures', []))} 个）"
    )


def _auto_heal_report() -> dict:
    """索引自愈闭环：自检发现问题 → 自动修复 → 复查，直至自检通过或轮次耗尽。

    停止条件：report['healthy'] 为 True。Milvus 不可达或修复异常时立即终止，
    等待下一轮自检再试。
    """
    with _index_write_section():
        max_rounds = int(vector_db_config.get("health_auto_repair_max_rounds", 3))
        report = index_health.check()
        for _ in range(max_rounds):
            if report.get("healthy"):
                break
            if report.get("milvus_error"):
                print("[索引自愈] 无法连接 Milvus，跳过本轮修复")
                break
            try:
                report = index_health.repair()
                repair = report.get("repair") or {}
                print(f"[索引自愈] 修复完成：{_format_repair_summary(repair)}")
            except Exception as e:
                print(f"[索引自愈] 修复失败: {e}")
                break
        return report


async def _scheduled_health_loop():
    """定时自检 + 自愈。

    多 worker/多实例下必须只由一个进程执行，否则会重复修复、重复触发嵌入调用；
    用 PG 会话级咨询锁选出唯一执行进程：只有获得锁的进程执行，持有者退出后其他进程自动接管。
    """
    interval = max(int(vector_db_config.get("health_check_interval_hours", 6) * 3600), 600)
    guard = SingleInstanceGuard(PG_CONN, _PG_LOCK_KEY_HEALTH)
    next_run = 0.0
    was_leader = False
    while True:
        is_leader = await asyncio.to_thread(guard.try_acquire)
        if is_leader != was_leader:
            print(
                "[索引自检] 本进程已成为定时自检/自愈执行者" if is_leader
                else "[索引自检] 已失去执行者身份（连接中断或锁被接管），暂停定时自检/自愈"
            )
            was_leader = is_leader
        if not is_leader or time.monotonic() < next_run:
            await asyncio.sleep(_HEALTH_LEADER_POLL_SECONDS)
            continue
        try:
            report = await asyncio.to_thread(_auto_heal_report)
            _set_health_report(report)
            state = "健康" if report["healthy"] else f"发现 {len(report['issues'])} 类问题"
            print(f"[索引自检] 完成：{state}（文件 {report['stats']['files']}，向量块 {report['stats']['chunks']}）")
        except Exception as e:
            print(f"[索引自检] 执行失败: {e}")
        next_run = time.monotonic() + interval



class ChatRequest(BaseModel):
    message: str = Field(max_length=CHAT_MESSAGE_MAX_CHARS)
    thread_id: str = Field(max_length=CHAT_THREAD_ID_MAX_CHARS)
    kb_only: bool = True


class RegisterRequest(BaseModel):
    username: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str


class CreateConversationRequest(BaseModel):
    title: str = Field(default="新对话", max_length=CONV_TITLE_MAX_CHARS)


class UpdateTitleRequest(BaseModel):
    title: str = Field(max_length=CONV_TITLE_MAX_CHARS)


class AppendMessageRequest(BaseModel):
    role: str
    content: str
    status: str | None = None



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



@app.get("/api/health")
def health():
    return {"status": "ok"}



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
def api_list_conversations(user: dict = Depends(get_current_user)):
    return {"conversations": list_conversations(user["user_id"])}


@app.post("/api/conversations")
def api_create_conversation(req: CreateConversationRequest, user: dict = Depends(get_current_user)):
    conv_id = str(uuid.uuid4())
    if not create_conversation(user["user_id"], conv_id, req.title):
        raise HTTPException(status_code=500, detail="创建会话失败，请稍后重试")
    return {"id": conv_id, "title": req.title, "messages": []}


@app.patch("/api/conversations/{conv_id}")
def api_update_title(conv_id: str, req: UpdateTitleRequest, user: dict = Depends(get_current_user)):
    if not update_conversation_title(user["user_id"], conv_id, req.title):
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"message": "ok"}


@app.post("/api/conversations/{conv_id}/messages")
def api_append_message(conv_id: str, req: AppendMessageRequest, user: dict = Depends(get_current_user)):
    if req.role not in ("user", "assistant"):
        raise HTTPException(status_code=400, detail="非法的消息角色")
    if req.status is not None and req.status not in MESSAGE_STATUS_VALUES:
        raise HTTPException(status_code=400, detail="非法的消息状态")
    if not append_message(user["user_id"], conv_id, req.role, req.content, req.status):
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"message": "ok"}


@app.delete("/api/conversations/{conv_id}")
def api_delete_conversation(conv_id: str, user: dict = Depends(get_current_user)):
    thread_id = f"{user['user_id']}:{conv_id}"
    last_err = None
    for attempt in range(3):
        try:
            agent.checkpointer.delete_thread(thread_id)
            last_err = None
            break
        except Exception as e:
            last_err = e
            print(f"[会话] 清理检查点失败 thread={thread_id}（第 {attempt + 1} 次尝试）: {e}")
            time.sleep(0.5)
    if last_err is not None:
        raise HTTPException(status_code=500, detail="清理会话上下文失败，请稍后重试")
    if not delete_conversation(user["user_id"], conv_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"message": "ok"}



_chat_slots: dict[int, dict[int, float]] = {}
_chat_slot_seq = itertools.count(1)
_chat_slots_lock = threading.Lock()


def _acquire_chat_slot(user_id: int) -> int | None:
    """占用一个对话并发槽，返回槽位 token；已达上限返回 None。

    顺带清理超过 CHAT_SLOT_MAX_AGE_SECONDS 的槽位，作为兜底：正常路径下槽位由
    流式生成器的 finally 释放（客户端中止时异步生成器会被 Starlette 取消并必定
    走到 finally）；该超时只用于极端情况，避免槽位泄漏后该用户被长期限流。
    """
    now = time.monotonic()
    with _chat_slots_lock:
        slots = {
            token: started for token, started in _chat_slots.get(user_id, {}).items()
            if now - started < CHAT_SLOT_MAX_AGE_SECONDS
        }
        if len(slots) >= MAX_CONCURRENT_CHAT_PER_USER:
            _chat_slots[user_id] = slots
            return None
        token = next(_chat_slot_seq)
        slots[token] = now
        _chat_slots[user_id] = slots
        return token


def _release_chat_slot(user_id: int, token: int) -> None:
    with _chat_slots_lock:
        slots = _chat_slots.get(user_id)
        if not slots:
            return
        slots.pop(token, None)
        if not slots:
            _chat_slots.pop(user_id, None)


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest, user: dict = Depends(get_current_user)):
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="消息不能为空")
    if not await asyncio.to_thread(
        check_rate, "chat", str(user["user_id"]), CHAT_RATE_LIMIT, CHAT_RATE_WINDOW_SECONDS
    ):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    slot = _acquire_chat_slot(user["user_id"])
    if slot is None:
        raise HTTPException(status_code=429, detail="并发回答数过多，请等待当前回答完成")

    async def generate():
        stream = agent.ask_deep_agent(req.message, f"{user['user_id']}:{req.thread_id}", kb_only=req.kb_only)
        sentinel = object()

        def _next_chunk():
            try:
                return next(stream)
            except StopIteration:
                return sentinel

        try:
            while True:
                chunk = await anyio.to_thread.run_sync(_next_chunk)
                if chunk is sentinel:
                    break
                yield f"data: {json.dumps({'content': chunk}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'done': True}, ensure_ascii=False)}\n\n"
        except Exception as e:
            print(f"[对话流] 回答失败 thread={user['user_id']}:{req.thread_id}: {e}")
            yield f"data: {json.dumps({'error': '服务处理异常，请稍后重试'}, ensure_ascii=False)}\n\n"
        finally:
            try:
                stream.close()
            except ValueError:
                pass
            except Exception as e:
                print(f"[对话流] 关闭上游生成器失败（已忽略）: {e}")
            _release_chat_slot(user["user_id"], slot)

    return StreamingResponse(generate(), media_type="text/event-stream")



@app.get("/api/files")
def list_files(user: dict = Depends(require_admin)):
    os.makedirs(knowledge_dir, exist_ok=True)
    files = list_files_with_allowed_type(knowledge_dir, tuple(allowed_types))
    indexed_set = read_md5_set()
    items = []
    for f in files:
        md5_hex = get_file_md5_hex(f)
        indexed = bool(md5_hex) and md5_hex in indexed_set
        items.append({"name": os.path.basename(f), "path": f, "indexed": indexed})
    return {"files": items}


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
            overwritten = os.path.exists(dest)
            tmp_path = os.path.join(knowledge_dir, f".tmp_{uuid.uuid4().hex}")
            try:
                await _write_upload_to(f, tmp_path)
                index_result = await asyncio.to_thread(_replace_and_index, tmp_path, dest, overwritten)
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            if not index_result["ok"]:
                print(f"[知识库] 入库失败 name={name}: {index_result['error']}")
            uploaded.append({
                "name": name,
                "overwritten": overwritten,
                "indexed": index_result["ok"],
                "error": None if index_result["ok"] else index_result["error"],
            })
        except TimeoutError as e:
            print(f"[知识库] 上传失败 name={name}: 等待索引写锁超时（{e}）")
            errors.append({"name": name, "error": "索引正被其他操作占用，请稍后重试"})
        except Exception as e:
            print(f"[知识库] 上传失败 name={name}: {e}")
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
            md5_hex = get_file_md5_hex(file_path)
            os.remove(file_path)
            vector_store.delete_file(file_path, md5_hex)
        return {"message": f"已删除: {safe_name}"}
    except TimeoutError as e:
        print(f"[知识库] 删除失败 name={safe_name}: 等待索引写锁超时（{e}）")
        raise HTTPException(status_code=503, detail="索引正被其他操作占用，请稍后重试")
    except Exception as e:
        if os.path.exists(file_path):
            print(f"[知识库] 删除失败 name={safe_name}: 磁盘文件未删除，向量与入库记录未改动（{e}）")
            raise HTTPException(status_code=500, detail="删除文件失败，请确认文件未被其他程序占用后重试")
        print(f"[知识库] 删除后清理失败 name={safe_name}: 磁盘文件已删除，向量与入库记录未清理（{e}），需执行索引修复")
        raise HTTPException(status_code=500, detail="文件已删除，但向量/入库记录清理失败，请稍后执行一次「修复」")


@app.post("/api/vector/build")
def build_vector(user: dict = Depends(require_admin)):
    try:
        with _index_write_section():
            failures = vector_store.do_store()
    except TimeoutError as e:
        print(f"[知识库] 构建失败: 等待索引写锁超时（{e}）")
        raise HTTPException(status_code=503, detail="索引正被其他操作占用，请稍后重试")
    except Exception as e:
        print(f"[知识库] 构建失败: {e}")
        raise HTTPException(status_code=500, detail="构建失败，请稍后重试")
    if failures:
        print(f"[知识库] 构建完成：{len(failures)} 个文件入库失败")
        return {
            "message": f"构建完成，但有 {len(failures)} 个文件失败",
            "failures": failures,
        }
    print("[知识库] 构建完成：无失败文件")
    return {"message": "构建完成"}


@app.get("/api/index/health")
def get_index_health(refresh: bool = False, user: dict = Depends(require_admin)):
    """返回最近一次自检报告；refresh=1 时立即执行一次全新检查。"""
    if refresh or _health_report is None or time.monotonic() - _health_report_at > HEALTH_REPORT_TTL_SECONDS:
        try:
            with shared_section(PG_CONN, _PG_LOCK_KEY_INDEX_WRITE, INDEX_READ_LOCK_TIMEOUT_SECONDS):
                _set_health_report(index_health.check())
        except TimeoutError as e:
            print(f"[索引健康] 自检失败: 等待索引写锁超时（{e}）")
            raise HTTPException(status_code=503, detail="索引正被其他操作占用，请稍后重试")
        except Exception as e:
            print(f"[索引健康] 自检失败: {e}")
            raise HTTPException(status_code=500, detail="自检失败，请稍后重试")
    return _health_report


@app.post("/api/index/health/repair")
def repair_index_health(user: dict = Depends(require_admin)):
    """手动触发一致性修复：清理残留向量、重建入库记录、重索引向量丢失的文件。"""
    try:
        with _index_write_section():
            report = index_health.repair()
            _set_health_report(report)
    except TimeoutError as e:
        print(f"[索引健康] 修复失败: 等待索引写锁超时（{e}）")
        raise HTTPException(status_code=503, detail="索引正被其他操作占用，请稍后重试")
    except Exception as e:
        print(f"[索引健康] 修复失败: {e}")
        raise HTTPException(status_code=500, detail="修复失败，请稍后重试")
    print(f"[索引健康] 修复完成：{_format_repair_summary(report.get('repair') or {})}")
    return _health_report


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "api_server:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        workers=UVICORN_WORKERS,
    )
