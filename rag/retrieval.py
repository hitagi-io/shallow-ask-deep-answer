"""知识库检索：向量与 BM25 各召回候选，RRF 去重融合后精排返回 TopK。"""
from __future__ import annotations

import json
import math
import time
from typing import TYPE_CHECKING

from langchain_core.documents import Document

from utils.core.config_loader import load_config
from utils.retrieval.source_evidence import text_digest
from utils.core.dashscope_client import DashScopeRequestError, make_client, request_json, validate_request_settings
from utils.core.logging_config import get_logger
from utils.observability.tracing import span, fingerprint
from utils.observability.context import remaining_seconds

if TYPE_CHECKING:
    from rag.vector_store import VectorStore

logger = get_logger(__name__)


def validate_settings(config: dict) -> dict:
    for name, high in (("top_k", 250), ("candidate_k", 250), ("rrf_k", 16383)):
        value = config.get(name)
        if type(value) is not int or not 1 <= value <= high:
            raise ValueError(f"{name} 必须为 1～{high} 的整数")
    if config["candidate_k"] < config["top_k"]:
        raise ValueError("每路候选数 candidate_k 不得小于 top_k")
    timeout = config.get("search_timeout_seconds")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("search_timeout_seconds 必须为有限正数")
    rerank = validate_request_settings(config.get("rerank"))
    if not isinstance(rerank.get("model"), str) or not rerank["model"].strip():
        raise ValueError("rerank.model 必须为非空字符串")
    return config


SETTINGS = validate_settings(load_config("retrieval.yaml"))


def chunk_id(document) -> str:
    metadata = document.metadata
    identity = [metadata["content_sha256"], metadata["source_format"], metadata["chunk_index"], metadata["chunk_sha256"]]
    return text_digest(json.dumps(identity, ensure_ascii=False, separators=(",", ":")))


def fuse_rrf(rankings, constant: int):
    scores, documents = {}, {}
    duplicates = 0
    for ranking in rankings:
        seen = set()
        rank = 0
        for document in ranking:
            identifier = chunk_id(document)
            if identifier in seen:
                duplicates += 1
                continue
            seen.add(identifier)
            rank += 1
            documents.setdefault(identifier, document)
            scores[identifier] = scores.get(identifier, 0.0) + 1.0 / (constant + rank)
    if duplicates:
        logger.warning("[混合检索] 已忽略 %s 个重复分块，可通过索引维护清理残留", duplicates)
    # 两路相同分块只送精排一次；RRF 同分使用稳定 ID，避免随机主键影响顺序。
    return [documents[key] for key in sorted(scores, key=lambda key: (-scores[key], key))]


def rerank_documents(query: str, candidates, top_k: int, config: dict):
    if not candidates:
        return []
    top_n = min(top_k, len(candidates))
    with make_client(config) as client:
        body = request_json(client, config, {
            "model": config["model"],
            "input": {"query": query, "documents": [doc.page_content for doc in candidates]},
            "parameters": {"top_n": top_n, "return_documents": False},
        }, "rerank")
    try:
        results = body["output"]["results"]
        if not isinstance(results, list):
            raise ValueError("返回格式无效")
        indices = [row["index"] for row in results]
        scores = [row["relevance_score"] for row in results]
        if (len(indices) != top_n or any(type(i) is not int or not 0 <= i < len(candidates) for i in indices)
                or len(set(indices)) != len(indices)
                or any(type(s) not in (int, float) or not math.isfinite(s) or not 0 <= s <= 1 for s in scores)
                or scores != sorted(scores, reverse=True)):
            raise ValueError("返回索引或分数无效")
    except (KeyError, TypeError, ValueError) as exc:
        raise DashScopeRequestError("rerank 响应条数、索引或分数无效") from exc
    return [candidates[index] for index in indices]


class HybridRetriever:
    def __init__(self, store: VectorStore):
        self.store = store

    def search(self, query: str) -> list[Document]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("检索 query 必须为非空字符串")
        started = time.perf_counter()
        # 调用方在两路检索期间持有项目读锁；只读取一次发布白名单，
        # 确保稠密向量与稀疏向量召回使用同一份发布快照。
        # 白名单为空时不回退到无过滤检索。
        index_ids = self.store.manifests.published_index_ids(self.store.index_version)
        if not index_ids:
            return []
        expression = f"index_id in {json.dumps(index_ids)}"
        self.store.ensure_search_ready()
        vector_store = self.store.vector_store
        if not {"vector", "sparse"}.issubset(vector_store.fields):
            raise RuntimeError("当前集合缺少混合检索字段，需从源文件重建混合索引")
        params = dict(zip(vector_store._vector_field, vector_store.search_params))
        with span("retrieval.embedding"):
            remaining_seconds()
            vector = vector_store.embedding_func.embed_query(query)

        def search_field(field, value):
            remaining = remaining_seconds()
            timeout = SETTINGS["search_timeout_seconds"]
            raw = vector_store.client.search(
                collection_name=vector_store.collection_name, data=[value], anns_field=field,
                filter=expression,
                search_params=params[field], limit=SETTINGS["candidate_k"],
                output_fields=vector_store._get_output_fields(), consistency_level="Strong",
                timeout=min(timeout, remaining) if remaining is not None else timeout,
            )
            return [vector_store._parse_document(dict(hit["entity"])) for hit in raw[0]]

        with span("retrieval.dense") as dense_span:
            remaining_seconds()
            dense = search_field("vector", vector)
            dense_span.attributes["count"] = len(dense)
        with span("retrieval.bm25") as sparse_span:
            remaining_seconds()
            sparse = search_field("sparse", query)
            sparse_span.attributes["count"] = len(sparse)
        # 保留两路候选的完整并集（最多 40），不能先截成 Top5 再精排。
        with span("retrieval.fusion", config_hash=fingerprint(json.dumps(SETTINGS, sort_keys=True))) as fusion:
            candidates = fuse_rrf([dense, sparse], SETTINGS["rrf_k"])
            fusion.attributes["count"] = len(candidates)
        with span("retrieval.rerank", model=SETTINGS["rerank"]["model"]) as rerank:
            remaining_seconds()
            documents = rerank_documents(query, candidates, SETTINGS["top_k"], SETTINGS["rerank"])
            rerank.attributes["count"] = len(documents)
        logger.info("[混合检索] 向量=%s BM25=%s 融合候选=%s 返回=%s 耗时=%.0fms",
                    len(dense), len(sparse), len(candidates), len(documents),
                    (time.perf_counter() - started) * 1000)
        return documents
