import base64
import json
import hashlib
import os
import re
import random
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from dotenv import load_dotenv

from utils.pg_lock import PG_LOCK_KEY_SETUP, exclusive_section
from utils.logging_config import get_logger
from rag.index_manifest import ManifestStore
from utils.upload_tasks import UploadTaskStore

logger = get_logger(__name__)

load_dotenv()

PG_CONN = os.getenv("PG_CONNECTION_STRING", "postgresql://postgres:postgres@localhost:5432/shallow_ask_deep_answer")
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
PASSWORD_MAX_LENGTH = 128

LOGIN_ACCOUNT_RATE_LIMIT = int(os.getenv("LOGIN_ACCOUNT_RATE_LIMIT", "10"))
LOGIN_ACCOUNT_RATE_WINDOW_SECONDS = int(os.getenv("LOGIN_ACCOUNT_RATE_WINDOW_SECONDS", "60"))
LOGIN_IP_RATE_LIMIT = int(os.getenv("LOGIN_IP_RATE_LIMIT", "100"))
LOGIN_IP_RATE_WINDOW_SECONDS = int(os.getenv("LOGIN_IP_RATE_WINDOW_SECONDS", "60"))
LOGIN_ACCOUNT_IP_MAX_ATTEMPTS = int(os.getenv("LOGIN_ACCOUNT_IP_MAX_ATTEMPTS", "5"))
LOGIN_ACCOUNT_IP_WINDOW_SECONDS = int(os.getenv("LOGIN_ACCOUNT_IP_WINDOW_SECONDS", "900"))
REGISTER_RATE_LIMIT = int(os.getenv("REGISTER_RATE_LIMIT", "5"))
REGISTER_RATE_WINDOW_SECONDS = int(os.getenv("REGISTER_RATE_WINDOW_SECONDS", "3600"))

# 对话并发槽位的跨进程咨询锁（advisory lock）命名空间（二参数形式：classid, user_id）。
_CHAT_SLOT_LOCK_CLASS = 0x64656573
_CONVERSATION_CREATE_LOCK_CLASS = 0x64656563
_ACCOUNT_LOCK_CLASS = 0x64656561


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


@contextmanager
def _connection():
    """借用业务连接；提交由调用方决定，退出时沿用未提交事务的回滚策略。"""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
    finally:
        if conn.info.transaction_status == TransactionStatus.INTRANS:
            conn.rollback()
        pool.putconn(conn)


def init_db():
    """在全新数据库中创建账号、私有项目、会话及生成任务结构。"""
    with exclusive_section(PG_CONN, PG_LOCK_KEY_SETUP):
        with _connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id          SERIAL PRIMARY KEY,
                        username    VARCHAR(50) UNIQUE NOT NULL,
                        password    VARCHAR(255) NOT NULL,
                        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS projects (
                        id          VARCHAR(36) PRIMARY KEY,
                        user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        name        VARCHAR(80) NOT NULL CHECK (length(trim(name)) > 0),
                        created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE (user_id, id)
                    );
                """)
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS auth_version INTEGER NOT NULL DEFAULT 0")
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_projects_user_updated "
                    "ON projects (user_id, updated_at DESC, id DESC)"
                )
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS conversations (
                        id          VARCHAR(36) PRIMARY KEY,
                        user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        title       VARCHAR(255) NOT NULL DEFAULT '新对话',
                        kb_only     BOOLEAN,
                        project_id  VARCHAR(36),
                        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE (user_id, id),
                        FOREIGN KEY (user_id, project_id) REFERENCES projects(user_id, id) ON DELETE CASCADE,
                        CHECK ((kb_only IS NULL AND project_id IS NULL)
                            OR (kb_only IS TRUE AND project_id IS NOT NULL)
                            OR (kb_only IS FALSE AND project_id IS NULL))
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS messages (
                        id              SERIAL PRIMARY KEY,
                        conversation_id VARCHAR(36) NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                        role            VARCHAR(10) NOT NULL CHECK (role IN ('user', 'assistant')),
                        content         TEXT NOT NULL,
                        status          VARCHAR(20),
                        generation_id   BIGINT,
                        kb_only         BOOLEAN,
                        created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_conversations_user_updated "
                    "ON conversations (user_id, updated_at DESC, id DESC)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_conversations_project "
                    "ON conversations (user_id, project_id)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_messages_conversation "
                    "ON messages (conversation_id, id)"
                )
                # 生成任务：业务消息与模型上下文之外的第三份状态，记录每次生成的
                # 幂等键、归属、终态和关联消息，供重放、互斥与中断补偿使用。
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS generations (
                        id                  BIGSERIAL PRIMARY KEY,
                        conversation_id     VARCHAR(36) NOT NULL,
                        user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        request_id          VARCHAR(64) NOT NULL,
                        kb_only             BOOLEAN NOT NULL,
                        project_id          VARCHAR(36),
                        status              VARCHAR(16) NOT NULL DEFAULT 'running'
                            CHECK (status IN ('running', 'completed', 'failed', 'cancelled')),
                        cancel_requested    BOOLEAN NOT NULL DEFAULT FALSE,
                        error               TEXT,
                        phase               TEXT NOT NULL DEFAULT 'thinking',
                        user_message_id     BIGINT,
                        assistant_message_id BIGINT,
                        heartbeat_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE (conversation_id, request_id),
                        FOREIGN KEY (user_id, conversation_id) REFERENCES conversations(user_id, id) ON DELETE CASCADE,
                        FOREIGN KEY (user_id, project_id) REFERENCES projects(user_id, id) ON DELETE CASCADE,
                        CHECK ((kb_only AND project_id IS NOT NULL) OR (NOT kb_only AND project_id IS NULL))
                    );
                """)
                cur.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_generations_running "
                    "ON generations (conversation_id) WHERE status = 'running'"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_generations_project "
                    "ON generations (user_id, project_id)"
                )
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS rate_limits (
                        id           BIGSERIAL PRIMARY KEY,
                        scope        VARCHAR(20) NOT NULL,
                        key          VARCHAR(128) NOT NULL,
                        attempted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_rate_limits_lookup "
                    "ON rate_limits (scope, key, attempted_at)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_rate_limits_attempted_at "
                    "ON rate_limits (attempted_at)"
                )
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS chat_slots (
                        token         VARCHAR(64) PRIMARY KEY,
                        user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        generation_id BIGINT,
                        started_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_chat_slots_user "
                    "ON chat_slots (user_id)"
                )
                UploadTaskStore.setup(cur)
                ManifestStore.setup(cur)
            conn.commit()



_ph = PasswordHasher()


def _hash_password(password: str) -> str:
    """使用 Argon2id 生成密码哈希（自动加盐）。"""
    return _ph.hash(password)


def _verify_password(password: str, stored: str) -> bool:
    try:
        _ph.verify(stored, password)
        return True
    except VerifyMismatchError:
        return False


def _validate_password(password: str) -> str | None:
    """校验密码长度与强度：10～128 字符，含数字、英文字母、特殊符号。"""
    if len(password) > PASSWORD_MAX_LENGTH:
        return f"密码最长 {PASSWORD_MAX_LENGTH} 个字符"
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



def _create_token(user_id: int, username: str, auth_version: int = 0) -> str:
    payload = {
        "user_id": user_id,
        "username": username,
        "auth_version": auth_version,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def verify_token(token: str) -> dict | None:
    """验证 JWT，返回 payload 或 None。"""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return payload if payload.get("purpose", "access") == "access" else None
    except jwt.PyJWTError:
        return None


def get_user_by_id(user_id: int) -> dict | None:
    """按 id 查询用户及凭证版本（不缓存）；不存在返回 None。"""
    with _connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, username, auth_version FROM users WHERE id = %s",
                (user_id,),
            )
            return cur.fetchone()


_RATE_PURGE_PROBABILITY = 0.01
_RATE_RETENTION = timedelta(hours=2)


def _rate_lock_key(scope: str, key: str) -> int:
    """生成限流键对应的咨询锁 key；哈希碰撞只会让无关键短暂串行，不影响正确性。"""
    digest = hashlib.blake2b(
        f"rate-limit:{scope}:{key}".encode("utf-8"), digest_size=8
    ).digest()
    return int.from_bytes(digest, "big") & 0x7FFFFFFFFFFFFFFF


def _rate_lock_keys(cur, pairs: list[tuple[str, str]]) -> None:
    """按固定顺序获取多个限流键的事务级咨询锁（advisory lock），避免并发请求之间形成死锁。"""
    for lock_key in sorted({_rate_lock_key(scope, key) for scope, key in pairs}):
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))


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


def _rate_consume(cur, scope: str, key: str, limit: int, window_seconds: int, now: datetime) -> bool:
    """原子地「检查并记录」一次尝试：先获取该限流键的事务级咨询锁（advisory lock）。"""
    _rate_lock_keys(cur, [(scope, key)])
    if _rate_exceeded(cur, scope, key, limit, window_seconds, now):
        return False
    _rate_record(cur, scope, key, now)
    return True


def consume_rate_limit(scope: str, key: str, limit: int, window_seconds: int) -> bool:
    """检查限额并在获准时记录一次尝试、返回 True；超限返回 False。

    以数据库为计数载体，并在同一事务内按 (scope, key) 持有事务级咨询锁（advisory lock）：
    检查与插入因此成为原子操作，多进程部署下同一限流键也只会放行到上限，
    且不依赖长期驻留内存的字典。锁在事务提交/回滚时自动释放。
    """
    with _connection() as conn:
        now = datetime.now()
        with conn.cursor(row_factory=dict_row) as cur:
            allowed = _rate_consume(cur, scope, key, limit, window_seconds, now)
        conn.commit()
        return allowed



def register(username: str, password: str, ip: str | None = None) -> dict:
    """注册新用户。返回 {'ok': True} 或 {'ok': False, 'error': ...}。"""
    username = username.strip()
    if not username or len(username) < 2:
        return {"ok": False, "error": "用户名至少 2 个字符"}
    if len(username) > USERNAME_MAX_LENGTH:
        return {"ok": False, "error": f"用户名最长 {USERNAME_MAX_LENGTH} 个字符"}
    err = _validate_password(password)
    if err:
        return {"ok": False, "error": err}

    if ip and not consume_rate_limit("register", ip, REGISTER_RATE_LIMIT, REGISTER_RATE_WINDOW_SECONDS):
        return {"ok": False, "error": "注册过于频繁，请稍后再试", "rate_limited": True}

    password_hash = _hash_password(password)
    with _connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (username, password) VALUES (%s, %s) "
                "ON CONFLICT (username) DO NOTHING",
                (username, password_hash),
            )
            created = cur.rowcount > 0
        conn.commit()
        if not created:
            return {"ok": False, "error": "用户名已存在"}
        return {"ok": True}


def login(username: str, password: str, ip: str | None = None) -> dict:
    """登录，返回 token 和用户信息。

    限流策略（目标：既不误伤共享出口的正常用户，也不让攻击者定向踢人）：
    - 账号级限流：按用户名计数，挡住针对单一账号的多 IP 爆破，与来源 IP 无关；
    - IP 级限流：阈值放宽，仅作粗粒度防护，避免 NAT / 机房共享出口被整体拒绝；
    - 单 IP × 单账号失败冷却：只挡该来源对该账号的尝试，该账号在其他 IP 的正常登录不受影响。

    爆破防护完全由上述三层限流承担，不做账号级锁定：否则攻击者只要知道用户名、
    故意输错密码，就能把真实用户踢下线。
    """
    if len(password) > PASSWORD_MAX_LENGTH:
        return {"ok": False, "error": f"密码最长 {PASSWORD_MAX_LENGTH} 个字符"}
    username = username.strip()
    if len(username) > USERNAME_MAX_LENGTH:
        return {"ok": False, "error": "用户名或密码错误"}
    now = datetime.now()
    acct_ip_key = f"{username}|{ip}" if ip else None

    with _connection() as conn:
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

        with conn.cursor(row_factory=dict_row) as cur:
            # 三个限流键一次性加锁、按固定顺序获取：检查与记录在同一临界区内
            # 完成，并发登录请求无法同时读到「未超限」的计数而全部放行。
            _rate_lock_keys(cur, [(scope, key) for scope, key, _, _ in checks])
            for scope, key, limit, window in checks:
                if _rate_exceeded(cur, scope, key, limit, window, now):
                    conn.rollback()
                    return {"ok": False, "error": "尝试过于频繁，请稍后再试", "rate_limited": True}

            _rate_record(cur, "login_acct", username, now)
            if ip:
                _rate_record(cur, "login_ip", ip, now)

            cur.execute(
                "SELECT id, username, password, auth_version FROM users WHERE username = %s",
                (username,),
            )
            row = cur.fetchone()
        # 提交释放咨询锁，并退出上下文归还连接，再执行耗时的密码校验。
        conn.commit()

    if not row:
        return {"ok": False, "error": "用户名或密码错误"}

    if not _verify_password(password, row["password"]):
        if acct_ip_key:
            # 按需借用连接，在独立事务内原子检查并记录失败次数。
            allowed = consume_rate_limit(
                "login_fail_ip", acct_ip_key,
                LOGIN_ACCOUNT_IP_MAX_ATTEMPTS, LOGIN_ACCOUNT_IP_WINDOW_SECONDS,
            )
            if not allowed:
                return {"ok": False, "error": "尝试过于频繁，请稍后再试", "rate_limited": True}
        return {"ok": False, "error": "用户名或密码错误"}

    token = _create_token(row["id"], row["username"], row["auth_version"])
    return {
        "ok": True,
        "token": token,
        "user": {
            "id": row["id"],
            "username": row["username"],
        },
    }



_PROJECT_FIELDS = "id, name, created_at, updated_at"


def _project_name(name: str) -> str:
    name = name.strip()
    if not 1 <= len(name) <= 80:
        raise ValueError("项目名称需为 1～80 个字符")
    return name


def list_projects(user_id: int) -> list[dict]:
    with _connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(f"SELECT {_PROJECT_FIELDS} FROM projects WHERE user_id = %s "
                        "ORDER BY updated_at DESC, id DESC", (user_id,))
            return cur.fetchall()


def list_project_scopes() -> list[dict]:
    """内部维护任务枚举存储范围；不作为用户接口返回。"""
    with _connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT id, user_id FROM projects ORDER BY user_id, id")
            return cur.fetchall()


def get_project(user_id: int, project_id: str) -> dict | None:
    with _connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(f"SELECT {_PROJECT_FIELDS} FROM projects WHERE id = %s AND user_id = %s",
                        (project_id, user_id))
            return cur.fetchone()


@contextmanager
def project_access(user_id: int, project_id: str):
    """文件及索引操作持有项目读锁，防止归属检查与外部操作之间发生项目删除。"""
    with _connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(f"SELECT {_PROJECT_FIELDS} FROM projects WHERE id = %s AND user_id = %s FOR SHARE",
                        (project_id, user_id))
            project = cur.fetchone()
        yield project


def account_write_access(cur, user_id: int) -> None:
    """与注销互斥；等待结束后重新验证账号，防止旧请求在删除后创建数据。"""
    cur.execute("SELECT pg_advisory_xact_lock_shared(%s, %s)", (_ACCOUNT_LOCK_CLASS, user_id))
    cur.execute("SELECT id FROM users WHERE id = %s", (user_id,))
    if cur.fetchone() is None:
        raise ValueError("账号不存在或已被注销")


def create_project(user_id: int, name: str) -> dict:
    name = _project_name(name)
    with _connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            account_write_access(cur, user_id)
            cur.execute("INSERT INTO projects (id, user_id, name) VALUES (%s, %s, %s) "
                        f"RETURNING {_PROJECT_FIELDS}", (str(uuid.uuid4()), user_id, name))
            project = cur.fetchone()
        conn.commit()
        return project


def update_project(user_id: int, project_id: str, name: str) -> dict | None:
    name = _project_name(name)
    with _connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("UPDATE projects SET name = %s, updated_at = CURRENT_TIMESTAMP "
                        f"WHERE id = %s AND user_id = %s RETURNING {_PROJECT_FIELDS}",
                        (name, project_id, user_id))
            project = cur.fetchone()
        conn.commit()
        return project


def delete_project(user_id: int, project_id: str, stale_seconds: float = 120, cleanup=None) -> dict:
    """删除私有项目及其会话；运行中拒绝删除。cleanup(ids) 在持锁事务内清理外部存储。"""
    with _connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            # 与登记生成保持相同顺序：项目行锁在先，会话咨询锁在后。
            cur.execute("SELECT id FROM projects WHERE id = %s AND user_id = %s FOR UPDATE",
                        (project_id, user_id))
            if cur.fetchone() is None:
                return {"status": "not_found", "conversation_ids": []}
            cur.execute("SELECT id FROM conversations WHERE project_id = %s AND user_id = %s ORDER BY id",
                        (project_id, user_id))
            conversation_ids = [row["id"] for row in cur.fetchall()]
            for conv_id in conversation_ids:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (_conversation_lock_key(conv_id),))
                cur.execute("SELECT id FROM conversations WHERE id = %s AND user_id = %s FOR UPDATE",
                            (conv_id, user_id))
                _reap_stale_generations(cur, conv_id, stale_seconds)
            cur.execute("SELECT 1 FROM generations WHERE project_id = %s AND user_id = %s "
                        "AND status = 'running' LIMIT 1", (project_id, user_id))
            if cur.fetchone() is not None:
                conn.commit()
                return {"status": "busy", "conversation_ids": conversation_ids}
        if cleanup is not None:
            cleanup(conversation_ids)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM projects WHERE id = %s AND user_id = %s", (project_id, user_id))
        conn.commit()
        return {"status": "deleted", "conversation_ids": conversation_ids}


def list_conversations(user_id: int, limit: int = 20, cursor: str | None = None) -> dict:
    """按更新时间和 ID 翻页，只返回会话摘要。游标保存原始数据库时间精度。"""
    params: list = [user_id]
    boundary = ""
    if cursor:
        try:
            stamp, conv_id = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
            stamp = datetime.fromisoformat(stamp)
            if stamp.tzinfo is not None or not isinstance(conv_id, str) or not 1 <= len(conv_id) <= 36:
                raise ValueError()
        except (ValueError, TypeError, UnicodeError) as exc:
            raise ValueError("无效的会话分页游标") from exc
        boundary = " AND (c.updated_at, c.id) < (%s, %s)"
        params.extend([stamp, conv_id])
    params.append(limit + 1)
    with _connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT c.id, c.title, c.updated_at, "
                "c.updated_at AT TIME ZONE current_setting('TIMEZONE') AS display_time, "
                "EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id = c.id) AS has_messages, "
                "c.kb_only, c.project_id, p.name AS project_name "
                "FROM conversations c LEFT JOIN projects p ON p.id = c.project_id AND p.user_id = c.user_id "
                "WHERE c.user_id = %s" + boundary +
                " ORDER BY c.updated_at DESC, c.id DESC LIMIT %s", params,
            )
            rows = cur.fetchall()
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = base64.urlsafe_b64encode(json.dumps(
            [last["updated_at"].isoformat(), last["id"]]
        ).encode()).decode()
    return {
        "conversations": [{"id": r["id"], "title": r["title"],
                           "updatedAt": r["display_time"].isoformat(),
                           "hasMessages": r["has_messages"], "kbOnly": r["kb_only"],
                           "project_id": r["project_id"], "project_name": r["project_name"]} for r in page],
        "next_cursor": next_cursor,
    }


def list_messages(
    user_id: int,
    conv_id: str,
    limit: int = 50,
    before: int | None = None,
    stale_seconds: float = 120,
) -> dict | None:
    """验证归属后读取会话消息及固定模式，按 ID 向前翻页，页内正序返回。"""
    with _connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT c.id, c.kb_only, c.project_id, p.name AS project_name FROM conversations c "
                        "LEFT JOIN projects p ON p.id = c.project_id AND p.user_id = c.user_id "
                        "WHERE c.id = %s AND c.user_id = %s FOR SHARE OF c",
                        (conv_id, user_id))
            conversation = cur.fetchone()
            if conversation is None:
                return None
            _reap_stale_generations(cur, conv_id, stale_seconds)
            # 先读取任务、再读取消息：结束发生在两次读取之间时，订阅会再取一次终态。
            cur.execute("SELECT request_id FROM generations WHERE conversation_id = %s AND status = 'running'",
                        (conv_id,))
            active_generation = cur.fetchone()
            params: list = [conv_id]
            boundary = ""
            if before is not None:
                boundary = " AND id < %s"
                params.append(before)
            params.append(limit + 1)
            cur.execute('SELECT id, role, content, status, kb_only AS "kbOnly" FROM messages '
                        "WHERE conversation_id = %s" +
                        boundary + " ORDER BY id DESC LIMIT %s", params)
            rows = cur.fetchall()
        conn.commit()
    page = rows[:limit]
    return {"messages": list(reversed(page)),
            "kbOnly": conversation["kb_only"],
            "project_id": conversation["project_id"], "project_name": conversation["project_name"],
            "active_generation": active_generation,
            "next_before": page[-1]["id"] if len(rows) > limit else None}



def create_conversation(user_id: int, conv_id: str, title: str) -> dict | None:
    """复用用户唯一的空白会话，否则创建；ID 冲突时返回 None。"""
    with _connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            account_write_access(cur, user_id)
            # 同一用户跨标签页、跨进程的新建操作串行化，不依赖前端是否加载了全部分页。
            cur.execute("SELECT pg_advisory_xact_lock(%s, %s)",
                        (_CONVERSATION_CREATE_LOCK_CLASS, user_id))
            while True:
                cur.execute(
                    "SELECT c.id FROM conversations c WHERE c.user_id = %s "
                    "AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id = c.id) "
                    "ORDER BY c.updated_at DESC, c.id DESC LIMIT 1",
                    (user_id,),
                )
                empty = cur.fetchone()
                if empty is None:
                    cur.execute(
                        "INSERT INTO conversations (id, user_id, title) VALUES (%s, %s, %s) "
                        "ON CONFLICT (id) DO NOTHING RETURNING id, title, "
                        "updated_at AT TIME ZONE current_setting('TIMEZONE') AS display_time",
                        (conv_id, user_id, title),
                    )
                    row = cur.fetchone()
                    break
                # 与发送、删除共用会话锁，再次核实它仍为空，避免覆盖刚开始的对话。
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (_conversation_lock_key(empty["id"]),))
                cur.execute(
                    "UPDATE conversations c SET updated_at = CURRENT_TIMESTAMP "
                    "WHERE c.id = %s AND c.user_id = %s "
                    "AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id = c.id) "
                    "RETURNING c.id, c.title, "
                    "c.updated_at AT TIME ZONE current_setting('TIMEZONE') AS display_time",
                    (empty["id"], user_id),
                )
                row = cur.fetchone()
                if row is not None:
                    break
        conn.commit()
        if row is None:
            return None
        return {"id": row["id"], "title": row["title"],
                "updatedAt": row["display_time"].isoformat(), "hasMessages": False, "kbOnly": None,
                "project_id": None, "project_name": None}


def update_conversation_title(user_id: int, conv_id: str, title: str) -> bool:
    with _connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE conversations SET title = %s, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = %s AND user_id = %s",
                (title, conv_id, user_id),
            )
            changed = cur.rowcount > 0
        conn.commit()
        return changed


def _conversation_lock_key(conv_id: str) -> int:
    """会话级咨询锁（advisory lock） key：让登记生成任务与删除会话共享同一把跨进程锁。"""
    digest = hashlib.blake2b(
        f"chat-generation:{conv_id}".encode("utf-8"), digest_size=8
    ).digest()
    return int.from_bytes(digest, "big") & 0x7FFFFFFFFFFFFFFF


_GENERATION_REPLAY_QUERY = """
    SELECT g.id, g.request_id, g.status, g.error, g.phase,
           g.user_message_id, g.assistant_message_id, g.kb_only, g.project_id,
           p.name AS project_name,
           EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - g.created_at)) AS elapsed_seconds,
           m.content AS assistant_content
    FROM generations g
    LEFT JOIN messages m ON m.id = g.assistant_message_id
    LEFT JOIN projects p ON p.id = g.project_id AND p.user_id = g.user_id
    WHERE g.conversation_id = %s AND g.request_id = %s
"""


def _reap_stale_generations(cur, conv_id: str, stale_seconds: float) -> int:
    """将长时间未更新心跳时间戳的运行中生成任务置为 failed，并补写一条中断提示消息。

    进程异常退出或重启时，生成任务无法自行结束；心跳时间戳停止更新后，
    由后续请求在同一事务内执行补偿，避免 generations 中的任务长期停留在
    running 状态。
    """
    cur.execute(
        "SELECT id, assistant_message_id, kb_only FROM generations "
        "WHERE conversation_id = %s AND status = 'running' "
        "AND heartbeat_at < now() - make_interval(secs => %s) "
        "FOR UPDATE",
        (conv_id, stale_seconds),
    )
    rows = cur.fetchall()
    for row in rows:
        message_id = row["assistant_message_id"]
        cur.execute("UPDATE messages SET status = 'error', content = CASE WHEN content = '' "
                    "THEN '回答已中断，请重新提问。' ELSE content END WHERE id = %s", (message_id,))
        cur.execute(
            "UPDATE generations SET status = 'failed', error = %s, "
            "assistant_message_id = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
            ("服务暂时不可用，回答已中断，请稍后再试。", message_id, row["id"]),
        )
    if rows:
        logger.warning("[会话] 已将 %s 个长期未更新的生成任务标记为失败 conversation_id=%s", len(rows), conv_id)
    return len(rows)


def begin_generation(
    user_id: int,
    conv_id: str,
    request_id: str,
    content: str,
    stale_seconds: float,
    kb_only: bool = True,
    project_id: str | None = None,
) -> dict:
    """登记一次生成任务，并原子写入用户消息。

    同一会话同时只允许一个运行中的任务（由数据库部分唯一索引保证），
    首次接受消息时固定会话模式和私有项目，绑定与消息原子提交。
    request_id 重放也必须匹配模式和项目；归属检查不依赖前端传入用户信息。

    返回：
    - {"status": "started", "generation_id": ..., "user_message_id": ...}
    - {"status": "replay", "generation": {...}}
    - {"status": "busy"}
    - {"status": "mode_conflict"}：请求编号已经用于其他模式
    - {"status": "conversation_mode_conflict"}：会话已经固定为其他模式
    - {"status": "project_required"}：知识库模式未选择项目
    - {"status": "project_not_found"}：项目不存在或不属于当前用户
    - {"status": "project_conflict"}：通用模式携带项目，或重放使用了不同项目
    - {"status": "conversation_project_conflict"}：会话已经固定为其他项目
    - {"status": "not_found"}：会话不存在或不属于当前用户
    """
    if kb_only and not project_id:
        return {"status": "project_required"}
    if not kb_only and project_id is not None:
        return {"status": "project_conflict"}
    with _connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            account_write_access(cur, user_id)
            if project_id is not None:
                cur.execute("SELECT id FROM projects WHERE id = %s AND user_id = %s FOR SHARE",
                            (project_id, user_id))
                if cur.fetchone() is None:
                    return {"status": "project_not_found"}
            # 与删除会话共用同一把事务级咨询锁（advisory lock），避免「登记—删除」互相穿插
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (_conversation_lock_key(conv_id),))
            cur.execute(
                "SELECT id, kb_only, project_id FROM conversations WHERE id = %s AND user_id = %s FOR UPDATE",
                (conv_id, user_id),
            )
            conversation = cur.fetchone()
            if conversation is None:
                conn.rollback()
                return {"status": "not_found"}
            if conversation["kb_only"] is not None and conversation["kb_only"] != kb_only:
                conn.rollback()
                return {"status": "conversation_mode_conflict"}
            if conversation["kb_only"] is not None and conversation["project_id"] != project_id:
                return {"status": "conversation_project_conflict"}

            _reap_stale_generations(cur, conv_id, stale_seconds)

            cur.execute(_GENERATION_REPLAY_QUERY, (conv_id, request_id))
            existing = cur.fetchone()
            if existing is not None:
                conn.commit()
                if existing["kb_only"] != kb_only:
                    return {"status": "mode_conflict"}
                if existing["project_id"] != project_id:
                    return {"status": "project_conflict"}
                return {"status": "replay", "generation": existing}

            cur.execute("SELECT 1 FROM generations WHERE conversation_id = %s AND status = 'running'",
                        (conv_id,))
            if cur.fetchone() is not None:
                conn.commit()
                return {"status": "busy"}

            cur.execute(
                "INSERT INTO generations (conversation_id, user_id, request_id, kb_only, project_id) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (conv_id, user_id, request_id, kb_only, project_id),
            )
            generation_id = cur.fetchone()["id"]

            cur.execute(
                "INSERT INTO messages (conversation_id, role, content, generation_id, kb_only) "
                "VALUES (%s, 'user', %s, %s, %s) RETURNING id",
                (conv_id, content, generation_id, kb_only),
            )
            user_message_id = cur.fetchone()["id"]
            cur.execute(
                "INSERT INTO messages (conversation_id, role, content, status, generation_id, kb_only) "
                "VALUES (%s, 'assistant', '', 'streaming', %s, %s) RETURNING id",
                (conv_id, generation_id, kb_only),
            )
            assistant_message_id = cur.fetchone()["id"]
            cur.execute(
                "UPDATE generations SET user_message_id = %s, assistant_message_id = %s WHERE id = %s",
                (user_message_id, assistant_message_id, generation_id),
            )
            cur.execute(
                "UPDATE conversations SET kb_only = %s, project_id = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                (kb_only, project_id, conv_id),
            )
        conn.commit()
        return {
            "status": "started",
            "generation_id": generation_id,
            "user_message_id": user_message_id,
            "assistant_message_id": assistant_message_id,
        }


def get_generation(user_id: int, conv_id: str, request_id: str, stale_seconds: float) -> dict | None:
    """可跨 worker 订阅的完整快照；每次读取校验归属并补偿失联任务。"""
    with _connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            query = _GENERATION_REPLAY_QUERY + " AND g.user_id = %s"
            cur.execute(query, (conv_id, request_id, user_id))
            row = cur.fetchone()
            if row is not None and row["status"] == "running":
                _reap_stale_generations(cur, conv_id, stale_seconds)
                cur.execute(query, (conv_id, request_id, user_id))
                row = cur.fetchone()
        conn.commit()
    if row is not None:
        row["elapsed_seconds"] = float(row["elapsed_seconds"])
    return row


def cancel_generation(user_id: int, conv_id: str, request_id: str) -> bool:
    """显式停止与断开订阅分离；重复停止已结束的任务也成功。"""
    with _connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE generations SET cancel_requested = TRUE "
                        "WHERE conversation_id = %s AND request_id = %s AND user_id = %s",
                        (conv_id, request_id, user_id))
            found = cur.rowcount > 0
        conn.commit()
    return found


def save_generation_progress(generation_id: int, content: str | None, phase: str) -> bool:
    """保存增量快照和心跳；返回 False 表示已取消或已由补偿流程结束。"""
    with _connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("UPDATE generations SET heartbeat_at = CURRENT_TIMESTAMP, phase = %s "
                        "WHERE id = %s AND status = 'running' "
                        "RETURNING assistant_message_id, cancel_requested", (phase, generation_id))
            row = cur.fetchone()
            if row is not None and content is not None:
                cur.execute("UPDATE messages SET content = %s WHERE id = %s",
                            (content, row["assistant_message_id"]))
        conn.commit()
    return row is not None and not row["cancel_requested"]


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
    with _connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT conversation_id, status, kb_only, assistant_message_id, cancel_requested "
                "FROM generations WHERE id = %s FOR UPDATE",
                (generation_id,),
            )
            row = cur.fetchone()
            if row is None or row["status"] != "running":
                conn.rollback()
                return None
            if row["cancel_requested"] and status == "completed":
                status, message_status, error = "cancelled", "cancelled", "已停止回答。"
            if not content and message_status == "error":
                content = error or "回答已中断，请重新提问。"
            message_id = row["assistant_message_id"]
            cur.execute("UPDATE messages SET content = %s, status = %s WHERE id = %s",
                        (content, message_status, message_id))
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
    with _connection() as conn:
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


def acquire_chat_slot(
    user_id: int,
    limit: int,
    max_age_seconds: int,
    stale_seconds: float,
) -> dict:
    """跨进程占用一个对话并发槽位（数据库租约），超额时 token 为 None。

    limit <= 0 时不限制数量，仍记录租约并清理失效任务。

    计数以 chat_slots 表为唯一来源：按 user_id 持有事务级咨询锁（advisory lock）后，先清理失效
    租约再判定剩余数量并插入，多个 worker/实例因此共享同一上限。失效租约分两类：
    超过 max_age_seconds 的租约，以及绑定的生成任务已结束或心跳超时
    stale_seconds 的租约；后者对应的运行中任务会被写入 cancel_requested，
    由持有该任务的心跳循环停止上游生成，避免只删租约而任务继续占用资源。

    返回 {"token": str | None, "reaped": [失效租约 token]}，reaped 供调用方
    取消本进程内仍然存活的任务。
    """
    with _connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            account_write_access(cur, user_id)
            cur.execute(
                "SELECT pg_advisory_xact_lock(%s, %s)",
                (_CHAT_SLOT_LOCK_CLASS, user_id),
            )
            cur.execute(
                """
                SELECT s.token, s.generation_id,
                       s.started_at <= now() - make_interval(secs => %s) AS lease_expired,
                       s.generation_id IS NOT NULL AND (
                           g.id IS NULL
                           OR g.status <> 'running'
                           OR g.heartbeat_at <= now() - make_interval(secs => %s)
                       ) AS generation_dead
                FROM chat_slots s
                LEFT JOIN generations g ON g.id = s.generation_id
                WHERE s.user_id = %s
                ORDER BY s.started_at
                """,
                (max_age_seconds, stale_seconds, user_id),
            )
            rows = cur.fetchall()

            reaped = [
                row["token"]
                for row in rows
                if row["lease_expired"] or row["generation_dead"]
            ]
            cancel_ids = [
                row["generation_id"]
                for row in rows
                if row["generation_id"] is not None
                and (row["lease_expired"] or row["generation_dead"])
            ]
            if reaped:
                cur.execute(
                    "DELETE FROM chat_slots WHERE user_id = %s AND token = ANY(%s)",
                    (user_id, reaped),
                )
            if cancel_ids:
                cur.execute(
                    "UPDATE generations SET cancel_requested = TRUE "
                    "WHERE id = ANY(%s) AND status = 'running'",
                    (cancel_ids,),
                )

            token = None
            if limit <= 0 or len(rows) - len(reaped) < limit:
                token = uuid.uuid4().hex
                cur.execute(
                    "INSERT INTO chat_slots (token, user_id) VALUES (%s, %s)",
                    (token, user_id),
                )
        conn.commit()
        return {"token": token, "reaped": reaped}


def bind_chat_slot_generation(user_id: int, token: str, generation_id: int) -> bool:
    """把租约与生成任务关联，使租约过期时能跨进程请求该任务停止生成。"""
    with _connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE chat_slots SET generation_id = %s WHERE token = %s AND user_id = %s",
                (generation_id, token, user_id),
            )
            bound = cur.rowcount > 0
        conn.commit()
        return bound


def release_chat_slot(user_id: int, token: str) -> None:
    """释放租约；进程异常退出时未释放的租约由后续 acquire 按过期规则清理。"""
    with _connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM chat_slots WHERE token = %s AND user_id = %s",
                (token, user_id),
            )
        conn.commit()
