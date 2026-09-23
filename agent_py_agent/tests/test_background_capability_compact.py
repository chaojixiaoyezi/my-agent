"""后台 Compact 同一工作片恢复展示，下一片重新评估；只用原运行链和本地 fake provider。"""
from dataclasses import asdict, replace
from functools import partial
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import background_execution as execution
from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest, _run_params
from agent_py_agent.tests.test_decision_capability_consumer import provider
from agent_py_agent.tests.test_decision_settings import patch
from agent_py_agent.tests.test_subagent_capability_compact import (
    append_prior_turn,
    assert_retained_provider_surface,
    capability_host,  # noqa: F401
    capture_host_runs,
    tool_surface,  # noqa: F401
)


# LLM: 线程、历史、参数和活动接收器均经原后台入口；新工作片只变原 request.conversation_turn_id，不另建持久载体。
# 函数用途: 运行一个可复用 task 身份的后台业务片，保留完整模型和 Compact 调用用于断言。
def run_slice(fixture, request):
    store = fixture.agent.conversation_store
    return execution.run_background_turn_with_compact(
        execution.BackgroundExecutionDependencies(agent=fixture.agent, store=store,
            prepare_run=partial(_run_params, request=request, agent=fixture.agent)),
        store.threads.load(request.thread_id), request, user_prompt="继续核对来源事实", continuation_injection=[],
        proactive_delivery_available=False,
        activity_sink=execution.BackgroundMainActivitySink(fixture.agent, thread_id=request.thread_id, task_id=request.task_id),
    )


@pytest.mark.parametrize("choice", [None, "not_needed"])
@pytest.mark.parametrize("existing_main", [False, True])
def test_background_retry_keeps_selection_and_provider_surface_then_new_turn_resets(capability_host, monkeypatch, choice, existing_main):  # noqa: F811
    fixture = capability_host
    thread = fixture.agent.conversation_store.threads.get_or_create({"channel": "chat", "channel_conversation_id": "background"})
    append_prior_turn(fixture.agent, thread.thread_id)
    request = BackgroundRunRequest(thread_id=thread.thread_id, task_id="background-task", conversation_turn_id="background-turn-1")
    if existing_main:
        fixture.agent.subagents.runtime_db.record_run_creation(
            owner_id=fixture.agent.home_paths.owner_id, goal="核对来源事实", conversation_task_id=request.task_id,
            thread_id=request.thread_id, run_id="foreground-run", role="main",
        )
    calls = provider(monkeypatch, choice=choice)
    captured = capture_host_runs(monkeypatch, fixture.agent)
    result = run_slice(fixture, request)
    assert result.runtime_status == "ok"
    assert len(calls) == 1 and len(captured) == 2
    first, second = captured
    assert first[1:] == (None, False, request.conversation_turn_id)
    assert second[1] is not None and second[2:] == (True, request.conversation_turn_id)
    assert second[1].selected_skill_ids == (() if choice else ("workspace:method-001",))
    assert second[1].binding.run_id == ("foreground-run" if existing_main else request.task_id)
    assert first[0].run_id == second[0].run_id == request.task_id
    assert_retained_provider_surface(fixture, empty=bool(choice))
    assert "capability_presentation" not in str(asdict(request))
    assert "capability_presentation" not in str(asdict(result))
    result = run_slice(fixture, replace(request, conversation_turn_id="background-turn-2"))
    assert result.runtime_status == "ok" and len(calls) == 2
    assert captured[2][1:] == (None, False, "background-turn-2")
    assert captured[2][0].task_id == request.task_id


def test_background_runtime_binding_preserves_original_host_rejection(capability_host):  # noqa: F811
    fixture = capability_host
    thread = fixture.agent.conversation_store.threads.get_or_create({"channel": "chat", "channel_conversation_id": "reject"})
    request = BackgroundRunRequest(thread_id=thread.thread_id, task_id="rejected-task", conversation_turn_id="rejected-turn")
    bindings = []

    class RejectBinding:
        def bind_runtime_authority(self, binding):
            bindings.append(binding)
            return False

    params = _run_params(thread.thread_id, request=request, agent=fixture.agent, thread=thread)
    params.conversation_task_binding_callback = RejectBinding()
    with pytest.raises(RuntimeError, match="执行身份无法可靠保存"):
        execution._run_background_model_attempt(fixture.agent, "核对来源事实", params)
    assert len(bindings) == 1 and bindings[0]["task_id"] == request.task_id
    assert fixture.backend.model_prompts == []
    row = fixture.agent.subagents.runtime_db.main_agent_run_for_task(request.task_id)
    assert row["status"] == "failed"


@pytest.mark.parametrize("mode", ["failed_decision", "cleared_selection"])
def test_background_compact_reuses_evaluated_selection_without_another_decision(capability_host, monkeypatch, mode):  # noqa: F811
    from agent_py_agent.agent.agent_core.compact_request_recovery import PreparedCompactRecovery

    fixture = capability_host
    thread = fixture.agent.conversation_store.threads.get_or_create({"channel": "chat", "channel_conversation_id": mode})
    append_prior_turn(fixture.agent, thread.thread_id)
    request = BackgroundRunRequest(thread_id=thread.thread_id, task_id="fallback-task", conversation_turn_id="fallback-turn")
    calls = provider(monkeypatch, fail=RuntimeError("fake unavailable") if mode == "failed_decision" else None)
    captured = capture_host_runs(monkeypatch, fixture.agent)
    original = PreparedCompactRecovery.select
    selected = []

    def select(self, agent, params, prompt):
        recovery_decision = self.force and not self.consumed
        if recovery_decision:
            selected.append(params)
        if recovery_decision and mode == "cleared_selection":
            assert captured[-1][1] is not None
            patch(fixture.agent, {"points.skill_tool.mode": "off"})
        try:
            return original(self, agent, params, prompt)
        finally:
            if recovery_decision and mode == "cleared_selection":
                patch(fixture.agent, {"points.skill_tool.mode": "apply"})

    monkeypatch.setattr(PreparedCompactRecovery, "select", select)
    result = run_slice(fixture, request)
    assert result.runtime_status == "ok" and len(calls) == 1 and len(captured) == 2
    assert len(selected) == 1
    assert captured[1][2:] == (True, request.conversation_turn_id)
    assert (captured[1][1] is None) == (mode == "failed_decision")
    if mode == "cleared_selection":
        assert "method-059" not in fixture.backend.model_prompts[1]
        assert fixture.backend.model_prompts[1].startswith("# System")
    else:
        assert "method-059" in fixture.backend.model_prompts[1]
    assert fixture.first.calls == fixture.second.calls == 0


@pytest.mark.parametrize("previous", [None, object(), lambda _link: True, lambda _link: False])
def test_background_binding_keeps_original_task_confirmation_and_closes(previous):
    from agent_py_agent.agent.conversation.task_promotion import (
        _publish_current_request_task_binding,
    )

    original = _publish_current_request_task_binding(SimpleNamespace(conversation_task_binding_callback=previous), "link")
    control = execution._BackgroundRunControl("request", previous)
    wrapped = SimpleNamespace(conversation_task_binding_callback=control)
    assert _publish_current_request_task_binding(wrapped, "link") is original
    control.finish()
    assert not _publish_current_request_task_binding(wrapped, "link")


def test_background_closed_binding_does_not_forward_late_publication():
    published = []
    control = execution._BackgroundRunControl("request", SimpleNamespace(bind_runtime_authority=published.append))
    control.finish()
    with pytest.raises(InterruptedError):
        control.bind_runtime_authority({"invocation_run_id": "request"})
    assert published == []
