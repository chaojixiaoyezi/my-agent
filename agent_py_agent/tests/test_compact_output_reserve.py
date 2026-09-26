"""完整宿主请求与输出预留组合；摘要和末端HTTP为替身，计量、候选及CAS保持生产实现。"""
from __future__ import annotations

import json
from functools import partial
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import compact_request_recovery as recovery
from agent_py_agent.agent.agent_core.model.context_pressure import (
    projected_model_context_components,
)
from agent_py_agent.agent.conversation import background_execution, compact
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.gateway_parts import request_execution
from agent_py_agent.agent.settings import model_profiles
from agent_py_agent.tests.test_background_compact_recovery import _background
from agent_py_agent.tests.test_gateway_compact_recovery import _history_request
from agent_py_agent.tests.test_mixed_compact_recovery import _wire_from_material
from agent_py_agent.tests.test_model_profiles import add
from agent_py_agent.tests.test_subagent_compact_recovery import _child, _http

WINDOW = 200_000
OUTPUT = 50_000


# LLM: 复用隔离宿主和完整真实提示/schema；large 输入须落在触发线下、输出预留线上，不能通过删 schema 校准。
# 函数用途: 为三种宿主构造输出预留才会拒绝的候选；下方双边容量断言验证输入校准，不替换模型准备流程。
def _case(tmp_path, host, backend, large):
    requirement = "CURRENT_REQUIREMENT_BEGIN " + "a" * (425_000 if large else 1000) + " CURRENT_REQUIREMENT_END"
    if host == "gateway":
        fixture = _history_request(tmp_path, mode="disabled", tools=True, original_window=WINDOW)
        agent, tid = fixture.agent, fixture.thread_id
        profile, _ = add(agent, model_name="reserve-test", model_backend=backend, model_context_window_tokens=WINDOW)
        model_profiles.execute_model_profile_operation(agent, "select", {"profile_id": profile}, thread_id=tid)
        run = partial(request_execution._run_gateway_ask, fixture.context)
    elif host == "child":
        agent, task = _child(tmp_path, backend=backend, tools=False)
        tid = task.agent_thread_id
        run = partial(agent.run_subagent, task.id, instruction=requirement, dry_run=False, probe=False)
    else:
        agent, _, thread, request, execution, sink = _background(tmp_path, backend=backend, detached=False)
        tid = thread.thread_id
        run = partial(background_execution.run_background_turn_with_compact,
            execution, thread, request, user_prompt="继续核对材料", continuation_injection=[],
            proactive_delivery_available=False, activity_sink=sink)
    agent.config.max_tokens = OUTPUT
    agent.backend.max_tokens = OUTPUT
    if host != "child":
        agent.config.system_prompt += "\n" + requirement
    agent.config.memory_compact_auto_trigger_percent = 90
    for number in range(2):
        agent.conversation_store.messages.append({
            "thread_id": tid, "role": "user" if number == 0 else "assistant",
            "content": "OLD_SOURCE_BEGIN " + "b" * 360_000 + " OLD_SOURCE_END",
            "metadata": {"conversation_request_id": f"large-history-{number}"},
        })
    return agent, tid, run, requirement


@pytest.mark.parametrize("host", ["gateway", "child", "background"])
@pytest.mark.parametrize("backend", ["anthropic_compatible", "openai_compatible"])
@pytest.mark.parametrize("large", [False, True])
def test_full_candidate_preserves_required_output_and_current_input(tmp_path, monkeypatch, host, backend, large):
    agent, tid, run, requirement = _case(tmp_path, host, backend, large)
    projections, summaries, sent, instances = [], [], [], []
    original_project = recovery._project_mixed_recovery_material
    original_select = recovery.PreparedCompactRecovery.select

    def select(instance, *args):
        instances.append(instance)
        assert instance.agent.backend.max_tokens == OUTPUT
        return original_select(instance, *args)

    def project(*args):
        value = original_project(*args)
        if args[1].is_candidate:
            tokens, _ = projected_model_context_components(value.projection)
            wire = _wire_from_material(agent, value)
            assert wire["max_tokens"] == OUTPUT
            if host == "gateway":
                assert wire.get("tools"), "完整候选必须包含实际原生工具schema"
            assert requirement in json.dumps(wire, ensure_ascii=False)
            projections.append((tokens, wire))
        return value

    def summarize(*args, **kwargs):
        summaries.append(True)
        return "旧资料已完成核对，后续遵守当前完整要求。"

    monkeypatch.setattr(recovery.PreparedCompactRecovery, "select", select)
    monkeypatch.setattr(recovery, "_project_mixed_recovery_material", project)
    monkeypatch.setattr(compact, "_summarize", summarize)
    _http(monkeypatch, backend=backend, on_business=lambda wire, _: sent.append(wire))
    if large and host != "child":
        with pytest.raises((RuntimeError, ConversationCompactError)) as failure:
            run()
        assert getattr(failure.value, "error_code", None) == "COMPACT_CANDIDATE_TOO_LARGE"
    else:
        result = run()
        if host == "child":
            assert result.ok is not large, (result.message, result.runner_last_error)
    assert projections and summaries and instances, ((result.message, result.runner_last_error) if host == "child" else "no projection")
    measured = [tokens for tokens, _ in projections]
    assert min(measured) < WINDOW * .9, measured
    thread = agent.conversation_store.threads.require(tid)
    if large:
        assert min(measured) >= WINDOW - OUTPUT, measured
        assert sent == []
        assert thread.compact_generation == 0 and thread.compact_checkpoint_id == ""
        assert thread.summary == "" and thread.compacted_through_byte_offset == 0
        assert thread.compact_failure_code == "COMPACT_CANDIDATE_TOO_LARGE"
        assert all(not instance.committed for instance in instances)
    else:
        assert len(sent) == 1 and thread.compact_generation == 1
        selected = [(tokens, wire) for tokens, wire in projections if wire == sent[0]]
        assert len(selected) == 1 and selected[0][0] + OUTPUT < WINDOW
        assert instances[-1].committed
    assert all(instance.agent.config.max_tokens == OUTPUT for instance in instances)


@pytest.mark.parametrize("oauth", [False, True])
def test_responses_output_reserve_matches_actual_field_or_unknown(monkeypatch, oauth):
    from agent_py_agent.agent.agent_core.model.context_pressure import (
        _known_shared_window_output_reserve,
        model_request_input_ceiling,
    )
    from agent_py_agent.agent.backends.base import BackendOptions
    from agent_py_agent.agent.backends.responses import OpenAIResponsesBackend

    backend = OpenAIResponsesBackend(BackendOptions(
        "https://example.test/v1", "fake-key", "test-model", stream_enabled=False,
        max_tokens=OUTPUT, context_window_tokens=WINDOW,
    ))
    if oauth:
        backend.auth_ref = {"mode": "chatgpt"}
    agent = SimpleNamespace(backend=backend, config=SimpleNamespace(
        model_context_window_explicit=True, model_context_window_tokens=WINDOW,
    ))
    sent = []

    def send(path, payload, headers):
        sent.append(payload)
        return {"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": "完成"},
        ]}]}

    monkeypatch.setattr(backend, "request_json", send)
    assert backend.generate("完整保留当前要求").text == "完成"
    assert len(sent) == 1
    reserve = _known_shared_window_output_reserve(agent)
    if oauth:
        assert "max_output_tokens" not in sent[0] and reserve == 0
        assert model_request_input_ceiling(agent, WINDOW) == WINDOW  # 未知不代表输出有零消耗保证。
    else:
        assert sent[0]["max_output_tokens"] == reserve == OUTPUT
        assert model_request_input_ceiling(agent, WINDOW) == WINDOW - OUTPUT
    assert backend.max_tokens == OUTPUT
    assert backend.project_generate_payload("完整保留当前要求") is None  # 不借Chat投影证明Responses候选可用。
