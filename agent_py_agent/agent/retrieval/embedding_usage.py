# LLM: S7（3a 2026-10-02）：嵌入调用与记忆召回方式的进程内计数，只为展示（/model vector 的“本次启动以来”几行），不写盘、
#   不加开关、不改请求，也不进 model call ledger（那里的准入、停机关门、预算、校准语义与嵌入无关，别牵连）。计数只有一处：
#   两个嵌入客户端的 embed 都经 count_embedding_request；用途按正在执行的操作标注（embedding_purpose 上下文：记忆写入、召回、
#   重建、工具检索），没标注的记为 other，不猜。token 只记供应商回报的数，没回报的请求单独计数，不估算。召回方式由
#   memory_store.jsonl._scoped_retrieval_facts 每次检索记一次。只有数字和原因码，不含正文。改动同步 test_embedding_usage.py。
# 模块用途: 统计本进程嵌入请求的次数、条数、失败和供应商回报的 token，以及记忆召回走语义、关键词还是没检索。
from __future__ import annotations

import functools
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

# 嵌入用途：记忆写入时嵌新条目、召回时嵌查询与缺向量的条目、重建向量库、工具语义检索；没标注的归 other。
EMBEDDING_PURPOSES: tuple[str, ...] = ("memory_write", "memory_recall", "memory_rebuild", "tool_retrieval", "other")
# 召回方式（与 retrieval.hybrid.HybridRetriever.last_retrieval_mode 同一组值）。
RETRIEVAL_MODES: tuple[str, ...] = ("semantic", "keyword", "none")

_PURPOSE: ContextVar[str] = ContextVar("embedding_purpose", default="other")


# LLM: 计数器是进程级单例 EMBEDDING_USAGE；线程安全（Gateway 多 owner 并发）。snapshot 返回副本，调用方改了不影响计数。
# 类用途: 保存本进程的嵌入用量与召回方式计数。
class EmbeddingUsage:
    # 函数用途: 建一个空计数器并记下开始时间。
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    # LLM: 只给测试和进程内重置用；生产不调用（计数随进程生灭）。
    # 函数用途: 清空全部计数并重记开始时间。
    def reset(self) -> None:
        with self._lock:
            self._started_at = time.time()
            self._embedding = {purpose: _empty_embedding_row() for purpose in EMBEDDING_PURPOSES}
            self._retrieval: dict[str, object] = {**dict.fromkeys(RETRIEVAL_MODES, 0),
                                                  "last_mode": "", "last_fallback_reason": ""}

    # LLM: 一次嵌入请求记一条：texts 是本次请求的文本条数；tokens 只收供应商回报的非负整数，None 表示没回报。
    #   未知用途归 other。失败的请求不记 token。
    # 函数用途: 记一次嵌入请求的结果。
    def record_embedding(self, purpose: str, texts: int, *, ok: bool, tokens: int | None) -> None:
        key = purpose if purpose in self._embedding else "other"
        delta = {"requests": 1, "texts": max(0, int(texts)), "failures": 0 if ok else 1,
                 "tokens": tokens if ok and tokens is not None else 0,
                 "tokens_unreported_requests": 1 if ok and tokens is None else 0}
        with self._lock:
            row = self._embedding[key]
            for name, value in delta.items():
                row[name] += value

    # LLM: mode 只收 RETRIEVAL_MODES 里的值，其它按 none 记；fallback_reason 是结构化原因码，原样保存最近一次。
    # 函数用途: 记一次记忆检索走的方式。
    def record_retrieval(self, mode: str, fallback_reason: str) -> None:
        key = mode if mode in RETRIEVAL_MODES else "none"
        with self._lock:
            self._retrieval[key] = int(self._retrieval[key]) + 1
            self._retrieval["last_mode"] = key
            self._retrieval["last_fallback_reason"] = str(fallback_reason or "")

    # 函数用途: 返回计数副本：开始时间、各用途嵌入计数、召回方式计数。
    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {"started_at": self._started_at,
                    "embedding": {key: dict(row) for key, row in self._embedding.items()},
                    "retrieval": dict(self._retrieval)}


# 函数用途: 一种用途的空计数行。
def _empty_embedding_row() -> dict[str, int]:
    return {"requests": 0, "texts": 0, "failures": 0, "tokens": 0, "tokens_unreported_requests": 0}


EMBEDDING_USAGE = EmbeddingUsage()


# LLM: 用途标注只在操作入口用（JsonlMemory 写入、召回、重建，工具语义检索），作用域内的嵌入请求按它计；不影响请求本身。
# 函数用途: 在一段代码里把嵌入请求标成某个用途。
@contextmanager
def embedding_purpose(purpose: str) -> Iterator[None]:
    token = _PURPOSE.set(purpose)
    try:
        yield
    finally:
        _PURPOSE.reset(token)


# LLM: 方法级的用途标注（装饰器形式的 embedding_purpose），给记忆写入/召回/重建与工具语义检索的操作入口用；返回值原样。
# 函数用途: 把一个方法里发出的嵌入请求都标成某个用途。
def counted_as(purpose: str) -> Callable[[Callable], Callable]:
    def decorate(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            with embedding_purpose(purpose):
                return func(*args, **kwargs)
        return wrapper
    return decorate


# LLM: 两个嵌入客户端的唯一计数点：request 发出真实请求并返回 (向量, 供应商回报的 token 或 None)；成功、失败都记一次，
#   异常原样抛出（调用方的降级逻辑不变）。空列表不发请求，也不计数。
# 函数用途: 执行一次嵌入请求并按当前用途记账。
def count_embedding_request(
    texts: list[str], request: Callable[[list[str]], tuple[list[list[float]], int | None]],
) -> list[list[float]]:
    if not texts:
        return []
    purpose = _PURPOSE.get()
    try:
        vectors, tokens = request(texts)
    except BaseException:
        EMBEDDING_USAGE.record_embedding(purpose, len(texts), ok=False, tokens=None)
        raise
    EMBEDDING_USAGE.record_embedding(purpose, len(texts), ok=True, tokens=tokens)
    return vectors


# LLM: 只认非负整数（bool 除外），别的一律当“没回报”，不从别的字段估算。
# 函数用途: 从供应商响应里取出回报的 token 数。
def reported_tokens(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


__all__ = [
    "EMBEDDING_PURPOSES",
    "EMBEDDING_USAGE",
    "RETRIEVAL_MODES",
    "EmbeddingUsage",
    "count_embedding_request",
    "counted_as",
    "embedding_purpose",
    "reported_tokens",
]
