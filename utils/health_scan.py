"""Bounded, read-only project scans. Workers never import the API application."""
from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
import json
import math
import sys
import time

from utils.process_deadline import run_isolated_json


class ProjectGoneError(Exception):
    pass


class HealthScanRunner:
    def __init__(self, timeout_seconds: float = 60, max_concurrent: int = 4):
        self.timeout_seconds = timeout_seconds
        self._slots = asyncio.Semaphore(max(1, max_concurrent))

    async def scan(self, user_id: int, project_id: str, conninfo: str,
                   lock_timeout_seconds: float) -> dict:
        # Admission wait is part of the deadline; waiting requests own no DB resources.
        expires_at = time.monotonic() + self.timeout_seconds
        async with asyncio.timeout(self.timeout_seconds):
            async with self._slots:
                result = await run_isolated_json("utils.health_scan", {
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
    # Imports, client initialization, disk reads, database operations and JSON
    # serialization are all covered by the parent's process deadline.
    expires_at = time.monotonic() + request["timeout_seconds"]
    import psycopg
    from psycopg.conninfo import conninfo_to_dict
    from rag.index_health import IndexHealth
    from utils.pg_lock import project_index_lock_key

    remaining = expires_at - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("检查初始化超时")
    statement_ms = max(1, math.ceil(remaining * 1000))
    lock_ms = max(1, math.ceil(min(remaining, request["lock_timeout_seconds"]) * 1000))
    options = conninfo_to_dict(request["conninfo"]).get("options", "")
    options = f"{options} -c statement_timeout={statement_ms} -c lock_timeout={lock_ms}".strip()
    # The worker owns one transaction for the project/advisory locks. Closing the
    # connection (including process termination) releases both without an unlock RPC.
    with psycopg.connect(
        request["conninfo"], connect_timeout=max(1, math.ceil(remaining)),
        options=options,
    ) as conn:
        conn.execute("SELECT pg_advisory_xact_lock_shared(%s)", (
            project_index_lock_key(request["user_id"], request["project_id"]),
        ))
        project = conn.execute(
            "SELECT id FROM projects WHERE id = %s AND user_id = %s FOR SHARE",
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
