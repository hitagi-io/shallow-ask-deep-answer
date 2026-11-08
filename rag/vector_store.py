from __future__ import annotations

import json
import os
import re
import time
import threading
import uuid
from collections import OrderedDict

from langchain_core.runnables import RunnableLambda
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_milvus import BM25BuiltInFunction, Milvus
from dotenv import load_dotenv

from rag.embeddings import DashScopeEmbeddings
from rag.index_manifest import IndexManifest, ManifestStore, chunks_match, current_index_version, text_sha256
from rag.retrieval import HybridRetriever
from utils.config_loader import vector_db_config
from utils.knowledge_files import list_supported_files, compute_file_sha256, get_document_loader
from utils.project_paths import get_knowledge_dir, knowledge_scope, scoped_knowledge_file
from utils.logging_config import get_logger

logger = get_logger(__name__)
load_dotenv()

_vector_stores: OrderedDict[tuple[int, str], VectorStore] = OrderedDict()
_lock = threading.Lock()
_MAX_CACHED_PROJECTS = 128
_INDEX_MAX_ATTEMPTS = 3
_INDEX_RETRY_SLEEP = 1.0
_INDEX_FAIL_GENERIC = "入库失败，详见服务端日志"


def get_vector_store(user_id: int, project_id: str) -> "VectorStore":
    scope = knowledge_scope(user_id, project_id)
    with _lock:
        if scope not in _vector_stores:
            _vector_stores[scope] = VectorStore(*scope)
            if len(_vector_stores) > _MAX_CACHED_PROJECTS:
                _vector_stores.popitem(last=False)
        _vector_stores.move_to_end(scope)
        return _vector_stores[scope]


class VectorStore:
    _ITER_BATCH_SIZE = 1000

    def __init__(self, user_id: int, project_id: str):
        self.user_id, self.project_id = knowledge_scope(user_id, project_id)
        self.knowledge_dir = get_knowledge_dir(self.user_id, self.project_id)
        prefix = vector_db_config["collection_prefix"]
        if not isinstance(prefix, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", prefix):
            raise ValueError("collection_prefix 必须是以字母开头的字母、数字或下划线，最多 80 字符")
        collection_name = f"{prefix}_u{self.user_id}_p{uuid.UUID(self.project_id).hex}"
        self.manifests = ManifestStore({**vector_db_config, "collection_name": collection_name})
        self.index_version = current_index_version(vector_db_config)
        self.vector_store = Milvus(
            collection_name=collection_name,
            embedding_function=DashScopeEmbeddings(vector_db_config),
            vector_field=["vector", "sparse"],
            builtin_function=BM25BuiltInFunction(
                input_field_names="text", output_field_names="sparse",
                analyzer_params=vector_db_config["bm25_analyzer"],
                function_name="knowledge_bm25",
            ),
            connection_args={
                "uri": vector_db_config["milvus_uri"],
                "pool_size": vector_db_config.get("pool_size", 50),
            },
            drop_old=False,
            auto_id=False,
            enable_dynamic_field=True,
            consistency_level="Strong",
        )
        self.splitter = RecursiveCharacterTextSplitter(
            chunk_size=vector_db_config["chunk_size"],
            chunk_overlap=vector_db_config["chunk_overlap"],
            separators=vector_db_config["separators"],
            length_function=len,
        )
        self._retriever = RunnableLambda(HybridRetriever(self).search)
        self._store_lock = threading.RLock()

    def get_retriever(self):
        return self._retriever

    def ensure_search_ready(self):
        with self._store_lock:
            vs = self.vector_store
            # 另一个 worker 可能刚为之前的空项目创建集合，补齐客户端字段与检索配置。
            if vs.client.has_collection(collection_name=vs.collection_name) and not vs.fields:
                vs._init()

    def drop_project_index(self) -> None:
        """调用方持有项目排他锁并禁止新生成后，清除此项目的向量及清单。"""
        with self._store_lock:
            vs = self.vector_store
            if vs.client.has_collection(collection_name=vs.collection_name):
                vs.client.drop_collection(collection_name=vs.collection_name)
            self.manifests.delete_all()
            with _lock:
                _vector_stores.pop((self.user_id, self.project_id), None)

    def chunk_rows(self, expression: str = "") -> list[dict]:
        """强一致性游标遍历；空集合属于正常初始状态，连接错误继续抛出。"""
        vs = self.vector_store
        if not vs.client.has_collection(collection_name=vs.collection_name):
            return []
        iterator = vs.client.query_iterator(
            collection_name=vs.collection_name,
            batch_size=self._ITER_BATCH_SIZE,
            filter=expression,
            output_fields=["pk", "text", "source", "content_sha256", "index_version",
                           "index_id", "chunk_index", "chunk_sha256"],
            consistency_level="Strong",
        )
        try:
            rows = []
            while batch := iterator.next():
                # 动态元数据损坏时仍要报告/清理该主键，不能让异常类型中断全库检查。
                for row in batch:
                    for field in ("content_sha256", "source"):
                        if not isinstance(row.get(field), str):
                            row[field] = None
                rows.extend(batch)
            return rows
        finally:
            iterator.close()

    def _content_rows(self, sha256: str) -> list[dict]:
        return self.chunk_rows(f"content_sha256 == {json.dumps(sha256)}")

    def manifest_is_current(self, manifest: IndexManifest, rows: list[dict] | None = None) -> bool:
        source = scoped_knowledge_file(self.knowledge_dir, manifest.source)
        if (manifest.state != "ready" or manifest.index_version != self.index_version
                or compute_file_sha256(source) != manifest.content_sha256):
            return False
        return chunks_match(manifest, self._content_rows(manifest.content_sha256) if rows is None else rows)

    def index_pending_files(self, file_paths=None) -> list[dict]:
        """补建未入库或校验失败的内容；成功后清理同路径的过期版本。"""
        paths = file_paths if file_paths is not None else list_supported_files(
            self.knowledge_dir, tuple(vector_db_config["allowed_types"])
        )
        failures = []
        with self._store_lock:
            for path in paths:
                try:
                    result = self.index_file(path)
                    if result["ok"]:
                        self._retire_source_versions(path, result["content_sha256"])
                    else:
                        failures.append({"file": path, "error": result["error"]})
                except Exception:
                    logger.exception("[向量库] 入库失败 file=%s", os.path.basename(path))
                    failures.append({"file": path, "error": _INDEX_FAIL_GENERIC})
        return failures

    def index_file(self, file_path: str, *, force: bool = False) -> dict:
        """写入独立代次 → 验证每块实际文本 → 提交清单 → 删除旧代次。

        新内容先保存 building 清单，中断与部分写入仍可被健康检查发现。
        重建已有内容时保留旧清单和向量，失败只删除本次预分配的主键。
        调用方须持有 API 层的 PostgreSQL 索引写锁。
        """
        file_path = scoped_knowledge_file(self.knowledge_dir, file_path)
        result = {"file": file_path, "chunks": None, "ok": False, "error": _INDEX_FAIL_GENERIC}
        with self._store_lock:
            sha256 = compute_file_sha256(file_path)
            if sha256 is None:
                return {**result, "error": "SHA-256 计算失败"}
            result["content_sha256"] = sha256
            old = self.manifests.all().get(sha256)
            if not force and old and self.manifest_is_current(old):
                return {**result, "chunks": old.expected_chunks, "ok": True, "error": None}
            loader = get_document_loader(file_path)
            if loader is None:
                return {**result, "error": "不支持的文件类型"}
            try:
                documents = self.splitter.split_documents(loader(file_path))
            except Exception:
                logger.exception("[向量库] 文件加载或分块失败 file=%s", file_path)
                return result
            if not documents:
                return {**result, "error": "文件分割后为空"}
            if compute_file_sha256(file_path) != sha256:
                return {**result, "error": "解析期间文件发生变化，请重试"}
            hashes = tuple(text_sha256(doc.page_content) for doc in documents)
            if old is None or old.state != "ready":
                self.manifests.put(IndexManifest(sha256, file_path, self.index_version,
                                                uuid.uuid4().hex, len(documents), hashes, "building"))
            texts = [doc.page_content for doc in documents]
            try:
                # 每批请求由嵌入适配器有限重试。写库重试复用这些向量，避免再次计费。
                embeddings = self.vector_store.embedding_func.embed_documents(texts)
            except Exception:
                logger.exception("[向量库] 嵌入失败，停止本次入库 file=%s", os.path.basename(file_path))
                return result
            for attempt in range(1, _INDEX_MAX_ATTEMPTS + 1):
                manifest = IndexManifest(sha256, file_path, self.index_version,
                                         uuid.uuid4().hex, len(documents), hashes, "building")
                ids = [uuid.uuid4().hex for _ in documents]
                for ordinal, doc in enumerate(documents):
                    doc.metadata.update({
                        "source": file_path, "content_sha256": sha256,
                        "index_version": manifest.index_version, "index_id": manifest.index_id,
                        "chunk_index": ordinal, "chunk_sha256": hashes[ordinal],
                    })
                try:
                    if old is None or old.state != "ready":
                        self.manifests.put(manifest)
                    if not self.vector_store.client.has_collection(collection_name=self.vector_store.collection_name):
                        # 集合被外部删除后，LangChain 的 collection view 仍可能缓存旧对象。
                        self.vector_store.col = None
                        self.vector_store.fields = []
                    self.vector_store.add_embeddings(
                        texts=texts, embeddings=embeddings,
                        metadatas=[doc.metadata for doc in documents], ids=ids,
                    )
                    rows = self.chunk_rows(f"index_id == {json.dumps(manifest.index_id)}")
                    if not chunks_match(manifest, rows):
                        raise RuntimeError("写入后的分块数、序号或内容哈希校验失败")
                    if compute_file_sha256(file_path) != sha256:
                        raise RuntimeError("入库期间源文件发生变化")
                    self.manifests.put(manifest.ready())
                except Exception:
                    logger.exception("[向量库] 入库失败 file=%s 第 %s/%s 次尝试",
                                     file_path, attempt, _INDEX_MAX_ATTEMPTS)
                    try:
                        self._delete_pks(ids)
                    except Exception:
                        logger.exception("[向量库] 清理本次写入失败，保留清单供健康检查修复")
                        return result
                    if attempt < _INDEX_MAX_ATTEMPTS:
                        time.sleep(_INDEX_RETRY_SLEEP)
                    continue

                # 清单已提交，此后清理失败不能回滚磁盘到旧内容。
                result = {**result, "chunks": len(documents), "ok": True,
                          "error": None, "committed": True}
                try:
                    self._delete_pks([row["pk"] for row in self._content_rows(sha256)
                                      if row.get("index_id") != manifest.index_id])
                except Exception:
                    logger.exception("[向量库] 新清单已提交，旧分块清理失败 file=%s", file_path)
                    return {**result, "ok": False, "error": "新版本已入库，但旧分块清理失败，请执行索引修复"}
                return result
            return result

    def pks_by_source(self, file_path: str) -> list:
        file_path = scoped_knowledge_file(self.knowledge_dir, file_path)
        return [row["pk"] for row in self.chunk_rows(f"source == {json.dumps(file_path)}")]

    def delete_vectors_by_source(self, file_path: str) -> int:
        return self._delete_pks(self.pks_by_source(file_path))

    def _delete_pks(self, pks: list) -> int:
        if not pks or not self.vector_store.client.has_collection(
            collection_name=self.vector_store.collection_name
        ):
            return 0
        for i in range(0, len(pks), self._ITER_BATCH_SIZE):
            self.vector_store.delete(ids=pks[i:i + self._ITER_BATCH_SIZE])
        return len(pks)

    def _another_file_with_same_content(self, file_path: str, sha256: str) -> str | None:
        norm_self = os.path.normcase(os.path.abspath(file_path))
        for path in list_supported_files(self.knowledge_dir, tuple(vector_db_config["allowed_types"])):
            if os.path.normcase(os.path.abspath(path)) == norm_self:
                continue
            digest = compute_file_sha256(path)
            if digest is None:
                raise OSError(f"无法确认同内容副本，保留原索引：{path}")
            if digest == sha256:
                return path
        return None

    def _retire_source_versions(self, file_path: str, keep_sha256: str | None = None) -> None:
        """过期内容先转移到同内容副本；无副本时先删向量再删清单，失败仍可追踪。"""
        file_path = scoped_knowledge_file(self.knowledge_dir, file_path)
        for sha256, record in self.manifests.all().items():
            if os.path.normcase(record.source) != os.path.normcase(file_path) or sha256 == keep_sha256:
                continue
            survivor = self._another_file_with_same_content(file_path, sha256)
            if survivor:
                result = self.index_file(survivor)
                if not result["ok"]:
                    raise RuntimeError(f"同内容副本重建失败：{survivor}")
            else:
                self._delete_pks([row["pk"] for row in self._content_rows(sha256)])
                self.manifests.delete(sha256)
        self._delete_pks([row["pk"] for row in self.chunk_rows(f"source == {json.dumps(file_path)}")
                          if keep_sha256 is None or row.get("content_sha256") != keep_sha256])

    def delete_file_index(self, file_path: str) -> None:
        """调用方先删除磁盘文件；清单中的原始 SHA-256 不依赖文件仍存在。"""
        with self._store_lock:
            self._retire_source_versions(file_path)

    def replace_and_index_file(self, file_path: str, tmp_path: str) -> dict:
        """覆盖失败回滚源文件；提交新清单前始终保留旧向量和清单。"""
        file_path = scoped_knowledge_file(self.knowledge_dir, file_path)
        tmp_path = scoped_knowledge_file(self.knowledge_dir, tmp_path)
        with self._store_lock:
            existed = os.path.isfile(file_path)
            backup = os.path.join(os.path.dirname(file_path), f".bak_{uuid.uuid4().hex}")
            if existed:
                os.replace(file_path, backup)
            try:
                os.replace(tmp_path, file_path)
            except Exception:
                if existed:
                    os.replace(backup, file_path)
                raise
            try:
                result = self.index_file(file_path)
            except Exception:
                logger.exception("[向量库] 覆盖上传入库异常 file=%s", file_path)
                result = {"file": file_path, "chunks": None, "ok": False, "error": _INDEX_FAIL_GENERIC}
            if not result["ok"] and not result.get("committed"):
                if existed:
                    os.replace(backup, file_path)
                return {**result, "overwritten": existed}
            try:
                self._retire_source_versions(file_path, result["content_sha256"])
            except Exception:
                logger.exception("[向量库] 覆盖后清理旧版本失败 file=%s", file_path)
                result = {**result, "ok": False, "error": "新版本已入库，但旧版本清理失败，请执行索引修复"}
            finally:
                if existed:
                    try:
                        os.remove(backup)
                    except OSError:
                        logger.exception("[向量库] 删除备份文件失败 file=%s", backup)
            return {**result, "overwritten": existed}
