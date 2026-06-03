from __future__ import annotations

import json

from agent_py_agent.agent.collaboration import CollaborationStore
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent


def test_case_status_reports_dirty_jsonl_rows_without_hiding_good_records(tmp_path) -> None:
    store, case_id, request_id = _store_with_request(tmp_path)
    store.update_request_status(
        {
            "case_id": case_id,
            "request_id": request_id,
            "status": "working",
            "summary": "已收到协作请求。",
            "actor_agent_id": "agent-b",
            "now": 2.5,
        }
    )
    store.submit_evidence(
        {
            "case_id": case_id,
            "request_id": request_id,
            "source_agent_id": "agent-b",
            "matched": True,
            "summary": "找到证据。",
            "evidence_refs": ["artifact://agent-b/e1"],
        }
    )
    _append_dirty_rows(store._requests_path(case_id))
    _append_dirty_rows(store._evidence_path(case_id))
    _append_dirty_rows(store._participants_path(case_id))
    _append_dirty_rows(store._decisions_path(case_id))

    status = store.case_status(case_id)
    contexts = {str(item.get("context") or "") for item in status["load_errors"]}

    assert status["request_count"] == 1
    assert status["evidence_count"] == 1
    assert status["participant_count"] >= 1
    assert status["decision_count"] >= 1
    assert "collaboration.requests.read" in contexts
    assert "collaboration.evidence.read" in contexts
    assert "collaboration.participants.read" in contexts
    assert "collaboration.decisions.read" in contexts


def test_inspect_pending_requests_reports_dirty_request_rows(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    case_id, _request_id = _case_with_request(agent.collaboration_store)
    _append_dirty_rows(agent.collaboration_store._requests_path(case_id))

    result = agent.tools.tools["inspect_collaboration"].execute({"agent_id": "agent-b"})
    payload = json.loads(result.output)

    assert result.ok is True
    assert payload["request_count"] == 1
    assert payload["load_errors"][0]["context"] == "collaboration.requests.read"


def _store_with_request(tmp_path) -> tuple[CollaborationStore, str, str]:
    store = CollaborationStore(tmp_path / "collaboration")
    case_id, request_id = _case_with_request(store)
    return store, case_id, request_id


def _case_with_request(store: CollaborationStore) -> tuple[str, str]:
    case = store.open_case(
        {
            "thread_id": "thread-1",
            "task_id": "task-1",
            "title": "协作账本测试",
            "summary": "需要其他代理补证据。",
            "created_by": "agent-a",
            "now": 1.0,
        }
    )
    request = store.request_collaboration(
        {
            "case_id": case.case_id,
            "requester_agent_id": "agent-a",
            "target_agent_ids": ["agent-b"],
            "required_capabilities": ["query"],
            "question": "请补充证据。",
            "now": 2.0,
        }
    )
    return case.case_id, request.request_id


def _append_dirty_rows(path) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n{not-json\n[]\n")
