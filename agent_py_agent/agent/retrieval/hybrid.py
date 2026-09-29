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
    - ``vector_min_score``:向量臂最低 cosine。低于门槛的弱相关候选不进语义臂
      (注入侧"阈值化":只带真相关,不靠弱相关凑数);BM25 词面臂不受影响(词面
      重合本身就是相关信号)。None/0 表示不过滤。
    """

    def __init__(self, embedder: EmbeddingProvider | None = None) -> None:
        self._embedder = embedder
        # 本轮真正现嵌得到的文档向量（id → vector），供调用方回写缓存；未嵌时为空。
        self.last_fresh_doc_vectors: dict[str, list[float]] = {}

    def rank(
        self,
        query: str,
        docs: list[tuple[str, str]],
        *,
        top_k: int = 10,
        vector_min_score: float = 0.0,
        cached_vectors: dict[str, list[float]] | None = None,
    ) -> list[tuple[str, float]]:
        if not docs:
            return []
        ids = [d[0] for d in docs]
        texts = [d[1] for d in docs]

        bm25_order = [ids[i] for i in bm25_rank(query, texts)]

        vec_order = self._vector_order(
            query, ids, texts, min_score=vector_min_score, cached_vectors=cached_vectors
        )
        if vec_order is None:
            # 无 embedder 或端点失败 → 纯 BM25(给个递减分,保持可比)
            return [(id, 1.0 / (i + 1)) for i, id in enumerate(bm25_order)][:top_k]

        fused = reciprocal_rank_fusion([bm25_order, vec_order])
        return fused[:top_k]

    # LLM: 向量臂按文档 id 复用调用方传入的已有向量，只对缺失项调用 embed；调用方负责保证缓存与
    #   embedder 的模型/维度一致（不一致时查找落空 → 全量重算，结果不变）。返回顺序仍由 cosine 决定。
    # 函数用途: 计算向量臂顺序，有缓存时不重复嵌入未变化的正文。
    def _vector_order(
        self,
        query: str,
        ids: list[str],
        texts: list[str],
        *,
        min_score: float,
        cached_vectors: dict[str, list[float]] | None = None,
    ) -> list[str] | None:
        if self._embedder is None:
            return None
        cache = cached_vectors or {}
        self.last_fresh_doc_vectors = {}
        try:
            query_vec = self._embedder.embed([query])[0]
        except (EmbeddingError, IndexError):
            return None  # 端点抖动 → 降级纯 BM25
        # 缓存里确有该 id、值是向量、且长度与本轮 query 一致，才算命中；键缺失、值非法或
        # **长度不一致**都必须现嵌——同名模型背后实际向量长度变了时，旧向量喂进 mean_center
        # 会越界抛 IndexError，整轮对话失败（复审探针 P3）。
        qlen = len(query_vec)
        missing_positions = [
            i
            for i, doc_id in enumerate(ids)
            if not (isinstance(cache.get(doc_id), list) and len(cache[doc_id]) == qlen)
        ]
        try:
            doc_vecs: list[list[float]] = [
                list(cache[doc_id]) if isinstance(cache.get(doc_id), list) and len(cache[doc_id]) == qlen else []
                for doc_id in ids
            ]
            if missing_positions:
                fresh = self._embedder.embed([texts[i] for i in missing_positions])
                if len(fresh) != len(missing_positions):
                    return None
                for slot, vec in zip(missing_positions, fresh):
                    doc_vecs[slot] = list(vec)
                    self.last_fresh_doc_vectors[ids[slot]] = list(vec)
        except (EmbeddingError, IndexError):
            return None  # 端点抖动 → 降级纯 BM25
        # 去"通用方向偏置"(某些文档和所有查询都高相似→干扰召回),提升语义召回区分度;通道运行时 也没做这步
        query_vec, doc_vecs = mean_center(query_vec, doc_vecs)
        sims = [(ids[i], cosine(query_vec, doc_vecs[i])) for i in range(min(len(ids), len(doc_vecs)))]
        return [
            id
            for id, score in sorted(sims, key=lambda kv: (-kv[1], kv[0]))
            if score > 0.0 and (min_score <= 0.0 or score >= min_score)
        ]
