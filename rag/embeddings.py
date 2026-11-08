"""有界请求的文本嵌入；每批独立重试，保留 document/query 的原生语义。"""
from __future__ import annotations

import math

from langchain_core.embeddings import Embeddings

from utils.dashscope_client import DashScopeRequestError, make_client, request_json, validate_request_settings


class DashScopeEmbeddings(Embeddings):
    def __init__(self, config: dict):
        self.model = config["embedding_model"]
        self.batch_size = config["embedding_batch_size"]
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("embedding_model 必须为非空字符串")
        if type(self.batch_size) is not int or self.batch_size <= 0:
            raise ValueError("embedding_batch_size 必须为正整数")
        self.request_settings = validate_request_settings({
            "endpoint": config["embedding_endpoint"],
            "timeout_seconds": config["embedding_timeout_seconds"],
            "connect_timeout_seconds": config["embedding_connect_timeout_seconds"],
            "max_attempts": config["embedding_max_attempts"],
        })

    def _embed(self, texts: list[str], text_type: str) -> list[list[float]]:
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError("嵌入文本必须为非空字符串")
        if not texts:
            return []
        vectors = []
        dimension = None
        with make_client(self.request_settings) as client:
            for start in range(0, len(texts), self.batch_size):
                batch = texts[start:start + self.batch_size]
                body = request_json(client, self.request_settings, {
                    "model": self.model, "input": {"texts": batch},
                    "parameters": {"text_type": text_type},
                }, "embedding")
                try:
                    items = body["output"]["embeddings"]
                    if not isinstance(items, list) or len(items) != len(batch):
                        raise ValueError("返回条数不符")
                    indexed = {}
                    for item in items:
                        index, vector = item["text_index"], item["embedding"]
                        if type(index) is not int or not 0 <= index < len(batch) or index in indexed:
                            raise ValueError("返回索引无效")
                        if (not isinstance(vector, list) or not vector
                                or any(type(v) not in (int, float) or not math.isfinite(v) for v in vector)):
                            raise ValueError("向量内容无效")
                        if dimension is None:
                            dimension = len(vector)
                        if len(vector) != dimension:
                            raise ValueError("向量维度不一致")
                        indexed[index] = vector
                    vectors.extend(indexed[i] for i in range(len(batch)))
                except (KeyError, TypeError, ValueError) as exc:
                    raise DashScopeRequestError("embedding 响应条数、索引或向量格式无效") from exc
        return vectors

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, "document")

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], "query")[0]
