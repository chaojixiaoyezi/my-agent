"""Subagent board action-plan and recent-list tests."""

from __future__ import annotations

import time
from pathlib import Path


class TestPlanActions:
    """测试 plan_actions() 方法。"""

    def test_plan_actions_empty(self, tmp_path: Path):
        """无问题时返回空计划。"""
        from agent_py_agent.agent.subagents.manager import SubAgentManager
        from agent_py_agent.agent.subagents.services.board.service import SubAgentBoardService

        class TestMixin(SubAgentManager, SubAgentBoardService):
            def __init__(self, workspace: Path):
                SubAgentManager.__init__(self, workspace=workspace)

            def due_check(self, config=None, **_kwargs):
                from agent_py_agent.agent.subagents.reports import DueCheckReport

                return DueCheckReport(generated_at=time.time(), summary={"total": 0}, issues=[])

        mixin = TestMixin(workspace=tmp_path)
        report = mixin.board.plan_actions()

        assert report.summary["total"] == 0

    def test_plan_actions_can_scope_to_root_id(self, tmp_path: Path):
        """动作计划只使用指定 root_id 的 due-check 问题。"""
        from agent_py_agent.agent.subagents.manager import SubAgentManager
        from agent_py_agent.agent.subagents.models import SubAgentPlanActionsOptions, SubAgentTask
        from agent_py_agent.agent.subagents.services.board.service import SubAgentBoardService

        class TestMixin(SubAgentManager, SubAgentBoardService):
            def __init__(self, workspace: Path):
                SubAgentManager.__init__(self, workspace=workspace)
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

        report = mixin.board.plan_actions(params=SubAgentPlanActionsOptions(root_id="root-a"))

        assert {action.run_id for action in report.actions} == {"run_failed_a"}

    def test_plan_actions_can_scope_to_explicit_run_ids(self, tmp_path: Path):
        """显式 run_ids 调度时，action plan 只生成目标 run 的动作。"""
        from agent_py_agent.agent.subagents.manager import SubAgentManager
        from agent_py_agent.agent.subagents.models import SubAgentPlanActionsOptions, SubAgentTask
        from agent_py_agent.agent.subagents.services.board.service import SubAgentBoardService

        class TestMixin(SubAgentManager, SubAgentBoardService):
            def __init__(self, workspace: Path):
                SubAgentManager.__init__(self, workspace=workspace)
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

        report = mixin.board.plan_actions(params=SubAgentPlanActionsOptions(include_run_ids=["target_run"]))

        assert {action.run_id for action in report.actions} == {"target_run"}

    def test_plan_actions_uses_no_progress_fuse_action(self, tmp_path: Path):
        """no-progress fuse 应生成停止自动重试的动作计划。"""
        from agent_py_agent.agent.capability.config import CapabilityConfig
        from agent_py_agent.agent.subagents.manager import SubAgentManager
        from agent_py_agent.agent.subagents.models import SubAgentTask
        from agent_py_agent.agent.subagents.services.board.service import SubAgentBoardService

        class TestMixin(SubAgentManager, SubAgentBoardService):
            def __init__(self, workspace: Path):
                SubAgentManager.__init__(self, workspace=workspace)
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

        report = mixin.board.plan_actions(CapabilityConfig(subagent_no_progress_attempt_limit=4))

        assert [action.action for action in report.actions] == ["stop_no_progress_and_escalate"]
        assert report.actions[0].source_issue_kinds == ["no_progress_fuse"]


class TestWriteBoard:
    """测试 write_board() 方法。"""

    def test_writes_json_and_markdown(self, tmp_path: Path):
        """写出 JSON 和 Markdown 看板。"""
        from agent_py_agent.agent.subagents.manager import SubAgentManager
        from agent_py_agent.agent.subagents.services.board.service import SubAgentBoardService

        class TestMixin(SubAgentManager, SubAgentBoardService):
            def __init__(self, workspace: Path):
                SubAgentManager.__init__(self, workspace=workspace)

            def list_runs(self):
                return []

        mixin = TestMixin(workspace=tmp_path)
        mixin.board.write_board()

        assert (tmp_path / "subagent_board.json").exists()
        assert (tmp_path / "SUBAGENT_BOARD.md").exists()


class TestBoardHotList:
    """测试看板热榜。"""

    def test_hot_list_contains_risky_tasks(self, tmp_path: Path):
        """热榜包含风险任务。"""
        from agent_py_agent.agent.subagents.manager import SubAgentManager
        from agent_py_agent.agent.subagents.models import SubAgentTask, WorkOrderValidation
        from agent_py_agent.agent.subagents.services.board.service import SubAgentBoardService

        class TestMixin(SubAgentManager, SubAgentBoardService):
            def __init__(self, workspace: Path):
                SubAgentManager.__init__(self, workspace=workspace)
                self._tasks = []

            def list_runs(self):
                return self._tasks

            def validate_work_order(self, run_id):
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
            **mixin._build_work_order_paths("run_risky"),
        )
        mixin._tasks = [task]

        assert len(mixin.board.build_board().hot_list) > 0


class TestBoardRecent:
    """测试看板最近列表。"""

    def test_respects_recent_limit(self, tmp_path: Path):
        """限制最近条目数。"""
        from agent_py_agent.agent.subagents.manager import SubAgentManager
        from agent_py_agent.agent.subagents.models import SubAgentTask, WorkOrderValidation
        from agent_py_agent.agent.subagents.services.board.service import SubAgentBoardService

        class TestMixin(SubAgentManager, SubAgentBoardService):
            def __init__(self, workspace: Path):
                SubAgentManager.__init__(self, workspace=workspace)
                self._tasks = []

            def list_runs(self):
                return self._tasks

            def validate_work_order(self, run_id):
                return WorkOrderValidation(run_id=run_id, ok=True, missing=[], warnings=[])

        mixin = TestMixin(workspace=tmp_path)
        for index in range(5):
            mixin._tasks.append(
                SubAgentTask(
                    id=f"run_{index}",
                    goal=f"任务{index}",
                    thought="思考",
                    plan=["步骤1"],
                    agent_name="test",
                    status="RUNNING",
                    verification_status="PENDING",
                    channel_status="OK",
                    created_at=1234567890.0 + index,
                    updated_at=1234567890.0 + index,
                    **mixin._build_work_order_paths(f"run_{index}"),
                )
            )

        board = mixin.board.build_board(recent_limit=3)

        assert len(board.recent) == 3
