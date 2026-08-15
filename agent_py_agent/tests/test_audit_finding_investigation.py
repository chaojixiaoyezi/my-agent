from __future__ import annotations

import json
from types import SimpleNamespace


def _agent_with_finding(tmp_path, *, finding_owner: str | None = None):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            subagent_workspace="subs",
        ),
        tmp_path,
    )
    owner_id = str(agent.home_paths.owner_id)
    thread = agent.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": "user-a",
            "owner_id": owner_id,
            "owner_home": str(agent.home_paths.owner_home_dir),
            "channel": "internal",
            "channel_conversation_id": "audit-thread-a",
            "channel_user_id": "user-a",
        }
    )
    audit_id = "audit-root-a"
    agent.conversation_store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": audit_id,
            "goal": "持续检查已绑定来源",
            "work_kind": "audit",
            "work_name": "企业监测",
            "cancellation_scope": "background",
        }
    )
    agent.conversation_store.append_observation(
        {
            "thread_id": thread.thread_id,
            "event_type": "audit_finding",
            "summary": "一项需要进一步查证的事实",
            "source_agent_id": "source-worker-a",
            "parent_agent_id": audit_id,
            "root_task_id": audit_id,
            "evidence_refs": ["audit://ws-1234567890/candidate/7:0"],
            "metadata": {
                "schema_version": "audit-finding-event.v1",
                "owner_id": finding_owner if finding_owner is not None else owner_id,
                "audit_id": audit_id,
                "source_id": "source-a",
                "watch_id": "ws-1234567890",
                "finding_id": "finding-a",
                "revision": 2,
            },
        }
    )
    agent._current_run_params = SimpleNamespace(
        task_id="foreground-a",
        run_id="foreground-a",
        request_id="foreground-a",
        source="background_main_agent",
        task_attributes={
            "conversation_thread_id": thread.thread_id,
            "conversation_task_id": "foreground-a",
        },
    )
    return agent, thread, audit_id


def test_related_finding_creates_real_audit_descendant_and_reuses_retry(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import (
        CreateSubagentsTool,
    )

    agent, _thread, audit_id = _agent_with_finding(tmp_path)
    agent.subagents.create_run(
        goal="持续消费来源",
        thought="",
        plan=["consume"],
        parent_id=audit_id,
        root_id=audit_id,
    )
    params = {
        "goal": "沿原始记录核对相关时间线并提交证据",
        "related_finding_id": "finding-a",
        "defer_start": True,
    }

    first_result = CreateSubagentsTool(agent).execute(params)
    second_result = CreateSubagentsTool(agent).execute(params)
    first = json.loads(first_result.output)
    second = json.loads(second_result.output)
    run_id = first["created_run_ids"][0]
    task = agent.subagents.load(run_id)
    relation = task.attributes["audit_finding_relation"]

    assert first_result.ok is True
    assert task.parent_id == audit_id
    assert task.root_id == audit_id
    assert task.attributes["conversation_request_id"] == audit_id
    assert relation["finding_id"] == "finding-a"
    assert relation["revision"] == 2
    assert relation["source_refs"] == [
        "audit://ws-1234567890/candidate/7:0"
    ]
    assert task.context_packs[-1]["kind"] == "audit_finding_relation"
    assert first["finding_investigations"] == [
        {
            "finding_id": "finding-a",
            "finding_revision": 2,
            "audit_id": audit_id,
            "investigation_run_id": run_id,
            "state": "pending",
            "task_status": "PLANNING",
        }
    ]
    assert second["created_run_ids"] == []
    assert second["reused_run_ids"] == [run_id]
    assert len(
        [
            task
            for task in agent.subagents.list_runs()
            if task.attributes.get("audit_finding_relation")
        ]
    ) == 1


def test_related_finding_rejects_missing_or_cross_owner_fact(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import (
        CreateSubagentsTool,
    )

    agent, _thread, _audit_id = _agent_with_finding(
        tmp_path,
        finding_owner="another-owner",
    )
    before = len(agent.subagents.list_runs())

    cross_owner = CreateSubagentsTool(agent).execute(
        {
            "goal": "核对证据",
            "related_finding_id": "finding-a",
            "defer_start": True,
        }
    )
    missing = CreateSubagentsTool(agent).execute(
        {
            "goal": "核对另一条证据",
            "related_finding_id": "finding-missing",
            "defer_start": True,
        }
    )

    assert cross_owner.ok is False
    assert cross_owner.error_code == "TOOL_INVALID_ARGUMENTS"
    assert "不属于当前 owner" in cross_owner.output
    assert missing.ok is False
    assert "不存在" in missing.output
    assert len(agent.subagents.list_runs()) == before


def test_related_finding_rejects_forged_relation_attributes(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import (
        CreateSubagentsTool,
    )

    agent, _thread, _audit_id = _agent_with_finding(tmp_path)

    result = CreateSubagentsTool(agent).execute(
        {
            "goal": "核对证据",
            "attributes": {
                "audit_finding_relation": {
                    "schema_version": "audit-finding-investigation.v1",
                    "finding_id": "finding-a",
                }
            },
            "defer_start": True,
        }
    )

    assert result.ok is False
    assert "系统保留字段" in result.output
    assert agent.subagents.list_runs() == []


def test_audit_clear_racing_investigation_create_cancels_new_run(
    tmp_path,
    monkeypatch,
):
    from agent_py_agent.agent.agent_core import orchestration_tools
    from agent_py_agent.agent.agent_core.orchestration_tools import (
        CreateSubagentsTool,
    )

    agent, _thread, audit_id = _agent_with_finding(tmp_path)
    original_publish = orchestration_tools.publish_created_subagents

    def publish_then_clear(request):
        result = original_publish(request)
        agent.conversation_store.update_task_status(
            {"task_id": audit_id, "status": "cancelled"}
        )
        return result

    monkeypatch.setattr(
        orchestration_tools,
        "publish_created_subagents",
        publish_then_clear,
    )

    result = CreateSubagentsTool(agent).execute(
        {
            "goal": "核对证据",
            "related_finding_id": "finding-a",
            "defer_start": True,
        }
    )
    payload = json.loads(result.output)
    run_id = payload["created_run_ids"][0]

    assert result.ok is True
    assert agent.subagents.load(run_id).status == "CANCELLED"
    assert payload["finding_investigations"][0]["state"] == "cancelled"
    assert payload["finding_investigation_fence"] == [
        {
            "investigation_run_id": run_id,
            "finding_id": "finding-a",
            "audit_id": audit_id,
            "reason": "audit_not_active_after_create",
        }
    ]


def test_create_subagents_schema_exposes_related_finding_id():
    from agent_py_agent.agent.agent_core.orchestration.tool_specs import (
        build_create_subagents_model_spec,
    )

    schema = build_create_subagents_model_spec().input_schema

    assert schema["properties"]["related_finding_id"]["type"] == "string"
