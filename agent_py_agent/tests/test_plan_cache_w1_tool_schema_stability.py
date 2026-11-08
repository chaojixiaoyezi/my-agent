"""真实线程与原生工具循环验证预载持久化和发现后的完整schema稳定性。"""
from __future__ import annotations

import json

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests.test_heavy_user_cache_and_recommendation_gate import _CaptureProvider


@pytest.fixture
def schema_case(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agent = SimpleAgent(AgentConfig(
        model_backend="echo", my_agent_home=str(tmp_path / "home"),
        memory_path=str(tmp_path / "memory.jsonl"), enable_tools=True,
        enable_self_learning=False, prompt_files=[], model_context_window_tokens=262144,
        max_tokens=128, memory_compact_remote_enabled=False, tool_default_deferral_enabled=True,
    ), workspace)
    provider = _CaptureProvider()
    agent.backend = provider
    thread = agent.conversation_store.threads.get_or_create({
        "channel": "test", "channel_conversation_id": "cache-schema-stability",
    })
    return agent, provider, thread


def _turn(case, request_id, *, preloads=(), actions=()):
    agent, provider, thread = case
    provider.phase, provider.actions = request_id, list(actions)
    before = len(provider.calls)
    result = agent.run("继续核对当前处理步骤", params=RunParams(
        save=False, context_scope="conversation", request_id=request_id, run_id=request_id,
        task_attributes={"conversation_thread_id": thread.thread_id, "requested_tool_names": list(preloads)},
    ))
    assert result.response == "GATE1完成" and not provider.actions
    return provider.calls[before:]


def _wire_tools(call):
    return json.dumps(call["tools"], ensure_ascii=False, separators=(",", ":"))


@pytest.mark.parametrize("preloads", [
    ("scripts", "workflows", "skill_search"), ("skill_search", "workflows", "scripts"),
])
def test_explicit_preloads_keep_complete_tool_schemas_on_followup(schema_case, preloads):
    agent, _provider, thread = schema_case
    first = _turn(schema_case, "preload", preloads=preloads)
    assert len(first) == 1
    assert agent.conversation_store.threads.require(thread.thread_id).loaded_tool_names == (
        "scripts", "skill_search", "workflows",
    )
    followup = _turn(schema_case, "followup")
    assert len(followup) == 1
    assert _wire_tools(followup[0]) == _wire_tools(first[0])
    assert followup[0]["layout"].stable_prefix == first[0]["layout"].stable_prefix


def test_search_appends_one_schema_after_preloads_and_keeps_it_on_followup(schema_case):
    agent, _provider, thread = schema_case
    first = _turn(schema_case, "preload", preloads=("scripts", "workflows", "skill_search"))[0]
    assert "package_build" not in [tool["name"] for tool in first["tools"]]
    discovery = _turn(schema_case, "discover", actions=(("tool_search", {"names": ["package_build"]}),))
    assert len(discovery) == 2
    assert _wire_tools(discovery[0]) == _wire_tools(first)
    assert discovery[-1]["tools"][:-1] == first["tools"]
    assert discovery[-1]["tools"][-1]["name"] == "package_build"
    assert agent.conversation_store.threads.require(thread.thread_id).loaded_tool_names == (
        "scripts", "skill_search", "workflows", "package_build",
    )
    followup = _turn(schema_case, "followup")
    assert len(followup) == 1
    assert _wire_tools(followup[0]) == _wire_tools(discovery[-1])
    assert followup[0]["layout"].stable_prefix == first["layout"].stable_prefix
