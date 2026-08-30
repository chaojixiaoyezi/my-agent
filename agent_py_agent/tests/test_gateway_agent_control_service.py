from __future__ import annotations

import threading
import time

import pytest

from agent_py_agent.agent.contracts.tool_approval import (
    ToolApprovalDecision,
    build_tool_approval_request,
)
from agent_py_agent.agent.conversation.agent_control import (
    AgentControlError,
    enqueue_agent_stop,
    read_agent_view,
    resolve_agent_permission,
    send_agent_guidance,
    stop_agent,
)
from agent_py_agent.agent.conversation.agent_tool_approval import (
    list_pending_subagent_tool_approvals,
    publish_subagent_tool_approval,
    wait_for_subagent_tool_approval,
)
from agent_py_agent.agent.conversation.background_transcript import (
    BackgroundTranscriptSink,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.http_handlers import (
    _agent_control_context,
    read_gateway_client_notices,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall
from agent_py_agent.cli.chat_client_context import GatewayChatClientAgent


def _bound_agent_tree(tmp_path):
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            gateway_per_user_owner_scoping=False,
        ),
        tmp_path / "workspace",
    )
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "local-agent",
            "channel": "chat",
            "channel_conversation_id": "session-a",
            "channel_user_id": "local-agent",
        }
    )
    root_id = "task-root"
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": root_id,
            "goal": "主任务",
            "status": "active",
            "task_path": str(tmp_path / "workspace" / "task-root"),
            "now": time.time(),
        }
    )
    store.select_workspace_task(
        {
            "thread_id": thread.thread_id,
            "task_id": root_id,
            "now": time.time(),
        }
    )
    child = agent.subagents.create_run(
        goal="完成子任务",
        root_id=root_id,
        parent_id=root_id,
    )
    child = agent.subagents.lifecycle.prepare_runner_attempt(child.id)
    scope = GatewayControlScope("local-agent", "chat", "session-a")
    return agent, scope, child


def test_agent_control_http_context_uses_exact_scoped_owner(tmp_path) -> None:
    base = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            gateway_per_user_owner_scoping=True,
        ),
        tmp_path / "base",
    )

    class Handler:
        _auth_middleware = None

    owner_agent, scope = _agent_control_context(
        Handler(),
        type("Server", (), {"agent": base})(),
        {
            "user_id": "alice",
            "channel": "tui-test",
            "conversation_id": "session-alice",
        },
    )

    assert owner_agent is not base
    assert owner_agent.home_paths.owner_id == "providers/tui-test/users/alice"
    assert scope.user_id == "alice"
    assert scope.channel == "tui-test"
    assert scope.conversation_id == "session-alice"
    assert base.home_paths.owner_id != owner_agent.home_paths.owner_id


def _child_approval_request(run_id: str, request_id: str = "attempt-child"):
    return build_tool_approval_request(
        ToolCall(
            call_id=f"call-{request_id}",
            tool_name="controlled_exec",
            arguments={"apply": True, "command": ["python3", "-V"]},
            source_protocol="native",
            schema_hash="sha256:child-approval",
            run_id=run_id,
            turn_id=request_id,
            attempt_id=request_id,
        ),
        request_id=request_id,
        round_number=1,
        call_index=1,
        description="controlled_exec(python3 -V)",
    )


def test_owner_can_view_steer_stop_and_reopen_terminal_child(tmp_path) -> None:
    agent, scope, child = _bound_agent_tree(tmp_path)
    grandchild = agent.subagents.create_run(
        goal="完成孙任务",
        root_id="task-root",
        parent_id=child.id,
    )
    grandchild = agent.subagents.lifecycle.prepare_runner_attempt(grandchild.id)

    view = read_agent_view(agent, scope=scope, run_id=child.id)
    assert view["ok"] is True
    assert view["agent"]["run_id"] == child.id
    assert view["terminal"] is False

    accepted = send_agent_guidance(
        agent,
        scope=scope,
        run_id=child.id,
        message="请先运行完整测试",
        message_id="agent-steer-1",
    )
    replay = send_agent_guidance(
        agent,
        scope=scope,
        run_id=child.id,
        message="请先运行完整测试",
        message_id="agent-steer-1",
    )
    assert accepted["delivery"] == "queued"
    assert accepted["status"] == "pending"
    assert accepted["operation_id"] == "agent-steer-1"
    assert accepted["expected_turn_id"]
    assert accepted["guidance_id"] == replay["guidance_id"]
    assert replay["delivery"] == "queued"
    assert replay["status"] == "pending"
    entries = agent.conversation_store.recent_guidance("agent_run", child.id)
    assert len(entries) == 1
    entry = entries[0]
    expected_turn_id = str(entry.metadata["expected_turn_id"])
    assert expected_turn_id.startswith("attempt-")
    assert agent.conversation_store.claim_guidance_once_for_turn(
        entry,
        expected_turn_id=expected_turn_id,
        attempt_id=expected_turn_id,
    ) is True
    assert agent.conversation_store.mark_guidance_entries_submitted(
        expected_turn_id,
        [entry],
        attempt_id=expected_turn_id,
    ) == (entry.guidance_id,)

    stopped = stop_agent(
        agent,
        scope=scope,
        run_id=child.id,
        operation_id="agent-stop-1",
    )
    assert stopped["status"] == "cancelled"
    assert agent.subagents.load(grandchild.id).status == "CANCELLED"
    terminal_view = read_agent_view(agent, scope=scope, run_id=child.id)
    assert terminal_view["terminal"] is True
    replayed_stop = stop_agent(
        agent,
        scope=scope,
        run_id=child.id,
        operation_id="agent-stop-1",
    )
    assert replayed_stop["status"] == "already_terminal"
    with pytest.raises(AgentControlError) as exc_info:
        send_agent_guidance(
            agent,
            scope=scope,
            run_id=child.id,
            message="继续",
            message_id="agent-steer-2",
        )
    assert exc_info.value.status == 409
    assert exc_info.value.error_code == "AGENT_ALREADY_TERMINAL"


def test_interactive_stop_acknowledges_before_slow_canonical_closeout(
    tmp_path, monkeypatch
) -> None:
    agent, scope, child = _bound_agent_tree(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def slow_cancel(_agent, request):
        calls.append(request.task.id)
        entered.set()
        release.wait(timeout=2.0)
        return {"run_id": request.task.id, "status": "CANCELLED"}

    monkeypatch.setattr(
        "agent_py_agent.agent.conversation.agent_control.cancel_subagent_tree",
        slow_cancel,
    )

    started = time.perf_counter()
    accepted = enqueue_agent_stop(
        agent,
        scope=scope,
        run_id=child.id,
        operation_id="agent-stop-fast-1",
    )
    elapsed = time.perf_counter() - started
    assert accepted["status"] == "accepted"
    assert elapsed < 0.5
    assert entered.wait(timeout=1.0)

    duplicate = enqueue_agent_stop(
        agent,
        scope=scope,
        run_id=child.id,
        operation_id="agent-stop-fast-2",
    )
    assert duplicate["status"] == "accepted"
    assert duplicate["already_active"] is True
    assert calls == [child.id]
    release.set()


def test_interactive_stop_reconciles_grandchild_created_inside_inflight_guard(
    tmp_path,
) -> None:
    """详情页停止先打断目标，再在创建 guard 后复读并关闭迟到孙代理。"""
    agent, scope, child = _bound_agent_tree(tmp_path)

    with agent.subagents.creation_guard():
        accepted = enqueue_agent_stop(
            agent,
            scope=scope,
            run_id=child.id,
            operation_id="agent-stop-late-grandchild",
        )
        late_grandchild = agent.subagents.create_run(
            goal="模拟在途派工落盘的孙任务",
            root_id="task-root",
            parent_id=child.id,
        )
        late_grandchild = agent.subagents.lifecycle.prepare_runner_attempt(
            late_grandchild.id
        )

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if (
            agent.subagents.load(child.id).status == "CANCELLED"
            and agent.subagents.load(late_grandchild.id).status == "CANCELLED"
        ):
            break
        time.sleep(0.01)

    assert accepted["status"] == "accepted"
    assert agent.subagents.load(child.id).status == "CANCELLED"
    assert agent.subagents.load(late_grandchild.id).status == "CANCELLED"


def test_agent_guidance_rejects_nonterminal_task_without_active_attempt(tmp_path) -> None:
    agent, scope, child = _bound_agent_tree(tmp_path)
    repo = agent.subagents.runtime_db
    agent_run = repo.agent_run_for_run_id(child.id)
    assert agent_run is not None
    attempt = repo.current_attempt(str(agent_run["agent_run_id"]))
    assert attempt is not None
    settled = repo.settle_agent_attempt(
        agent_run_id=str(agent_run["agent_run_id"]),
        attempt_id=str(attempt["attempt_id"]),
    )
    assert settled["settled"] is True

    with pytest.raises(AgentControlError) as exc_info:
        send_agent_guidance(
            agent,
            scope=scope,
            run_id=child.id,
            message="继续检查",
            message_id="agent-steer-no-turn",
        )

    assert exc_info.value.error_code == "AGENT_NOT_RUNNING"
    assert agent.conversation_store.recent_guidance("agent_run", child.id) == []


def test_agent_guidance_rejects_runtime_attempt_before_task_projection_commits(tmp_path) -> None:
    agent, scope, child = _bound_agent_tree(tmp_path)
    projected = agent.subagents.load(child.id)
    projected.runner_active_attempt_id = ""
    agent.subagents.save(projected)

    with pytest.raises(AgentControlError) as exc_info:
        send_agent_guidance(
            agent,
            scope=scope,
            run_id=child.id,
            message="恢复临界区不要误绑",
            message_id="agent-steer-transition",
        )

    assert exc_info.value.error_code == "AGENT_NOT_RUNNING"
    assert agent.conversation_store.recent_guidance("agent_run", child.id) == []


def test_agent_control_rejects_run_outside_conversation_root(tmp_path) -> None:
    agent, scope, _child = _bound_agent_tree(tmp_path)
    outside = agent.subagents.create_run(
        goal="其它任务",
        root_id="task-other",
        parent_id="task-other",
    )

    with pytest.raises(AgentControlError) as exc_info:
        read_agent_view(agent, scope=scope, run_id=outside.id)
    assert exc_info.value.status == 403
    assert exc_info.value.error_code == "AGENT_OUTSIDE_CONVERSATION"


def test_thin_client_agent_requests_keep_local_conversation_identity() -> None:
    client = object.__new__(GatewayChatClientAgent)
    calls: list[tuple[str, dict[str, object], float]] = []

    def post(path: str, payload: dict[str, object], *, timeout: float):
        calls.append((path, payload, timeout))
        return 200, {"ok": True}

    client.post_gateway_json = post
    client.request_background_notices("session-a", after=0.0)
    client.request_agent_view("session-a", run_id="child-a")
    client.request_agent_guidance(
        "session-a",
        run_id="child-a",
        message="继续",
        message_id="agent-steer-1",
    )
    request = _child_approval_request("child-a")
    client.request_agent_permission(
        "session-a",
        run_id="child-a",
        request=request.to_dict(),
        decision=ToolApprovalDecision(
            request.permission_id,
            "approved",
        ).to_dict(),
    )
    client.request_agent_stop(
        "session-a",
        run_id="child-a",
        operation_id="agent-stop-1",
    )

    assert [path for path, _payload, _timeout in calls] == [
        "/client/notices",
        "/client/agent-view",
        "/client/agent-guidance",
        "/client/agent-permission",
        "/client/agent-stop",
    ]
    assert all(payload["user_id"] == "local-agent" for _path, payload, _ in calls)
    assert all(payload["channel"] == "chat" for _path, payload, _ in calls)
    assert all(payload["conversation_id"] == "session-a" for _path, payload, _ in calls)
    assert calls[0][1]["client_capabilities"] == {"tool_approval": True}
    assert calls[-1][2] == 10.0


def test_owner_tui_decision_resumes_exact_child_approval(tmp_path) -> None:
    agent, scope, child = _bound_agent_tree(tmp_path)
    request = _child_approval_request(child.id)
    initial = read_gateway_client_notices(
        agent,
        scope=scope,
        after=0.0,
        interactive_approvals=True,
    )
    assert initial["agent_permission_requests"] == []
    sink = BackgroundTranscriptSink(
        agent,
        thread_id=child.agent_thread_id,
        task_id=child.id,
        request_id="child-approval-transcript",
        event_writer=lambda *_args, **_kwargs: None,
    )
    result: dict[str, object] = {}

    def wait() -> None:
        result.update(sink.request_permission(request.to_dict()))

    thread = threading.Thread(target=wait)
    thread.start()
    pending: list[dict[str, object]] = []
    deadline = time.monotonic() + 1.0
    while not pending and time.monotonic() < deadline:
        pending = list_pending_subagent_tool_approvals(
            agent,
            root_task_id=child.root_id,
        )
        if not pending:
            time.sleep(0.01)
    assert [row["run_id"] for row in pending] == [child.id]
    noninteractive = read_gateway_client_notices(
        agent,
        scope=scope,
        after=0.0,
    )
    assert noninteractive["agent_permission_requests"] == []
    projected = read_gateway_client_notices(
        agent,
        scope=scope,
        after=0.0,
        interactive_approvals=True,
    )
    assert projected["agent_permission_requests"][0]["request"] == request.to_dict()
    decision = ToolApprovalDecision(
        request.permission_id,
        "approved",
        "只执行这一次",
    )
    written = resolve_agent_permission(
        agent,
        scope=scope,
        run_id=child.id,
        request=request.to_dict(),
        decision=decision.to_dict(),
    )
    thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert written["ok"] is True
    assert result["decision"] == "approved"
    assert result["feedback"] == "只执行这一次"
    assert list_pending_subagent_tool_approvals(
        agent,
        root_task_id=child.root_id,
    ) == []


def test_child_approval_without_interactive_consumer_fails_closed(tmp_path) -> None:
    agent, _scope, child = _bound_agent_tree(tmp_path)
    request = _child_approval_request(child.id, "attempt-no-consumer")
    handle = publish_subagent_tool_approval(
        agent,
        run_id=child.id,
        thread_id=child.agent_thread_id,
        request_value=request,
    )

    decision = wait_for_subagent_tool_approval(
        handle,
        poll_seconds=0.01,
        discovery_seconds=0.01,
        consumer_lease_seconds=0.01,
    )

    assert decision.decision == "unavailable"
    assert list_pending_subagent_tool_approvals(
        agent,
        root_task_id=child.root_id,
    ) == []


def test_child_session_approval_reuses_same_sink_and_args(tmp_path) -> None:
    agent, scope, child = _bound_agent_tree(tmp_path)
    read_gateway_client_notices(
        agent,
        scope=scope,
        after=0.0,
        interactive_approvals=True,
    )
    sink = BackgroundTranscriptSink(
        agent,
        thread_id=child.agent_thread_id,
        task_id=child.id,
        request_id="child-session-approval",
        event_writer=lambda *_args, **_kwargs: None,
    )
    first = _child_approval_request(child.id, "attempt-session-first")
    first_result: dict[str, object] = {}
    thread = threading.Thread(
        target=lambda: first_result.update(sink.request_permission(first.to_dict()))
    )
    thread.start()
    pending: list[dict[str, object]] = []
    deadline = time.monotonic() + 1.0
    while not pending and time.monotonic() < deadline:
        pending = list_pending_subagent_tool_approvals(
            agent,
            root_task_id=child.root_id,
        )
        if not pending:
            time.sleep(0.01)
    assert pending

    resolve_agent_permission(
        agent,
        scope=scope,
        run_id=child.id,
        request=first.to_dict(),
        decision=ToolApprovalDecision(
            first.permission_id,
            "approved_session",
        ).to_dict(),
    )
    thread.join(timeout=1.0)
    second = _child_approval_request(child.id, "attempt-session-second")
    second_result = sink.request_permission(second.to_dict())

    assert not thread.is_alive()
    assert first_result["decision"] == "approved_session"
    assert second_result["decision"] == "approved"
    assert list_pending_subagent_tool_approvals(
        agent,
        root_task_id=child.root_id,
    ) == []
