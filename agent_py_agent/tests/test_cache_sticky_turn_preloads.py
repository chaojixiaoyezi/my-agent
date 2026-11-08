"""前台本轮预载（创作/显式任务）写回线程：下一轮工具表不再缩回，缓存前缀不按提示词翻转。"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime import loop_support
from agent_py_agent.agent.conversation import tool_discovery
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.tests.test_cache_prefix_regression import CROSS_TURN_HIT_FLOOR, _environment


# 函数用途: 建一个真实线程存储和最小 agent，只持有 conversation_store。
def _store_case(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.create()
    return SimpleNamespace(conversation_store=store), store, thread


# 函数用途: 生成带线程身份的本轮参数；scope/attrs 可覆盖。
def _params(thread_id, *, scope="default", **attrs):
    return SimpleNamespace(context_scope=scope, task_attributes={"conversation_thread_id": thread_id, **attrs})


# LLM: 真实 Gateway ask + 缓存模拟器：第二轮提到“脚本”触发创作预载，第三轮普通追问必须沿用同一工具表并命中前缀。
# 函数用途: 钉住 e8353c857 引入的逐轮预载翻转（修复前第三轮首调命中≈0）。
def test_authoring_preload_stays_on_the_next_turn(tmp_path, monkeypatch):
    chain, simulator, scenario = _environment(tmp_path, monkeypatch)
    starts = []
    for prompt in ("SIM-TURN1 第一条：看一下工作区。", "SIM-TURN2 第二条：帮我写个脚本整理一下。",
                   "SIM-TURN3 第三条：最后确认一次。"):
        starts.append(len(simulator.calls))
        chain.ask("SIM", prompt)
    assert len(simulator.calls) == len(scenario.payloads)
    third = starts[2]
    assert json.dumps(scenario.payloads[third]["tools"], ensure_ascii=False) == json.dumps(
        scenario.payloads[third - 1]["tools"], ensure_ascii=False)
    record = simulator.calls[third]
    assert record.hit_tokens >= CROSS_TURN_HIT_FLOOR * record.prompt_tokens, (record.hit_tokens, record.prompt_tokens)
    thread = chain.agent.conversation_store.threads.require(chain.threads["SIM"])
    assert {"scripts", "workflows"} <= set(thread.loaded_tool_names)


# 函数用途: 只写线程里还没有的名字，按名称排序追加；重复回合不再写盘。
def test_persist_writes_only_fresh_names_in_sorted_order(tmp_path, monkeypatch):
    agent, store, thread = _store_case(tmp_path)
    store.threads.update_atomic(thread.thread_id, lambda t: replace(t, loaded_tool_names=("read_file",)))
    tool_discovery.persist_turn_preloaded_tools(agent, _params(thread.thread_id), {"workflows", "scripts", "read_file"})
    assert store.threads.require(thread.thread_id).loaded_tool_names == ("read_file", "scripts", "workflows")
    writes = []
    original = store.threads.update_atomic
    monkeypatch.setattr(store.threads, "update_atomic", lambda *a, **k: writes.append(a) or original(*a, **k))
    tool_discovery.persist_turn_preloaded_tools(agent, _params(thread.thread_id), {"scripts", "workflows"})
    assert writes == []


# 函数用途: 隔离/局部/控制面范围、子线程身份与子代理上下文都不写回本线程。
@pytest.mark.parametrize("case", ["task_local", "isolated", "control_plane", "agent_thread", "subagent"])
def test_persist_respects_main_thread_scope(tmp_path, case):
    from agent_py_agent.agent.runtime_context import (
        restore_current_subagent_context,
        set_current_subagent_context,
    )

    agent, store, thread = _store_case(tmp_path)
    params = _params(thread.thread_id, scope=case if case in {"task_local", "isolated", "control_plane"} else "default")
    if case == "agent_thread":
        params.task_attributes["agent_thread_id"] = thread.thread_id
    previous = set_current_subagent_context(agent, run_id="child") if case == "subagent" else None
    try:
        tool_discovery.persist_turn_preloaded_tools(agent, params, {"scripts"})
    finally:
        if previous is not None:
            restore_current_subagent_context(agent, previous)
    assert store.threads.require(thread.thread_id).loaded_tool_names == ()


# 函数用途: 后台唤醒轮沿原精确白名单展示预载，但不写回线程；前台同样的预载会写回。
@pytest.mark.parametrize("source", ["background_main_agent", "gateway"])
def test_background_turn_preloads_are_not_persisted(tmp_path, monkeypatch, source):
    from agent_py_agent.agent.capability import method_carry

    agent, store, thread = _store_case(tmp_path)
    monkeypatch.setattr(method_carry, "prompt_method_ids", lambda *_: ())
    snapshot = SimpleNamespace(available_tool_names=frozenset({"read_file", "skill_search", "scripts", "workflows"}),
                               runtimes=(), presentation_deferred_names=frozenset())
    params = SimpleNamespace(source=source, allowed_tools=None, user_prompt="帮我写个脚本", context_scope="default",
                             task_attributes={"conversation_thread_id": thread.thread_id}, turn_trigger=None, config=None,
                             owner_policy=None)
    seed = SimpleNamespace(effective_contract_snapshot=None, selected_skill_ids=(), tool_runtime_snapshot=snapshot)
    loaded = loop_support._initial_loop_loaded_tool_names(agent, params, seed, SimpleNamespace(loaded_tool_names=set()))
    assert {"scripts", "workflows"} <= loaded
    persisted = set(store.threads.require(thread.thread_id).loaded_tool_names)
    assert persisted == (set() if source == "background_main_agent" else {"skill_search", "scripts", "workflows"})
