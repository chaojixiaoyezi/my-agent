from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

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
    list_pending_agent_tool_approvals,
    publish_agent_tool_approval,
    wait_for_agent_tool_approval,
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
from agent_py_agent.agent.subagents.direct_parent_lifecycle import (
    mark_parent_waiting_for_direct_children,
    parent_wait_blocks_dispatch,
)
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
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "local-agent",
            "channel": "chat",
            "channel_conversation_id": "session-a",
            "channel_user_id": "local-agent",
        }
    )
    root_id = "task-root"
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": root_id,
            "goal": "主任务",
            "status": "active",
            "task_path": str(tmp_path / "workspace" / "task-root"),
            "now": time.time(),
        }
    )
    store.tasks.select_workspace_task(
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


def _park_child_waiting_for_grandchild(agent, child):
    grandchild = agent.subagents.create_run(
        goal="继续处理分片",
        root_id="task-root",
        parent_id=child.id,
    )
    agent.subagents.add_child(child.id, grandchild.id)
    repo = agent.subagents.runtime_db
    agent_run = repo.agent_run_for_run_id(child.id)
    assert agent_run is not None
    current = repo.current_attempt(str(agent_run["agent_run_id"]))
    assert current is not None
    assert repo.settle_agent_attempt(
        agent_run_id=str(agent_run["agent_run_id"]),
        attempt_id=str(current["attempt_id"]),
    )["settled"] is True
    waiting = agent.subagents.load(child.id)
    waiting.status = "PENDING"
    waiting.runner_active_attempt_id = ""
    agent.subagents.save(waiting)
    assert mark_parent_waiting_for_direct_children(
        agent.subagents,
        child.id,
        [grandchild.id],
    ) == (grandchild.id,)
    return grandchild, agent_run


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
    entries = agent.conversation_store.guidance.recent("agent_run", child.id)
    assert len(entries) == 1
    entry = entries[0]
    expected_turn_id = str(entry.metadata["expected_turn_id"])
    assert expected_turn_id.startswith("attempt-")
    assert entry.metadata["record_in_transcript"] is True
    assert entry.metadata["thread_id"] == child.agent_thread_id
    assert entry.metadata["agent_run_id"] == child.id
    assert agent.conversation_store.guidance.claim_for_turn(
        entry,
        expected_turn_id=expected_turn_id,
        attempt_id=expected_turn_id,
    ) is True
    assert agent.conversation_store.guidance.submissions.mark_submitted(
        expected_turn_id,
        [entry],
        attempt_id=expected_turn_id,
    ) == (entry.guidance_id,)
    assert agent.conversation_store.guidance.acknowledgements.consume_submitted(
        expected_turn_id,
        [entry],
    ) == (entry.guidance_id,)
    child_messages = agent.conversation_store.messages.recent(
        child.agent_thread_id,
        limit=10,
    )
    assert child_messages[-1].role == "user"
    assert child_messages[-1].content == "请先运行完整测试"
    assert child_messages[-1].metadata["agent_run_id"] == child.id

    root_thread = agent.conversation_store.tasks.thread_for("task-root")
    assert root_thread is not None
    agent.conversation_store.tasks.bind(
        {
            "thread_id": root_thread.thread_id,
            "task_id": child.id,
            "goal": child.goal,
            "status": "active",
            "now": time.time(),
        }
    )

    stopped = stop_agent(
        agent,
        scope=scope,
        run_id=child.id,
        operation_id="agent-stop-1",
    )
    assert stopped["status"] == "cancelled"
    assert agent.subagents.load(grandchild.id).status == "CANCELLED"
    child_authority = agent.subagents.runtime_db.agent_run_for_run_id(child.id)
    grandchild_authority = agent.subagents.runtime_db.agent_run_for_run_id(
        grandchild.id
    )
    assert child_authority is not None
    assert grandchild_authority is not None
    assert child_authority["status"] == "cancelled"
    assert grandchild_authority["status"] == "cancelled"
    assert stopped["result"]["parent_delivery"]["status"] == "delivered"
    signals = agent.conversation_store.wakes.pending(limit=0)
    assert len(signals) == 1
    assert signals[0].reason == "subagent_runner_finished"
    assert signals[0].source_agent_id == child.id
    assert signals[0].metadata["status"] == "CANCELLED"
    terminal_view = read_agent_view(agent, scope=scope, run_id=child.id)
    assert terminal_view["terminal"] is True
    replayed_stop = stop_agent(
        agent,
        scope=scope,
        run_id=child.id,
        operation_id="agent-stop-1",
    )
    assert replayed_stop["status"] == "preserved"
    delivered_replay = send_agent_guidance(
        agent,
        scope=scope,
        run_id=child.id,
        message="请先运行完整测试",
        message_id="agent-steer-1",
    )
    assert delivered_replay["status"] == "consumed"
    assert delivered_replay["replayed"] is True
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


def test_owner_stop_grandchild_immediately_resumes_waiting_direct_parent(
    tmp_path,
    monkeypatch,
) -> None:
    agent, scope, parent = _bound_agent_tree(tmp_path)
    grandchild, _agent_run = _park_child_waiting_for_grandchild(agent, parent)
    started: list[str] = []

    def record_start(_agent, run_id: str):
        started.append(run_id)
        return {"status": "started", "run_ids": [run_id]}

    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.orchestration.dispatch.capability_auto_sweep.auto_start_orphan_run",
        record_start,
    )

    stopped = stop_agent(
        agent,
        scope=scope,
        run_id=grandchild.id,
        operation_id="agent-stop-grandchild-1",
    )

    assert stopped["status"] == "cancelled"
    assert agent.subagents.load(grandchild.id).status == "CANCELLED"
    assert parent_wait_blocks_dispatch(agent.subagents.load(parent.id)) is False
    assert started == [parent.id]
    delivery = stopped["result"]["parent_delivery"]
    assert delivery["status"] == "delivered"
    assert delivery["parent_run_id"] == parent.id
    assert delivery["resume_requested"] is True
    assert agent.conversation_store.wakes.pending(limit=0) == []


def test_owner_stop_grandchild_resumes_parent_while_sibling_keeps_running(
    tmp_path,
    monkeypatch,
) -> None:
    """用户停止一个孙代理时，协调代理不能等其余兄弟全结束才醒。"""
    agent, scope, parent = _bound_agent_tree(tmp_path)
    grandchild, _agent_run = _park_child_waiting_for_grandchild(agent, parent)
    sibling = agent.subagents.create_run(
        goal="继续处理另一个分片",
        root_id="task-root",
        parent_id=parent.id,
    )
    sibling = agent.subagents.lifecycle.prepare_runner_attempt(sibling.id)
    agent.subagents.add_child(parent.id, sibling.id)
    assert mark_parent_waiting_for_direct_children(
        agent.subagents,
        parent.id,
        [grandchild.id, sibling.id],
    ) == (grandchild.id, sibling.id)
    started: list[str] = []

    def record_start(_agent, run_id: str):
        started.append(run_id)
        return {"status": "started", "run_ids": [run_id]}

    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.orchestration.dispatch.capability_auto_sweep.auto_start_orphan_run",
        record_start,
    )

    stopped = stop_agent(
        agent,
        scope=scope,
        run_id=grandchild.id,
        operation_id="agent-stop-grandchild-with-sibling",
    )

    assert stopped["status"] == "cancelled"
    assert agent.subagents.load(grandchild.id).status == "CANCELLED"
    assert agent.subagents.load(sibling.id).status == "RUNNING"
    assert parent_wait_blocks_dispatch(agent.subagents.load(parent.id)) is False
    assert started == [parent.id]
    delivery = stopped["result"]["parent_delivery"]
    assert delivery["reason"] == "child_attention_required"
    assert delivery["resume_requested"] is True


def test_interactive_stop_freezes_before_ack_and_does_not_wait_for_cleanup(
    tmp_path, monkeypatch
) -> None:
    agent, scope, child = _bound_agent_tree(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def slow_cancel(batch, root_id):
        calls.append(root_id)
        assert agent.subagents.load(root_id).status == "CANCELLED"
        assert {item.report["run_id"] for item in batch.stops} == {root_id}
        entered.set()
        release.wait(timeout=2.0)
        return {"run_id": root_id, "status": "CANCELLED"}

    monkeypatch.setattr(
        "agent_py_agent.agent.conversation.agent_control.cleanup_subagent_tree",
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


def test_interactive_stop_eventually_wakes_root_parent(tmp_path) -> None:
    agent, scope, child = _bound_agent_tree(tmp_path)
    root_thread = agent.conversation_store.tasks.thread_for("task-root")
    assert root_thread is not None
    agent.conversation_store.tasks.bind(
        {
            "thread_id": root_thread.thread_id,
            "task_id": child.id,
            "goal": child.goal,
            "status": "active",
            "now": time.time(),
        }
    )

    accepted = enqueue_agent_stop(
        agent,
        scope=scope,
        run_id=child.id,
        operation_id="agent-stop-root-wake",
    )

    deadline = time.monotonic() + 2.0
    signals = []
    while time.monotonic() < deadline:
        signals = agent.conversation_store.wakes.pending(limit=0)
        if agent.subagents.load(child.id).status == "CANCELLED" and signals:
            break
        time.sleep(0.01)

    assert accepted["status"] == "accepted"
    assert agent.subagents.load(child.id).status == "CANCELLED"
    assert len(signals) == 1
    assert signals[0].source_agent_id == child.id
    assert signals[0].metadata["status"] == "CANCELLED"


def test_interactive_stop_reconciles_grandchild_created_inside_inflight_guard(
    tmp_path,
) -> None:
    """独立创建和控制线程交错；停止须覆盖创建事务已经落盘的孙代理。"""
    agent, scope, child = _bound_agent_tree(tmp_path)
    creating, finish_creation, controlling = (threading.Event() for _ in range(3))
    created, results, errors = [], [], []

    def create():
        try:
            with agent.subagents.creation_guard():
                creating.set()
                assert finish_creation.wait(3)
                task = agent.subagents.create_run(
                    goal="模拟在途派工落盘的孙任务", root_id="task-root", parent_id=child.id,
                )
                created.append(agent.subagents.lifecycle.prepare_runner_attempt(task.id))
        except BaseException as exc:
            errors.append(exc)

    def stop():
        controlling.set()
        try:
            results.append(enqueue_agent_stop(
                agent, scope=scope, run_id=child.id, operation_id="agent-stop-late-grandchild",
            ))
        except BaseException as exc:
            errors.append(exc)

    creator = threading.Thread(target=create, daemon=True)
    controller = threading.Thread(target=stop, daemon=True)
    creator.start()
    try:
        assert creating.wait(2)
        controller.start()
        assert controlling.wait(2)
    finally:
        finish_creation.set()
        creator.join(5)
        if controller.ident is not None:
            controller.join(5)
    assert not creator.is_alive() and not controller.is_alive()
    assert not errors
    late_grandchild = created[0]

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if (
            agent.subagents.load(child.id).status == "CANCELLED"
            and agent.subagents.load(late_grandchild.id).status == "CANCELLED"
        ):
            break
        time.sleep(0.01)

    assert results[0]["status"] == "accepted"
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
    assert agent.conversation_store.guidance.recent("agent_run", child.id) == []


def test_consumed_guidance_http_replay_does_not_queue_another_attempt(tmp_path) -> None:
    agent, scope, child = _bound_agent_tree(tmp_path)
    accepted = send_agent_guidance(
        agent,
        scope=scope,
        run_id=child.id,
        message="确认收到后继续等待。",
        message_id="agent-steer-consumed-replay",
    )
    entry = agent.conversation_store.guidance.recent("agent_run", child.id)[0]
    turn_id = str(entry.metadata["expected_turn_id"])
    assert agent.conversation_store.guidance.claim_for_turn(
        entry,
        expected_turn_id=turn_id,
        attempt_id=turn_id,
    ) is True
    assert agent.conversation_store.guidance.submissions.mark_submitted(
        turn_id,
        [entry],
        attempt_id=turn_id,
    ) == (entry.guidance_id,)
    assert agent.conversation_store.guidance.acknowledgements.consume_submitted(
        turn_id,
        [entry],
    ) == (entry.guidance_id,)
    repo = agent.subagents.runtime_db
    agent_run = repo.agent_run_for_run_id(child.id)
    assert agent_run is not None
    assert repo.settle_agent_attempt(
        agent_run_id=str(agent_run["agent_run_id"]),
        attempt_id=turn_id,
    )["settled"] is True
    idle = agent.subagents.load(child.id)
    idle.status = "PENDING"
    idle.runner_active_attempt_id = ""
    agent.subagents.save(idle)

    replay = send_agent_guidance(
        agent,
        scope=scope,
        run_id=child.id,
        message="确认收到后继续等待。",
        message_id="agent-steer-consumed-replay",
    )

    assert replay["ok"] is True
    assert replay["replayed"] is True
    assert replay["status"] == "consumed"
    assert replay["guidance_id"] == accepted["guidance_id"]
    assert replay["expected_turn_id"] == turn_id
    assert replay["resume"]["status"] == "replay_consumed"
    assert len(repo.attempts_for_run(str(agent_run["agent_run_id"]))) == 1


@pytest.mark.parametrize("status", ["reserved", "submitted", "consumed", "rejected", "submitted_batch", "consumed_batch"])
def test_guidance_replay_refreshes_receipt_before_reserving_successor(tmp_path, monkeypatch, status):
    """旧 pending 快照与独立消费者交错，重放不能为已接纳消息多预留一轮。"""
    from agent_py_agent.agent.conversation import agent_control

    agent, scope, child = _bound_agent_tree(tmp_path)
    args = dict(scope=scope, run_id=child.id, message="确认收到后继续等待。", message_id="racing-replay")
    accepted = send_agent_guidance(agent, **args)
    guidance = agent.conversation_store.guidance
    key = "user-agent-guidance:racing-replay"
    entry = guidance.receipt(key).entry
    turn_id = entry.metadata["expected_turn_id"]
    repo = agent.subagents.runtime_db
    agent_run_id = repo.agent_run_for_run_id(child.id)["agent_run_id"]
    assert repo.settle_agent_attempt(agent_run_id=agent_run_id, attempt_id=turn_id)["settled"]
    idle = agent.subagents.load(child.id)
    idle.status, idle.runner_active_attempt_id = "PENDING", ""
    agent.subagents.save(idle)
    prior_read, changed = threading.Event(), threading.Event()
    errors, starts = [], []
    outcome = status.removesuffix("_batch")

    def consume():
        try:
            assert prior_read.wait(3)
            if outcome == "rejected":
                guidance.mark_status(key, "rejected")
            else:
                assert guidance.claim_for_turn(entry, expected_turn_id=turn_id, attempt_id=turn_id)
                if status == "submitted_batch":
                    with monkeypatch.context() as patch:
                        patch.setattr(guidance.submissions, "apply_locked", fail_projection)
                        with pytest.raises(OSError, match="projection fault"):
                            guidance.submissions.mark_submitted(turn_id, [entry], attempt_id=turn_id)
                elif outcome in {"submitted", "consumed"}:
                    guidance.submissions.mark_submitted(turn_id, [entry], attempt_id=turn_id)
                if status == "consumed_batch":
                    with monkeypatch.context() as patch:
                        patch.setattr(guidance.acknowledgements, "_apply_locked", fail_projection)
                        with pytest.raises(OSError, match="projection fault"):
                            guidance.acknowledgements.consume_submitted(turn_id, [entry])
                elif outcome == "consumed":
                    guidance.acknowledgements.consume_submitted(turn_id, [entry])
        except BaseException as exc:
            errors.append(exc)
        finally:
            changed.set()

    def fail_projection(*args, **kwargs):
        raise OSError("projection fault")

    read = agent_control._read_guidance_receipt

    def pause_after_read(submission):
        prior = read(submission)
        assert prior.status == "pending"
        prior_read.set()
        assert changed.wait(3)
        return prior

    monkeypatch.setattr(agent_control, "_read_guidance_receipt", pause_after_read)
    monkeypatch.setattr(agent_control, "_resume_agent_for_guidance", lambda *a, **kw: starts.append(kw))
    consumer = threading.Thread(target=consume, daemon=True)
    consumer.start()
    try:
        if outcome == "rejected":
            with pytest.raises(AgentControlError) as exc_info:
                send_agent_guidance(agent, **args)
            assert exc_info.value.error_code == "AGENT_GUIDANCE_REJECTED"
        else:
            replay = send_agent_guidance(agent, **args)
            assert replay["status"] == outcome
            assert replay["guidance_id"] == accepted["guidance_id"]
            assert replay["expected_turn_id"] == turn_id
    finally:
        consumer.join(3)
    assert not consumer.is_alive() and not errors
    assert not starts
    assert len(repo.attempts_for_run(agent_run_id)) == 1
    assert guidance.receipt(key).status == outcome
    assert guidance.receipt(key).entry.metadata["expected_turn_id"] == turn_id


# LLM: 使用真实临时 DB/邮箱建立已结束原轮中的 pending 消息，不预留后继；后续故障只注入正式写入边界。
# 函数用途: 为消息重放测试提供一个确实需要恢复的空闲代理及其原始回执。
def _idle_guidance_replay_state(tmp_path):
    agent, scope, child = _bound_agent_tree(tmp_path)
    args = dict(agent=agent, scope=scope, run_id=child.id, message="继续检查剩余工作。", message_id="idle-replay")
    send_agent_guidance(**args)
    guidance, repo = agent.conversation_store.guidance, agent.subagents.runtime_db
    key = "user-agent-guidance:idle-replay"
    entry = guidance.receipt(key).entry
    agent_run_id = repo.agent_run_for_run_id(child.id)["agent_run_id"]
    assert repo.settle_agent_attempt(agent_run_id=agent_run_id, attempt_id=entry.metadata["expected_turn_id"])["settled"]
    current = agent.subagents.load(child.id)
    current.status, current.runner_active_attempt_id = "PENDING", ""
    agent.subagents.save(current)
    return SimpleNamespace(agent=agent, child=child, args=args, guidance=guidance, repo=repo,
                           key=key, entry=entry, agent_run_id=agent_run_id)


@pytest.mark.parametrize("fault", ["receipt", "index"])
def test_guidance_replay_reuses_committed_successor_after_write_failure(tmp_path, monkeypatch, fault):
    from agent_py_agent.agent.conversation import agent_control

    state = _idle_guidance_replay_state(tmp_path)
    starts = []
    monkeypatch.setattr(agent_control, "_resume_agent_for_guidance", lambda *a, **kw: starts.append(kw) or {"status": "started"})
    target = state.guidance.recovery._rebinding if fault == "receipt" else state.guidance.ledger
    method = "write_rebound_locked" if fault == "receipt" else "ensure_turn_index"
    original = getattr(target, method)

    def fail_new_binding(receipt, **kwargs):
        if fault == "receipt" or receipt.entry.metadata["expected_turn_id"] != state.entry.metadata["expected_turn_id"]:
            raise OSError("rebind write fault")
        return original(receipt, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(target, method, fail_new_binding)
        with pytest.raises(AgentControlError, match="处理轮暂未启动"):
            send_agent_guidance(**state.args)
    assert not starts
    pending = state.repo.current_attempt(state.agent_run_id)
    assert pending["status"] == "pending"
    assert len(state.repo.attempts_for_run(state.agent_run_id)) == 2
    replay = send_agent_guidance(**state.args)
    assert replay["status"] == "pending"
    assert replay["guidance_id"] == state.entry.guidance_id
    assert replay["expected_turn_id"] == pending["attempt_id"]
    assert len(state.repo.attempts_for_run(state.agent_run_id)) == 2
    assert starts == [{"expected_attempt_id": pending["attempt_id"]}]


@pytest.mark.parametrize("candidate", ["a-new-turn", "z-new-turn"])
def test_guidance_replay_locks_both_turns_in_order_and_rebinds_only_one_message(tmp_path, monkeypatch, candidate):
    from agent_py_agent.agent.conversation import agent_control

    state = _idle_guidance_replay_state(tmp_path)
    original = state.guidance.ledger.turn_guard
    locks = []
    request = {**state.entry.to_dict(), "message": "另一条独立消息"}
    other = state.guidance.append_once(request, dedupe_key="other-guidance")

    @contextmanager
    def observe(turn_id):
        locks.append(turn_id)
        with original(turn_id):
            yield

    monkeypatch.setattr(state.guidance.ledger, "turn_guard", observe)
    monkeypatch.setattr(agent_control, "new_id", lambda kind: candidate)
    monkeypatch.setattr(agent_control, "_resume_agent_for_guidance", lambda *a, **kw: {"status": "started"})
    replay = send_agent_guidance(**state.args)
    old = state.entry.metadata["expected_turn_id"]
    assert locks == sorted([old, candidate])
    assert replay["expected_turn_id"] == candidate
    assert state.guidance.receipt("other-guidance").entry.metadata["expected_turn_id"] == old
    assert state.guidance.receipt("other-guidance").entry.guidance_id == other.guidance_id
    assert state.repo.current_attempt(state.agent_run_id)["attempt_id"] == candidate


def test_consumer_cannot_claim_old_message_during_successor_reservation(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import agent_control, store_guidance
    from agent_py_agent.agent.gateway_parts import io

    state = _idle_guidance_replay_state(tmp_path)
    reserving, attempted, release = (threading.Event() for _ in range(3))
    errors, claimed, replies, lock_states = [], [], [], []
    queue = state.repo.queue_pending_attempt
    transition = store_guidance.locked_file_transition

    def slow_reserve(*args, **kwargs):
        reserving.set()
        assert release.wait(4)
        return queue(*args, **kwargs)

    @contextmanager
    def observed_transition(path):
        if threading.current_thread().name == "late-guidance-consumer":
            lock_states.append(io._path_lock(path).locked())
            attempted.set()
        with transition(path):
            yield

    def replay():
        try:
            replies.append(send_agent_guidance(**state.args))
        except BaseException as exc:
            errors.append(exc)

    def consume():
        try:
            old = state.entry.metadata["expected_turn_id"]
            claimed.append(state.guidance.claim_for_turn(state.entry, expected_turn_id=old, attempt_id=old))
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(state.agent.subagents.runtime_db, "queue_pending_attempt", slow_reserve)
    monkeypatch.setattr(store_guidance, "locked_file_transition", observed_transition)
    monkeypatch.setattr(agent_control, "_resume_agent_for_guidance", lambda *a, **kw: {"status": "started"})
    controller = threading.Thread(target=replay, daemon=True)
    consumer = threading.Thread(target=consume, daemon=True, name="late-guidance-consumer")
    controller.start()
    try:
        assert reserving.wait(3)
        consumer.start()
        assert attempted.wait(3)
        assert lock_states == [True]
    finally:
        release.set()
        controller.join(4)
        if consumer.ident is not None:
            consumer.join(4)
    assert not errors and not controller.is_alive() and not consumer.is_alive()
    assert claimed == [False]
    assert replies[0]["expected_turn_id"] != state.entry.metadata["expected_turn_id"]
    assert len(state.repo.attempts_for_run(state.agent_run_id)) == 2


def test_agent_guidance_wakes_waiting_parent_on_same_run_once(
    tmp_path,
    monkeypatch,
) -> None:
    agent, scope, child = _bound_agent_tree(tmp_path)
    grandchild, agent_run = _park_child_waiting_for_grandchild(agent, child)
    repo = agent.subagents.runtime_db
    starts: list[str] = []

    def fake_auto_start(_agent, tasks, _request_params, *, expected_attempt_ids):
        target = tasks[0]
        assert expected_attempt_ids[target.id]
        starts.append(target.id)
        refreshed = agent.subagents.load(target.id)
        attrs = dict(refreshed.attributes or {})
        attrs["background_start"] = {
            "launch_id": "launch-user-guidance",
            "status": "launching",
            "updated_at": time.time(),
        }
        refreshed.attributes = attrs
        agent.subagents.save(refreshed)
        return {
            "status": "started",
            "run_ids": [target.id],
            "launch_id": "launch-user-guidance",
        }

    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.orchestration.background.dispatch.auto_start_tasks",
        fake_auto_start,
    )

    accepted = send_agent_guidance(
        agent,
        scope=scope,
        run_id=child.id,
        message="先回复我这条插话，再继续等待孩子。",
        message_id="agent-steer-waiting-parent",
    )
    replay = send_agent_guidance(
        agent,
        scope=scope,
        run_id=child.id,
        message="先回复我这条插话，再继续等待孩子。",
        message_id="agent-steer-waiting-parent",
    )

    successor = repo.current_attempt(str(agent_run["agent_run_id"]))
    assert successor is not None
    assert successor["attempt_generation"] == 2
    assert successor["status"] == "pending"
    assert accepted["expected_turn_id"] == successor["attempt_id"]
    assert accepted["guidance_id"] == replay["guidance_id"]
    assert accepted["resume"]["status"] == "started"
    assert replay["resume"]["status"] == "already_starting"
    assert starts == [child.id]
    assert parent_wait_blocks_dispatch(agent.subagents.load(child.id)) is False
    assert agent.subagents.load(grandchild.id).status == "PLANNING"
    entries = agent.conversation_store.guidance.recent("agent_run", child.id)
    assert len(entries) == 1
    assert entries[0].metadata["expected_turn_id"] == successor["attempt_id"]


def test_guidance_launch_does_not_hold_child_creation_guard(tmp_path, monkeypatch):
    agent, scope, child = _bound_agent_tree(tmp_path)
    _park_child_waiting_for_grandchild(agent, child)

    def fake_auto_start(_agent, tasks, _params, *, expected_attempt_ids):
        entered = threading.Event()

        def another_child_transaction():
            with agent.subagents.creation_guard():
                entered.set()

        thread = threading.Thread(target=another_child_transaction, daemon=True)
        thread.start()
        thread.join(2)
        assert entered.is_set(), "宿主启动不得占用子代理树协调锁"
        return {"status": "started", "run_ids": [tasks[0].id]}

    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.orchestration.background.dispatch.auto_start_tasks",
        fake_auto_start,
    )
    result = send_agent_guidance(
        agent, scope=scope, run_id=child.id, message="继续当前任务", message_id="launch-outside-guard",
    )
    assert result["resume"]["status"] == "started"


def test_guidance_waits_for_control_then_rejects_terminal_without_reservation(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import agent_control

    agent, scope, child = _bound_agent_tree(tmp_path)
    _grandchild, agent_run = _park_child_waiting_for_grandchild(agent, child)
    repo = agent.subagents.runtime_db
    before = len(repo.attempts_for_run(str(agent_run["agent_run_id"])))
    errors = []
    acquiring, reloading, finished = (threading.Event() for _ in range(3))
    original_guard = agent.subagents.creation_guard
    original_reload = agent_control._reload_guidance_task

    @contextmanager
    def observed_guard():
        if threading.current_thread().name == "waiting-guidance":
            acquiring.set()
        with original_guard():
            yield

    def observed_reload(*args):
        reloading.set()
        return original_reload(*args)

    monkeypatch.setattr(agent.subagents, "creation_guard", observed_guard)
    monkeypatch.setattr(agent_control, "_reload_guidance_task", observed_reload)

    def send():
        try:
            send_agent_guidance(
                agent, scope=scope, run_id=child.id, message="继续当前任务", message_id="stale-guidance",
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    with agent.subagents.creation_guard():
        thread = threading.Thread(target=send, daemon=True, name="waiting-guidance")
        thread.start()
        assert acquiring.wait(2)
        assert not reloading.is_set()
        assert not finished.is_set()
        current = agent.subagents.load(child.id)
        current.status = "CANCELLED"
        agent.subagents.save(current)
    thread.join(5)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], AgentControlError)
    assert errors[0].error_code == "AGENT_ALREADY_TERMINAL"
    assert len(repo.attempts_for_run(str(agent_run["agent_run_id"]))) == before
    assert agent.conversation_store.guidance.recent("agent_run", child.id) == []


def test_concurrent_waiting_parent_guidance_starts_one_runner(
    tmp_path,
    monkeypatch,
) -> None:
    agent, scope, child = _bound_agent_tree(tmp_path)
    _park_child_waiting_for_grandchild(agent, child)
    starts: list[str] = []

    def fake_auto_start(_agent, tasks, _request_params, *, expected_attempt_ids):
        target = tasks[0]
        assert expected_attempt_ids[target.id]
        starts.append(target.id)
        time.sleep(0.05)
        refreshed = agent.subagents.load(target.id)
        attrs = dict(refreshed.attributes or {})
        attrs["background_start"] = {
            "launch_id": "launch-concurrent-guidance",
            "status": "launching",
            "updated_at": time.time(),
        }
        refreshed.attributes = attrs
        agent.subagents.save(refreshed)
        return {
            "status": "started",
            "run_ids": [target.id],
            "launch_id": "launch-concurrent-guidance",
        }

    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.orchestration.background.dispatch.auto_start_tasks",
        fake_auto_start,
    )
    barrier = threading.Barrier(3)
    responses: list[dict[str, object]] = []
    failures: list[BaseException] = []

    def send(index: int) -> None:
        try:
            barrier.wait(timeout=2.0)
            responses.append(
                send_agent_guidance(
                    agent,
                    scope=scope,
                    run_id=child.id,
                    message=f"并发补充 {index}",
                    message_id=f"agent-steer-concurrent-{index}",
                )
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    workers = [threading.Thread(target=send, args=(index,)) for index in range(2)]
    for worker in workers:
        worker.start()
    barrier.wait(timeout=2.0)
    for worker in workers:
        worker.join(timeout=2.0)

    assert failures == []
    assert len(responses) == 2
    assert starts == [child.id]
    assert len(agent.conversation_store.guidance.recent("agent_run", child.id)) == 2


def test_waiting_parent_guidance_retries_launch_without_duplicate_message(
    tmp_path,
    monkeypatch,
) -> None:
    agent, scope, child = _bound_agent_tree(tmp_path)
    _park_child_waiting_for_grandchild(agent, child)
    starts = 0

    def flaky_auto_start(_agent, tasks, _request_params, *, expected_attempt_ids):
        nonlocal starts
        starts += 1
        if starts == 1:
            return {"status": "failed", "run_ids": [tasks[0].id]}
        return {
            "status": "started",
            "run_ids": [tasks[0].id],
            "launch_id": "launch-retried-guidance",
        }

    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.orchestration.background.dispatch.auto_start_tasks",
        flaky_auto_start,
    )
    with pytest.raises(AgentControlError) as first_error:
        send_agent_guidance(
            agent,
            scope=scope,
            run_id=child.id,
            message="这条消息必须保留并重试启动。",
            message_id="agent-steer-retry-launch",
        )
    assert first_error.value.error_code == "AGENT_RESUME_UNAVAILABLE"
    assert len(agent.conversation_store.guidance.recent("agent_run", child.id)) == 1

    accepted = send_agent_guidance(
        agent,
        scope=scope,
        run_id=child.id,
        message="这条消息必须保留并重试启动。",
        message_id="agent-steer-retry-launch",
    )

    assert accepted["resume"]["status"] == "started"
    assert starts == 2
    assert len(agent.conversation_store.guidance.recent("agent_run", child.id)) == 1


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
    assert agent.conversation_store.guidance.recent("agent_run", child.id) == []


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
    assert calls[0][1]["client_capabilities"] == {"tool_approval": True, "foreground_messages": True, "foreground_transcript": True, "display_checkpoints": True}
    assert calls[-1][2] == 10.0


@pytest.mark.parametrize("parent_projection", [False, True])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("decision_kind", ["approved", "denied"])
def test_owner_tui_decision_resumes_exact_child_approval(tmp_path, parent_projection, nested, decision_kind) -> None:
    agent, scope, child = _bound_agent_tree(tmp_path)
    parent_thread_id = agent.conversation_store.tasks.load(child.root_id).thread_id
    if nested:
        parent_thread_id = child.agent_thread_id
        child = agent.subagents.create_run(goal="完成下一层子任务", root_id=child.root_id, parent_id=child.id)
        child = agent.subagents.lifecycle.prepare_runner_attempt(child.id)
    if parent_projection:
        from agent_py_agent.agent.agent_core.orchestration.lifecycle import (
            _bind_tasks_to_conversation,
        )

        child.attributes["conversation_thread_id"] = parent_thread_id
        agent.subagents.save(child)
        assert _bind_tasks_to_conversation(agent, [child]) == []
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
        pending = list_pending_agent_tool_approvals(
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
        decision_kind,
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
    assert result["decision"] == decision_kind
    assert result["feedback"] == "只执行这一次"
    assert list_pending_agent_tool_approvals(
        agent,
        root_task_id=child.root_id,
    ) == []


def test_child_approval_without_interactive_consumer_fails_closed(tmp_path) -> None:
    agent, _scope, child = _bound_agent_tree(tmp_path)
    request = _child_approval_request(child.id, "attempt-no-consumer")
    handle = publish_agent_tool_approval(
        agent,
        run_id=child.id,
        thread_id=child.agent_thread_id,
        request_value=request,
    )

    decision = wait_for_agent_tool_approval(
        handle,
        poll_seconds=0.01,
        discovery_seconds=0.01,
        consumer_lease_seconds=0.01,
    )

    assert decision.decision == "unavailable"
    assert list_pending_agent_tool_approvals(
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
        pending = list_pending_agent_tool_approvals(
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
    assert list_pending_agent_tool_approvals(
        agent,
        root_task_id=child.root_id,
    ) == []
