"""账号自助管理：凭证轮换，以及短期验证凭证保护的完整注销。"""
from datetime import datetime, timedelta, timezone

import jwt
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row

import auth
from utils.pg_lock import project_index_lock_key


class AccountError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _check_rate(user_id: int):
    if not auth.consume_rate_limit("account_change", str(user_id), 10, 60):
        raise AccountError("操作过于频繁，请稍后再试", 429)


def _locked_user(cur, user_id: int, version: int):
    cur.execute("SELECT id, username, password, auth_version FROM users WHERE id = %s FOR UPDATE", (user_id,))
    user = cur.fetchone()
    if user is None or user["auth_version"] != version:
        raise AccountError("登录已失效，请重新登录", 401)
    return user


def update_account(user_id: int, version: int, *, username: str | None = None,
                   current_password: str | None = None, new_password: str | None = None) -> dict:
    _check_rate(user_id)
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
    try:
        with auth._connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                auth.account_write_access(cur, user_id)
                user = _locked_user(cur, user_id, version)
                if new_password is not None:
                    if not auth._verify_password(current_password or "", user["password"]):
                        raise AccountError("当前密码不正确")
                    if current_password == new_password:
                        raise AccountError("新密码不能与当前密码相同")
                    cur.execute("UPDATE users SET password = %s, auth_version = auth_version + 1 WHERE id = %s",
                                (auth._hash_password(new_password), user_id))
                    user["auth_version"] += 1
                if username is not None:
                    cur.execute("UPDATE users SET username = %s WHERE id = %s", (username, user_id))
                    user["username"] = username
            conn.commit()
    except UniqueViolation as exc:
        raise AccountError("用户名已存在，请换一个名字", 409) from exc
    if new_password is not None:
        return {"user": {"id": user_id, "username": user["username"]}, "reauthenticate": True}
    return {"user": {"id": user_id, "username": user["username"]},
            "token": auth._create_token(user_id, user["username"], user["auth_version"])}


def prepare_deletion(user_id: int, version: int, password: str) -> dict:
    _check_rate(user_id)
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


def delete_account(user_id: int, version: int, token: str, confirmation: str, *,
                   cleanup, stale_seconds: float, slot_max_age: int) -> None:
    """持账号排他锁，按索引→项目→会话顺序清理；外部失败保留账号以便重试。"""
    _check_rate(user_id)
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
            cleanup(project_ids, conversation_ids)
            cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
        conn.commit()
