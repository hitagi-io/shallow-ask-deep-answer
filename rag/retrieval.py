"""知识库检索：向量与 BM25 各召回候选，RRF 去重融合后精排返回 TopK。"""
from __future__ import annotations

import hashlib
import json
import math
import time

from utils.config_loader import load_config
from utils.dashscope_client import DashScopeRequestError, make_client, request_json, validate_request_settings
from utils.logging_config import get_logger

logger = get_logger(__name__)


def validate_settings(config: dict) -> dict:
    if not isinstance(config, dict):
        raise ValueError("retrieval.yaml 必须为键值映射")
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
    identity = [metadata["content_sha256"], metadata["chunk_index"], metadata["chunk_sha256"]]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def fuse_rrf(rankings, constant: int):
    scores, documents = {}, {}
    for ranking in rankings:
        seen = set()
        for rank, document in enumerate(ranking, 1):
            identifier = chunk_id(document)
            if identifier in seen:
                raise ValueError("召回结果出现重复分块，请检查索引")
            seen.add(identifier)
            documents[identifier] = document
            scores[identifier] = scores.get(identifier, 0.0) + 1.0 / (constant + rank)
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
    def __init__(self, store, settings=None):
        self.store = store
        self.settings = validate_settings(SETTINGS if settings is None else settings)

    def search(self, query: str):
        if not isinstance(query, str) or not query.strip():
            raise ValueError("检索 query 必须为非空字符串")
        started = time.perf_counter()
        self.store.ensure_search_ready()
        vector_store = self.store.vector_store
        if not vector_store.fields:
            return []
        if not {"vector", "sparse"}.issubset(vector_store.fields):
            raise RuntimeError("当前集合缺少混合检索字段，需从源文件重建混合索引")
        params = dict(zip(vector_store._vector_field, vector_store.search_params))
        vector = vector_store.embedding_func.embed_query(query)

        def search_field(field, value):
            raw = vector_store.client.search(
                collection_name=vector_store.collection_name, data=[value], anns_field=field,
                search_params=params[field], limit=self.settings["candidate_k"],
                output_fields=vector_store._get_output_fields(), consistency_level="Strong",
                timeout=self.settings["search_timeout_seconds"],
            )
            return [vector_store._parse_document(dict(hit["entity"])) for hit in raw[0]]

        dense = search_field("vector", vector)
        sparse = search_field("sparse", query)
        # 保留两路候选的完整并集（最多 40），不能先截成 Top5 再精排。
        candidates = fuse_rrf([dense, sparse], self.settings["rrf_k"])
        documents = rerank_documents(query, candidates, self.settings["top_k"], self.settings["rerank"])
        logger.info("[混合检索] 向量=%s BM25=%s 融合候选=%s 返回=%s 耗时=%.0fms",
                    len(dense), len(sparse), len(candidates), len(documents),
                    (time.perf_counter() - started) * 1000)
        return documents
