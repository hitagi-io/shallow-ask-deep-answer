"""校验磁盘 SHA-256、PostgreSQL 清单与 Milvus 中每一块实际内容。"""
from __future__ import annotations

import os
import threading
from collections import defaultdict
from datetime import datetime, timezone

from rag.index_manifest import chunks_match
from rag.vector_store import get_vector_store
from utils.knowledge_files import list_supported_files, compute_file_sha256
from utils.config_loader import vector_db_config
from utils.logging_config import get_logger

logger = get_logger(__name__)


class IndexHealth:
    def __init__(self, user_id: int, project_id: str):
        self.vector_store = get_vector_store(user_id, project_id)
        self.knowledge_dir = self.vector_store.knowledge_dir
        self.allowed_types = tuple(vector_db_config["allowed_types"])
        self._lock = threading.RLock()

    def _scan(self):
        report = {
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "healthy": True, "milvus_error": None, "manifest_error": None,
            "stats": {"files": 0, "indexed_files": 0, "chunks": 0}, "issues": [],
        }
        issues = {}

        def issue(kind, title, detail, item):
            report["healthy"] = False
            entry = issues.setdefault(kind, {
                "type": kind, "severity": "error", "title": title, "detail": detail,
                "count": 0, "items": [],
            })
            if item not in entry["items"]:
                entry["items"].append(item)
                entry["count"] += 1

        disk = {path: compute_file_sha256(path) for path in
                list_supported_files(self.knowledge_dir, self.allowed_types)}
        by_norm = {os.path.normcase(path): path for path in disk}
        by_hash = defaultdict(list)
        for path, digest in disk.items():
            if digest:
                by_hash[digest].append(path)
            else:
                issue("file_unreadable", "源文件读取失败", "无法确认文件内容，修复不会删除其索引", path)
        report["stats"]["files"] = len(disk)
        state = {"disk": disk, "by_norm": by_norm, "by_hash": by_hash,
                 "records": {}, "rows": [], "healthy_hashes": set()}
        try:
            records = state["records"] = self.vector_store.manifests.all()
        except Exception:
            logger.exception("[索引健康检查] PostgreSQL 清单读取失败")
            report["manifest_error"] = "无法读取 PostgreSQL 索引清单，详情见服务端日志"
            issue("manifest_unreachable", "索引清单不可用", report["manifest_error"], "PostgreSQL")
            report["issues"] = list(issues.values())
            return report, state
        try:
            rows = state["rows"] = self.vector_store.chunk_rows()
        except Exception:
            logger.exception("[索引健康检查] Milvus 查询失败")
            report["milvus_error"] = "无法读取 Milvus，详情见服务端日志"
            issue("milvus_unreachable", "向量库不可用", report["milvus_error"], "Milvus")
            report["issues"] = list(issues.values())
            return report, state
        report["stats"]["chunks"] = len(rows)
        rows_by_hash = defaultdict(list)
        for row in rows:
            rows_by_hash[row.get("content_sha256")].append(row)
            source = row.get("source")
            if not isinstance(source, str) or os.path.normcase(source) not in by_norm:
                issue("ghost_vectors", "向量库存在残留分块", "对应源文件不存在或来源元数据缺失", source or str(row.get("pk")))
            if row.get("content_sha256") not in records:
                issue("missing_manifest", "分块缺少索引清单", "现有分块不能证明完整性，需从源文件重新索引", source or str(row.get("pk")))

        for digest, record in records.items():
            path = by_norm.get(os.path.normcase(record.source))
            source_current = path is not None and disk[path] == digest
            if not by_hash.get(digest) and (path is None or disk[path] is not None):
                issue("orphan_manifest", "索引清单对应内容已不在磁盘", "源文件已删除或被修改，需重建或清理旧索引", record.source)
            if path is None:
                issue("missing_source", "索引来源文件已丢失", "存在同内容副本时会转移索引来源", record.source)
            elif disk[path] is not None and disk[path] != digest:
                issue("stale_content", "索引内容已陈旧", "源文件 SHA-256 与入库时不一致", path)
            if record.state != "ready":
                issue("incomplete_index", "索引构建未完成", "上次写入未完成校验和提交，需要重建", record.source)
            if record.index_version != self.vector_store.index_version:
                issue("outdated_index_version", "索引版本已过期", "解析、分块或嵌入配置已改变，需要重建", record.source)
            actual = rows_by_hash.get(digest, [])
            matched = chunks_match(record, actual)
            if not actual:
                issue("missing_vectors", "索引分块全部缺失", "清单存在，但 Milvus 中没有对应分块", record.source)
            elif len(actual) != record.expected_chunks:
                issue("chunk_count_mismatch", "索引分块数量不符", "存在部分缺失、重复或残留分块", f"{record.source}（预期 {record.expected_chunks}，实际 {len(actual)}）")
            elif not matched:
                issue("chunk_integrity_mismatch", "索引分块内容或身份不符", "分块序号、逐块 SHA-256、来源、版本或写入代次与清单不一致", record.source)
            if source_current and record.state == "ready" and record.index_version == self.vector_store.index_version and matched:
                state["healthy_hashes"].add(digest)
        report["stats"]["indexed_files"] = sum(digest in state["healthy_hashes"] for digest in disk.values())
        report["issues"] = list(issues.values())
        return report, state

    def check(self) -> dict:
        """只读检查；从未入库、也没有残留分块的文件仍视为待构建。"""
        with self._lock, self.vector_store._store_lock:
            return self._scan()[0]

    def file_statuses(self) -> list[dict]:
        """文件列表与健康检查共用完整性判定，不把清单存在当成已正确入库。"""
        snapshot = self.snapshot()
        report = snapshot["report"]
        if report["milvus_error"] or report["manifest_error"]:
            raise RuntimeError("无法确认索引状态")
        return snapshot["files"]

    def snapshot(self) -> dict:
        """一次只读扫描同时供健康报告和文件列表请求共享。"""
        with self._lock, self.vector_store._store_lock:
            report, state = self._scan()
            return {"report": report, "files": [
                {"name": os.path.basename(path), "path": path,
                 "indexed": digest in state["healthy_hashes"]}
                for path, digest in state["disk"].items()
            ]}

    def repair(self) -> dict:
        """按源文件重建异常内容；失败保留清单供重试，绝不据剩余向量补齐清单。"""
        with self._lock, self.vector_store._store_lock:
            report, state = self._scan()
            if report["milvus_error"] or report["manifest_error"]:
                raise RuntimeError("存储不可用，修复终止")
            summary = {"ghost_deleted": [], "ghost_errors": [], "manifest_rebuilt": False,
                       "re_indexed": [], "re_index_failures": []}
            targets = {}
            for digest, record in state["records"].items():
                if digest in state["healthy_hashes"]:
                    continue
                copies = state["by_hash"].get(digest)
                if copies:
                    targets[digest] = copies[0]
                source = state["by_norm"].get(os.path.normcase(record.source))
                if source and state["disk"][source]:
                    targets[state["disk"][source]] = source
            # 清单遗失时重新解析源文件，不信任现存块数和元数据。
            for row in state["rows"]:
                source = row.get("source")
                path = state["by_norm"].get(os.path.normcase(source)) if isinstance(source, str) else None
                if row.get("content_sha256") not in state["records"]:
                    if path and state["disk"][path]:
                        targets[state["disk"][path]] = path
                    copies = state["by_hash"].get(row.get("content_sha256"))
                    if copies:
                        targets[row["content_sha256"]] = copies[0]
            if targets:
                failures = self.vector_store.index_pending_files(list(targets.values()))
                failed = {item["file"] for item in failures}
                summary["re_indexed"] = [path for path in targets.values() if path not in failed]
                summary["re_index_failures"] = failures
                summary["manifest_rebuilt"] = bool(summary["re_indexed"])

            # 重建完成后重新取快照；仅清理确认已无磁盘内容的清单。
            after_report, after = self._scan()
            if after_report["milvus_error"] or after_report["manifest_error"]:
                after_report["repair"] = summary
                return after_report
            unreadable = any(digest is None for digest in after["disk"].values())
            for digest, record in after["records"].items():
                if after["by_hash"].get(digest) or unreadable:
                    continue
                path = after["by_norm"].get(os.path.normcase(record.source))
                if path and after["disk"][path] not in after["healthy_hashes"]:
                    continue  # 文件不可读或新版本仍未成功，保留旧清单以便再次修复。
                try:
                    deleted = self.vector_store._delete_pks([row["pk"] for row in after["rows"]
                                                            if row.get("content_sha256") == digest])
                    self.vector_store.manifests.delete(digest)
                    summary["manifest_rebuilt"] = True
                    if deleted and record.source not in summary["ghost_deleted"]:
                        summary["ghost_deleted"].append(record.source)
                except Exception:
                    logger.exception("[索引健康检查] 清理过期清单失败 source=%s", record.source)
                    summary["ghost_errors"].append(f"{record.source}: 清理失败，详情见服务端日志")
            records = self.vector_store.manifests.all()
            # 无清单的残留块仅在源文件已丢失或替代索引已验证后删除。
            leftovers = defaultdict(list)
            for row in self.vector_store.chunk_rows():
                if row.get("content_sha256") in records:
                    continue
                source = row.get("source")
                path = after["by_norm"].get(os.path.normcase(source)) if isinstance(source, str) else None
                if unreadable or (path and after["disk"][path] not in after["healthy_hashes"]):
                    continue
                leftovers[source or "(来源缺失)"].append(row["pk"])
            for source, pks in leftovers.items():
                try:
                    self.vector_store._delete_pks(pks)
                    summary["ghost_deleted"].append(source)
                except Exception:
                    logger.exception("[索引健康检查] 清理残留块失败 source=%s", source)
                    summary["ghost_errors"].append(f"{source}: 清理失败，详情见服务端日志")
            report = self.check()
            report["repair"] = summary
            return report
