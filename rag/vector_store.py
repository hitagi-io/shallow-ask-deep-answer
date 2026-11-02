from __future__ import annotations

import os.path
import time
import threading
import uuid
from langchain_community.embeddings import DashScopeEmbeddings
import langchain_community.embeddings.dashscope as _dashscope_embeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from utils.config_loader import vector_db_config
from langchain_milvus import Milvus
from utils.knowledge_files import (
    list_supported_files,
    compute_file_md5,
    has_indexed_md5,
    get_document_loader,
    record_indexed_md5,
    remove_indexed_md5,
)
from utils.project_paths import resolve_project_path
from utils.logging_config import get_logger

logger = get_logger(__name__)
from dotenv import load_dotenv
load_dotenv()

_embedding_batch_size = vector_db_config.get("embedding_batch_size")
if _embedding_batch_size:
    _dashscope_embeddings.BATCH_SIZE[vector_db_config["embedding_model"]] = int(_embedding_batch_size)

_vector_store_instance: VectorStore | None = None
_lock: threading.Lock = threading.Lock()

_INDEX_MAX_ATTEMPTS = 3
_INDEX_RETRY_SLEEP = 1.0

_INDEX_FAIL_GENERIC = "入库失败，详见服务端日志"


def get_vector_store() -> "VectorStore":
    """返回 VectorStore 的全局单例（双重检查锁），线程安全。"""
    global _vector_store_instance
    if _vector_store_instance is None:
        with _lock:
            if _vector_store_instance is None:
                _vector_store_instance = VectorStore()
    return _vector_store_instance


class VectorStore:
    def __init__(self):
        milvus_uri = vector_db_config["milvus_uri"]
        self.vector_store = Milvus(
            collection_name=vector_db_config["collection_name"],
            embedding_function=DashScopeEmbeddings(model=vector_db_config["embedding_model"]),
            connection_args={
                "uri": milvus_uri,
                "pool_size": vector_db_config.get("pool_size", 50),
            },
            drop_old=False,
        )
        self.splitter = RecursiveCharacterTextSplitter(
            chunk_size=vector_db_config["chunk_size"],
            chunk_overlap=vector_db_config["chunk_overlap"],
            separators=vector_db_config["separators"],
            length_function=len,
        )
        self._retriever = self.vector_store.as_retriever(search_kwargs={"k": vector_db_config["k"]})
        self._store_lock = threading.RLock()

    def get_retriever(self):
        return self._retriever

    def index_pending_files(self, file_paths=None) -> list[dict]:
        """加载未入库文件并完成入库闭环（写入 → 校验 → 失败删向量重建复验）。

        跳过已有 MD5 索引记录的内容；不清空或重建整个向量库。
        返回失败文件清单 [{'file': ..., 'error': ...}]，全部成功时为空列表。
        """
        failures: list[dict] = []
        if file_paths is None:
            allowed_type_files_list = list_supported_files(
                vector_db_config["knowledge_dir"],
                tuple(vector_db_config["allowed_types"])
            )
        else:
            allowed_type_files_list = file_paths
        with self._store_lock:
            for f in allowed_type_files_list:
                try:
                    md5_hex = compute_file_md5(f)
                    if md5_hex is None:
                        failures.append({"file": f, "error": "MD5 计算失败"})
                        continue
                    if has_indexed_md5(md5_hex):
                        continue
                    result = self.index_file(f)
                    if not result["ok"]:
                        logger.error(f"[向量库] 入库失败 file={os.path.basename(f)}：{result['error']}")
                        failures.append({"file": f, "error": result["error"]})
                except Exception as e:
                    logger.error(f"[向量库] 入库失败 file={os.path.basename(f)}：{e}")
                    failures.append({"file": f, "error": _INDEX_FAIL_GENERIC})
        return failures


    def index_file(self, file_path: str, previous_pks: list | None = None) -> dict:
        """单文件入库闭环：切分 → 写入向量 → 写 md5 记录 → 立即校验。

        校验标准：Milvus 中该 source 的条数 == 切块数，且 md5.txt 含该记录；
        两项任一不过即删除该文件全部向量与记录后重建复验，最多重试
        `_INDEX_MAX_ATTEMPTS` 次。成功返回 {'ok': True, 'chunks': n}；
        失败返回 {'ok': False, 'error': ...}，不抛异常。每个文件入库
        每个文件的入库过程独立完成，不依赖定时健康检查结果。

        `previous_pks` 为覆盖上传时旧版本已占用的主键：校验只统计本次新增的
        向量，失败清理也只删本次新增的部分，旧版本在切换完成前保持可检索。
        """
        file_path = resolve_project_path(file_path)
        previous = set(previous_pks or ())
        # 写向量、MD5 记录和校验必须在同一把进程内锁下完成。
        with self._store_lock:
            md5_hex = compute_file_md5(file_path)
            if md5_hex is None:
                return {"file": file_path, "chunks": None, "ok": False, "error": "MD5 计算失败"}
            if has_indexed_md5(md5_hex):
                return {"file": file_path, "chunks": None, "ok": True, "error": None}
            loader = get_document_loader(file_path)
            if loader is None:
                return {"file": file_path, "chunks": None, "ok": False, "error": "不支持的文件类型"}
            doc = loader(file_path)
            if doc is None:
                return {"file": file_path, "chunks": None, "ok": False, "error": "文件加载失败"}
            split_doc = self.splitter.split_documents(doc)
            if not split_doc:
                return {"file": file_path, "chunks": None, "ok": False, "error": "文件分割后为空"}

            last_err: str = _INDEX_FAIL_GENERIC
            for attempt in range(1, _INDEX_MAX_ATTEMPTS + 1):
                try:
                    self.vector_store.add_documents(split_doc)
                    record_indexed_md5(md5_hex)
                    if self._verify_indexed(file_path, md5_hex, len(split_doc), previous):
                        return {"file": file_path, "chunks": len(split_doc), "ok": True, "error": None}
                    last_err = f"校验未通过（Milvus 条数 ≠ 切块数 {len(split_doc)}）"
                except Exception as e:
                    last_err = _INDEX_FAIL_GENERIC
                    logger.error(
                        f"[向量库] 入库失败 file={os.path.basename(file_path)}"
                        f" 第 {attempt}/{_INDEX_MAX_ATTEMPTS} 次尝试：{e}"
                    )
                self._delete_added_pks(file_path, previous)
                if has_indexed_md5(md5_hex):
                    remove_indexed_md5(md5_hex)
                if attempt < _INDEX_MAX_ATTEMPTS:
                    time.sleep(_INDEX_RETRY_SLEEP)
            return {"file": file_path, "chunks": None, "ok": False, "error": last_err}

    def _verify_indexed(
        self, file_path: str, md5_hex: str, expected_chunks: int,
        previous_pks: set | None = None,
    ) -> bool:
        """校验单文件入库结果：md5 记录存在 且 Milvus 条数 == 切块数。

        `previous_pks` 是覆盖上传时旧版本占用的主键，校验只统计本次新增的向量。
        Milvus Bounded 一致性下刚插入的数据可能短暂不可见，
        首次计数不符时 flush 后复验一次，避免误判重建。
        """
        if not has_indexed_md5(md5_hex):
            return False
        if self._count_new_vectors(file_path, previous_pks) == expected_chunks:
            return True
        self._flush_milvus()
        return self._count_new_vectors(file_path, previous_pks) == expected_chunks

    def _count_new_vectors(self, file_path: str, previous_pks: set | None = None) -> int:
        """返回该 source 下不属于 previous_pks 的向量条数（即本次新增部分）。"""
        previous = previous_pks or set()
        return sum(1 for pk in self.pks_by_source(file_path) if pk not in previous)

    def _flush_milvus(self) -> None:
        """强制 Milvus 写入磁盘，使刚插入的数据对后续 query 可见。"""
        client = getattr(self.vector_store, "client", None)
        if client is not None:
            client.flush(self.vector_store.collection_name)

    _ITER_BATCH_SIZE = 1000

    def pks_by_source(self, file_path: str) -> list:
        """拉取指定 source 的全部主键，供校验、删除和健康检查共用。"""
        vs = self.vector_store
        pk_field = getattr(vs, "_primary_field", "pk")
        escaped_path = file_path.replace('\\', '\\\\').replace('"', '\\"')
        iterator = vs.client.query_iterator(
            collection_name=vs.collection_name,
            batch_size=self._ITER_BATCH_SIZE,
            filter=f'source == "{escaped_path}"',
            output_fields=[pk_field],
        )
        try:
            pks: list = []
            while True:
                rows = iterator.next()
                if not rows:
                    break
                pks.extend(row.get(pk_field) for row in rows)
            return pks
        finally:
            iterator.close()

    def delete_vectors_by_source(self, file_path: str) -> int:
        """按 source 删除该文件的全部向量（分批，避免单次请求过大），返回删除条数。"""
        return self._delete_pks(self.pks_by_source(file_path))

    def _delete_pks(self, pks: list) -> int:
        """按主键分批删除向量，返回删除条数。"""
        for i in range(0, len(pks), self._ITER_BATCH_SIZE):
            self.vector_store.delete(ids=pks[i:i + self._ITER_BATCH_SIZE])
        return len(pks)

    def _delete_added_pks(self, file_path: str, previous_pks: set | None = None) -> int:
        """只删除本次新增的向量，保留 previous_pks 中的旧版本向量。"""
        previous = previous_pks or set()
        pks = [pk for pk in self.pks_by_source(file_path) if pk not in previous]
        return self._delete_pks(pks)

    def _another_file_with_same_content(self, file_path: str, md5_hex: str) -> str | None:
        """返回知识库中另一份内容相同的文件路径；不存在则返回 None。

        内容级去重下，同内容文件共享一条索引记录与一组向量（向量挂在首个入库
        文件的 source 上）。md5 计算走缓存，重复调用开销可忽略。
        """
        norm_self = os.path.normcase(os.path.abspath(file_path))
        for f in list_supported_files(
            vector_db_config["knowledge_dir"], tuple(vector_db_config["allowed_types"])
        ):
            if os.path.normcase(os.path.abspath(f)) == norm_self:
                continue
            if compute_file_md5(f) == md5_hex:
                return f
        return None

    def delete_file_index(self, file_path, md5_hex: str | None = None):
        """清理文件对应的向量块和 MD5 记录，不删除磁盘源文件。

        失败时抛出异常，不静默吞掉；同内容副本的索引按下述规则保留或重建。

        `md5_hex` 可由调用方传入：若调用方先删掉了磁盘文件（Windows 下文件被占用时
        删除会失败，先删文件能把失败挡在改动索引之前），文件已不在，本方法算不出 md5。

        先删索引记录、再删向量；向量删除失败时回滚恢复索引记录，
        保证失败后状态要么完整未删、要么"记录缺失+向量仍在"，均由
        index_health 按"重建记录"修复，而不是误把待删文件重新索引。

        若被删的正是"持有向量的那一份"（向量挂在其 source 上）而磁盘上还有同内容
        副本，则删完向量后立即以副本重新入库——否则会留下 missing_vectors：
        界面会显示“已入库”，但实际无法检索，必须等待定时健康检查或手动修复才能恢复。
        """
        file_path = resolve_project_path(file_path)
        if md5_hex is None:
            md5_hex = compute_file_md5(file_path)

        with self._store_lock:
            survivor = self._another_file_with_same_content(file_path, md5_hex) if md5_hex else None
            drop_record = bool(md5_hex) and survivor is None
            if drop_record:
                try:
                    remove_indexed_md5(md5_hex)
                except Exception:
                    remove_indexed_md5(md5_hex)

            try:
                deleted = self.delete_vectors_by_source(file_path)
            except Exception:
                if drop_record:
                    try:
                        record_indexed_md5(md5_hex)
                        logger.error(f"[向量库] 删除向量失败，已恢复索引记录 file={os.path.basename(file_path)}")
                    except Exception as rollback_err:
                        logger.error(
                            f"[向量库] 删除向量失败，恢复索引记录也失败 file={os.path.basename(file_path)}："
                            f"{rollback_err}（可执行索引修复重建记录）"
                        )
                raise

            if deleted and survivor is not None:
                remove_indexed_md5(md5_hex)
                result = self.index_file(survivor)
                if result["ok"]:
                    logger.info(f"[向量库] 已使用同内容副本重建向量 file={os.path.basename(survivor)}")
                else:
                    record_indexed_md5(md5_hex)
                    logger.error(f"[向量库] 同内容副本重建向量失败 file={os.path.basename(survivor)}：{result['error']}")

    def replace_and_index_file(self, file_path: str, tmp_path: str) -> dict:
        """覆盖上传：新版本先入库校验，通过后再清理旧版本；失败回滚磁盘文件。

        相比"先删旧向量再入库新版本"，旧版本向量在新版本校验通过前始终保留，
        入库失败时磁盘文件与旧索引一起回滚，检索侧不会出现不可用窗口。
        是否存在同名文件在锁内判定，并发上传不会把覆盖误判成新建。
        返回 {'file', 'chunks', 'ok', 'error', 'overwritten'}。
        """
        file_path = resolve_project_path(file_path)
        with self._store_lock:
            new_md5 = compute_file_md5(tmp_path)
            existed = os.path.isfile(file_path)
            if new_md5 is None:
                return {
                    "file": file_path, "chunks": None, "ok": False,
                    "error": "MD5 计算失败", "overwritten": existed,
                }

            old_md5 = compute_file_md5(file_path) if existed else None
            old_pks = self.pks_by_source(file_path)

            # 内容未变且已入库：只需替换磁盘文件，向量与索引记录保持不动
            if existed and old_md5 == new_md5 and has_indexed_md5(new_md5):
                os.replace(tmp_path, file_path)
                return {
                    "file": file_path, "chunks": None, "ok": True,
                    "error": None, "overwritten": True,
                }

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
                result = self.index_file(file_path, previous_pks=old_pks)
            except Exception as e:
                logger.error(f"[向量库] 覆盖上传入库异常 file={os.path.basename(file_path)}：{e}")
                result = {
                    "file": file_path, "chunks": None, "ok": False,
                    "error": _INDEX_FAIL_GENERIC,
                }

            if not result["ok"]:
                # 回滚：只删新版本新增的向量，旧版本向量与记录保持可检索
                try:
                    self._delete_added_pks(file_path, set(old_pks))
                except Exception as e:
                    logger.error(f"[向量库] 覆盖上传清理新版本向量失败 file={os.path.basename(file_path)}：{e}")
                if existed:
                    try:
                        os.replace(backup, file_path)
                    except Exception as e:
                        logger.info(
                            f"[向量库] 覆盖上传回滚磁盘文件失败 file={os.path.basename(file_path)}：{e}"
                            f"（旧索引仍保留，建议执行索引修复）"
                        )
                return {**result, "overwritten": existed}

            # 切换生效版本：新版本已入库并通过校验，清理旧版本向量与过期索引记录
            try:
                deleted_old = self._delete_pks(old_pks)
                if old_md5 and old_md5 != new_md5:
                    survivor = self._another_file_with_same_content(file_path, old_md5)
                    if survivor is None:
                        remove_indexed_md5(old_md5)
                    elif deleted_old:
                        # 旧内容的向量原本挂在本文件 source 上，已随旧版本删除；
                        # 只保留记录而不重建副本向量会让副本"显示已入库、实际检索不到"，
                        # 因此与 delete_file_index 一致：改挂到存活副本上。
                        remove_indexed_md5(old_md5)
                        reindex = self.index_file(survivor)
                        if reindex["ok"]:
                            logger.info(f"[向量库] 覆盖后已使用同内容副本重建向量 file={os.path.basename(survivor)}")
                        else:
                            record_indexed_md5(old_md5)
                            logger.error(
                                f"[向量库] 覆盖后同内容副本重建向量失败 "
                                f"file={os.path.basename(survivor)}：{reindex['error']}"
                            )
            except Exception as e:
                logger.error(f"[向量库] 覆盖上传清理旧版本失败 file={os.path.basename(file_path)}：{e}")
                return {
                    **result, "ok": False, "overwritten": existed,
                    "error": "新版本已入库，但旧版本向量清理失败，请重试上传",
                }

            if existed:
                try:
                    os.remove(backup)
                except OSError as e:
                    logger.error(f"[向量库] 删除备份文件失败 file={backup}：{e}")
            return {**result, "overwritten": existed}
