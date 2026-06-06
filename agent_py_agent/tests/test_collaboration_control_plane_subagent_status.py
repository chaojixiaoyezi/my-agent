from __future__ import annotations

import json

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


def test_subagent_execution_context_includes_targeted_collaboration_requests(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    child = agent.subagents.create_run(
        goal="响应已有协作请求，提交证据并更新请求状态。",
        agent_name="Responder B",
        allowed_tools=["inspect_collaboration", "submit_collaboration_result", "update_collaboration"],
        acceptance_checks=["复用已有 case/request 完成响应"],
    )
    case = agent.collaboration_store.open_case({'thread_id': "thread-1", 'task_id': "task-1", 'title': "需要 B 补证据", 'summary': "A 已打开 case，等待 B 响应。", 'created_by': "agent-a", 'now': 1.0})
    request = agent.collaboration_store.request_collaboration({'case_id': case.case_id, 'requester_agent_id': "agent-a", 'target_agent_ids': (child.id,), 'question': "请补一条证据引用。", 'now': 2.0})

    context = agent.subagents.write_execution_context(child.id)

    collaboration = context.context_bundle.get("collaboration")
    assert isinstance(collaboration, dict)
    targeted = collaboration["targeted_requests"]
    assert targeted[0]["case_id"] == case.case_id
    assert targeted[0]["request_id"] == request.request_id
    assert targeted[0]["request_ref"] == f"collaboration://request/{request.request_id}"


def test_subagent_execution_context_includes_role_targeted_collaboration_requests(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    child = agent.subagents.create_run(
        goal="响应已有协作请求。",
        agent_name="Responder",
        role="agent-b",
        allowed_tools=["inspect_collaboration", "submit_collaboration_result", "update_collaboration"],
    )
    case = agent.collaboration_store.open_case({'thread_id': "thread-1", 'task_id': "task-1", 'title': "需要 agent-b 补证据", 'created_by': "agent-a", 'now': 1.0})
    request = agent.collaboration_store.request_collaboration({'case_id': case.case_id, 'requester_agent_id': "agent-a", 'target_agent_ids': ("agent-b",), 'question': "请 agent-b 补一条证据引用。", 'now': 2.0})

    context = agent.subagents.write_execution_context(child.id)

    targeted = context.context_bundle["collaboration"]["targeted_requests"]
    assert targeted[0]["request_id"] == request.request_id


def test_dispatch_candidates_include_done_agent_with_new_collaboration_request(tmp_path) -> None:
    from agent_py_agent.agent.agent_core.orchestration.dispatch.runner_batches import (
        collaboration_request_runner_candidates,
    )

    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    child = agent.subagents.create_run(
        goal="等待协作请求。",
        agent_name="Agent-B",
        role="contributor",
        allowed_tools=["inspect_collaboration", "submit_collaboration_result", "update_collaboration"],
    )
    child.status = "DONE"
    child.verification_status = "VERIFIED"
    agent.subagents.save(child)
    case = agent.collaboration_store.open_case({'thread_id': "thread-1", 'task_id': "task-1", 'title': "完成后出现的新协作请求", 'created_by': "agent-a", 'now': 1.0})
    agent.collaboration_store.request_collaboration({'case_id': case.case_id, 'requester_agent_id': "agent-a", 'target_agent_ids': ("Agent-B",), 'question': "请已经完成的 B 继续补一条证据。", 'now': 2.0})

    candidates = collaboration_request_runner_candidates(agent, agent.subagents.list_runs())

    assert [item.id for item in candidates] == [child.id]


def test_created_subagent_registers_explicit_collaboration_capabilities_for_request_matching(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    child = agent.subagents.create_run(
        goal="响应协作请求并提交证据。",
        agent_name="Agent-B",
        role="worker",
        allowed_tools=["read_file", "search_text", "submit_collaboration_result"],
        attributes={"capabilities": ["custom-source", "query", "evidence_submission"]},
    )
    case = agent.collaboration_store.open_case({'thread_id': "thread-1", 'task_id': "task-1", 'title': "按能力匹配响应者", 'created_by': "agent-a", 'now': 1.0})

    request = agent.collaboration_store.request_collaboration({'case_id': case.case_id, 'requester_agent_id': "agent-a", 'required_capabilities': ("query", "evidence_submission"), 'question': "请能查询并提交证据的代理补证据。", 'now': 2.0})

    assert child.id in request.target_agent_ids
    capability = next(
        item for item in agent.collaboration_store.agent_capabilities() if item.agent_id == child.id
    )
    assert "custom-source" in capability.capabilities


def test_collaboration_overview_counts_ready_and_blocked_cases(tmp_path) -> None:
    store, blocked_case = _overview_blocked_case(tmp_path)

    overview = store.overview()

    assert overview["case_count"] == 2
    assert overview["open_case_count"] == 1
    assert overview["blocked_request_count"] == 1
    assert overview["ready_case_count"] == 1
    assert overview["readiness"]["ready"] is False
    assert overview["readiness"]["blocker_count"] == 1
    assert overview["readiness"]["blockers"][0]["case_id"] == blocked_case.case_id


def test_inspect_collaboration_includes_collection_result_for_unanswered_requests(tmp_path) -> None:
    store, case, blocked_request, missing_request = _rework_target_case(tmp_path)

    status = store.case_status(case.case_id)

    assert status["collection_result"]["status"] == "ready_to_report"
    assert status["collection_result"]["missing_responder_agent_ids_by_request"][
        missing_request.request_id
    ] == ["agent-c"]
    assert status["collection_result"]["unavailable_target_count"] == 0
    by_request = {item["request_id"]: item for item in status["requests"]}
    assert by_request[blocked_request.request_id]["response_status"] == "unavailable"
    assert by_request[missing_request.request_id]["response_status"] == "waiting"


def _overview_blocked_case(tmp_path):
    from agent_py_agent.agent.collaboration import CollaborationStore

    store = CollaborationStore(tmp_path / "collaboration")
    raise_collaboration = store.open_case({'thread_id': "thread-1", 'task_id': "task-1", 'title': "待响应 case", 'created_by': "agent-a", 'now': 1.0})
    blocked_case = store.open_case({'thread_id': "thread-2", 'task_id': "task-2", 'title': "阻塞 case", 'created_by': "agent-b", 'now': 2.0})
    request = store.request_collaboration({'case_id': blocked_case.case_id, 'requester_agent_id': "agent-b", 'target_agent_ids': ("agent-c",), 'question': "请补充事实。", 'now': 3.0})
    store.update_request_status({'case_id': blocked_case.case_id, 'request_id': request.request_id, 'status': "blocked", 'actor_agent_id': "agent-c", 'summary': "来源不可用。", 'now': 4.0})
    store.record_case_status({'case_id': raise_collaboration.case_id, 'status': "closed", 'actor_agent_id': "agent-a", 'summary': "无需继续协作。", 'now': 5.0})
    return store, blocked_case


def _rework_target_case(tmp_path):
    from agent_py_agent.agent.collaboration import CollaborationStore

    store = CollaborationStore(tmp_path / "collaboration")
    case = store.open_case({'thread_id': "thread-1", 'task_id': "task-1", 'title': "需要返工的协作", 'created_by': "agent-a", 'now': 1.0})
    blocked = store.request_collaboration({'case_id': case.case_id, 'requester_agent_id': "agent-a", 'target_agent_ids': ("agent-b",), 'question': "请查询来源 A。", 'now': 2.0})
    missing = store.request_collaboration({'case_id': case.case_id, 'requester_agent_id': "agent-a", 'target_agent_ids': ("agent-c",), 'question': "请查询来源 B。", 'now': 3.0})
    store.update_request_status({'case_id': case.case_id, 'request_id': blocked.request_id, 'status': "blocked", 'actor_agent_id': "agent-b", 'summary': "凭证过期。", 'now': 4.0})
    return store, case, blocked, missing


def test_inspect_collaboration_preserves_reroute_metadata_without_rework_gate(tmp_path) -> None:
    from agent_py_agent.agent.collaboration import CollaborationStore

    store = CollaborationStore(tmp_path / "collaboration")
    case = store.open_case({'thread_id': "thread-1", 'task_id': "task-1", 'title': "阻塞后换路", 'created_by': "source-a", 'now': 1.0})
    request = store.request_collaboration({'case_id': case.case_id, 'requester_agent_id': "source-a", 'target_agent_ids': ("source-a",), 'required_capabilities': ("query",), 'question': "请查询这个线索。", 'now': 2.0})
    store.update_request_status({'case_id': case.case_id, 'request_id': request.request_id, 'status': "blocked", 'actor_agent_id': "source-a", 'summary': "source-a 查询失败。", 'metadata': {"alternate_sources_available": ["source-b", "source-c"]}, 'now': 3.0})

    target = store.case_status(case.case_id)["requests"][0]

    assert target["metadata"]["alternate_sources_available"] == ["source-b", "source-c"]
    assert target["response_status"] == "unavailable"


def test_many_cases_and_requests_keep_overview_structural_and_bounded(tmp_path) -> None:
    from agent_py_agent.agent.collaboration import CollaborationStore

    store = CollaborationStore(tmp_path / "collaboration")
    for case_index in range(10):
        _populate_overview_case(store, case_index)

    overview = store.overview()
    first_status = store.case_status(store.list_cases()[0].case_id)

    assert overview["case_count"] == 10
    assert overview["request_count"] == 100
    assert overview["blocked_request_count"] == 30
    assert overview["completed_request_count"] == 40
    assert overview["evidence_count"] == 40
    assert len(first_status["requests"]) == 10
    assert first_status["request_history_count"] == 20
    assert first_status["response_coverage"]["request_count"] == 10
    assert first_status["response_coverage"]["target_count"] == 10
    assert len(first_status["response_coverage"]["requests"]) == 10


def test_case_status_includes_response_coverage_without_blocking_case(tmp_path) -> None:
    from agent_py_agent.agent.collaboration import CollaborationStore

    store = CollaborationStore(tmp_path / "collaboration")
    case = store.open_case({'thread_id': "thread-1", 'task_id': "task-1", 'title': "覆盖率账本", 'created_by': "agent-a", 'now': 1.0})
    request = store.request_collaboration({
        'case_id': case.case_id,
        'requester_agent_id': "agent-a",
        'target_agent_ids': ("agent-b", "agent-c", "agent-d"),
        'question': "请各自确认。",
        'now': 2.0,
        'metadata': {"unavailable_targets": [{"agent_id": "agent-d"}]},
    })
    store.submit_evidence({'case_id': case.case_id, 'request_id': request.request_id, 'source_agent_id': "agent-b", 'summary': "命中。", 'evidence_refs': ("artifact://b/evidence",), 'now': 3.0})

    coverage = store.case_status(case.case_id)["response_coverage"]
    row = coverage["requests"][0]

    assert coverage["target_count"] == 3
    assert coverage["responded_target_count"] == 1
    assert coverage["missing_target_count"] == 2
    assert coverage["unavailable_target_count"] == 1
    assert row["responded_target_sample"] == ["agent-b"]
    assert row["missing_target_sample"] == ["agent-c", "agent-d"]
    assert row["unavailable_target_sample"] == ["agent-d"]


def _populate_overview_case(store, case_index: int) -> None:
    case = store.open_case({'thread_id': f"thread-{case_index}", 'task_id': f"task-{case_index}", 'title': f"case {case_index}", 'created_by': "main", 'now': float(case_index)})
    for request_index in range(10):
        _populate_overview_request(store, case.case_id, case_index, request_index)


def _populate_overview_request(store, case_id: str, case_index: int, request_index: int) -> None:
    request = store.request_collaboration({'case_id': case_id, 'requester_agent_id': "main", 'target_agent_ids': (f"agent-{request_index}",), 'question': "请补充结构化事实。", 'now': float(case_index * 100 + request_index)})
    status = ("completed", "blocked", "working")[request_index % 3]
    store.update_request_status({'case_id': case_id, 'request_id': request.request_id, 'status': status, 'actor_agent_id': f"agent-{request_index}", 'summary': _overview_status_summary(status), 'now': float(case_index * 100 + request_index + 0.1)})
    if status == "completed":
        store.submit_evidence({'case_id': case_id, 'request_id': request.request_id, 'source_agent_id': f"agent-{request_index}", 'summary': "证据 ref。", 'evidence_refs': (f"artifact://case-{case_index}/ev-{request_index}",), 'now': float(case_index * 100 + request_index + 0.2)})


def _overview_status_summary(status: str) -> str:
    return {
        "completed": "已完成。",
        "blocked": "来源暂不可用。",
        "working": "仍在处理中。",
    }[status]
