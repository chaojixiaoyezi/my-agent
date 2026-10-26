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
from agent_py_agent.agent.retrieval.embedding_usage import embedding_input_type
from agent_py_agent.agent.retrieval.lexical import rank as bm25_rank
from agent_py_agent.agent.retrieval.lexical import reciprocal_rank_fusion

# LLM: 混合召回必须保持 query 与候选文档的结构化输入角色分离；改嵌入调用时同步核对向量缓存与 S7 计数。
# 模块用途: 组合 BM25 与向量排序，并在端点失败时保持关键词降级。


# LLM: 标准模式保持同步嵌入；显式latency_safe或embed_missing=False只读已有向量，不发网络。
# 类用途: 提供标准混合召回及前台缓存模式。
class HybridRetriever:
    """对 ``docs``(``[(id, text), ...]``)按与 query 的相关度排序。

    - ``embedder=None`` → 纯 BM25 词面召回。
    - 提供 embedder → BM25 + 向量 cosine 两路,RRF 融合(语义 + 词面互补)。
      embedder 端点抖动(``EmbeddingError``)自动降级到纯 BM25,检索绝不崩。
    - ``vector_min_score``:向量臂最低 cosine。低于门槛的弱相关候选不进语义臂
      (注入侧"阈值化":只带真相关,不靠弱相关凑数);BM25 词面臂不受影响(词面
      重合本身就是相关信号)。None/0 表示不过滤。
    """

    # LLM: 一个实例只服务一次检索；last_* 字段是本轮观察事实（现嵌向量、检索方式、降级原因），下一次 rank 覆盖。
    # LLM: last_query_vector只保留本轮真正现嵌查询，供调用方有界进程缓存复用，不落磁盘。
    # 函数用途: 绑定嵌入端和本轮观察字段。
    def __init__(self, embedder: EmbeddingProvider | None = None) -> None:
        self._embedder = embedder
        # 本轮真正现嵌得到的文档向量（id → vector），供调用方回写缓存；未嵌时为空。
        self.last_fresh_doc_vectors: dict[str, list[float]] = {}
        # 本轮实际走的检索方式（semantic=词面+向量融合，keyword=只有词面，none=没有文档未检索）与降级原因，
        # 只是观察事实：调用方据此如实告诉模型这次走的是哪条路，不参与排序。
        self.last_retrieval_mode = "none"
        self.last_fallback_reason = ""
        self.last_query_vector = None

    # LLM: 排序结果与降级规则不变；只额外记下 last_retrieval_mode/last_fallback_reason，供记忆检索报告如实回给模型。
    # LLM: latency_safe禁网络；缺查询或文档缓存报告bm25_fallback，已有文档仍可参与语义臂。
    # 函数用途: 显式选择同步或缓存融合排序，缺缓存不阻塞前台。
    def rank(
        self,
        query: str,
        docs: list[tuple[str, str]],
        *,
        top_k: int = 10,
        vector_min_score: float = 0.0,
        cached_vectors: dict[str, list[float]] | None = None,
        latency_safe: bool = False,
        cached_query_vector: list[float] | None = None,
        embed_missing: bool = True,
    ) -> list[tuple[str, float]]:
        self.last_retrieval_mode, self.last_fallback_reason = "none", ""
        self.last_fresh_doc_vectors = {}
        self.last_query_vector = None
        if not docs:
            return []
        ids = [d[0] for d in docs]
        texts = [d[1] for d in docs]

        bm25_order = [ids[i] for i in bm25_rank(query, texts)]

        if latency_safe or not embed_missing:
            return self._rank_cached(ids, bm25_order, cached_vectors or {},
                                     cached_query_vector, (top_k, vector_min_score))

        vec_order = self._vector_order(
            query, ids, texts, min_score=vector_min_score, cached_vectors=cached_vectors
        )
        if vec_order is None:
            # 无 embedder 或端点失败 → 纯 BM25(给个递减分,保持可比)
            self.last_retrieval_mode = "keyword"
            self.last_fallback_reason = "embedder_unavailable" if self._embedder is None else "embedding_failed"
            return [(id, 1.0 / (i + 1)) for i, id in enumerate(bm25_order)][:top_k]

        self.last_retrieval_mode = "semantic"
        fused = reciprocal_rank_fusion([bm25_order, vec_order])
        return fused[:top_k]

    # LLM: 向量臂按文档 id 复用调用方传入的已有向量，只对缺失项调用 embed；调用方负责保证缓存与
    #   embedder 的模型/维度一致（不一致时查找落空 → 全量重算，结果不变）。返回顺序仍由 cosine 决定。
    # LLM: 同步模式保持网络调用及维度失配重嵌；现嵌query副本供调用方缓存。
    # 函数用途: 计算标准同步向量臂并记下现嵌查询。
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
            with embedding_input_type("query"):
                query_vec = self._embedder.embed([query])[0]
                self.last_query_vector = list(query_vec)
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
                with embedding_input_type("db"):
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

    # LLM: 仅用长度与query一致的缓存子集；缺项只走BM25，不调用embedder或写缓存。
    # 函数用途: 融合缓存语义候选及全体词面候选。
    def _rank_cached(self, ids, bm25_order, cache, query_vector, limits):
        top_k, min_score = limits
        available = [(entry_id, vector) for entry_id in ids
                     if isinstance(vector := cache.get(entry_id), (list, tuple))
                     and query_vector and len(vector) == len(query_vector)]
        missing = not query_vector or len(available) != len(ids)
        self.last_retrieval_mode = "bm25_fallback" if missing else "semantic"
        self.last_fallback_reason = "vectors_missing" if missing else ""
        if not available:
            return [(entry_id, 1.0 / (index + 1)) for index, entry_id in enumerate(bm25_order)][:top_k]
        centered_query, vectors = mean_center(query_vector, [list(vector) for _, vector in available])
        scores = [(entry_id, cosine(centered_query, vector))
                  for (entry_id, _), vector in zip(available, vectors)]
        vec_order = [entry_id for entry_id, score in sorted(scores, key=lambda row: (-row[1], row[0]))
                     if score > 0 and (min_score <= 0 or score >= min_score)]
        return reciprocal_rank_fusion([bm25_order, vec_order])[:top_k]
