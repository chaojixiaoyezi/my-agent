"""subagents/manager_board.py 单元测试。

测试看板数据生成、状态汇总、HTML输出。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _make_board_mixin(tmp_path: Path):
    from agent_py_agent.agent.subagents.manager_base import SubAgentBaseMixin
    from agent_py_agent.agent.subagents.manager_board import SubAgentBoardMixin

    class TestMixin(SubAgentBaseMixin, SubAgentBoardMixin):
        def __init__(self, workspace: Path):
            SubAgentBaseMixin.__init__(self, workspace=workspace)

        def validate_work_order(self, run_id):
            from agent_py_agent.agent.subagents.models import WorkOrderValidation
            return WorkOrderValidation(run_id=run_id, ok=True, missing=[], warnings=[])

    return TestMixin(workspace=tmp_path)


def _make_open_capability_request(run_id: str):
    from agent_py_agent.agent.subagents.models import CapabilityRequest

    return CapabilityRequest(
        id="req_1",
        from_run_id=run_id,
        needed_capability="test_cap",
        problem="问题",
        expected_output="输出",
        status="OPEN",
        created_at=1234567890.0,
    )


def _make_board_task(mixin, run_id: str, *, capability_requests=None, channel_status="OK"):
    from agent_py_agent.agent.subagents.models import SubAgentTask

    return SubAgentTask(
        id=run_id,
        goal="测试",
        thought="思考",
        plan=["步骤1"],
        agent_name="test",
        status="RUNNING",
        verification_status="PENDING",
        channel_status=channel_status,
        created_at=1234567890.0,
        updated_at=1234567890.0,
        capability_requests=capability_requests or [],
        capability_gaps=[],
        evidence=[],
        child_ids=[],
        locked_files=[],
        takeover_by="",
        **mixin._build_work_order_paths(run_id),
    )


class TestBuildBoard:
    """测试 build_board() 方法。"""

    def test_build_board_returns_subagent_board(self, tmp_path: Path):
        """验证返回 SubAgentBoard。"""
        from agent_py_agent.agent.subagents.manager_base import SubAgentBaseMixin
        from agent_py_agent.agent.subagents.manager_board import SubAgentBoardMixin
        from agent_py_agent.agent.subagents.models import SubAgentTask

        class TestMixin(SubAgentBaseMixin, SubAgentBoardMixin):
            def __init__(self, workspace: Path):
                SubAgentBaseMixin.__init__(self, workspace=workspace)

            def list_runs(self):
                return []

        mixin = TestMixin(workspace=tmp_path)
        board = mixin.build_board()

        assert board.summary["total"] == 0
        assert board.items == []

    def test_board_counts_tasks_by_status(self, tmp_path: Path):
        """验证按状态统计。"""
        from agent_py_agent.agent.subagents.manager_base import SubAgentBaseMixin
        from agent_py_agent.agent.subagents.manager_board import SubAgentBoardMixin
        from agent_py_agent.agent.subagents.models import SubAgentTask

        class TestMixin(SubAgentBaseMixin, SubAgentBoardMixin):
            def __init__(self, workspace: Path):
                SubAgentBaseMixin.__init__(self, workspace=workspace)
                self._tasks = []

            def list_runs(self):
                return self._tasks

            def validate_work_order(self, run_id):
                from agent_py_agent.agent.subagents.models import WorkOrderValidation
                return WorkOrderValidation(run_id=run_id, ok=True, missing=[], warnings=[])

        mixin = TestMixin(workspace=tmp_path)

        task1 = SubAgentTask(
            id="run_1",
            goal="测试1",
            thought="思考",
            plan=["步骤1"],
            agent_name="test",
            status="RUNNING",
            verification_status="PENDING",
            channel_status="OK",
            created_at=1234567890.0,
            updated_at=1234567890.0,
            capability_requests=[],
            capability_gaps=[],
            evidence=[],
            child_ids=[],
            locked_files=[],
            takeover_by="",
            **mixin._build_work_order_paths("run_1"),
        )
        mixin._tasks = [task1]

        board = mixin.build_board()

        assert board.summary["total"] == 1
        assert board.summary.get("RUNNING", 0) == 1


class TestToBoardItem:
    """测试 _to_board_item() 方法。"""

    def test_converts_task_to_board_item(self, tmp_path: Path):
        """验证任务转换为看板行。"""
        from agent_py_agent.agent.subagents.manager_base import SubAgentBaseMixin
        from agent_py_agent.agent.subagents.manager_board import SubAgentBoardMixin
        from agent_py_agent.agent.subagents.models import SubAgentTask

        class TestMixin(SubAgentBaseMixin, SubAgentBoardMixin):
            def __init__(self, workspace: Path):
                SubAgentBaseMixin.__init__(self, workspace=workspace)

            def _risk_flags(self, task, **kwargs):
                return []

        mixin = TestMixin(workspace=tmp_path)

        task = SubAgentTask(
            id="run_board",
            goal="测试看板",
            thought="思考",
            plan=["步骤1"],
            agent_name="test",
            status="RUNNING",
            verification_status="PENDING",
            channel_status="OK",
            owner="user1",
            supervisor="parent",
            final_owner="",
            created_at=1234567890.0,
            updated_at=1234567890.0,
            heartbeat_at=1234567890.0,
            capability_requests=[],
            capability_gaps=[],
            evidence=[MagicMock(), MagicMock()],  # 2 evidence items
            child_ids=["child1", "child2"],
            locked_files=["file1.txt"],
            takeover_by="",
            **mixin._build_work_order_paths("run_board"),
        )

        item = mixin._to_board_item(task)

        assert item.id == "run_board"
        assert item.status == "RUNNING"
        assert item.evidence_count == 2
        assert item.child_count == 2
        assert item.locked_file_count == 1


class TestRiskFlags:
    """测试 _risk_flags() 方法。"""

    def test_blocked_status_flag(self, tmp_path: Path):
        """BLOCKED 状态添加风险标记。"""
        from agent_py_agent.agent.subagents.manager_base import SubAgentBaseMixin
        from agent_py_agent.agent.subagents.manager_board import SubAgentBoardMixin
        from agent_py_agent.agent.subagents.models import SubAgentTask

        class TestMixin(SubAgentBaseMixin, SubAgentBoardMixin):
            def __init__(self, workspace: Path):
                SubAgentBaseMixin.__init__(self, workspace=workspace)

            def validate_work_order(self, run_id):
                from agent_py_agent.agent.subagents.models import WorkOrderValidation
                return WorkOrderValidation(run_id=run_id, ok=True, missing=[], warnings=[])

        mixin = TestMixin(workspace=tmp_path)

        task = SubAgentTask(
            id="run_blocked",
            goal="测试",
            thought="思考",
            plan=["步骤1"],
            agent_name="test",
            status="BLOCKED",
            verification_status="PENDING",
            channel_status="OK",
            created_at=1234567890.0,
            updated_at=1234567890.0,
            capability_requests=[],
            capability_gaps=[],
            evidence=[],
            child_ids=[],
            locked_files=[],
            takeover_by="",
            **mixin._build_work_order_paths("run_blocked"),
        )

        flags = mixin._risk_flags(task, open_request_count=0, open_gap_count=0)

        assert "blocked" in flags

    def test_open_capability_request_flag(self, tmp_path: Path):
        mixin = _make_board_mixin(tmp_path)
        request = _make_open_capability_request("run_req")
        task = _make_board_task(mixin, "run_req", capability_requests=[request])

        flags = mixin._risk_flags(task, open_request_count=1, open_gap_count=0)

        assert "open_capability_request" in flags

    def test_channel_broken_flag(self, tmp_path: Path):
        """通道损坏时添加标记。"""
        from agent_py_agent.agent.subagents.manager_base import SubAgentBaseMixin
        from agent_py_agent.agent.subagents.manager_board import SubAgentBoardMixin
        from agent_py_agent.agent.subagents.models import SubAgentTask

        class TestMixin(SubAgentBaseMixin, SubAgentBoardMixin):
            def __init__(self, workspace: Path):
                SubAgentBaseMixin.__init__(self, workspace=workspace)

            def validate_work_order(self, run_id):
                from agent_py_agent.agent.subagents.models import WorkOrderValidation
                return WorkOrderValidation(run_id=run_id, ok=True, missing=[], warnings=[])

        mixin = TestMixin(workspace=tmp_path)

        task = SubAgentTask(
            id="run_broken",
            goal="测试",
            thought="思考",
            plan=["步骤1"],
            agent_name="test",
            status="RUNNING",
            verification_status="PENDING",
            channel_status="BROKEN",
            created_at=1234567890.0,
            updated_at=1234567890.0,
            capability_requests=[],
            capability_gaps=[],
            evidence=[],
            child_ids=[],
            locked_files=[],
            takeover_by="",
            **mixin._build_work_order_paths("run_broken"),
        )

        flags = mixin._risk_flags(task, open_request_count=0, open_gap_count=0)

        assert "channel_broken" in flags


class TestDueCheck:
    """测试 due_check() 方法。"""

    def test_due_check_empty(self, tmp_path: Path):
        """无任务时返回空报告。"""
        from agent_py_agent.agent.capability_config import CapabilityConfig
        from agent_py_agent.agent.subagents.manager_base import SubAgentBaseMixin
        from agent_py_agent.agent.subagents.manager_board import SubAgentBoardMixin

        class TestMixin(SubAgentBaseMixin, SubAgentBoardMixin):
            def __init__(self, workspace: Path):
                SubAgentBaseMixin.__init__(self, workspace=workspace)

            def list_runs(self):
                return []

        mixin = TestMixin(workspace=tmp_path)
        report = mixin.due_check()

        assert report.summary["total"] == 0
        assert report.issues == []

    def test_due_check_detects_failed_task(self, tmp_path: Path):
        """检测失败任务。"""
        from agent_py_agent.agent.subagents.manager_base import SubAgentBaseMixin
        from agent_py_agent.agent.subagents.manager_board import SubAgentBoardMixin
        from agent_py_agent.agent.subagents.models import SubAgentTask

        class TestMixin(SubAgentBaseMixin, SubAgentBoardMixin):
            def __init__(self, workspace: Path):
                SubAgentBaseMixin.__init__(self, workspace=workspace)
                self._tasks = []

            def list_runs(self):
                return self._tasks

            def validate_work_order(self, run_id):
                from agent_py_agent.agent.subagents.models import WorkOrderValidation
                return WorkOrderValidation(run_id=run_id, ok=True, missing=[], warnings=[])

        mixin = TestMixin(workspace=tmp_path)

        task = SubAgentTask(
            id="run_failed",
            goal="失败任务",
            thought="思考",
            plan=["步骤1"],
            agent_name="test",
            status="FAILED",
            verification_status="PENDING",
            channel_status="OK",
            created_at=1234567890.0,
            updated_at=1234567890.0,
            capability_requests=[],
            capability_gaps=[],
            evidence=[],
            child_ids=[],
            locked_files=[],
            takeover_by="",
            **mixin._build_work_order_paths("run_failed"),
        )
        mixin._tasks = [task]

        report = mixin.due_check()

        assert report.summary["total"] > 0

    def test_due_check_can_scope_to_root_id(self, tmp_path: Path):
        """只巡检指定 root_id 的任务树。"""
        from agent_py_agent.agent.subagents.manager_base import SubAgentBaseMixin
        from agent_py_agent.agent.subagents.manager_board import SubAgentBoardMixin
        from agent_py_agent.agent.subagents.models import SubAgentDueCheckOptions, SubAgentTask

        class TestMixin(SubAgentBaseMixin, SubAgentBoardMixin):
            def __init__(self, workspace: Path):
                SubAgentBaseMixin.__init__(self, workspace=workspace)
                self._tasks = []

            def list_runs(self):
                return self._tasks

            def validate_work_order(self, run_id):
                from agent_py_agent.agent.subagents.models import WorkOrderValidation
                return WorkOrderValidation(run_id=run_id, ok=True, missing=[], warnings=[])

        mixin = TestMixin(workspace=tmp_path)
        first = SubAgentTask(
            id="run_failed_a",
            root_id="root-a",
            goal="失败任务 A",
            thought="check",
            plan=["inspect"],
            status="FAILED",
            verification_status="FAILED",
            channel_status="OK",
            **mixin._build_work_order_paths("run_failed_a"),
        )
        second = SubAgentTask(
            id="run_failed_b",
            root_id="root-b",
            goal="失败任务 B",
            thought="check",
            plan=["inspect"],
            status="FAILED",
            verification_status="FAILED",
            channel_status="OK",
            **mixin._build_work_order_paths("run_failed_b"),
        )
        mixin._tasks = [first, second]

        report = mixin.due_check(params=SubAgentDueCheckOptions(root_id="root-a"))

        assert {issue.run_id for issue in report.issues} == {"run_failed_a"}

    def test_due_check_can_scope_to_explicit_run_ids(self, tmp_path: Path):
        """显式 run_ids 调度时，due-check 不应把同目录其它失败 run 混进来。"""
        from agent_py_agent.agent.subagents.manager_base import SubAgentBaseMixin
        from agent_py_agent.agent.subagents.manager_board import SubAgentBoardMixin
        from agent_py_agent.agent.subagents.models import SubAgentDueCheckOptions, SubAgentTask

        class TestMixin(SubAgentBaseMixin, SubAgentBoardMixin):
            def __init__(self, workspace: Path):
                SubAgentBaseMixin.__init__(self, workspace=workspace)
                self._tasks = []

            def list_runs(self):
                return self._tasks

            def validate_work_order(self, run_id):
                from agent_py_agent.agent.subagents.models import WorkOrderValidation
                return WorkOrderValidation(run_id=run_id, ok=True, missing=[], warnings=[])

        mixin = TestMixin(workspace=tmp_path)
        first = SubAgentTask(
            id="target_run",
            goal="目标任务",
            thought="check",
            plan=["inspect"],
            status="FAILED",
            verification_status="FAILED",
            channel_status="OK",
            **mixin._build_work_order_paths("target_run"),
        )
        second = SubAgentTask(
            id="unrelated_run",
            goal="无关失败任务",
            thought="check",
            plan=["inspect"],
            status="FAILED",
            verification_status="FAILED",
            channel_status="OK",
            **mixin._build_work_order_paths("unrelated_run"),
        )
        mixin._tasks = [first, second]

        report = mixin.due_check(params=SubAgentDueCheckOptions(include_run_ids=["target_run"]))

        assert {issue.run_id for issue in report.issues} == {"target_run"}

    def test_due_check_reports_no_progress_fuse_before_generic_failure(self, tmp_path: Path):
        """连续恢复无进展时，due-check 应明确报告熔断而不是普通失败。"""
        from agent_py_agent.agent.capability_config import CapabilityConfig
        from agent_py_agent.agent.subagents.manager_base import SubAgentBaseMixin
        from agent_py_agent.agent.subagents.manager_board import SubAgentBoardMixin
        from agent_py_agent.agent.subagents.models import SubAgentTask

        class TestMixin(SubAgentBaseMixin, SubAgentBoardMixin):
            def __init__(self, workspace: Path):
                SubAgentBaseMixin.__init__(self, workspace=workspace)
                self._tasks = []

            def list_runs(self):
                return self._tasks

            def validate_work_order(self, run_id):
                from agent_py_agent.agent.subagents.models import WorkOrderValidation
                return WorkOrderValidation(run_id=run_id, ok=True, missing=[], warnings=[])

        mixin = TestMixin(workspace=tmp_path)
        task = SubAgentTask(
            id="stuck_run",
            goal="反复恢复仍失败",
            thought="check",
            plan=["inspect"],
            status="FAILED",
            verification_status="FAILED",
            channel_status="OK",
            runner_attempts=4,
            **mixin._build_work_order_paths("stuck_run"),
        )
        task.agent_run_checkpoint_json = str(tmp_path / "stuck_run" / "reports" / "checkpoint.json")
        Path(task.agent_run_checkpoint_json).parent.mkdir(parents=True, exist_ok=True)
        Path(task.agent_run_checkpoint_json).write_text("{}", encoding="utf-8")
        mixin._tasks = [task]

        report = mixin.due_check(CapabilityConfig(subagent_no_progress_attempt_limit=4))

        assert [issue.kind for issue in report.issues] == ["no_progress_fuse"]
        assert report.issues[0].suggested_action == "stop_no_progress_and_escalate"
        assert task.agent_run_checkpoint_json in report.issues[0].related_refs

class TestPlanActions:
    """测试 plan_actions() 方法。"""

    def test_plan_actions_empty(self, tmp_path: Path):
        """无问题时返回空计划。"""
        from agent_py_agent.agent.subagents.manager_base import SubAgentBaseMixin
        from agent_py_agent.agent.subagents.manager_board import SubAgentBoardMixin

        class TestMixin(SubAgentBaseMixin, SubAgentBoardMixin):
            def __init__(self, workspace: Path):
                SubAgentBaseMixin.__init__(self, workspace=workspace)

            def due_check(self, config=None):
                from agent_py_agent.agent.subagents.reports import DueCheckReport
                return DueCheckReport(generated_at=time.time(), summary={"total": 0}, issues=[])

        mixin = TestMixin(workspace=tmp_path)
        report = mixin.plan_actions()

        assert report.summary["total"] == 0

    def test_plan_actions_can_scope_to_root_id(self, tmp_path: Path):
        """动作计划只使用指定 root_id 的 due-check 问题。"""
        from agent_py_agent.agent.subagents.manager_base import SubAgentBaseMixin
        from agent_py_agent.agent.subagents.manager_board import SubAgentBoardMixin
        from agent_py_agent.agent.subagents.models import SubAgentPlanActionsOptions, SubAgentTask

        class TestMixin(SubAgentBaseMixin, SubAgentBoardMixin):
            def __init__(self, workspace: Path):
                SubAgentBaseMixin.__init__(self, workspace=workspace)
                self._tasks = []

            def list_runs(self):
                return self._tasks

            def validate_work_order(self, run_id):
                from agent_py_agent.agent.subagents.models import WorkOrderValidation
                return WorkOrderValidation(run_id=run_id, ok=True, missing=[], warnings=[])

        mixin = TestMixin(workspace=tmp_path)
        first = SubAgentTask(
            id="run_failed_a",
            root_id="root-a",
            goal="失败任务 A",
            thought="check",
            plan=["inspect"],
            status="FAILED",
            verification_status="FAILED",
            channel_status="OK",
            **mixin._build_work_order_paths("run_failed_a"),
        )
        second = SubAgentTask(
            id="run_failed_b",
            root_id="root-b",
            goal="失败任务 B",
            thought="check",
            plan=["inspect"],
            status="FAILED",
            verification_status="FAILED",
            channel_status="OK",
            **mixin._build_work_order_paths("run_failed_b"),
        )
        mixin._tasks = [first, second]

        report = mixin.plan_actions(params=SubAgentPlanActionsOptions(root_id="root-a"))

        assert {action.run_id for action in report.actions} == {"run_failed_a"}

    def test_plan_actions_can_scope_to_explicit_run_ids(self, tmp_path: Path):
        """显式 run_ids 调度时，action plan 只生成目标 run 的动作。"""
        from agent_py_agent.agent.subagents.manager_base import SubAgentBaseMixin
        from agent_py_agent.agent.subagents.manager_board import SubAgentBoardMixin
        from agent_py_agent.agent.subagents.models import SubAgentPlanActionsOptions, SubAgentTask

        class TestMixin(SubAgentBaseMixin, SubAgentBoardMixin):
            def __init__(self, workspace: Path):
                SubAgentBaseMixin.__init__(self, workspace=workspace)
                self._tasks = []

            def list_runs(self):
                return self._tasks

            def validate_work_order(self, run_id):
                from agent_py_agent.agent.subagents.models import WorkOrderValidation
                return WorkOrderValidation(run_id=run_id, ok=True, missing=[], warnings=[])

        mixin = TestMixin(workspace=tmp_path)
        first = SubAgentTask(
            id="target_run",
            goal="目标任务",
            thought="check",
            plan=["inspect"],
            status="FAILED",
            verification_status="FAILED",
            channel_status="OK",
            **mixin._build_work_order_paths("target_run"),
        )
        second = SubAgentTask(
            id="unrelated_run",
            goal="无关失败任务",
            thought="check",
            plan=["inspect"],
            status="FAILED",
            verification_status="FAILED",
            channel_status="OK",
            **mixin._build_work_order_paths("unrelated_run"),
        )
        mixin._tasks = [first, second]

        report = mixin.plan_actions(params=SubAgentPlanActionsOptions(include_run_ids=["target_run"]))

        assert {action.run_id for action in report.actions} == {"target_run"}

    def test_plan_actions_uses_no_progress_fuse_action(self, tmp_path: Path):
        """no-progress fuse 应生成停止自动重试的动作计划。"""
        from agent_py_agent.agent.capability_config import CapabilityConfig
        from agent_py_agent.agent.subagents.manager_base import SubAgentBaseMixin
        from agent_py_agent.agent.subagents.manager_board import SubAgentBoardMixin
        from agent_py_agent.agent.subagents.models import SubAgentTask

        class TestMixin(SubAgentBaseMixin, SubAgentBoardMixin):
            def __init__(self, workspace: Path):
                SubAgentBaseMixin.__init__(self, workspace=workspace)
                self._tasks = []

            def list_runs(self):
                return self._tasks

            def validate_work_order(self, run_id):
                from agent_py_agent.agent.subagents.models import WorkOrderValidation
                return WorkOrderValidation(run_id=run_id, ok=True, missing=[], warnings=[])

        mixin = TestMixin(workspace=tmp_path)
        task = SubAgentTask(
            id="stuck_run",
            goal="反复恢复仍失败",
            thought="check",
            plan=["inspect"],
            status="FAILED",
            verification_status="FAILED",
            channel_status="OK",
            runner_attempts=5,
            **mixin._build_work_order_paths("stuck_run"),
        )
        mixin._tasks = [task]

        report = mixin.plan_actions(CapabilityConfig(subagent_no_progress_attempt_limit=4))

        assert [action.action for action in report.actions] == ["stop_no_progress_and_escalate"]
        assert report.actions[0].source_issue_kinds == ["no_progress_fuse"]

class TestWriteBoard:
    """测试 write_board() 方法。"""

    def test_writes_json_and_markdown(self, tmp_path: Path):
        """写出 JSON 和 Markdown 看板。"""
        from agent_py_agent.agent.subagents.manager_base import SubAgentBaseMixin
        from agent_py_agent.agent.subagents.manager_board import SubAgentBoardMixin

        class TestMixin(SubAgentBaseMixin, SubAgentBoardMixin):
            def __init__(self, workspace: Path):
                SubAgentBaseMixin.__init__(self, workspace=workspace)

            def list_runs(self):
                return []

        mixin = TestMixin(workspace=tmp_path)
        board = mixin.write_board()

        json_path = tmp_path / "subagent_board.json"
        md_path = tmp_path / "SUBAGENT_BOARD.md"

        assert json_path.exists()
        assert md_path.exists()


class TestBoardHotList:
    """测试看板热榜。"""

    def test_hot_list_contains_risky_tasks(self, tmp_path: Path):
        """热榜包含风险任务。"""
        from agent_py_agent.agent.subagents.manager_base import SubAgentBaseMixin
        from agent_py_agent.agent.subagents.manager_board import SubAgentBoardMixin
        from agent_py_agent.agent.subagents.models import SubAgentTask

        class TestMixin(SubAgentBaseMixin, SubAgentBoardMixin):
            def __init__(self, workspace: Path):
                SubAgentBaseMixin.__init__(self, workspace=workspace)
                self._tasks = []

            def list_runs(self):
                return self._tasks

            def validate_work_order(self, run_id):
                from agent_py_agent.agent.subagents.models import WorkOrderValidation
                return WorkOrderValidation(run_id=run_id, ok=True, missing=[], warnings=[])

        mixin = TestMixin(workspace=tmp_path)

        task = SubAgentTask(
            id="run_risky",
            goal="风险任务",
            thought="思考",
            plan=["步骤1"],
            agent_name="test",
            status="FAILED",
            verification_status="PENDING",
            channel_status="OK",
            created_at=1234567890.0,
            updated_at=1234567890.0,
            capability_requests=[],
            capability_gaps=[],
            evidence=[],
            child_ids=[],
            locked_files=[],
            takeover_by="",
            **mixin._build_work_order_paths("run_risky"),
        )
        mixin._tasks = [task]

        board = mixin.build_board()

        assert len(board.hot_list) > 0


class TestBoardRecent:
    """测试看板最近列表。"""

    def test_respects_recent_limit(self, tmp_path: Path):
        """限制最近条目数。"""
        from agent_py_agent.agent.subagents.manager_base import SubAgentBaseMixin
        from agent_py_agent.agent.subagents.manager_board import SubAgentBoardMixin
        from agent_py_agent.agent.subagents.models import SubAgentTask

        class TestMixin(SubAgentBaseMixin, SubAgentBoardMixin):
            def __init__(self, workspace: Path):
                SubAgentBaseMixin.__init__(self, workspace=workspace)
                self._tasks = []

            def list_runs(self):
                return self._tasks

            def validate_work_order(self, run_id):
                from agent_py_agent.agent.subagents.models import WorkOrderValidation
                return WorkOrderValidation(run_id=run_id, ok=True, missing=[], warnings=[])

        mixin = TestMixin(workspace=tmp_path)

        for i in range(5):
            task = SubAgentTask(
                id=f"run_{i}",
                goal=f"任务{i}",
                thought="思考",
                plan=["步骤1"],
                agent_name="test",
                status="RUNNING",
                verification_status="PENDING",
                channel_status="OK",
                created_at=1234567890.0 + i,
                updated_at=1234567890.0 + i,
                capability_requests=[],
                capability_gaps=[],
                evidence=[],
                child_ids=[],
                locked_files=[],
                takeover_by="",
                **mixin._build_work_order_paths(f"run_{i}"),
            )
            mixin._tasks.append(task)

        board = mixin.build_board(recent_limit=3)

        assert len(board.recent) == 3
