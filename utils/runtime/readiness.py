"""有整体截止时间的存储就绪探测；独立进程拥有并回收全部连接。"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import json
import math
import sys
import time

from utils.core.logging_config import get_logger

logger = get_logger(__name__)


def _remaining(deadline: float) -> float:
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError("就绪检查超时")
    return seconds


def _result(started: float, error: str | None = None) -> dict:
    result = {"ok": error is None, "latency_ms": round((time.monotonic() - started) * 1000, 1)}
    if error:
        result["error"] = error
    return result


def _probe_postgres(conninfo: str, deadline: float) -> dict:
    """建连和 SQL 各自有限时；网络卡死由父进程的总期限兜底。"""
    started = time.monotonic()
    conn = None
    try:
        import psycopg
        from psycopg.conninfo import conninfo_to_dict

        seconds = _remaining(deadline)
        # startup options 使第一条 SQL 也受约束，并保留连接串原有的选项。
        options = conninfo_to_dict(conninfo).get("options", "")
        options = f"{options} -c statement_timeout={max(1, math.ceil(seconds * 1000))}".strip()
        conn = psycopg.connect(
            conninfo, autocommit=True, connect_timeout=max(1, math.ceil(seconds)), options=options,
        )
        # 建连消耗的时间不再分配给查询。
        conn.execute("SELECT set_config('statement_timeout', %s, false)",
                     (str(max(1, math.ceil(_remaining(deadline) * 1000))),))
        conn.execute("SELECT 1")
        _remaining(deadline)
        return _result(started)
    except TimeoutError:
        return _result(started, "数据库检查超时")
    except Exception:
        logger.exception("[就绪检查] PostgreSQL 探测失败")
        return _result(started, "数据库不可用")
    finally:
        if conn is not None:
            conn.close()


def _probe_milvus(uri: str, deadline: float) -> dict:
    started = time.monotonic()
    client = None
    try:
        from pymilvus import MilvusClient

        # dedicated 确保 close 真正关闭本探针的连接，不放回共享连接管理器。
        client = MilvusClient(uri=uri, timeout=_remaining(deadline), dedicated=True)
        client.list_collections(timeout=_remaining(deadline))
        _remaining(deadline)
        return _result(started)
    except TimeoutError:
        return _result(started, "向量库检查超时")
    except Exception:
        logger.exception("[就绪检查] Milvus 探测失败")
        return _result(started, "向量库不可用")
    finally:
        if client is not None:
            client.close()


def _worker_checks(payload: dict) -> dict:
    # 两项探测共享同一截止时间。即使驱动/close 卡住，父进程仍会终止并回收本进程。
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="readiness") as pool:
        postgres = pool.submit(_probe_postgres, payload["pg_conn"], payload["deadline"])
        milvus = pool.submit(_probe_milvus, payload["milvus_uri"], payload["deadline"])
        return {"postgres": postgres.result(), "milvus": milvus.result()}


async def check_readiness(pg_conn: str, milvus_uri: str, timeout_seconds: float) -> dict:
    """返回 postgres/milvus 检查结果；期限包含启动、探测及结果传输。"""
    from utils.runtime.process_deadline import run_isolated_json

    started = time.monotonic()
    try:
        return await run_isolated_json(
            "utils.runtime.readiness",
            {"pg_conn": pg_conn, "milvus_uri": milvus_uri, "deadline": started + timeout_seconds},
            timeout_seconds,
        )
    except asyncio.CancelledError:
        # runner 先终止并回收子进程，再传播取消；不能把客户端取消当作成功。
        raise
    except TimeoutError:
        return {name: _result(started, "就绪检查超时，暂未就绪") for name in ("postgres", "milvus")}
    except Exception:
        logger.exception("[就绪检查] 检查进程失败")
        return {name: _result(started, "就绪检查失败，暂未就绪") for name in ("postgres", "milvus")}


def main() -> None:
    payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    # 第三方库的输出不能混入返回协议；stdout 仅保留一份 JSON。
    with redirect_stdout(sys.stderr):
        checks = _worker_checks(payload)
    json.dump(checks, sys.stdout)


if __name__ == "__main__":
    main()
