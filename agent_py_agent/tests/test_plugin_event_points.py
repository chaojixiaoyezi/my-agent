import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.plugin_events.points import EventPointContext, emit_event, project_event
from agent_py_agent.agent.plugin_events.protocol import EventEnvelope, build_event_payload

FIELDS = {
    "prompt_submitted": {"request_id", "chars", "has_attachments"},
    "turn_started": {"request_id", "model_name"},
    "turn_ended": {"request_id", "status", "duration_ms", "tool_calls", "error_code"},
    "tool_call_started": {"call_id", "tool", "effect", "args_hash"},
    "tool_call_finished": {"call_id", "tool", "ok", "error_code", "failure_stage", "duration_ms", "handler_executed"},
    "command_executed": {"command", "operation_id", "state"},
}
PUBLIC = {"event_id", "type", "seq", "occurred_at", "dropped_before", "channel", "thread_ref",
          "channel_conversation_ref", "actor", "facts"}
SOURCE = {
    "request_id": "request-1", "prompt": "PRIVATE-PROMPT-MARKER", "has_attachments": True,
    "model_name": "test-model", "status": "done", "duration_ms": 12, "tool_calls": 2, "error_code": "",
    "call_id": "call-1", "tool": "read_file", "effect": "read_only", "args_hash": "sha256:123",
    "ok": True, "failure_stage": "", "handler_executed": True,
    "command": "/model", "operation_id": "op-1", "state": "ok",
    "arguments": {"token": "ARGUMENT-MARKER", "__run_scope": "INTERNAL-MARKER"},
    "output": "OUTPUT-MARKER", "path": "/PRIVATE-PATH-MARKER",
}


def context(publish=lambda event: None, enabled=True, actor="main"):
    return EventPointContext(SimpleNamespace(plugin_events_enabled=enabled), publish, "tui", "private-thread", actor)


@pytest.mark.parametrize("event_type", FIELDS)
def test_exact_fields_and_default_no_content(event_type):
    fact = project_event(context(), event_type, SOURCE)
    assert fact is not None
    payload = build_event_payload(EventEnvelope(fact, "ev-1", 1, 0, 1.0))
    assert set(payload) == PUBLIC
    assert set(payload["facts"]) == FIELDS[event_type]
    encoded = json.dumps(payload)
    for marker in ("PRIVATE", "ARGUMENT-MARKER", "INTERNAL-MARKER", "OUTPUT-MARKER", "private-thread"):
        assert marker not in encoded
    assert len(fact.thread_ref) == 64


def test_prompt_redacted_then_capped_and_only_authorized_envelope_has_content():
    source = SOURCE | {"prompt": "token=synthetic-credential-mark " + "字" * 5000}
    fact = project_event(context(), "prompt_submitted", source)
    assert fact is not None
    assert len(fact.content) == 4000
    assert "synthetic-credential-mark" not in fact.content
    assert "<redacted>" in fact.content
    assert fact.facts["chars"] == len(source["prompt"])
    denied = build_event_payload(EventEnvelope(fact, "ev", 1, 0, 1))
    approved = build_event_payload(EventEnvelope(fact, "ev", 1, 0, 1, include_content=True))
    assert "content" not in denied
    assert approved["content"] == fact.content


@pytest.mark.parametrize("actor", ["main", "subagent", "decision"])
def test_actor_only_host_context(actor):
    fact = project_event(context(actor=actor), "tool_call_started", SOURCE | {"actor": "spoofed"})
    assert fact is not None and fact.actor == actor


def test_disabled_never_projects_or_publishes(monkeypatch):
    from agent_py_agent.agent.plugin_events import points
    calls = []
    def forbidden(*args):
        calls.append(args)
        raise AssertionError("关闭时不能读取安装表或发布")
    monkeypatch.setattr(points, "project_event", forbidden)
    emit_event(context(forbidden, enabled=False), "prompt_submitted", SOURCE)
    assert calls == []


@pytest.mark.parametrize("error", [RuntimeError, ValueError, AssertionError])
def test_publication_failure_does_not_escape(error):
    calls = []
    def failing(event):
        calls.append(event)
        raise error("synthetic failure")
    emit_event(context(failing), "turn_started", SOURCE)
    assert len(calls) == 1


def test_non_prompt_never_retains_content():
    for kind in set(FIELDS) - {"prompt_submitted"}:
        fact = project_event(context(), kind, SOURCE | {"content": "PRIVATE-MARKER"})
        assert fact is not None and fact.content == ""
    assert project_event(replace(context(), actor="invalid"), "turn_started", SOURCE).actor == "main"


@pytest.mark.parametrize("actor", ["main", "subagent", "decision"])
def test_executor_events_surround_real_handler(tmp_path, actor):
    from agent_py_agent.agent.tooling.executor import ToolExecutor, ToolExecutorRequest
    from agent_py_agent.agent.tooling.models import BaseTool, ToolHandlerOutcome
    from agent_py_agent.tests._tool_runtime_harness import (
        canonical_test_call,
        make_test_model_spec,
        make_test_runtime_policy,
        runtime_snapshot_for_tools,
    )
    observed = []
    class Tool(BaseTool):
        model_spec = make_test_model_spec("event_probe", input_schema={"type": "object", "additionalProperties": True})
        runtime_policy = make_test_runtime_policy()
        def execute(self, params):
            assert [item.type for item in observed] == ["tool_call_started"]
            return ToolHandlerOutcome("event_probe", True, "OUTPUT-MARKER")
    snapshot = runtime_snapshot_for_tools({"event_probe": Tool()})
    call = canonical_test_call(snapshot, "event_probe", SOURCE["arguments"])
    request = ToolExecutorRequest(call, snapshot, tmp_path, operation_store_required=False,
                                  event_context=context(observed.append, actor=actor))
    result = ToolExecutor().execute(request).result
    assert result.ok
    assert [item.type for item in observed] == ["tool_call_started", "tool_call_finished"]
    assert {item.actor for item in observed} == {actor}
    assert observed[0].facts["args_hash"] == call.args_hash
    assert observed[1].facts["handler_executed"] is True
    assert set(observed[0].facts) == FIELDS["tool_call_started"]
    assert set(observed[1].facts) == FIELDS["tool_call_finished"]
    assert "MARKER" not in json.dumps([item.facts for item in observed])


@pytest.mark.parametrize("effect", ["read_only", "dangerous"])
def test_executor_denied_or_asking_has_no_events(tmp_path, effect):
    from agent_py_agent.agent.tooling.executor import ToolExecutor, ToolExecutorRequest
    from agent_py_agent.agent.tooling.models import BaseTool, ToolHandlerOutcome
    from agent_py_agent.tests._tool_runtime_harness import (
        canonical_test_call,
        make_test_model_spec,
        make_test_runtime_policy,
        runtime_snapshot_for_tools,
    )
    observed = []
    class Tool(BaseTool):
        model_spec = make_test_model_spec("event_denied")
        runtime_policy = make_test_runtime_policy(effect)
        def execute(self, params):
            raise AssertionError("必须在 handler 前拒绝")
    snapshot = runtime_snapshot_for_tools({"event_denied": Tool()})
    call = canonical_test_call(snapshot, "event_denied", {} if effect == "dangerous" else {"extra": True})
    request = ToolExecutorRequest(call, snapshot, tmp_path, operation_store_required=False,
                                  event_context=context(observed.append))
    result = ToolExecutor().execute(request).result
    assert not result.ok and not result.handler_executed
    assert observed == []


def test_observation_inputs_exclude_arguments_before_projection(monkeypatch):
    from agent_py_agent.agent.tooling import event_observation
    from agent_py_agent.agent.tooling.runtime_contracts import ToolCall, ToolResult
    from agent_py_agent.tests._tool_runtime_harness import make_test_model_spec

    inputs = []
    monkeypatch.setattr(event_observation, 'emit_event', lambda _context, kind, source: inputs.append((kind, dict(source))))
    call = ToolCall(call_id='probe', tool_name='read_file', arguments=SOURCE['arguments'],
                    source_protocol='native', schema_hash=make_test_model_spec('read_file').schema_hash,
                    run_id='run', turn_id='turn', attempt_id='attempt')
    observation = event_observation.ToolEventObservation(context(), call, 'read_only')
    observation.start()
    observation.finish(ToolResult.failed(call, 'OUTPUT-MARKER', error_code='TOOL_ERROR', failure_stage='execution'))
    assert [kind for kind, _source in inputs] == ['tool_call_started', 'tool_call_finished']
    assert all(set(source) == FIELDS[kind] for kind, source in inputs)
    assert 'MARKER' not in json.dumps(inputs)


@pytest.mark.parametrize("enabled", [False, True])
def test_real_executor_survives_publisher_exception_and_disabled_point(tmp_path, enabled):
    from agent_py_agent.agent.tooling.executor import ToolExecutor, ToolExecutorRequest
    from agent_py_agent.agent.tooling.models import BaseTool, ToolHandlerOutcome
    from agent_py_agent.tests._tool_runtime_harness import (
        canonical_test_call,
        make_test_model_spec,
        make_test_runtime_policy,
        runtime_snapshot_for_tools,
    )
    calls = []
    def failing(event):
        calls.append(event.type)
        raise RuntimeError("观察发布故障")
    class Tool(BaseTool):
        model_spec = make_test_model_spec("event_fault")
        runtime_policy = make_test_runtime_policy()
        def execute(self, params):
            return ToolHandlerOutcome("event_fault", True, "正常业务结果")
    snapshot = runtime_snapshot_for_tools({"event_fault": Tool()})
    call = canonical_test_call(snapshot, "event_fault", {})
    request = ToolExecutorRequest(call, snapshot, tmp_path, operation_store_required=False,
                                  event_context=context(failing, enabled=enabled))
    result = ToolExecutor().execute(request).result
    assert result.ok and result.handler_executed
    assert calls == (["tool_call_started", "tool_call_finished"] if enabled else [])


def test_projection_does_not_copy_nested_values_or_command_parameters():
    fact = project_event(context(), "command_executed", SOURCE | {
        "command": "/model private-path token=MARKER", "operation_id": {"arguments": "MARKER"},
    })
    assert fact.facts["command"] == "" and fact.facts["operation_id"] == ""
    assert "MARKER" not in json.dumps(fact.facts)


@pytest.mark.parametrize("channel", ["tui", "feishu"])
def test_v2_turn_and_tool_events_share_thread_ref_across_channels(channel):
    # v2（tref2）：同一会话里 turn 类与 tool 类事件的 thread_ref 同源（本系统会话线程）；
    # 渠道会话哈希只在 Gateway 侧事件上有值（工具侧的宿主上下文不带渠道会话号），
    # 且与线程哈希不是同一个值。
    gateway_point = EventPointContext(SimpleNamespace(plugin_events_enabled=True), lambda event: None,
                                      channel, "thread-abc", "main", "conv-1")
    tool_point = EventPointContext(SimpleNamespace(plugin_events_enabled=True), lambda event: None,
                                   channel, "thread-abc", "main")
    turn = project_event(gateway_point, "turn_started", SOURCE)
    started = project_event(tool_point, "tool_call_started", SOURCE)
    finished = project_event(tool_point, "tool_call_finished", SOURCE)
    assert turn.thread_ref == started.thread_ref == finished.thread_ref
    assert len(turn.thread_ref) == 64
    assert turn.channel_conversation_ref and len(turn.channel_conversation_ref) == 64
    assert turn.channel_conversation_ref != turn.thread_ref
    assert started.channel_conversation_ref == "" and finished.channel_conversation_ref == ""
    payload = build_event_payload(EventEnvelope(turn, "ev", 1, 0, 1.0))
    assert payload["channel_conversation_ref"] == turn.channel_conversation_ref
    assert "conv-1" not in json.dumps(payload) and "thread-abc" not in json.dumps(payload)


def test_v2_prompt_submitted_before_thread_resolution_keeps_empty_thread_ref():
    # v2（tref2）A2：入队时点线程可能还没解析，thread_ref 留空；渠道会话哈希此时已有值。
    point = EventPointContext(SimpleNamespace(plugin_events_enabled=True), lambda event: None,
                              "tui", "", "main", "conv-1")
    fact = project_event(point, "prompt_submitted", SOURCE)
    assert fact is not None and fact.thread_ref == ""
    assert len(fact.channel_conversation_ref) == 64
    command = project_event(EventPointContext(SimpleNamespace(plugin_events_enabled=True), lambda event: None,
                                              "tui", "thread-abc", "main", "conv-1"), "command_executed", SOURCE)
    assert len(command.thread_ref) == 64 and len(command.channel_conversation_ref) == 64


def test_handler_parameter_preparation_failure_is_not_observed(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling import registry_invoke
    from agent_py_agent.agent.tooling.executor import ToolExecutor, ToolExecutorRequest
    from agent_py_agent.agent.tooling.models import BaseTool
    from agent_py_agent.tests._tool_runtime_harness import (
        canonical_test_call,
        make_test_model_spec,
        make_test_runtime_policy,
        runtime_snapshot_for_tools,
    )
    observed = []
    class Tool(BaseTool):
        model_spec = make_test_model_spec("event_preparation")
        runtime_policy = make_test_runtime_policy()
        def execute(self, params):
            raise AssertionError("参数准备失败时不应进入 handler")
    def failing(request):
        raise ValueError("宿主参数准备故障")
    monkeypatch.setattr(registry_invoke, "_tool_params_with_runtime_boundary", failing)
    snapshot = runtime_snapshot_for_tools({"event_preparation": Tool()})
    call = canonical_test_call(snapshot, "event_preparation", {})
    request = ToolExecutorRequest(call, snapshot, tmp_path, operation_store_required=False,
                                  event_context=context(observed.append))
    result = ToolExecutor().execute(request).result
    assert not result.ok and not result.handler_executed
    assert observed == []
