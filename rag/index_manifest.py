"""PostgreSQL 索引清单及分块完整性校验；只支持当前格式。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass, replace
from importlib.metadata import version

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from utils.project_paths import knowledge_scope


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def current_index_version(config: dict) -> str:
    """只跟踪影响解析、分块及嵌入语义的配置和依赖。"""
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
        "dependencies": {
            **{name: version(name) for name in (
                "langchain-community", "langchain-text-splitters", "pypdf", "docx2txt",
            )},
            # 保留现有指纹的种子值，避免本次修复本身触发全库重建。
            # 这两项不再读取运行时版本；传输或存储客户端升级不改变索引内容。
            "langchain-milvus": "0.4.0", "httpx": "0.28.1",
        },
    }
    return text_sha256(json.dumps(recipe, sort_keys=True, ensure_ascii=False))


def project_collection_name(config: dict, user_id: int, project_id: str) -> str:
    user_id, project_id = knowledge_scope(user_id, project_id)
    prefix = config["collection_prefix"]
    if not isinstance(prefix, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", prefix):
        raise ValueError("collection_prefix 必须是以字母开头的字母、数字或下划线，最多 80 字符")
    return f"{prefix}_u{user_id}_p{uuid.UUID(project_id).hex}"


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

    @staticmethod
    def setup(conn) -> None:
        """由服务启动事务初始化结构，普通读写不执行 DDL。"""
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS indexed_source_files (
                namespace TEXT NOT NULL,
                source TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                size_bytes BIGINT NOT NULL,
                mtime_ns BIGINT NOT NULL,
                PRIMARY KEY (namespace, source)
            )
        """)
        # File observations may be refreshed by health checks even after a
        # failed build. Publication must have a separate, transactional pointer.
        # setup is serialized by the caller's PostgreSQL setup advisory lock.
        migrate_publications = conn.execute(
            "SELECT to_regclass('published_source_files') IS NULL"
        ).fetchone()[0]
        conn.execute("""
            CREATE TABLE IF NOT EXISTS published_source_files (
                namespace TEXT NOT NULL,
                source TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                PRIMARY KEY (namespace, source),
                FOREIGN KEY (namespace, content_sha256)
                    REFERENCES index_manifests(namespace, content_sha256) ON DELETE CASCADE
            )
        """)
        if migrate_publications:
            # Adopt only file mappings already backed by a ready manifest.
            # Do not recreate pointers from observations on later startups.
            conn.execute("""
                INSERT INTO published_source_files (namespace, source, content_sha256)
                SELECT f.namespace, f.source, f.content_sha256
                FROM indexed_source_files f JOIN index_manifests m
                  ON m.namespace = f.namespace AND m.content_sha256 = f.content_sha256
                WHERE m.state = 'ready'
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
            if manifest.state == "ready":
                self._record_files(conn, {manifest.source: manifest.content_sha256})
                self._publish_file(conn, manifest.source, manifest.content_sha256)

    def _publish_file(self, conn, source: str, digest: str) -> None:
        conn.execute("""
            INSERT INTO published_source_files (namespace, source, content_sha256)
            VALUES (%s, %s, %s)
            ON CONFLICT (namespace, source) DO UPDATE SET
                content_sha256 = EXCLUDED.content_sha256
        """, (self.namespace, source, digest))

    def publish_existing_file(self, source: str, digest: str) -> None:
        """Publish another file reusing an already verified content index.

        The caller holds the project write lock and has validated that the
        manifest and its chunks are current before taking this shortcut.
        """
        with self._connect() as conn:
            record = conn.execute("""
                SELECT state FROM index_manifests
                WHERE namespace = %s AND content_sha256 = %s FOR SHARE
            """, (self.namespace, digest)).fetchone()
            if record is None or record["state"] != "ready":
                raise ValueError("只能发布已校验就绪的索引")
            self._record_files(conn, {source: digest})
            self._publish_file(conn, source, digest)

    def published_index_ids(self, index_version: str) -> list[str]:
        """Read the committed search allowlist; storage errors must propagate."""
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT DISTINCT m.index_id
                FROM published_source_files f JOIN index_manifests m
                  ON m.namespace = f.namespace AND m.content_sha256 = f.content_sha256
                WHERE f.namespace = %s AND m.state = 'ready' AND m.index_version = %s
                ORDER BY m.index_id
            """, (self.namespace, index_version)).fetchall()
        return [row["index_id"] for row in rows]

    def published_files(self) -> dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT source, content_sha256 FROM published_source_files WHERE namespace = %s
            """, (self.namespace,)).fetchall()
        return {row["source"]: row["content_sha256"] for row in rows}

    def _record_files(self, conn, files: dict[str, str | None]) -> None:
        for path, digest in files.items():
            if digest is None:
                continue
            info = os.stat(path)
            conn.execute("""
                INSERT INTO indexed_source_files (namespace, source, content_sha256, size_bytes, mtime_ns)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (namespace, source) DO UPDATE SET
                    content_sha256 = EXCLUDED.content_sha256,
                    size_bytes = EXCLUDED.size_bytes, mtime_ns = EXCLUDED.mtime_ns
            """, (self.namespace, path, digest, info.st_size, info.st_mtime_ns))

    def record_files(self, files: dict[str, str | None]) -> None:
        """入库或定时扫描已计算摘要后记录文件元数据，文件列表不再重复读取正文。"""
        with self._connect() as conn:
            self._record_files(conn, files)

    def file_statuses(self, paths: list[str], index_version: str) -> list[dict]:
        """返回已记录的入库状态；完整性由独立的深度检查报告表达。"""
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT f.source, f.size_bytes, f.mtime_ns, m.state, m.index_version,
                       p.source AS published_source
                FROM indexed_source_files f LEFT JOIN index_manifests m
                  ON m.namespace = f.namespace AND m.content_sha256 = f.content_sha256
                LEFT JOIN published_source_files p ON p.namespace = f.namespace
                  AND p.source = f.source AND p.content_sha256 = f.content_sha256
                WHERE f.namespace = %s
            """, (self.namespace,)).fetchall()
        records = {os.path.normcase(row["source"]): row for row in rows}
        files = []
        for path in paths:
            try:
                info = os.stat(path)
            except FileNotFoundError:
                continue  # 文件可在目录枚举后被并发删除或替换。
            record = records.get(os.path.normcase(path))
            indexed = None if record is None else (
                record["state"] == "ready" and record["index_version"] == index_version
                and record["published_source"] is not None
                and record["size_bytes"] == info.st_size and record["mtime_ns"] == info.st_mtime_ns
            )
            files.append({"name": os.path.basename(path), "path": path, "indexed": indexed})
        return files

    def forget_file(self, path: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM published_source_files WHERE namespace = %s AND source = %s",
                         (self.namespace, path))
            conn.execute("DELETE FROM indexed_source_files WHERE namespace = %s AND source = %s",
                         (self.namespace, path))

    def delete(self, content_sha256: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM index_manifests WHERE namespace = %s AND content_sha256 = %s",
                         (self.namespace, content_sha256))

    def delete_all(self) -> None:
        """只删除当前项目的索引清单。"""
        with self._connect() as conn:
            conn.execute("DELETE FROM indexed_source_files WHERE namespace = %s", (self.namespace,))
            conn.execute("DELETE FROM index_manifests WHERE namespace = %s", (self.namespace,))
