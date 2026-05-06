"""子代理渲染模块单元测试。

测试 agent_py_agent/agent/subagents/rendering.py 中的 Markdown 渲染函数。
包括看板、due-check、动作计划、验收报告、patch 审核等渲染功能。
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from agent_py_agent.agent.subagents.rendering import (
    render_acceptance_record_markdown,
    render_acceptance_review_markdown,
    render_action_apply_markdown,
    render_action_plan_markdown,
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
