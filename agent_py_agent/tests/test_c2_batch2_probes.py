# LLM: 冻结审计回归只用临时owner、真实CAS/原生投影和受控摘要响应；不访问生产记忆或供应商。
# 模块用途: 复现后台预载、发现新近性、并发压缩、metadata隐藏和HOT预算缺陷；W1-5默认32项，小限额显式传入。
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.tests.test_compact_scoped_checkpoint import compact_case as compact_case


@pytest.mark.parametrize("reason,required", [
    ("scheduled_task_due", {"schedule"}),
    ("audit_continue", {"watch_stream"}),
    ("subagent_runner_finished", {"create_subagents", "list_agents", "send_guidance"}),
])
def test_background_preload_uses_real_policy(reason, required):
    from agent_py_agent.agent.agent_core.runtime import loop_support as loop

    loaded = loop._background_preload_tool_names(SimpleNamespace(turn_trigger=None), {
        loop.CONVERSATION_BACKGROUND_EVENT_REASON_ATTR: reason,
    })
    assert required <= loaded


def test_background_policy_receives_structured_trigger_not_event_prose(monkeypatch):
    from agent_py_agent.agent.agent_core.runtime import loop_support as loop
    from agent_py_agent.agent.agent_core.runtime.turn_trigger import TurnTrigger
    from agent_py_agent.agent.conversation import background_tool_policy as policy

    received = []
    def capture(request):
        received.append(request)
        return frozenset({"read_file"})
    monkeypatch.setattr(policy, "background_initially_presented_tools", capture)
    params = SimpleNamespace(turn_trigger=TurnTrigger("lifecycle_wake", reason="subagent_runner_finished",
                             source_agent_id="child", event_facts='{"reason":"audit_continue"}'),
                             allowed_tools=["read_file"])
    assert loop._background_preload_tool_names(params, {"thread_goal_id": "g"}) == {"read_file"}
    assert len(received) == 1
    assert received[0].reason == "subagent_runner_finished"
    assert received[0].active_goal
    assert received[0].wake_signal["source_agent_id"] == "child"


def test_rediscovery_refreshes_history_without_reordering_wire(tmp_path):
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.conversation.tool_discovery import persist_discovered_tools
    from agent_py_agent.agent.tooling.tool_search_state import resolve_active_loaded_tool_names

    store = ConversationStore(tmp_path)
    thread = store.threads.get_or_create({"canonical_user_id": "local"})
    agent = SimpleNamespace(conversation_store=store)
    params = SimpleNamespace(task_attributes={"conversation_thread_id": thread.thread_id})
    history = [f"t{i}" for i in range(1, 10)]
    for names in (history, ["t1"]):
        persist_discovered_tools(agent, params, {"tool_round": 1,
            "tool_result_envelope": {"tool_search": {"loaded_tool_names": names}}})
    current = store.threads.require(thread.thread_id)
    assert current.loaded_tool_names == tuple(history)
    assert current.metadata["discovered_tool_history"] == [*history[1:], "t1"]
    assert "t1" in resolve_active_loaded_tool_names(current.metadata["discovered_tool_history"])


# LLM: 显式8项只验证旧新近性算法，不再把8误作会话默认值；默认32另有回归。
# 函数用途: 保留有限容量下的末次发现优先与wire相对顺序保护。
def test_active_selection_uses_last_occurrence_but_preserves_selected_wire_order():
    from agent_py_agent.agent.tooling.tool_search_state import resolve_active_loaded_tool_names

    history = [f"t{i}" for i in range(1, 10)]
    loaded = [*history[1:], "t1"]
    selected = resolve_active_loaded_tool_names([*history, *loaded], max_active_discovered=8)
    assert selected == ("t1", *history[2:])


def test_blind_call_after_tuple_seed_preserves_wire_prefix():
    from agent_py_agent.agent.agent_core.tool_loop.deferred_schema_reload import (
        reload_schema_after_blind_call,
    )
    from agent_py_agent.agent.conversation.tool_discovery import seed_loop_discovery_order
    from agent_py_agent.agent.tooling.tool_search_state import ordered_loop_loaded_tool_names

    params = SimpleNamespace(loaded_tool_names={"zeta_tool"}, live_archive_state={}, allowed_tools=None,
                             task_attributes={}, tool_runtime_snapshot=SimpleNamespace(runtime=lambda _name: object()))
    agent = SimpleNamespace(conversation_store=None, tools=SimpleNamespace(
        catalog_default_deferral_enabled=True, model_visible_specs=lambda **_kw: []))
    seed_loop_discovery_order(agent, params)
    for name in ("zz_blind", "aa_blind"):
        assert reload_schema_after_blind_call(agent, params, {
            "tool": name, "ok": False, "tool_execution": {"failure_stage": "validation"},
        })
    assert ordered_loop_loaded_tool_names(params) == ("zeta_tool", "zz_blind", "aa_blind")


def test_metadata_is_hidden_without_losing_persistence():
    from agent_py_agent.agent.conversation.models import (
        MODEL_HIDDEN_THREAD_FIELDS,
        ConversationThread,
    )

    thread = ConversationThread(thread_id="t", canonical_user_id="local", metadata={"discovered_tool_history": ["x"]})
    assert "metadata" in MODEL_HIDDEN_THREAD_FIELDS
    restored = ConversationThread.from_dict(thread.to_dict())
    assert restored.metadata == thread.metadata
    visible = {k: v for k, v in restored.to_dict().items() if k not in MODEL_HIDDEN_THREAD_FIELDS}
    assert "discovered_tool_history" not in str(visible)


# LLM: 真实transcript Compact须保留9个已发现项及原近期使用事实，不调用外部供应商。
# 函数用途: 验证新默认容量不在压缩时回收旧工具。
def test_transcript_compact_prioritizes_real_recent_tool_use(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.compact import (
        ConversationCompactOptions,
        prepare_conversation_context,
    )
    from agent_py_agent.agent.conversation.native_history import canonical_native_messages_envelope
    from agent_py_agent.tests.test_compactcall_websocket import _Probe, _setup

    agent, thread, _, _ = _setup(tmp_path, monkeypatch, True)
    _Probe(monkeypatch, True, live=False)
    history = tuple(f"t{i}" for i in range(1, 10))
    thread = agent.conversation_store.threads.update_atomic(thread.thread_id, lambda current: replace(
        current, loaded_tool_names=history, metadata={"discovered_tool_history": list(history)}))
    agent.conversation_store.messages.append({"thread_id": thread.thread_id, "role": "assistant", "content": "used",
        "metadata": {"canonical_native_messages": canonical_native_messages_envelope([
            {"role": "assistant", "content": [{"type": "tool_use", "id": "call", "name": "t1", "input": {}}]},
        ])}})
    result = prepare_conversation_context(agent, agent.conversation_store, thread,
        options=ConversationCompactOptions(force=True))
    assert result.compacted
    assert "t1" in result.thread.loaded_tool_names
    assert result.thread.loaded_tool_names == history


# LLM: 原真实CAS注入并发发现，W1-5不改变元数据历史或代次提交条件。
# 函数用途: 保证压缩前9项和压缩期间新发现的一项都保留。
def test_live_compact_bounds_active_tools_and_preserves_concurrent_discovery(compact_case):
    from agent_py_agent.agent.conversation.live_tool_compact import (
        LiveToolCompactBinding,
        LiveToolCompactCommitRequest,
        commit_live_tool_compact,
    )
    from agent_py_agent.agent.conversation.tool_discovery import persist_discovered_tools

    case = compact_case
    history = tuple(f"t{i}" for i in range(1, 10))
    before = case.store.threads.update_atomic(case.thread_id, lambda current: replace(
        current, loaded_tool_names=history, metadata={"discovered_tool_history": list(history)}))
    def concurrent():
        persist_discovered_tools(SimpleNamespace(conversation_store=case.store),
            SimpleNamespace(task_attributes={"conversation_thread_id": case.thread_id}),
            {"tool_result_envelope": {"tool_search": {"loaded_tool_names": ["new"]}}})
    updated = commit_live_tool_compact(case.agent, LiveToolCompactBinding(case.store, before),
        LiveToolCompactCommitRequest(summary="summary", source_tool_call_ids=("call",), retained_tool_call_ids=(),
            projected_tokens_before=900, projected_tokens_after=300, policy=case.policy, request_id="r", attempt_id="a",
            source_tool_refs=({"run_id": "r", "attempt_id": "a", "turn_id": "t", "call_id": "call"},),
            after_checkpoint=concurrent))
    # W1-5：默认32项容量，保留9个原项及压缩期间新发现的成员。
    assert updated.loaded_tool_names == (*history, "new")
    assert updated.metadata["discovered_tool_history"] == [*history, "new"]
    assert updated.compact_generation == 1


def _memory(kind, content, entry_id):
    from agent_py_agent.agent.memory_store import MemoryRecord

    return MemoryRecord(role="user", content=content, kind=kind, entry_id=entry_id,
                        created_at=1, updated_at=1, attributes={"origin": "user_explicit"})


# LLM: 原生tool_use才是使用信号，默认32保留9项；不以正文补造工具。
# 函数用途: 验证live Compact后旧工具及其它已发现项不丢失。
def test_live_compact_preserves_old_tool_used_in_recent_message(compact_case):
    from agent_py_agent.agent.conversation.live_tool_compact import (
        LiveToolCompactBinding,
        LiveToolCompactCommitRequest,
        commit_live_tool_compact,
    )
    from agent_py_agent.agent.conversation.native_history import canonical_native_messages_envelope

    case = compact_case
    history = tuple(f"t{i}" for i in range(1, 10))
    before = case.store.threads.update_atomic(case.thread_id, lambda current: replace(
        current, loaded_tool_names=history, metadata={"discovered_tool_history": list(history)}))
    case.store.messages.append({"thread_id": case.thread_id, "role": "assistant", "content": "used",
        "metadata": {"canonical_native_messages": canonical_native_messages_envelope([
            {"role": "assistant", "content": [{"type": "tool_use", "id": "used", "name": "t1", "input": {}}]},
        ])}})
    updated = commit_live_tool_compact(case.agent, LiveToolCompactBinding(case.store, before),
        LiveToolCompactCommitRequest(summary="summary", source_tool_call_ids=("call",), retained_tool_call_ids=(),
            projected_tokens_before=900, projected_tokens_after=300, policy=case.policy, request_id="r", attempt_id="a",
            source_tool_refs=({"run_id": "r", "attempt_id": "a", "turn_id": "t", "call_id": "call"},)))
    assert "t1" in updated.loaded_tool_names
    assert updated.loaded_tool_names == history


# LLM: 保留全部并发时序变体及重搜版本断言，只把旧8项默认预期升级为完整9项。
# 函数用途: 防止摘要期间的工具重搜被压缩提交覆盖。
@pytest.mark.parametrize("requery", ["t1", "t9"])
@pytest.mark.parametrize("full_recent", [False, True])
@pytest.mark.parametrize("versioned_baseline", [False, True])
def test_transcript_cas_preserves_existing_tool_rediscovered_during_summary(
    tmp_path, monkeypatch, requery, full_recent, versioned_baseline,
):
    from agent_py_agent.agent.conversation import compact_request_budget
    from agent_py_agent.agent.conversation.compact import (
        ConversationCompactOptions,
        prepare_conversation_context,
    )
    from agent_py_agent.agent.conversation.native_history import canonical_native_messages_envelope
    from agent_py_agent.agent.conversation.tool_discovery import persist_discovered_tools
    from agent_py_agent.tests.test_compactcall_websocket import _Probe, _setup

    agent, thread, _, _ = _setup(tmp_path, monkeypatch, True)
    _Probe(monkeypatch, True, live=False)
    history = tuple(f"t{i}" for i in range(1, 10))
    thread = agent.conversation_store.threads.update_atomic(thread.thread_id, lambda current: replace(
        current, loaded_tool_names=history, metadata={"discovered_tool_history": list(history)}))
    params = SimpleNamespace(task_attributes={"conversation_thread_id": thread.thread_id})
    if versioned_baseline:
        persist_discovered_tools(agent, params,
            {"tool_result_envelope": {"tool_search": {"loaded_tool_names": list(history)}}})
        thread = agent.conversation_store.threads.require(thread.thread_id)
    if full_recent:
        agent.conversation_store.messages.append({"thread_id": thread.thread_id, "role": "assistant", "content": "used",
            "metadata": {"canonical_native_messages": canonical_native_messages_envelope([
                {"role": "assistant", "content": [{"type": "tool_use", "id": name, "name": name, "input": {}}
                    for name in history if name != requery]},
            ])}})
    generate = compact_request_budget.generate_auxiliary_model_response
    def interleave(request):
        persist_discovered_tools(agent, params,
            {"tool_result_envelope": {"tool_search": {"loaded_tool_names": [requery]}}})
        return generate(request)
    monkeypatch.setattr(compact_request_budget, "generate_auxiliary_model_response", interleave)
    result = prepare_conversation_context(agent, agent.conversation_store, thread,
        options=ConversationCompactOptions(force=True))
    assert result.compacted
    assert requery in result.thread.loaded_tool_names
    assert result.thread.loaded_tool_names == history
    assert result.thread.metadata["discovered_tool_history"][-1] == requery


def test_hot_4800_content_is_preserved_in_formal_envelope():
    from agent_py_agent.agent.agent_core.runtime.loop_support import _budgeted_formal_memories
    from agent_py_agent.agent.prompting_parts.memory_context import memory_context_text

    records = [_memory("hot", "规" * 600, f"hot-{i}") for i in range(8)]
    kept = _budgeted_formal_memories(records)
    assert [row.entry_id for row in kept] == [row.entry_id for row in records]
    assert "规" * 600 in memory_context_text(kept)


def test_memory_budget_omissions_are_counted_and_stay_inside_safe_envelope():
    from agent_py_agent.agent.agent_core.runtime.loop_support import _budgeted_formal_memories
    from agent_py_agent.agent.conversation.user_visible_text import sanitize_user_visible_text
    from agent_py_agent.agent.prompting_parts.memory_context import memory_context_text

    records = [_memory("fact", "事实" * 700, f"fact-{i}") for i in range(5)]
    kept = _budgeted_formal_memories(records)
    omitted = len(records) - len(kept)
    assert omitted > 0
    rendered = memory_context_text(kept)
    assert f"{omitted} formal memory entries omitted" in rendered
    assert "memory_search" in rendered
    assert sanitize_user_visible_text("answer\n" + rendered).content == "answer"
    assert memory_context_text(_budgeted_formal_memories(kept)) == rendered


# LLM: 显式8项保持旧算法反例，默认容量的改变不能掩盖近期使用顺序退化。
# 函数用途: 验证实际末次调用优先，而非首次调用优先。
def test_compact_recent_use_extraction_uses_last_call_not_first_call():
    from agent_py_agent.agent.conversation.models import MessageLogEntry
    from agent_py_agent.agent.conversation.native_history import canonical_native_messages_envelope
    from agent_py_agent.agent.conversation.tool_discovery import (
        active_tools_for_compact,
        recent_used_tool_names_from_messages,
    )

    history = tuple(f"t{i}" for i in range(1, 10))
    row = MessageLogEntry("m", "thread", "assistant", "prose is not a usage signal", metadata={
        "canonical_native_messages": canonical_native_messages_envelope([{"role": "assistant", "content": [
            {"type": "tool_use", "id": str(i), "name": name, "input": {}}
            for i, name in enumerate([*history, "t1"])
        ]}]),
    })
    recent = recent_used_tool_names_from_messages([row])
    selected = active_tools_for_compact(SimpleNamespace(loaded_tool_names=history,
        metadata={"discovered_tool_history": list(history)}), recent_used_tool_names=recent, max_active_discovered=8)
    assert selected == ("t1", *history[2:])
