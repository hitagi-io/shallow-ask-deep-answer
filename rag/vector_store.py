from __future__ import annotations

import json
import os
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from functools import partial
from typing import NotRequired, TypedDict

from langchain_core.runnables import RunnableLambda
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_milvus import BM25BuiltInFunction, Milvus
from dotenv import load_dotenv

from rag.embeddings import DashScopeEmbeddings
from rag.index_manifest import IndexManifest, ManifestStore, chunks_match, current_index_version, project_collection_name
from rag.retrieval import HybridRetriever
from utils.core.config_loader import vector_db_config
from utils.documents.knowledge_files import list_supported_files, compute_file_sha256, get_document_loader, document_format
from utils.retrieval.source_evidence import SOURCE_FIELDS, chunk_signature, split_located_documents, text_digest
from utils.core.project_paths import get_knowledge_dir, knowledge_scope, scoped_knowledge_file
from utils.core.logging_config import get_logger
from utils.indexing.progress import IndexProgress, IndexStage, ProgressCallback
from utils.indexing.recovery import UploadJournal, PublicationUncertain, UploadRecoveryResult
from utils.observability.tracing import span, traced
from utils.runtime.shutdown import ServiceStopping, check_shutdown, interruptible_lock

logger = get_logger(__name__)
load_dotenv()

_vector_stores: OrderedDict[tuple[int, str], VectorStore] = OrderedDict()
_lock = threading.Lock()
_MAX_CACHED_PROJECTS = 128
_INDEX_FAIL_GENERIC = "入库失败，详见服务端日志"


class IndexResult(TypedDict):
    file: str
    chunks: int | None
    ok: bool
    committed: bool
    error: str | None
    content_sha256: NotRequired[str]


class UploadedFileResult(IndexResult):
    overwritten: bool


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
    _INSERT_BATCH_SIZE = 1000

    def __init__(self, user_id: int, project_id: str):
        self.user_id, self.project_id = knowledge_scope(user_id, project_id)
        self.knowledge_dir = get_knowledge_dir(self.user_id, self.project_id)
        collection_name = project_collection_name(vector_db_config, self.user_id, self.project_id)
        self.manifests = ManifestStore({**vector_db_config, "collection_name": collection_name})
        self.index_version = current_index_version(vector_db_config)
        self.embeddings = DashScopeEmbeddings(vector_db_config)
        self.vector_store = Milvus(
            collection_name=collection_name,
            embedding_function=self.embeddings,
            vector_field=["vector", "sparse"],
            builtin_function=BM25BuiltInFunction(
                input_field_names="text", output_field_names="sparse",
                analyzer_params=vector_db_config["bm25_analyzer"],
                function_name="knowledge_bm25",
            ),
            connection_args={"uri": vector_db_config["milvus_uri"]},
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
            add_start_index=True,
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
                           "index_id", "chunk_index", "chunk_sha256", "source_format", *SOURCE_FIELDS],
            consistency_level="Strong",
        )
        try:
            rows = []
            while batch := iterator.next():
                # 动态元数据损坏时仍要报告/清理该主键，不能让异常类型中断全库检查。
                for row in batch:
                    for field in ("content_sha256", "source", "source_format"):
                        if not isinstance(row.get(field), str):
                            row[field] = None
                rows.extend(batch)
            return rows
        finally:
            iterator.close()

    def _content_rows(self, identity: tuple[str, str]) -> list[dict]:
        digest, kind = identity
        return self.chunk_rows(f"content_sha256 == {json.dumps(digest)} and source_format == {json.dumps(kind)}")

    def manifest_is_current(self, manifest: IndexManifest, rows: list[dict] | None = None) -> bool:
        source = scoped_knowledge_file(self.knowledge_dir, manifest.source)
        if (manifest.state != "ready" or manifest.index_version != self.index_version
                or compute_file_sha256(source) != manifest.content_sha256):
            return False
        return chunks_match(manifest, self._content_rows(manifest.identity) if rows is None else rows)

    def index_pending_files(self, file_paths=None) -> list[dict]:
        """补建未入库或校验失败的内容；成功后清理同路径的过期版本。"""
        paths = file_paths if file_paths is not None else list_supported_files(
            self.knowledge_dir, tuple(vector_db_config["allowed_types"])
        )
        failures = []
        with interruptible_lock(self._store_lock):
            for path in paths:
                check_shutdown()
                try:
                    result = self.index_file(path)
                    if result["ok"]:
                        self._retire_source_versions(path, result["content_sha256"])
                    else:
                        if result.get("content_sha256"):
                            self.manifests.record_files({path: result["content_sha256"]})
                        failures.append({"file": path, "error": result["error"]})
                except ServiceStopping:
                    raise
                except Exception:
                    logger.exception("[向量库] 入库失败 file=%s", os.path.basename(path))
                    failures.append({"file": path, "error": _INDEX_FAIL_GENERIC})
        return failures

    @traced("index.file")
    def index_file(self, file_path: str, *, force: bool = False,
                   on_progress: ProgressCallback | None = None, journal: UploadJournal | None = None) -> IndexResult:
        """写入独立代次 → 验证每块实际文本 → 提交清单 → 删除旧代次。

        新内容先保存 building 清单，中断与部分写入仍可被健康检查发现。
        重建已有内容时保留旧清单和向量，失败只删除本次预分配的主键。
        调用方须持有 API 层的 PostgreSQL 索引写锁。
        """
        file_path = scoped_knowledge_file(self.knowledge_dir, file_path)
        result: IndexResult = {"file": file_path, "chunks": None, "ok": False, "committed": False, "error": _INDEX_FAIL_GENERIC}

        def report(stage: IndexStage, completed: int, total: int) -> None:
            if on_progress is not None:
                on_progress(IndexProgress(stage=stage, completed=completed, total=total))

        with interruptible_lock(self._store_lock):
            report("preparing", 0, 1)
            sha256 = compute_file_sha256(file_path)
            if sha256 is None:
                return {**result, "error": "SHA-256 计算失败"}
            result["content_sha256"] = sha256
            loader = get_document_loader(file_path)
            if loader is None:
                return {**result, "error": "不支持的文件类型"}
            identity = (sha256, document_format(file_path))
            old = self.manifests.all().get(identity)
            if not force and old and self.manifest_is_current(old):
                report("finalizing", 0, 1)
                if journal is not None:
                    journal.advance("publishing", index_id=old.index_id)
                self._confirm_publication(file_path, sha256, old.index_id,
                                          lambda: self.manifests.publish_existing_file(file_path, sha256))
                if journal is not None:
                    self._record_published(journal)
                return {**result, "chunks": old.expected_chunks, "ok": True, "error": None, "committed": True}
            try:
                report("parsing", 0, 1)
                parsed = loader(file_path, on_progress=partial(report, "parsing"))
                report("splitting", 0, max(1, len(parsed)))
                documents = split_located_documents(parsed, self.splitter, on_progress=partial(report, "splitting"))
                check_shutdown()
            except ServiceStopping as exc:
                return {**result, "error": str(exc)}
            except ValueError as exc:
                logger.warning("[向量库] 文件内容无法解析 file=%s: %s", file_path, exc)
                return {**result, "error": str(exc)}
            except Exception:
                logger.exception("[向量库] 文件加载或分块失败 file=%s", file_path)
                return result
            if not documents:
                return {**result, "error": "文件分割后为空"}
            if compute_file_sha256(file_path) != sha256:
                return {**result, "error": "解析期间文件发生变化，请重试"}
            hashes = tuple(chunk_signature(doc.page_content, doc.metadata) for doc in documents)
            manifest = IndexManifest(sha256, file_path, identity[1], self.index_version,
                                     uuid.uuid4().hex, len(documents), hashes, "building")
            if journal is not None:
                journal.advance("indexing", index_id=manifest.index_id)
            if old is None or old.state != "ready":
                self.manifests.put(manifest)
            texts = [doc.page_content for doc in documents]
            try:
                # 每批请求由嵌入适配器有限重试。
                embeddings = self.embeddings.embed_documents(texts, on_progress=partial(report, "embedding"))
                check_shutdown()
            except ServiceStopping as exc:
                return {**result, "error": str(exc)}
            except Exception:
                logger.exception("[向量库] 嵌入失败，停止本次入库 file=%s", os.path.basename(file_path))
                return result
            ids = [uuid.uuid4().hex for _ in documents]
            for ordinal, doc in enumerate(documents):
                doc.metadata.update({
                    "source": file_path, "content_sha256": sha256,
                    "source_format": identity[1],
                    "index_version": manifest.index_version, "index_id": manifest.index_id,
                    "chunk_index": ordinal, "chunk_sha256": text_digest(doc.page_content),
                })
            try:
                report("writing", 0, len(documents))
                if journal is not None:
                    journal.advance("writing")
                if not self.vector_store.client.has_collection(collection_name=self.vector_store.collection_name):
                    # 集合被外部删除后，LangChain 的 collection view 仍可能缓存旧对象。
                    self.vector_store.col = None
                    self.vector_store.fields = []
                # Milvus SDK 负责请求级重试；不要因参数或完整性错误重跑整次写入。
                # 沿用 SDK 的 1000 条批大小，只在实际写入成功后推进进度。
                for start in range(0, len(documents), self._INSERT_BATCH_SIZE):
                    check_shutdown()
                    end = min(start + self._INSERT_BATCH_SIZE, len(documents))
                    self.vector_store.add_embeddings(
                        texts=texts[start:end], embeddings=embeddings[start:end],
                        metadatas=[doc.metadata for doc in documents[start:end]], ids=ids[start:end],
                    )
                    report("writing", end, len(documents))
                report("verifying", 0, len(documents))
                rows = self.chunk_rows(f"index_id == {json.dumps(manifest.index_id)}")
                if not chunks_match(manifest, rows, on_progress=partial(report, "verifying")):
                    raise RuntimeError("写入后的分块数、序号或内容哈希校验失败")
                if compute_file_sha256(file_path) != sha256:
                    raise RuntimeError("入库期间源文件发生变化")
                # 提交后不再调用可能失败的进度写入，避免错误触发源文件回滚。
                report("finalizing", 0, 1)
            except Exception:
                logger.exception("[向量库] 入库失败 file=%s", file_path)
                try:
                    self._delete_pks(ids)
                except Exception:
                    logger.exception("[向量库] 清理本次写入失败，保留清单供健康检查修复")
                return result

            if journal is not None:
                journal.advance("publishing")
            try:
                self._confirm_publication(file_path, sha256, manifest.index_id,
                                          lambda: self.manifests.put(manifest.ready()))
            except PublicationUncertain:
                raise
            except Exception:
                # 已核对未发布；保留 building 清单供修复，清理只限本次代次。
                self._delete_pks(ids)
                raise
            if journal is not None:
                self._record_published(journal)

            # 清单已提交，此后清理失败不能回滚磁盘到旧内容。
            result = {**result, "chunks": len(documents), "ok": True,
                      "error": None, "committed": True}
            try:
                self._delete_pks([row["pk"] for row in self._content_rows(identity)
                                  if row.get("index_id") != manifest.index_id])
            except Exception:
                logger.exception("[向量库] 新清单已提交，旧分块清理失败 file=%s", file_path)
                return {**result, "ok": False, "error": "新版本已入库，但旧分块清理失败，请执行索引修复"}
            return result

    def _confirm_publication(self, source: str, digest: str, index_id: str, publish: Callable[[], None]) -> None:
        with span("index.publish", index_id=index_id, content_hash=digest):
            try:
                publish()
            except Exception as original:
                try:
                    published = self.manifests.is_published(source, digest, index_id)
                except Exception as verification:
                    raise PublicationUncertain("发布结果暂时无法确认，请执行索引修复；已保留文件和索引") from verification
                if not published:
                    raise original
                logger.warning("[索引] 发布回执异常，已核对目标版本提交成功 index_id=%s", index_id)

    @staticmethod
    def _record_published(journal: UploadJournal) -> None:
        try:
            journal.advance("published")
        except Exception as exc:
            raise PublicationUncertain("索引已发布，任务状态尚未确认，请执行索引修复") from exc

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
            if document_format(path) != document_format(file_path):
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
        for identity, record in self.manifests.all().items():
            sha256 = record.content_sha256
            if os.path.normcase(record.source) != os.path.normcase(file_path) or sha256 == keep_sha256:
                continue
            survivor = self._another_file_with_same_content(file_path, sha256)
            if survivor:
                result = self.index_file(survivor)
                if not result["ok"]:
                    raise RuntimeError(f"同内容副本重建失败：{survivor}")
            else:
                self._delete_pks([row["pk"] for row in self._content_rows(identity)])
                self.manifests.delete(identity)
        self._delete_pks([row["pk"] for row in self.chunk_rows(f"source == {json.dumps(file_path)}")
                          if keep_sha256 is None or row.get("content_sha256") != keep_sha256])

    def delete_file_index(self, file_path: str) -> None:
        """调用方先删除磁盘文件；清单中的原始 SHA-256 不依赖文件仍存在。"""
        with self._store_lock:
            self.manifests.forget_file(file_path)
            self._retire_source_versions(file_path)

    @traced("upload.replace")
    def replace_and_index_file(self, file_path: str, tmp_path: str, *, journal: UploadJournal,
                               on_progress: ProgressCallback | None = None) -> UploadedFileResult:
        """保存恢复意图后才移动文件；未确认发布结果时保留全部恢复材料。"""
        file_path = scoped_knowledge_file(self.knowledge_dir, file_path)
        tmp_path = scoped_knowledge_file(self.knowledge_dir, tmp_path)
        with interruptible_lock(self._store_lock):
            record = journal.prepare(self.knowledge_dir, file_path, tmp_path)
            try:
                journal.advance("replacing")
                if record.backup_path is not None:
                    os.replace(file_path, record.backup_path)
                os.replace(tmp_path, file_path)
                journal.advance("indexing")
                result = self.index_file(file_path, on_progress=on_progress, journal=journal)
            except PublicationUncertain:
                raise
            except Exception:
                # 即使异常出现在状态写回，恢复流程也先核对发布结果。
                self.recover_upload(journal)
                raise
            if not result["ok"] and not result["committed"]:
                self.recover_upload(journal)
                return {**result, "overwritten": record.old_hash is not None}
            try:
                self._finish_uploaded_version(journal)
            except Exception:
                logger.exception("[向量库] 新版本已入库，清理未完成")
                return {**result, "ok": False, "committed": True, "overwritten": record.old_hash is not None,
                        "error": "新版本已入库，清理未完成，请执行索引修复"}
            return {**result, "ok": True, "error": None, "committed": True, "overwritten": record.old_hash is not None}

    def _finish_uploaded_version(self, journal: UploadJournal) -> None:
        record = journal.validated_record(self.knowledge_dir)
        if record.index_id is None:
            raise RuntimeError("已发布上传缺少索引代次")
        if compute_file_sha256(record.destination) != record.new_hash:
            raise RuntimeError("源文件版本已变化，停止自动恢复")
        self._delete_pks([row["pk"] for row in self._content_rows((record.new_hash, document_format(record.destination)))
                          if row["index_id"] != record.index_id])
        self._retire_source_versions(record.destination, record.new_hash)
        if record.backup_path is not None and os.path.exists(record.backup_path):
            if compute_file_sha256(record.backup_path) != record.old_hash:
                raise RuntimeError("备份文件校验失败，停止清理")
            os.remove(record.backup_path)
        journal.advance("cleaned")

    @traced("upload.recover")
    def recover_upload(self, journal: UploadJournal) -> UploadRecoveryResult:
        """调用方持项目写锁；按磁盘及发布清单核对，重复恢复不重复覆盖。"""
        with interruptible_lock(self._store_lock):
            record = journal.validated_record(self.knowledge_dir)
            result: UploadRecoveryResult = {"ok": False, "committed": False, "overwritten": record.old_hash is not None,
                      "error": "上传已中断，原文件已恢复，请重新上传" if record.old_hash is not None
                               else "上传尚未发布，请重新上传或执行修复与补建"}
            if record.stage == "cleaned":
                return {**result, "ok": True, "committed": True, "error": None}
            if record.stage in ("rolled_back", "unpublished"):
                return result
            if record.index_id is not None:
                try:
                    published = self.manifests.is_published(record.destination, record.new_hash, record.index_id)
                except Exception as exc:
                    raise PublicationUncertain("无法核对发布结果，已保留文件和索引，请稍后修复") from exc
                if published:
                    manifest = self.manifests.all()[(record.new_hash, document_format(record.destination))]
                    rows = self.chunk_rows(f"index_id == {json.dumps(record.index_id)}")
                    if not chunks_match(manifest, rows):
                        raise PublicationUncertain("已发布版本校验失败，已保留恢复材料")
                    journal.advance("published")
                    self._finish_uploaded_version(journal)
                    return {**result, "ok": True, "committed": True, "error": None}
                if record.stage == "published":
                    raise PublicationUncertain("已确认的发布映射发生变化，停止自动回滚")
            current_hash = compute_file_sha256(record.destination)
            if os.path.exists(record.destination) and current_hash is None:
                raise OSError("无法核对当前源文件，停止恢复")
            if current_hash not in (None, record.old_hash, record.new_hash):
                raise RuntimeError("源文件已被更新为其他版本，停止自动回滚")
            if record.backup_path is not None:
                if os.path.exists(record.backup_path):
                    if compute_file_sha256(record.backup_path) != record.old_hash:
                        raise RuntimeError("备份文件内容不匹配，停止恢复")
                    os.replace(record.backup_path, record.destination)
                elif current_hash != record.old_hash:
                    raise RuntimeError("原文件备份缺失，无法恢复")
            else:
                if current_hash is None and os.path.isfile(record.staged_path):
                    if compute_file_sha256(record.staged_path) != record.new_hash:
                        raise RuntimeError("暂存文件内容不匹配，停止恢复")
                    os.replace(record.staged_path, record.destination)
                    current_hash = record.new_hash
                if current_hash == record.new_hash:
                    # 首次上传失败仍保留源文件，供现有“修复与补建”使用。
                    self.manifests.record_files({record.destination: record.new_hash})
                    result["error"] = "上传已中断，源文件已保留，请执行修复与补建"
            if record.index_id is not None:
                self._delete_pks([row["pk"] for row in self.chunk_rows(f"index_id == {json.dumps(record.index_id)}")])
            journal.advance("rolled_back" if record.old_hash is not None else "unpublished")
            return result
