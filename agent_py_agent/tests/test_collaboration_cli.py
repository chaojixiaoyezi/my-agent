from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from agent_py_agent.agent.collaboration import AgentCapability
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent


def test_collaboration_list_summarizes_cases_and_pending_requests(tmp_path, capsys) -> None:
    from agent_py_agent.cli.collaboration import cmd_collaboration_list

    agent = _agent_with_collaboration_case(tmp_path, with_evidence=False)
    args = SimpleNamespace(config=str(tmp_path / "config.yaml"), status="", limit=20, json=True)

    with patch("agent_py_agent.cli.collaboration.make_agent", return_value=agent):
        assert cmd_collaboration_list(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["total"] == 1
    assert payload["cases"][0]["title"] == "跨源协作入口"
    assert payload["cases"][0]["request_count"] == 1
    assert payload["cases"][0]["pending_request_count"] == 1
    assert payload["cases"][0]["evidence_count"] == 0


def test_collaboration_overview_command_reports_readiness(tmp_path, capsys) -> None:
    from agent_py_agent.cli.collaboration import cmd_collaboration_overview

    agent = _agent_with_collaboration_case(tmp_path, with_evidence=False)
    case_id = agent.collaboration_store.list_cases()[0].case_id
    request_id = agent.collaboration_store.case_requests(case_id)[0].request_id
    agent.collaboration_store.update_request_status({'case_id': case_id, 'request_id': request_id, 'status': "blocked", 'actor_agent_id': "source-b", 'summary': "来源不可用。", 'now': 8.0})
    args = SimpleNamespace(config=str(tmp_path / "config.yaml"), json=True)

    with patch("agent_py_agent.cli.collaboration.make_agent", return_value=agent):
        assert cmd_collaboration_overview(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["case_count"] == 1
    assert payload["blocked_request_count"] == 1
    assert payload["readiness"]["ready"] is False
    assert payload["readiness"]["blockers"][0]["case_id"] == case_id


def test_collaboration_status_returns_case_detail_with_evidence(tmp_path, capsys) -> None:
    from agent_py_agent.cli.collaboration import cmd_collaboration_status

    agent = _agent_with_collaboration_case(tmp_path, with_evidence=True)
    case_id = agent.collaboration_store.list_cases()[0].case_id
    args = SimpleNamespace(config=str(tmp_path / "config.yaml"), case_id=case_id, json=True)

    with patch("agent_py_agent.cli.collaboration.make_agent", return_value=agent):
        assert cmd_collaboration_status(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["case"]["case_id"] == case_id
    assert payload["overview"]["pending_request_count"] == 0
    assert payload["overview"]["evidence_count"] == 1
    assert payload["evidence"][0]["evidence_refs"] == ["artifact://source-b/e1"]


def test_collaboration_update_status_closes_case_with_summary(tmp_path, capsys) -> None:
    from agent_py_agent.cli.collaboration import cmd_collaboration_update_status

    agent = _agent_with_collaboration_case(tmp_path, with_evidence=True)
    case_id = agent.collaboration_store.list_cases()[0].case_id
    args = SimpleNamespace(
        config=str(tmp_path / "config.yaml"),
        case_id=case_id,
        status="closed",
        actor_agent_id="main-agent",
        summary="证据已处理，case 关闭。",
        decision_type="closed_by_main_agent",
        json=True,
    )

    with patch("agent_py_agent.cli.collaboration.make_agent", return_value=agent):
        assert cmd_collaboration_update_status(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["case"]["status"] == "closed"
    assert payload["decision"]["summary"] == "证据已处理，case 关闭。"


def test_collaboration_update_request_updates_request_lifecycle(tmp_path, capsys) -> None:
    from agent_py_agent.cli.collaboration import cmd_collaboration_update_request

    agent = _agent_with_collaboration_case(tmp_path, with_evidence=False)
    case_id = agent.collaboration_store.list_cases()[0].case_id
    request_id = agent.collaboration_store.case_requests(case_id)[0].request_id
    args = SimpleNamespace(
        config=str(tmp_path / "config.yaml"),
        case_id=case_id,
        request_id=request_id,
        status="blocked",
        actor_agent_id="source-b",
        summary="目标来源暂时不可用，需要主代理调整策略。",
        json=True,
    )

    with patch("agent_py_agent.cli.collaboration.make_agent", return_value=agent):
        assert cmd_collaboration_update_request(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["request"]["request_id"] == request_id
    assert payload["request"]["status"] == "blocked"
    assert payload["overview"]["blocked_request_count"] == 1
    assert payload["overview"]["ready_for_main_agent"] is True


def test_collaboration_parser_registers_commands() -> None:
    from agent_py_agent.cli.parser import build_parser

    parser = build_parser()
    overview_args = parser.parse_args(["collaboration", "overview", "--json"])
    list_args = parser.parse_args(["collaboration", "list", "--status", "open", "--json"])
    status_args = parser.parse_args(["collaboration", "status", "--case-id", "case-1", "--json"])
    update_args = parser.parse_args(_update_status_argv())
    update_request_args = parser.parse_args(_update_request_argv())

    assert overview_args.collaboration_command == "overview"
    assert overview_args.json is True
    assert list_args.command == "collaboration"
    assert list_args.collaboration_command == "list"
    assert list_args.status == "open"
    assert list_args.json is True
    assert status_args.collaboration_command == "status"
    assert status_args.case_id == "case-1"
    assert update_args.collaboration_command == "update-status"
    assert update_args.status == "closed"
    assert update_args.summary == "done"
    assert update_request_args.collaboration_command == "update-request"
    assert update_request_args.request_id == "creq-1"
    assert update_request_args.status == "completed"


def _update_status_argv() -> list[str]:
    return [
        "collaboration",
        "update-status",
        "--case-id",
        "case-1",
        "--status",
        "closed",
        "--summary",
        "done",
        "--json",
    ]


def _update_request_argv() -> list[str]:
    return [
        "collaboration",
        "update-request",
        "--case-id",
        "case-1",
        "--request-id",
        "creq-1",
        "--status",
        "completed",
        "--summary",
        "done",
        "--json",
    ]


def _agent_with_collaboration_case(tmp_path, *, with_evidence: bool) -> SimpleAgent:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    thread = agent.conversation_store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 1.0})
    agent.conversation_store.bind_task({'thread_id': thread.thread_id, 'task_id': "task-1", 'goal': "协作任务", 'now': 2.0})
    agent.collaboration_store.register_agent(AgentCapability(agent_id="source-a", capabilities=("query",)))
    agent.collaboration_store.register_agent(AgentCapability(agent_id="source-b", capabilities=("query",)))
    case = agent.collaboration_store.open_case({'thread_id': thread.thread_id, 'task_id': "task-1", 'title': "跨源协作入口", 'summary': "需要多代理补证据。", 'priority': "urgent", 'created_by': "source-a", 'now': 3.0})
    request = agent.collaboration_store.request_collaboration({'case_id': case.case_id, 'requester_agent_id': "source-a", 'target_agent_ids': ("source-b",), 'required_capabilities': ("query",), 'question': "请补充证据。", 'now': 4.0})
    if with_evidence:
        agent.collaboration_store.submit_evidence({'case_id': case.case_id, 'request_id': request.request_id, 'source_agent_id': "source-b", 'matched': True, 'summary': "找到证据。", 'evidence_refs': ("artifact://source-b/e1",), 'now': 5.0})
    return agent
