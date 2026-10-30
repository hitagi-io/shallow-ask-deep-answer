import os
import re
import random
import threading
import time
from datetime import datetime, timedelta, timezone

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
                SELECT c.id, c.title, m.role, m.content, m.status
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
                    {"role": r["role"], "content": r["content"], "status": r["status"]}
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


def append_message(user_id: int, conv_id: str, role: str, content: str, status: str | None) -> bool:
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM conversations WHERE id = %s AND user_id = %s",
                (conv_id, user_id),
            )
            if not cur.fetchone():
                return False
            cur.execute(
                "INSERT INTO messages (conversation_id, role, content, status) VALUES (%s, %s, %s, %s)",
                (conv_id, role, content, status),
            )
            cur.execute(
                "UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                (conv_id,),
            )
        conn.commit()
        return True
    finally:
        _put_conn(conn)


def delete_conversation(user_id: int, conv_id: str) -> bool:
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM conversations WHERE id = %s AND user_id = %s",
                (conv_id, user_id),
            )
            changed = cur.rowcount > 0
        conn.commit()
        return changed
    finally:
        _put_conn(conn)
