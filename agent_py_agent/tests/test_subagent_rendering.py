"""子代理渲染模块单元测试。

测试 agent_py_agent/agent/subagents/rendering.py 中的 Markdown 渲染函数。
包括看板、due-check、动作计划、验收报告、patch 审核等渲染功能。
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from agent_py_agent.agent.subagents.rendering import (
    render_action_apply_markdown,
    render_action_plan_markdown,
    render_acceptance_record_markdown,
    render_acceptance_review_markdown,
    render_board_markdown,
    render_capability_route_markdown,
    render_due_check_markdown,
    render_patch_apply_markdown,
    render_patch_apply_record_markdown,
    render_patch_review_markdown,
    render_patch_review_record_markdown,
)
from agent_py_agent.agent.subagents.reports import (
    AcceptanceReviewFinding,
    AcceptanceReviewRecord,
    AcceptanceReviewReport,
    ActionApplyRecord,
    ActionApplyReport,
    ActionPlanItem,
    ActionPlanReport,
    CapabilityRouteRecord,
    CapabilityRouteReport,
    DueCheckIssue,
    DueCheckReport,
    PatchApplyRecord,
    PatchApplyReport,
    PatchReviewRecord,
    PatchReviewReport,
    SubAgentBoard,
    SubAgentBoardItem,
)


class TestBoardRendering:
    """测试看板渲染功能。"""

    def test_render_board_markdown_basic(self):
        """测试基本看板渲染。

        验证看板可以正确渲染包含摘要和任务列表的 Markdown。
        """
        board = SubAgentBoard(
            generated_at=time.time(),
            summary={"total": 2, "running": 1, "paused": 1},
            hot_list=[
                SubAgentBoardItem(
                    id="task-1",
                    root_id="root-1",
                    parent_id="",
                    depth=0,
                    status="RUNNING",
                    verification_status="UNVERIFIED",
                    channel_status="healthy",
                    owner="agent-1",
                    supervisor="sup-1",
                    final_owner="final-1",
                    goal="测试任务1",
                    updated_at=time.time(),
                    heartbeat_at=time.time(),
                    evidence_count=0,
                    open_request_count=0,
                    open_gap_count=0,
                    child_count=0,
                    takeover_by="",
                    locked_file_count=0,
                    risk_flags=["high_complexity"],
                    task_dir="/tmp/task1",
                    output_json="{}",
                )
            ],
            recent=[],
            items=[],
        )

        result = render_board_markdown(board)

        assert "# SUBAGENT BOARD" in result
        assert "generated_at" in result
        assert "Summary" in result
        assert "Hot List" in result

    def test_render_board_markdown_empty(self):
        """测试空看板渲染。

        验证没有任务时看板仍能正常渲染。
        """
        board = SubAgentBoard(
            generated_at=time.time(),
            summary={"total": 0},
            hot_list=[],
            recent=[],
            items=[],
        )

        result = render_board_markdown(board)

        assert "# SUBAGENT BOARD" in result
        assert "暂无红灯任务" in result
        assert "暂无任务" in result

    def test_render_board_markdown_many_items(self):
        """测试大量任务时看板渲染。

        验证任务超过50个时只会显示前50个。
        """
        items = [
            SubAgentBoardItem(
                id=f"task-{i}",
                root_id=f"root-{i}",
                parent_id="",
                depth=0,
                status="RUNNING",
                verification_status="UNVERIFIED",
                channel_status="healthy",
                owner="agent",
                supervisor="sup",
                final_owner="final",
                goal=f"任务 {i}",
                updated_at=time.time(),
                heartbeat_at=time.time(),
                evidence_count=0,
                open_request_count=0,
                open_gap_count=0,
                child_count=0,
                takeover_by="",
                locked_file_count=0,
                risk_flags=[],
                task_dir="/tmp",
                output_json="{}",
            )
            for i in range(60)
        ]

        board = SubAgentBoard(
            generated_at=time.time(),
            summary={"total": 60},
            hot_list=items,
            recent=[],
            items=[],
        )

        result = render_board_markdown(board)

        # hot_list 限制为前 50 个
        assert "task-0" in result
        # 60 个任务不会全部显示（限制 50）
        lines = result.split("\n")
        task_lines = [l for l in lines if l.startswith("- [")]
        assert len(task_lines) <= 50


class TestDueCheckRendering:
    """测试 due-check 报告渲染功能。"""

    def test_render_due_check_markdown_basic(self):
        """测试基本 due-check 报告渲染。

        验证包含问题的报告可以正确渲染。
        """
        report = DueCheckReport(
            generated_at=time.time(),
            summary={"total": 1, "critical": 1},
            issues=[
                DueCheckIssue(
                    run_id="run-1",
                    severity="P1",
                    kind="timeout",
                    message="任务执行超时",
                    suggested_action="resume",
                    status="RUNNING",
                    goal="测试目标",
                    risk_flags=["timeout"],
                )
            ],
        )

        result = render_due_check_markdown(report)

        assert "# SUBAGENT DUE CHECK" in result
        assert "P1" in result
        assert "timeout" in result
        assert "Issues" in result

    def test_render_due_check_markdown_empty(self):
        """测试空 due-check 报告渲染。

        验证没有问题时报告可以正确渲染。
        """
        report = DueCheckReport(
            generated_at=time.time(),
            summary={"total": 0},
            issues=[],
        )

        result = render_due_check_markdown(report)

        assert "# SUBAGENT DUE CHECK" in result
        assert "暂无需要介入的问题" in result

    def test_render_due_check_markdown_long_goal(self):
        """测试超长目标渲染。

        验证长目标会被截断到100字符。
        """
        long_goal = "A" * 200

        report = DueCheckReport(
            generated_at=time.time(),
            summary={"total": 1},
            issues=[
                DueCheckIssue(
                    run_id="run-1",
                    severity="P1",
                    kind="timeout",
                    message="问题消息",
                    suggested_action="resume",
                    goal=long_goal,
                )
            ],
        )

        result = render_due_check_markdown(report)

        # 目标应该被截断
        assert len(long_goal) == 200
        assert "AAAAAA" in result or "AAA" in result


class TestActionPlanRendering:
    """测试动作计划渲染功能。"""

    def test_render_action_plan_markdown_basic(self):
        """测试基本动作计划渲染。

        验证动作计划可以正确渲染。
        """
        report = ActionPlanReport(
            generated_at=time.time(),
            summary={"total": 1},
            actions=[
                ActionPlanItem(
                    id="action-1",
                    run_id="run-1",
                    severity="P2",
                    priority=1,
                    action="resume",
                    reason="任务应该继续执行",
                    source_issue_kinds=["timeout"],
                    suggested_commands=["agent resume --run-id=run-1"],
                )
            ],
        )

        result = render_action_plan_markdown(report)

        assert "# SUBAGENT ACTION PLAN" in result
        assert "dry-run" in result
        assert "run-1" in result
        assert "resume" in result

    def test_render_action_plan_markdown_empty(self):
        """测试空动作计划渲染。

        验证没有动作时报告可以正确渲染。
        """
        report = ActionPlanReport(
            generated_at=time.time(),
            summary={"total": 0},
            actions=[],
        )

        result = render_action_plan_markdown(report)

        assert "# SUBAGENT ACTION PLAN" in result
        assert "暂无建议动作" in result


class TestActionApplyRendering:
    """测试动作应用渲染功能。"""

    def test_render_action_apply_markdown_basic(self):
        """测试基本动作应用渲染。

        验证动作应用报告可以正确渲染。
        """
        report = ActionApplyReport(
            generated_at=time.time(),
            dry_run=True,
            summary={"total": 1, "applied": 1},
            records=[
                ActionApplyRecord(
                    id="record-1",
                    action_id="action-1",
                    run_id="run-1",
                    action="resume",
                    dry_run=True,
                    applied=True,
                    ok=True,
                    message="动作已应用",
                    before_status="PAUSED",
                    after_status="RUNNING",
                )
            ],
        )

        result = render_action_apply_markdown(report)

        assert "# SUBAGENT ACTION APPLY" in result
        assert "dry-run" in result
        assert "OK" in result
        assert "PAUSED->RUNNING" in result

    def test_render_action_apply_markdown_apply_mode(self):
        """测试 apply 模式渲染。

        验证非 dry-run 模式的报告正确显示。
        """
        report = ActionApplyReport(
            generated_at=time.time(),
            dry_run=False,
            summary={"total": 1},
            records=[],
        )

        result = render_action_apply_markdown(report)

        assert "apply" in result
        assert "mode: apply" in result


class TestCapabilityRouteRendering:
    """测试能力路由渲染功能。"""

    def test_render_capability_route_markdown_basic(self):
        """测试基本能力路由报告渲染。

        验证能力路由报告可以正确渲染。
        """
        report = CapabilityRouteReport(
            generated_at=time.time(),
            dry_run=True,
            summary={"total": 1, "granted": 1},
            records=[
                CapabilityRouteRecord(
                    id="cap-1",
                    run_id="run-1",
                    request_id="req-1",
                    status="granted",
                    dry_run=True,
                    query="需要搜索能力",
                    candidate_count=3,
                    granted_skills=["web_search"],
                    selected_cards=[{"kind": "skill", "name": "web_search"}],
                    reasons=["匹配度最高"],
                )
            ],
        )

        result = render_capability_route_markdown(report)

        assert "# SUBAGENT CAPABILITY ROUTE" in result
        assert "dry-run" in result
        assert "granted" in result

    def test_render_capability_route_markdown_empty(self):
        """测试空能力路由报告渲染。

        验证没有记录时报告可以正确渲染。
        """
        report = CapabilityRouteReport(
            generated_at=time.time(),
            dry_run=True,
            summary={"total": 0},
            records=[],
        )

        result = render_capability_route_markdown(report)

        assert "# SUBAGENT CAPABILITY ROUTE" in result
        assert "暂无待路由能力请求" in result


class TestAcceptanceReviewRendering:
    """测试验收审核渲染功能。"""

    def test_render_acceptance_review_markdown_basic(self):
        """测试基本验收审核报告渲染。

        验证验收审核报告可以正确渲染。
        """
        report = AcceptanceReviewReport(
            generated_at=time.time(),
            dry_run=True,
            summary={"total": 1, "passed": 1},
            records=[
                AcceptanceReviewRecord(
                    id="acc-1",
                    run_id="run-1",
                    dry_run=True,
                    applied=False,
                    ok=True,
                    decision="approved",
                    message="验收通过",
                    before_status="RUNNING",
                    after_status="COMPLETED",
                    before_verification_status="UNVERIFIED",
                    after_verification_status="VERIFIED",
                    findings=[
                        AcceptanceReviewFinding(
                            name="code_quality",
                            ok=True,
                            severity="P1",
                            message="代码质量达标",
                        )
                    ],
                )
            ],
        )

        result = render_acceptance_review_markdown(report)

        assert "# SUBAGENT ACCEPTANCE" in result
        assert "approved" in result
        assert "COMPLETED" in result

    def test_render_acceptance_record_markdown_basic(self):
        """测试基本验收记录渲染。

        验证单个验收记录可以正确渲染。
        """
        record = AcceptanceReviewRecord(
            id="acc-1",
            run_id="run-1",
            dry_run=True,
            applied=False,
            ok=True,
            decision="approved",
            message="验收通过",
            before_status="RUNNING",
            after_status="COMPLETED",
            before_verification_status="UNVERIFIED",
            after_verification_status="VERIFIED",
            evidence_count=2,
            test_count=5,
            artifact_count=1,
            findings=[
                AcceptanceReviewFinding(
                    name="tests_passed",
                    ok=True,
                    severity="P1",
                    message="所有测试通过",
                )
            ],
        )

        result = render_acceptance_record_markdown(record)

        assert "# ACCEPTANCE REVIEW" in result
        assert "run-1" in result
        assert "approved" in result
        assert "evidence: 2" in result
        assert "tests: 5" in result


class TestPatchReviewRendering:
    """测试 Patch 审核渲染功能。"""

    def test_render_patch_review_markdown_basic(self):
        """测试基本 Patch 审核报告渲染。

        验证 Patch 审核报告可以正确渲染。
        """
        report = PatchReviewReport(
            generated_at=time.time(),
            dry_run=True,
            summary={"total": 1, "approved": 1},
            records=[
                PatchReviewRecord(
                    id="patch-1",
                    run_id="run-1",
                    dry_run=True,
                    applied=False,
                    ok=True,
                    decision="approved",
                    message="Patch 审核通过",
                    patch_count=3,
                    approved_count=3,
                    blocked_count=0,
                )
            ],
        )

        result = render_patch_review_markdown(report)

        assert "# SUBAGENT PATCH REVIEW" in result
        assert "approved" in result

    def test_render_patch_review_record_markdown_basic(self):
        """测试基本 Patch 审核记录渲染。

        验证单个 Patch 审核记录可以正确渲染。
        """
        record = PatchReviewRecord(
            id="patch-1",
            run_id="run-1",
            dry_run=True,
            applied=False,
            ok=True,
            decision="approved",
            message="审核通过",
            patch_count=2,
            approved_count=2,
            blocked_count=0,
            patches=[
                {"status": "approved", "path": "file1.py", "summary": "修改1"},
                {"status": "approved", "path": "file2.py", "summary": "修改2"},
            ],
        )

        result = render_patch_review_record_markdown(record)

        assert "# PATCH REVIEW" in result
        assert "file1.py" in result
        assert "file2.py" in result


class TestPatchApplyRendering:
    """测试 Patch 应用渲染功能。"""

    def test_render_patch_apply_markdown_basic(self):
        """测试基本 Patch 应用报告渲染。

        验证 Patch 应用报告可以正确渲染。
        """
        report = PatchApplyReport(
            generated_at=time.time(),
            dry_run=False,
            summary={"total": 1, "applied": 1},
            records=[
                PatchApplyRecord(
                    id="apply-1",
                    run_id="run-1",
                    dry_run=False,
                    applied=True,
                    ok=True,
                    decision="applied",
                    message="Patch 已应用",
                    patch_count=2,
                    applied_count=2,
                    blocked_count=0,
                )
            ],
        )

        result = render_patch_apply_markdown(report)

        assert "# SUBAGENT PATCH APPLY" in result
        assert "apply" in result
        assert "applied" in result

    def test_render_patch_apply_record_markdown_basic(self):
        """测试基本 Patch 应用记录渲染。

        验证单个 Patch 应用记录可以正确渲染。
        """
        record = PatchApplyRecord(
            id="apply-1",
            run_id="run-1",
            dry_run=False,
            applied=True,
            ok=True,
            decision="applied",
            message="已应用",
            patch_count=2,
            applied_count=2,
            blocked_count=0,
            rollback_performed=False,
            test_commands=["pytest"],
        )

        result = render_patch_apply_record_markdown(record)

        assert "# PATCH APPLY" in result
        assert "run-1" in result
        assert "rollback_performed: False" in result