"""检索子系统(Phase 2 向量记忆):纯 Python 词面 BM25 + 可选语义向量 + RRF 混合。

治 my-agent 检索召回头号短板。综合 claw(BM25/LocalHashingEmbedder)+ 通道运行时(RRF 混合/
可降级)+ 长期助手(长程回想痛点动机)。自建优先:核心全 stdlib,语义 embedding 端点也是
stdlib urllib 自建客户端、不引库;真要换 ANN 后端时接口不变。
"""

from __future__ import annotations

from agent_py_agent.agent.retrieval.embedding import (
    DEFAULT_EMBED_DIM,
    EmbeddingError,
    EmbeddingProvider,
    LocalHashingEmbedder,
    OpenAICompatibleEmbedder,
    build_embedder,
    cosine,
    l2_normalize,
)
from agent_py_agent.agent.retrieval.hybrid import HybridRetriever
from agent_py_agent.agent.retrieval.pgvector_store import PgVectorStore
from agent_py_agent.agent.retrieval.lexical import (
    bm25_scores,
    rank,
    reciprocal_rank_fusion,
    tokenize,
)
from agent_py_agent.agent.retrieval.vector_store import VectorHit, VectorStore

__all__ = [
    "DEFAULT_EMBED_DIM",
    "EmbeddingError",
    "EmbeddingProvider",
    "HybridRetriever",
    "LocalHashingEmbedder",
    "OpenAICompatibleEmbedder",
    "PgVectorStore",
    "VectorHit",
    "VectorStore",
    "bm25_scores",
    "build_embedder",
    "cosine",
    "l2_normalize",
    "rank",
    "reciprocal_rank_fusion",
    "tokenize",
]
