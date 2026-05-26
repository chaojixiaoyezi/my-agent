from __future__ import annotations

import json

from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.core import SimpleAgent


def test_coordinator_wakes_main_agent_when_collaboration_request_is_blocked(tmp_path) -> None:
    from agent_py_agent.agent.collaboration import CollaborationCoordinator

    conversation, thread, store, case = _blocked_collaboration_fixture(tmp_path)

    decisions = CollaborationCoordinator(store=store, conversation_store=conversation).tick(now=13.0)

    assert len(decisions) == 1
    assert decisions[0].requires_main_agent is True
    assert decisions[0].metadata["blocked_request_count"] == 1
    assert store.load_case(case.case_id).status == "close"
    wake = conversation.pending_wake_signals()[0]
    assert wake.thread_id == thread.thread_id
    assert wake.urgency == "normal"


def test_coordinator_closes_urgent_case_to_existing_wake_queue(tmp_path) -> None:
    from agent_py_agent.agent.collaboration import CollaborationCoordinator

    conversation, thread, store, case = _urgent_evidence_fixture(tmp_path)

    decisions = CollaborationCoordinator(store=store, conversation_store=conversation).tick(now=13.0)

    assert len(decisions) == 1
    assert decisions[0].requires_main_agent is True
    assert store.load_case(case.case_id).status == "close"
    wake = conversation.pending_wake_signals()[0]
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
    assert status["missing_responder_agent_ids_by_request"] == {
        request.request_id: ["source-b"],
    }


def _conversation_with_task(tmp_path, *, goal: str):
    conversation = ConversationStore(tmp_path / "conversations")
    thread = conversation.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 1.0})
    conversation.bind_task({'thread_id': thread.thread_id, 'task_id': "task-1", 'goal': goal, 'now': 2.0})
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
