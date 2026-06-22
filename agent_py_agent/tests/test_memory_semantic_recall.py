"""检索拓宽 #1 P1 真测:记忆召回加语义向量一路 + RRF 融合(本地 per-owner 向量,不碰共享库)。

记忆召回原本纯关键词(FTS5/BM25),"换词就召不回"。本条加一路语义向量召回融合。向量只存各 owner
自己 home 的本地 memory_vectors.json(零外部依赖、不碰共享向量库、按 owner 隔离)。默认无 embedder=
纯关键词不变;embed-on-write 把记忆写进本地向量库;端点抖动/无 embedder 自动降级不崩。
"""

from __future__ import annotations

from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
from agent_py_agent.agent.retrieval.embedding import LocalHashingEmbedder


def test_no_embedder_is_pure_keyword(tmp_path) -> None:
    mem = JsonlMemory(tmp_path / "mem.jsonl")  # 无 embedder
    mem.add("user", "deploy the payment service")
    assert mem._embedder is None
    assert mem._vector_store() is None  # 不建向量库
    assert any("deploy" in r.content for r in mem.search("deploy"))  # 关键词召回照常


def test_embed_on_write_uses_local_vector_file(tmp_path) -> None:
    mem = JsonlMemory(tmp_path / "mem.jsonl", embedder=LocalHashingEmbedder(dim=64))
    mem.add("user", "客户预算五十万元")
    store = mem._vector_store()
    assert store is not None and len(store) == 1  # 写时 embed 进本地向量库
    assert (tmp_path / "memory_vectors.json").exists()  # ⭐ 向量在 owner home 本地,不碰共享向量库


def test_semantic_channel_returns_records(tmp_path) -> None:
    mem = JsonlMemory(tmp_path / "mem.jsonl", embedder=LocalHashingEmbedder(dim=128))
    mem.add("user", "项目总花费是五十万元")
    mem.add("user", "今天天气不错")
    hits = mem._semantic_records("项目总花费是五十万元", top_k=5)
    assert hits and any("五十万" in r.content for r in hits)  # 语义一路真能召回


def test_search_fuses_keyword_and_semantic(tmp_path) -> None:
    mem = JsonlMemory(tmp_path / "mem.jsonl", embedder=LocalHashingEmbedder(dim=128))
    mem.add("user", "alpha beta gamma delta")
    mem.add("user", "unrelated content here")
    results = mem.search("alpha beta", top_k=5)
    assert any("alpha" in r.content for r in results)  # 融合后仍召回到目标


def test_embedder_failure_degrades_to_keyword(tmp_path) -> None:
    class _BoomEmbedder:
        def embed(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("embeddings endpoint down")

    mem = JsonlMemory(tmp_path / "mem.jsonl", embedder=_BoomEmbedder())
    mem.add("user", "deploy the service now")  # embed-on-write 失败被吞,JSONL 仍写入
    results = mem.search("deploy")  # 语义路抛错 → 降级纯关键词,不崩
    assert any("deploy" in r.content for r in results)


def test_build_memory_embedder_gating(tmp_path) -> None:
    from agent_py_agent.agent.core import _build_memory_embedder
    from agent_py_agent.agent.settings.config import AgentConfig

    assert _build_memory_embedder(AgentConfig()) is None  # 默认关 → None
    assert _build_memory_embedder(AgentConfig(memory_semantic_recall=True)) is None  # 开但没配 model → None
    embedder = _build_memory_embedder(
        AgentConfig(memory_semantic_recall=True, memory_embedding_model="text-embedding-3-small")
    )
    assert embedder is not None  # 开 + 配 model → 建出 embedder
