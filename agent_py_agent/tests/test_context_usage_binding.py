"""F6 P2：真实临时store与原CAS；估算/账本/准备链不替换，只有并发时间点注入。"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import tool_model_generation as generation
from agent_py_agent.agent.agent_core.model import call_runtime, context_pressure
from agent_py_agent.agent.conversation.context_usage import (
    ContextUsageBinding,
    capture_context_usage_binding,
    record_model_context_usage,
)
from agent_py_agent.agent.conversation.models import ConversationCompactCommit
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.settings.model_scope import (
    ModelScopeDependencies,
    model_dependencies_scope,
    model_selection_snapshot,
)
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.tests.test_conversation_context_usage import usage
from agent_py_agent.tests.test_tool_model_generation import _tool_loop_params


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = (tmp_path / "explicit-home").resolve()
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    assert home_paths(str(home)).root == home
    assert home != (Path.home() / ".my-agent").resolve()
    return home


# LLM: 只创建临时store及原参数，backend没有generate，不可能调用真实供应商；准备/计量/账本函数保留。
# 函数用途: 为并发模型选择/压缩时间点提供准确线程、请求和配置。
def _case(home):
    store = ConversationStore(home / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "owner-a"})
    agent = SimpleNamespace(conversation_store=store,
        config=AgentConfig(my_agent_home=str(home), memory_context_calibration_carry_enabled=False),
        backend=SimpleNamespace(name="offline", model_name="sampled-old-model", max_tokens=128),
        _current_subagent_run_id="")
    params = replace(_tool_loop_params(), context_scope="conversation", request_id="request-a", run_id="run-a",
                     task_attributes={"conversation_thread_id": thread.thread_id})
    request = generation.ModelGenerateParams(agent, params, "完整合成上下文", 0)
    return agent, thread, request


def _advance(store, thread, change):
    if change == "model":
        return store.threads.update_atomic(thread.thread_id, lambda row: replace(row,
            model_profile_id="model-new", model_selection_revision=row.model_selection_revision + 1,
            model_selection_source="explicit", model_selection_last_explicit_revision=row.model_selection_revision + 1,
            model_context_usage={}))
    if change == "compact":
        return store.threads.update_compact_state(thread.thread_id, expected_generation=thread.compact_generation,
            commit=ConversationCompactCommit(summary="保留完整工作事实", operation_evidence={}, checkpoint_id="new-checkpoint",
                compacted_through_message_id="", compacted_through_byte_offset=0, source_messages=0, source_tool_pairs=0))
    return thread


@pytest.mark.parametrize("change", ["model", "compact"])
def test_captured_binding_rejects_late_model_or_generation_without_fresh_rebinding(isolated_home, change):
    agent, thread, request = _case(isolated_home)
    binding = capture_context_usage_binding(agent, request.params)
    assert isinstance(binding, ContextUsageBinding)
    _advance(agent.conversation_store, thread, change)
    assert record_model_context_usage(agent, request.params, usage(), binding=binding) is False
    assert agent.conversation_store.threads.require(thread.thread_id).model_context_usage == {}


def test_missing_binding_never_loads_latest_thread_to_bless_old_usage(isolated_home, monkeypatch):
    agent, thread, request = _case(isolated_home)
    _advance(agent.conversation_store, thread, "model")
    monkeypatch.setattr(agent.conversation_store.threads, "load_report", lambda *_: pytest.fail("保存点不得重新签版本"))
    assert record_model_context_usage(agent, request.params, usage()) is False


@pytest.mark.parametrize("change", ["none", "model", "compact"])
@pytest.mark.parametrize("entry", ["generation", "call-fallback"])
def test_real_call_preparation_carries_sampled_versions_to_original_cas(isolated_home, monkeypatch, change, entry):
    agent, thread, request = _case(isolated_home)
    target = generation if entry == "generation" else call_runtime
    original = context_pressure.model_visible_context_snapshot
    samples = []

    def measure_then_change(*args, **kwargs):
        measured = original(*args, **kwargs)
        samples.append(measured)
        _advance(agent.conversation_store, thread, change)
        return measured

    monkeypatch.setattr(target, "model_visible_context_snapshot", measure_then_change)
    if entry == "generation":
        state = generation._start_model_generation(request)
        ledger = state.ledger
    else:
        ledger, _, _ = call_runtime.start_model_call_record(request)
    assert len(samples) == 1
    captured = samples[0].usage_binding
    assert isinstance(captured, ContextUsageBinding)
    assert (captured.compact_generation, captured.model_selection_revision) == (0, 0)
    assert "usage_binding" not in samples[0].to_public_dict()
    assert "compact_generation" not in samples[0].to_public_dict()
    assert "model_selection_revision" not in samples[0].to_public_dict()
    assert len(ledger.records()) == 1 and ledger.records()[0].input_tokens == samples[0].current_tokens
    saved = agent.conversation_store.threads.require(thread.thread_id).model_context_usage
    if change == "none":
        assert saved["current_tokens"] == samples[0].current_tokens
        assert saved["model_selection_revision"] == saved["compact_generation"] == 0
    else:
        assert saved == {}


def test_already_bound_old_model_cannot_capture_new_selection_revision(isolated_home):
    agent, thread, request = _case(isolated_home)
    selection = model_selection_snapshot(thread, agent.config)
    dependencies = ModelScopeDependencies(agent, (("config", agent.config),), selection)
    with model_dependencies_scope(agent, dependencies):
        _advance(agent.conversation_store, thread, "model")
        assert capture_context_usage_binding(agent, request.params) is None
        ledger, _, _ = call_runtime.start_model_call_record(request)
    assert len(ledger.records()) == 1
    assert agent.conversation_store.threads.require(thread.thread_id).model_context_usage == {}


@pytest.mark.parametrize("fault", ["owner-store", "thread", "untyped", "bool-generation", "bool-revision"])
def test_capture_cannot_cross_store_thread_or_typed_boundary(isolated_home, fault):
    agent, thread, request = _case(isolated_home)
    binding = capture_context_usage_binding(agent, request.params)
    if fault == "owner-store":
        agent.conversation_store = ConversationStore(isolated_home / "other-owner")
    elif fault == "thread":
        request = replace(request, params=replace(request.params, task_attributes={"conversation_thread_id": "other"}))
    elif fault == "untyped":
        binding = SimpleNamespace(store=agent.conversation_store, thread_id=thread.thread_id,
                                  compact_generation=0, model_selection_revision=0)
    elif fault == "bool-generation":
        binding = replace(binding, compact_generation=False)
    else:
        binding = replace(binding, model_selection_revision=False)
    assert record_model_context_usage(agent, request.params, usage(), binding=binding) is False


def test_capture_read_failure_does_not_block_original_model_call(isolated_home, monkeypatch):
    agent, _thread, request = _case(isolated_home)

    def fail(*_):
        raise OSError("synthetic temporary read failure")

    monkeypatch.setattr(agent.conversation_store.threads, "load_report", fail)
    ledger, _, _ = call_runtime.start_model_call_record(request)
    assert len(ledger.records()) == 1


def test_budget_snapshot_does_not_capture_extra_telemetry_binding(isolated_home, monkeypatch):
    agent, thread, request = _case(isolated_home)
    reads = []
    original = agent.conversation_store.threads.load_report

    def record_read(thread_id):
        reads.append(thread_id)
        return original(thread_id)

    monkeypatch.setattr(agent.conversation_store.threads, "load_report", record_read)
    measured = context_pressure.model_visible_context_snapshot(agent, request.params, request.prompt)
    assert measured.usage_binding is None
    # 原校准水合与思考档位各读一次；纯计量不能因遥测再增加第三次读取。
    assert reads == [thread.thread_id, thread.thread_id]
