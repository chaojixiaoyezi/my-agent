import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent_py_agent.agent.agent_core.agent_tree.model_view import (
    agent_tree_model_payload,
    agent_tree_model_preview,
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
