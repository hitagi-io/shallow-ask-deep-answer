"""PostgreSQL 索引清单及分块完整性校验；只支持当前格式。"""
from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, replace
from importlib.metadata import version

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from utils.core.project_paths import knowledge_scope
from utils.documents.knowledge_files import document_format
from utils.documents.file_listing import KnowledgeFile
from utils.retrieval.source_evidence import chunk_signature, text_digest
from utils.indexing.progress import UnitProgressCallback


def current_index_version(config: dict) -> str:
    """只跟踪影响解析、分块及嵌入语义的配置和依赖。"""
    recipe = {
        "revision": config["index_version"],
        "embedding_model": config["embedding_model"],
        "embedding_endpoint": config["embedding_endpoint"],
        "vector_fields": ["vector", "sparse"],
        "bm25_analyzer": config["bm25_analyzer"],
        "chunk_size": config["chunk_size"],
        "chunk_overlap": config["chunk_overlap"],
        "separators": config["separators"],
        "parsing": config["parsing"],
        "dependencies": {name: version(name) for name in (
            "langchain-text-splitters", "pdfplumber", "pdfminer.six", "pypdfium2", "python-docx",
            "python-pptx", "openpyxl", "Pillow", "markdown-it-py", "rapidocr", "onnxruntime",
            "opencv-python-headless", "numpy",
            "lxml", "shapely", "pyclipper",
        )},
    }
    return text_digest(json.dumps(recipe, sort_keys=True, ensure_ascii=False))


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
    source_format: str
    index_version: str
    index_id: str
    expected_chunks: int
    chunk_hashes: tuple[str, ...]
    state: str = "ready"

    @property
    def identity(self) -> tuple[str, str]:
        return self.content_sha256, self.source_format

    def ready(self) -> IndexManifest:
        return replace(self, state="ready")


def chunks_match(manifest: IndexManifest, rows: list[dict], *, on_progress: UnitProgressCallback | None = None) -> bool:
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
            or row.get("source_format") != manifest.identity[1]
            or not isinstance(text, str)
            or text_digest(text) != row.get("chunk_sha256")
        ):
            return False
        try:
            if chunk_signature(text, row) != manifest.chunk_hashes[ordinal]:
                return False
        except (KeyError, TypeError, ValueError):
            return False
        if on_progress is not None:
            on_progress(len(seen), expected)
    return True


class ManifestStore:
    """每次写入使用独立事务；业务操作由 API 的索引咨询锁串行化。"""

    def __init__(self, config: dict):
        self.conninfo = os.getenv(
            "PG_CONNECTION_STRING", "postgresql://postgres:postgres@localhost:5432/shallow_ask_deep_answer"
        )
        self.namespace = text_digest(json.dumps([
            config["milvus_uri"], config["collection_name"],
        ]))

    @staticmethod
    def setup(conn) -> None:
        """由服务启动事务初始化结构，普通读写不执行 DDL。"""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS index_manifests (
                namespace TEXT NOT NULL,
                content_sha256 TEXT NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
                source_format TEXT NOT NULL,
                source TEXT NOT NULL,
                index_version TEXT NOT NULL,
                index_id TEXT NOT NULL,
                expected_chunks INTEGER NOT NULL CHECK (expected_chunks > 0),
                chunk_hashes JSONB NOT NULL CHECK (jsonb_typeof(chunk_hashes) = 'array'),
                state TEXT NOT NULL CHECK (state IN ('building', 'ready')),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (namespace, content_sha256, source_format),
                CHECK (jsonb_array_length(chunk_hashes) = expected_chunks)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS indexed_source_files (
                namespace TEXT NOT NULL,
                source TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                source_format TEXT NOT NULL,
                size_bytes BIGINT NOT NULL,
                mtime_ns BIGINT NOT NULL,
                PRIMARY KEY (namespace, source)
            )
        """)
        # 即使构建失败，健康检查仍可能更新文件观察记录，
        # 因此发布版本必须使用独立且受事务保护的指针。
        # 调用方通过 PostgreSQL 初始化咨询锁串行执行 setup。
        conn.execute("""
            CREATE TABLE IF NOT EXISTS published_source_files (
                namespace TEXT NOT NULL,
                source TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                source_format TEXT NOT NULL,
                PRIMARY KEY (namespace, source),
                FOREIGN KEY (namespace, content_sha256, source_format)
                    REFERENCES index_manifests(namespace, content_sha256, source_format) ON DELETE CASCADE
            )
        """)

    def _connect(self):
        return psycopg.connect(self.conninfo, connect_timeout=5, row_factory=dict_row)

    def all(self) -> dict[tuple[str, str], IndexManifest]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT content_sha256, source, source_format, index_version, index_id,
                       expected_chunks, chunk_hashes, state
                FROM index_manifests WHERE namespace = %s
            """, (self.namespace,)).fetchall()
        records = [IndexManifest(**{**row, "chunk_hashes": tuple(row["chunk_hashes"])}) for row in rows]
        return {record.identity: record for record in records}

    def put(self, manifest: IndexManifest) -> None:
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO index_manifests
                    (namespace, content_sha256, source_format, source, index_version, index_id,
                     expected_chunks, chunk_hashes, state)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (namespace, content_sha256, source_format) DO UPDATE SET
                    source = EXCLUDED.source, index_version = EXCLUDED.index_version,
                    index_id = EXCLUDED.index_id, expected_chunks = EXCLUDED.expected_chunks,
                    chunk_hashes = EXCLUDED.chunk_hashes, state = EXCLUDED.state,
                    updated_at = now()
            """, (self.namespace, manifest.content_sha256, manifest.identity[1], manifest.source,
                  manifest.index_version, manifest.index_id, manifest.expected_chunks,
                  Jsonb(list(manifest.chunk_hashes)), manifest.state))
            if manifest.state == "ready":
                self._record_files(conn, {manifest.source: manifest.content_sha256})
                self._publish_file(conn, manifest.source, manifest.content_sha256)

    def _publish_file(self, conn, source: str, digest: str) -> None:
        conn.execute("""
            INSERT INTO published_source_files (namespace, source, content_sha256, source_format)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (namespace, source) DO UPDATE SET
                content_sha256 = EXCLUDED.content_sha256, source_format = EXCLUDED.source_format
        """, (self.namespace, source, digest, document_format(source)))

    def publish_existing_file(self, source: str, digest: str) -> None:
        """复用已校验的内容索引，为另一个文件发布索引版本。

        调用方须持有项目写锁，并已确认清单及其分块仍有效，才能复用。
        """
        with self._connect() as conn:
            record = conn.execute("""
                SELECT state FROM index_manifests
                WHERE namespace = %s AND content_sha256 = %s AND source_format = %s FOR SHARE
            """, (self.namespace, digest, document_format(source))).fetchone()
            if record is None or record["state"] != "ready":
                raise ValueError("只能发布已校验就绪的索引")
            self._record_files(conn, {source: digest})
            self._publish_file(conn, source, digest)

    def published_index_ids(self, index_version: str) -> list[str]:
        """读取已提交的检索批次白名单；存储错误必须继续抛出。"""
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT DISTINCT m.index_id
                FROM published_source_files f JOIN index_manifests m
                  ON m.namespace = f.namespace AND m.content_sha256 = f.content_sha256
                  AND m.source_format = f.source_format
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
                INSERT INTO indexed_source_files (namespace, source, content_sha256, source_format, size_bytes, mtime_ns)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (namespace, source) DO UPDATE SET
                    content_sha256 = EXCLUDED.content_sha256,
                    source_format = EXCLUDED.source_format,
                    size_bytes = EXCLUDED.size_bytes, mtime_ns = EXCLUDED.mtime_ns
            """, (self.namespace, path, digest, document_format(path), info.st_size, info.st_mtime_ns))

    def record_files(self, files: dict[str, str | None]) -> None:
        """入库或定时扫描已计算摘要后记录文件元数据，文件列表不再重复读取正文。"""
        with self._connect() as conn:
            self._record_files(conn, files)

    def file_statuses(self, paths: tuple[str, ...], index_version: str) -> list[KnowledgeFile]:
        """返回已记录的入库状态；完整性由独立的深度检查报告表达。"""
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT f.source, f.size_bytes, f.mtime_ns, m.state, m.index_version,
                       p.source AS published_source
                FROM indexed_source_files f LEFT JOIN index_manifests m
                  ON m.namespace = f.namespace AND m.content_sha256 = f.content_sha256
                  AND m.source_format = f.source_format
                LEFT JOIN published_source_files p ON p.namespace = f.namespace
                  AND p.source = f.source AND p.content_sha256 = f.content_sha256
                  AND p.source_format = f.source_format
                WHERE f.namespace = %s
            """, (self.namespace,)).fetchall()
        records = {os.path.normcase(row["source"]): row for row in rows}
        files: list[KnowledgeFile] = []
        for path in paths:
            try:
                info = os.stat(path)
            except FileNotFoundError:
                continue  # 文件可在目录枚举后被并发删除或替换。
            record = records.get(os.path.normcase(path))
            indexed = record is not None and (
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

    def delete(self, identity: tuple[str, str]) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM index_manifests WHERE namespace = %s AND content_sha256 = %s AND source_format = %s",
                         (self.namespace, *identity))

    def delete_all(self) -> None:
        """只删除当前项目的索引清单。"""
        with self._connect() as conn:
            conn.execute("DELETE FROM indexed_source_files WHERE namespace = %s", (self.namespace,))
            conn.execute("DELETE FROM index_manifests WHERE namespace = %s", (self.namespace,))
