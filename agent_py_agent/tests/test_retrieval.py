"""Phase 2 检索子系统测试:BM25 词面 + 本地语义 embedder + 暴力向量库 + RRF 混合 + 降级。"""

from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.retrieval import (
    HybridRetriever,
    LocalHashingEmbedder,
    VectorStore,
    build_embedder,
    cosine,
    l2_normalize,
    rank,
    reciprocal_rank_fusion,
    tokenize,
)
from agent_py_agent.agent.retrieval.embedding import EmbeddingError, EmbeddingProvider


# --- 词面 BM25 ---
def test_tokenize_cjk_bigram_and_words() -> None:
    toks = tokenize("部署口令 deploy_v2")
    assert "部署" in toks and "署口" in toks and "口令" in toks  # 中文 bigram
    assert "deploy_v2" in toks  # 英文按词


def test_bm25_lifts_most_relevant_above_weak_matches() -> None:
    """治 my-agent 短板:真正相关的那条要顶到第一,而非被大量弱命中淹没。"""
    lines = [
        "今天天气不错适合散步",
        "部署 QQ 网关需要先配置 scoped lock 和 PID 文件",  # 最相关
        "QQ 是一个聊天软件",
        "网关大概是某种东西",
    ]
    order = rank("QQ 网关 scoped lock 部署", lines)
    assert order  # 有命中
    assert order[0] == 1  # 最相关那条排第一


def test_bm25_empty_query_or_lines() -> None:
    assert rank("", ["a", "b"]) == [0, 1]  # 切不出 token → 原序
    assert rank("x", []) == []


def test_reciprocal_rank_fusion() -> None:
    # b 在两路都排第一 → 融合分最高;a/c 各有一路靠前(平局按 id)
    fused = reciprocal_rank_fusion([["b", "a", "c"], ["b", "c", "a"]])
    assert fused[0][0] == "b"
    assert {f[0] for f in fused} == {"a", "b", "c"}


# --- 语义 embedder + cosine ---
def test_local_hashing_embedder_deterministic_and_normalized() -> None:
    emb = LocalHashingEmbedder(dim=128)
    v1 = emb.embed(["子代理交付护城河"])[0]
    v2 = emb.embed(["子代理交付护城河"])[0]
    assert v1 == v2  # 确定性
    assert abs(sum(x * x for x in v1) - 1.0) < 1e-6  # L2 归一
    assert emb.dim == 128


def test_cosine_similar_more_than_dissimilar() -> None:
    emb = LocalHashingEmbedder(dim=256)
    base = emb.embed(["压缩恢复 compaction resume 子系统"])[0]
    near = emb.embed(["compaction resume 压缩续航"])[0]
    far = emb.embed(["今天午饭吃什么"])[0]
    assert cosine(base, near) > cosine(base, far)


def test_l2_normalize_zero_vector() -> None:
    assert l2_normalize([0.0, 0.0]) == [0.0, 0.0]


# --- 向量库 ---
def test_vector_store_upsert_search_persist(tmp_path: Path) -> None:
    emb = LocalHashingEmbedder(dim=128)
    vs = VectorStore(tmp_path / "vecs.json")
    for i, text in enumerate(["压缩恢复子系统", "QQ 网关稳定性", "向量记忆检索"]):
        vs.upsert(f"m{i}", emb.embed([text])[0], text=text, metadata={"i": i})
    hits = vs.search(emb.embed(["compaction 压缩恢复"])[0], top_k=2)
    assert len(hits) == 2
    assert hits[0].id == "m0"  # 最相关
    assert hits[0].text == "压缩恢复子系统"
    # 持久化:新实例从同文件加载
    assert len(VectorStore(tmp_path / "vecs.json")) == 3


def test_vector_store_remove(tmp_path: Path) -> None:
    vs = VectorStore(tmp_path / "vecs.json")
    vs.upsert("a", [1.0, 0.0], text="x")
    assert vs.remove("a") is True
    assert vs.remove("a") is False
    assert len(vs) == 0


# --- 混合检索 ---
def test_hybrid_bm25_only_without_embedder() -> None:
    r = HybridRetriever(embedder=None)
    docs = [("d1", "无关内容"), ("d2", "QQ 网关 scoped lock 部署"), ("d3", "网关")]
    out = r.rank("QQ 网关 scoped lock", docs, top_k=3)
    assert out[0][0] == "d2"  # 纯 BM25 也能把最相关顶第一


def test_hybrid_fuses_bm25_and_vector() -> None:
    r = HybridRetriever(embedder=LocalHashingEmbedder(dim=256))
    docs = [
        ("d1", "今天天气"),
        ("d2", "压缩恢复 compaction resume 子系统设计"),
        ("d3", "compaction 续航与 resume 包重建"),  # 语义近 query 但词面不全同
    ]
    out = r.rank("compaction resume 压缩续航", docs, top_k=3)
    ids = [o[0] for o in out]
    # d2/d3 都相关 → 进结果;d1 无关(BM25=0 且 cosine=0)→ 被正确排除
    assert "d2" in ids and "d3" in ids
    assert "d1" not in ids
    assert ids[0] in ("d2", "d3")  # 最相关的排第一


def test_hybrid_degrades_to_bm25_on_embedding_error() -> None:
    class BrokenEmbedder:
        dim = 8

        def embed(self, texts: list[str]) -> list[list[float]]:
            raise EmbeddingError("endpoint down")

    assert isinstance(BrokenEmbedder(), EmbeddingProvider)  # 满足协议
    r = HybridRetriever(embedder=BrokenEmbedder())
    docs = [("d1", "无关"), ("d2", "QQ 网关 scoped lock 部署")]
    out = r.rank("QQ 网关 部署", docs, top_k=2)
    assert out[0][0] == "d2"  # 端点挂了仍降级 BM25 正常返回


# --- build_embedder 工厂 ---
def test_build_embedder_local_and_none() -> None:
    assert isinstance(build_embedder({"provider": "local", "dim": 64}), LocalHashingEmbedder)
    assert build_embedder(None) is None
    assert build_embedder({"provider": "unknown"}) is None
    assert build_embedder({"provider": "openai_compatible"}) is None  # 缺 api_base/model


def test_build_embedder_openai_compatible_resolves_secret() -> None:
    seen = {}

    def resolver(source: str) -> str:
        seen["source"] = source
        return "resolved-key"

    emb = build_embedder(
        {"provider": "openai_compatible", "api_base": "https://x/v1", "model": "embed-1", "api_key_source": "env:K"},
        secret_resolver=resolver,
    )
    assert emb is not None
    assert seen["source"] == "env:K"  # 密钥经 by-ref 解析(接 Phase 1)
