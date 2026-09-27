"""测试用的确定性本地 embedder（2026-09-27 从 retrieval/embedding.py 原样搬来）。

产品代码只经 core._embedding_client 按 embedding_* 配置创建真实嵌入客户端；这个特征哈希 embedder 没有产品调用方，
只给记忆语义召回、混合检索、向量库这些测试当可复现的替身：词重合越多 cosine 越高，零依赖、不发网络请求。
"""

from __future__ import annotations

import hashlib

from agent_py_agent.agent.retrieval.embedding import DEFAULT_EMBED_DIM, l2_normalize
from agent_py_agent.agent.retrieval.lexical import tokenize


# LLM: 稳定哈希（sha1，不是进程内加盐的 hash），同一个词在任何进程里都落同一个桶，测试结果可复现。
# 函数用途: 把一个特征词映射成（桶下标，符号），做 signed feature hashing。
def _hash_bucket(token: str, dim: int) -> tuple[int, float]:
    """稳定哈希(sha1,非进程内 salted hash)→ (桶下标, 符号),signed feature hashing。"""
    digest = hashlib.sha1(token.encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:4], "big") % dim
    sign = 1.0 if digest[4] & 1 else -1.0
    return bucket, sign


# LLM: 特征 = lexical.tokenize 的词与 CJK 双字 + 字符三元组；改特征会改变依赖它的语义召回测试的排序。
# 函数用途: 把一段文字拆成用于哈希的特征词列表。
def _features(text: str) -> list[str]:
    """特征:词/CJK-bigram(复用 lexical.tokenize)+ 字符 trigram(捕模糊/形近)。"""
    low = text.lower()
    feats = list(tokenize(low))
    compact = "".join(low.split())
    feats += [compact[i : i + 3] for i in range(len(compact) - 2)]
    return feats or ["∅"]


# LLM: 实现 retrieval.embedding.EmbeddingProvider 协议（dim + embed），只在测试里当确定性替身，产品代码不引用。
# 类用途: 把文字特征哈希成定长向量并 L2 归一的本地 embedder，零依赖、不发网络请求、结果可复现。
class LocalHashingEmbedder:
    """确定性本地 embedder(默认/兜底/测试):特征哈希 → 定长向量,L2 归一。零依赖、可复现。"""

    def __init__(self, dim: int = DEFAULT_EMBED_DIM) -> None:
        self._dim = max(8, int(dim))

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        for token in _features(text):
            bucket, sign = _hash_bucket(token, self._dim)
            vec[bucket] += sign
        return l2_normalize(vec)
