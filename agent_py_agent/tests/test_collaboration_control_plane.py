from __future__ import annotations

import json

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


def _agent_with_thread(tmp_path, *, goal: str = "线索协作"):
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    thread = agent.conversation_store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 1.0})
    agent.conversation_store.bind_task({'thread_id': thread.thread_id, 'task_id': "task-1", 'goal': goal, 'now': 2.0})
    return agent, thread


def _open_tool_case(agent: SimpleAgent, *, title: str = "通用线索 case") -> str:
    return json.loads(
        agent.tools.tools["raise_collaboration"].execute(
            {
                "task_id": "task-1",
                "title": title,
                "summary": "需要其他代理协助查证。",
                "created_by": "source-a",
            }
        ).output
    )["case_id"]


def test_capability_registry_matches_agents_without_task_specific_types(tmp_path) -> None:
    from agent_py_agent.agent.collaboration import AgentCapability, CollaborationStore

    store = CollaborationStore(tmp_path / "collaboration")
    store.register_agent(
        AgentCapability(
            agent_id="api-owner-1",
            role="source_owner",
            capabilities=("query", "summarize", "api"),
            sources=("source-a",),
            status="available",
            load=0.2,
            updated_at=10.0,
        )
    )
    store.register_agent(
        AgentCapability(
            agent_id="writer-1",
            role="writer",
            capabilities=("write_report",),
            status="busy",
            load=0.9,
            updated_at=11.0,
        )
    )

    matches = store.match_agents(required_capabilities=["query", "api"], limit=5)

    assert [item.agent_id for item in matches] == ["api-owner-1"]


def test_capability_registry_only_matches_explicit_available_status(tmp_path) -> None:
    from agent_py_agent.agent.collaboration import AgentCapability, CollaborationStore
    from agent_py_agent.agent.collaboration.models import AGENT_CAPABILITY_STATUS_UNKNOWN

    store = CollaborationStore(tmp_path / "collaboration")
    for status in ("available", "idle", "ready", ""):
        store.register_agent(AgentCapability(agent_id=f"source-{status or 'blank'}", capabilities=("query",), status=status))
    store.capabilities_path.write_text(
        json.dumps(
            {
                "available": AgentCapability(agent_id="source-available", capabilities=("query",)).to_dict(),
                "missing-status": {"agent_id": "source-missing", "capabilities": ["query"]},
                "idle": AgentCapability(agent_id="source-idle", capabilities=("query",), status="idle").to_dict(),
                "ready": AgentCapability(agent_id="source-ready", capabilities=("query",), status="ready").to_dict(),
                "blank": AgentCapability(agent_id="source-blank", capabilities=("query",), status="").to_dict(),
            }
        ),
        encoding="utf-8",
    )

    loaded = {item.agent_id: item for item in store.agent_capabilities()}
    matches = store.match_agents(required_capabilities=["query"], limit=10)

    assert loaded["source-missing"].status == AGENT_CAPABILITY_STATUS_UNKNOWN
    assert [item.agent_id for item in matches] == ["source-available"]


def test_case_request_and_evidence_flow_is_refs_first(tmp_path) -> None:
    from agent_py_agent.agent.collaboration import AgentCapability, CollaborationStore

    store = CollaborationStore(tmp_path / "collaboration")
    store.register_agent(AgentCapability(agent_id="source-a", capabilities=("query", "api")))
    store.register_agent(AgentCapability(agent_id="source-b", capabilities=("query", "database")))
    case = store.open_case({'thread_id': "thread-1", 'task_id': "task-1", 'title': "跨源协作", 'summary': "发现一个需要其他来源协助判断的线索。", 'priority': "urgent", 'created_by': "source-a", 'entities': {"ip": "203.0.113.8"}, 'required_capabilities': ("query",), 'now': 20.0})

    request = store.request_collaboration({'case_id': case.case_id, 'requester_agent_id': "source-a", 'required_capabilities': ("query",), 'question': "围绕相同实体补充证据。", 'entities': {"ip": "203.0.113.8"}, 'deadline_at': 50.0, 'now': 21.0})
    evidence = store.submit_evidence({'case_id': case.case_id, 'request_id': request.request_id, 'source_agent_id': "source-b", 'matched': True, 'summary': "找到同一实体的补充证据。", 'evidence_refs': ("artifact://source-b/evidence-1",), 'confidence': 0.8, 'limitations': ("只覆盖最近 5 分钟",), 'now': 22.0})

    status = store.case_status(case.case_id)

    assert request.target_agent_ids == ("source-b",)
    assert evidence.evidence_refs == ("artifact://source-b/evidence-1",)
    assert status["case"]["status"] == "open"
    assert status["request_count"] == 1
    assert status["evidence_count"] == 1


def test_request_lifecycle_updates_effective_request_and_status_buckets(tmp_path) -> None:
    store, case, blocked_request, completed_request = _lifecycle_case_requests(tmp_path)

    _mark_request(store, (case.case_id, blocked_request.request_id, "source-b"), "working", 13.0)
    _mark_request(store, (case.case_id, blocked_request.request_id, "source-b"), "blocked", 14.0)
    _submit_agent_evidence(store, (case.case_id, completed_request.request_id, "source-c"), matched=True, now=14.5)
    _mark_request(store, (case.case_id, completed_request.request_id, "source-c"), "completed", 15.0)

    status = store.case_status(case.case_id)
    decisions_before = status["decision_count"]
    store.case_status(case.case_id)
    decisions_after = store.case_status(case.case_id)["decision_count"]
    requests = {item["request_id"]: item for item in status["requests"]}

    assert status["request_count"] == 2
    assert status["case"]["status"] == "open"
    assert requests[blocked_request.request_id]["status"] == "blocked"
    assert requests[completed_request.request_id]["status"] == "completed"
    assert status["pending_request_count"] == 0
    assert status["blocked_request_count"] == 1
    assert status["completed_request_count"] == 1
    assert status["missing_evidence_request_ids"] == [blocked_request.request_id]
    assert status["ready_for_main_agent"] is True
    assert decisions_after == decisions_before


def test_completed_request_status_without_evidence_stays_pending(tmp_path) -> None:
    from agent_py_agent.agent.collaboration import AgentCapability, CollaborationStore

    store = CollaborationStore(tmp_path / "collaboration")
    store.register_agent(AgentCapability(agent_id="source-b", capabilities=("query",)))
    case = store.open_case({'thread_id': "thread-1", 'task_id': "task-1", 'title': "无证据完成声明", 'created_by': "source-a", 'now': 10.0})
    request = store.request_collaboration({'case_id': case.case_id, 'requester_agent_id': "source-a", 'target_agent_ids': ("source-b",), 'question': "请查证。", 'now': 11.0})

    store.update_request_status({'case_id': case.case_id, 'request_id': request.request_id, 'status': "completed", 'actor_agent_id': "source-b", 'summary': "声称完成但未提交证据。", 'now': 12.0})
    status = store.case_status(case.case_id)

    assert status["pending_request_count"] == 1
    assert status["completed_request_count"] == 0
    assert status["missing_evidence_request_ids"] == [request.request_id]


def test_request_status_case_variants_do_not_drive_status_buckets() -> None:
    from agent_py_agent.agent.collaboration.request_status import (
        is_blocked_request_status,
        is_completed_request_status,
        is_declined_request_status,
        is_timed_out_request_status,
    )

    assert is_completed_request_status("completed") is True
    assert is_blocked_request_status("blocked") is True
    assert is_timed_out_request_status("timeout") is True
    assert is_declined_request_status("declined") is True
    assert is_completed_request_status("COMPLETED") is False
    assert is_blocked_request_status("BLOCKED") is False
    assert is_timed_out_request_status("TIMEOUT") is False
    assert is_declined_request_status("DECLINED") is False


def test_non_protocol_request_status_is_metadata_not_machine_status(tmp_path) -> None:
    from agent_py_agent.agent.collaboration import AgentCapability, CollaborationStore

    store = CollaborationStore(tmp_path / "collaboration")
    store.register_agent(AgentCapability(agent_id="source-b", capabilities=("query",)))
    case = store.open_case({'thread_id': "thread-1", 'task_id': "task-1", 'title': "非协议请求状态", 'created_by': "source-a", 'now': 10.0})
    request = store.request_collaboration({'case_id': case.case_id, 'requester_agent_id': "source-a", 'target_agent_ids': ("source-b",), 'question': "请查证。", 'now': 11.0})

    updated = store.update_request_status({'case_id': case.case_id, 'request_id': request.request_id, 'status': "COMPLETED", 'actor_agent_id': "source-b", 'summary': "大写完成不属于协议状态。", 'now': 12.0})
    status = store.case_status(case.case_id)

    assert updated.status == "pending"
    assert updated.metadata["raw_request_status"] == "COMPLETED"
    assert updated.metadata["request_status_protocol_error"] == "COLLABORATION_REQUEST_STATUS_INVALID"
    assert status["requests"][0]["status"] == "pending"
    assert status["completed_request_count"] == 0
    assert status["pending_request_count"] == 1


def test_non_protocol_case_status_is_metadata_not_collection_window_status(tmp_path) -> None:
    from agent_py_agent.agent.collaboration import CollaborationStore

    store = CollaborationStore(tmp_path / "collaboration")
    case = store.open_case({'thread_id': "thread-1", 'task_id': "task-1", 'title': "非协议 case 状态", 'created_by': "source-a", 'now': 10.0})

    updated = store.record_case_status({'case_id': case.case_id, 'status': "CLOSED", 'summary': "大写关闭不属于协议状态。", 'now': 11.0})
    status = store.case_status(case.case_id)

    assert updated.status == "open"
    assert updated.metadata["raw_case_status"] == "CLOSED"
    assert updated.metadata["case_status_protocol_error"] == "COLLABORATION_CASE_STATUS_INVALID"
    assert [item.case_id for item in store.list_cases(status="open")] == [case.case_id]
    assert status["case"]["status"] == "open"
    assert status["case_window"]["status"] == "open"


def test_low_level_case_status_update_keeps_invalid_status_out_of_machine_state(tmp_path) -> None:
    from agent_py_agent.agent.collaboration import CollaborationStore

    store = CollaborationStore(tmp_path / "collaboration")
    case = store.open_case(
        {
            'thread_id': "thread-1",
            'task_id': "task-1",
            'title': "底层状态更新",
            'created_by': "source-a",
            'now': 10.0,
        }
    )

    updated = store.update_case_status(case.case_id, status="resolved", now=11.0)

    assert updated.status == "open"
    assert updated.metadata["raw_case_status"] == "resolved"
    assert updated.metadata["case_status_protocol_error"] == "COLLABORATION_CASE_STATUS_INVALID"
    assert [item.case_id for item in store.list_cases(status="open")] == [case.case_id]


def test_collaboration_request_preserves_open_world_clue_packet(tmp_path) -> None:
    store, case, request = _open_world_clue_case(tmp_path)
    status = store.case_status(case.case_id)
    payload = status["requests"][0]

    assert request.problem_statement == "判断这组线索是否在其他来源里有独立佐证。"
    assert payload["observed_facts"][0]["kind"] == "caller-defined-observable"
    assert payload["query_hints"][0]["soft"] is True
    assert payload["response_contract"]["allow_not_matched"] is True
    assert payload["context_refs"] == ["artifact://source-a/event-1"]


def _lifecycle_case_requests(tmp_path):
    from agent_py_agent.agent.collaboration import AgentCapability, CollaborationStore

    store = CollaborationStore(tmp_path / "collaboration")
    store.register_agent(AgentCapability(agent_id="source-b", capabilities=("query",)))
    store.register_agent(AgentCapability(agent_id="source-c", capabilities=("query",)))
    case = store.open_case({'thread_id': "thread-1", 'task_id': "task-1", 'title': "协作生命周期", 'summary': "需要多个响应者补充事实。", 'created_by': "source-a", 'now': 10.0})
    blocked = store.request_collaboration({'case_id': case.case_id, 'requester_agent_id': "source-a", 'target_agent_ids': ("source-b",), 'required_capabilities': ("query",), 'question': "请查第一个来源。", 'now': 11.0})
    completed = store.request_collaboration({'case_id': case.case_id, 'requester_agent_id': "source-a", 'target_agent_ids': ("source-c",), 'required_capabilities': ("query",), 'question': "请查第二个来源。", 'now': 12.0})
    return store, case, blocked, completed


def _open_world_clue_case(tmp_path):
    from agent_py_agent.agent.collaboration import AgentCapability, CollaborationStore

    store = CollaborationStore(tmp_path / "collaboration")
    store.register_agent(AgentCapability(agent_id="source-a", capabilities=("query",)))
    store.register_agent(AgentCapability(agent_id="source-b", capabilities=("query", "evidence_submission")))
    case = store.open_case({'thread_id': "thread-1", 'task_id': "task-1", 'title': "开放世界线索协作", 'summary': "发现一个需要其他来源佐证的事件。", 'created_by': "source-a", 'now': 10.0})
    request = store.request_collaboration({'case_id': case.case_id, 'requester_agent_id': "source-a", 'required_capabilities': ("query", "evidence_submission"), 'question': "请在你负责的来源里围绕这些线索查证，查不到也要说明范围和限制。", 'problem_statement': "判断这组线索是否在其他来源里有独立佐证。", 'observed_facts': (_open_world_observed_fact(),), 'query_intent': {"goal": "collect_correlative_evidence", "allow_partial_match": True}, 'query_hints': ({"hint_id": "hint-1", "purpose": "先用完整值查；如果不适合，响应代理可以自行拆分或改写。", "terms": ["opaque-value-1"], "soft": True},), 'routing_requirements': {"source_scope": "any_relevant_source"}, 'response_contract': {"must_report": ["matched", "evidence_refs", "queried_scopes", "limitations"], "allow_not_matched": True}, 'context_refs': ("artifact://source-a/event-1",), 'now': 11.0})
    return store, case, request


def _open_world_observed_fact() -> dict[str, object]:
    return {
        "fact_id": "fact-1",
        "label": "触发事件中的关键标识",
        "kind": "caller-defined-observable",
        "value": "opaque-value-1",
        "source_refs": ["artifact://source-a/event-1"],
        "queryable": True,
    }


def test_collaboration_tools_accept_generic_clue_request_and_evidence_response(tmp_path) -> None:
    agent, _thread = _agent_with_thread(tmp_path)
    case_id = _open_tool_case(agent)

    request_payload = json.loads(
        agent.tools.tools["raise_collaboration"].execute(
            {
                "case_id": case_id,
                "requester_agent_id": "source-a",
                "target_agent_ids": ["source-b"],
                "required_capabilities": ["query", "evidence_submission"],
                "question": "请围绕线索查证，查不到也要说明。",
                "problem_statement": "判断线索是否有其他来源佐证。",
                "observed_facts": [
                    {
                        "fact_id": "fact-1",
                        "label": "关键片段",
                        "kind": "freeform-fragment",
                        "value": "opaque-value-2",
                        "source_refs": ["artifact://source-a/fact-1"],
                    }
                ],
                "query_hints": [
                    {
                        "hint_id": "hint-1",
                        "purpose": "必要时拆分查询。",
                        "terms": ["opaque-value-2"],
                    }
                ],
                "response_contract": {"allow_not_matched": True},
            }
        ).output
    )
    evidence_payload = _submit_generic_miss_evidence(agent, case_id, request_payload["request_id"])
    status = json.loads(agent.tools.tools["inspect_collaboration"].execute({"case_id": case_id}).output)

    assert request_payload["target_agent_ids"] == ["source-b"]
    assert evidence_payload["evidence_id"]
    assert status["requests"][0]["observed_facts"][0]["kind"] == "freeform-fragment"
    assert status["evidence"][0]["miss_reason"] == "no_matching_record_in_queried_scope"
    assert status["evidence"][0]["queried_scopes"] == ["source-b/default"]
    assert status["evidence"][0]["followup_suggestions"][0]["reason"] == "try_other_sources"


def _mark_request(store, request_ref: tuple[str, str, str], status: str, now: float) -> None:
    case_id, request_id, actor = request_ref
    store.update_request_status({'case_id': case_id, 'request_id': request_id, 'status': status, 'actor_agent_id': actor, 'summary': "来源暂时不可用，需要主代理换策略或确认。", 'now': now})


def _submit_agent_evidence(store, refs: tuple[str, str, str], *, matched: bool, now: float) -> None:
    case_id, request_id, agent_id = refs
    store.submit_evidence({'case_id': case_id, 'request_id': request_id, 'source_agent_id': agent_id, 'matched': matched, 'summary': f"{agent_id} checked", 'evidence_refs': (f"artifact://{agent_id}/evidence",), 'now': now})


def _submit_generic_miss_evidence(agent: SimpleAgent, case_id: str, request_id: str) -> dict[str, object]:
    return json.loads(
        agent.tools.tools["submit_collaboration_result"].execute(
            {
                "case_id": case_id,
                "request_id": request_id,
                "source_agent_id": "source-b",
                "matched": False,
                "summary": "在当前负责范围内未找到匹配证据。",
                "evidence_refs": ["artifact://source-b/query-1"],
                "queried_scopes": ["source-b/default"],
                "used_query_hints": ["hint-1"],
                "miss_reason": "no_matching_record_in_queried_scope",
                "response_facts": [{"fact_id": "resp-1", "kind": "not-matched-observation", "value": "no hit"}],
                "followup_suggestions": [{"target_capabilities": ["query"], "reason": "try_other_sources"}],
            }
        ).output
    )


def test_raise_collaboration_tool_accepts_relative_deadline_seconds(tmp_path, monkeypatch) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    thread = agent.conversation_store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 1.0})
    agent.conversation_store.bind_task({'thread_id': thread.thread_id, 'task_id': "task-1", 'goal': "短等待协作", 'now': 2.0})
    case_id = json.loads(
        agent.tools.tools["raise_collaboration"].execute(
            {
                "task_id": "task-1",
                "title": "短等待协作 case",
                "created_by": "source-a",
            }
        ).output
    )["case_id"]
    monkeypatch.setattr("agent_py_agent.agent.collaboration.tool_values.time.time", lambda: 100.0)

    result = agent.tools.tools["raise_collaboration"].execute(
        {
            "case_id": case_id,
            "requester_agent_id": "source-a",
            "target_agent_ids": ["source-b"],
            "question": "请尽快补充证据。",
            "deadline_seconds": 7,
        }
    )
    request = agent.collaboration_store.case_requests(case_id)[0]

    assert result.ok is True
    assert request.deadline_at == 107.0
