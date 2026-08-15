"""Phase 3 端到端验证:多用户飞书 per-用户隔离真生效(记忆不串户 + 成本/审计按用户分)。

纯验证(不改源码):Phase 1 池 + Phase 2 网关接线已把每个飞书用户跑在自己 owner 作用域的 agent 上。
这里真建两个用户的作用域 agent,断言:① A 写的记忆 B 召回不到(数据隔离的核心证明);② 成本按各自
飞书用户(owner)分账;③ 开关关时回退共享单 agent(现状不变)。
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.tool_model_generation import _record_run_cost
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.request_worker import _resolve_request_agent
from agent_py_agent.agent.llm_scale.cost_ledger import (
    global_cost_ledger,
    reset_global_cost_ledger_for_test,
)
from agent_py_agent.agent.settings.config import AgentConfig


def _gateway_agent(tmp_path, *, scoping: bool) -> SimpleAgent:
    config = AgentConfig(
        model_backend="echo",
        model_name="claude-opus-4-8",  # 让 backend.model_name 非空,成本可计(echo 默认无 model)
        my_agent_home=str(tmp_path / "home"),
        gateway_per_user_owner_scoping=scoping,
    )
    return SimpleAgent(config, tmp_path)


def _req(user_id: str) -> dict:
    return {"user_id": user_id, "metadata": {"user_id": user_id, "channel": "feishu"}}


def test_memory_isolation_user_a_write_user_b_cannot_read(tmp_path) -> None:
    base = _gateway_agent(tmp_path, scoping=True)
    alice = _resolve_request_agent(base, _req("alice"))
    bob = _resolve_request_agent(base, _req("bob"))
    alice.memory.add("user", "alicesecretnote 公司A机密合同金额")
    # A 召回得到自己的记忆
    assert any("alicesecretnote" in record.content for record in alice.memory.search("alicesecretnote"))
    # ⭐ B 召回不到 A 的记忆(数据隔离 = 不串户,多公司多用户的硬要求)
    assert not any("alicesecretnote" in record.content for record in bob.memory.search("alicesecretnote"))


def test_same_feishu_chat_id_does_not_share_history_between_users(tmp_path) -> None:
    base = _gateway_agent(tmp_path, scoping=True)
    alice = _resolve_request_agent(base, _req("alice"))
    bob = _resolve_request_agent(base, _req("bob"))
    alice_thread = alice.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": "alice",
            "channel": "feishu",
            "channel_conversation_id": "oc-shared-group",
            "channel_user_id": "alice",
        }
    )
    alice.conversation_store.append_message(
        {"thread_id": alice_thread.thread_id, "role": "user", "content": "只属于 Alice 的上下文"}
    )
    bob_thread = bob.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": "bob",
            "channel": "feishu",
            "channel_conversation_id": "oc-shared-group",
            "channel_user_id": "bob",
        }
    )
    assert bob.conversation_store.recent_messages(bob_thread.thread_id, limit=10) == []


def test_cost_attributed_per_feishu_user(tmp_path) -> None:
    reset_global_cost_ledger_for_test()
    base = _gateway_agent(tmp_path, scoping=True)
    alice = _resolve_request_agent(base, _req("alice"))
    bob = _resolve_request_agent(base, _req("bob"))
    # 作用域 agent 的 config owner = 各自飞书用户 → 既有 #2 成本/ #13 审计自动按用户分
    assert alice.config.my_agent_owner_id == "alice" and bob.config.my_agent_owner_id == "bob"
    resp = SimpleNamespace(usage={"input_tokens": 1_000_000, "output_tokens": 0})
    _record_run_cost(SimpleNamespace(agent=alice, params=SimpleNamespace(run_id="run-a")), resp)
    _record_run_cost(SimpleNamespace(agent=bob, params=SimpleNamespace(run_id="run-b")), resp)
    ledger = global_cost_ledger()
    assert ledger.tenant_cost("alice") > 0 and ledger.tenant_cost("bob") > 0  # 各自记到名下
    assert ledger.run_cost("run-a") > 0 and ledger.run_cost("run-b") > 0


def test_scoping_off_shares_single_agent(tmp_path) -> None:
    base = _gateway_agent(tmp_path, scoping=False)
    a = _resolve_request_agent(base, _req("alice"))
    b = _resolve_request_agent(base, _req("bob"))
    assert a is b is base  # 开关关 → 同一个 agent(现状:共享,行为不变)
