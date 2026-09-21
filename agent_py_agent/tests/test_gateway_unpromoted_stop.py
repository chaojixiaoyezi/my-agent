"""无持久任务链接的热请求停止合同；临时 DB/Store 和真实 writer，不调用模型或真实 Gateway。"""
from __future__ import annotations

import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.agent_core.runtime_mixin import _bind_main_agent_authority
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import control_service
from agent_py_agent.agent.gateway_parts.control_service import (
    GatewayControlScope,
    _GatewayRequestRecord,
)
from agent_py_agent.agent.gateway_parts.io import (
    gateway_turn_transition,
    read_json_file,
    write_json_file,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.gateway_parts.request_binding import GatewayTaskBindingWriter
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling import process_resource_stop as resources
from agent_py_agent.agent.tooling.process_scope import ProcessExecutionScope
from agent_py_agent.agent.tooling.process_session_store import (
    ProcessSessionStore,
    process_session_store_root,
)
from agent_py_agent.tests.test_task_resource_stop import _write


# LLM: fixture 使用真实发布者和私有临时账本；三个身份故意不同，且不创建 ConversationTaskLink。
# 函数用途: 构造已进入执行准备但尚未晋升为持久任务的请求。
@pytest.fixture
def hot(tmp_path):
    agent = SimpleAgent(AgentConfig(
        model_backend="echo", my_agent_home=str(tmp_path / "home"), gateway_per_user_owner_scoping=False,
    ), tmp_path)
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    conversation = {"channel": "chat", "channel_conversation_id": "session", "channel_user_id": "tester", "canonical_user_id": "tester"}
    thread = agent.conversation_store.threads.get_or_create(conversation)
    payload = {
        "id": "message", "status": "processing", "turn_phase": "open", "user_id": "tester",
        "execution_attempt_id": "transport", "conversation": conversation, "metadata": {"user_id": "tester", "channel": "chat"},
    }
    path = paths.processing / "message.json"
    write_json_file(path, payload)
    repo = agent.subagents.runtime_db
    record = repo.record_run_creation(
        owner_id=agent.home_paths.owner_id, conversation_task_id="task", run_id="main", thread_id=thread.thread_id, role="main",
    )
    scope = ProcessExecutionScope(str(agent.home_paths.owner_home_dir), thread.thread_id, "task", "main", record["attempt_id"])
    return SimpleNamespace(
        agent=agent, paths=paths, payload=payload, path=path, record=record, scope=scope, repo=repo,
        control_scope=GatewayControlScope("tester", "chat", "session"),
        writer=GatewayTaskBindingWriter(path, "message", execution_attempt_id="transport"),
        store=ProcessSessionStore(process_session_store_root(scope.owner_home, scope.owner_home)),
    )


# LLM: 只通过正式 writer 发布 fixture DB 身份，保留 discovery 的旧 payload 来验证控制端 fresh read。
# 函数用途: 在请求发现以后模拟 core 完成身份发布。
def _publish(hot):
    return hot.writer.bind_runtime_authority({
        "invocation_run_id": "message", "task_id": "task", "run_id": "main",
        "agent_run_id": hot.record["agent_run_id"], "attempt_id": hot.record["attempt_id"],
    })


def _stop(hot, *, interrupt_only=False):
    return control_service._stop_live_window_request(
        hot.agent, hot.paths, _GatewayRequestRecord(hot.path, hot.payload), hot.control_scope, interrupt_only=interrupt_only,
    )


def test_stop_reads_publication_after_discovery_and_only_cleans_frozen_main(hot, monkeypatch):
    _publish(hot)
    _write(hot.store, hot.scope, "bg-main")
    _write(hot.store, replace(hot.scope, root_task_id="other-task"), "bg-neighbor")
    entered, release, cleaned = threading.Event(), threading.Event(), threading.Event()
    observed, failures = [], []

    def cleanup(batch):
        try:
            entered.set()
            assert release.wait(3)
            observed.extend(row["session_id"] for row in batch.receipt.records)
        except BaseException as exc:
            failures.append(exc)
        finally:
            cleaned.set()

    monkeypatch.setattr(resources, "cleanup_process_stop", cleanup)

    try:
        result = _stop(hot)
        assert result.ok and result.delivery_status == "accepted"
        assert entered.wait(3)
        assert hot.repo.get_attempt(hot.record["attempt_id"])["status"] == "cancelled"
        fresh = hot.repo.create_attempt(hot.record["agent_run_id"])
        _write(hot.store, replace(hot.scope, attempt_id=fresh["attempt_id"]), "bg-resumed")
    finally:
        release.set()
    assert cleaned.wait(3) and not failures and observed == ["bg-main"]
    assert not hot.store.load("bg-neighbor").record["stop_requested"]
    assert not hot.store.load("bg-resumed").record["stop_requested"]
    assert hot.agent.conversation_store.tasks.load("task") is None


def test_interrupt_without_durable_link_preserves_main_resources_and_authority(hot, monkeypatch):
    _publish(hot)
    _write(hot.store, hot.scope, "bg-main")
    freeze = Mock(side_effect=AssertionError("中断不能冻结资源"))
    monkeypatch.setattr(resources, "freeze_process_stop", freeze)
    assert _stop(hot, interrupt_only=True).ok
    assert hot.repo.get_attempt(hot.record["attempt_id"])["status"] == "running"
    assert not hot.store.load("bg-main").record["stop_requested"]
    freeze.assert_not_called()


def test_stop_before_publication_closes_publish_gate_without_guessing_historical_main(hot):
    _write(hot.store, hot.scope, "bg-history")
    result = _stop(hot)
    assert not result.ok and result.delivery_status == "unknown"
    assert result.error_code == "TASK_RESOURCE_STOP_UNCONFIRMED"
    assert not hot.store.load("bg-history").record["stop_requested"]
    assert hot.repo.get_attempt(hot.record["attempt_id"])["status"] == "running"
    with pytest.raises(InterruptedError):
        _bind_main_agent_authority(hot.agent, RunParams(
            request_id="message", run_id="unstarted-run", task_id="unstarted-task",
            conversation_task_binding_callback=hot.writer,
        ))
    row = hot.repo.agent_run_for_run_id("unstarted-run")
    assert hot.repo.current_attempt(row["agent_run_id"])["status"] == "cancelled"
    assert hot.agent.conversation_store.tasks.load("unstarted-task") is None


@pytest.mark.parametrize("fault", ["corrupt_binding", "wrong_task", "stale_attempt", "unmanaged", "owner_unavailable", "thread_unavailable"])
def test_unconfirmed_binding_cannot_sweep_another_run_or_claim_resource_exit(hot, monkeypatch, fault):
    _publish(hot)
    _write(hot.store, hot.scope, "bg-main")
    payload = read_json_file(hot.path)
    if fault == "corrupt_binding":
        payload["runtime_authority"]["request_id"] = "other"
    elif fault == "wrong_task":
        payload["runtime_authority"]["task_id"] = "other"
    elif fault == "stale_attempt":
        hot.repo.create_attempt(hot.record["agent_run_id"])
    elif fault == "unmanaged":
        monkeypatch.setattr(hot.agent.subagents, "runtime_db", None)
    elif fault == "thread_unavailable":
        monkeypatch.setattr(hot.agent.conversation_store.threads, "resolve", lambda **_kwargs: None)
    else:
        monkeypatch.setattr(control_service, "_request_agent_for_scope", Mock(side_effect=OSError("unavailable")))
    write_json_file(hot.path, payload)
    result = _stop(hot)
    assert not result.ok and result.delivery_status == "unknown"
    assert not hot.store.load("bg-main").record["stop_requested"]
    assert hot.repo.current_attempt(hot.record["agent_run_id"])["status"] != "cancelled"


def test_old_discovery_cannot_interrupt_a_new_transport_generation(hot, monkeypatch):
    current = {**hot.payload, "execution_attempt_id": "resumed-transport"}
    write_json_file(hot.path, current)
    signal = Mock(side_effect=AssertionError("旧回执不能向新运输代次发信号"))
    monkeypatch.setattr(control_service, "interrupt_by_name", signal)
    assert not _stop(hot).ok
    assert read_json_file(hot.path) == current
    signal.assert_not_called()


def test_durable_stop_also_rejects_transport_changed_after_discovery(hot, monkeypatch):
    _publish(hot)
    old = _GatewayRequestRecord(hot.path, read_json_file(hot.path))
    link = hot.agent.conversation_store.tasks.bind({"task_id": "task", "thread_id": hot.scope.thread_id, "status": "active"})
    current = read_json_file(hot.path)
    current["execution_attempt_id"] = "resumed-transport"
    current["runtime_authority"]["gateway_execution_attempt_id"] = "resumed-transport"
    write_json_file(hot.path, current)
    signal = Mock(side_effect=AssertionError("旧回执不能向新代次发信号"))
    monkeypatch.setattr(control_service, "interrupt_by_name", signal)
    active = _GatewayRequestRecord(None, {
        "id": "task", "conversation_task_link_status": "active", "conversation_thread_id": link.thread_id,
    }, target_kind="task", linked_request=old)
    result = control_service._stop_active_task(hot.agent, active, hot.control_scope)
    assert not result.ok and result.delivery_status == "unknown"
    assert hot.repo.get_attempt(hot.record["attempt_id"])["status"] == "running"
    assert hot.agent.conversation_store.tasks.load("task").status == "active"
    signal.assert_not_called()


@pytest.mark.parametrize("late_link", [False, True])
def test_promoted_task_cannot_be_reclassified_as_unpromoted_on_lookup_failure(hot, monkeypatch, late_link):
    _publish(hot)
    _write(hot.store, hot.scope, "bg-main")
    if not late_link:
        payload = read_json_file(hot.path)
        payload["conversation_runtime"] = {"task_id": "task", "thread_id": hot.scope.thread_id, "request_id": "message"}
        write_json_file(hot.path, payload)
    # 故意不发布 request 的 conversation_runtime，模拟 canonical link 已先落盘的窗口。
    hot.agent.conversation_store.tasks.bind({"task_id": "task", "thread_id": hot.scope.thread_id, "goal": "继续", "status": "active"})
    hot.agent.conversation_store.goals.create({"task_id": "task", "thread_id": hot.scope.thread_id, "objective": "继续"})
    if not late_link:
        monkeypatch.setattr(hot.agent.conversation_store.tasks, "list_report", Mock(side_effect=OSError("unreadable")))
    result = _stop(hot)
    assert not result.ok and result.delivery_status == "unknown"
    assert hot.repo.get_attempt(hot.record["attempt_id"])["status"] == "running"
    assert not hot.store.load("bg-main").record["stop_requested"]
    assert hot.agent.conversation_store.tasks.load("task").status == "active"


def test_unreadable_canonical_task_is_not_treated_as_unpromoted(hot, monkeypatch):
    _publish(hot)
    _write(hot.store, hot.scope, "bg-main")
    monkeypatch.setattr(hot.agent.conversation_store.tasks, "load", Mock(side_effect=OSError("unreadable")))
    result = _stop(hot)
    assert not result.ok and result.delivery_status == "unknown"
    assert not hot.store.load("bg-main").record["stop_requested"]


def test_promotion_between_closing_and_task_guard_is_not_acknowledged_as_complete(hot, monkeypatch):
    _publish(hot)
    _write(hot.store, hot.scope, "bg-main")
    closed = threading.Event()
    original = control_service._mark_request_stopping_locked
    outcomes, failures = [], []

    def mark(record, scope):
        result = original(record, scope)
        closed.set()
        return result

    def stop():
        try:
            outcomes.append(_stop(hot))
        except BaseException as exc:
            failures.append(exc)

    monkeypatch.setattr(control_service, "_mark_request_stopping_locked", mark)
    stopping = threading.Thread(target=stop, daemon=True)
    try:
        with hot.agent.conversation_store.tasks.transition_guard("task"):
            stopping.start()
            assert closed.wait(3)
            hot.agent.conversation_store.tasks.bind({"task_id": "task", "thread_id": hot.scope.thread_id, "status": "active"})
    finally:
        stopping.join(4)
    assert not stopping.is_alive() and not failures, failures
    assert len(outcomes) == 1 and not outcomes[0].ok and outcomes[0].delivery_status == "unknown"
    assert hot.repo.get_attempt(hot.record["attempt_id"])["status"] == "running"
    assert not hot.store.load("bg-main").record["stop_requested"]


def test_stop_releases_request_lock_before_waiting_for_task_guard(hot, monkeypatch):
    _publish(hot)
    holding_task, request_closed, finished = threading.Event(), threading.Event(), threading.Event()
    outcomes, failures = [], []
    original = control_service._mark_request_stopping_locked

    def mark(record, scope):
        result = original(record, scope)
        request_closed.set()
        return result

    monkeypatch.setattr(control_service, "_mark_request_stopping_locked", mark)
    monkeypatch.setattr(resources, "cleanup_process_stop", lambda _batch: None)

    def binder():
        try:
            with hot.agent.conversation_store.tasks.transition_guard("task"):
                holding_task.set()
                assert request_closed.wait(3)
                with gateway_turn_transition(hot.paths, "message"):
                    assert read_json_file(hot.path)["turn_phase"] == "closing"
        except BaseException as exc:
            failures.append(exc)

    def stop():
        try:
            outcomes.append(_stop(hot))
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()

    binding = threading.Thread(target=binder, daemon=True)
    stopping = threading.Thread(target=stop, daemon=True)
    binding.start()
    try:
        assert holding_task.wait(3)
        stopping.start()
        assert finished.wait(4)
    finally:
        request_closed.set()
        binding.join(4)
        if stopping.ident is not None:
            stopping.join(4)
    assert not binding.is_alive() and not stopping.is_alive() and not failures, failures
    assert len(outcomes) == 1 and outcomes[0].ok
