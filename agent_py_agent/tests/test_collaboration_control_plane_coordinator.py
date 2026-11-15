from __future__ import annotations

import errno
import logging
import os
from pathlib import Path

import pytest

from agent_py_agent.agent.collaboration import (
    AgentCapability,
    CollaborationCoordinator,
)
from agent_py_agent.agent.collaboration import (
    store_common as collab_store_common_mod,
)
from agent_py_agent.agent.collaboration.handoff_board import HandoffBoardService
from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
    ConversationStore,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


def test_coordinator_wakes_main_agent_when_collaboration_request_is_blocked(tmp_path) -> None:
    from agent_py_agent.agent.collaboration import CollaborationCoordinator

    conversation, thread, store, case = _blocked_collaboration_fixture(tmp_path)

    decisions = CollaborationCoordinator(store=store, conversation_store=conversation).tick(now=13.0)

    assert len(decisions) == 1
    assert decisions[0].requires_main_agent is True
    assert decisions[0].metadata["blocked_request_count"] == 1
    assert store.load_case(case.case_id).status == "closed"
    wake = conversation.wakes.pending()[0]
    assert wake.thread_id == thread.thread_id
    assert wake.urgency == "normal"


def test_coordinator_closes_urgent_case_to_existing_wake_queue(tmp_path) -> None:
    from agent_py_agent.agent.collaboration import CollaborationCoordinator

    conversation, thread, store, case = _urgent_evidence_fixture(tmp_path)

    decisions = CollaborationCoordinator(store=store, conversation_store=conversation).tick(now=13.0)

    assert len(decisions) == 1
    assert decisions[0].requires_main_agent is True
    assert store.load_case(case.case_id).status == "closed"
    wake = conversation.wakes.pending()[0]
    assert wake.thread_id == thread.thread_id
    assert wake.root_task_id == "task-1"
    assert wake.summary.startswith("协作 case 收集窗口已关闭")


def test_coordinator_marks_expired_request_timeout_before_escalation(tmp_path) -> None:
    from agent_py_agent.agent.collaboration import CollaborationCoordinator

    conversation, _thread, store, case, request = _expired_request_fixture(tmp_path)

    decisions = CollaborationCoordinator(store=store, conversation_store=conversation).tick(now=11.0)
    status = store.case_status(case.case_id)
    requests = {item["request_id"]: item for item in status["requests"]}

    assert len(decisions) == 1
    assert requests[request.request_id]["status"] == "timeout"
    assert "deadline_at" in requests[request.request_id]["metadata"]["timeout"]
    assert status["blocked_request_count"] == 0
    assert status["timed_out_request_count"] == 1
    assert status["timed_out_request_ids"] == [request.request_id]
    assert status["response_coverage"]["missing_target_count"] == 1
    assert status["missing_responder_agent_ids_by_request"] == {
        request.request_id: ["source-b"],
    }
    assert decisions[0].metadata["response_coverage"]["missing_target_count"] == 1


def _conversation_with_task(tmp_path, *, goal: str):
    conversation = ConversationStore(tmp_path / "conversations")
    thread = conversation.threads.get_or_create({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 1.0})
    conversation.tasks.bind({'thread_id': thread.thread_id, 'task_id': "task-1", 'goal': goal, 'now': 2.0})
    return conversation, thread


def _blocked_collaboration_fixture(tmp_path):
    from agent_py_agent.agent.collaboration import AgentCapability, CollaborationStore

    conversation, thread = _conversation_with_task(tmp_path, goal="长期协作任务")
    store = CollaborationStore(tmp_path / "collaboration")
    store.register_agent(AgentCapability(agent_id="source-b", capabilities=("query",)))
    case = store.open_case({'thread_id': thread.thread_id, 'task_id': "task-1", 'title': "响应者阻塞", 'summary': "需要主代理知道协作阻塞。", 'priority': "normal", 'created_by': "source-a", 'now': 10.0})
    request = store.request_collaboration({'case_id': case.case_id, 'requester_agent_id': "source-a", 'target_agent_ids': ("source-b",), 'required_capabilities': ("query",), 'question': "请补充证据。", 'now': 11.0})
    store.update_request_status({'case_id': case.case_id, 'request_id': request.request_id, 'status': "blocked", 'actor_agent_id': "source-b", 'summary': "目标来源不可访问，需要主代理换策略。", 'now': 12.0})
    return conversation, thread, store, case


def _urgent_evidence_fixture(tmp_path):
    from agent_py_agent.agent.collaboration import AgentCapability, CollaborationStore

    conversation, thread = _conversation_with_task(tmp_path, goal="长期协作任务")
    store = CollaborationStore(tmp_path / "collaboration")
    store.register_agent(AgentCapability(agent_id="source-a", capabilities=("query", "api")))
    store.register_agent(AgentCapability(agent_id="source-b", capabilities=("query", "database")))
    case = store.open_case({'thread_id': thread.thread_id, 'task_id': "task-1", 'title': "需要主代理处理的协作事件", 'summary': "跨源证据已经足够，需要主代理判断。", 'priority': "urgent", 'created_by': "source-a", 'required_capabilities': ("query",), 'now': 10.0})
    request = store.request_collaboration({'case_id': case.case_id, 'requester_agent_id': "source-a", 'required_capabilities': ("query",), 'question': "补充证据。", 'deadline_at': 30.0, 'now': 11.0})
    store.submit_evidence({'case_id': case.case_id, 'request_id': request.request_id, 'source_agent_id': "source-b", 'matched': True, 'summary': "补充证据支持升级。", 'evidence_refs': ("artifact://source-b/evidence-1",), 'confidence': 0.9, 'now': 12.0})
    return conversation, thread, store, case


def _expired_request_fixture(tmp_path):
    from agent_py_agent.agent.collaboration import AgentCapability, CollaborationStore

    conversation, thread = _conversation_with_task(tmp_path, goal="协作超时任务")
    store = CollaborationStore(tmp_path / "collaboration")
    store.register_agent(AgentCapability(agent_id="source-b", capabilities=("query",)))
    case = store.open_case({'thread_id': thread.thread_id, 'task_id': "task-1", 'title': "超时协作 case", 'summary': "请求过期后要留下结构化状态。", 'priority': "normal", 'created_by': "source-a", 'now': 3.0})
    request = store.request_collaboration({'case_id': case.case_id, 'requester_agent_id': "source-a", 'target_agent_ids': ("source-b",), 'question': "请补充证据。", 'deadline_at': 10.0, 'now': 4.0})
    return conversation, thread, store, case, request


def _owner_two_case_handoff_fixture(tmp_path: Path):
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            enable_tools=False,
            my_agent_home=str(tmp_path / "home"),
        ),
        tmp_path,
    )
    conversation = agent.conversation_store
    store = agent.collaboration_store
    service = HandoffBoardService(store=store, workspace_root=tmp_path)
    store.register_agent(AgentCapability(agent_id="source-b", capabilities=("query",)))

    thread_a = conversation.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-a",
            "channel_user_id": "user-1",
            "now": 1.0,
        }
    )
    conversation.tasks.bind(
        {"thread_id": thread_a.thread_id, "task_id": "task-a", "goal": "协作任务 A", "now": 2.0}
    )
    thread_b = conversation.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-b",
            "channel_user_id": "user-1",
            "now": 3.0,
        }
    )
    conversation.tasks.bind(
        {"thread_id": thread_b.thread_id, "task_id": "task-b", "goal": "协作任务 B", "now": 4.0}
    )

    case_a = store.open_case(
        {
            "thread_id": thread_a.thread_id,
            "task_id": "task-a",
            "title": "协作事件 A",
            "summary": "Case A ready for coordinator",
            "priority": "normal",
            "created_by": "source-a",
            "now": 10.0,
        }
    )
    req_a = store.request_collaboration(
        {
            "case_id": case_a.case_id,
            "requester_agent_id": "source-a",
            "target_agent_ids": ("source-b",),
            "required_capabilities": ("query",),
            "question": "请补充 A 证据。",
            "deadline_at": 25.0,
            "now": 11.0,
        }
    )
    item_a = service.publish_item(
        case_id=case_a.case_id,
        request_id=req_a.request_id,
        responsible_actor="worker-a",
        frozen_input_version="v1",
        idempotency_key="case-a-item",
        now=13.0,
    )

    case_b = store.open_case(
        {
            "thread_id": thread_b.thread_id,
            "task_id": "task-b",
            "title": "协作事件 B",
            "summary": "Case B ready for coordinator",
            "priority": "normal",
            "created_by": "source-a",
            "now": 20.0,
        }
    )
    req_b = store.request_collaboration(
        {
            "case_id": case_b.case_id,
            "requester_agent_id": "source-a",
            "target_agent_ids": ("source-b",),
            "required_capabilities": ("query",),
            "question": "请补充 B 证据。",
            "now": 21.0,
        }
    )
    store.update_request_status(
        {
            "case_id": case_b.case_id,
            "request_id": req_b.request_id,
            "status": "blocked",
            "actor_agent_id": "source-b",
            "summary": "Case B target unavailable",
            "now": 22.0,
        }
    )
    item_b = service.publish_item(
        case_id=case_b.case_id,
        request_id=req_b.request_id,
        responsible_actor="worker-b",
        frozen_input_version="v1",
        idempotency_key="case-b-item",
        now=23.0,
    )

    runtime = BackgroundMainAgentRuntime(agent=agent, store=conversation)
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": conversation})
    return {
        "agent": agent,
        "conversation": conversation,
        "store": store,
        "scheduler": scheduler,
        "thread_a": thread_a,
        "thread_b": thread_b,
        "case_a": case_a,
        "case_b": case_b,
        "req_a": req_a,
        "req_b": req_b,
        "item_a": item_a,
        "item_b": item_b,
    }


def _assert_case_a_deferred_and_case_b_closed(env: dict, caplog: pytest.LogCaptureFixture, *, now: float) -> None:
    store = env["store"]
    conversation = env["conversation"]
    scheduler = env["scheduler"]
    case_a = env["case_a"]
    case_b = env["case_b"]
    req_b = env["req_b"]
    item_b = env["item_b"]
    thread_a = env["thread_a"]
    thread_b = env["thread_b"]

    assert any(
        "COLLABORATION_CASE_DEFERRED" in rec.message and case_a.case_id in rec.message
        for rec in caplog.records
    )
    assert store.load_case(case_a.case_id).status == "open"
    assert [
        d for d in store.case_decisions(case_a.case_id) if d.decision_type == "close_collaboration_window"
    ] == []
    assert conversation.observations.recent(thread_a.thread_id, limit=10) == []

    assert store.load_case(case_b.case_id).status == "closed"
    decisions_b = [
        d for d in store.case_decisions(case_b.case_id) if d.decision_type == "close_collaboration_window"
    ]
    assert len(decisions_b) == 1
    assert decisions_b[0].metadata["handoff_item_ids"] == [item_b.item_id]
    assert decisions_b[0].metadata["blocked_request_count"] == 1
    assert [r.request_id for r in store.case_requests(case_b.case_id)] == [req_b.request_id]

    obs_b = conversation.observations.recent(thread_b.thread_id, limit=10)
    assert any(event.event_type == "collaboration_case_closed" for event in obs_b)
    pending_wakes = conversation.wakes.pending(limit=0)
    assert [wake.thread_id for wake in pending_wakes] == [thread_b.thread_id]
    assert pending_wakes[0].reason == "collaboration_case_closed"
    assert pending_wakes[0].metadata["case_id"] == case_b.case_id
    assert thread_b.thread_id in scheduler.ready_thread_ids(now=now)
    assert thread_a.thread_id not in scheduler.ready_thread_ids(now=now)


def _assert_case_a_recovered(env: dict, *, now: float) -> None:
    store = env["store"]
    conversation = env["conversation"]
    scheduler = env["scheduler"]
    case_a = env["case_a"]
    req_a = env["req_a"]
    item_a = env["item_a"]
    thread_a = env["thread_a"]

    assert store.load_case(case_a.case_id).status == "closed"
    decisions_a = [
        d for d in store.case_decisions(case_a.case_id) if d.decision_type == "close_collaboration_window"
    ]
    assert len(decisions_a) == 1
    assert decisions_a[0].metadata["handoff_item_ids"] == [item_a.item_id]
    assert decisions_a[0].metadata["missing_responder_agent_ids_by_request"] == {
        req_a.request_id: ["source-b"]
    }
    assert [(it.item_id, it.request_id) for it in store.case_handoff_items(case_a.case_id)] == [
        (item_a.item_id, req_a.request_id)
    ]
    assert [r.request_id for r in store.case_requests(case_a.case_id)] == [req_a.request_id]

    obs_a = conversation.observations.recent(thread_a.thread_id, limit=10)
    assert any(event.event_type == "collaboration_case_closed" for event in obs_a)
    wakes_a = [
        wake for wake in conversation.wakes.pending(limit=0) if wake.thread_id == thread_a.thread_id
    ]
    assert len(wakes_a) == 1
    assert wakes_a[0].reason == "collaboration_case_closed"
    assert wakes_a[0].metadata["missing_responder_agent_ids_by_request"] == {
        req_a.request_id: ["source-b"]
    }
    assert thread_a.thread_id in scheduler.ready_thread_ids(now=now)


@pytest.mark.skipif(os.geteuid() == 0, reason="non-root chmod permission test requires euid != 0")
def test_scheduler_prepare_tick_defers_chmod_unreadable_handoff_and_recovers(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    env = _owner_two_case_handoff_fixture(tmp_path)
    handoff_path_a = env["store"]._handoff_items_path(env["case_a"].case_id)
    assert handoff_path_a.exists()

    try:
        handoff_path_a.chmod(0o000)
        with caplog.at_level(logging.WARNING, logger="agent_py_agent.agent.collaboration.coordinator"):
            env["scheduler"].prepare_tick(now=30.0)
        _assert_case_a_deferred_and_case_b_closed(env, caplog, now=30.0)
    finally:
        handoff_path_a.chmod(0o600)

    env["scheduler"].prepare_tick(now=31.0)
    _assert_case_a_recovered(env, now=31.0)


def test_scheduler_prepare_tick_defers_handoff_read_eio_and_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    env = _owner_two_case_handoff_fixture(tmp_path)
    target_handoff_path = env["store"]._handoff_items_path(env["case_a"].case_id)
    orig_cached_read = collab_store_common_mod.read_text_lines_cached
    io_error_active = {"active": True}

    def flaky_read_text_lines_cached(path: Path) -> tuple[str, ...]:
        if path == target_handoff_path and io_error_active["active"]:
            raise OSError(errno.EIO, "simulated handoff ledger read EIO")
        return orig_cached_read(path)

    monkeypatch.setattr(collab_store_common_mod, "read_text_lines_cached", flaky_read_text_lines_cached)

    with caplog.at_level(logging.WARNING, logger="agent_py_agent.agent.collaboration.coordinator"):
        env["scheduler"].prepare_tick(now=30.0)
    _assert_case_a_deferred_and_case_b_closed(env, caplog, now=30.0)

    io_error_active["active"] = False
    env["scheduler"].prepare_tick(now=31.0)
    _assert_case_a_recovered(env, now=31.0)


def test_coordinator_tick_propagates_non_oserror_from_tick_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    conversation, _thread, store, case = _blocked_collaboration_fixture(tmp_path)

    def fail_invariant(_case_id: str):
        raise RuntimeError("unexpected invariant check")

    monkeypatch.setattr(store, "case_handoff_items", fail_invariant)
    coordinator = CollaborationCoordinator(store=store, conversation_store=conversation)

    with caplog.at_level(logging.WARNING, logger="agent_py_agent.agent.collaboration.coordinator"):
        with pytest.raises(RuntimeError, match="unexpected invariant check"):
            coordinator.tick(now=13.0)

    assert "COLLABORATION_CASE_DEFERRED" not in caplog.text
    assert store.load_case(case.case_id).status == "open"
