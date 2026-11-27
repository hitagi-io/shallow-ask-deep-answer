"""账号自助管理：凭证轮换，以及短期验证凭证保护的完整注销。"""
from datetime import datetime, timedelta, timezone

import jwt
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row

import auth
from utils.core.pg_lock import project_index_lock_key
from utils.observability.audit import append_audit
from utils.runtime.shutdown import ServiceStopping


class AccountError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _locked_user(cur, user_id: int, version: int):
    cur.execute("SELECT id, username, password, auth_version FROM users "
                "WHERE id = %s AND NOT deletion_pending FOR NO KEY UPDATE", (user_id,))
    user = cur.fetchone()
    if user is None or user["auth_version"] != version:
        raise AccountError("登录已失效，请重新登录", 401)
    return user


def update_account(user_id: int, version: int, *, session_id: str, expires_at: int, username: str | None = None,
                   current_password: str | None = None, new_password: str | None = None) -> dict:
    if username is None and new_password is None:
        raise AccountError("请填写要修改的信息")
    if username is not None:
        username = username.strip()
        if not 2 <= len(username) <= auth.USERNAME_MAX_LENGTH:
            raise AccountError("用户名需为 2～50 个字符")
    if new_password is not None:
        error = auth._validate_password(new_password)
        if error:
            raise AccountError(error)
    with auth._connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            auth.account_write_access(cur, user_id)
            user = _locked_user(cur, user_id, version)
            if current_password is None or not auth._verify_password(current_password, user["password"]):
                raise AccountError("当前密码不正确")
            if new_password is not None:
                if current_password == new_password:
                    raise AccountError("新密码不能与当前密码相同")
                cur.execute("UPDATE users SET password = %s, auth_version = auth_version + 1 WHERE id = %s",
                            (auth._hash_password(new_password), user_id))
            if username is not None:
                try:
                    cur.execute("UPDATE users SET username = %s WHERE id = %s", (username, user_id))
                except UniqueViolation as exc:
                    raise AccountError("用户名已存在，请换一个名字", 409) from exc
                user["username"] = username
            append_audit(cur, user_id=user_id, action="account.update", resource_type="account",
                         resource_id=str(user_id), result="succeeded",
                         details={"fields": (["username"] if username is not None else []) +
                                  (["password"] if new_password is not None else [])})
        conn.commit()
    if new_password is not None:
        return {"user": {"id": user_id, "username": user["username"]}, "reauthenticate": True}
    return {"user": {"id": user_id, "username": user["username"]},
            "token": auth._create_token(user_id, user["username"], user["auth_version"],
                                        session_id=session_id, expires_at=expires_at)}


def prepare_deletion(user_id: int, version: int, password: str) -> dict:
    with auth._connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            user = _locked_user(cur, user_id, version)
            if not auth._verify_password(password, user["password"]):
                raise AccountError("当前密码不正确")
            payload = {"purpose": "account_deletion", "user_id": user_id,
                       "username": user["username"], "auth_version": version,
                       "exp": datetime.now(timezone.utc) + timedelta(minutes=5)}
    return {"confirmation_token": jwt.encode(payload, auth.JWT_SECRET, algorithm="HS256"),
            "confirmation_text": f"注销 {user['username']}"}


def _deletion_claims(token: str, user_id: int, version: int, confirmation: str) -> dict:
    try:
        claims = jwt.decode(token, auth.JWT_SECRET, algorithms=["HS256"],
                            options={"require": ["exp", "purpose", "user_id", "username", "auth_version"]})
    except jwt.PyJWTError as exc:
        raise AccountError("注销验证已失效，请重新验证当前密码") from exc
    if (claims["purpose"] != "account_deletion" or claims["user_id"] != user_id
            or claims["auth_version"] != version):
        raise AccountError("注销验证无效，请重新验证当前密码")
    if confirmation != f"注销 {claims['username']}":
        raise AccountError("确认文字不匹配")
    return claims


def begin_account_deletion(user_id: int, version: int, token: str, confirmation: str, *,
                           stale_seconds: float, slot_max_age: int) -> None:
    """确认身份并与写请求互斥，在任何外部清理之前持久化注销请求。"""
    claims = _deletion_claims(token, user_id, version, confirmation)
    with auth._connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SET LOCAL lock_timeout = '10s'")
            cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", (auth._ACCOUNT_LOCK_CLASS, user_id))
            user = _locked_user(cur, user_id, version)
            if user["username"] != claims["username"]:
                raise AccountError("用户名已变更，请重新验证注销")
            cur.execute("SELECT id FROM projects WHERE user_id = %s ORDER BY id", (user_id,))
            project_ids = [row["id"] for row in cur.fetchall()]
            for project_id in project_ids:
                # 与常规索引操作使用相同的锁键，所有锁都由同一个连接持有。
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (project_index_lock_key(user_id, project_id),))
            cur.execute("SELECT id FROM projects WHERE user_id = %s ORDER BY id FOR UPDATE", (user_id,))
            project_ids = [row["id"] for row in cur.fetchall()]
            cur.execute("SELECT id FROM conversations WHERE user_id = %s ORDER BY id", (user_id,))
            conversation_ids = [row["id"] for row in cur.fetchall()]
            for conv_id in conversation_ids:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (auth._conversation_lock_key(conv_id),))
                cur.execute("SELECT id FROM conversations WHERE id = %s FOR UPDATE", (conv_id,))
                auth._reap_stale_generations(cur, conv_id, stale_seconds)
            cur.execute("SELECT 1 FROM generations WHERE user_id = %s AND status = 'running' LIMIT 1", (user_id,))
            if cur.fetchone() is not None:
                raise AccountError("还有回答正在生成，请停止或等待完成后再注销", 409)
            cur.execute("SELECT 1 FROM chat_slots WHERE user_id = %s AND generation_id IS NULL "
                        "AND started_at > now() - make_interval(secs => %s) LIMIT 1", (user_id, slot_max_age))
            if cur.fetchone() is not None:
                raise AccountError("有对话请求正在启动，请稍后重试注销", 409)
            cur.execute("UPDATE users SET deletion_pending=TRUE WHERE id=%s", (user_id,))
            cur.execute("UPDATE projects SET deleted_at=now(), updated_at=now(), deletion_pending=TRUE "
                        "WHERE user_id=%s AND deleted_at IS NULL", (user_id,))
            append_audit(cur, user_id=user_id, action="account.delete", resource_type="account",
                         resource_id=str(user_id), result="accepted")
        conn.commit()


def finish_account_deletion(user_id: int, cleanup_project, cleanup_directory) -> None:
    with auth._connection() as conn:
        projects = conn.execute("SELECT p.id FROM projects p JOIN users u ON u.id=p.user_id "
                                "WHERE u.id=%s AND u.deletion_pending AND p.deletion_pending ORDER BY p.id",
                                (user_id,)).fetchall()
    last_error = None
    for (project_id,) in projects:
        try:
            cleanup_project(user_id, project_id)
        except ServiceStopping:
            raise
        except Exception as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    with auth._connection() as conn, conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE id=%s AND deletion_pending FOR UPDATE", (user_id,))
        if cur.fetchone() is None:
            return
        cur.execute("SELECT 1 FROM projects WHERE user_id=%s AND deletion_pending LIMIT 1", (user_id,))
        if cur.fetchone():
            raise RuntimeError("项目清理尚未完成")
        cleanup_directory(user_id)
        cur.execute("SELECT id FROM conversations WHERE user_id=%s", (user_id,))
        saver = PostgresSaver(conn)
        for (conv_id,) in cur.fetchall():
            saver.delete_thread(f"{user_id}:{conv_id}")
        cur.execute("DELETE FROM users WHERE id=%s", (user_id,))
        append_audit(cur, user_id=user_id, action="account.delete", resource_type="account",
                     resource_id=str(user_id), result="succeeded")
