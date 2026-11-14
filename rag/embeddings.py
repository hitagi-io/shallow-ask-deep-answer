"""有界请求的文本嵌入；每批独立重试，保留 document/query 的原生语义。"""
from __future__ import annotations

import math
import threading
import time
from concurrent.futures import CancelledError, FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import ExitStack
from typing import Literal

from langchain_core.embeddings import Embeddings

from utils.core.dashscope_client import DashScopeRequestError, make_client, request_json, validate_request_settings
from utils.core.config_loader import vector_db_config, validate_embedding_config
from utils.core.logging_config import get_logger
from utils.runtime.shutdown import check_shutdown, interruptible_lock
from utils.indexing.progress import UnitProgressCallback

logger = get_logger(__name__)
_TextType = Literal["document", "query"]
_request_slots = threading.BoundedSemaphore(vector_db_config["embedding_concurrency"])
_document_request_slots = threading.BoundedSemaphore(
    vector_db_config["embedding_concurrency"] - vector_db_config["embedding_query_reserved_slots"]
)


class DashScopeEmbeddings(Embeddings):
    def __init__(self, config: dict):
        self.model = config["embedding_model"]
        self.batch_size = config["embedding_batch_size"]
        validate_embedding_config(config)
        self.concurrency = config["embedding_concurrency"]
        self.document_concurrency = self.concurrency - config["embedding_query_reserved_slots"]
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

    def _embed_batch(self, client, batch: list[str], text_type: _TextType, stopped: threading.Event) -> list[list[float]]:
        with ExitStack() as slots:
            # 先取得入库名额，避免等待中的入库请求占用提问预留的全局名额。
            if text_type == "document":
                slots.enter_context(interruptible_lock(_document_request_slots))
            slots.enter_context(interruptible_lock(_request_slots))
            if stopped.is_set():
                raise CancelledError()
            body = request_json(client, self.request_settings, {
                "model": self.model, "input": {"texts": batch},
                "parameters": {"text_type": text_type},
            }, "embedding")
        check_shutdown()
        try:
            items = body["output"]["embeddings"]
            if not isinstance(items, list) or len(items) != len(batch):
                raise ValueError("返回条数不符")
            indexed = {}
            dimension = None
            for item in items:
                index, vector = item["text_index"], item["embedding"]
                if type(index) is not int or not 0 <= index < len(batch) or index in indexed:
                    raise ValueError("返回索引无效")
                if (not isinstance(vector, list) or not vector
                        or any(type(v) not in (int, float) or not math.isfinite(v) for v in vector)):
                    raise ValueError("向量内容无效")
                if dimension is None:
                    dimension = len(vector)
                elif len(vector) != dimension:
                    raise DashScopeRequestError("embedding 向量维度不一致")
                indexed[index] = vector
            return [indexed[i] for i in range(len(batch))]
        except (KeyError, TypeError, ValueError) as exc:
            raise DashScopeRequestError("embedding 响应条数、索引或向量格式无效") from exc

    def _embed(self, texts: list[str], text_type: _TextType, on_progress: UnitProgressCallback | None = None) -> list[list[float]]:
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError("嵌入文本必须为非空字符串")
        if not texts:
            return []
        check_shutdown()
        if on_progress is not None:
            on_progress(0, len(texts))
        started = time.monotonic()
        count = math.ceil(len(texts) / self.batch_size)
        concurrency = self.document_concurrency if text_type == "document" else self.concurrency
        if text_type == "document":
            logger.info("[嵌入] 开始 文本段=%s 批次=%s 并发上限=%s", len(texts), count, concurrency)
        stopped = threading.Event()
        batches = {}
        dimension = None
        completed = 0
        with make_client(self.request_settings) as client:
            if count == 1:
                batches[0] = self._embed_batch(client, texts, text_type, stopped)
            else:
                # 仅维持一个并发窗口；失败后不再发送后续批次，结果按原文本顺序归位。
                executor = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="embedding")
                pending = {}
                starts = iter(range(0, len(texts), self.batch_size))

                def submit_next():
                    start = next(starts, None)
                    if start is not None:
                        future = executor.submit(self._embed_batch, client,
                                                 texts[start:start + self.batch_size], text_type, stopped)
                        pending[future] = start

                try:
                    for _ in range(min(count, concurrency)):
                        submit_next()
                    while pending:
                        done, _ = wait(pending, return_when=FIRST_COMPLETED)
                        for future in done:
                            batch = future.result()
                            # 每批响应已校验内部维度，只需比较批次之间的维度。
                            if dimension is None:
                                dimension = len(batch[0])
                            elif len(batch[0]) != dimension:
                                raise DashScopeRequestError("embedding 向量维度不一致")
                            batches[pending.pop(future)] = batch
                            completed += len(batch)
                            if on_progress is not None:
                                on_progress(completed, len(texts))
                        check_shutdown()
                        for _ in done:
                            submit_next()
                finally:
                    stopped.set()
                    executor.shutdown(wait=True, cancel_futures=True)
        vectors = [vector for start in sorted(batches) for vector in batches[start]]
        if count == 1 and on_progress is not None:
            on_progress(len(texts), len(texts))
        if text_type == "document":
            logger.info("[嵌入] 完成 文本段=%s 批次=%s 耗时=%.1fs", len(texts), count, time.monotonic() - started)
        return vectors

    def embed_documents(self, texts: list[str], *, on_progress: UnitProgressCallback | None = None) -> list[list[float]]:
        return self._embed(texts, "document", on_progress)

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], "query")[0]
