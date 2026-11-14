"""有总时限的项目只读扫描；扫描进程不导入 API 应用。"""
from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
import json
import math
import sys
import time

from utils.runtime.process_deadline import run_isolated_json


class ProjectGoneError(Exception):
    pass


class HealthScanRunner:
    def __init__(self, timeout_seconds: float = 60, max_concurrent: int = 2):
        self.timeout_seconds = timeout_seconds
        self._slots = asyncio.Semaphore(max_concurrent)

    async def scan(self, user_id: int, project_id: str, conninfo: str,
                   lock_timeout_seconds: float) -> dict:
        # 等待并发名额也计入总时限；等待中的请求不占用数据库资源。
        expires_at = time.monotonic() + self.timeout_seconds
        async with asyncio.timeout(self.timeout_seconds):
            async with self._slots:
                result = await run_isolated_json("utils.indexing.health_scan", {
                    "user_id": user_id, "project_id": project_id, "conninfo": conninfo,
                    "lock_timeout_seconds": lock_timeout_seconds,
                }, expires_at - time.monotonic())
        if result.get("error") == "not_found":
            raise ProjectGoneError("项目不存在或已被删除")
        if result.get("error") == "timeout":
            raise TimeoutError("索引检查超时，请稍后重试")
        if result.get("error"):
            raise RuntimeError("索引检查失败，详情见服务端日志")
        return result["snapshot"]


def _scan_project(request: dict) -> dict:
    # 模块导入、客户端初始化、磁盘读取、数据库操作及 JSON 序列化，
    # 都受父进程设定的总时限约束。
    expires_at = time.monotonic() + request["timeout_seconds"]
    import psycopg
    from psycopg.conninfo import conninfo_to_dict
    from rag.index_health import IndexHealth
    from utils.core.pg_lock import project_index_lock_key

    remaining = expires_at - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("检查初始化超时")
    statement_ms = max(1, math.ceil(remaining * 1000))
    lock_ms = max(1, math.ceil(min(remaining, request["lock_timeout_seconds"]) * 1000))
    options = conninfo_to_dict(request["conninfo"]).get("options", "")
    options = f"{options} -c statement_timeout={statement_ms} -c lock_timeout={lock_ms}".strip()
    # 扫描进程在同一个事务中持有项目锁和咨询锁。
    # 关闭连接或终止进程即可释放两种锁，无需额外的解锁请求。
    with psycopg.connect(
        request["conninfo"], connect_timeout=max(1, math.ceil(remaining)),
        options=options,
    ) as conn:
        conn.execute("SELECT pg_advisory_xact_lock_shared(%s)", (
            project_index_lock_key(request["user_id"], request["project_id"]),
        ))
        project = conn.execute(
            "SELECT id FROM projects WHERE id = %s AND user_id = %s AND deleted_at IS NULL FOR SHARE",
            (request["project_id"], request["user_id"]),
        ).fetchone()
        if project is None:
            return {"error": "not_found"}
        health = IndexHealth(request["user_id"], request["project_id"])
        try:
            snapshot = health.snapshot()
        finally:
            health.vector_store.vector_store.client.close()
    return {"snapshot": snapshot}


def _main():
    request = json.load(sys.stdin)
    with redirect_stdout(sys.stderr):
        try:
            result = _scan_project(request)
        except TimeoutError:
            result = {"error": "timeout"}
        except Exception:
            import logging
            logging.getLogger(__name__).exception("[索引健康检查] 检查进程失败")
            result = {"error": "failed"}
    sys.stdout.write(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    _main()
