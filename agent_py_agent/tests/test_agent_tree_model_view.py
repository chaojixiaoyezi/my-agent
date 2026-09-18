import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent_py_agent.agent.agent_core.agent_tree.model_view import (
    agent_tree_model_payload,
    agent_tree_model_preview,
    agent_tree_progress_observation,
)
from agent_py_agent.agent.agent_core.orchestration.child_result_index import (
    child_result_index_from_nodes,
)
from agent_py_agent.agent.agent_core.orchestration.tools.list_agents import ListAgentsTool
from agent_py_agent.agent.agent_core.tool_context.reducer import render_tool_result_for_live_prompt
from agent_py_agent.tests.test_tool_context_reducer import _result


def _snapshot(nodes):
    """同一已授权快照供模型投影与归档测试，不提供旁路状态来源。"""
    return {
        "main": {"run_id": "main", "status": "RUNNING", "workspace_refs": {"inbox": "/private/inbox"}},
        "scope": "own_subtree",
        "scope_resolution": {"effective_run_id": "parent", "warnings": ["run_id_conflict"]},
        "nodes": nodes,
        "child_result_index": child_result_index_from_nodes(nodes),
        "source_refs": {"store": "/private/canonical_state.json"},
    }


@pytest.mark.parametrize("status", ["RUNNING", "DONE", "FAILED", "BLOCKED", "SUCCESS"])
def test_status_model_projection_preserves_raw_status_without_internal_paths(status):
    snapshot = _snapshot([{
        "run_id": "child", "parent_run_id": "parent", "status": status,
        "failure_type": "upstream_error", "last_progress_summary": "正在读取模块",
        "artifact_refs": ["output/report.md"], "blockers": ["provider_unavailable"],
        "workspace_refs": {"inbox": "/private/inbox"},
        "recovery_refs": {"summary": "/private/summary.md"},
    }])
    payload = agent_tree_model_payload(snapshot)
    row = payload["nodes"][0]
    assert row["status"] == status
    assert row["failure_type"] == "upstream_error"
    assert row["blockers"] == ["provider_unavailable"]
    assert row["read_order"] == ["output/report.md"]
    assert row["parent_run_id"] == "parent"
    assert payload["scope_resolution"] == snapshot["scope_resolution"]
    assert "/private" not in json.dumps(payload)
    assert snapshot["nodes"][0]["recovery_refs"]  # 宿主视图保持原样。


@pytest.mark.parametrize("status,readable", [("DONE", True), ("FAILED", True), ("RUNNING", False), ("SUCCESS", False)])
def test_only_actual_terminal_report_can_replace_absent_artifacts(tmp_path, status, readable):
    report = tmp_path / "final_report.md"
    report.write_text("实际结果或失败原因", encoding="utf-8")
    rows = child_result_index_from_nodes([{
        "run_id": "child", "status": status,
        "workspace_refs": {"final_report": str(report)},
        "recovery_refs": {"summary": str(tmp_path / "summary.md")},
    }])
    assert rows[0]["read_order"] == ([str(report)] if readable else [])
    assert rows[0]["status"] == status


def test_missing_report_and_existing_recovery_summary_are_not_a_result(tmp_path):
    summary = tmp_path / "summary.md"
    summary.write_text("恢复上下文", encoding="utf-8")
    row = child_result_index_from_nodes([{
        "run_id": "child", "status": "DONE",
        "workspace_refs": {"final_report": str(tmp_path / "missing.md")},
        "recovery_refs": {"summary": str(summary)},
    }])[0]
    assert row["read_order"] == []
    assert row["readiness"] == "done_without_refs"


def test_large_model_status_uses_bounded_valid_preview_and_exact_archive_ref():
    snapshot = _snapshot([
        {"run_id": f"child-{i}", "parent_run_id": "parent", "status": "RUNNING",
         "last_progress_summary": "正在处理模块" * 1000,
         "artifact_refs": [f"output/child-{i}/file-{j}.md" for j in range(20)]}
        for i in range(60)
    ])
    with patch(
        "agent_py_agent.agent.agent_core.orchestration.tools.list_agents.agent_tree_status_payload",
        return_value=snapshot,
    ) as query:
        outcome = ListAgentsTool(SimpleNamespace()).execute({"run_id": "parent"})
    query.assert_called_once_with(query.call_args.args[0], {"run_id": "parent", "scope": "own_subtree"})
    full = json.loads(outcome.output)
    assert len(full["nodes"]) == 60
    assert len(full["nodes"][-1]["read_order"]) == 20
    preview_text = outcome.model_visible_output()
    preview = json.loads(preview_text)
    assert len(preview_text) <= 12000
    assert preview["total_nodes"] == len(preview["nodes"]) + preview["omitted_nodes"]
    assert preview["nodes"][0]["omitted_read_refs"] == 19
    assert preview["nodes"][0]["read_order"] == ["output/child-0/file-0.md"]
    rendered = render_tool_result_for_live_prompt(
        _result("list_agents", True, outcome.output, result_envelope=outcome.result_envelope),
        {"output_externalized": True, "artifact_ref": "/private/blob.json", "scoped_call_id": "parent:call-1"},
    )
    assert preview_text in rendered
    assert '"artifact_ref": "parent:call-1"' in rendered
    assert "/private/blob.json" not in rendered
    assert "output_preview:" not in rendered


def test_normal_status_retains_all_eight_child_refs_without_readback():
    payload = agent_tree_model_payload(_snapshot([
        {"run_id": f"child-{i}", "status": "RUNNING", "artifact_refs": [f"output/{i}.md"]}
        for i in range(8)
    ]))
    assert json.loads(agent_tree_model_preview(payload)) == payload


def test_tree_progress_ignores_clock_heartbeat_and_query_activity():
    node = {"run_id": "child", "status": "RUNNING", "last_progress_at": 100.0}
    first = _snapshot([dict(node, updated_at=200, heartbeat_at=200, seconds_since_progress=100)])
    second = _snapshot([dict(node, updated_at=250, heartbeat_at=250, seconds_since_progress=150)])
    second["main"]["current_tool"] = "list_agents"
    left = agent_tree_progress_observation(first, agent_tree_model_payload(first))
    right = agent_tree_progress_observation(second, agent_tree_model_payload(second))
    assert left == right
    assert right["pending"] is True
    assert first["nodes"][0]["seconds_since_progress"] == 100


@pytest.mark.parametrize("source", ["current_runner_context", "explicit_params"])
def test_tree_progress_excludes_only_actual_runner_not_explicit_target(source):
    first = _snapshot([
        {"run_id": "child", "status": "RUNNING", "current_tool": "list_agents", "last_progress_at": 100.0},
        {"run_id": "grandchild", "parent_run_id": "child", "status": "DONE", "last_progress_at": 80.0},
    ])
    first["scope_resolution"] = {"source": source, "effective": {"run_id": "child", "scope": "own_subtree"}}
    second = json.loads(json.dumps(first))
    second["nodes"][0]["last_progress_at"] = 120.0
    left = agent_tree_progress_observation(first, agent_tree_model_payload(first))
    right = agent_tree_progress_observation(second, agent_tree_model_payload(second))
    assert (left == right) is (source == "current_runner_context")
    assert right["pending"] is (source != "current_runner_context")
    assert len(agent_tree_model_payload(second)["nodes"]) == 2
    second["nodes"][1]["artifact_refs"] = ["output/new.md"]
    second["child_result_index"] = child_result_index_from_nodes(second["nodes"])
    changed = agent_tree_progress_observation(second, agent_tree_model_payload(second))
    assert changed["sha256"] != right["sha256"]


@pytest.mark.parametrize("change", [
    {"last_progress_at": 101.0}, {"status": "DONE"},
    {"status": "FAILED", "failure_type": "runner_error"},
    {"artifact_refs": ["output/new.md"]}, {"findings_recorded": 1},
])
def test_tree_real_progress_changes_observation(change):
    node = {"run_id": "child", "status": "RUNNING", "last_progress_at": 100.0}
    first, second = _snapshot([node]), _snapshot([{**node, **change}])
    left = agent_tree_progress_observation(first, agent_tree_model_payload(first))
    right = agent_tree_progress_observation(second, agent_tree_model_payload(second))
    assert left["sha256"] != right["sha256"]
    assert right["pending"] is (second["nodes"][0]["status"] == "RUNNING")


def test_tree_polling_uses_existing_soft_observation_not_a_new_block():
    from agent_py_agent.agent.agent_core.tool_guard.call_guardrail import (
        record_tool_guard_observation,
    )
    from agent_py_agent.agent.tooling.runtime_contracts import ToolResult, ToolSuccessFacts
    from agent_py_agent.tests._tool_runtime_harness import (
        canonical_test_call,
        runtime_snapshot_for_tools,
    )

    agent = SimpleNamespace()
    tool = ListAgentsTool(agent)
    runtime = runtime_snapshot_for_tools({"list_agents": tool}, run_id="parent")
    params = SimpleNamespace(tool_runtime_snapshot=runtime, task_attributes={"repeated_success_hint_threshold": 3})
    warnings = []
    for index in range(7):
        snapshot = _snapshot([{
            "run_id": "child", "status": "RUNNING", "last_progress_at": 1,
            "seconds_since_progress": index, "updated_at": 100 + index,
        }])
        with patch(
            "agent_py_agent.agent.agent_core.orchestration.tools.list_agents.agent_tree_status_payload",
            return_value=snapshot,
        ):
            outcome = tool.execute({})
        call = canonical_test_call(runtime, "list_agents", {}, call_id=f"call-{index}")
        result = ToolResult.succeeded(call, outcome.output, facts=ToolSuccessFacts(
            metadata={"handler_details": outcome.result_envelope},
        ))
        warnings.append(record_tool_guard_observation(agent, params, call, result))
        assert json.loads(result.output)["nodes"][0]["seconds_since_progress"] == index
        assert outcome.ok is True
    assert [i + 1 for i, warning in enumerate(warnings) if warning] == [3, 6]
    assert "不代表进程卡死" in warnings[2]


def test_idle_followup_binds_status_query_to_current_conversation():
    from agent_py_agent.agent.agent_core.agent_tree.status import agent_tree_status_payload
    from agent_py_agent.agent.subagents.kernel import SubagentKernelSnapshot

    class Manager:
        def kernel_snapshot(self, query):
            assert query.scope == "conversation_thread"
            assert query.conversation_thread_id == "thread-current"
            return SubagentKernelSnapshot(schema_version="v1", scope=query.scope)

    agent = SimpleNamespace(
        subagents=Manager(),
        _current_run_params=SimpleNamespace(task_attributes={"conversation_thread_id": "thread-current"}),
    )
    payload = agent_tree_status_payload(agent)
    assert payload["scope_resolution"]["source"] == "current_conversation_context"
    assert payload["scope_resolution"]["effective"]["thread_id"] == "thread-current"
    assert payload["nodes"] == []
