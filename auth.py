import hashlib
import os
import re
import random
import threading
import time
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from dotenv import load_dotenv

from utils.pg_lock import PG_LOCK_KEY_SETUP, exclusive_section

load_dotenv()

PG_CONN = os.getenv("PG_CONNECTION_STRING", "postgresql://postgres:postgres@localhost:5432/deepagent_rag")
PG_POOL_MIN = int(os.getenv("PG_POOL_MIN", "2"))
# 多 worker 部署时需把业务池与检查点池的总量控制在 PG 上限内。
PG_POOL_MAX = int(os.getenv("PG_POOL_MAX", "20"))
JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET:
    raise RuntimeError(
        "未配置 JWT_SECRET，拒绝启动。请在 .env 中写入随机长字符串，"
        '例如执行 python -c "import secrets; print(secrets.token_hex(32))" 生成'
    )
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "72"))

USERNAME_MAX_LENGTH = 50

LOGIN_ACCOUNT_RATE_LIMIT = int(os.getenv("LOGIN_ACCOUNT_RATE_LIMIT", "10"))
LOGIN_ACCOUNT_RATE_WINDOW_SECONDS = int(os.getenv("LOGIN_ACCOUNT_RATE_WINDOW_SECONDS", "60"))
LOGIN_IP_RATE_LIMIT = int(os.getenv("LOGIN_IP_RATE_LIMIT", "100"))
LOGIN_IP_RATE_WINDOW_SECONDS = int(os.getenv("LOGIN_IP_RATE_WINDOW_SECONDS", "60"))
LOGIN_ACCOUNT_IP_MAX_ATTEMPTS = int(os.getenv("LOGIN_ACCOUNT_IP_MAX_ATTEMPTS", "5"))
LOGIN_ACCOUNT_IP_WINDOW_SECONDS = int(os.getenv("LOGIN_ACCOUNT_IP_WINDOW_SECONDS", "900"))
USER_CACHE_TTL_SECONDS = int(os.getenv("USER_CACHE_TTL_SECONDS", "30"))
REGISTER_RATE_LIMIT = int(os.getenv("REGISTER_RATE_LIMIT", "5"))
REGISTER_RATE_WINDOW_SECONDS = int(os.getenv("REGISTER_RATE_WINDOW_SECONDS", "3600"))


_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        # 双重检查避免并发首次调用创建多个池。
        with _pool_lock:
            if _pool is None:
                _pool = ConnectionPool(
                    conninfo=PG_CONN,
                    min_size=PG_POOL_MIN,
                    max_size=PG_POOL_MAX,
                )
    return _pool


def _get_conn():
    return _get_pool().getconn()


def _put_conn(conn):
    if conn.info.transaction_status == TransactionStatus.INTRANS:
        conn.rollback()
    _get_pool().putconn(conn)


def init_db():
    """创建所需表（users、conversations、messages、rate_limits）。"""
    with exclusive_section(PG_CONN, PG_LOCK_KEY_SETUP):
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id          SERIAL PRIMARY KEY,
                        username    VARCHAR(50) UNIQUE NOT NULL,
                        password    VARCHAR(255) NOT NULL,
                        role        VARCHAR(10) NOT NULL DEFAULT 'user'
                            CHECK (role IN ('user', 'admin')),
                        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                cur.execute("ALTER TABLE users DROP COLUMN IF EXISTS locked_until")
                cur.execute("ALTER TABLE users DROP COLUMN IF EXISTS failed_attempts")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS conversations (
                        id          VARCHAR(36) PRIMARY KEY,
                        user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        title       VARCHAR(255) NOT NULL DEFAULT '新对话',
                        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS messages (
                        id              SERIAL PRIMARY KEY,
                        conversation_id VARCHAR(36) NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                        role            VARCHAR(10) NOT NULL CHECK (role IN ('user', 'assistant')),
                        content         TEXT NOT NULL,
                        status          VARCHAR(20),
                        created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS generation_id BIGINT")
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_messages_conversation "
                    "ON messages (conversation_id, id)"
                )
                # 生成任务：业务消息与模型上下文之外的第三份状态，记录每次生成的
                # 幂等键、归属、终态和关联消息，供重放、互斥与中断补偿使用。
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS generations (
                        id                  BIGSERIAL PRIMARY KEY,
                        conversation_id     VARCHAR(36) NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                        user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        request_id          VARCHAR(64) NOT NULL,
                        status              VARCHAR(16) NOT NULL DEFAULT 'running'
                            CHECK (status IN ('running', 'completed', 'failed', 'cancelled')),
                        cancel_requested    BOOLEAN NOT NULL DEFAULT FALSE,
                        error               TEXT,
                        user_message_id     BIGINT,
                        assistant_message_id BIGINT,
                        heartbeat_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE (conversation_id, request_id)
                    );
                """)
                cur.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_generations_running "
                    "ON generations (conversation_id) WHERE status = 'running'"
                )
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS rate_limits (
                        id           BIGSERIAL PRIMARY KEY,
                        scope        VARCHAR(20) NOT NULL,
                        key          VARCHAR(128) NOT NULL,
                        attempted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                cur.execute("ALTER TABLE rate_limits ALTER COLUMN key TYPE VARCHAR(128)")
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_rate_limits_lookup "
                    "ON rate_limits (scope, key, attempted_at)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_rate_limits_attempted_at "
                    "ON rate_limits (attempted_at)"
                )
            conn.commit()
        finally:
            _put_conn(conn)



_ph = PasswordHasher()


def _hash_password(password: str) -> str:
    """使用 Argon2id 生成密码哈希（自动加盐）。"""
    return _ph.hash(password)


def _verify_password(password: str, stored: str) -> bool:
    try:
        _ph.verify(stored, password)
        return True
    except VerificationError:
        return False


def _validate_password(password: str) -> str | None:
    """校验密码强度：至少 10 字符，含数字、英文字母、特殊符号。返回错误信息，通过则返回 None。"""
    problems = []
    if len(password) < 10:
        problems.append("至少10个字符")
    if not re.search(r"[0-9]", password):
        problems.append("数字")
    if not re.search(r"[a-zA-Z]", password):
        problems.append("英文字母")
    if not re.search(r"[^0-9a-zA-Z\s]", password):
        problems.append("特殊符号")
    if problems:
        return "密码需包含" + "、".join(problems)
    return None



def _create_token(user_id: int, username: str, role: str) -> str:
    payload = {
        "user_id": user_id,
        "username": username,
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def verify_token(token: str) -> dict | None:
    """验证 JWT，返回 payload 或 None。"""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


def get_user_by_id(user_id: int) -> dict | None:
    """按 id 查询用户（不缓存），返回 {'id', 'username', 'role'}；不存在返回 None。"""
    conn = _get_conn()
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, username, role FROM users WHERE id = %s",
                (user_id,),
            )
            return cur.fetchone()
    finally:
        _put_conn(conn)


_user_cache: dict[int, tuple[float, dict | None]] = {}
_user_cache_lock = threading.Lock()
_USER_CACHE_MAX_ENTRIES = 20000


def get_user_cached(user_id: int) -> dict | None:
    """带短 TTL 缓存的用户查询，供 JWT 验签后的账号复核使用。"""
    now = time.monotonic()
    with _user_cache_lock:
        cached = _user_cache.get(user_id)
        if cached is not None and now - cached[0] < USER_CACHE_TTL_SECONDS:
            return cached[1]
    user = get_user_by_id(user_id)
    with _user_cache_lock:
        if len(_user_cache) >= _USER_CACHE_MAX_ENTRIES:
            expired = [k for k, v in _user_cache.items() if now - v[0] >= USER_CACHE_TTL_SECONDS]
            if expired:
                for k in expired:
                    _user_cache.pop(k, None)
            else:
                _user_cache.clear()
        _user_cache[user_id] = (now, user)
    return user



_RATE_PURGE_PROBABILITY = 0.01
_RATE_RETENTION = timedelta(hours=2)


def _rate_exceeded(cur, scope: str, key: str, limit: int, window_seconds: int, now: datetime) -> bool:
    """在已有游标上判断 key 在窗口期内的记录数是否已达上限。"""
    cur.execute(
        "SELECT COUNT(*) AS cnt FROM rate_limits "
        "WHERE scope = %s AND key = %s AND attempted_at > %s",
        (scope, key, now - timedelta(seconds=window_seconds)),
    )
    return cur.fetchone()["cnt"] >= limit


def _rate_record(cur, scope: str, key: str, now: datetime) -> None:
    """在已有游标上记录一次尝试，并惰性清理过期行。"""
    cur.execute(
        "INSERT INTO rate_limits (scope, key, attempted_at) VALUES (%s, %s, %s)",
        (scope, key, now),
    )
    if random.random() < _RATE_PURGE_PROBABILITY:
        cur.execute("DELETE FROM rate_limits WHERE attempted_at <= %s", (now - _RATE_RETENTION,))


def check_rate(scope: str, key: str, limit: int, window_seconds: int) -> bool:
    """记录并检查 key 在窗口期内的尝试次数，超限返回 False。

    以数据库为计数载体：并发安全、多进程部署下同样生效，
    且不依赖长期驻留内存的字典。
    """
    conn = _get_conn()
    try:
        now = datetime.now()
        with conn.cursor(row_factory=dict_row) as cur:
            if _rate_exceeded(cur, scope, key, limit, window_seconds, now):
                return False
            _rate_record(cur, scope, key, now)
        conn.commit()
        return True
    finally:
        _put_conn(conn)



def register(username: str, password: str, ip: str | None = None) -> dict:
    """注册新用户（角色固定为 user）。返回 {'ok': True} 或 {'ok': False, 'error': ...}。"""
    username = username.strip()
    if not username or len(username) < 2:
        return {"ok": False, "error": "用户名至少 2 个字符"}
    if len(username) > USERNAME_MAX_LENGTH:
        return {"ok": False, "error": f"用户名最长 {USERNAME_MAX_LENGTH} 个字符"}
    err = _validate_password(password)
    if err:
        return {"ok": False, "error": err}

    if ip and not check_rate("register", ip, REGISTER_RATE_LIMIT, REGISTER_RATE_WINDOW_SECONDS):
        return {"ok": False, "error": "注册过于频繁，请稍后再试", "rate_limited": True}

    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (username, password, role) VALUES (%s, %s, %s) "
                "ON CONFLICT (username) DO NOTHING",
                (username, _hash_password(password), "user"),
            )
            created = cur.rowcount > 0
        conn.commit()
        if not created:
            return {"ok": False, "error": "用户名已存在"}
        return {"ok": True}
    finally:
        _put_conn(conn)


def login(username: str, password: str, ip: str | None = None) -> dict:
    """登录，返回 token 和用户信息。

    限流策略（目标：既不误伤共享出口的正常用户，也不让攻击者定向踢人）：
    - 账号级限流：按用户名计数，挡住针对单一账号的多 IP 爆破，与来源 IP 无关；
    - IP 级限流：阈值放宽，仅作粗粒度兜底，避免 NAT / 机房共享出口被整体拒绝；
    - 单 IP × 单账号失败冷却：只挡该来源对该账号的尝试，该账号在其他 IP 的正常登录不受影响。

    爆破防护完全由上述三层限流承担，不做账号级锁定：否则攻击者只要知道用户名、
    故意输错密码，就能把真实用户踢下线。
    """
    username = username.strip()
    if len(username) > USERNAME_MAX_LENGTH:
        return {"ok": False, "error": "用户名或密码错误"}
    now = datetime.now()
    acct_ip_key = f"{username}|{ip}" if ip else None

    conn = _get_conn()
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            checks = [
                ("login_acct", username, LOGIN_ACCOUNT_RATE_LIMIT, LOGIN_ACCOUNT_RATE_WINDOW_SECONDS),
            ]
            if ip:
                checks.append(("login_ip", ip, LOGIN_IP_RATE_LIMIT, LOGIN_IP_RATE_WINDOW_SECONDS))
            if acct_ip_key:
                checks.append(
                    ("login_fail_ip", acct_ip_key,
                     LOGIN_ACCOUNT_IP_MAX_ATTEMPTS, LOGIN_ACCOUNT_IP_WINDOW_SECONDS)
                )
            for scope, key, limit, window in checks:
                if _rate_exceeded(cur, scope, key, limit, window, now):
                    return {"ok": False, "error": "尝试过于频繁，请稍后再试", "rate_limited": True}

            _rate_record(cur, "login_acct", username, now)
            if ip:
                _rate_record(cur, "login_ip", ip, now)

            cur.execute(
                "SELECT id, username, password, role FROM users WHERE username = %s",
                (username,),
            )
            row = cur.fetchone()
            if not row:
                conn.commit()
                return {"ok": False, "error": "用户名或密码错误"}

            if not _verify_password(password, row["password"]):
                if acct_ip_key:
                    _rate_record(cur, "login_fail_ip", acct_ip_key, now)
                conn.commit()
                return {"ok": False, "error": "用户名或密码错误"}

            conn.commit()
            token = _create_token(row["id"], row["username"], row["role"])
            return {
                "ok": True,
                "token": token,
                "user": {
                    "id": row["id"],
                    "username": row["username"],
                    "role": row["role"],
                },
            }
    finally:
        _put_conn(conn)



def list_conversations(user_id: int) -> list[dict]:
    """返回用户所有会话（含消息），按最近更新倒序。"""
    conn = _get_conn()
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT c.id, c.title, m.id AS message_id, m.role, m.content, m.status
                FROM conversations c
                LEFT JOIN messages m ON m.conversation_id = c.id
                WHERE c.user_id = %s
                ORDER BY c.updated_at DESC, c.id DESC, m.id ASC
                """,
                (user_id,),
            )
            rows = cur.fetchall()
        convs: dict[str, dict] = {}
        order: list[str] = []
        for r in rows:
            cid = r["id"]
            if cid not in convs:
                convs[cid] = {"id": cid, "title": r["title"], "messages": []}
                order.append(cid)
            if r["role"] is not None:
                convs[cid]["messages"].append(
                    {
                        "id": r["message_id"],
                        "role": r["role"],
                        "content": r["content"],
                        "status": r["status"],
                    }
                )
        return [convs[cid] for cid in order]
    finally:
        _put_conn(conn)


def create_conversation(user_id: int, conv_id: str, title: str) -> bool:
    """创建会话；id 已存在（含属于他人的会话）时返回 False，不抛异常。"""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO conversations (id, user_id, title) VALUES (%s, %s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (conv_id, user_id, title),
            )
            created = cur.rowcount > 0
        conn.commit()
        return created
    finally:
        _put_conn(conn)


def update_conversation_title(user_id: int, conv_id: str, title: str) -> bool:
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE conversations SET title = %s, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = %s AND user_id = %s",
                (title, conv_id, user_id),
            )
            changed = cur.rowcount > 0
        conn.commit()
        return changed
    finally:
        _put_conn(conn)


def _conversation_lock_key(conv_id: str) -> int:
    """会话级咨询锁 key：让登记生成任务与删除会话共享同一把跨进程锁。"""
    digest = hashlib.blake2b(
        f"chat-generation:{conv_id}".encode("utf-8"), digest_size=8
    ).digest()
    return int.from_bytes(digest, "big") & 0x7FFFFFFFFFFFFFFF


_GENERATION_REPLAY_QUERY = """
    SELECT g.id, g.status, g.error, g.user_message_id, g.assistant_message_id,
           m.content AS assistant_content
    FROM generations g
    LEFT JOIN messages m ON m.id = g.assistant_message_id
    WHERE g.conversation_id = %s AND g.request_id = %s
"""


def _reap_stale_generations(cur, conv_id: str, stale_seconds: float) -> int:
    """将长时间未更新心跳时间戳的运行中生成任务置为 failed，并补写一条中断提示消息。

    进程异常退出或重启时，生成任务无法自行结束；心跳时间戳停止更新后，
    由后续请求在同一事务内执行补偿，避免 generations 中的任务长期停留在
    running 状态。
    """
    cur.execute(
        "SELECT id, assistant_message_id FROM generations "
        "WHERE conversation_id = %s AND status = 'running' "
        "AND heartbeat_at < now() - make_interval(secs => %s) "
        "FOR UPDATE",
        (conv_id, stale_seconds),
    )
    rows = cur.fetchall()
    for row in rows:
        message_id = row["assistant_message_id"]
        if message_id is None:
            cur.execute(
                "INSERT INTO messages (conversation_id, role, content, status, generation_id) "
                "VALUES (%s, 'assistant', %s, 'error', %s) RETURNING id",
                (conv_id, "（回答中断）", row["id"]),
            )
            message_id = cur.fetchone()["id"]
        cur.execute(
            "UPDATE generations SET status = 'failed', error = %s, "
            "assistant_message_id = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
            ("生成中断：服务异常或进程重启", message_id, row["id"]),
        )
    if rows:
        print(f"[会话] 已将 {len(rows)} 个超时未更新的生成任务标记为失败 conv={conv_id}")
    return len(rows)


def begin_generation(
    user_id: int,
    conv_id: str,
    request_id: str,
    content: str,
    stale_seconds: float,
) -> dict:
    """登记一次生成任务，并原子写入用户消息。

    同一会话同时只允许一个运行中的任务（由数据库部分唯一索引保证），
    同一 request_id 重复到达时返回既有任务，供调用方幂等重放。

    返回：
    - {"status": "started", "generation_id": ..., "user_message_id": ...}
    - {"status": "replay", "generation": {...}}
    - {"status": "busy"}
    - {"status": "not_found"}：会话不存在或不属于当前用户
    """
    conn = _get_conn()
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            # 与删除会话共用同一把事务级咨询锁，避免「登记—删除」互相穿插
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (_conversation_lock_key(conv_id),))
            cur.execute(
                "SELECT id FROM conversations WHERE id = %s AND user_id = %s FOR SHARE",
                (conv_id, user_id),
            )
            if cur.fetchone() is None:
                conn.rollback()
                return {"status": "not_found"}

            _reap_stale_generations(cur, conv_id, stale_seconds)

            cur.execute(_GENERATION_REPLAY_QUERY, (conv_id, request_id))
            existing = cur.fetchone()
            if existing is not None:
                conn.commit()
                return {"status": "replay", "generation": existing}

            try:
                cur.execute(
                    "INSERT INTO generations (conversation_id, user_id, request_id) "
                    "VALUES (%s, %s, %s) RETURNING id",
                    (conv_id, user_id, request_id),
                )
                generation_id = cur.fetchone()["id"]
            except psycopg.errors.UniqueViolation:
                # 并发登记：可能是其他请求占用了运行位，也可能是同一 request_id 同时到达
                conn.rollback()
                with conn.cursor(row_factory=dict_row) as cur2:
                    cur2.execute(_GENERATION_REPLAY_QUERY, (conv_id, request_id))
                    existing = cur2.fetchone()
                    cur2.execute(
                        "SELECT id FROM conversations WHERE id = %s AND user_id = %s",
                        (conv_id, user_id),
                    )
                    conv_exists = cur2.fetchone() is not None
                conn.commit()
                if existing is not None:
                    return {"status": "replay", "generation": existing}
                return {"status": "busy" if conv_exists else "not_found"}

            cur.execute(
                "INSERT INTO messages (conversation_id, role, content, generation_id) "
                "VALUES (%s, 'user', %s, %s) RETURNING id",
                (conv_id, content, generation_id),
            )
            user_message_id = cur.fetchone()["id"]
            cur.execute(
                "UPDATE generations SET user_message_id = %s WHERE id = %s",
                (user_message_id, generation_id),
            )
            cur.execute(
                "UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                (conv_id,),
            )
        conn.commit()
        return {
            "status": "started",
            "generation_id": generation_id,
            "user_message_id": user_message_id,
        }
    finally:
        _put_conn(conn)


def touch_generation(generation_id: int) -> dict | None:
    """更新生成任务的心跳时间戳；任务已结束或被取消时返回停止信号。"""
    conn = _get_conn()
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "UPDATE generations SET heartbeat_at = CURRENT_TIMESTAMP "
                "WHERE id = %s AND status = 'running' "
                "RETURNING cancel_requested",
                (generation_id,),
            )
            row = cur.fetchone()
        conn.commit()
        if row is None:
            return None
        return {"running": True, "cancel_requested": bool(row["cancel_requested"])}
    finally:
        _put_conn(conn)


def finish_generation(
    generation_id: int,
    status: str,
    message_status: str,
    content: str,
    error: str | None,
) -> int | None:
    """将生成任务置为终态并写入助手消息（终态：completed / failed / cancelled）。

    只有仍处于 running 的任务会被处理；已被补偿流程处理过的任务直接跳过，
    因此重复调用不会产生重复消息。
    """
    conn = _get_conn()
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT conversation_id, status FROM generations WHERE id = %s FOR UPDATE",
                (generation_id,),
            )
            row = cur.fetchone()
            if row is None or row["status"] != "running":
                conn.rollback()
                return None
            cur.execute(
                "INSERT INTO messages (conversation_id, role, content, status, generation_id) "
                "VALUES (%s, 'assistant', %s, %s, %s) RETURNING id",
                (row["conversation_id"], content, message_status, generation_id),
            )
            message_id = cur.fetchone()["id"]
            cur.execute(
                "UPDATE generations SET status = %s, error = %s, assistant_message_id = %s, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                (status, error, message_id, generation_id),
            )
        # 不更新 conversations：会话的 updated_at 已在登记用户消息时刷新，
        # 这里只锁 generations 行，避免与删除会话（先锁 conversations 再锁 generations）
        # 形成锁顺序反转。
        conn.commit()
        return message_id
    finally:
        _put_conn(conn)


def delete_conversation_guarded(
    user_id: int,
    conv_id: str,
    stale_seconds: float,
    cleanup,
) -> str:
    """删除会话：与生成任务共用会话锁，运行中的任务会先收到取消请求并拒绝本次删除。

    cleanup 在持锁事务内执行（用于清理 LangGraph 检查点），失败时整体回滚，
    业务数据保持原样。返回 'deleted' | 'busy' | 'not_found'。
    """
    conn = _get_conn()
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (_conversation_lock_key(conv_id),))
            cur.execute(
                "SELECT id FROM conversations WHERE id = %s AND user_id = %s FOR UPDATE",
                (conv_id, user_id),
            )
            if cur.fetchone() is None:
                conn.rollback()
                return "not_found"

            _reap_stale_generations(cur, conv_id, stale_seconds)
            cur.execute(
                "SELECT id FROM generations "
                "WHERE conversation_id = %s AND status = 'running' LIMIT 1",
                (conv_id,),
            )
            if cur.fetchone() is not None:
                cur.execute(
                    "UPDATE generations SET cancel_requested = TRUE "
                    "WHERE conversation_id = %s AND status = 'running'",
                    (conv_id,),
                )
                conn.commit()
                return "busy"

        # 检查点清理可能包含重试等待，放在同一持锁事务内完成：
        # 期间其他进程无法登记新的生成任务；失败则回滚，业务数据不受影响。
        cleanup()
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM conversations WHERE id = %s AND user_id = %s",
                (conv_id, user_id),
            )
        conn.commit()
        return "deleted"
    except Exception:
        conn.rollback()
        raise
    finally:
        _put_conn(conn)
