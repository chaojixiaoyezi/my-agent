"""子代理报告模块单元测试。

测试 agent_py_agent/agent/subagents/reports.py 中的报告数据类。
重点测试报告生成、摘要统计、dry_run 模式、JSON 输出等。
"""
from __future__ import annotations

import time
from dataclasses import asdict

import pytest

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
    DispatchRecord,
    DispatchReport,
    DispatchWatchRecord,
    DispatchWatchReport,
    DueCheckIssue,
    DueCheckReport,
    ParentPlannerParsedOutput,
    ParentPlannerRecord,
    ParentPlannerReport,
    PatchApplyRecord,
    PatchApplyReport,
    PatchReviewRecord,
    PatchReviewReport,
    SubAgentBoard,
    SubAgentBoardItem,
)


class TestAcceptanceReviewReports:
    """测试验收审核报告数据类。"""

    def test_acceptance_review_report_creation(self):
        """测试 AcceptanceReviewReport 创建。

        验证验收审核报告可以正确创建。
        """
        report = AcceptanceReviewReport(
            generated_at=time.time(),
            dry_run=False,
            summary={"total": 1, "passed": 1},
            records=[
                AcceptanceReviewRecord(
                    id="acc-1",
                    run_id="run-1",
                    dry_run=False,
                    applied=True,
                    ok=True,
                    decision="approved",
                    message="验收通过",
                    before_status="RUNNING",
                    after_status="COMPLETED",
                    before_verification_status="UNVERIFIED",
                    after_verification_status="VERIFIED",
                )
            ],
        )

        assert len(report.records) == 1
        assert report.records[0].decision == "approved"

    def test_acceptance_review_finding_creation(self):
        """测试 AcceptanceReviewFinding 创建。

        验证发现记录可以正确创建。
        """
        finding = AcceptanceReviewFinding(
            name="code_quality",
            ok=True,
            severity="P1",
            message="代码质量达标",
            evidence_path="/tmp/quality_report.txt",
        )

        assert finding.name == "code_quality"
        assert finding.ok is True

    def test_acceptance_review_record_with_findings(self):
        """测试带发现的验收记录。

        验证发现列表可以正确添加。
        """
        record = AcceptanceReviewRecord(
            id="acc-2",
            run_id="run-2",
            dry_run=True,
            applied=False,
            ok=True,
            decision="needs_review",
            message="需要进一步审核",
            before_status="RUNNING",
            after_status="RUNNING",
            before_verification_status="UNVERIFIED",
            after_verification_status="UNVERIFIED",
            worker_claims=["worker says tests passed"],
            evidence_facts=["tests=1"],
            parent_conclusions=["decision=needs_review"],
            verifier_checks=[
                AcceptanceReviewFinding(
                    name="verifier_evidence_packets_traceable",
                    ok=True,
                    severity="P1",
                    message="refs ok",
                )
            ],
            findings=[
                AcceptanceReviewFinding(
                    name="test_coverage",
                    ok=False,
                    severity="P2",
                    message="测试覆盖率不足",
                )
            ],
        )

        assert len(record.findings) == 1
        assert record.findings[0].ok is False
        assert record.worker_claims == ["worker says tests passed"]
        assert record.verifier_checks[0].name == "verifier_evidence_packets_traceable"

class TestPatchReviewReports:
    """测试 Patch 审核报告数据类。"""

    def test_patch_review_report_creation(self):
        """测试 PatchReviewReport 创建。

        验证 patch 审核报告可以正确创建。
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

        assert len(report.records) == 1
        assert report.records[0].decision == "approved"

    def test_patch_apply_report_creation(self):
        """测试 PatchApplyReport 创建。

        验证 patch 应用报告可以正确创建。
        """
        report = PatchApplyReport(
            generated_at=time.time(),
            dry_run=False,
            summary={"total": 1, "applied": 1},
            records=[
                PatchApplyRecord(
                    id="papply-1",
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

        assert report.dry_run is False
        assert len(report.records) == 1

class TestParentPlannerReports:
    """测试父代理计划报告数据类。"""

    def test_parent_planner_report_creation(self):
        """测试 ParentPlannerReport 创建。

        验证父代理计划报告可以正确创建。
        """
        report = ParentPlannerReport(
            generated_at=time.time(),
            dry_run=True,
            summary={"total": 1, "triggered": 1},
            records=[
                ParentPlannerRecord(
                    id="planner-1",
                    dry_run=True,
                    triggered=True,
                    ok=True,
                    decision="dispatch",
                    message="计划触发调度",
                )
            ],
        )

        assert len(report.records) == 1
        assert report.records[0].triggered is True

    def test_parent_planner_parsed_output_creation(self):
        """测试 ParentPlannerParsedOutput 创建。

        验证解析输出可以正确创建。
        """
        output = ParentPlannerParsedOutput(
            found=True,
            ok=True,
            decision="dispatch",
            summary="应该调度",
            should_dispatch=True,
            runner_instruction="执行任务",
            suggested_max_runners=2,
        )

        assert output.should_dispatch is True
        assert output.suggested_max_runners == 2

class TestReportEdgeCases:
    """测试报告边界场景。"""

    def test_empty_dispatch_report(self):
        """测试空调度报告。

        验证没有记录的报告可以正确创建。
        """
        report = DispatchReport(
            generated_at=time.time(),
            dry_run=True,
            summary={"total": 0},
            records=[],
        )

        assert len(report.records) == 0
        assert report.summary["total"] == 0

    def test_empty_due_check_report(self):
        """测试空 due-check 报告。

        验证没有问题时报告可以正确创建。
        """
        report = DueCheckReport(
            generated_at=time.time(),
            summary={"total": 0},
            issues=[],
        )

        assert len(report.issues) == 0

    def test_report_with_many_records(self):
        """测试大量记录的报告。

        验证大量记录可以正确添加。
        """
        records = [
            DispatchRecord(
                id=f"rec-{i}",
                step="dispatch",
                action="dispatch",
                run_id=f"run-{i}",
                dry_run=True,
                applied=False,
                ok=True,
                message=f"记录 {i}",
            )
            for i in range(100)
        ]

        report = DispatchReport(
            generated_at=time.time(),
            dry_run=True,
            summary={"total": 100},
            records=records,
        )

        assert len(report.records) == 100

    def test_report_summary_stats(self):
        """测试报告摘要统计。

        验证摘要统计可以正确反映报告内容。
        """
        report = DispatchReport(
            generated_at=time.time(),
            dry_run=True,
            summary={"total": 5, "dispatched": 3, "skipped": 2},
            records=[],
        )

        assert report.summary["total"] == 5
        assert report.summary["dispatched"] == 3
        assert report.summary["skipped"] == 2
