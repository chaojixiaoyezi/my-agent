"""子代理报告模块单元测试。

测试 agent_py_agent/agent/subagents/reports.py 中的报告数据类。
重点测试报告生成、摘要统计、dry_run 模式、JSON 输出等。
"""
from __future__ import annotations

import time
from dataclasses import asdict

import pytest

from agent_py_agent.agent.subagents.reports import (
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


class TestDispatchReports:
    """测试调度报告数据类。"""

    def test_dispatch_report_creation(self):
        """测试 DispatchReport 创建。

        验证调度报告可以正确创建。
        """
        report = DispatchReport(
            generated_at=time.time(),
            dry_run=True,
            summary={"total": 1, "dispatched": 1},
            records=[
                DispatchRecord(
                    id="dispatch-1",
                    step="dispatch",
                    action="dispatch",
                    run_id="run-1",
                    dry_run=True,
                    applied=False,
                    ok=True,
                    message="任务已调度",
                )
            ],
        )

        assert report.generated_at > 0
        assert report.dry_run is True
        assert len(report.records) == 1

    def test_dispatch_record_creation(self):
        """测试 DispatchRecord 创建。

        验证单条调度记录可以正确创建。
        """
        record = DispatchRecord(
            id="rec-1",
            step="dispatch",
            action="dispatch",
            run_id="run-1",
            dry_run=True,
            applied=False,
            ok=True,
            message="调度成功",
            before_status="PLANNING",
            after_status="RUNNING",
        )

        assert record.id == "rec-1"
        assert record.ok is True
        assert record.after_status == "RUNNING"

    def test_dispatch_report_to_dict(self):
        """测试调度报告转字典。

        验证报告可以正确序列化为字典。
        """
        report = DispatchReport(
            generated_at=time.time(),
            dry_run=True,
            summary={"total": 0},
            records=[],
        )

        d = asdict(report)
        assert isinstance(d, dict)
        assert "generated_at" in d
        assert "dry_run" in d


class TestDispatchWatchReports:
    """测试调度观察报告数据类。"""

    def test_dispatch_watch_report_creation(self):
        """测试 DispatchWatchReport 创建。

        验证调度观察报告可以正确创建。
        """
        report = DispatchWatchReport(
            generated_at=time.time(),
            dry_run=True,
            summary={"total": 1},
            records=[
                DispatchWatchRecord(
                    id="watch-1",
                    cycle=1,
                    dry_run=True,
                    ok=True,
                    message="一轮观察完成",
                    dispatch_record_count=5,
                )
            ],
        )

        assert len(report.records) == 1
        assert report.records[0].cycle == 1

    def test_dispatch_watch_record_creation(self):
        """测试 DispatchWatchRecord 创建。

        验证单条观察记录可以正确创建。
        """
        record = DispatchWatchRecord(
            id="wrec-1",
            cycle=1,
            dry_run=True,
            ok=True,
            message="观察完成",
            dispatch_record_count=3,
            dispatch_summary={"dispatched": 3},
        )

        assert record.cycle == 1
        assert record.dispatch_record_count == 3


class TestDueCheckReports:
    """测试 Due-Check 报告数据类。"""

    def test_due_check_report_creation(self):
        """测试 DueCheckReport 创建。

        验证 due-check 报告可以正确创建。
        """
        report = DueCheckReport(
            generated_at=time.time(),
            summary={"total": 1, "critical": 1},
            issues=[
                DueCheckIssue(
                    run_id="run-1",
                    severity="P1",
                    kind="timeout",
                    message="任务超时",
                    suggested_action="resume",
                )
            ],
        )

        assert len(report.issues) == 1
        assert report.issues[0].severity == "P1"

    def test_due_check_issue_creation(self):
        """测试 DueCheckIssue 创建。

        验证问题记录可以正确创建。
        """
        issue = DueCheckIssue(
            run_id="issue-1",
            severity="P2",
            kind="error",
            message="发生错误",
            suggested_action="retry",
            risk_flags=["high_priority"],
        )

        assert issue.risk_flags == ["high_priority"]
        assert issue.suggested_action == "retry"


class TestActionPlanReports:
    """测试动作计划报告数据类。"""

    def test_action_plan_report_creation(self):
        """测试 ActionPlanReport 创建。

        验证动作计划报告可以正确创建。
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
                    reason="任务应该继续",
                    source_issue_kinds=["timeout"],
                )
            ],
        )

        assert len(report.actions) == 1
        assert report.actions[0].action == "resume"

    def test_action_plan_item_with_commands(self):
        """测试带建议命令的动作项目。

        验证建议命令可以正确添加。
        """
        item = ActionPlanItem(
            id="item-1",
            run_id="run-1",
            severity="P2",
            priority=1,
            action="pause",
            reason="需要暂停",
            source_issue_kinds=["user_request"],
            suggested_commands=["agent pause --run-id=run-1"],
        )

        assert len(item.suggested_commands) == 1


class TestActionApplyReports:
    """测试动作应用报告数据类。"""

    def test_action_apply_report_creation(self):
        """测试 ActionApplyReport 创建。

        验证动作应用报告可以正确创建。
        """
        report = ActionApplyReport(
            generated_at=time.time(),
            dry_run=False,
            summary={"total": 1, "applied": 1},
            records=[
                ActionApplyRecord(
                    id="apply-1",
                    action_id="action-1",
                    run_id="run-1",
                    action="resume",
                    dry_run=False,
                    applied=True,
                    ok=True,
                    message="动作已应用",
                    before_status="PAUSED",
                    after_status="RUNNING",
                )
            ],
        )

        assert report.dry_run is False
        assert len(report.records) == 1

    def test_action_apply_record_creation(self):
        """测试 ActionApplyRecord 创建。

        验证单条应用记录可以正确创建。
        """
        record = ActionApplyRecord(
            id="rec-1",
            action_id="act-1",
            run_id="run-1",
            action="pause",
            dry_run=True,
            applied=False,
            ok=True,
            message="dry-run 模式",
            evidence_paths=["/tmp/evidence1.json"],
        )

        assert record.dry_run is True
        assert len(record.evidence_paths) == 1


class TestCapabilityRouteReports:
    """测试能力路由报告数据类。"""

    def test_capability_route_report_creation(self):
        """测试 CapabilityRouteReport 创建。

        验证能力路由报告可以正确创建。
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
                )
            ],
        )

        assert len(report.records) == 1
        assert report.records[0].status == "granted"

    def test_capability_route_record_creation(self):
        """测试 CapabilityRouteRecord 创建。

        验证单条路由记录可以正确创建。
        """
        record = CapabilityRouteRecord(
            id="crec-1",
            run_id="run-1",
            request_id="req-1",
            status="granted",
            dry_run=True,
            query="搜索",
            candidate_count=5,
            selected_cards=[{"kind": "skill", "name": "web_search"}],
        )

        assert len(record.selected_cards) == 1
