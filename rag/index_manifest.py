"""PostgreSQL 索引清单及分块完整性校验；只支持当前格式。"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, replace
from importlib.metadata import version

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from utils.pg_lock import PG_LOCK_KEY_SETUP, exclusive_section


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def current_index_version(config: dict) -> str:
    """解析、分块、嵌入配置或依赖变化均使旧清单失效。修改管线代码时递增 pipeline。"""
    recipe = {
        "pipeline": 2,
        "revision": config["index_version"],
        "embedding_model": config["embedding_model"],
        "embedding_endpoint": config["embedding_endpoint"],
        "vector_fields": ["vector", "sparse"],
        "bm25_analyzer": config["bm25_analyzer"],
        "chunk_size": config["chunk_size"],
        "chunk_overlap": config["chunk_overlap"],
        "separators": config["separators"],
        "dependencies": {name: version(name) for name in (
            "langchain-community", "langchain-text-splitters", "langchain-milvus",
            "httpx", "pypdf", "docx2txt",
        )},
    }
    return text_sha256(json.dumps(recipe, sort_keys=True, ensure_ascii=False))


@dataclass(frozen=True)
class IndexManifest:
    content_sha256: str
    source: str
    index_version: str
    index_id: str
    expected_chunks: int
    chunk_hashes: tuple[str, ...]
    state: str = "ready"

    def ready(self) -> IndexManifest:
        return replace(self, state="ready")


def chunks_match(manifest: IndexManifest, rows: list[dict]) -> bool:
    """比较块数、唯一序号、代次和实际文本；不能只信任向量中的哈希元数据。"""
    expected = manifest.expected_chunks
    if expected <= 0 or len(manifest.chunk_hashes) != expected or len(rows) != expected:
        return False
    seen = set()
    for row in rows:
        ordinal = row.get("chunk_index")
        if type(ordinal) is not int or not 0 <= ordinal < expected or ordinal in seen:
            return False
        seen.add(ordinal)
        text = row.get("text")
        if (
            row.get("source") != manifest.source
            or row.get("content_sha256") != manifest.content_sha256
            or row.get("index_version") != manifest.index_version
            or row.get("index_id") != manifest.index_id
            or row.get("chunk_sha256") != manifest.chunk_hashes[ordinal]
            or not isinstance(text, str)
            or text_sha256(text) != manifest.chunk_hashes[ordinal]
        ):
            return False
    return True


class ManifestStore:
    """每次写入使用独立事务；业务操作由 API 的索引咨询锁串行化。"""

    def __init__(self, config: dict):
        self.conninfo = os.getenv(
            "PG_CONNECTION_STRING", "postgresql://postgres:postgres@localhost:5432/shallow_ask_deep_answer"
        )
        self.namespace = text_sha256(json.dumps([
            config["milvus_uri"], config["collection_name"],
        ]))
        with exclusive_section(self.conninfo, PG_LOCK_KEY_SETUP):
            with self._connect() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS index_manifests (
                        namespace TEXT NOT NULL,
                        content_sha256 TEXT NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
                        source TEXT NOT NULL,
                        index_version TEXT NOT NULL,
                        index_id TEXT NOT NULL,
                        expected_chunks INTEGER NOT NULL CHECK (expected_chunks > 0),
                        chunk_hashes JSONB NOT NULL CHECK (jsonb_typeof(chunk_hashes) = 'array'),
                        state TEXT NOT NULL CHECK (state IN ('building', 'ready')),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        PRIMARY KEY (namespace, content_sha256),
                        CHECK (jsonb_array_length(chunk_hashes) = expected_chunks)
                    )
                """)

    def _connect(self):
        return psycopg.connect(self.conninfo, connect_timeout=5, row_factory=dict_row)

    def all(self) -> dict[str, IndexManifest]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT content_sha256, source, index_version, index_id,
                       expected_chunks, chunk_hashes, state
                FROM index_manifests WHERE namespace = %s
            """, (self.namespace,)).fetchall()
        return {row["content_sha256"]: IndexManifest(
            **{**row, "chunk_hashes": tuple(row["chunk_hashes"])}
        ) for row in rows}

    def put(self, manifest: IndexManifest) -> None:
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO index_manifests
                    (namespace, content_sha256, source, index_version, index_id,
                     expected_chunks, chunk_hashes, state)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (namespace, content_sha256) DO UPDATE SET
                    source = EXCLUDED.source, index_version = EXCLUDED.index_version,
                    index_id = EXCLUDED.index_id, expected_chunks = EXCLUDED.expected_chunks,
                    chunk_hashes = EXCLUDED.chunk_hashes, state = EXCLUDED.state,
                    updated_at = now()
            """, (self.namespace, manifest.content_sha256, manifest.source,
                  manifest.index_version, manifest.index_id, manifest.expected_chunks,
                  Jsonb(list(manifest.chunk_hashes)), manifest.state))

    def delete(self, content_sha256: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM index_manifests WHERE namespace = %s AND content_sha256 = %s",
                         (self.namespace, content_sha256))

    def delete_all(self) -> None:
        """只删除当前项目的索引清单。"""
        with self._connect() as conn:
            conn.execute("DELETE FROM index_manifests WHERE namespace = %s", (self.namespace,))
