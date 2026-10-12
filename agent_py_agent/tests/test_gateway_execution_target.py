"""实际执行目标来自持有的车道，等待请求与迟到快照不取得控制资格。"""

from __future__ import annotations

import threading
import time
from dataclasses import replace

import pytest

from agent_py_agent.agent.gateway_parts.execution_target import (
    GatewayExecutionTarget,
    read_gateway_execution_target,
)
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.tests.test_steer_delivery_recovery import _background_input


def test_waiting_request_cannot_replace_background_execution(tmp_path):
    agent, paths, _ = _background_input(tmp_path, "task-a")
    store = agent.conversation_store
    link = store.tasks.load("task-a")
    claim = store.claims.acquire({"thread_id": link.thread_id, "task_id": link.task_id, "reason": "thread_goal_continue"})
    write_json_file_atomic(paths.processing / "request-b.json", {
        "id": "request-b", "status": "processing", "turn_phase": "open",
        "conversation_claim": {"schema_version": "gateway_conversation_claim.v1",
                               "request_id": "request-b", "thread_id": link.thread_id,
                               "task_id": "gateway:request-b"},
    })

    target = read_gateway_execution_target(paths, store, link.thread_id)

    assert target == GatewayExecutionTarget(link.thread_id, "task", "task-a", claim["claim_id"], task_id="task-a")
    assert GatewayExecutionTarget.from_dict(target.to_dict()) == target


def test_open_processing_without_claim_is_not_execution(tmp_path):
    agent, paths, _ = _background_input(tmp_path, "task-a")
    store = agent.conversation_store
    link = store.tasks.load("task-a")
    write_json_file_atomic(paths.processing / "request-b.json", {"id": "request-b", "status": "processing"})
    assert read_gateway_execution_target(paths, store, link.thread_id) is None


@pytest.mark.parametrize("invalid", ["expired", "wrong_thread", "finished", "missing_task", "corrupt"])
def test_unqualified_claim_is_not_execution(tmp_path, invalid):
    agent, paths, _ = _background_input(tmp_path, "task-a")
    store = agent.conversation_store
    link = store.tasks.load("task-a")
    claim = store.claims.acquire({"thread_id": link.thread_id, "task_id": link.task_id, "reason": "thread_goal_continue"})
    path = store.storage.background_claim_path(link.thread_id)
    if invalid == "corrupt":
        path.write_text("{", encoding="utf-8")
    else:
        field, value = {"expired": ("expires_at", time.time() - 1), "wrong_thread": ("thread_id", "other"),
                        "finished": ("status", "finished"), "missing_task": ("task_id", "missing")}[invalid]
        write_json_file_atomic(path, {**claim, field: value})
    assert read_gateway_execution_target(paths, store, link.thread_id) is None


@pytest.mark.parametrize("phase", ["executing", "waiting", "closed", "prebound", "wrong_binding"])
def test_foreground_requires_its_own_claim_and_open_binding(tmp_path, phase):
    agent, paths, _ = _background_input(tmp_path, "task-a")
    store = agent.conversation_store
    link = store.tasks.load("task-a")
    claim = store.claims.acquire({
        "thread_id": link.thread_id, "task_id": "gateway:request-b",
        "reason": "gateway_foreground_turn",
    })
    payload = {
        "id": "request-b", "status": "processing", "turn_phase": "open",
        "execution_attempt_id": "attempt-b",
        "conversation_claim": {"schema_version": "gateway_conversation_claim.v1",
                               "request_id": "request-b", "thread_id": link.thread_id,
                               "task_id": "gateway:request-b"},
    }
    if phase == "waiting":
        claim["task_id"] = "gateway:request-c"
        write_json_file_atomic(store.storage.background_claim_path(link.thread_id), claim)
    elif phase == "closed":
        payload["turn_phase"] = "closed"
    elif phase == "prebound":
        store.storage.background_claim_path(link.thread_id).unlink()
    elif phase == "wrong_binding":
        payload["conversation_claim"]["thread_id"] = "other"
    write_json_file_atomic(paths.processing / "request-b.json", payload)
    target = read_gateway_execution_target(paths, store, link.thread_id)
    if phase == "executing":
        assert target == GatewayExecutionTarget(link.thread_id, "request", "request-b", claim["claim_id"], "attempt-b")
    else:
        assert target is None


def test_claim_change_during_read_does_not_publish_old_target(tmp_path, monkeypatch):
    agent, paths, _ = _background_input(tmp_path, "task-a")
    store = agent.conversation_store
    link = store.tasks.load("task-a")
    claim = store.claims.acquire({"thread_id": link.thread_id, "task_id": link.task_id, "reason": "thread_goal_continue"})
    reads = iter([claim, {**claim, "claim_id": "new-claim"}])
    monkeypatch.setattr(store.claims, "load", lambda _thread: next(reads))
    assert read_gateway_execution_target(paths, store, link.thread_id) is None


# LLM: 使用真实临时 Store 与 claim，不用活动目标 mock；u-1/c-1 仅为合成身份。
# 函数用途: 创建可从 Gateway 控制入口解析的测试任务，不执行模型或网络。
def _controlled_task(tmp_path):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.tests.test_gateway_conversation_control import _bind_durable_task, _scope

    agent = SimpleAgent(AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False), tmp_path)
    thread, link = _bind_durable_task(agent, "task-a")
    return agent, gateway_paths(agent), thread, link, _scope()


def test_two_ordinary_inputs_choose_executing_background_over_waiting_foreground(tmp_path):
    from agent_py_agent.agent.gateway_parts.control_service import (
        steer_active_conversation_if_running,
    )
    from agent_py_agent.agent.gateway_parts.request_binding import GatewayTaskBindingWriter
    from agent_py_agent.tests.test_gateway_conversation_control import _request

    agent, paths, thread, link, scope = _controlled_task(tmp_path)
    agent.conversation_store.claims.acquire({
        "thread_id": thread.thread_id, "task_id": link.task_id, "reason": "thread_goal_continue",
    })
    waiting = paths.processing / "request-b.json"
    write_json_file_atomic(waiting, _request("request-b"))
    GatewayTaskBindingWriter(waiting, "request-b").bind_conversation_claim(thread.thread_id)
    for message_id in ("z-first", "a-second"):
        result = steer_active_conversation_if_running(
            agent, paths, message=message_id, scope=replace(scope, metadata={"message_id": message_id}),
        )
        assert result.ok and result.request_id == link.task_id
    assert [entry.message for entry in agent.conversation_store.guidance.pending("task", link.task_id)] == ["z-first", "a-second"]
    assert agent.conversation_store.guidance.pending("request", "request-b") == []


def test_old_execution_snapshot_cannot_steer_or_stop_new_claim(tmp_path):
    from agent_py_agent.agent.gateway_parts.control_service import (
        execute_gateway_conversation_control,
        steer_active_conversation_if_running,
    )
    from agent_py_agent.tests.test_gateway_conversation_control import _command

    agent, paths, thread, link, scope = _controlled_task(tmp_path)
    store = agent.conversation_store
    claim = store.claims.acquire({"thread_id": thread.thread_id, "task_id": link.task_id, "reason": "thread_goal_continue"})
    target = read_gateway_execution_target(paths, store, thread.thread_id)
    store.claims.finish({"thread_id": thread.thread_id, "claim_id": claim["claim_id"], "status": "finished"})
    newer = store.claims.acquire({"thread_id": thread.thread_id, "task_id": link.task_id, "reason": "thread_goal_continue"})
    bound = replace(scope, metadata={"message_id": "late", "expected_turn_id": link.task_id, "expected_execution": target.to_dict()})

    steer = steer_active_conversation_if_running(agent, paths, message="迟到补充", scope=bound)
    stopped = execute_gateway_conversation_control(agent, paths, _command("/stop"), bound)

    assert steer.error_code == stopped.error_code == "EXECUTION_TARGET_CHANGED"
    assert store.tasks.load(link.task_id).status == "active"
    assert store.claims.load(thread.thread_id)["claim_id"] == newer["claim_id"]
    assert store.guidance.pending("task", link.task_id) == []


def test_background_interrupt_hits_only_claim_then_allows_another_round(tmp_path):
    from agent_py_agent.agent.concurrency.interrupt import wait_interruptibly
    from agent_py_agent.agent.conversation.background_claim import (
        BackgroundClaimDependencies,
        run_claimed,
    )
    from agent_py_agent.agent.gateway_parts.control_service import (
        execute_gateway_conversation_control,
    )
    from agent_py_agent.tests.test_gateway_conversation_control import _command

    agent, paths, thread, link, scope = _controlled_task(tmp_path)
    ready, interrupted, finish = threading.Event(), threading.Event(), threading.Event()
    failures = []

    def run_once(_kwargs):
        ready.set()
        try:
            while not finish.is_set():
                wait_interruptibly(0.1)
        except InterruptedError:
            interrupted.set()
            raise

    dependencies = BackgroundClaimDependencies(
        claims=agent.conversation_store.claims, claim_scope_id=lambda tid, _task: tid,
        child_owns_task=lambda _task: False, terminal_task=lambda _args: False,
        recovery_block=lambda _task: None, source_admission=lambda _wake: "",
        retire_source=lambda _args: None, run_once=run_once, runtime_facts=lambda: {},
        record_policy_failure=lambda _args: None, lease_seconds=30, heartbeat_interval_seconds=5,
    )

    def worker():
        try:
            run_claimed(dependencies, {"thread_id": thread.thread_id, "task_id": link.task_id,
                                      "reason": "thread_goal_continue"})
        except BaseException as exc:
            failures.append(exc)

    running = threading.Thread(target=worker)
    running.start()
    try:
        assert ready.wait(3)
        target = read_gateway_execution_target(paths, agent.conversation_store, thread.thread_id)
        bound = replace(scope, metadata={"expected_turn_id": link.task_id, "expected_execution": target.to_dict()})
        result = execute_gateway_conversation_control(agent, paths, _command("/interrupt"), bound)
        assert result.ok
        assert interrupted.wait(3)
    finally:
        finish.set()
        running.join(3)
    assert not running.is_alive() and not failures
    assert agent.conversation_store.tasks.load(link.task_id).status == "active"
    newer = agent.conversation_store.claims.acquire({
        "thread_id": thread.thread_id, "task_id": link.task_id, "reason": "thread_goal_continue",
    })
    assert newer and newer["claim_id"] != target.claim_id


def test_same_claim_new_core_attempt_rejects_old_control_and_uses_real_turn_id(tmp_path):
    from agent_py_agent.agent.conversation.background_execution import _BackgroundRunControl
    from agent_py_agent.agent.conversation.claim_execution_binding import (
        ClaimExecutionBinding,
        bind_claim_execution,
    )
    from agent_py_agent.agent.gateway_parts.control_service import (
        execute_gateway_conversation_control,
        steer_active_conversation_if_running,
    )
    from agent_py_agent.tests.test_gateway_conversation_control import _command

    agent, paths, thread, link, scope = _controlled_task(tmp_path)
    store = agent.conversation_store
    claim = store.claims.acquire({"thread_id": thread.thread_id, "task_id": link.task_id, "reason": "thread_goal_continue"})
    binding = {"invocation_run_id": "background-turn", "task_id": link.task_id,
               "run_id": "run-main", "agent_run_id": "agent-main", "attempt_id": "attempt-1"}
    with bind_claim_execution(ClaimExecutionBinding(store.claims, thread.thread_id, claim["claim_id"], thread.thread_id)):
        control = _BackgroundRunControl("background-turn", None)
        assert control.bind_runtime_authority(binding)
        target = read_gateway_execution_target(paths, store, thread.thread_id)
        assert target.turn_id == "background-turn" and target.task_id == link.task_id
        accepted = steer_active_conversation_if_running(
            agent, paths, message="第一条", scope=replace(scope, metadata={"message_id": "first"}),
        )
        assert accepted.ok
        entry = store.guidance.pending("task", link.task_id)[0]
        assert entry.metadata["expected_turn_id"] == "background-turn"
        control.finish()
        assert _BackgroundRunControl("background-turn", None).bind_runtime_authority({**binding, "attempt_id": "attempt-2"})

    stale = replace(scope, metadata={"expected_turn_id": target.turn_id, "expected_execution": target.to_dict()})
    result = execute_gateway_conversation_control(agent, paths, _command("/stop"), stale)
    assert result.error_code == "EXECUTION_TARGET_CHANGED"
    assert store.tasks.load(link.task_id).status == "active"
    assert read_gateway_execution_target(paths, store, thread.thread_id).execution_attempt_id == "attempt-2"


def test_background_publication_cannot_overwrite_replaced_claim(tmp_path):
    from agent_py_agent.agent.conversation.background_execution import _BackgroundRunControl
    from agent_py_agent.agent.conversation.claim_execution_binding import (
        ClaimExecutionBinding,
        bind_claim_execution,
    )

    agent, _paths, thread, link, _scope = _controlled_task(tmp_path)
    store = agent.conversation_store
    claim = store.claims.acquire({"thread_id": thread.thread_id, "task_id": link.task_id, "reason": "thread_goal_continue"})
    store.claims.finish({"thread_id": thread.thread_id, "claim_id": claim["claim_id"], "status": "finished"})
    newer = store.claims.acquire({"thread_id": thread.thread_id, "task_id": link.task_id, "reason": "thread_goal_continue"})
    with bind_claim_execution(ClaimExecutionBinding(store.claims, thread.thread_id, claim["claim_id"], thread.thread_id)):
        with pytest.raises(InterruptedError):
            _BackgroundRunControl("background-turn", None).bind_runtime_authority({
                "invocation_run_id": "background-turn", "task_id": link.task_id,
                "run_id": "run-main", "agent_run_id": "agent-main", "attempt_id": "attempt-1",
            })
    current = store.claims.load(thread.thread_id)
    assert current["claim_id"] == newer["claim_id"] and "runtime_authority" not in current


@pytest.mark.parametrize("foreign", ["owner", "thread"])
def test_feishu_control_cannot_reuse_another_scope_execution_snapshot(tmp_path, foreign):
    from agent_py_agent.agent.gateway_parts.control_service import (
        execute_gateway_conversation_control,
        steer_active_conversation_if_running,
    )
    from agent_py_agent.tests.test_gateway_conversation_control import (
        _bind_durable_task,
        _command,
        _scope,
    )

    agent, paths, thread, link, scope = _controlled_task(tmp_path)
    store = agent.conversation_store
    original = store.claims.acquire({"thread_id": thread.thread_id, "task_id": link.task_id,
                                     "reason": "thread_goal_continue"})
    target = read_gateway_execution_target(paths, store, thread.thread_id)
    user, conversation = ("u-2", "c-1") if foreign == "owner" else ("u-1", "c-2")
    other_thread, other_task = _bind_durable_task(agent, "task-b", user=user, conversation_id=conversation)
    other = store.claims.acquire({"thread_id": other_thread.thread_id, "task_id": other_task.task_id,
                                  "reason": "thread_goal_continue"})
    bound = replace(_scope(user, conversation), metadata={
        "message_id": "wrong-scope", "expected_turn_id": target.turn_id, "expected_execution": target.to_dict(),
    })
    steer = steer_active_conversation_if_running(agent, paths, message="合成补充", scope=bound)
    stopped = execute_gateway_conversation_control(agent, paths, _command("/stop"), bound)
    assert steer is None or not steer.ok
    assert stopped.error_code == "EXECUTION_TARGET_CHANGED"
    for current_thread, task, claim in ((thread, link, original), (other_thread, other_task, other)):
        assert store.tasks.load(task.task_id).status == "active"
        assert store.claims.load(current_thread.thread_id)["claim_id"] == claim["claim_id"]
        assert store.guidance.pending("task", task.task_id) == []
