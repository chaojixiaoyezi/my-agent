"""subagents/manager_actions.py 单元测试。

测试 action apply、执行记录、dry_run模式。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.manager_actions import SubAgentActionMixin
from agent_py_agent.agent.subagents.manager_base import SubAgentBaseMixin
from agent_py_agent.agent.subagents.models import SubAgentTask
from agent_py_agent.agent.subagents.reports import ActionPlanItem


class _ActionTestMixin(SubAgentBaseMixin, SubAgentActionMixin):
    def __init__(self, workspace: Path):
        SubAgentBaseMixin.__init__(self, workspace=workspace)

    def _index_task(self, task) -> None:
        pass


def _make_action_mixin(tmp_path: Path) -> _ActionTestMixin:
    return _ActionTestMixin(workspace=tmp_path)


def _make_action_task(
    mixin: _ActionTestMixin,
    run_id: str,
    *,
    status: str = "RUNNING",
    channel_status: str = "OK",
) -> SubAgentTask:
    return SubAgentTask(
        id=run_id,
        goal="测试",
        thought="思考",
        plan=["步骤1"],
        agent_name="test",
        created_at=1234567890.0,
        updated_at=1234567890.0,
        status=status,
        channel_status=channel_status,
        **mixin._build_work_order_paths(run_id),
    )


def _make_action_item(
    *,
    action_id: str,
    run_id: str,
    action: str,
    would_change_status_to: str = "BLOCKED",
) -> ActionPlanItem:
    return ActionPlanItem(
        id=action_id,
        run_id=run_id,
        severity="P1",
        priority=1,
        action=action,
        reason="测试",
        source_issue_kinds=[],
        would_change_status_to=would_change_status_to,
        created_at=1234567890.0,
    )


class TestActionApplyReportInit:
    """测试 ActionApplyReport 数据类。"""

    def test_action_apply_report_creation(self):
        """验证 ActionApplyReport 创建。"""
        from agent_py_agent.agent.subagents.reports import ActionApplyReport

        report = ActionApplyReport(
            generated_at=1234567890.0,
            dry_run=True,
            summary={"total": 1},
            records=[],
        )

        assert report.dry_run is True
        assert report.summary["total"] == 1


class TestActionApplyRecordInit:
    """测试 ActionApplyRecord 数据类。"""

    def test_action_apply_record_creation(self):
        """验证 ActionApplyRecord 创建。"""
        from agent_py_agent.agent.subagents.reports import ActionApplyRecord

        record = ActionApplyRecord(
            id="apply_123",
            action_id="action_456",
            run_id="run_789",
            action="reopen_for_evidence",
            dry_run=False,
            applied=True,
            ok=True,
            message="成功重开",
            created_at=1234567890.0,
        )

        assert record.action == "reopen_for_evidence"
        assert record.applied is True
        assert record.ok is True


class TestApplyActionItemDryRun:
    """测试 _apply_action_item() dry-run 模式。"""

    def test_dry_run_returns_record_without_apply(self, tmp_path: Path):
        mixin = _make_action_mixin(tmp_path)
        task = _make_action_task(mixin, "run_dry")
        mixin.save(task)
        action = _make_action_item(
            action_id="action_dry",
            run_id="run_dry",
            action="reopen_for_evidence",
        )

        record = mixin._apply_action_item(
            action,
            apply=False,
            take_over_by="",
            locked_files=[],
        )

        assert record.dry_run is True
        assert record.applied is False


class TestApplyActionItemNotFound:
    """测试 _apply_action_item() 任务不存在时。"""

    def test_returns_error_record(self, tmp_path: Path):
        """任务不存在时返回错误记录。"""
        mixin = _make_action_mixin(tmp_path)
        action = _make_action_item(
            action_id="action_err",
            run_id="nonexistent_run",
            action="reopen_for_evidence",
        )

        record = mixin._apply_action_item(
            action,
            apply=True,
            take_over_by="",
            locked_files=[],
        )

        assert record.ok is False
        assert record.applied is False


class TestNoProgressFuseActionApply:
    """测试 no-progress fuse 写回动作。"""

    def test_apply_stop_no_progress_records_blocker_without_status_change(self, tmp_path: Path):
        manager = SubAgentManager(tmp_path)
        task = manager.create_run(
            goal="反复恢复仍失败",
            thought="check",
            plan=["inspect"],
        )
        task.status = "FAILED"
        task.runner_attempts = 5
        manager.save(task)
        action = _make_action_item(
            action_id="action_fuse",
            run_id=task.id,
            action="stop_no_progress_and_escalate",
            would_change_status_to="",
        )

        record = manager._apply_action_item(action, apply=True)
        loaded = manager.load(task.id)

        assert record.ok is True
        assert record.applied is True
        assert record.action == "stop_no_progress_and_escalate"
        assert record.before_status == "FAILED"
        assert record.after_status == "FAILED"
        assert loaded.failure_type == "no_progress_fuse"
        assert any("no_progress_fuse" in blocker for blocker in loaded.blockers)


class TestTakeoverReadinessActionApply:
    def test_takeover_apply_evidence_paths_follow_readiness_read_order_without_artifact_body(self, tmp_path: Path):
        manager = SubAgentManager(tmp_path)
        task = manager.create_run(
            goal="apply takeover with readiness refs",
            thought="takeover evidence should point at recovery refs",
            plan=["write artifact", "block", "takeover"],
        )
        artifact_path = Path(task.task_dir) / "reports" / "blackbox.txt"
        artifact_path.write_text("DO_NOT_PULL_ARTIFACT_BODY_INTO_APPLY_RECORD\n", encoding="utf-8")
        task.status = "BLOCKED"
        task.failure_type = "tool_output_context_overflow"
        task.channel_status = "OK"
        task.artifact_refs = [str(artifact_path)]
        manager.save(task)
        loaded = manager.load(task.id)
        packet = json.loads(Path(loaded.takeover_readiness_json).read_text(encoding="utf-8"))
        action = ActionPlanItem(
            id="action_takeover",
            run_id=task.id,
            severity="P1",
            priority=1,
            action="takeover_or_reassign",
            reason="blocked",
            source_issue_kinds=["status_blocked"],
            would_change_status_to="TAKEN_OVER",
            rescue_context_refs=list(packet["recommended_read_order"]),
            created_at=1234567890.0,
        )

        record = manager._apply_action_item(
            action,
            apply=True,
            take_over_by="parent-agent",
            locked_files=[],
        )
        encoded = json.dumps(record.evidence_paths, ensure_ascii=False)
        expected_order = list(dict.fromkeys([loaded.takeover_readiness_json, *packet["recommended_read_order"]]))

        assert record.ok is True
        assert record.applied is True
        assert record.evidence_paths[0] == loaded.takeover_readiness_json
        assert loaded.failure_handoff_json in record.evidence_paths
        assert loaded.agent_run_checkpoint_json in record.evidence_paths
        assert loaded.agent_run_artifact_manifest_jsonl in record.evidence_paths
        assert record.evidence_paths[0 : len(expected_order)] == expected_order
        assert "DO_NOT_PULL_ARTIFACT_BODY_INTO_APPLY_RECORD" not in encoded


class TestCoordinatorTakeoverGuard:
    def test_generic_takeover_refuses_dead_coordinator_with_children(self, tmp_path: Path):
        """带孩子的死 coordinator 不能被普通 takeover 新建空接管 run。"""
        manager = SubAgentManager(tmp_path)
        coordinator = manager.create_run(
            goal="协调 leaf",
            thought="handoff guard",
            plan=["dispatch leaf"],
            role="coordinator",
        )
        manager.create_run(
            goal="leaf work",
            thought="child",
            plan=["write proof"],
            parent_id=coordinator.id,
            root_id=coordinator.root_id,
            depth=1,
        )
        coordinator = manager.load(coordinator.id)
        coordinator.status = "TIMEOUT"
        coordinator.failure_type = "runner_timeout"
        manager.save(coordinator)
        action = _make_action_item(
            action_id="action_wrong_takeover",
            run_id=coordinator.id,
            action="takeover_or_reassign",
        )

        record = manager._apply_action_item(action, apply=True, take_over_by="new-leader")
        loaded = manager.load(coordinator.id)

        assert record.ok is False
        assert record.applied is False
        assert "recover_coordinator_leadership" in record.message
        assert loaded.status == "TIMEOUT"
        assert loaded.takeover_by == ""
        assert len(manager.list_runs()) == 2


class TestRecordAfterTaskAction:
    """测试 _record_after_task_action() 方法。"""

    def test_creates_apply_record(self, tmp_path: Path):
        from agent_py_agent.agent.subagents.services.actions.params import (
            RecordAfterTaskActionParams,
        )

        mixin = _make_action_mixin(tmp_path)
        task = _make_action_task(mixin, "run_after")
        action = _make_action_item(
            action_id="action_after",
            run_id="run_after",
            action="run_acceptance",
            would_change_status_to="VERIFIED",
        )

        record = mixin._record_after_task_action(
            RecordAfterTaskActionParams(
                action,
                task,
                "RUNNING",
                "OK",
                "已标记需要验收",
            )
        )

        assert record.ok is True
        assert record.action == "run_acceptance"


class TestAppendActionApplyLog:
    """测试 _append_action_apply_log() 方法。"""

    def test_creates_jsonl_log(self, tmp_path: Path):
        """验证创建 JSONL 审计日志。"""
        from agent_py_agent.agent.subagents.manager_actions import SubAgentActionMixin
        from agent_py_agent.agent.subagents.manager_base import SubAgentBaseMixin
        from agent_py_agent.agent.subagents.reports import ActionApplyRecord

        class TestMixin(SubAgentBaseMixin, SubAgentActionMixin):
            def __init__(self, workspace: Path):
                SubAgentBaseMixin.__init__(self, workspace=workspace)

            def _index_action_apply(self, record):
                pass

        mixin = TestMixin(workspace=tmp_path)

        record = ActionApplyRecord(
            id="apply_log_1",
            action_id="action_1",
            run_id="run_log",
            action="run_acceptance",
            dry_run=False,
            applied=True,
            ok=True,
            message="测试日志",
            created_at=1234567890.0,
        )

        mixin._append_action_apply_log(record)

        jsonl_path = tmp_path / "subagent_action_apply_log.jsonl"
        assert jsonl_path.exists()


class TestAppendTaskWorkLog:
    """测试 _append_task_work_log() 方法。"""

    def test_appends_to_work_log(self, tmp_path: Path):
        """验证追加到工作日志。"""
        from agent_py_agent.agent.subagents.manager_actions import SubAgentActionMixin
        from agent_py_agent.agent.subagents.manager_base import SubAgentBaseMixin
        from agent_py_agent.agent.subagents.models import SubAgentTask

        class TestMixin(SubAgentBaseMixin, SubAgentActionMixin):
            def __init__(self, workspace: Path):
                SubAgentBaseMixin.__init__(self, workspace=workspace)

            def _log_local_record(self, **kwargs) -> None:
                pass

        mixin = TestMixin(workspace=tmp_path)

        task = SubAgentTask(
            id="run_worklog",
            goal="测试",
            thought="思考",
            plan=["步骤1"],
            agent_name="test",
            created_at=1234567890.0,
            updated_at=1234567890.0,
            **mixin._build_work_order_paths("run_worklog"),
        )
        task.work_log_file = str(tmp_path / "run_worklog" / "WORK_LOG.md")
        Path(task.work_log_file).parent.mkdir(parents=True, exist_ok=True)
        Path(task.work_log_file).write_text("# WORK_LOG\n\n", encoding="utf-8")

        mixin._append_task_work_log(task, "执行测试动作")

        content = Path(task.work_log_file).read_text(encoding="utf-8")
        assert "执行测试动作" in content


class TestActionFilterByRunId:
    """测试按 run_id 过滤 action。"""

    def test_filter_action_plan_items_by_run_id(self):
        """验证按 run_id 过滤。"""
        from agent_py_agent.agent.subagents.policies import _filter_action_plan_items
        from agent_py_agent.agent.subagents.reports import ActionPlanItem

        actions = [
            ActionPlanItem(
                id="a1",
                run_id="run_1",
                severity="P1",
                priority=1,
                action="run_acceptance",
                reason="原因1",
                source_issue_kinds=[],
                would_change_status_to="",
                created_at=1234567890.0,
            ),
            ActionPlanItem(
                id="a2",
                run_id="run_2",
                severity="P1",
                priority=1,
                action="reopen_for_evidence",
                reason="原因2",
                source_issue_kinds=[],
                would_change_status_to="",
                created_at=1234567890.0,
            ),
        ]

        filtered = _filter_action_plan_items(actions, run_id="run_1")
        assert len(filtered) == 1
        assert filtered[0].run_id == "run_1"


class TestActionFilterByActionType:
    """测试按 action 类型过滤。"""

    def test_filter_action_plan_items_by_action(self):
        """验证按 action 类型过滤。"""
        from agent_py_agent.agent.subagents.policies import _filter_action_plan_items
        from agent_py_agent.agent.subagents.reports import ActionPlanItem

        actions = [
            ActionPlanItem(
                id="a1",
                run_id="run_1",
                severity="P1",
                priority=1,
                action="run_acceptance",
                reason="原因1",
                source_issue_kinds=[],
                would_change_status_to="",
                created_at=1234567890.0,
            ),
            ActionPlanItem(
                id="a2",
                run_id="run_2",
                severity="P1",
                priority=1,
                action="reopen_for_evidence",
                reason="原因2",
                source_issue_kinds=[],
                would_change_status_to="",
                created_at=1234567890.0,
            ),
        ]

        filtered = _filter_action_plan_items(actions, action_filter="run_acceptance")
        assert len(filtered) == 1
        assert filtered[0].action == "run_acceptance"
