from __future__ import annotations

import json

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


def test_raise_collaboration_uses_current_subagent_run_when_model_invents_task_id_without_thread(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    child = agent.subagents.create_run(
        goal="发现线索后打开协作 case。",
        agent_name="clue-requester",
        allowed_tools=["raise_collaboration"],
    )
    agent._current_subagent_run_id = child.id
    try:
        payload = json.loads(
            agent.tools.tools["raise_collaboration"].execute(
                {
                    "task_id": "task-clue-1",
                    "title": "通用线索 case",
                    "summary": "模型传了错误 task_id，工具应使用当前 runner 真实 run_id。",
                    "created_by": child.id,
                }
            ).output
        )
    finally:
        delattr(agent, "_current_subagent_run_id")

    thread = agent.conversation_store.thread_for_task(child.id)
    status = json.loads(agent.tools.tools["inspect_collaboration"].execute({"case_id": payload["case_id"]}).output)

    assert payload["task_id"] == child.id
    assert payload["scope_resolution"]["effective"]["agent_id"] == child.id
    assert thread is not None
    assert status["case"]["task_id"] == child.id


def test_collaboration_tools_scope_actor_to_current_runner_when_explicit_id_conflicts(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    child = agent.subagents.create_run(
        goal="发起协作并提交证据。",
        agent_name="real-runner",
        allowed_tools=["raise_collaboration", "raise_collaboration", "submit_collaboration_result"],
    )
    target = agent.subagents.create_run(goal="响应协作。", agent_name="target-runner")
    agent._current_subagent_run_id = child.id
    try:
        case_payload = json.loads(
            agent.tools.tools["raise_collaboration"].execute(
                {
                    "task_id": child.id,
                    "title": "身份冲突 case",
                    "summary": "模型传错 created_by 也不能冒充别的 run。",
                    "created_by": "invented-agent",
                }
            ).output
        )
        request_payload = json.loads(
            agent.tools.tools["raise_collaboration"].execute(
                {
                    "case_id": case_payload["case_id"],
                    "requester_agent_id": "invented-agent",
                    "target_agent_ids": [target.id],
                    "question": "请补充证据。",
                }
            ).output
        )
        evidence_payload = json.loads(
            agent.tools.tools["submit_collaboration_result"].execute(
                {
                    "case_id": case_payload["case_id"],
                    "request_id": request_payload["request_id"],
                    "source_agent_id": "invented-agent",
                    "summary": "提交当前 runner 的证据。",
                    "evidence_refs": ["artifact://real-runner/e1"],
                }
            ).output
        )
    finally:
        delattr(agent, "_current_subagent_run_id")

    status = json.loads(agent.tools.tools["inspect_collaboration"].execute({"case_id": case_payload["case_id"]}).output)
    assert status["case"]["created_by"] == child.id
    assert status["requests"][0]["requester_agent_id"] == child.id
    assert status["evidence"][0]["source_agent_id"] == child.id
    assert "explicit_scope_overridden_by_current_runner" in request_payload["scope_warnings"]
    assert request_payload["scope_resolution"]["ignored_explicit"]["requester_agent_id"] == "invented-agent"
    assert evidence_payload["scope_resolution"]["ignored_explicit"]["source_agent_id"] == "invented-agent"


def test_request_status_update_can_structurally_reroute_target_agents(tmp_path) -> None:
    from agent_py_agent.agent.collaboration import AgentCapability, CollaborationStore

    store = CollaborationStore(tmp_path / "collaboration")
    store.register_agent(AgentCapability(agent_id="source-a", capabilities=("query",)))
    store.register_agent(AgentCapability(agent_id="source-b", capabilities=("query",)))
    case = store.open_case({'thread_id': "thread-1", 'task_id': "task-1", 'title': "需要换路的请求", 'summary': "原目标不可用，但有替代目标。", 'created_by': "main", 'now': 1.0})
    request = store.request_collaboration({'case_id': case.case_id, 'requester_agent_id': "main", 'target_agent_ids': ("source-a",), 'required_capabilities': ("query",), 'question': "请查询这个线索。", 'now': 2.0})

    updated = store.update_request_status({'case_id': case.case_id, 'request_id': request.request_id, 'status': "pending", 'actor_agent_id': "main", 'summary': "source-a 失败，换到 source-b 继续查。", 'target_agent_ids': ("source-b",), 'metadata': {"reroute_reason": "source-a unavailable"}, 'now': 3.0})
    status = store.case_status(case.case_id)

    assert updated.target_agent_ids == ("source-b",)
    assert status["requests"][0]["target_agent_ids"] == ["source-b"]
    assert status["requests"][0]["metadata"]["original_target_agent_ids"] == ["source-a"]
    assert status["requests"][0]["metadata"]["rerouted_from"] == ["source-a"]
    assert status["requests"][0]["metadata"]["rerouted_to"] == ["source-b"]
    assert any(item.agent_id == "source-b" for item in store.case_participants(case.case_id))


def test_pending_requests_for_agent_returns_targeted_unanswered_request_refs(tmp_path) -> None:
    from agent_py_agent.agent.collaboration import CollaborationStore

    store = CollaborationStore(tmp_path / "collaboration")
    case = store.open_case({'thread_id': "thread-1", 'task_id': "task-1", 'title': "需要指定响应者", 'summary': "A 需要 B 补证据。", 'created_by': "agent-a", 'now': 1.0})
    pending = store.request_collaboration({'case_id': case.case_id, 'requester_agent_id': "agent-a", 'target_agent_ids': ("agent-b",), 'required_capabilities': ("query",), 'question': "请补一条证据引用。", 'now': 2.0})
    completed = store.request_collaboration({'case_id': case.case_id, 'requester_agent_id': "agent-a", 'target_agent_ids': ("Responder B",), 'question': "这条已经有证据。", 'now': 3.0})
    store.submit_evidence({'case_id': case.case_id, 'request_id': completed.request_id, 'source_agent_id': "agent-b", 'summary': "已有证据。", 'evidence_refs': ("artifact://agent-b/done",), 'now': 4.0})

    requests = store.pending_requests_for_agent(agent_id="agent-b", agent_name="Responder B")

    assert [item["request_id"] for item in requests] == [pending.request_id]
    assert requests[0]["case_id"] == case.case_id
    assert requests[0]["case_ref"] == f"collaboration://case/{case.case_id}"
    assert requests[0]["request_ref"] == f"collaboration://request/{pending.request_id}"
    assert requests[0]["recommended_tools"] == [
        "inspect_collaboration",
        "submit_collaboration_result",
        "update_collaboration",
    ]


def test_pending_requests_for_agent_does_not_guess_generated_agent_name(tmp_path) -> None:
    from agent_py_agent.agent.collaboration import CollaborationStore

    store = CollaborationStore(tmp_path / "collaboration")
    case = store.open_case({'thread_id': "thread-1", 'task_id': "task-1", 'title': "系统追加序号后的代理名仍应可响应", 'created_by': "agent-a", 'now': 1.0})
    request = store.request_collaboration({'case_id': case.case_id, 'requester_agent_id': "agent-a", 'target_agent_ids': ("Agent-B",), 'question': "请 B 补证据。", 'now': 2.0})

    requests = store.pending_requests_for_agent(agent_id="subagent-2", agent_name="Agent-B-2")

    assert request.request_id
    assert requests == []


def test_pending_requests_for_agent_matches_agent_role_identity(tmp_path) -> None:
    from agent_py_agent.agent.collaboration import CollaborationStore

    store = CollaborationStore(tmp_path / "collaboration")
    case = store.open_case({'thread_id': "thread-1", 'task_id': "task-1", 'title': "按角色点名", 'created_by': "agent-a", 'now': 1.0})
    request = store.request_collaboration({'case_id': case.case_id, 'requester_agent_id': "agent-a", 'target_agent_ids': ("agent-b",), 'question': "请 B 角色补证据。", 'now': 2.0})

    requests = store.pending_requests_for_agent(
        agent_id="agent-b",
        agent_name="Agent-B-2",
        agent_role="agent-b",
    )

    assert [item["request_id"] for item in requests] == [request.request_id]


def test_pending_requests_for_agent_ignores_closed_cases(tmp_path) -> None:
    from agent_py_agent.agent.collaboration import CollaborationStore

    store = CollaborationStore(tmp_path / "collaboration")
    case = store.open_case({'thread_id': "thread-1", 'task_id': "task-1", 'title': "已关闭的协作窗口", 'created_by': "agent-a", 'now': 1.0})
    store.request_collaboration({'case_id': case.case_id, 'requester_agent_id': "agent-a", 'target_agent_ids': ("agent-b",), 'question': "这条请求已经随 case 关闭。", 'now': 2.0})
    store.update_case_status(case.case_id, status="close", now=3.0)

    assert store.pending_requests_for_agent(agent_id="agent-b") == []


def test_multi_target_request_tracks_each_responder_until_all_submit_collaboration_result(tmp_path) -> None:
    from agent_py_agent.agent.collaboration import CollaborationStore

    store = CollaborationStore(tmp_path / "collaboration")
    case = store.open_case({'thread_id': "thread-1", 'title': "multi-target case"})
    request = store.request_collaboration({'case_id': case.case_id, 'requester_agent_id': "requester", 'target_agent_ids': ("agent-a", "agent-b"), 'question': "请各自检查自己负责的来源。", 'now': 1.0})

    assert store.pending_requests_for_agent(agent_id="agent-a")
    assert store.pending_requests_for_agent(agent_id="agent-b")

    _submit_agent_evidence(store, (case.case_id, request.request_id, "agent-a"), matched=True, now=2.0)
    store.update_request_status({'case_id': case.case_id, 'request_id': request.request_id, 'status': "completed", 'actor_agent_id': "agent-a", 'summary': "agent-a done", 'now': 3.0})

    assert store.pending_requests_for_agent(agent_id="agent-a") == []
    assert store.pending_requests_for_agent(agent_id="agent-b")
    partial = store.case_status(case.case_id)
    assert partial["pending_request_count"] == 1
    assert partial["completed_request_count"] == 0
    assert partial["missing_evidence_request_ids"] == [request.request_id]

    _submit_agent_evidence(store, (case.case_id, request.request_id, "agent-b"), matched=False, now=4.0)
    done = store.case_status(case.case_id)

    assert store.pending_requests_for_agent(agent_id="agent-b") == []
    assert done["pending_request_count"] == 0
    assert done["completed_request_count"] == 1
    assert done["missing_evidence_request_ids"] == []


def _submit_agent_evidence(store, refs: tuple[str, str, str], *, matched: bool, now: float) -> None:
    case_id, request_id, agent_id = refs
    store.submit_evidence({'case_id': case_id, 'request_id': request_id, 'source_agent_id': agent_id, 'matched': matched, 'summary': f"{agent_id} checked", 'evidence_refs': (f"artifact://{agent_id}/evidence",), 'now': now})
