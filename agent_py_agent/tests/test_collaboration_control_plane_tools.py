from __future__ import annotations

import json
import logging

from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent


def test_collaboration_tools_are_registered_and_write_case_flow(tmp_path) -> None:
    agent = _agent_with_task(tmp_path)

    registered = {spec.name for spec in agent.tools.specs(include_orchestration=True)}

    assert {
        "raise_collaboration",
        "inspect_collaboration",
        "submit_collaboration_result",
        "update_collaboration",
    }.issubset(registered)

    open_result = agent.tools.tools["raise_collaboration"].execute(_raise_collaboration_params())
    case_id = json.loads(open_result.output)["case_id"]
    request_result = agent.tools.tools["raise_collaboration"].execute(_request_params(case_id))
    request_id = json.loads(request_result.output)["request_id"]
    evidence_result = agent.tools.tools["submit_collaboration_result"].execute(_evidence_params(case_id, request_id))
    request_update_result = agent.tools.tools["update_collaboration"].execute(
        _request_update_params(case_id, request_id)
    )
    status_result = agent.tools.tools["inspect_collaboration"].execute({"case_id": case_id})

    assert all(item.ok is True for item in (open_result, request_result, evidence_result, request_update_result))
    assert json.loads(status_result.output)["evidence_count"] == 1
    assert json.loads(status_result.output)["completed_request_count"] == 1


def _agent_with_task(tmp_path) -> SimpleAgent:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    thread = agent.conversation_store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 1.0})
    agent.conversation_store.bind_task({'thread_id': thread.thread_id, 'task_id': "task-1", 'goal': "协作任务", 'now': 2.0})
    return agent


def _raise_collaboration_params() -> dict[str, object]:
    return {
        "task_id": "task-1",
        "title": "通用协作 case",
        "summary": "需要其他代理协作。",
        "priority": "urgent",
        "created_by": "agent-a",
        "required_capabilities": ["query"],
    }


def _request_params(case_id: str) -> dict[str, object]:
    return {
        "case_id": case_id,
        "requester_agent_id": "agent-a",
        "target_agent_ids": ["agent-b"],
        "required_capabilities": ["query"],
        "question": "请补充证据。",
    }


def _evidence_params(case_id: str, request_id: str) -> dict[str, object]:
    return {
        "case_id": case_id,
        "request_id": request_id,
        "source_agent_id": "agent-b",
        "matched": True,
        "summary": "找到证据。",
        "evidence_refs": ["artifact://agent-b/e1"],
    }


def _request_update_params(case_id: str, request_id: str) -> dict[str, object]:
    return {
        "case_id": case_id,
        "request_id": request_id,
        "status": "completed",
        "actor_agent_id": "agent-b",
        "summary": "已响应协作请求。",
    }


def test_inspect_collaboration_finds_targeted_request_without_case_id(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    thread = agent.conversation_store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 1.0})
    agent.conversation_store.bind_task({'thread_id': thread.thread_id, 'task_id': "task-1", 'goal': "协作任务", 'now': 2.0})
    case_id = json.loads(
        agent.tools.tools["raise_collaboration"].execute(
            {
                "task_id": "task-1",
                "title": "待发现协作请求",
                "summary": "响应者不知道 case_id，也要能发现自己被点名。",
                "created_by": "source-a",
            }
        ).output
    )["case_id"]
    request_id = json.loads(
        agent.tools.tools["raise_collaboration"].execute(
            {
                "case_id": case_id,
                "requester_agent_id": "source-a",
                "target_agent_ids": ["source-b"],
                "question": "请补充你负责来源里的证据。",
                "observed_facts": [{"fact_id": "fact-1", "value": "opaque-clue"}],
            }
        ).output
    )["request_id"]

    result = agent.tools.tools["inspect_collaboration"].execute({"agent_id": "source-b"})
    payload = json.loads(result.output)

    assert result.ok is True
    assert payload["request_count"] == 1
    assert payload["requests"][0]["case_id"] == case_id
    assert payload["requests"][0]["request_id"] == request_id
    assert payload["requests"][0]["observed_facts"][0]["value"] == "opaque-clue"


def test_raise_collaboration_materializes_internal_thread_for_known_local_task(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    child = agent.subagents.create_run(
        goal="本地协作子代理需要打开一个 case。",
        allowed_tools=["raise_collaboration", "raise_collaboration"],
        agent_name="local-source-a",
    )

    open_result = agent.tools.tools["raise_collaboration"].execute(
        {
            "thread_id": "guessed-thread-id",
            "task_id": child.id,
            "title": "本地任务协作 case",
            "summary": "没有外部会话绑定时也要能落到内部 thread。",
            "created_by": child.id,
        }
    )
    payload = json.loads(open_result.output)
    linked = agent.conversation_store.thread_for_task(child.id)

    assert open_result.ok is True
    assert payload["thread_id"]
    assert linked is not None
    assert linked.thread_id == payload["thread_id"]
    assert linked.channel_bindings[0].channel == "internal"


def test_raise_collaboration_warns_when_task_thread_binding_save_fails(tmp_path, monkeypatch, caplog) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    child = agent.subagents.create_run(
        goal="本地协作子代理需要打开一个 case。",
        allowed_tools=["raise_collaboration"],
        agent_name="local-source-a",
    )

    def broken_save(_task):
        raise OSError("task state locked")

    monkeypatch.setattr(agent.subagents, "save", broken_save)

    with caplog.at_level(logging.WARNING):
        open_result = agent.tools.tools["raise_collaboration"].execute(
            {
                "task_id": child.id,
                "title": "本地任务协作 case",
                "summary": "保存 thread 反写失败时仍能开 case。",
                "created_by": child.id,
            }
        )

    assert open_result.ok is True
    assert "collaboration thread binding could not be saved on task" in caplog.text
    assert "raise_collaboration.remember_thread_on_task" in caplog.text
    assert "task state locked" in caplog.text


def test_raise_collaboration_thread_binding_error_is_structured(tmp_path, monkeypatch) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)

    def broken_thread_for_task(_task_id):
        raise ValueError("bad task binding")

    monkeypatch.setattr(agent.conversation_store, "thread_for_task", broken_thread_for_task)

    result = agent.tools.tools["raise_collaboration"].execute(
        {
            "task_id": "task-1",
            "title": "账本读取失败 case",
            "summary": "读取任务绑定失败时要把错误交给模型。",
        }
    )
    payload = json.loads(result.output)

    assert result.ok is False
    assert payload["error"] == "task_thread_lookup_failed"
    assert payload["load_error"]["context"] == "raise_collaboration.thread_for_task"
    assert payload["load_error"]["category"] == "data_parse"


def test_raise_collaboration_subagent_load_error_is_structured(tmp_path, monkeypatch) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)

    def broken_load(_task_id):
        raise OSError("subagent ledger unavailable")

    monkeypatch.setattr(agent.subagents, "load", broken_load)

    result = agent.tools.tools["raise_collaboration"].execute(
        {
            "task_id": "task-1",
            "title": "子代理账本读取失败 case",
            "summary": "读取子代理账本失败时要把错误交给模型。",
        }
    )
    payload = json.loads(result.output)

    assert result.ok is False
    assert payload["error"] == "subagent_task_load_failed"
    assert payload["load_error"]["context"] == "raise_collaboration.subagents.load"
    assert payload["load_error"]["category"] == "io"


def test_raise_collaboration_internal_thread_materialize_error_is_structured(tmp_path, monkeypatch) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    child = agent.subagents.create_run(
        goal="本地协作子代理需要打开一个 case。",
        allowed_tools=["raise_collaboration"],
        agent_name="local-source-a",
    )

    def broken_get_or_create_thread(_attrs):
        raise OSError("thread store unavailable")

    monkeypatch.setattr(agent.conversation_store, "get_or_create_thread", broken_get_or_create_thread)

    result = agent.tools.tools["raise_collaboration"].execute(
        {
            "task_id": child.id,
            "title": "内部线程物化失败 case",
            "summary": "创建内部线程失败时要把错误交给模型。",
        }
    )
    payload = json.loads(result.output)

    assert result.ok is False
    assert payload["error"] == "internal_thread_materialize_failed"
    assert payload["load_error"]["context"] == "raise_collaboration.materialize_internal_thread"
    assert payload["load_error"]["category"] == "io"


def test_raise_collaboration_target_runtime_error_is_visible(tmp_path, monkeypatch) -> None:
    agent = _agent_with_task(tmp_path)

    def broken_list_runs():
        raise ValueError("subagent tree index broken")

    monkeypatch.setattr(agent.subagents, "list_runs", broken_list_runs)

    result = agent.tools.tools["raise_collaboration"].execute(
        {
            "task_id": "task-1",
            "title": "目标状态读取失败 case",
            "summary": "目标状态读取失败时请求仍应落账，但要报告错误。",
            "target_agent_ids": ["agent-b"],
            "question": "请补充证据。",
        }
    )
    payload = json.loads(result.output)
    status = json.loads(agent.tools.tools["inspect_collaboration"].execute({"case_id": payload["case_id"]}).output)

    assert result.ok is True
    assert payload["target_runtime_load_error"]["context"] == "raise_collaboration.target_runtime"
    assert payload["target_runtime_load_error"]["category"] == "data_parse"
    assert status["requests"][0]["metadata"]["target_runtime_load_error"]["category"] == "data_parse"


def test_raise_collaboration_target_alias_error_is_visible(tmp_path, monkeypatch) -> None:
    agent = _agent_with_task(tmp_path)
    original_aliases = agent.collaboration_store.agent_identity_aliases

    def broken_aliases(_text):
        raise ValueError("identity index broken")

    monkeypatch.setattr(agent.collaboration_store, "agent_identity_aliases", broken_aliases)

    result = agent.tools.tools["raise_collaboration"].execute(
        {
            "task_id": "task-1",
            "title": "目标别名解析失败 case",
            "summary": "目标别名索引坏了不能伪装成正常找不到目标。",
            "target_agent_ids": ["agent-b"],
            "question": "请补充证据。",
        }
    )
    payload = json.loads(result.output)
    monkeypatch.setattr(agent.collaboration_store, "agent_identity_aliases", original_aliases)
    status = json.loads(agent.tools.tools["inspect_collaboration"].execute({"case_id": payload["case_id"]}).output)

    assert result.ok is True
    assert payload["target_resolution_errors"][0]["context"] == "raise_collaboration.agent_identity_aliases"
    assert payload["target_resolution_errors"][0]["category"] == "data_parse"
    assert status["requests"][0]["metadata"]["target_resolution_errors"][0]["message"] == "identity index broken"


def test_inspect_collaboration_identity_load_error_is_visible(tmp_path, monkeypatch) -> None:
    agent = _agent_with_task(tmp_path)
    child = agent.subagents.create_run(
        goal="响应协作请求。",
        allowed_tools=["inspect_collaboration"],
        agent_name="source-b",
        role="responder",
    )

    def broken_load(_run_id):
        raise OSError("subagent identity ledger unavailable")

    monkeypatch.setattr(agent.subagents, "load", broken_load)

    result = agent.tools.tools["inspect_collaboration"].execute({"agent_id": child.id})
    payload = json.loads(result.output)

    assert result.ok is True
    assert payload["agent_id"] == child.id
    assert payload["identity_load_error"]["context"] == "collaboration.identity.subagents.load"
    assert payload["identity_load_error"]["category"] == "io"


def test_inspect_collaboration_case_status_error_is_structured(tmp_path, monkeypatch) -> None:
    agent = _agent_with_task(tmp_path)
    case_id = json.loads(agent.tools.tools["raise_collaboration"].execute(_raise_collaboration_params()).output)[
        "case_id"
    ]

    def broken_case_status(_case_id):
        raise ValueError("case ledger broken")

    monkeypatch.setattr(agent.collaboration_store, "case_status", broken_case_status)

    result = agent.tools.tools["inspect_collaboration"].execute({"case_id": case_id})
    payload = json.loads(result.output)

    assert result.ok is False
    assert payload["error"] == "case_status_read_failed"
    assert payload["load_error"]["context"] == "inspect_collaboration.case_status"
    assert payload["load_error"]["category"] == "data_parse"


def test_update_collaboration_request_keeps_update_when_overview_fails(tmp_path, monkeypatch) -> None:
    agent, case_id, request_id = _reroute_tool_fixture(tmp_path)

    def broken_case_status(_case_id):
        raise ValueError("overview ledger broken")

    monkeypatch.setattr(agent.collaboration_store, "case_status", broken_case_status)

    result = agent.tools.tools["update_collaboration"].execute(
        {
            "case_id": case_id,
            "request_id": request_id,
            "status": "working",
            "actor_agent_id": "source-a",
            "summary": "已经接手处理。",
        }
    )
    payload = json.loads(result.output)

    assert result.ok is True
    assert payload["request"]["status"] == "working"
    assert payload["overview"]["overview_load_error"]["context"] == "update_collaboration.case_status"


def test_update_collaboration_case_reports_decision_load_error(tmp_path, monkeypatch) -> None:
    agent = _agent_with_task(tmp_path)
    case_id = json.loads(agent.tools.tools["raise_collaboration"].execute(_raise_collaboration_params()).output)[
        "case_id"
    ]

    def broken_case_decisions(_case_id):
        raise OSError("decision ledger unavailable")

    monkeypatch.setattr(agent.collaboration_store, "case_decisions", broken_case_decisions)

    result = agent.tools.tools["update_collaboration"].execute(
        {
            "case_id": case_id,
            "status": "closed",
            "actor_agent_id": "main",
            "summary": "证据已收口。",
            "decision_type": "resolved_by_main_agent",
        }
    )
    payload = json.loads(result.output)

    assert result.ok is True
    assert payload["case"]["status"] == "closed"
    assert payload["decision_load_error"]["context"] == "update_collaboration.case_decisions"


def test_targeted_collaboration_request_carries_clue_packet_to_responder_context(tmp_path) -> None:
    agent, responder, request = _targeted_clue_request(tmp_path)

    context = agent.subagents.build_execution_context(responder.id)
    targeted = context.context_bundle["collaboration"]["targeted_requests"][0]

    assert (targeted["request_id"], targeted["observed_facts"][0]["kind"], targeted["query_hints"][0]["hint_id"], targeted["response_contract"]["allow_not_matched"]) == (request.request_id, "caller-defined-kind", "hint-1", True)


def _targeted_clue_request(tmp_path):
    from agent_py_agent.agent.collaboration import AgentCapability

    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    responder = agent.subagents.create_run(
        goal="响应开放世界线索协作请求。",
        allowed_tools=["inspect_collaboration", "submit_collaboration_result", "update_collaboration"],
        agent_name="source-b",
        role="responder",
    )
    agent.collaboration_store.register_agent(
        AgentCapability(agent_id=responder.id, capabilities=("query", "evidence_submission"))
    )
    case = agent.collaboration_store.open_case({'thread_id': "thread-1", 'task_id': "task-1", 'title': "响应者上下文线索", 'summary': "需要把线索包交给响应者。", 'created_by': "source-a", 'now': 1.0})
    request = agent.collaboration_store.request_collaboration({'case_id': case.case_id, 'requester_agent_id': "source-a", 'target_agent_ids': (responder.id,), 'question': "请围绕开放世界线索查证。", 'observed_facts': (
            {
                "fact_id": "fact-1",
                "label": "任意关键线索",
                "kind": "caller-defined-kind",
                "value": "opaque-value-3",
            },
        ), 'query_hints': (
            {
                "hint_id": "hint-1",
                "purpose": "响应代理自行决定完整查、拆分查或换来源。",
                "terms": ["opaque-value-3"],
            },
        ), 'response_contract': {"allow_not_matched": True}, 'now': 2.0})
    return agent, responder, request


def test_update_collaboration_tool_updates_targets_and_audit(tmp_path) -> None:
    agent, case_id, request_id = _reroute_tool_fixture(tmp_path)

    reroute_result = agent.tools.tools["update_collaboration"].execute(
        {
            "case_id": case_id,
            "request_id": request_id,
            "actor_agent_id": "main",
            "target_agent_ids": ["source-b"],
            "summary": "source-a 失败，改由 source-b 继续。",
            "metadata": {"reason_code": "source_unavailable"},
        }
    )
    payload = json.loads(reroute_result.output)
    status = json.loads(agent.tools.tools["inspect_collaboration"].execute({"case_id": case_id}).output)

    assert reroute_result.ok is True
    assert payload["request"]["target_agent_ids"] == ["source-b"]
    assert payload["request"]["status"] == "pending"
    assert status["requests"][0]["target_agent_ids"] == ["source-b"]
    assert status["requests"][0]["metadata"]["rerouted_from"] == ["source-a"]
    assert status["requests"][0]["metadata"]["rerouted_to"] == ["source-b"]


def _reroute_tool_fixture(tmp_path) -> tuple[SimpleAgent, str, str]:
    agent = _agent_with_task(tmp_path)
    case_id = json.loads(agent.tools.tools["raise_collaboration"].execute(_reroute_case_params()).output)["case_id"]
    request_id = json.loads(
        agent.tools.tools["raise_collaboration"].execute({
            "case_id": case_id,
            "requester_agent_id": "source-a",
            "target_agent_ids": ["source-a"],
            "question": "请查询线索。",
        }).output
    )["request_id"]
    return agent, case_id, request_id


def _reroute_case_params() -> dict[str, object]:
    return {
        "task_id": "task-1",
        "title": "换路工具 case",
        "summary": "原目标失败，需要替代目标。",
        "created_by": "source-a",
    }


def test_case_lifecycle_requires_summary_or_decision_when_closing(tmp_path) -> None:
    from agent_py_agent.agent.collaboration import CollaborationStore

    store = CollaborationStore(tmp_path / "collaboration")
    case = store.open_case({'thread_id': "thread-1", 'task_id': "task-1", 'title': "生命周期 case", 'summary': "需要后续关闭。", 'created_by': "agent-a", 'now': 10.0})

    _assert_empty_close_rejected(store, case.case_id)

    updated = store.record_case_status({'case_id': case.case_id, 'status': "triaged", 'actor_agent_id': "agent-a", 'summary': "已完成初步研判，等待更多证据。", 'now': 12.0})
    closed = store.record_case_status({'case_id': case.case_id, 'status': "closed", 'actor_agent_id': "agent-a", 'summary': "证据已收口，结论已同步。", 'decision_type': "closed_by_main_agent", 'now': 13.0})
    decisions = store.case_decisions(case.case_id)

    assert updated.status == "triaged"
    assert closed.status == "closed"
    assert [item.summary for item in decisions] == [
        "已完成初步研判，等待更多证据。",
        "证据已收口，结论已同步。",
    ]


def _assert_empty_close_rejected(store, case_id: str) -> None:
    try:
        store.record_case_status({'case_id': case_id, 'status': "closed", 'actor_agent_id': "agent-a", 'summary': "", 'now': 11.0})
    except ValueError as exc:
        assert "summary_or_decision_required" in str(exc)
    else:
        raise AssertionError("closing a case without summary or decision should fail")


def test_update_collaboration_tool_records_decision_and_inspect_collaboration(tmp_path) -> None:
    agent = _agent_with_task(tmp_path)
    open_result = agent.tools.tools["raise_collaboration"].execute(
        {
            "task_id": "task-1",
            "title": "状态工具 case",
            "summary": "需要状态推进。",
            "created_by": "agent-a",
        }
    )
    case_id = json.loads(open_result.output)["case_id"]

    update_result = agent.tools.tools["update_collaboration"].execute(
        {
            "case_id": case_id,
            "status": "resolved",
            "actor_agent_id": "agent-a",
            "summary": "已经完成研判并给出处理结论。",
            "decision_type": "resolved_by_main_agent",
        }
    )
    status = json.loads(agent.tools.tools["inspect_collaboration"].execute({"case_id": case_id}).output)

    assert update_result.ok is True
    assert json.loads(update_result.output)["case"]["status"] == "resolved"
    assert status["case"]["status"] == "resolved"
    assert status["decision_count"] == 1
