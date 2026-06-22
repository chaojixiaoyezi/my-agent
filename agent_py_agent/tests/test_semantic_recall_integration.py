"""#1 P2 集成:语义召回经真实 SimpleAgent 接线 + 多租户向量隔离(确定性,不上网)。

钉死两件生产关切:① config→SimpleAgent→memory→embedder 整条接线真的通(默认关=无 embedder,
开了=memory 层真持有 embedder);② 把多用户隔离红线**延伸到新的本地向量层**——owner A 的语义记忆
owner B 召回不到、各自 memory_vectors.json 物理隔离。embedder 用本地确定性 LocalHashingEmbedder
(隔离/接线与 embedding 质量无关,无需上网);真机换词召回见 test_minimax_embedder_real.py。
"""

from __future__ import annotations

from agent_py_agent.agent import core
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.request_worker import _resolve_request_agent
from agent_py_agent.agent.retrieval.embedding import LocalHashingEmbedder, MiniMaxEmbedder
from agent_py_agent.agent.settings.config import AgentConfig


def _agent(tmp_path, *, scoping: bool = False, **kw) -> SimpleAgent:
    config = AgentConfig(
        model_backend="echo",
        my_agent_home=str(tmp_path / "home"),
        gateway_per_user_owner_scoping=scoping,
        **kw,
    )
    return SimpleAgent(config, tmp_path)


def _req(user_id: str) -> dict:
    return {"user_id": user_id, "metadata": {"user_id": user_id, "channel": "feishu"}}


def test_simpleagent_no_embedder_by_default(tmp_path) -> None:
    assert _agent(tmp_path).memory._embedder is None  # 默认关 → 纯 BM25,连 embedder 都不建


def test_simpleagent_wires_embedder_from_config(tmp_path) -> None:
    agent = _agent(
        tmp_path,
        memory_semantic_recall=True,
        memory_embedding_model="embo-01",
        memory_embedding_api_base="https://api.minimaxi.com/v1",
    )
    assert isinstance(agent.memory._embedder, MiniMaxEmbedder)  # 配置真的接到 memory 层(生产接线打通,构造不上网)


def test_per_user_vector_isolation(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(core, "_build_memory_embedder", lambda config: LocalHashingEmbedder(dim=128))  # 强制本地确定性
    base = _agent(tmp_path, scoping=True)
    alice = _resolve_request_agent(base, _req("alice"))
    bob = _resolve_request_agent(base, _req("bob"))
    alice.memory.add("user", "公司A的支付系统迁移预算五十万")

    a_store, b_store = alice.memory._vector_store(), bob.memory._vector_store()
    assert a_store is not None and b_store is not None
    assert a_store._path != b_store._path  # ⭐ 两 owner 的 memory_vectors.json 物理隔离
    assert len(a_store) >= 1 and len(b_store) == 0  # alice 写入只进自己向量库,bob 库空
    hits = bob.memory.search("支付系统迁移预算", top_k=5)
    assert not any("公司A" in r.content for r in hits)  # ⭐ 跨租户语义召回零泄漏


def test_scoping_off_shares_one_vector_store(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(core, "_build_memory_embedder", lambda config: LocalHashingEmbedder(dim=128))
    base = _agent(tmp_path, scoping=False)
    a = _resolve_request_agent(base, _req("alice"))
    b = _resolve_request_agent(base, _req("bob"))
    assert a is b is base  # 开关关 → 共享单 agent(现状不变),向量库自然也是同一个
