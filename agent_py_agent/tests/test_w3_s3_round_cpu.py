"""S3 每轮固定 CPU：快照内 spec 读取不再逐次重算 schema hash，未变化的业务域工具不每轮重建，
同一请求内原生工具表对同一批 spec 只转换一次；按名查找与 Schema 转换仍各自拒绝原地篡改。
插话索引对已追平的目标在一次投影事务里完成恢复与读取。"""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import native_tool_protocol
from agent_py_agent.agent.backends import tool_schema
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import business_api
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.models import (
    EffectResolverPolicy,
    ToolModelSpec,
    ToolRuntime,
    ToolRuntimePolicy,
    ToolRuntimeSnapshot,
)


def _spec(name="probe_tool"):
    return ToolModelSpec(name, f"{name} probe", {
        "type": "object", "properties": {"limit": {"type": "integer", "minimum": 1}}, "required": [],
    })


def _snapshot(*specs):
    policy = ToolRuntimePolicy(EffectResolverPolicy("read_only"))
    runtimes = tuple(ToolRuntime(spec, policy, SimpleNamespace(model_spec=spec)) for spec in specs)
    return ToolRuntimeSnapshot(run_id="run-1", runtimes=runtimes,
                               available_tool_names=frozenset(spec.name for spec in specs),
                               unavailable_tools=(), allowed_tools=None)


def test_snapshot_spec_reads_reuse_the_construction_hash_check(monkeypatch):
    spec = _spec()
    snapshot = _snapshot(spec)
    calls = []
    original = ToolModelSpec.assert_schema_hash
    monkeypatch.setattr(ToolModelSpec, "assert_schema_hash", lambda self: (calls.append(self.name), original(self)))
    for _ in range(10):
        assert snapshot.specs == (spec,)
    assert calls == []
    # 按名查找仍在使用前重核：原地篡改照旧被拒。
    spec.input_schema["properties"]["limit"]["minimum"] = 0
    with pytest.raises(ValueError, match="mutated after snapshot"):
        snapshot.runtime(spec.name)


def _native_case(monkeypatch, specs):
    conversions = []
    original = tool_schema.tool_model_specs_to_anthropic_tools

    def counted(selected):
        conversions.append(tuple(spec.name for spec in selected))
        return original(selected)

    monkeypatch.setattr(tool_schema, "tool_model_specs_to_anthropic_tools", counted)
    monkeypatch.setattr(native_tool_protocol, "native_tool_use_active", lambda params: True)
    monkeypatch.setattr("agent_py_agent.agent.conversation.tool_discovery.seed_loop_discovery_order",
                        lambda agent, params: None)
    current = {"specs": list(specs)}
    registry = SimpleNamespace(model_visible_specs=lambda **kwargs: list(current["specs"]))
    agent = SimpleNamespace(tools=registry)
    params = SimpleNamespace(tool_runtime_snapshot=object(), allowed_tools=None, live_archive_state={},
                             _native_tools_memo={})
    return agent, params, conversions, current


def test_native_tools_convert_once_per_request_for_the_same_specs(monkeypatch):
    first, second = _spec("alpha"), _spec("beta")
    agent, params, conversions, current = _native_case(monkeypatch, (first, second))
    tools = native_tool_protocol.resolve_native_tools(agent, params)
    for _ in range(5):
        again = native_tool_protocol.resolve_native_tools(agent, params)
        assert again == tools and again is not tools
    assert conversions == [("alpha", "beta")]
    # 调用方改动返回值不回写缓存。
    tools[0]["input_schema"]["properties"].clear()
    assert native_tool_protocol.resolve_native_tools(agent, params)[0]["input_schema"]["properties"]
    # 换出新 spec 对象（快照重建、能力刷新、可见集合变化）时重新转换并重核。
    current["specs"] = [first, _spec("beta")]
    native_tool_protocol.resolve_native_tools(agent, params)
    current["specs"] = [first]
    native_tool_protocol.resolve_native_tools(agent, params)
    assert conversions == [("alpha", "beta"), ("alpha", "beta"), ("alpha",)]
    # 新的请求不共享缓存。
    fresh = SimpleNamespace(**{**vars(params), "_native_tools_memo": {}})
    native_tool_protocol.resolve_native_tools(agent, fresh)
    assert len(conversions) == 4


def test_unchanged_business_domain_tools_are_not_rebuilt_each_turn(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(home), memory_path="memory.jsonl",
                                    execution_mode="local_unmanaged"), tmp_path / "workspace")
    if not agent.config.business_spaces_enabled:
        pytest.skip("business spaces are disabled in this configuration")
    registered = dict(agent.tools.tools)
    assert "business_space" in registered
    built = []
    original = business_api._business_domain_model_spec
    monkeypatch.setattr(business_api, "_business_domain_model_spec",
                        lambda name, specs: (built.append(name), original(name, specs))[1])
    generation = getattr(agent.tools, "_tool_registry_generation", None)
    for _ in range(3):
        business_api.register_business_tools(agent)
    assert built == []
    assert getattr(agent.tools, "_tool_registry_generation", None) == generation
    assert all(agent.tools.tools[name] is registered[name] for name in registered)
    # 已登记工具的就绪动作变化时仍按原登记循环替换。
    stale = agent.tools.tools["business_space"]
    monkeypatch.setattr(stale, "_ready_action_names", ("sessions.space.get",))
    business_api.register_business_tools(agent)
    assert built == ["business_space"]
    assert agent.tools.tools["business_space"] is not stale


def _counted_projection_transactions(index, monkeypatch):
    opened = []
    original = index._connection

    @contextmanager
    def counted(*args, **kwargs):
        with original(*args, **kwargs) as conn:
            opened.append(conn)
            yield conn

    monkeypatch.setattr(index, "_connection", counted)
    return opened


def test_caught_up_guidance_targets_are_read_in_one_projection_transaction(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversations")
    index = store.guidance.ledger.pending_index()
    opened = _counted_projection_transactions(index, monkeypatch)
    # 每回合新的 request/run/task 目标：追平与读取共用一次写事务（原先三次）。
    for target_type, target_id in (("request", "r-new"), ("agent_run", "a-new"), ("task", "k-new")):
        opened.clear()
        assert index.rows(target_type, target_id, 20, False) == ([], [])
        assert len(opened) <= 1
    entry = store.guidance.append({"target_type": "request", "target_id": "r1", "message": "new"})
    opened.clear()
    rows, errors = index.rows("request", "r1", 20, False)
    assert [row["id"] for row in rows] == [entry.guidance_id] and errors == []
    assert len(opened) <= 1
    rows, errors = index.rows("request", "r1", 20, True)
    assert [row["id"] for row in rows] == [entry.guidance_id] and errors == []
    index.mark_delivered([entry.guidance_id], 10)
    assert index.rows("request", "r1", 20, False) == ([], [])


def test_unknown_guidance_receipts_are_still_published_before_the_read(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversations")
    index = store.guidance.ledger.pending_index()
    entry = store.guidance.append_once({"target_type": "request", "target_id": "r2", "message": "once"},
                                       dedupe_key="input-1")
    # 跨库提交窗口：投影仍停在提交结果未知，权威回执已是 pending。
    with index._connection() as conn:
        conn.execute("UPDATE pending SET status='unknown' WHERE id=?", (entry.guidance_id,))
    opened = _counted_projection_transactions(index, monkeypatch)
    rows, errors = index.rows("request", "r2", 20, False)
    assert [row["id"] for row in rows] == [entry.guidance_id] and errors == []
    assert len(opened) > 1
    with index._connection() as conn:
        assert conn.execute("SELECT status FROM pending WHERE id=?", (entry.guidance_id,)).fetchone()[0] == "pending"
