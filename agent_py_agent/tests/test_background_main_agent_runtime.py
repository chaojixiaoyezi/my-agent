from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.backends.errors import ProviderUsageLimitError
from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
    ConversationStore,
    FakeDeliveryService,
)
from agent_py_agent.agent.conversation.authority import (
    CONVERSATION_REQUEST_ID_ATTR,
    CONVERSATION_TASK_TURN_ACTIVE_ATTR,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.agent.settings import AgentConfig


def test_background_run_params_carry_structured_conversation_task_identity() -> None:
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest, _run_params

    request = BackgroundRunRequest(
        thread_id="thread-1",
        task_id="task-1",
        reason="scheduled_progress_report",
    )

    params = _run_params(request.thread_id, request)

    assert params.source == "background_main_agent"
    assert params.task_attributes == {
        "conversation_thread_id": "thread-1",
        "conversation_task_id": "task-1",
        CONVERSATION_REQUEST_ID_ATTR: "task-1",
        CONVERSATION_TASK_TURN_ACTIVE_ATTR: True,
    }


def test_claimed_background_turn_can_use_its_own_task_workspace(tmp_path) -> None:
    from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
    from agent_py_agent.agent.agent_core.tool_call_runtime import (
        ToolCallRuntimeRequest,
        _promote_conversation_task_for_work_tool,
    )
    from agent_py_agent.agent.agent_core.tool_loop.round_execution import (
        ToolCallExecuteParams,
    )
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest, _run_params
    from agent_py_agent.tests._tool_runtime_harness import canonical_test_call

    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")),
        tmp_path,
    )
    thread = agent.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "title": "后台续跑",
        }
    )
    task_root = tmp_path / "home" / "tasks" / "task-1"
    (task_root / "output").mkdir(parents=True)
    (task_root / "work").mkdir()
    agent.conversation_store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "goal": "继续既有任务",
            "status": "active",
            "task_path": str(task_root),
        }
    )
    claim = agent.conversation_store.claim_background_run(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "reason": "subagent_runner_finished",
            "lease_seconds": 90,
        }
    )
    assert claim is not None
    params = _run_params(
        thread.thread_id,
        BackgroundRunRequest(
            thread_id=thread.thread_id,
            task_id="task-1",
            reason="subagent_runner_finished",
        ),
        agent,
    )
    snapshot = agent.tools.runtime_snapshot(run_id=params.run_id)
    loop_params = ToolLoopExecuteParams(
        user_prompt="继续既有任务",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=params.allowed_tools,
        write_boundary=params.write_boundary,
        task_attributes=params.task_attributes,
        request_id=params.request_id,
        run_id=params.run_id,
        task_id=params.task_id,
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
        tool_runtime_snapshot=snapshot,
    )
    agent._current_run_params = params
    call = canonical_test_call(snapshot, "write_file", {})
    execute_request = ToolCallExecuteParams(loop_params, 1, 1, call)
    try:
        result = _promote_conversation_task_for_work_tool(
            ToolCallRuntimeRequest(agent, execute_request, call)
        )
    finally:
        delattr(agent, "_current_run_params")
        agent.conversation_store.finish_background_run(
            {
                "thread_id": thread.thread_id,
                "claim_id": claim["claim_id"],
                "task_id": "task-1",
                "status": "finished",
            }
        )

    assert result is None
    assert params.task_attributes[CONVERSATION_TASK_TURN_ACTIVE_ATTR] is True


def test_background_run_without_task_does_not_invent_conversation_task_identity() -> None:
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest, _run_params

    request = BackgroundRunRequest(thread_id="thread-chat", task_id="", reason="observation_batch")

    params = _run_params(request.thread_id, request)

    assert params.run_id == "bg-main-thread-chat"
    assert params.task_id == ""
    assert params.task_attributes is None


def test_internal_background_run_uses_bounded_existing_tool_loop_controls() -> None:
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest, _run_params
    from agent_py_agent.agent.settings.runtime_guard_config import RuntimeGuardPolicy

    agent = SimpleNamespace(
        config=SimpleNamespace(background_main_agent_allowed_tools=[]),
        runtime_guard_policy=RuntimeGuardPolicy(
            values={
                "background_max_tool_rounds": 7,
                "background_max_tool_calls_per_round": 3,
            }
        ),
        owner_policy=None,
        home_paths=None,
        conversation_store=None,
    )
    internal = _run_params(
        "thread-1",
        BackgroundRunRequest(
            thread_id="thread-1",
            task_id="task-1",
            reason="scheduled_progress_report",
        ),
        agent,
    )
    assert internal.task_attributes["max_tool_rounds"] == 7
    assert internal.task_attributes["max_tool_calls_per_round"] == 3

    incoming = _run_params(
        "thread-1",
        BackgroundRunRequest(
            thread_id="thread-1",
            task_id="task-1",
            reason="incoming_channel_message",
        ),
        agent,
    )
    assert "max_tool_rounds" not in incoming.task_attributes
    assert "max_tool_calls_per_round" not in incoming.task_attributes


def test_internal_background_run_fallback_allows_a_complete_work_slice() -> None:
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest, _run_params
    from agent_py_agent.agent.settings.runtime_guard_config import RuntimeGuardPolicy

    agent = SimpleNamespace(
        config=SimpleNamespace(background_main_agent_allowed_tools=[]),
        runtime_guard_policy=RuntimeGuardPolicy(values={}),
        owner_policy=None,
        home_paths=None,
        conversation_store=None,
    )

    params = _run_params(
        "thread-1",
        BackgroundRunRequest(
            thread_id="thread-1",
            task_id="task-1",
            reason="scheduled_progress_report",
        ),
        agent,
    )

    assert params.task_attributes["max_tool_rounds"] == 32
    assert params.task_attributes["max_tool_calls_per_round"] == 4


def test_background_material_progress_uses_structured_tool_effect_variants() -> None:
    from agent_py_agent.agent.conversation.runtime import _material_tool_success_count
    from agent_py_agent.agent.tooling import BaseTool, ToolHandlerOutcome
    from agent_py_agent.tests._tool_runtime_harness import (
        make_test_model_spec,
        make_test_runtime_policy,
        runtime_snapshot_for_tools,
    )

    class _Tool(BaseTool):
        def __init__(self, name, effect, effect_by_parameter=()):
            self.model_spec = make_test_model_spec(
                name,
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {"type": "string"},
                        "path": {"type": "string"},
                        "summary": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            )
            self.runtime_policy = make_test_runtime_policy(
                effect,
                effect_by_parameter=effect_by_parameter,
            )

        def execute(self, params):
            return ToolHandlerOutcome(self.model_spec.name, True, str(params))

    tools = {
        "task_progress": _Tool(
            "task_progress",
            "mutating",
            (
                (
                    "action",
                    (("", "read_only"), ("read", "read_only"), ("update", "mutating")),
                ),
            ),
        ),
        "read_file": _Tool("read_file", "read_only"),
    }
    snapshot = runtime_snapshot_for_tools(tools)
    agent = SimpleNamespace(tools=SimpleNamespace(runtime_snapshot=lambda: snapshot))
    readonly_calls = [
        {
            "tool": "task_progress",
            "ok": True,
            "parameters": {"tool": "task_progress", "action": "read"},
        },
        {
            "tool": "read_file",
            "ok": True,
            "parameters": {"tool": "read_file", "path": "README.md"},
        },
    ]
    assert _material_tool_success_count(agent, readonly_calls) == 0

    update = {
        "tool": "task_progress",
        "ok": True,
        "parameters": {
            "tool": "task_progress",
            "action": "update",
            "summary": "checkpoint",
        },
    }
    assert _material_tool_success_count(agent, [*readonly_calls, update]) == 1


def test_background_response_persists_public_operation_verification(tmp_path) -> None:
    from agent_py_agent.agent.conversation.channels import DeliveryContext
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundRunRequest,
        _public_result_operation_verification,
    )

    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-operation",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=store,
        channels=FakeDeliveryService(),
    )
    internal = {
        "schema": "operation_verification.v1",
        "status": "succeeded",
        "operation_count": 1,
        "counts": {"succeeded": 1},
        "operations": [
            {
                "tool": "remember",
                "action": "add",
                "verification_status": "succeeded",
                "call_id": "private-call",
                "operation_id": "private-operation",
                "attempt_count": 1,
                "replayed": False,
            }
        ],
    }
    public = _public_result_operation_verification(
        type("Result", (), {"operation_verification": internal})()
    )

    runtime._record_response(
        BackgroundRunRequest(
            thread_id=thread.thread_id,
            reason="scheduled_progress_report",
            now=11.0,
        ),
        DeliveryContext(
            channel="internal",
            target="thread-operation",
            thread_id=thread.thread_id,
        ),
        "我已经保存。",
        operation_verification=public,
        deliver=True,
        delivery_reason="scheduled_progress_report",
    )

    row = store.recent_messages(thread.thread_id, limit=1)[0]
    serialized = json.dumps(row.metadata["operation_verification"], ensure_ascii=False)
    assert row.metadata["operation_verification"]["groups"][0]["label"] == "remember/add"
    assert "private-call" not in serialized
    assert "private-operation" not in serialized


def test_internal_audit_report_commits_source_refs_after_transcript_append(
    tmp_path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.conversation.channels import (
        DeliveryContext,
        DeliveryReceipt,
    )
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest
    from agent_py_agent.agent.ingestion import harvester

    class _InternalDelivery:
        def supports_proactive(self, _channel: str) -> bool:
            return False

        def deliver(self, context, envelope):
            return DeliveryReceipt(
                channel=context.channel,
                target=context.target,
                content=envelope.content,
                thread_id=context.thread_id,
                task_id=context.task_id,
                delivery_status="not_applicable",
                evidence_refs=envelope.evidence_refs,
            )

    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "owner-a",
            "channel": "internal",
            "channel_conversation_id": "audit-local-delivery",
            "channel_user_id": "owner-a",
        }
    )
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=store,
        channels=_InternalDelivery(),
    )
    source_ref = "audit://watch-a/candidate/7:0"
    request = BackgroundRunRequest(
        thread_id=thread.thread_id,
        task_id="audit-a",
        reason="audit_finding",
        wake_signal={
            "wake_signal_id": "wake-a",
            "root_task_id": "audit-a",
            "evidence_refs": [source_ref],
            "metadata": {
                "schema_version": "audit-finding-event.v1",
                "audit_id": "audit-a",
                "watch_id": "watch-a",
                "finding_id": "finding-a",
                "requires_llm_report": True,
                "delivery_evidence_refs": [source_ref],
            },
        },
    )
    recorded: list[tuple[tuple[str, ...], str, str]] = []

    def record(_owner_home, refs, *, receipt_id, channel, delivered_at=None):
        del delivered_at
        recorded.append((tuple(refs), receipt_id, channel))
        return list(refs)

    monkeypatch.setattr(harvester, "record_audit_delivery_refs", record)
    context = DeliveryContext(
        channel="internal",
        target=thread.thread_id,
        thread_id=thread.thread_id,
        task_id="audit-a",
    )

    first = runtime._record_response(
        request,
        context,
        "发现一项高风险事件。",
        deliver=True,
        delivery_reason="audit_finding_report",
    )
    second = runtime._record_response(
        request,
        context,
        "发现一项高风险事件。",
        deliver=True,
        delivery_reason="audit_finding_report",
    )

    assert first == ("发现一项高风险事件。", "not_applicable")
    assert second == first
    messages = store.recent_messages(thread.thread_id, limit=0)
    assert len(messages) == 1
    assert recorded == [
        ((source_ref,), messages[0].message_id, "internal"),
        ((source_ref,), messages[0].message_id, "internal"),
    ]


def test_internal_audit_finding_run_uses_transcript_fallback_and_handles_wake(
    tmp_path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.conversation.channels import DeliveryReceipt
    from agent_py_agent.agent.ingestion import harvester

    class _InternalDelivery:
        def supports_proactive(self, _channel: str) -> bool:
            return False

        def deliver(self, context, envelope):
            return DeliveryReceipt(
                channel=context.channel,
                target=context.target,
                content=envelope.content,
                thread_id=context.thread_id,
                task_id=context.task_id,
                delivery_status="not_applicable",
                evidence_refs=envelope.evidence_refs,
            )

    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "owner-a",
            "channel": "internal",
            "channel_conversation_id": "audit-local-delivery",
            "channel_user_id": "owner-a",
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-a",
            "goal": "持续研判",
            "work_kind": "audit",
            "work_name": "本地审计",
        }
    )
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=store,
        channels=_InternalDelivery(),
    )
    source_ref = "audit://watch-a/candidate/7:0"
    monkeypatch.setattr(
        runtime,
        "_run_agent",
        lambda *_args, **_kwargs: (
            "发现一项高风险事件。",
            0,
            0,
            0,
            (),
            (),
            {},
        ),
    )
    recorded: list[tuple[tuple[str, ...], str, str]] = []

    def record(_owner_home, refs, *, receipt_id, channel, delivered_at=None):
        del delivered_at
        recorded.append((tuple(refs), receipt_id, channel))
        return list(refs)

    monkeypatch.setattr(harvester, "record_audit_delivery_refs", record)
    report = runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-a",
            "reason": "audit_finding",
            "route_channel": "internal",
            "wake_signal": {
                "wake_signal_id": "wake-a",
                "root_task_id": "audit-a",
                "evidence_refs": [source_ref],
                "metadata": {
                    "schema_version": "audit-finding-event.v1",
                    "audit_id": "audit-a",
                    "watch_id": "watch-a",
                    "finding_id": "finding-a",
                    "requires_llm_report": True,
                    "delivery_evidence_refs": [source_ref],
                },
            },
        }
    )

    assert report.delivery_reason == "audit_finding_transcript"
    assert report.delivery_status == "not_applicable"
    assert report.wake_handled is True
    messages = store.recent_messages(thread.thread_id, limit=0)
    assert [row.content for row in messages] == ["发现一项高风险事件。"]
    assert recorded == [((source_ref,), messages[0].message_id, "internal")]


def test_chat_audit_finding_commits_to_transcript_and_handles_wake(
    tmp_path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.conversation.channels import DeliveryReceipt
    from agent_py_agent.agent.ingestion import harvester

    class _ChatTranscriptDelivery:
        def supports_proactive(self, _channel: str) -> bool:
            return False

        def supports_transcript(self, channel: str) -> bool:
            return channel == "chat"

        def deliver(self, context, envelope):
            return DeliveryReceipt(
                channel=context.channel,
                target=context.target,
                content=envelope.content,
                thread_id=context.thread_id,
                task_id=context.task_id,
                delivery_status="not_applicable",
                evidence_refs=envelope.evidence_refs,
            )

    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "local-agent",
            "channel": "chat",
            "channel_conversation_id": "cli-session-a",
            "channel_user_id": "local-agent",
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-chat",
            "goal": "持续审计",
            "work_kind": "audit",
            "work_name": "CLI 审计",
        }
    )
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=store,
        channels=_ChatTranscriptDelivery(),
    )
    source_ref = "audit://watch-chat/candidate/9:0"
    monkeypatch.setattr(
        runtime,
        "_run_agent",
        lambda *_args, **_kwargs: (
            "发现一项高风险事件。",
            0,
            0,
            0,
            (),
            (),
            {},
        ),
    )
    recorded: list[tuple[tuple[str, ...], str, str]] = []

    def record(_owner_home, refs, *, receipt_id, channel, delivered_at=None):
        del delivered_at
        recorded.append((tuple(refs), receipt_id, channel))
        return list(refs)

    monkeypatch.setattr(harvester, "record_audit_delivery_refs", record)
    report = runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-chat",
            "reason": "audit_finding",
            "route_channel": "chat",
            "route_target": "cli-session-a",
            "wake_signal": {
                "wake_signal_id": "wake-chat",
                "root_task_id": "audit-chat",
                "evidence_refs": [source_ref],
                "metadata": {
                    "schema_version": "audit-finding-event.v1",
                    "audit_id": "audit-chat",
                    "watch_id": "watch-chat",
                    "finding_id": "finding-chat",
                    "requires_llm_report": True,
                    "delivery_evidence_refs": [source_ref],
                },
            },
        }
    )

    assert report.delivery_reason == "audit_finding_transcript"
    assert report.delivery_status == "not_applicable"
    assert report.wake_handled is True
    messages = store.recent_messages(thread.thread_id, limit=0)
    assert [row.content for row in messages] == ["发现一项高风险事件。"]
    assert recorded == [((source_ref,), messages[0].message_id, "chat")]


def test_chat_transcript_persists_delivery_service_redaction(
    tmp_path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.delivery import DeliveryService, build_default_channel_registry
    from agent_py_agent.agent.ingestion import harvester

    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "local-agent",
            "channel": "chat",
            "channel_conversation_id": "cli-session-redaction",
            "channel_user_id": "local-agent",
        }
    )
    task_id = "audit-chat-private-123456"
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": task_id,
            "goal": "持续审计",
            "work_kind": "audit",
            "work_name": "CLI 审计",
        }
    )
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=store,
        channels=DeliveryService(build_default_channel_registry(agent.config)),
    )
    source_ref = "audit://watch-chat/candidate/10:0"
    monkeypatch.setattr(
        runtime,
        "_run_agent",
        lambda *_args, **_kwargs: (
            f"当前任务 {task_id}，会话 {thread.thread_id} 已发现高风险事件。",
            0,
            0,
            0,
            (),
            (),
            {},
        ),
    )
    monkeypatch.setattr(
        harvester,
        "record_audit_delivery_refs",
        lambda *_args, **_kwargs: [source_ref],
    )

    report = runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": task_id,
            "reason": "audit_finding",
            "route_channel": "chat",
            "route_target": "cli-session-redaction",
            "wake_signal": {
                "wake_signal_id": "wake-chat-redaction",
                "root_task_id": task_id,
                "evidence_refs": [source_ref],
                "metadata": {
                    "schema_version": "audit-finding-event.v1",
                    "finding_id": "finding-chat-redaction",
                    "requires_llm_report": True,
                    "delivery_evidence_refs": [source_ref],
                },
            },
        }
    )

    assert report.wake_handled is True
    assert task_id not in report.response
    assert thread.thread_id not in report.response
    assert "当前任务" in report.response
    assert "当前会话" in report.response
    assert store.recent_messages(thread.thread_id, limit=1)[0].content == report.response


def test_unknown_external_audit_route_remains_retryable() -> None:
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundRunRequest,
        _background_delivery_decision,
    )

    request = BackgroundRunRequest(
        thread_id="thread-a",
        task_id="audit-a",
        reason="audit_finding",
        wake_signal={
            "root_task_id": "audit-a",
            "evidence_refs": ["audit://watch-a/candidate/1:0"],
            "metadata": {
                "schema_version": "audit-finding-event.v1",
                "finding_id": "finding-a",
                "requires_llm_report": True,
                "delivery_evidence_refs": ["audit://watch-a/candidate/1:0"],
            },
        },
    )

    assert _background_delivery_decision(
        object(),
        request,
        resolved_channel="future-im",
        resolved_route_supports_proactive=False,
        resolved_route_supports_transcript=False,
    ) == (False, "audit_finding_delivery_unavailable")


def test_internal_audit_finding_empty_reply_remains_retryable(
    tmp_path,
    monkeypatch,
) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "owner-a",
            "channel": "internal",
            "channel_conversation_id": "audit-local-empty",
            "channel_user_id": "owner-a",
        }
    )
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store)
    monkeypatch.setattr(
        runtime,
        "_run_agent",
        lambda *_args, **_kwargs: ("", 0, 0, 0, (), (), {}),
    )

    report = runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-a",
            "reason": "audit_finding",
            "route_channel": "internal",
            "wake_signal": {
                "wake_signal_id": "wake-empty",
                "root_task_id": "audit-a",
                "evidence_refs": ["audit://watch-a/candidate/7:0"],
                "metadata": {
                    "schema_version": "audit-finding-event.v1",
                    "finding_id": "finding-a",
                    "requires_llm_report": True,
                    "delivery_evidence_refs": ["audit://watch-a/candidate/7:0"],
                },
            },
        }
    )

    assert report.delivery_status == "suppressed"
    assert report.wake_handled is False
    assert store.recent_messages(thread.thread_id, limit=0) == []


def test_background_run_restores_authoritative_task_workspace_and_title(tmp_path) -> None:
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest, _run_params

    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    workspace = tmp_path / "task-library"
    (workspace / "output").mkdir(parents=True)
    (workspace / "work").mkdir()
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-library",
            "goal": "图书馆运营方案",
            "task_path": str(workspace),
            "now": 11.0,
        }
    )

    params = _run_params(
        thread.thread_id,
        BackgroundRunRequest(thread_id=thread.thread_id, task_id="task-library"),
        agent,
    )

    assert params.task_attributes["task_title"] == "图书馆运营方案"
    assert params.task_attributes["run_workspace"] == {
        "task_root": str(workspace),
        "output_dir": str(workspace / "output"),
        "work_dir": str(workspace / "work"),
    }


def test_named_background_run_keeps_work_name_as_workspace_title(tmp_path) -> None:
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest, _run_params

    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    workspace = tmp_path / "audit-workspace"
    (workspace / "output").mkdir(parents=True)
    (workspace / "work").mkdir()
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-stable-title",
            "goal": "一段会随 prepare 更新且可能很长的 Audit 生效要求",
            "task_path": str(workspace),
            "work_kind": "audit",
            "work_name": "生产安全巡检",
            "now": 11.0,
        }
    )

    params = _run_params(
        thread.thread_id,
        BackgroundRunRequest(
            thread_id=thread.thread_id,
            task_id="audit-stable-title",
            reason="audit_finding",
        ),
        agent,
    )

    assert params.task_attributes["task_title"] == "生产安全巡检"
    assert params.task_attributes["conversation_work_name"] == "生产安全巡检"


def _native_probe(self):
    from agent_py_agent.agent.backends.base import ProviderToolCapability, _utc_now_iso

    return ProviderToolCapability(
        provider=str(self.name or "bg-test"), endpoint="local://bg-test",
        model="", stream=False, native_supported=True,
        evidence="test_backend_declares_native_tools",
        observed_at=_utc_now_iso(),
    )


class _CapturingBackend:
    name = "capturing"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def probe_tool_capability(self):
        return _native_probe(self)

    def generate(self, prompt: str, on_chunk=None, **kwargs) -> ModelResponse:
        self.prompts.append(prompt)
        return ModelResponse(text="后台主代理已检查任务树，并给出阶段汇报。", backend=self.name)


class _NaturalCompletionBackend:
    name = "natural-completion"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def probe_tool_capability(self):
        return _native_probe(self)

    def generate(self, prompt: str, on_chunk=None, **kwargs) -> ModelResponse:
        self.prompts.append(prompt)
        return ModelResponse(text="任务全部完成。", backend=self.name)


class _BlockedCollaborationBackend:
    name = "blocked-collaboration"

    def __init__(self, *, case_id: str):
        self.case_id = case_id
        self.prompts: list[str] = []
        self.calls = 0

    def probe_tool_capability(self):
        return _native_probe(self)

    def generate(self, prompt: str, on_chunk=None, **kwargs) -> ModelResponse:
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls == 1:
            assert "collaboration_case_closed" in prompt
            assert "不可达=1" in prompt
            return ModelResponse(
                text=f'[TOOL_CALL]\n{{"tool":"inspect_collaboration","case_id":"{self.case_id}"}}\n[/TOOL_CALL]',
                backend=self.name,
            )
        assert "collection_result" in prompt
        assert "ready_to_report" in prompt
        return ModelResponse(text="协作阻塞已确认：需要主代理调整策略。", backend=self.name)


class _PlainLanguageCollaborationBackend:
    name = "plain-language-collaboration"

    def __init__(self, *, case_id: str):
        self.case_id = case_id
        self.prompts: list[str] = []
        self.calls = 0

    def probe_tool_capability(self):
        return _native_probe(self)

    def generate(self, prompt: str, on_chunk=None, **kwargs) -> ModelResponse:
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls == 1:
            assert "帮我协调几个后台代理，有阻塞就继续安排或告诉我" in prompt
            return ModelResponse(
                text=f'[TOOL_CALL]\n{{"tool":"inspect_collaboration","case_id":"{self.case_id}"}}\n[/TOOL_CALL]',
                backend=self.name,
            )
        if self.calls == 2:
            assert "collection_result" in prompt
            assert "ready_to_report" in prompt
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    f'{{"tool":"update_collaboration","case_id":"{self.case_id}",'
                    '"status":"needs_replan","summary":"已看到阻塞请求，下一步需要换来源或补派代理。"}'
                    "\n[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        assert "needs_replan" in prompt
        return ModelResponse(
            text="我已经看到阻塞点，会换来源或补派代理继续推进。", backend=self.name
        )


class _SlowBackend:
    name = "slow"

    def __init__(self, *, sleep_seconds: float):
        self.sleep_seconds = sleep_seconds

    def probe_tool_capability(self):
        return _native_probe(self)

    def generate(self, prompt: str, on_chunk=None, **kwargs) -> ModelResponse:
        time.sleep(self.sleep_seconds)
        return ModelResponse(text="后台主代理慢速检查完成。", backend=self.name)


class _FailingBackend:
    name = "failing"

    def probe_tool_capability(self):
        return _native_probe(self)

    def generate(self, prompt: str, on_chunk=None, **kwargs) -> ModelResponse:
        raise RuntimeError("backend boom")


class _GoalToolProgressBackend:
    name = "goal-tool-progress"

    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    def probe_tool_capability(self):
        return _native_probe(self)

    def generate(self, prompt: str, on_chunk=None, **kwargs) -> ModelResponse:
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls == 1:
            return ModelResponse(
                text="",
                backend=self.name,
                tool_use_blocks=[{
                    "id": "call-goal-list-1",
                    "name": "list_files",
                    "input": {"path": "."},
                }],
            )
        return ModelResponse(text="本轮已经根据目录事实继续推进。", backend=self.name)


class _GoalCompletingBackend:
    name = "goal-completing"

    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    def probe_tool_capability(self):
        return _native_probe(self)

    def generate(self, prompt: str, on_chunk=None, **kwargs) -> ModelResponse:
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls == 1:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"update_goal","status":"complete"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        return ModelResponse(text="已经完成整合和验证。", backend=self.name)


class _MidTurnLifecycleBackend:
    name = "mid-turn-lifecycle"

    def __init__(self, *, store, thread_id: str, task_id: str, fail_after_injection: bool = False):
        self.store = store
        self.thread_id = thread_id
        self.task_id = task_id
        self.fail_after_injection = fail_after_injection
        self.calls = 0
        self.prompts: list[str] = []
        self.signal = None

    def probe_tool_capability(self):
        return _native_probe(self)

    def generate(self, prompt: str, on_chunk=None, **kwargs) -> ModelResponse:
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls == 1:
            self.signal = self.store.raise_wake_signal(
                {
                    "thread_id": self.thread_id,
                    "root_task_id": self.task_id,
                    "reason": "subagent_runner_finished",
                    "source_agent_id": "child-mid-turn",
                    "metadata": {"task_id": "child-mid-turn", "status": "DONE"},
                    "now": 20.5,
                }
            )
            return ModelResponse(text="这是子代理完成前生成的旧状态。", backend=self.name)
        assert "[RUNTIME_TASK_EVENTS]" in prompt
        assert "child-mid-turn" in prompt
        if self.fail_after_injection:
            raise RuntimeError("provider failed after runtime event injection")
        return ModelResponse(text="已接收子代理的新结果并继续整合。", backend=self.name)


class _InternalStatusBackend:
    name = "internal-status"

    def probe_tool_capability(self):
        return _native_probe(self)

    def generate(self, prompt: str, on_chunk=None, **kwargs) -> ModelResponse:
        return ModelResponse(
            text=(
                "[RUN_TOOL_EVIDENCE_BLOCKED]\n"
                '{"reason":"scheduled_progress_report","private":"must-not-enter-chat"}'
            ),
            backend=self.name,
        )


def test_background_runtime_reports_corrupt_thread_before_running_model(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store._thread_path(thread.thread_id).write_text("{bad-json", encoding="utf-8")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())

    try:
        runtime.run_once({"thread_id": thread.thread_id, "reason": "scheduled_progress_report"})
    except DataCorruptionError as exc:
        assert "conversation.thread.read" in str(exc)
        assert thread.thread_id in str(exc)
    else:
        raise AssertionError("corrupt thread should be reported as data corruption")


def test_due_progress_policy_wakes_background_main_agent_and_sends_message(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})

    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "title": "长期后台任务",
            "now": 10.0,
        }
    )
    store.append_message(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": "每小时帮我看一次进展，有问题就调度。",
            "channel": "internal",
            "metadata": {"gateway_request_id": "task-1"},
            "now": 11.0,
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "goal": "观察子代理任务树",
            "now": 12.0,
        }
    )
    store.set_progress_policy(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "interval_seconds": 60,
            "route_channel": "internal",
            "route_target": "thread-1",
            "now": 13.0,
        }
    )

    reports = scheduler.tick(now=73.0)

    assert len(reports) == 1
    assert len(backend.prompts) == 1
    assert "每小时帮我看一次进展" in backend.prompts[0]
    assert "inspect_agent_tree" in backend.prompts[0]
    assert "create_subagents" in backend.prompts[0]
    assert "dispatch_subagents" not in backend.prompts[0]
    sent = channels.adapter("internal").sent_messages
    assert sent[0].target == "thread-1"
    assert "后台主代理已检查任务树" in sent[0].content


def test_thread_goal_turn_with_no_tool_calls_stops_auto_continuation(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = agent.conversation_store
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-goal",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    goal = store.create_goal(
        {"thread_id": thread.thread_id, "objective": "持续推进同一件工作", "now": 11.0}
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": goal.task_id,
            "goal": goal.objective,
            "status": "active",
            "now": 12.0,
        }
    )
    first_wake = store.raise_wake_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": goal.task_id,
            "reason": "thread_goal_continue",
            "dedupe_key": f"thread-goal:{goal.goal_id}",
            "metadata": {"goal_id": goal.goal_id},
            "now": 13.0,
        }
    )

    reports = scheduler.tick(now=14.0)

    assert len(reports) == 1
    assert "Continue working toward the active thread goal" in backend.prompts[0]
    updated = store.load_goal(thread.thread_id)
    assert updated is not None and updated.status == "active"
    pending = store.pending_wake_signals()
    assert pending == []
    assert first_wake.status == "pending"


def test_thread_goal_with_tool_progress_schedules_exactly_one_next_turn(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=True, memory_path="memory.jsonl"),
        tmp_path,
    )
    agent.backend = _GoalToolProgressBackend()
    store = agent.conversation_store
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-goal-progress",
            "channel_user_id": "user-1",
        }
    )
    goal = store.create_goal({"thread_id": thread.thread_id, "objective": "持续检查目录并推进"})
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": goal.task_id,
            "goal": goal.objective,
            "status": "active",
        }
    )
    first = store.raise_wake_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": goal.task_id,
            "reason": "thread_goal_continue",
            "dedupe_key": f"thread-goal:{goal.goal_id}",
            "metadata": {"goal_id": goal.goal_id},
        }
    )

    reports = scheduler.tick()

    assert len(reports) == 1 and reports[0].tool_call_count == 1
    pending = store.pending_wake_signals()
    assert len(pending) == 1
    assert pending[0].wake_signal_id != first.wake_signal_id
    assert pending[0].reason == "thread_goal_continue"


def test_thread_goal_waits_for_child_events_without_polling_or_chat_noise(tmp_path) -> None:
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest, _run_params

    agent = SimpleAgent(
        AgentConfig(
            enable_tools=True,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
        ),
        tmp_path,
    )
    backend = _GoalToolProgressBackend()
    agent.backend = backend
    store = agent.conversation_store
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-goal-child",
            "channel_user_id": "user-1",
        }
    )
    goal = store.create_goal({"thread_id": thread.thread_id, "objective": "完成一个并行项目"})
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": goal.task_id,
            "goal": goal.objective,
            "status": "active",
        }
    )
    agent.subagents.create_run(
        goal="实现模块甲",
        thought="",
        plan=["实现"],
        parent_id=goal.task_id,
        root_id=goal.task_id,
    )
    first = store.raise_wake_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": goal.task_id,
            "reason": "thread_goal_continue",
            "dedupe_key": f"thread-goal:{goal.goal_id}",
            "metadata": {"goal_id": goal.goal_id},
        }
    )

    params = _run_params(
        thread.thread_id,
        BackgroundRunRequest(
            thread_id=thread.thread_id,
            task_id=goal.task_id,
            reason="thread_goal_continue",
        ),
        agent,
    )
    reports = scheduler.tick()

    assert "inspect_agent_tree" in params.allowed_tools
    assert "create_subagents" in params.allowed_tools
    assert reports == []
    assert backend.prompts == []
    assert store.pending_wake_signals() == []
    assert first.status == "pending"
    assert channels.adapter("internal").sent_messages == []
    assert store.recent_messages(thread.thread_id) == []

    guidance = store.append_guidance(
        {
            "target_type": "task",
            "target_id": goal.task_id,
            "message": "补充一个当前任务要求",
            "sender": "user",
        }
    )
    store.raise_wake_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": goal.task_id,
            "reason": "thread_goal_continue",
            "dedupe_key": f"thread-goal-guided:{goal.goal_id}",
            "metadata": {"goal_id": goal.goal_id, "guidance_id": guidance.guidance_id},
        }
    )

    guided_reports = scheduler.tick()

    assert len(guided_reports) == 1
    assert guided_reports[0].delivery_status == "suppressed"
    assert "Their lifecycle events will wake this same goal again" in backend.prompts[0]
    assert store.pending_wake_signals() == []


@pytest.mark.xfail(
    reason="EXEC-31b 存量债: native 语义下 goal 完成整合轮投递被抑制(suppressed), 整合收口投递路径待适配"
)
def test_terminal_goal_children_trigger_one_integrating_closeout(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=True,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
        ),
        tmp_path,
    )
    backend = _GoalCompletingBackend()
    agent.backend = backend
    store = agent.conversation_store
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-goal-closeout",
            "channel_user_id": "user-1",
        }
    )
    goal = store.create_goal({"thread_id": thread.thread_id, "objective": "完成并验证整个项目"})
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": goal.task_id,
            "goal": goal.objective,
            "status": "active",
        }
    )
    child = agent.subagents.create_run(
        goal="完成实现",
        thought="",
        plan=["实现"],
        parent_id=goal.task_id,
        root_id=goal.task_id,
    )
    agent.subagents.lifecycle.set_status(child.id, "DONE")
    signal = store.raise_wake_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": goal.task_id,
            "reason": "subagent_runner_finished",
            "dedupe_key": f"goal-child:{child.id}",
            "metadata": {"task_id": child.id, "status": "DONE"},
        }
    )

    reports = scheduler.tick(now=signal.created_at + 100)

    assert len(reports) == 1
    assert reports[0].delivery_status == "sent"
    assert reports[0].delivery_reason == "thread_goal_completion"
    assert "Completion audit" in backend.prompts[0]
    assert "A child status or prose is evidence, not authority" in backend.prompts[0]
    assert store.load_goal(thread.thread_id).status == "complete"
    links = {item.task_id: item for item in store.task_links(thread.thread_id)}
    assert links[goal.task_id].status == "completed"
    assert [item.content for item in channels.adapter("internal").sent_messages] == [
        "已经完成整合和验证。"
    ]
    final_row = store.recent_messages(thread.thread_id, limit=1)[0]
    assert final_row.metadata["operation_verification"]["groups"][0]["label"] == "update_goal"

    stale = store.raise_wake_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": goal.task_id,
            "reason": "subagent_runner_finished",
            "dedupe_key": f"goal-child-stale:{child.id}",
            "metadata": {"task_id": child.id, "status": "DONE"},
        }
    )
    assert scheduler._run_wake_signal(stale, now=time.time()) is None
    assert backend.calls == 2
    assert signal.status == "pending"


def test_thread_goal_provider_usage_limit_maps_to_usage_limited(tmp_path, monkeypatch) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    store = agent.conversation_store
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-goal-limit",
            "channel_user_id": "user-1",
        }
    )
    goal = store.create_goal({"thread_id": thread.thread_id, "objective": "持续推进"})
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": goal.task_id,
            "goal": goal.objective,
            "status": "active",
        }
    )
    signal = store.raise_wake_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": goal.task_id,
            "reason": "thread_goal_continue",
            "dedupe_key": f"thread-goal:{goal.goal_id}",
            "metadata": {"goal_id": goal.goal_id},
        }
    )

    def fail_run(_request):
        raise ProviderUsageLimitError("HTTP 429")

    monkeypatch.setattr(scheduler, "_run_claimed", fail_run)
    with pytest.raises(ProviderUsageLimitError):
        scheduler._run_wake_signal(signal, now=20.0)

    updated = store.load_goal(thread.thread_id)
    assert updated is not None and updated.status == "usage_limited"
    links = {item.task_id: item for item in store.task_links(thread.thread_id)}
    assert links[goal.task_id].status == "interrupted"
    assert store.pending_wake_signals() == []


def test_completed_task_drops_queued_scheduled_continuation(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = agent.conversation_store
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=FakeDeliveryService(),
            ),
            "store": store,
        }
    )
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "foreground-terminal-wake",
            "channel_user_id": "user-1",
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-complete",
            "goal": "完成长任务",
            "status": "active",
        }
    )
    signal = store.raise_wake_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": "task-complete",
            "reason": "scheduled_progress_report",
            "dedupe_key": "foreground-task-complete",
        }
    )
    store.update_task_status({"task_id": "task-complete", "status": "completed"})

    assert scheduler._run_wake_signal(signal, now=time.time()) is None
    assert backend.prompts == []
    assert store.pending_wake_signals() == []


def test_task_continuation_uses_the_same_thread_history_and_compact(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = agent.conversation_store
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.append_message(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": "请完成任务甲的七天晚餐方案。",
            "channel": "feishu",
            "metadata": {"gateway_request_id": "task-1"},
            "now": 11.0,
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "goal": "完成任务甲的七天晚餐方案",
            "now": 12.0,
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-2",
            "goal": "任务乙私有目标-不应出现在任务甲",
            "now": 13.0,
        }
    )
    store.update_summary(thread.thread_id, "普通聊天压缩摘要-青柚47", now=14.0)
    store.append_message(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": "普通聊天核对词青柚47，不要把它写进任务。",
            "channel": "feishu",
            "metadata": {"gateway_request_id": "chat-request-2"},
            "now": 15.0,
        }
    )
    store.append_message(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": "预算控制在三百元内。",
            "channel": "feishu",
            "metadata": {"kind": "active_turn_user_input"},
            "now": 16.0,
        }
    )
    store.append_message(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": "第二步只实现营养评分、时间衰减和对应测试。",
            "channel": "feishu",
            "metadata": {"gateway_request_id": "chat-request-3"},
            "now": 16.75,
        }
    )
    store.set_progress_policy(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "interval_seconds": 60,
            "route_channel": "feishu",
            "route_target": "chat-1",
            "now": 17.0,
        }
    )

    reports = scheduler.tick(now=77.0)
    prompt = backend.prompts[0]

    assert len(reports) == 1
    assert "请完成任务甲的七天晚餐方案" in prompt
    assert "完成任务甲的七天晚餐方案" in prompt
    assert "预算控制在三百元内" in prompt
    assert "第二步只实现营养评分、时间衰减和对应测试" in prompt
    assert "青柚47" in prompt
    assert "普通聊天压缩摘要-青柚47" in prompt
    # The shared transcript/compact stays visible, but an unrelated task link is
    # operational metadata rather than conversation history.
    assert "任务乙私有目标" not in prompt
    assert '"ordinary_thread_messages_included": false' not in prompt
    assert '"conversation_compact_included": false' not in prompt


@pytest.mark.xfail(
    reason="EXEC-31b 存量债: native 语义下 goal 完成整合轮投递被抑制(suppressed), 整合收口投递路径待适配"
)
def test_detached_named_task_excludes_future_ordinary_turns_from_background_context(
    tmp_path,
) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = agent.conversation_store
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=FakeDeliveryService(),
            ),
            "store": store,
        }
    )
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.update_summary(thread.thread_id, "创建前安全摘要-银杏31", now=10.25)
    store.append_message(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": "创建前约定：使用已确认的五个来源。",
            "channel": "feishu",
            "metadata": {"gateway_request_id": "chat-before"},
            "now": 11.0,
        }
    )
    link = store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-1",
            "goal": "持续研判五个来源",
            "work_kind": "audit",
            "work_name": "五路监测",
            "cancellation_scope": "detached",
            "now": 12.0,
        }
    )
    store.append_message(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": "启动五路监测。",
            "channel": "feishu",
            "metadata": {"gateway_request_id": "audit-1"},
            "now": 12.25,
        }
    )
    store.append_message(
        {
            "thread_id": thread.thread_id,
            "role": "assistant",
            "content": "五路来源工作者已建立。",
            "channel": "feishu",
            "metadata": {"task_id": "audit-1"},
            "now": 12.5,
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "ordinary-code-task",
            "goal": "实现普通代码任务-不应进入 Audit",
            "now": 13.0,
        }
    )
    store.update_summary(thread.thread_id, "后来污染摘要-红杉99", now=14.0)
    store.append_message(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": "后来普通任务：请新建 LRU 缓存项目-红杉99。",
            "channel": "feishu",
            "metadata": {"gateway_request_id": "ordinary-code-task"},
            "now": 15.0,
        }
    )
    guidance = store.append_guidance(
        {
            "target_type": "task",
            "target_id": "audit-1",
            "message": "只给高置信发现发消息。",
            "metadata": {
                "record_in_transcript": True,
                "thread_id": thread.thread_id,
                "channel": "feishu",
            },
            "now": 16.0,
        }
    )
    store.set_progress_policy(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-1",
            "interval_seconds": 60,
            "route_channel": "feishu",
            "route_target": "chat-1",
            "now": 17.0,
        }
    )

    reports = scheduler.tick(now=77.0)
    prompt = backend.prompts[0]
    rows = store.recent_messages(thread.thread_id, limit=0)
    guidance_row = next(
        row for row in rows if row.metadata.get("guidance_id") == guidance.guidance_id
    )

    assert len(reports) == 1
    assert link.context_anchor_message_id
    assert "创建前约定：使用已确认的五个来源" in prompt
    assert "创建前安全摘要-银杏31" not in prompt
    assert "持续研判五个来源" in prompt
    assert "启动五路监测" in prompt
    assert "五路来源工作者已建立" in prompt
    assert "只给高置信发现发消息" in prompt
    assert "后来普通任务" not in prompt
    assert "LRU 缓存" not in prompt
    assert "红杉99" not in prompt
    assert "实现普通代码任务" not in prompt
    assert guidance_row.metadata["task_id"] == "audit-1"


def test_automatic_supervision_skips_unchanged_llm_turn_and_runs_on_material_delta(
    tmp_path,
) -> None:
    from agent_py_agent.agent.conversation.progress_fingerprint import subagent_material_signature

    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
        ),
        tmp_path,
    )
    backend = _CapturingBackend()
    agent.backend = backend
    child = agent.subagents.create_run(
        goal="后台做长任务",
        thought="",
        plan=["执行"],
        parent_id="task-1",
        root_id="task-1",
    )
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "goal": "后台做长任务",
            "now": 11.0,
        }
    )
    signature = subagent_material_signature(
        agent,
        task_id="task-1",
        watched_run_ids=[child.id],
    )
    policy = store.set_progress_policy(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "interval_seconds": 60,
            "now": 12.0,
            "metadata": {
                "kind": "subagent_progress_watch",
                "tool": "dispatch_supervision_auto",
                "watched_run_ids": [child.id],
                "material_signature": signature,
            },
        }
    )
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=store,
        channels=FakeDeliveryService(),
    )
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})

    assert scheduler.tick(now=73.0) == []
    assert backend.prompts == []
    checked = store.get_progress_policy(policy.policy_id)
    assert checked is not None
    assert checked.last_report_at == 0.0
    assert checked.metadata["last_material_check_at"] == 73.0

    changed = agent.subagents.load(child.id)
    changed.progress = 0.5
    changed.last_progress_at = 80.0
    changed.last_progress_summary = "完成一半"
    agent.subagents.save(changed)

    reports = scheduler.tick(now=checked.next_due_at + 1)

    assert len(reports) == 1
    assert len(backend.prompts) == 2
    assert "[natural-user-reply]" not in backend.prompts[0]
    assert "[natural-user-reply]" in backend.prompts[1]
    updated = store.get_progress_policy(policy.policy_id)
    assert updated is not None
    assert updated.metadata["material_signature"] != signature


def test_periodic_policy_for_durable_audit_source_worker_is_retired(tmp_path) -> None:
    from agent_py_agent.agent.common.audit_activation import AUDIT_SOURCE_WORKER_ATTR
    from agent_py_agent.agent.conversation.runtime import (
        _runnable_due_policies,
        _snooze_suppressed_policies,
    )

    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    child = agent.subagents.create_run(
        goal="持续处理一个来源",
        thought="",
        plan=["按租约处理批次"],
        parent_id="audit-1",
        root_id="audit-1",
        attributes={AUDIT_SOURCE_WORKER_ATTR: True},
    )
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 9.0,
        }
    )
    policy = store.set_progress_policy(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-1",
            "interval_seconds": 60,
            "now": 10.0,
            "metadata": {
                "kind": "subagent_progress_watch",
                "tool": "dispatch_supervision_auto",
                "watched_run_ids": [child.id],
            },
        }
    )

    runnable, suppressed = _runnable_due_policies(
        store,
        [policy],
        now=71.0,
        agent=agent,
    )
    rows = _snooze_suppressed_policies(
        store,
        suppressed,
        now=71.0,
        agent=agent,
    )

    assert runnable == []
    assert suppressed == [(policy, "durable_audit_source_worker_policy")]
    assert rows[0]["reason"] == "durable_audit_source_worker_policy"
    retired = store.get_progress_policy(policy.policy_id)
    assert retired is not None and retired.enabled is False


def test_partial_successful_subagent_wake_stays_out_of_ordinary_chat_until_batch_finishes(
    tmp_path,
) -> None:
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False, memory_path="memory.jsonl", orphan_supervision_interval_seconds=0
        ),
        tmp_path,
    )
    agent.backend = _CapturingBackend()
    first = agent.subagents.create_run(
        goal="完成第一部分",
        thought="",
        plan=["执行"],
        parent_id="task-root",
        root_id="task-root",
    )
    second = agent.subagents.create_run(
        goal="完成第二部分",
        thought="",
        plan=["执行"],
        parent_id="task-root",
        root_id="task-root",
    )
    agent.subagents.lifecycle.set_status(first.id, "DONE")
    agent.subagents.lifecycle.set_status(second.id, "RUNNING")
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.bind_task(
        {"thread_id": thread.thread_id, "task_id": "task-root", "goal": "分两部分完成", "now": 11.0}
    )
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)

    partial = runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-root",
            "reason": "subagent_runner_finished",
            "wake_signal": {
                "root_task_id": "task-root",
                "source_agent_id": first.id,
                "metadata": {"task_id": first.id, "status": "DONE"},
            },
            "now": 20.0,
        }
    )

    assert partial.delivery_status == "suppressed"
    assert partial.delivery_reason == "partial_subagent_success"
    assert channels.adapter("internal").sent_messages == []
    assert store.recent_messages(thread.thread_id) == []

    agent.backend = _NaturalCompletionBackend()
    agent.subagents.lifecycle.set_status(second.id, "DONE")
    final = runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-root",
            "reason": "subagent_runner_finished",
            "wake_signal": {
                "root_task_id": "task-root",
                "source_agent_id": second.id,
                "metadata": {"task_id": second.id, "status": "DONE"},
            },
            "now": 30.0,
        }
    )

    assert final.delivery_status == "sent"
    assert final.delivery_reason == "root_subagents_terminal"
    assert len(channels.adapter("internal").sent_messages) == 1
    assert [row.content for row in store.recent_messages(thread.thread_id)] == [final.response]


def test_subagent_state_load_error_suppresses_until_exact_root_task_is_completed(tmp_path) -> None:
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundRunRequest,
        _background_delivery_decision,
    )

    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    child = agent.subagents.create_run(
        goal="完成当前部分",
        thought="",
        plan=["执行"],
        parent_id="task-root",
        root_id="task-root",
    )
    agent.subagents.lifecycle.set_status(child.id, "DONE")
    unrelated = agent.subagents.workspace / "unrelated-broken-run"
    unrelated.mkdir(parents=True)
    (unrelated / "task.json").write_text("{ broken", encoding="utf-8")
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.bind_task(
        {"thread_id": thread.thread_id, "task_id": "task-root", "goal": "完成全部工作", "now": 11.0}
    )
    request = BackgroundRunRequest(
        thread_id=thread.thread_id,
        task_id="task-root",
        reason="subagent_runner_finished",
        wake_signal={
            "root_task_id": "task-root",
            "source_agent_id": child.id,
            "metadata": {"task_id": child.id, "status": "DONE"},
        },
    )

    deliver, reason = _background_delivery_decision(agent, request, store=store)

    assert deliver is False
    assert reason == "subagent_state_load_error"

    store.update_task_status({"task_id": "task-root", "status": "completed", "now": 20.0})
    deliver, reason = _background_delivery_decision(agent, request, store=store)

    assert deliver is True
    assert reason == "root_task_completed_with_subagent_state_load_error"


@pytest.mark.parametrize(
    ("requires_llm_report", "evidence_refs", "expected"),
    [
        (
            True,
            ["audit://watch-1/candidate/1:0"],
            (False, "audit_finding_message_tool_only"),
        ),
        (False, ["audit://watch-1/candidate/1:0"], (False, "audit_finding_not_reportable")),
        (True, [], (False, "audit_finding_not_reportable")),
    ],
)
def test_audit_finding_delivery_requires_typed_report_request_and_evidence(
    tmp_path,
    requires_llm_report,
    evidence_refs,
    expected,
) -> None:
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundRunRequest,
        _background_delivery_decision,
    )

    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "owner-a",
            "channel": "feishu",
            "channel_conversation_id": "chat-a",
            "channel_user_id": "owner-a",
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-1",
            "goal": "持续研判",
            "work_kind": "audit",
            "work_name": "安全审计",
        }
    )
    request = BackgroundRunRequest(
        thread_id=thread.thread_id,
        task_id="audit-1",
        reason="audit_finding",
        wake_signal={
            "root_task_id": "audit-1",
            "source_agent_id": "source-worker-1",
            "evidence_refs": evidence_refs,
            "metadata": {
                "schema_version": "audit-finding-event.v1",
                "audit_id": "audit-1",
                "watch_id": "watch-1",
                "finding_id": "af-1",
                "requires_llm_report": requires_llm_report,
                "delivery_evidence_refs": evidence_refs,
            },
        },
    )

    assert _background_delivery_decision(agent, request, store=store) == expected


def test_audit_finding_tool_profile_uses_typed_message_delivery() -> None:
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundToolPolicyRequest,
        background_tool_policy_decision,
    )

    decision = background_tool_policy_decision(
        request=BackgroundToolPolicyRequest(reason="audit_finding")
    )

    assert decision.profile == "audit_finding"
    assert "send_message" in decision.allowed_tools
    assert "watch_stream" in decision.allowed_tools
    assert "record_finding" not in decision.allowed_tools


def test_audit_finding_internal_route_hides_proactive_delivery_tool() -> None:
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundRunRequest,
        BackgroundToolPolicyRequest,
        _run_params,
        background_prompt,
        background_tool_policy_decision,
    )

    decision = background_tool_policy_decision(
        request=BackgroundToolPolicyRequest(
            reason="audit_finding",
            proactive_delivery_available=False,
        )
    )
    params = _run_params(
        "thread-1",
        BackgroundRunRequest(
            thread_id="thread-1",
            task_id="audit-1",
            reason="audit_finding",
        ),
        proactive_delivery_available=False,
    )
    prompt = background_prompt(
        "audit_finding",
        proactive_delivery_available=False,
    )

    assert decision.profile == "audit_finding"
    assert "send_message" not in decision.allowed_tools
    assert "send_message" in decision.removed_tools
    assert "delivery_route_capability" in decision.sources
    assert "send_message" not in (params.allowed_tools or [])
    assert "conversation transcript" in prompt
    assert "final assistant text" in prompt


def test_audit_finding_message_tool_delivery_is_mirrored_once_with_evidence(
    tmp_path,
    monkeypatch,
) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "owner-a",
            "channel": "feishu",
            "channel_conversation_id": "chat-a",
            "channel_user_id": "owner-a",
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-1",
            "goal": "持续研判",
            "work_kind": "audit",
            "work_name": "安全审计",
        }
    )
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=store,
        channels=channels,
    )
    request = {
        "thread_id": thread.thread_id,
        "task_id": "audit-1",
        "reason": "audit_finding",
        "wake_signal": {
            "wake_signal_id": "wake-af-1",
            "root_task_id": "audit-1",
            "evidence_refs": ["audit://watch-1/candidate/1:0"],
            "metadata": {
                "schema_version": "audit-finding-event.v1",
                "audit_id": "audit-1",
                "watch_id": "watch-1",
                "finding_id": "af-1",
                "requires_llm_report": True,
                "delivery_evidence_refs": ["audit://watch-1/candidate/1:0"],
            },
        },
        "now": 20.0,
    }

    monkeypatch.setattr(
        runtime,
        "_run_agent",
        lambda *_args, **_kwargs: (
            "这段内部收口文字不能成为第二条用户消息。",
            0,
            0,
            0,
            (),
            (
                {
                    "schema_version": "message_tool_delivery.v1",
                    "delivery_status": "sent",
                    "source_owner_delivery": True,
                    "channel": "feishu",
                    "content": "发现一项明确事件，证据已保留。",
                    "receipt_id": "receipt-af-1",
                    "evidence_refs": ["audit://watch-1/candidate/1:0"],
                    "deduplicated": False,
                    "attachments": [],
                },
            ),
            {},
        ),
    )
    sent = runtime.run_once(request)

    assert sent.delivery_status == "sent"
    assert sent.delivery_reason == "audit_finding_report"
    assert sent.wake_handled is True
    messages = store.recent_messages(thread.thread_id)
    assert [row.content for row in messages] == ["发现一项明确事件，证据已保留。"]
    assert messages[0].metadata["evidence_refs"] == ["audit://watch-1/candidate/1:0"]
    # send_message already performed the external side effect. The runtime only
    # mirrors its typed receipt and must not call the channel a second time.
    assert channels.adapter("feishu").sent_messages == []


def test_audit_finding_without_message_tool_delivery_stays_internal_and_retryable(
    tmp_path,
    monkeypatch,
) -> None:
    class _FailedDelivery:
        def supports_proactive(self, channel: str) -> bool:
            return channel == "feishu"

        def deliver(self, context, envelope):
            raise AssertionError("internal final text must not reach the channel")

    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "owner-a",
            "channel": "feishu",
            "channel_conversation_id": "chat-a",
            "channel_user_id": "owner-a",
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-1",
            "goal": "持续研判",
            "work_kind": "audit",
            "work_name": "安全审计",
        }
    )
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=store,
        channels=_FailedDelivery(),
    )
    monkeypatch.setattr(
        runtime,
        "_run_agent",
        lambda *_args, **_kwargs: (
            "发现一项明确事件，证据已保留。",
            0,
            0,
            0,
            (),
            (),
            {},
        ),
    )

    report = runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-1",
            "reason": "audit_finding",
            "wake_signal": {
                "wake_signal_id": "wake-af-failed",
                "root_task_id": "audit-1",
                "evidence_refs": ["audit://watch-1/candidate/1:0"],
                "metadata": {
                    "schema_version": "audit-finding-event.v1",
                    "finding_id": "af-1",
                    "requires_llm_report": True,
                    "delivery_evidence_refs": ["audit://watch-1/candidate/1:0"],
                },
            },
            "now": 20.0,
        }
    )

    assert report.delivery_status == "suppressed"
    assert report.delivery_reason == "audit_finding_message_tool_only"
    assert report.wake_handled is False
    assert store.recent_messages(thread.thread_id) == []


def test_audit_finding_context_projects_exact_event_without_supervision_noise(
    tmp_path,
) -> None:
    from agent_py_agent.agent.conversation.runtime import (
        _AUDIT_FINDING_REPORT_PROMPT,
        BackgroundRunRequest,
        context_markdown,
    )

    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "owner-a",
            "channel": "feishu",
            "channel_conversation_id": "chat-a",
            "channel_user_id": "owner-a",
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-1",
            "goal": "持续研判五路安全数据并及时报告真实事件",
            "work_kind": "audit",
            "work_name": "安全审计",
            "cancellation_scope": "detached",
            "run_prompt": "继续当前五路监控并及时报告真实事件",
            "effective_revision": 5,
            "effective_source_bindings": [
                {
                    "source_id": "proc-source",
                    "url": "https://events.invalid/proc",
                    "source_profile_ref": "/owner/audits/audit-1/work/sources/proc.txt",
                }
            ],
        }
    )
    request = BackgroundRunRequest(
        thread_id=thread.thread_id,
        task_id="audit-1",
        reason="audit_finding",
        wake_signal={
            "wake_signal_id": "wake-af-1",
            "root_task_id": "audit-1",
            "summary": "检测到命令执行并获得稳定回显",
            "evidence_refs": ["audit://watch-1/candidate/97:0"],
            "metadata": {
                "schema_version": "audit-finding-event.v1",
                "report_scope": "incremental",
                "finding_id": "af-1",
                "requires_llm_report": True,
                "source_id": "proc-source",
                "score": 95,
                "verdict": "hit",
                "evidence_records": [
                    {
                        "source_ref": "audit://watch-1/candidate/97:0",
                        "inline": True,
                        "raw_complete": True,
                        "raw_event": {"process_event_id": "PROC-0097"},
                    }
                ],
            },
        },
    )

    rendered = context_markdown(
        agent=agent,
        store=store,
        thread=thread,
        request=request,
    )

    assert "检测到命令执行并获得稳定回显" in rendered
    assert "audit://watch-1/candidate/97:0" in rendered
    assert "PROC-0097" in rendered
    assert "finding_id is an internal delivery" in _AUDIT_FINDING_REPORT_PROMPT
    assert "report_scope is incremental" in _AUDIT_FINDING_REPORT_PROMPT
    assert "继续当前五路监控并及时报告真实事件" in rendered
    assert "/owner/audits/audit-1/work/sources/proc.txt" in rendered
    # A prepare-derived cross-source summary is not the reporting authority for
    # one exact finding; the matching source binding and typed wake are.
    assert "持续研判五路安全数据并及时报告真实事件" not in rendered
    assert "## Audit Task Objective" in rendered
    assert "## Recent Messages" not in rendered
    assert "## Bound Tasks" not in rendered
    assert "## Guidance" not in rendered
    assert "## Agent Tree Snapshot" not in rendered
    assert "## Pending Wake Signals" not in rendered
    assert "## Recovery Snapshot" not in rendered
    assert "## Task Runtime State" not in rendered


def test_failed_audit_finding_delivery_does_not_consume_wake(
    tmp_path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.conversation.models import BackgroundMainAgentReport

    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
        ),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "owner-a",
            "channel": "internal",
            "channel_conversation_id": "chat-a",
            "channel_user_id": "owner-a",
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-1",
            "goal": "持续研判",
            "work_kind": "audit",
            "work_name": "安全审计",
        }
    )
    signal = store.raise_wake_signal(
        {
            "thread_id": thread.thread_id,
            "reason": "audit_finding",
            "root_task_id": "audit-1",
            "evidence_refs": ["audit://watch-1/candidate/1:0"],
            "metadata": {
                "schema_version": "audit-finding-event.v1",
                "audit_id": "audit-1",
                "watch_id": "watch-1",
                "finding_id": "af-1",
                "requires_llm_report": True,
                "delivery_evidence_refs": ["audit://watch-1/candidate/1:0"],
            },
            "now": 10.0,
        }
    )
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=FakeDeliveryService(),
            ),
            "store": store,
        }
    )
    attempts = []

    def pending_report(_kwargs):
        attempts.append(1)
        return BackgroundMainAgentReport(
            thread_id=thread.thread_id,
            task_id="audit-1",
            reason="audit_finding",
            response="",
            route_channel="internal",
            route_target="owner-a",
            created_at=20.0,
            delivery_status="failed",
            delivery_reason="audit_finding_report",
            wake_handled=False,
        )

    monkeypatch.setattr(scheduler, "_run_claimed", pending_report)

    assert scheduler._run_wake_signal(signal, now=20.0) is None
    assert [item.wake_signal_id for item in store.pending_wake_signals()] == [signal.wake_signal_id]
    assert scheduler.tick(now=21.0) == []
    assert len(attempts) == 1
    assert scheduler.tick(now=50.0) == []
    assert len(attempts) == 2
    assert scheduler._wake_retry_after[signal.wake_signal_id] == 80.0


def test_completed_audit_keeps_unreceipted_typed_finding_until_delivery(
    tmp_path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.conversation.models import BackgroundMainAgentReport

    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
        ),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "owner-a",
            "channel": "feishu",
            "channel_conversation_id": "chat-a",
            "channel_user_id": "owner-a",
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-1",
            "goal": "持续研判",
            "work_kind": "audit",
            "work_name": "安全审计",
            "status": "active",
        }
    )
    signal = store.raise_wake_signal(
        {
            "thread_id": thread.thread_id,
            "reason": "audit_finding",
            "root_task_id": "audit-1",
            "source_agent_id": "source-worker-1",
            "evidence_refs": ["audit://watch-1/candidate/97:0"],
            "metadata": {
                "schema_version": "audit-finding-event.v1",
                "audit_id": "audit-1",
                "watch_id": "watch-1",
                "finding_id": "finding-97",
                "requires_llm_report": True,
                "delivery_evidence_refs": ["audit://watch-1/candidate/97:0"],
            },
            "now": 10.0,
        }
    )
    store.update_task_status({"task_id": "audit-1", "status": "completed", "now": 20.0})
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=FakeDeliveryService(),
            ),
            "store": store,
        }
    )
    attempts: list[str] = []

    def failed_delivery(kwargs):
        attempts.append("failed")
        return BackgroundMainAgentReport(
            thread_id=thread.thread_id,
            task_id="audit-1",
            reason="audit_finding",
            response="",
            route_channel="feishu",
            route_target="owner-a",
            created_at=21.0,
            delivery_status="failed",
            delivery_reason="audit_finding_report",
            wake_handled=False,
        )

    monkeypatch.setattr(scheduler, "_run_claimed", failed_delivery)

    assert scheduler.tick(now=21.0) == []
    assert attempts == ["failed"]
    assert store.pending_wake_signal(signal.wake_signal_id) is not None

    def delivered(kwargs):
        attempts.append("sent")
        return BackgroundMainAgentReport(
            thread_id=thread.thread_id,
            task_id="audit-1",
            reason="audit_finding",
            response="已发送。",
            route_channel="feishu",
            route_target="owner-a",
            created_at=51.0,
            delivery_status="sent",
            delivery_reason="audit_finding_report",
            wake_handled=True,
        )

    monkeypatch.setattr(scheduler, "_run_claimed", delivered)

    reports = scheduler.tick(now=51.0)
    assert [item.delivery_status for item in reports] == ["sent"]
    assert attempts == ["failed", "sent"]
    assert store.pending_wake_signal(signal.wake_signal_id) is None


def test_completed_root_retires_ordinary_late_child_wake(tmp_path, monkeypatch) -> None:
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
        ),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "owner-a",
            "channel": "feishu",
            "channel_conversation_id": "chat-a",
            "channel_user_id": "owner-a",
        }
    )
    store.bind_task({"thread_id": thread.thread_id, "task_id": "task-1", "goal": "旧任务"})
    store.update_task_status({"task_id": "task-1", "status": "completed", "now": 20.0})
    signal = store.raise_wake_signal(
        {
            "thread_id": thread.thread_id,
            "reason": "subagent_runner_finished",
            "root_task_id": "task-1",
            "source_agent_id": "child-1",
            "metadata": {"task_id": "child-1", "status": "DONE"},
            "now": 21.0,
        }
    )
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=FakeDeliveryService(),
            ),
            "store": store,
        }
    )
    monkeypatch.setattr(
        scheduler,
        "_run_claimed",
        lambda _kwargs: pytest.fail("stale child wake must not start a model turn"),
    )

    assert scheduler.tick(now=30.0) == []
    assert store.pending_wake_signal(signal.wake_signal_id) is None


def test_linked_observation_does_not_fork_while_delivery_wake_is_pending(
    tmp_path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.conversation.models import BackgroundMainAgentReport

    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
        ),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "owner-a",
            "channel": "feishu",
            "channel_conversation_id": "chat-a",
            "channel_user_id": "owner-a",
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-1",
            "goal": "持续研判",
            "work_kind": "audit",
            "work_name": "安全审计",
        }
    )
    observation, signal = store.append_observation_with_wake(
        {
            "thread_id": thread.thread_id,
            "event_type": "audit_capacity_alert",
            "summary": "容量已超过阈值",
            "requires_main_agent": True,
            "root_task_id": "audit-1",
        },
        {
            "thread_id": thread.thread_id,
            "reason": "audit_capacity_alert",
            "root_task_id": "audit-1",
            "metadata": {
                "schema_version": "audit-capacity-event.v2",
                "audit_id": "audit-1",
                "capacity_state": "alert",
                "pending": 100,
            },
        },
    )
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=FakeDeliveryService(),
            ),
            "store": store,
        }
    )
    attempts: list[str] = []

    def failed_delivery(kwargs):
        attempts.append(str(kwargs.get("reason") or ""))
        return BackgroundMainAgentReport(
            thread_id=thread.thread_id,
            task_id="audit-1",
            reason=str(kwargs.get("reason") or ""),
            response="",
            route_channel="feishu",
            route_target="owner-a",
            created_at=20.0,
            delivery_status="failed",
            delivery_reason="channel_delivery_failed",
            wake_handled=False,
        )

    monkeypatch.setattr(scheduler, "_run_claimed", failed_delivery)

    assert scheduler.tick(now=20.0) == []
    assert attempts == ["audit_capacity_alert"]
    assert store.pending_wake_signal(signal.wake_signal_id) is not None
    assert [
        item.observation_id for item in store.unhandled_observations_requiring_main(limit=10)
    ] == [observation.observation_id]


def test_completion_observation_fallback_uses_same_partial_delivery_policy(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False, memory_path="memory.jsonl", orphan_supervision_interval_seconds=0
        ),
        tmp_path,
    )
    agent.backend = _CapturingBackend()
    first = agent.subagents.create_run(
        goal="完成第一部分", thought="", plan=["执行"], parent_id="task-root", root_id="task-root"
    )
    agent.subagents.create_run(
        goal="完成第二部分", thought="", plan=["执行"], parent_id="task-root", root_id="task-root"
    )
    agent.subagents.lifecycle.set_status(first.id, "DONE")
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.bind_task(
        {"thread_id": thread.thread_id, "task_id": "task-root", "goal": "分两部分完成", "now": 11.0}
    )
    store.append_observation(
        {
            "thread_id": thread.thread_id,
            "event_type": "subagent_runner_finished",
            "summary": "第一部分已完成。",
            "source_agent_id": first.id,
            "root_task_id": "task-root",
            "requires_main_agent": True,
            "metadata": {"task_id": first.id, "status": "DONE"},
            "now": 20.0,
        }
    )
    channels = FakeDeliveryService()
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels),
            "store": store,
        }
    )

    reports = scheduler.tick(now=21.0)

    assert len(reports) == 1
    assert reports[0].reason == "subagent_runner_finished"
    assert reports[0].delivery_status == "suppressed"
    assert reports[0].delivery_reason == "partial_subagent_success"
    assert channels.adapter("internal").sent_messages == []
    assert store.recent_messages(thread.thread_id) == []


def test_internal_wait_continuation_stays_out_of_chat_while_child_runs(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False, memory_path="memory.jsonl", orphan_supervision_interval_seconds=0
        ),
        tmp_path,
    )
    agent.backend = _CapturingBackend()
    agent.subagents.create_run(
        goal="继续执行", thought="", plan=["执行"], parent_id="task-root", root_id="task-root"
    )
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.bind_task(
        {"thread_id": thread.thread_id, "task_id": "task-root", "goal": "后台继续", "now": 11.0}
    )
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)

    report = runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-root",
            "reason": "scheduled_progress_report",
            "wake_signal": {
                "kind": "progress_policy_due",
                "registered_by_tool": "wait",
                "task_id": "task-root",
            },
            "now": 20.0,
        }
    )

    assert report.delivery_status == "suppressed"
    assert report.delivery_reason == "internal_scheduled_continuation"
    assert channels.adapter("internal").sent_messages == []
    assert store.recent_messages(thread.thread_id) == []


def test_internal_wait_completion_delivers_model_authored_final_reply(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False, memory_path="memory.jsonl", orphan_supervision_interval_seconds=0
        ),
        tmp_path,
    )
    backend = _CapturingBackend()
    agent.backend = backend
    child = agent.subagents.create_run(
        goal="继续执行", thought="", plan=["执行"], parent_id="task-root", root_id="task-root"
    )
    agent.subagents.lifecycle.set_status(child.id, "DONE")
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.bind_task(
        {"thread_id": thread.thread_id, "task_id": "task-root", "goal": "后台继续", "now": 11.0}
    )
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)

    report = runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-root",
            "reason": "scheduled_progress_report",
            "wake_signal": {
                "kind": "progress_policy_due",
                "registered_by_tool": "wait",
                "task_id": "task-root",
            },
            "now": 20.0,
        }
    )

    assert report.delivery_status == "sent"
    assert report.delivery_reason == "internal_scheduled_completion"
    assert channels.adapter("internal").sent_messages[0].content == report.response
    assert [row.content for row in store.recent_messages(thread.thread_id)] == [report.response]


def test_scheduled_turn_accepts_mid_turn_child_event_without_starting_second_main_run(
    tmp_path,
) -> None:
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False, memory_path="memory.jsonl", orphan_supervision_interval_seconds=0
        ),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.bind_task(
        {"thread_id": thread.thread_id, "task_id": "task-root", "goal": "后台继续", "now": 11.0}
    )
    store.set_progress_policy(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-root",
            "interval_seconds": 60,
            "route_channel": "internal",
            "route_target": "thread-1",
            "now": 12.0,
        }
    )
    backend = _MidTurnLifecycleBackend(
        store=store,
        thread_id=thread.thread_id,
        task_id="task-root",
    )
    agent.backend = backend
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=FakeDeliveryService(),
            ),
            "store": store,
        }
    )

    reports = scheduler.tick(now=72.0)

    assert len(reports) == 1
    assert backend.calls == 2
    assert "RUNTIME_TASK_EVENTS" not in backend.prompts[0]
    assert backend.signal is not None
    assert backend.signal.wake_signal_id in backend.prompts[1]
    assert reports[0].response == "已接收子代理的新结果并继续整合。"
    assert store.pending_wake_signals() == []
    claim = store.load_background_run_claim(thread.thread_id)
    assert claim["status"] == "finished"


def test_mid_turn_child_event_stays_retryable_when_provider_fails_after_injection(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False, memory_path="memory.jsonl", orphan_supervision_interval_seconds=0
        ),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.bind_task(
        {"thread_id": thread.thread_id, "task_id": "task-root", "goal": "后台继续", "now": 11.0}
    )
    store.set_progress_policy(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-root",
            "interval_seconds": 60,
            "route_channel": "internal",
            "route_target": "thread-1",
            "now": 12.0,
        }
    )
    backend = _MidTurnLifecycleBackend(
        store=store,
        thread_id=thread.thread_id,
        task_id="task-root",
        fail_after_injection=True,
    )
    agent.backend = backend
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=FakeDeliveryService(),
            ),
            "store": store,
        }
    )

    try:
        scheduler.tick(now=72.0)
    except RuntimeError as exc:
        assert "provider failed after runtime event injection" in str(exc)
    else:
        raise AssertionError("provider failure should leave the runtime event retryable")

    assert backend.signal is not None
    assert [item.wake_signal_id for item in store.pending_wake_signals()] == [
        backend.signal.wake_signal_id
    ]
    assert store.load_background_run_claim(thread.thread_id)["status"] == "failed"


def test_internal_continuation_delivers_natural_runtime_completion(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False, memory_path="memory.jsonl", orphan_supervision_interval_seconds=0
        ),
        tmp_path,
    )
    agent.backend = _NaturalCompletionBackend()
    child = agent.subagents.create_run(
        goal="继续执行", thought="", plan=["执行"], parent_id="task-root", root_id="task-root"
    )
    agent.subagents.lifecycle.set_status(child.id, "DONE")
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.bind_task(
        {"thread_id": thread.thread_id, "task_id": "task-root", "goal": "后台继续", "now": 11.0}
    )
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)

    report = runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-root",
            "reason": "scheduled_progress_report",
            "wake_signal": {
                "kind": "progress_policy_due",
                "registered_by_tool": "wait",
                "task_id": "task-root",
            },
            "now": 20.0,
        }
    )

    assert report.delivery_status == "sent"
    assert report.delivery_reason == "internal_scheduled_completion"
    messages = store.recent_messages(thread.thread_id)
    assert [row.content for row in messages] == ["任务全部完成。"]
    assert messages[0].created_at > 20.0


def test_done_child_wake_delivers_natural_final_response(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False, memory_path="memory.jsonl", orphan_supervision_interval_seconds=0
        ),
        tmp_path,
    )
    agent.backend = _CapturingBackend()
    child = agent.subagents.create_run(
        goal="继续执行", thought="", plan=["执行"], parent_id="task-root", root_id="task-root"
    )
    agent.subagents.lifecycle.set_status(child.id, "DONE")
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.bind_task(
        {"thread_id": thread.thread_id, "task_id": "task-root", "goal": "后台继续", "now": 11.0}
    )
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)

    report = runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-root",
            "reason": "subagent_runner_finished",
            "wake_signal": {
                "root_task_id": "task-root",
                "source_agent_id": child.id,
                "metadata": {"task_id": child.id, "status": "DONE"},
            },
            "now": 20.0,
        }
    )

    assert report.delivery_status == "sent"
    assert report.delivery_reason == "root_subagents_terminal"
    assert channels.adapter("internal").sent_messages[0].content == report.response
    assert [row.content for row in store.recent_messages(thread.thread_id)] == [report.response]


def test_successful_sibling_completion_wakes_are_coalesced_before_one_llm_turn(
    tmp_path,
) -> None:
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
            background_completion_coalesce_seconds=5,
        ),
        tmp_path,
    )
    backend = _NaturalCompletionBackend()
    agent.backend = backend
    for goal in ("第一部分", "第二部分"):
        child = agent.subagents.create_run(
            goal=goal,
            thought="",
            plan=["执行"],
            parent_id="task-root",
            root_id="task-root",
        )
        agent.subagents.lifecycle.set_status(child.id, "DONE")
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.bind_task(
        {"thread_id": thread.thread_id, "task_id": "task-root", "goal": "两路并行", "now": 11.0}
    )
    for index, created_at in enumerate((20.0, 21.0), start=1):
        store.raise_wake_signal(
            {
                "thread_id": thread.thread_id,
                "reason": "subagent_runner_finished",
                "root_task_id": "task-root",
                "source_agent_id": f"child-{index}",
                "metadata": {"task_id": f"child-{index}", "status": "DONE"},
                "now": created_at,
            }
        )
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})

    assert scheduler.tick(now=23.0) == []
    assert backend.prompts == []
    assert len(store.pending_wake_signals()) == 2

    reports = scheduler.tick(now=26.0)

    assert len(reports) == 1
    assert len(backend.prompts) == 1
    assert store.pending_wake_signals() == []
    assert reports[0].delivery_status == "sent"


def test_two_audit_findings_on_one_thread_share_one_receipted_model_turn(
    tmp_path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.conversation.models import BackgroundMainAgentReport

    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
        ),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "owner-a",
            "channel": "internal",
            "channel_conversation_id": "audit-findings-thread",
            "channel_user_id": "owner-a",
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-root",
            "goal": "ordinary-root-for-delivery-test",
            "status": "active",
        }
    )
    signals = []
    for index in (1, 2):
        signals.append(
            store.raise_wake_signal(
                {
                    "thread_id": thread.thread_id,
                    "reason": "audit_finding",
                    "root_task_id": "task-root",
                    "evidence_refs": [f"audit://watch-1/candidate/1:{index}"],
                    "metadata": {
                        "schema_version": "audit-finding-event.v1",
                        "audit_id": "task-root",
                        "watch_id": "watch-1",
                        "finding_id": f"af-{index}",
                        "revision": 1,
                        "requires_llm_report": True,
                        "delivery_evidence_refs": [f"audit://watch-1/candidate/1:{index}"],
                    },
                }
            )
        )
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=FakeDeliveryService(),
            ),
            "store": store,
        }
    )
    processed: list[dict[str, object]] = []

    def delivered(kwargs):
        wake = kwargs["wake_signal"]
        processed.append(wake.to_dict())
        return BackgroundMainAgentReport(
            thread_id=thread.thread_id,
            task_id="task-root",
            reason="audit_finding",
            response="sent",
            route_channel="internal",
            route_target="owner-a",
            created_at=time.time(),
            delivery_status="sent",
            delivery_reason="audit_finding_report",
            wake_handled=True,
        )

    monkeypatch.setattr(scheduler, "_run_claimed", delivered)

    assert len(scheduler.tick()) == 1
    assert len(processed) == 1
    assert processed[0]["wake_signal_id"] == signals[0].wake_signal_id
    assert processed[0]["evidence_refs"] == [
        "audit://watch-1/candidate/1:1",
        "audit://watch-1/candidate/1:2",
    ]
    assert processed[0]["metadata"]["finding_count"] == 2
    assert processed[0]["metadata"]["report_scope"] == "incremental"
    assert processed[0]["metadata"]["delivery_evidence_refs"] == [
        "audit://watch-1/candidate/1:1",
        "audit://watch-1/candidate/1:2",
    ]
    assert processed[0]["metadata"]["batched_wake_signal_ids"] == [
        signal.wake_signal_id for signal in signals
    ]
    assert store.pending_wake_signals() == []


def test_reported_audit_wake_ignores_supplementary_evidence_for_delivery_identity(
    tmp_path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.ingestion import harvester

    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
        ),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "owner-a",
            "channel": "internal",
            "channel_conversation_id": "audit-receipt-thread",
            "channel_user_id": "owner-a",
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-1",
            "goal": "持续研判",
            "status": "active",
            "work_kind": "audit",
        }
    )
    canonical = "audit://watch-1/candidate/1601:0"
    store.raise_wake_signal(
        {
            "thread_id": thread.thread_id,
            "reason": "audit_finding",
            "root_task_id": "audit-1",
            "evidence_refs": [canonical, "SOAK-C-000001600"],
            "metadata": {
                "schema_version": "audit-finding-event.v1",
                "audit_id": "audit-1",
                "watch_id": "watch-1",
                "finding_id": "af-mixed-evidence",
                "requires_llm_report": True,
                "delivery_evidence_refs": [canonical],
            },
        }
    )
    checked: list[tuple[str, ...]] = []

    def already_sent(_owner_home, refs):
        checked.append(tuple(refs))
        return tuple(refs) == (canonical,)

    monkeypatch.setattr(harvester, "audit_source_refs_reported", already_sent)
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=FakeDeliveryService(),
            ),
            "store": store,
        }
    )

    assert scheduler.tick() == []
    assert checked == [(canonical,)]
    assert store.pending_wake_signals() == []


def test_failed_audit_finding_batch_stays_pending_and_shares_retry_boundary(
    tmp_path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.conversation.models import BackgroundMainAgentReport

    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
        ),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "owner-a",
            "channel": "internal",
            "channel_conversation_id": "audit-findings-failed-thread",
            "channel_user_id": "owner-a",
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-1",
            "goal": "持续研判",
            "status": "active",
            "work_kind": "audit",
        }
    )
    signals = [
        store.raise_wake_signal(
            {
                "thread_id": thread.thread_id,
                "reason": "audit_finding",
                "root_task_id": "audit-1",
                "evidence_refs": [f"audit://watch-1/candidate/1:{index}"],
                "metadata": {
                    "schema_version": "audit-finding-event.v1",
                    "audit_id": "audit-1",
                    "run_epoch": 1,
                    "watch_id": "watch-1",
                    "finding_id": f"af-{index}",
                    "requires_llm_report": True,
                    "delivery_evidence_refs": [f"audit://watch-1/candidate/1:{index}"],
                },
            }
        )
        for index in (1, 2)
    ]
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=FakeDeliveryService(),
            ),
            "store": store,
        }
    )
    attempts: list[list[str]] = []

    def failed(kwargs):
        wake = kwargs["wake_signal"]
        attempts.append(list(wake.evidence_refs))
        return BackgroundMainAgentReport(
            thread_id=thread.thread_id,
            task_id="audit-1",
            reason="audit_finding",
            response="",
            route_channel="internal",
            route_target="owner-a",
            created_at=time.time(),
            delivery_status="failed",
            delivery_reason="audit_finding_report",
            wake_handled=False,
        )

    monkeypatch.setattr(scheduler, "_run_claimed", failed)
    current = time.time()

    assert scheduler.tick(now=current) == []
    assert attempts == [
        [
            "audit://watch-1/candidate/1:1",
            "audit://watch-1/candidate/1:2",
        ]
    ]
    assert [item.wake_signal_id for item in store.pending_wake_signals()] == [
        signal.wake_signal_id for signal in signals
    ]
    assert all(
        scheduler._wake_retry_after[signal.wake_signal_id] == current + 30.0 for signal in signals
    )

    assert scheduler.tick(now=current + 1.0) == []
    assert len(attempts) == 1


def test_audit_finding_batch_honors_existing_wake_projection_limit(
    tmp_path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.conversation.models import BackgroundMainAgentReport

    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
            background_pending_wake_prompt_limit=1,
        ),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "owner-a",
            "channel": "internal",
            "channel_conversation_id": "audit-findings-bounded-thread",
            "channel_user_id": "owner-a",
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-1",
            "goal": "持续研判",
            "status": "active",
            "work_kind": "audit",
        }
    )
    for index in (1, 2):
        store.raise_wake_signal(
            {
                "thread_id": thread.thread_id,
                "reason": "audit_finding",
                "root_task_id": "audit-1",
                "evidence_refs": [f"audit://watch-1/candidate/1:{index}"],
                "metadata": {
                    "schema_version": "audit-finding-event.v1",
                    "audit_id": "audit-1",
                    "run_epoch": 1,
                    "watch_id": "watch-1",
                    "finding_id": f"af-{index}",
                    "requires_llm_report": True,
                    "delivery_evidence_refs": [f"audit://watch-1/candidate/1:{index}"],
                },
            }
        )
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=FakeDeliveryService(),
            ),
            "store": store,
        }
    )
    processed: list[list[str]] = []

    def delivered(kwargs):
        wake = kwargs["wake_signal"]
        processed.append(list(wake.evidence_refs))
        return BackgroundMainAgentReport(
            thread_id=thread.thread_id,
            task_id="audit-1",
            reason="audit_finding",
            response="sent",
            route_channel="internal",
            route_target="owner-a",
            created_at=time.time(),
            delivery_status="sent",
            delivery_reason="audit_finding_report",
            wake_handled=True,
        )

    monkeypatch.setattr(scheduler, "_run_claimed", delivered)

    assert len(scheduler.tick(now=time.time())) == 1
    assert processed == [["audit://watch-1/candidate/1:1"]]
    assert len(store.pending_wake_signals()) == 1


def test_failed_subagent_completion_wake_is_not_delayed_by_success_coalescing(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
            background_completion_coalesce_seconds=30,
        ),
        tmp_path,
    )
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.bind_task(
        {"thread_id": thread.thread_id, "task_id": "task-root", "goal": "失败立即处理", "now": 11.0}
    )
    store.raise_wake_signal(
        {
            "thread_id": thread.thread_id,
            "reason": "subagent_runner_finished",
            "root_task_id": "task-root",
            "source_agent_id": "child-failed",
            "metadata": {"task_id": "child-failed", "status": "FAILED"},
            "now": 20.0,
        }
    )
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})

    reports = scheduler.tick(now=20.1)

    assert len(reports) == 1
    assert len(backend.prompts) == 1
    assert reports[0].delivery_reason == "subagent_non_success_terminal"


def test_audit_source_worker_lifecycle_is_supervised_without_owner_model_turn(
    tmp_path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.common.audit_activation import (
        audit_source_worker_key,
    )

    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
        ),
        tmp_path,
    )
    backend = _CapturingBackend()
    agent.backend = backend
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "user-1",
        }
    )
    audit_id = "audit-1"
    watch_id = "watch-1"
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": audit_id,
            "goal": "持续审计",
            "status": "active",
            "work_kind": "audit",
            "work_name": "安全审计",
        }
    )
    signal = store.raise_wake_signal(
        {
            "thread_id": thread.thread_id,
            "reason": "subagent_runner_finished",
            "root_task_id": audit_id,
            "source_agent_id": "source-worker-1",
            "metadata": {
                "task_id": "source-worker-1",
                "status": "DONE",
                "audit_source_worker": True,
                "audit_id": audit_id,
                "watch_id": watch_id,
                "worker_key": audit_source_worker_key(audit_id, watch_id),
            },
        }
    )
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=store,
        channels=FakeDeliveryService(),
    )
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    swept: list[str] = []
    completed: list[str] = []
    monkeypatch.setattr(
        scheduler,
        "_pre_wake_capability_sweep",
        lambda reason, _signal: swept.append(reason),
    )
    monkeypatch.setattr(
        "agent_py_agent.agent.conversation.task_promotion.complete_named_audit_task_if_settled",
        lambda _agent, task_id: completed.append(task_id) or True,
    )

    report = scheduler._run_wake_signal(signal, now=time.time())

    assert report is None
    assert swept == ["subagent_runner_finished"]
    assert completed == [audit_id]
    assert backend.prompts == []
    assert store.pending_wake_signals() == []
    assert store.recent_messages(thread.thread_id, limit=10) == []


def test_audit_source_worker_terminal_failure_is_supervisor_internal(tmp_path) -> None:
    from agent_py_agent.agent.common.audit_activation import audit_source_worker_key

    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
        ),
        tmp_path,
    )
    backend = _CapturingBackend()
    agent.backend = backend
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "chat-failed",
            "channel_user_id": "user-1",
        }
    )
    audit_id = "audit-failed"
    watch_id = "watch-failed"
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": audit_id,
            "goal": "持续审计",
            "status": "active",
            "work_kind": "audit",
            "work_name": "安全审计",
        }
    )
    store.raise_wake_signal(
        {
            "thread_id": thread.thread_id,
            "reason": "subagent_runner_finished",
            "root_task_id": audit_id,
            "source_agent_id": "source-worker-failed",
            "metadata": {
                "task_id": "source-worker-failed",
                "status": "FAILED",
                "failure_type": "unknown_error",
                "audit_source_worker": True,
                "audit_id": audit_id,
                "watch_id": watch_id,
                "worker_key": audit_source_worker_key(audit_id, watch_id),
            },
        }
    )
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=FakeDeliveryService(),
            ),
            "store": store,
        }
    )

    reports = scheduler.tick(now=time.time())

    assert reports == []
    assert backend.prompts == []
    assert store.pending_wake_signals() == []


def test_pending_audit_source_binding_failure_is_supervisor_internal(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
        ),
        tmp_path,
    )
    backend = _CapturingBackend()
    agent.backend = backend
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "chat-pending-failed",
            "channel_user_id": "user-1",
        }
    )
    audit_id = "audit-pending-failed"
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": audit_id,
            "goal": "持续审计",
            "status": "active",
            "work_kind": "audit",
            "work_name": "安全审计",
        }
    )
    store.raise_wake_signal(
        {
            "thread_id": thread.thread_id,
            "reason": "subagent_runner_finished",
            "root_task_id": audit_id,
            "source_agent_id": "source-binding-pending",
            "metadata": {
                "task_id": "source-binding-pending",
                "status": "BLOCKED",
                "failure_type": "status_blocked",
                "audit_source_worker": True,
                "audit_source_worker_phase": "binding_pending",
                "audit_id": audit_id,
                "source_id": "source-pending",
                "watch_id": "",
                "worker_key": "",
            },
        }
    )
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=FakeDeliveryService(),
            ),
            "store": store,
        }
    )

    assert scheduler.tick(now=time.time()) == []
    assert backend.prompts == []
    assert store.pending_wake_signals() == []


def test_legacy_pending_audit_source_wake_uses_durable_task_identity() -> None:
    from agent_py_agent.agent.common.audit_activation import (
        AUDIT_ATTR,
        AUDIT_SOURCE_BINDING_PENDING_ATTR,
    )
    from agent_py_agent.agent.conversation.runtime import (
        _is_internal_audit_source_worker_lifecycle_signal,
    )

    audit_id = "audit-legacy-pending"
    task = SimpleNamespace(
        attributes={
            AUDIT_ATTR: True,
            AUDIT_SOURCE_BINDING_PENDING_ATTR: True,
            CONVERSATION_REQUEST_ID_ATTR: audit_id,
        }
    )
    agent = SimpleNamespace(
        subagents=SimpleNamespace(
            load=lambda task_id: (
                task
                if task_id == "source-legacy-pending"
                else (_ for _ in ()).throw(KeyError(task_id))
            )
        )
    )
    signal = SimpleNamespace(
        reason="subagent_runner_finished",
        root_task_id=audit_id,
        source_agent_id="source-legacy-pending",
        metadata={
            "task_id": "source-legacy-pending",
            "status": "BLOCKED",
            "failure_type": "status_blocked",
            "audit_source_worker": True,
            "audit_id": audit_id,
            "watch_id": "",
            "worker_key": "",
        },
    )

    assert _is_internal_audit_source_worker_lifecycle_signal(signal, agent) is True


def test_audit_source_worker_provider_timeout_is_supervisor_internal(
    tmp_path,
) -> None:
    from agent_py_agent.agent.common.audit_activation import audit_source_worker_key
    from agent_py_agent.agent.subagents.models import FailureType

    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    backend = _CapturingBackend()
    agent.backend = backend
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "chat-provider-timeout",
            "channel_user_id": "user-1",
        }
    )
    audit_id = "audit-provider-timeout"
    watch_id = "watch-provider-timeout"
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": audit_id,
            "goal": "持续审计",
            "status": "active",
            "work_kind": "audit",
            "work_name": "安全审计",
        }
    )
    store.raise_wake_signal(
        {
            "thread_id": thread.thread_id,
            "reason": "subagent_runner_finished",
            "root_task_id": audit_id,
            "source_agent_id": "source-worker-timeout",
            "metadata": {
                "task_id": "source-worker-timeout",
                "status": "BLOCKED",
                "failure_type": FailureType.PROVIDER_TIMEOUT.value,
                "audit_source_worker": True,
                "audit_id": audit_id,
                "watch_id": watch_id,
                "worker_key": audit_source_worker_key(audit_id, watch_id),
            },
        }
    )
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=FakeDeliveryService(),
            ),
            "store": store,
        }
    )

    assert scheduler.tick(now=time.time()) == []
    assert backend.prompts == []
    assert store.pending_wake_signals() == []


def test_legacy_per_source_capacity_wake_is_retired_without_model_turn(
    tmp_path,
) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    backend = _CapturingBackend()
    agent.backend = backend
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "legacy-capacity",
            "channel_user_id": "user-1",
        }
    )
    audit_id = "audit-legacy-capacity"
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": audit_id,
            "goal": "持续审计",
            "status": "active",
            "work_kind": "audit",
            "work_name": "安全审计",
        }
    )
    store.raise_wake_signal(
        {
            "thread_id": thread.thread_id,
            "reason": "audit_capacity_alert",
            "root_task_id": audit_id,
            "metadata": {
                "schema_version": "audit-capacity-event.v1",
                "audit_id": audit_id,
                "watch_id": "watch-legacy",
                "capacity_state": "alert",
            },
        }
    )
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=FakeDeliveryService(),
            ),
            "store": store,
        }
    )

    assert scheduler.tick(now=time.time()) == []
    assert backend.prompts == []
    assert store.pending_wake_signals() == []


def test_aggregate_capacity_wake_runs_one_owner_model_turn(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    backend = _CapturingBackend()
    agent.backend = backend
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "aggregate-capacity",
            "channel_user_id": "open-id-capacity",
        }
    )
    audit_id = "audit-aggregate-capacity"
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": audit_id,
            "goal": "持续审计",
            "status": "active",
            "work_kind": "audit",
            "work_name": "安全审计",
        }
    )
    agent.subagents.create_run(
        goal="持续处理一个来源",
        thought="",
        plan=["继续"],
        parent_id=audit_id,
        root_id=audit_id,
    )
    store.append_message(
        {
            "thread_id": thread.thread_id,
            "role": "assistant",
            "content": "OLD_FALSE_NO_BACKLOG: 当前没有积压。",
            "channel": "feishu",
        }
    )
    store.raise_wake_signal(
        {
            "thread_id": thread.thread_id,
            "reason": "audit_capacity_alert",
            "root_task_id": audit_id,
            "metadata": {
                "schema_version": "audit-capacity-event.v2",
                "audit_id": audit_id,
                "run_epoch": 3,
                "capacity_state": "alert",
                "source_count": 10,
                "alert_source_count": 7,
                "pending": 8642,
                "oldest_pending_age_seconds": 123.0,
                "ingest_records_per_second": 50.0,
                "processing_throughput": {"records_per_second": 41.0},
                "processing_latency": {"p95_seconds": 80.0},
                "reasons": ["backlog_threshold"],
                "sources": [],
            },
        }
    )
    channels = FakeDeliveryService()
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=channels,
            ),
            "store": store,
        }
    )

    reports = scheduler.tick(now=time.time())

    assert len(reports) == 1
    assert len(backend.prompts) == 1
    assert "typed Audit capacity event" in backend.prompts[0]
    assert '"capacity_state": "alert"' in backend.prompts[0]
    assert '"pending": 8642' in backend.prompts[0]
    assert '"records_per_second": 41.0' in backend.prompts[0]
    assert "OLD_FALSE_NO_BACKLOG" not in backend.prompts[0]
    assert "## Recent Messages" not in backend.prompts[0]
    assert "## Agent Tree Snapshot" not in backend.prompts[0]
    assert reports[0].delivery_status == "sent"
    assert len(channels.adapter("feishu").sent_messages) == 1


def test_capacity_wake_channel_failure_stays_retryable_and_out_of_transcript(
    tmp_path,
) -> None:
    class _RejectedDelivery:
        def supports_proactive(self, channel: str) -> bool:
            return channel == "feishu"

        def deliver(self, context, envelope):
            del envelope
            return SimpleNamespace(
                delivery_status="rejected",
                channel=context.channel,
                evidence_refs=(),
                receipt_id="",
            )

    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    agent.backend = _CapturingBackend()
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "aggregate-capacity",
            "channel_user_id": "open-id-capacity",
        }
    )
    audit_id = "audit-aggregate-capacity"
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": audit_id,
            "goal": "持续审计",
            "status": "active",
            "work_kind": "audit",
            "work_name": "安全审计",
        }
    )
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=store,
        channels=_RejectedDelivery(),
    )

    report = runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": audit_id,
            "reason": "audit_capacity_alert",
            "route_channel": "feishu",
            "route_target": "open-id-capacity",
            "wake_signal": {
                "wake_signal_id": "wake-capacity-rejected",
                "root_task_id": audit_id,
                "metadata": {
                    "schema_version": "audit-capacity-event.v2",
                    "audit_id": audit_id,
                    "capacity_state": "alert",
                    "pending": 8642,
                },
            },
        }
    )

    assert report.delivery_status == "rejected"
    assert report.wake_handled is False
    assert store.recent_messages(thread.thread_id) == []


def test_capacity_wake_retry_reuses_frozen_reply_without_second_model_turn(
    tmp_path,
) -> None:
    class _RejectedDelivery:
        def supports_proactive(self, channel: str) -> bool:
            return channel == "feishu"

        def deliver(self, context, envelope):
            del envelope
            return SimpleNamespace(
                delivery_status="rejected",
                channel=context.channel,
                evidence_refs=(),
                receipt_id="",
            )

    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
        ),
        tmp_path,
    )
    backend = _CapturingBackend()
    agent.backend = backend
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "aggregate-capacity",
            "channel_user_id": "open-id-capacity",
            "now": 1.0,
        }
    )
    audit_id = "audit-aggregate-capacity"
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": audit_id,
            "goal": "持续审计",
            "status": "active",
            "work_kind": "audit",
            "work_name": "安全审计",
            "now": 2.0,
        }
    )
    agent.subagents.create_run(
        goal="持续处理一个来源",
        thought="",
        plan=["继续"],
        parent_id=audit_id,
        root_id=audit_id,
    )
    observation, signal = store.append_observation_with_wake(
        {
            "thread_id": thread.thread_id,
            "event_type": "audit_capacity_alert",
            "summary": "容量已超过阈值",
            "requires_main_agent": True,
            "root_task_id": audit_id,
            "now": 3.0,
        },
        {
            "thread_id": thread.thread_id,
            "reason": "audit_capacity_alert",
            "root_task_id": audit_id,
            "metadata": {
                "schema_version": "audit-capacity-event.v2",
                "audit_id": audit_id,
                "capacity_state": "alert",
                "pending": 8642,
            },
            "now": 3.0,
        },
    )
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=_RejectedDelivery(),
            ),
            "store": store,
        }
    )

    assert scheduler.tick(now=20.0) == []
    assert len(backend.prompts) == 1
    cached = store.pending_wake_signal(signal.wake_signal_id)
    assert cached is not None
    assert cached.metadata["owner_delivery"]["schema_version"] == ("wake-owner-delivery.v1")
    assert scheduler.tick(now=51.0) == []
    assert len(backend.prompts) == 1
    assert store.pending_wake_signal(signal.wake_signal_id) is not None
    assert [
        item.observation_id for item in store.unhandled_observations_requiring_main(limit=10)
    ] == [observation.observation_id]
    assert store.recent_messages(thread.thread_id) == []


def test_audit_source_worker_quota_wakes_owner_model_and_is_delivered(tmp_path) -> None:
    from agent_py_agent.agent.common.audit_activation import audit_source_worker_key
    from agent_py_agent.agent.subagents.models import FailureType

    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    backend = _CapturingBackend()
    agent.backend = backend
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "open-id-1",
        }
    )
    audit_id = "audit-quota"
    watch_id = "watch-quota"
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": audit_id,
            "goal": "持续审计",
            "status": "active",
            "work_kind": "audit",
            "work_name": "安全审计",
        }
    )
    store.raise_wake_signal(
        {
            "thread_id": thread.thread_id,
            "reason": "subagent_runner_finished",
            "root_task_id": audit_id,
            "source_agent_id": "source-worker-1",
            "metadata": {
                "task_id": "source-worker-1",
                "status": "BLOCKED",
                "failure_type": FailureType.PROVIDER_QUOTA_EXHAUSTED.value,
                "audit_source_worker": True,
                "audit_id": audit_id,
                "watch_id": watch_id,
                "worker_key": audit_source_worker_key(audit_id, watch_id),
            },
        }
    )
    channels = FakeDeliveryService()
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=channels,
            ),
            "store": store,
        }
    )

    reports = scheduler.tick(now=time.time())

    assert len(reports) == 1
    assert len(backend.prompts) == 1
    assert "exhausted its usable account or plan quota" in backend.prompts[0]
    assert reports[0].delivery_status == "sent"
    assert len(channels.adapter("feishu").sent_messages) == 1


def test_background_internal_status_is_not_saved_as_ordinary_chat(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    agent.backend = _InternalStatusBackend()
    store = ConversationStore(tmp_path / "conversations")
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "open-id-1",
            "now": 10.0,
        }
    )

    report = runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "reason": "scheduled_progress_report",
            "route_channel": "feishu",
            "route_target": "chat-1",
            "now": 20.0,
        }
    )

    assert report.response == ""
    assert report.delivery_status == "suppressed"
    assert store.recent_messages(thread.thread_id, limit=1) == []
    assert channels.adapter("feishu").sent_messages == []


def test_scheduler_records_bad_progress_policy_without_blocking_due_policy(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})

    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.set_progress_policy(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "interval_seconds": 60,
            "route_channel": "internal",
            "route_target": "thread-1",
            "now": 13.0,
        }
    )
    bad_path = store.policies_dir / "broken.json"
    bad_path.write_text("[]", encoding="utf-8")

    reports = scheduler.tick(now=73.0)

    assert len(reports) == 1
    assert scheduler.last_progress_policy_load_errors
    assert (
        scheduler.last_progress_policy_load_errors[0]["context"]
        == "conversation.progress_policy.read"
    )
    assert scheduler.last_progress_policy_load_errors[0]["policy_id"] == "broken"
    assert channels.adapter("internal").sent_messages


def test_scheduler_retires_stale_missed_progress_policy_without_model_call(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "goal": "陈年提醒退休不复活",
            "now": 11.0,
        }
    )
    policy = store.set_progress_policy(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "interval_seconds": 60,
            "route_channel": "internal",
            "route_target": "thread-1",
            "now": 12.0,
        }
    )

    reports = scheduler.tick(now=12.0 + 7200 + 61)

    assert reports == []
    assert backend.prompts == []
    assert scheduler.last_progress_policy_suppressed[0]["policy_id"] == policy.policy_id
    assert scheduler.last_progress_policy_suppressed[0]["reason"] == "stale_missed_interval"
    # 早已超出 catchup 宽限(>2h)的 stale 策略应被退休(enabled=False),不再续命。
    # 旧行为 mark_progress_reported 把 next_due 重置成 now+interval,下个间隔又变 runnable 发 LLM
    # 进度汇报,无限 churn 占满 gateway worker。退休=从 due 扫描里彻底消失。
    retired = store.get_progress_policy(policy.policy_id)
    assert retired is not None and retired.enabled is False


def test_only_explicit_goal_progress_keeps_background_continuation_chain(tmp_path) -> None:
    from agent_py_agent.agent.agent_core.runtime.owner_roots import runtime_owner_root
    from agent_py_agent.agent.conversation.runtime import (
        _ensure_goal_progress_wake_chain,
        ledger_open_progress_item_count,
    )
    from agent_py_agent.agent.task_progress import write_task_progress

    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=store,
        channels=FakeDeliveryService(),
    )
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-plain-items",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-plain-items",
            "goal": "完成普通任务清单",
            "now": 11.0,
        }
    )
    write_task_progress(
        runtime_owner_root(agent),
        "task-plain-items",
        {
            "items": [
                {"id": "read", "title": "读源码", "status": "done"},
                {"id": "report", "title": "写报告", "status": "pending"},
            ]
        },
    )
    signal = store.raise_wake_signal(
        {
            "thread_id": thread.thread_id,
            "reason": "subagent_runner_finished",
            "root_task_id": "task-plain-items",
            "source_agent_id": "child-1",
            "now": 12.0,
        }
    )

    assert ledger_open_progress_item_count(agent, "task-plain-items") == 1
    _ensure_goal_progress_wake_chain(scheduler, signal, now=13.0)

    assert store.list_progress_policies(enabled_only=True) == []
    store.create_goal(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-plain-items",
            "objective": "完成显式持续目标清单",
            "now": 13.5,
        }
    )
    _ensure_goal_progress_wake_chain(scheduler, signal, now=14.0)

    policies = store.list_progress_policies(enabled_only=True)
    assert len(policies) == 1
    assert policies[0].task_id == "task-plain-items"
    assert policies[0].metadata["tool"] == "goal_progress_continuation"
    write_task_progress(
        runtime_owner_root(agent),
        "task-plain-items",
        {"items": [{"id": "report", "status": "done"}]},
    )
    assert ledger_open_progress_item_count(agent, "task-plain-items") == 0


def test_scheduler_renews_stale_policy_while_coverage_open(tmp_path) -> None:
    """g8 问题B·stale 不杀活任务:任务清单还有未闭环项时,错过追赶窗(唤醒轮长期领不到
    claim/网关中断)只把排期推进到下一 interval 继续追,不许永久退休——账没对完唤醒链不许死。
    无清单的 stale(上一测试)仍照旧退休,churn 防护不变。"""
    from agent_py_agent.agent.agent_core.runtime.owner_roots import runtime_owner_root
    from agent_py_agent.agent.task_progress import write_task_progress

    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "goal": "活任务的续推提醒",
            "now": 11.0,
        }
    )
    policy = store.set_progress_policy(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "interval_seconds": 60,
            "route_channel": "internal",
            "route_target": "thread-1",
            "now": 12.0,
        }
    )
    write_task_progress(
        runtime_owner_root(agent),
        "task-1",
        {"coverage": {"targets": [{"id": "req-01", "title": "模块1", "status": "pending"}]}},
    )
    stale_now = 12.0 + 7200 + 61

    reports = scheduler.tick(now=stale_now)

    assert reports == []
    assert backend.prompts == []
    assert scheduler.last_progress_policy_suppressed[0]["reason"] == "stale_missed_interval"
    renewed = store.get_progress_policy(policy.policy_id)
    assert renewed is not None and renewed.enabled is True, "清单未闭环的 stale 提醒只续命不退休"
    assert renewed.next_due_at > stale_now, "排期推进到下一 interval,下轮照常追"


@pytest.mark.parametrize("terminal_status", ["DONE", "completed", "superseded"])
def test_scheduler_retires_terminal_task_progress_policy_without_model_call(
    tmp_path, terminal_status
) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "goal": "终态任务退休watch",
            "now": 11.0,
        }
    )
    policy = store.set_progress_policy(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "interval_seconds": 60,
            "route_channel": "internal",
            "route_target": "thread-1",
            "now": 12.0,
        }
    )
    store.update_task_status({"task_id": "task-1", "status": terminal_status, "now": 70.0})

    reports = scheduler.tick(now=100.0)

    assert reports == []
    assert backend.prompts == []
    assert scheduler.last_progress_policy_suppressed[0]["policy_id"] == policy.policy_id
    assert scheduler.last_progress_policy_suppressed[0]["reason"] == "terminal_task_link"
    # 被观察任务已终态时，watch 策略应退休(enabled=False),不再每个间隔唤醒后台主代理发
    # LLM 进度汇报(churn 根因)。这里 now=100 未到 stale 窗口,确保抑制原因是终态而非陈旧。
    retired = store.get_progress_policy(policy.policy_id)
    assert retired is not None and retired.enabled is False


def test_terminal_child_watch_policy_is_not_revived_by_owner_backlog(tmp_path) -> None:
    # Child-bound policy belongs to the removed fixed watch route. Pending input
    # is now recovered by an exact root-task policy, so a terminal child link
    # must not be revived merely because the owner has unrelated backlog.
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.runtime import _runnable_due_policies
    from agent_py_agent.agent.ingestion.watch_state import new_state, persist_state, state_dir

    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "conv:run-w",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.bind_task(
        {"thread_id": thread.thread_id, "task_id": "run-w", "goal": "盯守", "now": 11.0}
    )
    policy = store.set_progress_policy(
        {
            "thread_id": thread.thread_id,
            "task_id": "run-w",
            "interval_seconds": 60,
            "route_channel": "internal",
            "route_target": "",
            "metadata": {
                "kind": "subagent_progress_watch",
                "tool": "wait",
                "watch_run_id": "run-w",
            },
            "now": 12.0,
        }
    )
    store.update_task_status({"task_id": "run-w", "status": "DONE", "now": 70.0})
    owner_home = tmp_path / "owner"
    lane = new_state(owner_home, "http://127.0.0.1:9/pull", {"watch_window_seconds": 600})
    lane.opened_at = time.time() - 900.0  # 窗口已走完
    lane.totals["spool_candidates"] = 7  # 已抬 7 条、无人 ack = 未清账
    persist_state(lane)
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=str(owner_home)))

    runnable, suppressed = _runnable_due_policies(store, [policy], now=time.time(), agent=agent)
    assert runnable == []
    assert [reason for _p, reason in suppressed] == ["terminal_task_link"]

    # Clearing the backlog does not change the terminal-link decision.
    sidecar = state_dir(owner_home) / f"{lane.watch_id}.read.json"
    sidecar.write_text(
        json.dumps(
            {
                "read_seq": 9,
                "candidates_consumed": 7,
                "candidates_acked": 7,
                "updated_at": time.time(),
            }
        ),
        encoding="utf-8",
    )
    runnable2, suppressed2 = _runnable_due_policies(store, [policy], now=time.time(), agent=agent)
    assert runnable2 == []
    assert [reason for _p, reason in suppressed2] == ["terminal_task_link"]


def test_scheduler_retires_legacy_child_bound_watch_backstop(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    backend = _CapturingBackend()
    agent.backend = backend
    child = agent.subagents.create_run(goal="判读一路数据")
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=store,
        channels=FakeDeliveryService(),
    )
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-child-watch",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": child.id,
            "goal": "判读一路数据",
            "now": 11.0,
        }
    )
    policy = store.set_progress_policy(
        {
            "thread_id": thread.thread_id,
            "task_id": child.id,
            "interval_seconds": 60,
            "route_channel": "internal",
            "route_target": "",
            "metadata": {
                "kind": "subagent_progress_watch",
                "tool": "watch_backlog_backstop",
                "watch_run_id": child.id,
            },
            "now": 12.0,
        }
    )

    reports = scheduler.tick(now=100.0)

    assert reports == []
    assert backend.prompts == []
    assert scheduler.last_progress_policy_suppressed == [
        {
            "policy_id": policy.policy_id,
            "thread_id": thread.thread_id,
            "task_id": child.id,
            "reason": "child_watch_backstop_policy",
        }
    ]
    retired = store.get_progress_policy(policy.policy_id)
    assert retired is not None and retired.enabled is False


def test_scheduler_retires_legacy_audit_root_poll_without_model_turn(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=store,
        channels=FakeDeliveryService(),
    )
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-audit-root",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-1",
            "goal": "持续审计",
            "status": "active",
            "work_kind": "audit",
            "work_name": "audit-one",
            "cancellation_scope": "detached",
            "now": 11.0,
        }
    )
    policy = store.set_progress_policy(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-1",
            "interval_seconds": 60,
            "route_channel": "internal",
            "route_target": "",
            "metadata": {
                "kind": "named_work_progress",
                "tool": "audit_durable_backstop",
                "scope": "exact_named_task",
            },
            "now": 12.0,
        }
    )

    reports = scheduler.tick(now=100.0)

    assert reports == []
    assert backend.prompts == []
    assert scheduler.last_progress_policy_suppressed == [
        {
            "policy_id": policy.policy_id,
            "thread_id": thread.thread_id,
            "task_id": "audit-1",
            "reason": "audit_root_poll_policy",
        }
    ]
    retired = store.get_progress_policy(policy.policy_id)
    assert retired is not None and retired.enabled is False


def test_scheduler_retires_running_durable_audit_root_wait_without_model_turn(
    tmp_path,
) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(
                agent=agent,
                store=store,
                channels=FakeDeliveryService(),
            ),
            "store": store,
        }
    )
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-audit-root-wait",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-running",
            "goal": "opaque objective",
            "status": "active",
            "work_kind": "audit",
            "work_name": "audit-one",
            "duration_seconds": 600,
            "expires_at": 611.0,
            "cancellation_scope": "detached",
            "effective_source_bindings": [{"source_id": "source-1", "profile_ref": "profile-1"}],
            "run_epoch": 1,
            "now": 11.0,
        }
    )
    policy = store.set_progress_policy(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-running",
            "interval_seconds": 60,
            "route_channel": "internal",
            "route_target": "",
            "metadata": {
                "kind": "subagent_progress_watch",
                "tool": "wait",
                "watch_run_id": "audit-running",
            },
            "now": 12.0,
        }
    )

    assert scheduler.tick(now=100.0) == []
    assert backend.prompts == []
    assert scheduler.last_progress_policy_suppressed == [
        {
            "policy_id": policy.policy_id,
            "thread_id": thread.thread_id,
            "task_id": "audit-running",
            "reason": "durable_audit_root_policy",
        }
    ]
    retired = store.get_progress_policy(policy.policy_id)
    assert retired is not None and retired.enabled is False


def test_due_policy_backs_off_on_no_progress_rounds_and_recovers(tmp_path) -> None:
    # §6-B4 退避钉子:唤醒轮【零成功物质变更】(卡死空转,真机=BLOCKED 子代理让主代理每分钟
    # 醒来空转解阻、饿死并发建站用户)→ 间隔按 2^streak 拉长、封顶 8×,让出调度资源但永不
    # 停机;一有成功写入/调度立即归零复原。只读成功不算推进，判据不看模型文本。
    from agent_py_agent.agent.conversation.models import BackgroundMainAgentReport

    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 0.0,
        }
    )
    policy = store.set_progress_policy(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "interval_seconds": 60,
            "route_channel": "internal",
            "route_target": "thread-1",
            "now": 0.0,
        }
    )

    class _FakeRuntime:
        agent = None

        def __init__(self) -> None:
            self.tool_success_count = 0
            self.material_progress_count = 0

        def run_once(self, params: dict) -> BackgroundMainAgentReport:
            return BackgroundMainAgentReport(
                thread_id=str(params.get("thread_id") or ""),
                task_id=str(params.get("task_id") or ""),
                reason=str(params.get("reason") or ""),
                response="轮次完成",
                route_channel="internal",
                route_target="thread-1",
                created_at=float(params.get("now") or 0.0),
                tool_call_count=2,
                tool_success_count=self.tool_success_count,
                material_progress_count=self.material_progress_count,
            )

    runtime = _FakeRuntime()
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})

    # 第 1 轮无进展:streak=1 → 间隔 60 → 120
    scheduler.tick(now=61.0)
    after_first = store.get_progress_policy(policy.policy_id)
    assert after_first.metadata["no_progress_streak"] == 1
    assert after_first.next_due_at == 61.0 + 120

    # 第 2 轮无进展:streak=2 → ×4
    scheduler.tick(now=after_first.next_due_at + 1)
    after_second = store.get_progress_policy(policy.policy_id)
    assert after_second.metadata["no_progress_streak"] == 2
    assert after_second.next_due_at == after_first.next_due_at + 1 + 240

    # 连续无进展只封顶不停机:streak 再涨,倍数封在 8×
    scheduler.tick(now=after_second.next_due_at + 1)
    scheduler.tick(now=store.get_progress_policy(policy.policy_id).next_due_at + 1)
    capped = store.get_progress_policy(policy.policy_id)
    assert capped.metadata["no_progress_streak"] == 4
    assert capped.next_due_at == capped.last_report_at + 480  # 60 × 8 封顶
    assert capped.enabled is True  # 退避≠退休

    # 只有只读成功仍要退避；不能靠重复 read/list 冒充推进。
    runtime.tool_success_count = 1
    scheduler.tick(now=capped.next_due_at + 1)
    readonly = store.get_progress_policy(policy.policy_id)
    assert readonly.metadata["no_progress_streak"] == 5

    # 有成功物质变更 → streak 归零、间隔复原
    runtime.material_progress_count = 1
    scheduler.tick(now=readonly.next_due_at + 1)
    recovered = store.get_progress_policy(policy.policy_id)
    assert recovered.metadata["no_progress_streak"] == 0
    assert recovered.next_due_at == recovered.last_report_at + 60


def test_scheduler_runs_one_duplicate_progress_policy_per_target(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "goal": "重复提醒只跑一次",
            "now": 11.0,
        }
    )
    store.set_progress_policy(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "interval_seconds": 60,
            "route_channel": "internal",
            "route_target": "thread-1",
            "now": 12.0,
        }
    )
    store.set_progress_policy(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "interval_seconds": 60,
            "route_channel": "internal",
            "route_target": "thread-1",
            "now": 13.0,
        }
    )

    reports = scheduler.tick(now=73.0)

    assert len(reports) == 1
    assert len(backend.prompts) == 1
    assert [item["reason"] for item in scheduler.last_progress_policy_suppressed] == [
        "duplicate_policy"
    ]


def test_urgent_wake_uses_full_background_tool_profile(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "title": "紧急事件",
            "now": 10.0,
        }
    )

    runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "reason": "urgent_wake_signal",
            "wake_signal": {
                "wake_signal_id": "wake-1",
                "thread_id": thread.thread_id,
                "urgency": "urgent",
                "summary": "需要主代理马上处理。",
            },
            "now": 20.0,
        }
    )
    prompt = backend.prompts[0]

    assert "create_subagents" in prompt
    assert "dispatch_subagents" not in prompt


def test_background_runtime_uses_configured_allowed_tools(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            background_main_agent_allowed_tools=["inspect_agent_tree", "send_guidance"],
        ),
        tmp_path,
    )
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "title": "长期后台任务",
            "now": 10.0,
        }
    )
    store.bind_task(
        {"thread_id": thread.thread_id, "task_id": "task-1", "goal": "只读看树并提醒", "now": 12.0}
    )
    store.set_progress_policy(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "interval_seconds": 60,
            "route_channel": "internal",
            "route_target": "thread-1",
            "now": 13.0,
        }
    )

    scheduler.tick(now=73.0)
    prompt = backend.prompts[0]

    assert "inspect_agent_tree" in prompt
    assert "send_guidance" in prompt
    assert "dispatch_subagents" not in prompt
    assert "create_subagents" not in prompt


def test_background_runtime_applies_owner_disabled_tools(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    agent.owner_policy = type(
        "OwnerPolicy", (), {"disabled_tools": ("create_subagents", "dispatch_subagents")}
    )()
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "title": "紧急事件",
            "now": 10.0,
        }
    )

    runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "reason": "urgent_wake_signal",
            "wake_signal": {"urgency": "urgent", "summary": "需要处理。"},
            "now": 20.0,
        }
    )
    prompt = backend.prompts[0]

    assert "create_subagents: 创建" not in prompt
    assert "dispatch_subagents: 推进" not in prompt
    assert "removed_tools" in prompt


def test_background_runtime_applies_wake_policy_snapshot(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "title": "策略快照",
            "now": 10.0,
        }
    )

    runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "reason": "urgent_wake_signal",
            "wake_signal": {
                "urgency": "urgent",
                "summary": "只允许观察。",
                "policy_snapshot": {"allowed_tools": ["inspect_agent_tree"]},
            },
            "now": 20.0,
        }
    )
    prompt = backend.prompts[0]

    assert "inspect_agent_tree" in prompt
    assert "dispatch_subagents: 只有需要推进" not in prompt
    assert "create_subagents: 创建" not in prompt


def test_background_context_budget_truncates_large_messages(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "title": "长上下文后台任务",
            "now": 10.0,
        }
    )
    long_message = "A" * 12000
    store.append_message(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": long_message,
            "channel": "internal",
            "metadata": {"gateway_request_id": "task-1"},
            "now": 11.0,
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "goal": "检查长上下文裁剪",
            "now": 12.0,
        }
    )
    store.set_progress_policy(
        {"thread_id": thread.thread_id, "task_id": "task-1", "interval_seconds": 60, "now": 13.0}
    )

    reports = scheduler.tick(now=73.0)
    prompt = backend.prompts[0]

    assert len(reports) == 1
    assert "A" * 2000 not in prompt
    assert "truncated" in prompt


def test_scheduler_recovers_due_policy_after_process_restart(tmp_path) -> None:
    first_store = ConversationStore(tmp_path / "conversations")
    thread = first_store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "open-id-1",
            "now": 100.0,
        }
    )
    first_store.append_message(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": "一小时后继续检查。",
            "channel": "feishu",
            "metadata": {"gateway_request_id": "task-1"},
            "now": 101.0,
        }
    )
    first_store.bind_task(
        {"thread_id": thread.thread_id, "task_id": "task-1", "goal": "重启后继续", "now": 102.0}
    )
    first_store.set_progress_policy(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "interval_seconds": 3600,
            "route_channel": "feishu",
            "route_target": "chat-1",
            "now": 103.0,
        }
    )

    restarted_agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path
    )
    backend = _CapturingBackend()
    restarted_agent.backend = backend
    restarted_store = ConversationStore(tmp_path / "conversations")
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(
        agent=restarted_agent,
        store=restarted_store,
        channels=channels,
    )
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": restarted_store})

    reports = scheduler.tick(now=3703.0)

    assert len(reports) == 1
    assert "一小时后继续检查" in backend.prompts[0]
    assert channels.adapter("feishu").sent_messages[0].target == "chat-1"


def test_scheduler_skips_thread_with_active_background_claim(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 1.0,
        }
    )
    store.bind_task(
        {"thread_id": thread.thread_id, "task_id": "task-1", "goal": "避免重复唤醒", "now": 2.0}
    )
    store.set_progress_policy(
        {"thread_id": thread.thread_id, "task_id": "task-1", "interval_seconds": 60, "now": 3.0}
    )
    claim = store.claim_background_run(
        {
            "thread_id": thread.thread_id,
            "reason": "already_running",
            "lease_seconds": 300,
            "now": 63.0,
        }
    )

    reports = scheduler.tick(now=64.0)

    assert claim is not None
    assert reports == []
    assert backend.prompts == []


def test_scheduler_renews_background_claim_while_runtime_is_still_running(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    agent.backend = _SlowBackend(sleep_seconds=1.2)
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": runtime,
            "store": store,
            "claim_ttl_seconds": 1,
            "claim_heartbeat_interval_seconds": 0.2,
        }
    )
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 1.0,
        }
    )
    store.bind_task(
        {"thread_id": thread.thread_id, "task_id": "task-1", "goal": "长后台运行要续租", "now": 2.0}
    )
    store.set_progress_policy(
        {"thread_id": thread.thread_id, "task_id": "task-1", "interval_seconds": 1, "now": 3.0}
    )

    reports = scheduler.tick(now=4.0)

    assert reports[0].response == "后台主代理慢速检查完成。"
    claim_path = store.background_claims_dir / f"{thread.thread_id}.json"
    claim = claim_path.read_text(encoding="utf-8")
    assert '"status": "finished"' in claim
    assert '"heartbeat_at": 4.0' not in claim


def test_scheduler_default_heartbeat_interval_stays_below_small_ttl(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())

    scheduler = BackgroundMainAgentScheduler(
        {"runtime": runtime, "store": store, "claim_ttl_seconds": 9}
    )

    assert scheduler.claim_heartbeat_interval_seconds == 3.0


def test_detached_task_claim_does_not_occupy_foreground_thread_lane(tmp_path) -> None:
    from agent_py_agent.agent.conversation.run_claim import (
        detached_task_claim_scope_id,
    )
    from agent_py_agent.agent.conversation.runtime import _background_claim_scope_id

    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 1.0,
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-1",
            "goal": "持续检查来源",
            "status": "active",
            "work_kind": "audit",
            "work_name": "安全巡检",
            "duration_seconds": 600,
            "cancellation_scope": "detached",
            "now": 1.0,
        }
    )
    scope_id = _background_claim_scope_id(
        store,
        thread.thread_id,
        "audit-1",
    )
    assert scope_id == detached_task_claim_scope_id(thread.thread_id, "audit-1")

    task_claim = store.claim_background_run(
        {
            "thread_id": thread.thread_id,
            "claim_scope_id": scope_id,
            "task_id": "audit-1",
            "reason": "audit_progress",
            "lease_seconds": 90,
            "now": 2.0,
        }
    )
    foreground_claim = store.claim_background_run(
        {
            "thread_id": thread.thread_id,
            "task_id": "foreground-1",
            "reason": "gateway_foreground_turn",
            "lease_seconds": 90,
            "now": 2.0,
        }
    )

    assert task_claim is not None
    assert foreground_claim is not None
    assert (
        store.claim_background_run(
            {
                "thread_id": thread.thread_id,
                "claim_scope_id": scope_id,
                "task_id": "audit-1",
                "reason": "duplicate_audit_turn",
                "lease_seconds": 90,
                "now": 3.0,
            }
        )
        is None
    )
    assert (
        store.load_background_run_claim(thread.thread_id)["claim_id"]
        == foreground_claim["claim_id"]
    )
    assert (
        store.load_background_run_claim(
            thread.thread_id,
            claim_scope_id=scope_id,
        )["claim_id"]
        == task_claim["claim_id"]
    )


def test_background_claim_immediately_takes_over_dead_same_host_owner(tmp_path) -> None:
    from agent_py_agent.agent.gateway_parts.daemon_metadata import process_host_id

    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 1.0,
        }
    )
    first = store.claim_background_run(
        {"thread_id": thread.thread_id, "reason": "first", "lease_seconds": 900, "now": 2.0}
    )
    assert first is not None
    claim_path = store.background_claims_dir / f"{thread.thread_id}.json"
    payload = json.loads(claim_path.read_text(encoding="utf-8"))
    payload["owner_process"] = {
        "host_id": process_host_id(),
        "pid": 999_999_999,
        "start_time": 1,
    }
    claim_path.write_text(json.dumps(payload), encoding="utf-8")

    second = store.claim_background_run(
        {"thread_id": thread.thread_id, "reason": "recovery", "lease_seconds": 90, "now": 3.0}
    )

    assert second is not None
    assert second["claim_id"] != first["claim_id"]
    assert second["acquisition"]["reason"] == "owner_process_stale"
    assert second["previous_claim"]["expired"] is False


def test_background_claim_legacy_owner_waits_for_ttl_instead_of_guessing(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 1.0,
        }
    )
    first = store.claim_background_run(
        {"thread_id": thread.thread_id, "reason": "first", "lease_seconds": 90, "now": 2.0}
    )
    assert first is not None
    claim_path = store.background_claims_dir / f"{thread.thread_id}.json"
    payload = json.loads(claim_path.read_text(encoding="utf-8"))
    payload.pop("owner_process", None)
    claim_path.write_text(json.dumps(payload), encoding="utf-8")

    assert (
        store.claim_background_run(
            {"thread_id": thread.thread_id, "reason": "recovery", "lease_seconds": 90, "now": 3.0}
        )
        is None
    )


def test_background_claim_different_process_domain_waits_for_ttl(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 1.0,
        }
    )
    first = store.claim_background_run(
        {"thread_id": thread.thread_id, "reason": "first", "lease_seconds": 90, "now": 2.0}
    )
    assert first is not None
    claim_path = store.background_claims_dir / f"{thread.thread_id}.json"
    payload = json.loads(claim_path.read_text(encoding="utf-8"))
    payload["owner_process"] = {"host_id": "another-process-domain", "pid": 999_999_999}
    claim_path.write_text(json.dumps(payload), encoding="utf-8")

    assert (
        store.claim_background_run(
            {"thread_id": thread.thread_id, "reason": "recovery", "lease_seconds": 90, "now": 3.0}
        )
        is None
    )


def test_scheduler_marks_background_claim_failed_when_runtime_raises(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    agent.backend = _FailingBackend()
    agent._current_tool = "web_fetch"
    agent._last_progress_summary = "正在核对来源"
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler(
        {"runtime": runtime, "store": store, "claim_ttl_seconds": 30}
    )
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 1.0,
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "goal": "失败时留下可接手事实",
            "now": 2.0,
        }
    )
    store.set_progress_policy(
        {"thread_id": thread.thread_id, "task_id": "task-1", "interval_seconds": 1, "now": 3.0}
    )

    try:
        scheduler.tick(now=4.0)
    except RuntimeError:
        pass

    claim = json.loads(
        (store.background_claims_dir / f"{thread.thread_id}.json").read_text(encoding="utf-8")
    )
    assert claim["status"] == "failed"
    assert claim["task_id"] == "task-1"
    assert claim["last_error"]["type"] == "RuntimeError"
    assert "backend boom" in claim["last_error"]["message"]
    assert claim["takeover"]["allowed"] is True
    assert claim["takeover"]["reason"] == "runtime_failed"
    assert claim["phase"] == "failed"
    assert claim["last_runtime_facts"]["current_tool"] == "web_fetch"
    assert claim["last_runtime_facts"]["last_progress_summary"] == "正在核对来源"
    assert "tree_status_buckets" in claim["last_runtime_facts"]


def test_background_prompt_includes_recovery_snapshot_for_previous_failed_claim(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _CapturingBackend()
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 1.0,
        }
    )
    store.bind_task(
        {"thread_id": thread.thread_id, "task_id": "task-1", "goal": "接手时先对账", "now": 2.0}
    )
    failed = store.claim_background_run(
        {"thread_id": thread.thread_id, "reason": "previous_run", "lease_seconds": 10, "now": 3.0}
    )
    assert failed is not None
    store.finish_background_run(
        {
            "thread_id": thread.thread_id,
            "claim_id": failed["claim_id"],
            "status": "failed",
            "task_id": "task-1",
            "error": {"type": "RuntimeError", "message": "previous run crashed"},
            "now": 4.0,
        }
    )
    store.set_progress_policy(
        {"thread_id": thread.thread_id, "task_id": "task-1", "interval_seconds": 1, "now": 5.0}
    )

    scheduler.tick(now=7.0)
    prompt = backend.prompts[0]

    assert "Recovery Snapshot" in prompt
    assert '"previous_claim_status": "failed"' in prompt
    assert (
        '"takeover_advice": "接手前先核对 claim、任务树和产物登记；不要把模型文本里的完成声明当成事实。"'
        in prompt
    )


def test_background_claim_unknown_finish_status_is_explicit_protocol_error(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 1.0,
        }
    )
    claim = store.claim_background_run(
        {"thread_id": thread.thread_id, "reason": "unknown_status", "lease_seconds": 10, "now": 2.0}
    )
    assert claim is not None

    finished = store.finish_background_run(
        {
            "thread_id": thread.thread_id,
            "claim_id": claim["claim_id"],
            "status": "succeeded",
            "now": 3.0,
        }
    )

    assert finished is not None
    assert finished["status"] == "invalid_status"
    assert finished["takeover"] == {"allowed": True, "reason": "runtime_invalid_status"}
    assert finished["last_error"]["type"] == "InvalidBackgroundClaimStatus"


def test_cancelled_background_claim_is_not_recovery_takeover_candidate(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 1.0,
        }
    )
    claim = store.claim_background_run(
        {"thread_id": thread.thread_id, "reason": "user_work", "lease_seconds": 10, "now": 2.0}
    )
    assert claim is not None

    finished = store.finish_background_run(
        {
            "thread_id": thread.thread_id,
            "claim_id": claim["claim_id"],
            "status": "cancelled",
            "now": 3.0,
        }
    )

    assert finished is not None
    assert finished["takeover"] == {"allowed": False, "reason": "user_interrupted"}


# ── 后台 claim 心跳:线程缺失不得裸崩 daemon 线程(修多 owner ticking 下 KeyError 崩心跳) ──


def test_renew_background_run_claim_present_thread_still_renews(tmp_path) -> None:
    """行为保持:线程在时续租照常成功、写入新的 heartbeat_at/expires_at。"""
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "u1",
            "channel": "internal",
            "channel_conversation_id": "c1",
            "channel_user_id": "u1",
            "now": 1.0,
        }
    )
    claim = store.claim_background_run(
        {"thread_id": thread.thread_id, "reason": "wake_signal", "lease_seconds": 30, "now": 2.0}
    )
    assert claim is not None

    renewed = store.renew_background_run_claim(
        {
            "thread_id": thread.thread_id,
            "claim_id": claim["claim_id"],
            "lease_seconds": 30,
            "now": 5.0,
        }
    )

    assert renewed is not None
    assert renewed["heartbeat_at"] == 5.0
    assert renewed["expires_at"] == 35.0


def test_renew_background_run_claim_missing_thread_returns_none_not_keyerror(tmp_path) -> None:
    """根因修:线程文件在长跑中消失(边缘/竞态)时,续租返回 None 让心跳优雅停机,绝不抛 KeyError。"""
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "u1",
            "channel": "internal",
            "channel_conversation_id": "c1",
            "channel_user_id": "u1",
            "now": 1.0,
        }
    )
    claim = store.claim_background_run(
        {"thread_id": thread.thread_id, "reason": "wake_signal", "lease_seconds": 30, "now": 2.0}
    )
    assert claim is not None
    # 模拟真机现象:claim 成功后线程文件不再可读(store 根竞态/外部清理/长跑中消失)。
    (store.threads_dir / f"{thread.thread_id}.json").unlink()

    renewed = store.renew_background_run_claim(
        {
            "thread_id": thread.thread_id,
            "claim_id": claim["claim_id"],
            "lease_seconds": 30,
            "now": 5.0,
        }
    )

    assert renewed is None  # 修前:此处抛 KeyError('unknown conversation thread') 崩心跳线程


def test_finish_background_run_missing_thread_finalizes_claim_without_crash(tmp_path) -> None:
    """收尾在 _run_with_heartbeat 的 finally 跑:线程缺失也要能释放已存在的 claim 租约,绝不二次抛 KeyError。"""
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "u1",
            "channel": "internal",
            "channel_conversation_id": "c1",
            "channel_user_id": "u1",
            "now": 1.0,
        }
    )
    claim = store.claim_background_run(
        {"thread_id": thread.thread_id, "reason": "wake_signal", "lease_seconds": 30, "now": 2.0}
    )
    assert claim is not None
    (store.threads_dir / f"{thread.thread_id}.json").unlink()

    finished = store.finish_background_run(
        {
            "thread_id": thread.thread_id,
            "claim_id": claim["claim_id"],
            "status": "finished",
            "now": 5.0,
        }
    )

    assert finished is not None
    assert finished["status"] == "finished"


def test_finish_background_run_no_claim_file_returns_none_without_crash(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    assert (
        store.finish_background_run(
            {"thread_id": "thread-never", "claim_id": "x", "status": "finished", "now": 1.0}
        )
        is None
    )


def test_background_claim_heartbeat_stops_gracefully_when_renew_raises() -> None:
    """防御纵深:renew 抛任何异常时,daemon 心跳线程记账后优雅停机,不把未捕获异常抛出杀线程。"""
    import threading as _threading

    from agent_py_agent.agent.conversation.run_claim import ConversationRunClaimHeartbeat

    class _RaisingStore:
        def renew_background_run_claim(self, request: dict):
            raise KeyError("unknown conversation thread: thread-boom")

    heartbeat = ConversationRunClaimHeartbeat(
        {
            "store": _RaisingStore(),
            "thread_id": "thread-boom",
            "claim_id": "c1",
            "lease_seconds": 1,
            "interval_seconds": 0.05,
        }
    )
    uncaught: list[type] = []
    previous_hook = _threading.excepthook
    _threading.excepthook = lambda args: uncaught.append(args.exc_type)
    try:
        heartbeat.start()
        time.sleep(0.3)
        heartbeat.join(timeout=2.0)
    finally:
        _threading.excepthook = previous_hook

    assert not heartbeat.is_alive()  # 线程已优雅退出
    assert uncaught == []  # 没有未捕获异常杀线程(修前:KeyError 裸崩 "Exception in thread")


def test_policy_failure_backoff_is_deterministic_exponential_and_bounded() -> None:
    """失败退避纯函数:5min×2^(n-1) 上限 1h,抖动 0.90~1.10 按 policy_id 确定性派生。

    同一 policy 每次失败同值(可复现、可精确断言);不同 policy 抖动错峰(防齐醒)。
    """
    from agent_py_agent.agent.conversation.runtime import _policy_failure_backoff

    def base_seconds(n: int) -> float:
        return min(300 * (2 ** (n - 1)), 3600)

    def ratio(failures: int, policy_id: str) -> float:
        return _policy_failure_backoff(failures, policy_id) / base_seconds(failures)

    assert _policy_failure_backoff(1, "p-a") == _policy_failure_backoff(1, "p-a")
    for n in (1, 2, 3, 4, 5, 8, 9, 20):
        assert base_seconds(n) <= 3600  # 封顶 1h,永不超
        for pid in ("p-a", "p-b", "p-c"):
            assert 0.9 <= ratio(n, pid) <= 1.1  # 抖动区间
    assert base_seconds(1) == 300
    assert base_seconds(2) == 600
    assert base_seconds(4) == 2400
    assert base_seconds(5) == 3600
    assert base_seconds(20) == 3600


def test_failed_policy_run_records_backoff_and_retires_after_three(tmp_path) -> None:
    """问题6:失败 run 后 policy 记账(退避顺延),连续 3 次失败退休,绝不无限重试。

    修前:失败异常被 _consume_with_supply_guard 吸收 → policy.next_due_at 不动 →
    下个 tick 又 due = 无限重试。修后:失败落账 failure_count/last_failure_at,
    next_due_at 退避顺延;第 3 次失败 → enabled=False 退休,离开 due 扫描等用户。
    """
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
        _policy_failure_backoff,
    )

    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            enable_tools=False,
            my_agent_home=str(tmp_path / "home"),
        ),
        tmp_path,
    )
    agent.backend = _FailingBackend()
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-fail-policy",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    task_root = tmp_path / "home" / "tasks" / "task-fail-policy"
    (task_root / "output").mkdir(parents=True)
    (task_root / "work").mkdir()
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-fail-policy",
            "goal": "失败续跑记账",
            "status": "active",
            "task_path": str(task_root),
        }
    )
    policy = store.set_progress_policy(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-fail-policy",
            "interval_seconds": 30,
            "route_channel": "internal",
            "route_target": thread.thread_id,
            "now": 20.0,
        }
    )
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=store,
        channels=FakeDeliveryService(),
    )
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})

    # 第 1、2 次失败:记账 + 退避顺延,policy 仍 enabled。
    for attempt, tick in ((1, 30.0), (2, 40.0)):
        with pytest.raises(RuntimeError, match="backend boom"):
            scheduler._run_due_policy(policy, now=tick)
        after = store.get_progress_policy(policy.policy_id)
        assert after is not None and after.enabled is True
        assert after.metadata["failure_count"] == attempt
        assert after.metadata["last_failure_at"] == tick
        expected_backoff = _policy_failure_backoff(attempt, policy.policy_id)
        assert after.metadata["last_backoff_seconds"] == expected_backoff
        assert after.next_due_at == tick + expected_backoff
        assert after.next_due_at > tick + 30  # 比原 interval 退避更长(1 次=5min 基数)

    # 第 3 次失败:退休(enabled=False),离开 due 扫描,账目保留供复盘。
    with pytest.raises(RuntimeError, match="backend boom"):
        scheduler._run_due_policy(policy, now=50.0)
    retired = store.get_progress_policy(policy.policy_id)
    assert retired is not None
    assert retired.enabled is False
    assert retired.metadata["failure_count"] == 3
    assert retired.metadata["retired_at"] == 50.0
    assert retired.policy_id not in {
        p.policy_id for p in store.due_progress_policies(now=60.0)
    }


def test_successful_policy_run_resets_failure_accounting(tmp_path) -> None:
    """问题6成功半边:policy run 成功 → failure_count 清零,退避账复原。

    先造 1 次失败账,再换能成功的 backend 跑一轮 → metadata.failure_count==0,
    排期回到正常 interval(不再带旧失败历史减速)。
    """
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
    )

    agent = SimpleAgent(
        AgentConfig(model_backend="echo", enable_tools=False, my_agent_home=str(tmp_path / "home")),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-reset-policy",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    task_root = tmp_path / "home" / "tasks" / "task-reset-policy"
    (task_root / "output").mkdir(parents=True)
    (task_root / "work").mkdir()
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-reset-policy",
            "goal": "成功清零失败账",
            "status": "active",
            "task_path": str(task_root),
        }
    )
    policy = store.set_progress_policy(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-reset-policy",
            "interval_seconds": 30,
            "route_channel": "internal",
            "route_target": thread.thread_id,
            "now": 20.0,
        }
    )
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=store,
        channels=FakeDeliveryService(),
    )
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})

    agent.backend = _FailingBackend()
    with pytest.raises(RuntimeError, match="backend boom"):
        scheduler._run_due_policy(policy, now=30.0)
    assert store.get_progress_policy(policy.policy_id).metadata["failure_count"] == 1

    agent.backend = _CapturingBackend()
    report = scheduler._run_due_policy(policy, now=40.0)
    assert report is not None  # run 成功
    after = store.get_progress_policy(policy.policy_id)
    assert after.metadata["failure_count"] == 0  # 失败账清零复原
    assert after.enabled is True
    # 排期只受既有「无进展退避」(echo backend 无工具调用 → streak=1 → interval×2)
    # 影响,失败账已归零不带退避;两种账独立:streak 管无进展,失败账管连续失败。
    assert after.metadata["no_progress_streak"] == 1
    assert after.next_due_at == 40.0 + 30 * 2


def test_supply_failure_does_not_record_policy_failure_accounting(tmp_path, monkeypatch) -> None:
    """429/限流等供应类错误不记 policy 失败账(走 supply backoff 专属退避)。

    C 批失败记账若把 429 也计 failure_count,连续 3 次 429 就把 policy 退休
    (错杀:额度恢复后应照常排期)。判据只认 typed provider error。
    """
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
    )

    agent = SimpleAgent(
        AgentConfig(model_backend="echo", enable_tools=False, my_agent_home=str(tmp_path / "home")),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-supply-fail",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    task_root = tmp_path / "home" / "tasks" / "task-supply-fail"
    (task_root / "output").mkdir(parents=True)
    (task_root / "work").mkdir()
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-supply-fail",
            "goal": "429 不算任务失败",
            "status": "active",
            "task_path": str(task_root),
        }
    )
    policy = store.set_progress_policy(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-supply-fail",
            "interval_seconds": 30,
            "route_channel": "internal",
            "route_target": thread.thread_id,
            "now": 20.0,
        }
    )
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=store,
        channels=FakeDeliveryService(),
    )
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})

    # 直接让 run_once 抛 429(绕过 run_once 内部 provider auto_resume 重试环,
    # 测试目标=finally 的供应错误排除分支,与重试环无关)。
    def quota_boom(_request):
        raise ProviderUsageLimitError("HTTP 429: 已达到 Token Plan 用量上限")

    monkeypatch.setattr(runtime, "run_once", quota_boom)
    with pytest.raises(ProviderUsageLimitError):
        scheduler._run_due_policy(policy, now=30.0)

    after = store.get_progress_policy(policy.policy_id)
    assert after is not None
    assert after.enabled is True  # 不退休
    assert after.metadata.get("failure_count") in (None, 0)  # 不记失败账
    assert after.metadata == {}  # 连失败账字段都没写:供应错误完全不碰 policy
    assert after.next_due_at == 50.0  # 初始排期(20+30)原样,无失败退避
