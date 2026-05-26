"""LLM: focused tests for runner-created child refs in dispatch payloads.

模块用途: 验证父级 dispatch 输出能看到 runner 实际创建的下级数量、id 和角色，避免把调度记录数误当成孩子数。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent_py_agent.agent.agent_core.dispatch_params import DispatchContext
from agent_py_agent.agent.agent_core.dispatch_runner_batches import _runner_candidates_for_context
from agent_py_agent.agent.agent_core.dispatch_runner_selection import (
    scoped_current_turn_runner_tasks,
    scoped_runner_tasks,
)
from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.hierarchy_scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)


# LLM: _dispatch_payload_for_record keeps payload tests focused and below size guard limits.
# 函数用途: 用单条 dispatch record 构造工具返回 payload，避免每个测试重复 mock report/agent。
def _dispatch_payload_for_record(record: SimpleNamespace) -> dict:
    mock_report = MagicMock()
    mock_report.dry_run = False
    mock_report.summary = {record.step: 1}
    mock_report.records = [record]
    mock_agent = MagicMock()
    mock_agent.subagents.workspace = Path("/tmp/workspace")
    mock_agent.subagents.list_runs.return_value = []
    return DispatchSubagentsTool(mock_agent)._report_payload(mock_report)


# LLM: _dispatch_payload_with_direct_children builds a runner-context dispatch payload fixture.
# 函数用途: 构造带当前 parent runner 和 direct child 状态的 payload，验证继续调度指令。
def _dispatch_payload_with_direct_children(
    children: list[SimpleNamespace],
    *,
    parent: SimpleNamespace | None = None,
) -> dict:
    mock_report = MagicMock()
    mock_report.dry_run = False
    mock_report.summary = {"total": 0}
    mock_report.records = []
    mock_agent = MagicMock()
    mock_agent._current_subagent_run_id = "root"
    mock_agent.subagents.workspace = Path("/tmp/workspace")
    mock_agent.subagents.list_runs.return_value = children
    if parent is not None:
        by_id = {str(parent.id): parent, **{str(item.id): item for item in children}}
        mock_agent.subagents.load.side_effect = lambda run_id: by_id[str(run_id)]
    return DispatchSubagentsTool(mock_agent)._report_payload(mock_report)


# LLM: test_dispatch_payload_exposes_runner_created_children protects parent progress handoff.
# 函数用途: 顶层主代理推进 root 后，要能直接看到 root runner 创建的孩子事实。
def test_dispatch_payload_exposes_runner_created_children():
    record = SimpleNamespace(
        step="runner",
        action="execute_runner",
        run_id="root",
        ok=True,
        dry_run=False,
        applied=True,
        message="runner 已完成模型调用，等待独立验收。",
        before_status="PLANNING",
        after_status="AWAITING_ACCEPTANCE",
        runner_summary="root 创建了 2 个直接孩子。",
        runner_created_child_count=2,
        runner_created_child_ids=["child-a", "child-b"],
        runner_created_roles=["researcher", "worker"],
        runner_child_status_counts={"DONE": 1, "PLANNING": 1},
        runner_unfinished_child_ids=["child-b"],
        runner_partial_success=True,
    )
    payload = _dispatch_payload_for_record(record)

    assert payload["records"][0]["runner_created_child_count"] == 2
    assert payload["records"][0]["runner_created_child_ids"] == ["child-a", "child-b"]
    assert payload["records"][0]["runner_created_roles"] == ["researcher", "worker"]
    assert payload["records"][0]["runner_child_status_counts"] == {"DONE": 1, "PLANNING": 1}
    assert payload["records"][0]["runner_unfinished_child_ids"] == ["child-b"]
    assert payload["records"][0]["runner_partial_success"] is True
    assert "root 创建了 2 个直接孩子" in payload["records"][0]["runner_summary"]


# LLM: test_dispatch_payload_includes_acceptance_followup keeps rescue hints visible to runner context.
# 函数用途: runner 内部要能看到 child 测试失败和 follow-up 动作，才可能继续救援。
def test_dispatch_payload_includes_acceptance_followup():
    record = SimpleNamespace(
        step="acceptance",
        action="reject",
        run_id="leaf-1",
        ok=False,
        dry_run=True,
        applied=False,
        message="验收失败。",
        before_status="AWAITING_ACCEPTANCE",
        after_status="AWAITING_ACCEPTANCE",
        parent_acceptance_auto_execution_test_failed=5,
        parent_acceptance_test_failure_summary="inferred static site check: inert_control_hits=15",
        parent_acceptance_test_failure_details=[
            "inert_control_hits: index2.html:a:Collection href=#; index2.html:a:Contact href=#missing",
        ],
        parent_acceptance_followup_action="plan_rescue",
        parent_acceptance_followup_command="subagents-acceptance-plan leaf-1 --take-over-by <agent>",
    )
    payload = _dispatch_payload_for_record(record)

    assert payload["records"][0]["test_failed"] == 5
    assert payload["records"][0]["test_failure_summary"] == "inferred static site check: inert_control_hits=15"
    assert payload["records"][0]["test_failure_details"] == [
        "inert_control_hits: index2.html:a:Collection href=#; index2.html:a:Contact href=#missing",
    ]
    assert payload["records"][0]["followup_action"] == "plan_rescue"
    assert payload["records"][0]["followup_command"].endswith("--take-over-by <agent>")


def test_dispatch_payload_lifts_parent_acceptance_advice_before_records():
    """父级验收返工建议必须出现在长 records 前，避免真实模型只读前段时错过。"""
    record = SimpleNamespace(
        step="acceptance",
        action="reject",
        run_id="leaf-1",
        ok=False,
        dry_run=False,
        applied=True,
        message="产物缺少目标文件，需要按 failure refs 修复。",
        before_status="AWAITING_ACCEPTANCE",
        after_status="BLOCKED",
        parent_acceptance_auto_execution_test_failed=0,
        parent_acceptance_test_failure_summary="missing target artifact",
        parent_acceptance_test_failure_details=[],
        parent_acceptance_followup_action="",
        parent_acceptance_followup_command="",
    )

    payload = _dispatch_payload_for_record(record)

    keys = list(payload)
    assert payload["next_action"] == "create_repair_child_from_parent_acceptance_refs"
    assert payload["parent_acceptance_repair_advice"]["next_action"] == "create_repair_child_from_parent_acceptance_refs"
    assert keys.index("parent_acceptance_repair_advice") < keys.index("records")
    assert "suggested_tool_call" not in payload


# LLM: dispatch payload should tell models when only audit/classify actions remain.
# 函数用途: 防止父模型看到重复 due-check/classify 记录后继续无限调用 dispatch_subagents。
def test_dispatch_payload_marks_no_progress_terminal_actions():
    mock_report = MagicMock()
    mock_report.dry_run = False
    mock_report.summary = {"total": 2}
    mock_report.records = [
        SimpleNamespace(
            step="due_check",
            action="scan",
            run_id="",
            ok=True,
            dry_run=False,
            applied=False,
            message="scan",
            before_status="",
            after_status="",
        ),
        SimpleNamespace(
            step="action_apply",
            action="classify_blocker",
            run_id="blocked-run",
            ok=True,
            dry_run=False,
            applied=True,
            message="classified",
            before_status="BLOCKED",
            after_status="BLOCKED",
        ),
    ]
    mock_agent = MagicMock()
    mock_agent.subagents.workspace = Path("/tmp/workspace")
    mock_agent.subagents.list_runs.return_value = []

    payload = DispatchSubagentsTool(mock_agent)._report_payload(mock_report)

    terminal = payload["dispatch_terminal"]
    assert terminal["no_progress_actions_only"] is True
    assert terminal["recommended_next_action"] == "stop_dispatch_and_report_blockers"
    assert terminal["blocked_run_ids"] == ["blocked-run"]


# LLM: dry-run takeover previews should give parents an exact apply call instead of a terminal stop.
# 函数用途: 防止父 runner 把接管预览当成完成或阻塞，导致不接管又反复新建 repair。
def test_dispatch_payload_suggests_apply_for_dry_run_recovery_actions():
    mock_report = MagicMock()
    mock_report.dry_run = True
    mock_report.summary = {"total": 2}
    mock_report.records = [
        SimpleNamespace(
            step="due_check",
            action="scan",
            run_id="",
            ok=True,
            dry_run=True,
            applied=False,
            message="scan",
            before_status="",
            after_status="",
        ),
        SimpleNamespace(
            step="action_apply",
            action="takeover_or_reassign",
            run_id="stale-run",
            ok=True,
            dry_run=True,
            applied=False,
            message="dry-run: would takeover",
            before_status="RUNNING",
            after_status="RUNNING",
        ),
    ]
    mock_agent = MagicMock()
    mock_agent.subagents.workspace = Path("/tmp/workspace")
    mock_agent.subagents.list_runs.return_value = []

    payload = DispatchSubagentsTool(mock_agent)._report_payload(mock_report)

    terminal = payload["dispatch_terminal"]
    assert terminal["recommended_next_action"] == "rerun_dispatch_with_apply_for_recovery"
    assert terminal["suggested_tool_call"]["apply"] is True
    assert terminal["suggested_tool_call"]["execute_runners"] is False


# LLM: test_dispatch_payload_exposes_recovery_valid_run_ids covers model retry ergonomics.
# 函数用途: 模型传错 run_id 时，顶层恢复 payload 要直接给机器可读的 valid_run_ids。
def test_dispatch_payload_exposes_recovery_valid_run_ids():
    record = SimpleNamespace(
        step="runner_selection",
        action="invalid_run_ids",
        run_id="",
        ok=False,
        dry_run=False,
        applied=False,
        message="blocked",
        before_status="",
        after_status="",
        evidence_paths=["/tmp/subagents/child-a", "/tmp/subagents/child-b"],
    )
    payload = _dispatch_payload_for_record(record)

    recovery = payload["runner_selection_recovery"]
    assert recovery["action"] == "retry_dispatch_with_valid_run_id"
    assert recovery["valid_run_ids"] == ["child-a", "child-b"]
    assert recovery["valid_task_refs"] == ["/tmp/subagents/child-a", "/tmp/subagents/child-b"]


# LLM: test_dispatch_payload_tells_runner_to_continue_unfinished_children covers partial runner waves.
# 函数用途: runner 内还有 PLANNING/RUNNING 直接孩子时，payload 必须给出机器可读的继续调度动作。
def test_dispatch_payload_tells_runner_to_continue_unfinished_children():
    payload = _dispatch_payload_with_direct_children([
        SimpleNamespace(id="child-a", parent_id="root", status="PLANNING"),
        SimpleNamespace(id="child-b", parent_id="root", status="RUNNING"),
        SimpleNamespace(id="child-c", parent_id="root", status="DONE"),
    ])

    direct = payload["direct_children"]
    assert direct["needs_more_dispatch"] is True
    assert direct["unfinished_run_ids"] == ["child-a", "child-b"]
    assert direct["next_action"] == "continue_dispatch_direct_children"
    assert direct["suggested_tool_call"]["tool"] == "dispatch_subagents"
    assert direct["suggested_tool_call"]["execute_runners"] is True
    assert direct["suggested_tool_call"]["run_ids"] == ["child-a", "child-b"]
    assert direct["suggested_tool_call"]["workflow_mode"] == "off"


# LLM: test_dispatch_payload_tells_runner_to_summarize_ready_children covers R5 over-read prevention.
# 函数用途: 直接孩子都等待验收或完成时，父 runner 应收口汇总 refs，不该反复读取子产物正文。
def test_dispatch_payload_tells_runner_to_summarize_ready_children():
    payload = _dispatch_payload_with_direct_children([
        SimpleNamespace(id="child-a", parent_id="root", status="AWAITING_ACCEPTANCE"),
        SimpleNamespace(id="child-b", parent_id="root", status="DONE"),
    ])

    direct = payload["direct_children"]
    assert direct["ready_for_parent_acceptance"] is True
    assert direct["next_action"] == "summarize_direct_children_refs"
    assert "不要反复 read_file/read_artifact" in direct["closeout_hint"]


# LLM: Required QA roles should steer parent to create QA children instead of rereading artifacts.
# 函数用途: 父级目标点名 tester/bug_finder/acceptor 且实现已 ready 时，dispatch payload 要提示补 QA 波次。
def test_dispatch_payload_suggests_quality_wave_before_closeout():
    parent = SimpleNamespace(
        id="root",
        goal="示例网站必须有 tester / bug_finder / acceptor 三类 QA 子代理。",
        acceptance_checks=[],
        child_ids=["child-a"],
        allowed_write_roots=["/tmp/site/build"],
        task_dir="",
        role="coordinator",
        agent_name="root",
        attributes={"required_qa_roles": ["tester", "bug_finder", "acceptor"]},
    )
    child = SimpleNamespace(
        id="child-a",
        parent_id="root",
        status="AWAITING_ACCEPTANCE",
        verification_status="NEEDS_ACCEPTANCE",
        role="leaf_worker",
        agent_name="小傻妞-worker",
        child_ids=[],
    )
    payload = _dispatch_payload_with_direct_children([child], parent=parent)

    direct = payload["direct_children"]
    assert direct["ready_for_parent_acceptance"] is False
    assert direct["next_action"] == "create_quality_children_from_ready_refs"
    assert direct["quality_advice"]["phase"] == "quality_wave_ready"
    assert set(direct["quality_advice"]["suggested_roles"]) == {"tester", "bug_finder", "acceptor"}
    assert direct["quality_advice"]["ready_work_refs"][0]["run_id"] == "child-a"
    assert direct["quality_advice"]["suggested_children"][0]["source_run_ids"] == ["child-a"]


# LLM: QA self-reported failures should guide repair without forcing an automatic workflow.
# 函数用途: tester 自己报告流程缺失时，dispatch payload 要返回 repair advice，而不是让父级直接收口。
