import json

from agent_py_agent.agent.agent_core.tool_context.reducer import render_tool_result_for_live_prompt
from agent_py_agent.agent.tools import ToolExecutionResult


def test_dispatch_externalized_result_keeps_compact_next_action_without_read_hint():
    output = _dispatch_externalized_output()
    rendered = render_tool_result_for_live_prompt(
        ToolExecutionResult("dispatch_subagents", True, output),
        _dispatch_externalized_archive_record(output),
    )

    assert "orchestration_summary" in rendered
    assert "inspect_or_rescue_direct_children" in rendered
    assert "rerun_original_from_continue_packet" in rendered
    assert "latest_continue_packet.json" in rendered
    assert "child-1" in rendered
    assert "output_scoped_call_id: root-1:1-1" in rendered
    assert "prefer output_scoped_call_id" in rendered
    assert "records" not in rendered
    assert "read_artifact_hint" not in rendered
    assert len(rendered) < 1400


def _dispatch_externalized_output() -> str:
    return json.dumps(
        {
            "summary": {"runner": 3, "acceptance": 2},
            "records": [{"message": "x" * 2000}],
            "direct_children": _direct_children_recovery_payload(),
        }
    )


def _direct_children_recovery_payload() -> dict:
    return {
        "parent_run_id": "root-1",
        "total": 1,
        "by_status": {"DONE": 1},
        "needs_recovery": True,
        "next_action": "inspect_or_rescue_direct_children",
        "recovery_run_ids": ["child-1"],
        "recovery_action_counts": {"rerun_original_from_continue_packet": 1},
        "recovery_strategies": [
            {
                "run_id": "child-1",
                "recommended_action": "rerun_original_from_continue_packet",
                "packet_status": "ready",
                "uses_continue_packet": True,
                "runner_instruction": "先读 latest_continue_packet.json 再继续当前步骤",
            }
        ],
        "suggested_tool_call": {"tool": "dispatch_subagents", "run_ids": ["child-1"], "dry_run": False},
    }


def _dispatch_externalized_archive_record(output: str) -> dict:
    return {
        "output_externalized": True,
        "artifact_ref": "/tmp/tool_outputs/dispatch_subagents-1.json",
        "output_path": "/tmp/tool_outputs/dispatch_subagents-1.json",
        "call_id": "1-1",
        "scoped_call_id": "root-1:1-1",
        "output_hash": "abc",
        "output_size_bytes": len(output),
    }


def test_read_artifact_dispatch_content_is_summarized_for_live_prompt():
    dispatch_content = json.dumps(
        {
            "direct_children": {
                "parent_run_id": "root-1",
                "next_action": "create_quality_children_from_ready_refs",
                "quality_advice": {"phase": "quality_wave_ready"},
                "suggested_tool_call": {
                    "tool": "schedule_child_subagents",
                    "children": [
                        {"role": "tester", "agent_name": "tester", "goal": "check refs"},
                    ],
                },
            },
            "records": [{"message": "y" * 3000}],
        }
    )
    output = json.dumps(
        {
            "ok": True,
            "artifact_ref": "/tmp/tool_outputs/dispatch_subagents-1.json",
            "tool": "dispatch_subagents",
            "content": dispatch_content,
            "content_chars": len(dispatch_content),
            "truncated": True,
        }
    )
    rendered = render_tool_result_for_live_prompt(
        ToolExecutionResult("read_artifact", True, output),
        {
            "output_externalized": True,
            "artifact_ref": "/tmp/tool_outputs/read_artifact-2.json",
            "output_path": "/tmp/tool_outputs/read_artifact-2.json",
            "output_hash": "def",
            "output_size_bytes": len(output),
        },
    )

    assert "artifact_read_summary" in rendered
    assert "create_quality_children_from_ready_refs" in rendered
    assert "schedule_child_subagents" in rendered
    assert "records" not in rendered
    assert "read_artifact_hint" not in rendered
    assert len(rendered) < 1600


def test_inspect_agent_tree_externalized_result_keeps_deliverable_refs():
    output = json.dumps(
        {
            "summary": {"DONE": 2, "VERIFIED": 2},
            "deliverable_artifact_refs": ["/tmp/site/final_report.md"],
            "deliverable_evidence_refs": ["/tmp/site/evidence.json"],
            "items": [
                {
                    "id": "child-1",
                    "status": "DONE",
                    "artifact_refs": ["/tmp/site/final_report.md"],
                    "evidence_refs": ["/tmp/site/evidence.json"],
                    "goal": "x" * 2000,
                }
            ],
        }
    )

    rendered = render_tool_result_for_live_prompt(
        ToolExecutionResult("inspect_agent_tree", True, output),
        _dispatch_externalized_archive_record(output),
    )

    assert "deliverable_artifact_refs" in rendered
    assert "/tmp/site/final_report.md" in rendered
    assert "refs_policy" in rendered
    assert "do not guess task_dir child paths" in rendered
    assert '"goal"' not in rendered


def test_inspect_agent_tree_externalized_result_keeps_deliverable_artifact_ids():
    output = json.dumps(
        {
            "summary": {"DONE": 1, "VERIFIED": 1},
            "deliverable_artifact_ids": ["artifact-report-1"],
            "deliverable_artifact_refs": ["/tmp/site/final_report.md"],
            "items": [
                {
                    "id": "child-1",
                    "status": "DONE",
                    "artifact_ids": ["artifact-report-1"],
                    "artifact_refs": ["/tmp/site/final_report.md"],
                    "artifact_registry_refs": [
                        {
                            "artifact_id": "artifact-report-1",
                            "path": "/tmp/site/final_report.md",
                            "status": "ready",
                        }
                    ],
                }
            ],
        }
    )

    rendered = render_tool_result_for_live_prompt(
        ToolExecutionResult("inspect_agent_tree", True, output),
        _dispatch_externalized_archive_record(output),
    )

    assert "deliverable_artifact_ids" in rendered
    assert "artifact-report-1" in rendered


def test_dispatch_externalized_result_keeps_top_level_completion_gate():
    output = json.dumps(
        {
            "completion_status": {
                "status": "not_complete",
                "blocking_run_ids": ["child-bad"],
                "must_not_report_done": True,
            },
            "must_not_report_done": True,
            "blocking_run_ids": ["child-bad"],
            "next_action": "repair_or_continue_blocking_run_ids",
            "final_closeout_repair_advice": {
                "failed_run_ids": ["child-bad"],
                "suggested_tool_call": {"tool": "create_subagents", "goal": "修复 child-bad"},
            },
            "records": [{"message": "z" * 2000}],
        }
    )

    rendered = render_tool_result_for_live_prompt(
        ToolExecutionResult("dispatch_subagents", True, output),
        _dispatch_externalized_archive_record(output),
    )

    assert "not_complete" in rendered
    assert "must_not_report_done" in rendered
    assert "child-bad" in rendered
    assert "repair_or_continue_blocking_run_ids" in rendered
    assert "create_subagents" not in rendered
    assert "records" not in rendered


def test_orchestration_externalized_result_keeps_current_turn_run_state():
    output = json.dumps(
        {
            "created_run_ids": ["child-new"],
            "current_turn_run_state": {
                "total": 1,
                "by_status": {"PLANNING": 1},
                "dispatchable_run_ids": ["child-new"],
                "next_action": "continue_dispatch_unfinished_run_ids",
                "suggested_tool_call": {"tool": "dispatch_subagents", "run_ids": ["child-new"]},
            },
            "records": [{"message": "z" * 2000}],
        }
    )

    rendered = render_tool_result_for_live_prompt(
        ToolExecutionResult("create_subagents", True, output),
        _dispatch_externalized_archive_record(output),
    )

    assert "current_turn_run_state" in rendered
    assert "continue_dispatch_unfinished_run_ids" in rendered
    assert "child-new" in rendered
    assert "dispatch_subagents" in rendered
    assert "records" not in rendered


def test_dispatch_externalized_result_keeps_final_closeout_repair_advice():
    output = json.dumps(
        {
            "direct_children": {
                "parent_run_id": "root-1",
                "total": 1,
                "by_status": {"DONE": 1},
                "next_action": "create_repair_child_from_final_closeout_refs",
                "rejected_acceptance_run_ids": ["child-1"],
                "final_closeout_repair_advice": {
                    "failed_run_ids": ["child-1"],
                    "failure_refs": [{"run_id": "child-1", "test_ref": "/tmp/test_execution.json"}],
                    "suggested_tool_call": {
                        "tool": "schedule_child_subagents",
                        "children": [{"role": "worker", "goal": "修复 HTML语法静态检查"}],
                    },
                },
            },
            "records": [{"message": "z" * 2000}],
        }
    )

    rendered = render_tool_result_for_live_prompt(
        ToolExecutionResult("dispatch_subagents", True, output),
        _dispatch_externalized_archive_record(output),
    )

    assert "final_closeout_repair_advice" not in rendered
    assert "schedule_child_subagents" not in rendered
    assert "records" not in rendered


def test_dispatch_externalized_result_keeps_top_level_parent_repair_tool_call():
    output = json.dumps(
        {
            "completion_status": {"status": "not_complete"},
            "blocking_run_ids": ["child-1"],
            "final_closeout_repair_advice": {
                "failed_run_ids": ["child-1"],
                "failure_refs": [{"run_id": "child-1", "test_ref": "/tmp/test_execution.json"}],
                "suggested_tool_call": {
                    "tool": "create_subagents",
                    "count": 1,
                    "role": "worker",
                    "agent_name": "小傻妞-验收修复",
                    "goal": "修复最终收口失败的 child-1，读取 test_execution.json 后只修复被点名的问题。",
                },
            },
            "records": [{"message": "z" * 2000}],
        }
    )

    rendered = render_tool_result_for_live_prompt(
        ToolExecutionResult("dispatch_subagents", True, output),
        _dispatch_externalized_archive_record(output),
    )

    assert "final_closeout_repair_next_tool" not in rendered
    assert "final_closeout_repair_suggested_tool_call" not in rendered
    assert "records" not in rendered


def test_dispatch_externalized_result_keeps_result_refs_by_run():
    output = json.dumps(
        {
            "summary": {"runner": 3},
            "result_refs_by_run": [
                {
                    "run_id": "market",
                    "status": "DONE",
                    "verification_status": "VERIFIED",
                    "summary": "市场环境报告完成",
                    "primary_artifact_refs": ["/tmp/subagents/market/market_env_comprehensive_report.md"],
                    "output_json": "/tmp/subagents/market/output.json",
                },
                {
                    "run_id": "competitor",
                    "status": "DONE",
                    "verification_status": "VERIFIED",
                    "summary": "竞争格局报告完成",
                    "primary_artifact_refs": ["/tmp/subagents/competitor/competitive_landscape_report.md"],
                    "primary_artifact_summaries": [{
                        "path": "/tmp/subagents/competitor/competitive_landscape_report.md",
                        "kind": "report",
                        "summary": "竞品矩阵和差异化机会",
                    }],
                    "output_json": "/tmp/subagents/competitor/output.json",
                },
            ],
            "deliverable_artifact_refs": ["/tmp/too-many/market.md", *[f"/tmp/too-many/ref-{i}.md" for i in range(60)]],
            "records": [{"message": "z" * 2000}],
        }
    )

    rendered = render_tool_result_for_live_prompt(
        ToolExecutionResult("dispatch_subagents", True, output),
        _dispatch_externalized_archive_record(output),
    )

    assert "result_refs_by_run" in rendered
    assert "/tmp/subagents/market/market_env_comprehensive_report.md" in rendered
    assert "/tmp/subagents/competitor/competitive_landscape_report.md" in rendered
    assert "竞品矩阵和差异化机会" in rendered
    assert "do not guess child filenames" in rendered
    assert "records" not in rendered


def test_dispatch_externalized_result_keeps_result_ref_artifact_ids():
    output = json.dumps(
        {
            "result_refs_by_run": [
                {
                    "run_id": "market",
                    "status": "DONE",
                    "verification_status": "VERIFIED",
                    "primary_artifact_ids": ["artifact-market-1"],
                    "primary_artifact_refs": ["/tmp/subagents/market/report.md"],
                    "primary_artifact_registry_refs": [
                        {
                            "artifact_id": "artifact-market-1",
                            "path": "/tmp/subagents/market/report.md",
                            "status": "ready",
                        }
                    ],
                },
            ],
        }
    )

    rendered = render_tool_result_for_live_prompt(
        ToolExecutionResult("dispatch_subagents", True, output),
        _dispatch_externalized_archive_record(output),
    )

    assert "primary_artifact_ids" in rendered
    assert "artifact-market-1" in rendered


def test_read_artifact_summary_hides_nested_wrapper_artifact_path():
    output = json.dumps(
        {
            "ok": True,
            "artifact_ref": "/tmp/tool_outputs/read_file-1.json",
            "tool": "read_file",
            "call_id": "1-1",
            "content": "latest_continue_packet body" + ("x" * 2000),
            "content_chars": 2027,
            "truncated": True,
            "reads_artifact_body": True,
        }
    )
    rendered = render_tool_result_for_live_prompt(
        ToolExecutionResult("read_artifact", True, output),
        {
            "output_externalized": True,
            "artifact_ref": "/tmp/tool_outputs/read_artifact-2.json",
            "output_path": "/tmp/tool_outputs/read_artifact-2.json",
            "output_hash": "def",
            "output_size_bytes": len(output),
        },
    )

    assert "artifact_read_summary" in rendered
    assert "source_artifact_ref: /tmp/tool_outputs/read_file-1.json" in rendered
    assert "read_artifact-2.json" not in rendered
    assert "read_artifact_hint" not in rendered
