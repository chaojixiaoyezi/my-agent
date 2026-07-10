"""混合检索:BM25 词面 + 向量语义,经 RRF 融合(Phase 2)。

混合检索支持降级：配了 embedder → BM25 与向量两路各自排序、RRF 融合(语义召回
治"换词就召不回"的短板);没配 embedder → 纯 BM25(仍比 my-agent 原裸 n-gram 强)。全自建。
"""

from __future__ import annotations

from agent_py_agent.agent.retrieval.embedding import (
    EmbeddingError,
    EmbeddingProvider,
    cosine,
    mean_center,
)
from agent_py_agent.agent.retrieval.lexical import rank as bm25_rank
from agent_py_agent.agent.retrieval.lexical import reciprocal_rank_fusion


class HybridRetriever:
    """对 ``docs``(``[(id, text), ...]``)按与 query 的相关度排序。

    - ``embedder=None`` → 纯 BM25 词面召回。
    - 提供 embedder → BM25 + 向量 cosine 两路,RRF 融合(语义 + 词面互补)。
      embedder 端点抖动(``EmbeddingError``)自动降级到纯 BM25,检索绝不崩。
    """

    def __init__(self, embedder: EmbeddingProvider | None = None) -> None:
        self._embedder = embedder

    def rank(self, query: str, docs: list[tuple[str, str]], *, top_k: int = 10) -> list[tuple[str, float]]:
        if not docs:
            return []
        ids = [d[0] for d in docs]
        texts = [d[1] for d in docs]

        bm25_order = [ids[i] for i in bm25_rank(query, texts)]

        vec_order = self._vector_order(query, ids, texts)
        if vec_order is None:
            # 无 embedder 或端点失败 → 纯 BM25(给个递减分,保持可比)
            return [(id, 1.0 / (i + 1)) for i, id in enumerate(bm25_order)][:top_k]

        fused = reciprocal_rank_fusion([bm25_order, vec_order])
        return fused[:top_k]

    def _vector_order(self, query: str, ids: list[str], texts: list[str]) -> list[str] | None:
        if self._embedder is None:
            return None
        try:
            query_vec = self._embedder.embed([query])[0]
            doc_vecs = self._embedder.embed(texts)
        except (EmbeddingError, IndexError):
            return None  # 端点抖动 → 降级纯 BM25
        # 去"通用方向偏置"(某些文档和所有查询都高相似→干扰召回),提升语义召回区分度;通道运行时 也没做这步
        query_vec, doc_vecs = mean_center(query_vec, doc_vecs)
        sims = [(ids[i], cosine(query_vec, doc_vecs[i])) for i in range(min(len(ids), len(doc_vecs)))]
        return [id for id, score in sorted(sims, key=lambda kv: (-kv[1], kv[0])) if score > 0.0]
