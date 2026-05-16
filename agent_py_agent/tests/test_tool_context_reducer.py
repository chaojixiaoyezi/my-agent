import json

from agent_py_agent.agent.agent_core.tool_context_reducer import render_tool_result_for_live_prompt
from agent_py_agent.agent.tools import ToolExecutionResult


# LLM: dispatch externalization tests protect root context from growing by artifact rereads.
# 函数用途: 验证大型 dispatch_subagents 输出外置后，live prompt 仍保留 refs-first 下一步，而不是诱导模型读正文。
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


# LLM: _dispatch_externalized_output keeps the reducer regression fixture out of the assertion body.
# 函数用途: 构造带 direct_children/recovery 策略的大型 dispatch_subagents 输出。
def _dispatch_externalized_output() -> str:
    return json.dumps(
        {
            "summary": {"runner": 3, "acceptance": 2},
            "records": [{"message": "x" * 2000}],
            "direct_children": _direct_children_recovery_payload(),
        }
    )


# LLM: _direct_children_recovery_payload models the compact next-action facts the reducer must preserve.
# 函数用途: 返回 direct_children 恢复摘要，保护 latest_continue_packet 路径提示不丢失。
def _direct_children_recovery_payload() -> dict:
    return {
        "parent_run_id": "root-1",
        "total": 1,
        "by_status": {"AWAITING_ACCEPTANCE": 1},
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
        "suggested_tool_call": {"tool": "dispatch_subagents", "run_ids": ["child-1"], "execute_runners": True},
    }


# LLM: _dispatch_externalized_archive_record mirrors the tool-output archive metadata shape.
# 函数用途: 给 live prompt reducer 提供外置 artifact 元数据和 scoped call id。
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


# LLM: read_artifact reducer tests prevent repeated full dispatch artifact rereads.
# 函数用途: 验证模型显式读了 dispatch artifact 后，下一轮 prompt 也只保留调度摘要和少量正文预览。
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


# LLM: Board externalization must keep deliverable refs visible to the root synthesis turn.
# 函数用途: subagent_board 输出过大时，live prompt 仍给出可读产物 refs，避免模型乱猜 task_dir。
def test_subagent_board_externalized_result_keeps_deliverable_refs():
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
        ToolExecutionResult("subagent_board", True, output),
        _dispatch_externalized_archive_record(output),
    )

    assert "deliverable_artifact_refs" in rendered
    assert "/tmp/site/final_report.md" in rendered
    assert "refs_policy" in rendered
    assert "do not guess task_dir child paths" in rendered
    assert '"goal"' not in rendered


# LLM: Top-level dispatch completion gates must survive output externalization.
# 函数用途: dispatch 大输出被外置时，root 仍能看到 not_complete 和修复建议，避免先报完成。
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
            "parent_acceptance_repair_advice": {
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
    assert "create_subagents" in rendered
    assert "records" not in rendered


# LLM: Parent acceptance repair advice must survive dispatch output externalization.
# 函数用途: dispatch_subagents 输出过大时，live prompt 摘要仍要保留修复子代理建议，而不是丢掉测试失败线索。
def test_dispatch_externalized_result_keeps_parent_acceptance_repair_advice():
    output = json.dumps(
        {
            "direct_children": {
                "parent_run_id": "root-1",
                "total": 1,
                "by_status": {"AWAITING_ACCEPTANCE": 1},
                "next_action": "create_repair_child_from_parent_acceptance_refs",
                "rejected_acceptance_run_ids": ["child-1"],
                "parent_acceptance_repair_advice": {
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

    assert "parent_acceptance_repair_advice" in rendered
    assert "create_repair_child_from_parent_acceptance_refs" in rendered
    assert "schedule_child_subagents" in rendered
    assert "HTML语法静态检查" in rendered
    assert "records" not in rendered


# LLM: Result refs by run must survive clipping that affects flat artifact lists.
# 函数用途: dispatch 大输出外置后，每个直接子代理的主产物路径仍单独展示，避免 root 只看到第一类报告就猜其它文件名。
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


# LLM: Artifact integrity repair advice must survive orchestration output externalization.
# 函数用途: dispatch 大输出被外置时，live prompt 仍保留产物修复建议，避免 root 去读正文自己修。
def test_dispatch_externalized_result_keeps_artifact_integrity_repair_advice():
    output = json.dumps(
        {
            "next_action": "create_repair_child_from_artifact_integrity_refs",
            "artifact_integrity_repair_advice": {
                "failed_run_ids": ["child-1"],
                "failure_refs": [{
                    "run_id": "child-1",
                    "output_ref": "/tmp/child-1/output.json",
                    "artifact_refs": ["/tmp/site/index.html"],
                    "blockers": ["artifact_integrity_failed:/tmp/site/index.html:missing_html_close"],
                }],
                "suggested_tool_call": {
                    "tool": "create_subagents",
                    "goal": "修复 /tmp/site/index.html",
                },
            },
            "records": [{"message": "z" * 2000}],
        }
    )

    rendered = render_tool_result_for_live_prompt(
        ToolExecutionResult("dispatch_subagents", True, output),
        _dispatch_externalized_archive_record(output),
    )

    assert "artifact_integrity_repair_advice" in rendered
    assert "create_repair_child_from_artifact_integrity_refs" in rendered
    assert "create_subagents" in rendered
    assert "missing_html_close" in rendered
    assert "records" not in rendered


# LLM: legacy nested read_artifact archive records must not invite models to chase artifact-of-artifact files.
# 函数用途: 验证显式 read_artifact 后的 live prompt 只保留原始 artifact 引用，不暴露二次外置 wrapper 路径。
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
