"""公开别名测试 - 验证公开方法正确委托给内部实现。

测试以下公开别名:
1. manager_acceptance_findings.py: acceptance_findings() 和 _acceptance_findings()
2. manager_indexing.py: select_runs, index_task, index_dispatch_record 等
3. manager_patch.py: resolve_patch_target()
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


class TestAcceptanceFindingsAliases:
    """测试 acceptance_findings 公开别名的委托行为。"""

    def test_acceptance_findings_public_method_works(self, tmp_path: Path):
        """验证 acceptance_findings() 公开方法能正常调用。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import (
            SubAgentAcceptanceFindingMixin,
        )

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()

        task = MagicMock()
        task.id = "test-task-001"
        task.status = "AWAITING_ACCEPTANCE"
        task.verification_status = "NEEDS_ACCEPTANCE"
        task.output_json = str(tmp_path / "output.json")
        task.runner_result_json = str(tmp_path / "runner.json")
        task.evidence = [MagicMock(ok=True)]
        task.task_dir = str(tmp_path / "task_dir")
        task.channel_status = "OK"
        task.channel_probe_file = str(tmp_path / "probe.json")
        task.acceptance_file = str(tmp_path / "acceptance.json")
        task.capability_requests = []
        task.capability_gaps = []
        task.acceptance_checks = []
        task.used_tools = []

        (tmp_path / "output.json").write_text("{}", encoding="utf-8")
        (tmp_path / "runner.json").write_text("{}", encoding="utf-8")
        (tmp_path / "probe.json").write_text("{}", encoding="utf-8")
        (tmp_path / "acceptance.json").write_text("[]", encoding="utf-8")

        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = manager.acceptance_findings(task, {}, {}, 1234567890.0)

        assert isinstance(findings, list)
        assert len(findings) > 0
        assert all(hasattr(f, "name") for f in findings)

    def test_acceptance_findings_private_alias_works(self, tmp_path: Path):
        """验证 _acceptance_findings() 向后兼容别名也能正常工作。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import (
            SubAgentAcceptanceFindingMixin,
        )

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()

        task = MagicMock()
        task.id = "test-task-001"
        task.status = "AWAITING_ACCEPTANCE"
        task.verification_status = "NEEDS_ACCEPTANCE"
        task.output_json = str(tmp_path / "output.json")
        task.runner_result_json = str(tmp_path / "runner.json")
        task.evidence = [MagicMock(ok=True)]
        task.task_dir = str(tmp_path / "task_dir")
        task.channel_status = "OK"
        task.channel_probe_file = str(tmp_path / "probe.json")
        task.acceptance_file = str(tmp_path / "acceptance.json")
        task.capability_requests = []
        task.capability_gaps = []
        task.acceptance_checks = []
        task.used_tools = []

        (tmp_path / "output.json").write_text("{}", encoding="utf-8")
        (tmp_path / "runner.json").write_text("{}", encoding="utf-8")
        (tmp_path / "probe.json").write_text("{}", encoding="utf-8")
        (tmp_path / "acceptance.json").write_text("[]", encoding="utf-8")

        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = manager._acceptance_findings(task, {}, {}, 1234567890.0)

        assert isinstance(findings, list)
        assert len(findings) > 0

    def test_acceptance_findings_public_and_private_return_same_result(self, tmp_path: Path):
        """验证 acceptance_findings() 和 _acceptance_findings() 返回相同结果。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import (
            SubAgentAcceptanceFindingMixin,
        )

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()

        task = MagicMock()
        task.id = "test-task-001"
        task.status = "AWAITING_ACCEPTANCE"
        task.verification_status = "NEEDS_ACCEPTANCE"
        task.output_json = str(tmp_path / "output.json")
        task.runner_result_json = str(tmp_path / "runner.json")
        task.evidence = [MagicMock(ok=True)]
        task.task_dir = str(tmp_path / "task_dir")
        task.channel_status = "OK"
        task.channel_probe_file = str(tmp_path / "probe.json")
        task.acceptance_file = str(tmp_path / "acceptance.json")
        task.capability_requests = []
        task.capability_gaps = []
        task.acceptance_checks = []
        task.used_tools = []

        (tmp_path / "output.json").write_text("{}", encoding="utf-8")
        (tmp_path / "runner.json").write_text("{}", encoding="utf-8")
        (tmp_path / "probe.json").write_text("{}", encoding="utf-8")
        (tmp_path / "acceptance.json").write_text("[]", encoding="utf-8")

        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        public_findings = manager.acceptance_findings(task, {}, {}, 1234567890.0)
        private_findings = manager._acceptance_findings(task, {}, {}, 1234567890.0)

        assert len(public_findings) == len(private_findings)
        for pub, priv in zip(public_findings, private_findings):
            assert pub.name == priv.name
            assert pub.ok == priv.ok
            assert pub.severity == priv.severity


class TestIndexingAliases:
    """测试 manager_indexing.py 公开别名的委托行为。"""

    def test_select_runs_delegates_to_private(self, tmp_path: Path):
        """验证 select_runs() 正确委托给 _select_runs。"""
        from agent_py_agent.agent.subagents.manager_indexing import SubAgentIndexingMixin
        from agent_py_agent.agent.subagents.models import DISPATCH_INELIGIBLE_STATUSES

        class MockManager(SubAgentIndexingMixin):
            def __init__(self):
                self.workspace = tmp_path
                self._local_store = None

        manager = MockManager()

        mock_task = MagicMock()
        mock_task.status = "PLANNING"

        manager.list_runs = MagicMock(return_value=[mock_task])

        result_public = manager.select_runs(None)
        result_private = manager._select_runs(None)

        assert result_public == result_private

    def test_select_runs_with_run_ids(self, tmp_path: Path):
        """验证 select_runs(run_ids) 传参正确。"""
        from agent_py_agent.agent.subagents.manager_indexing import SubAgentIndexingMixin

        class MockManager(SubAgentIndexingMixin):
            def __init__(self):
                self.workspace = tmp_path
                self._local_store = None

        manager = MockManager()

        mock_task = MagicMock()
        mock_task.status = "PLANNING"

        manager.load = MagicMock(return_value=mock_task)

        result = manager.select_runs(["run-001", "run-002"])

        assert isinstance(result, list)
        assert manager.load.call_count == 2

    def test_index_task_delegates_to_private(self, tmp_path: Path):
        """验证 index_task() 正确委托给 _index_task。"""
        from agent_py_agent.agent.subagents.manager_indexing import SubAgentIndexingMixin

        class MockManager(SubAgentIndexingMixin):
            def __init__(self):
                self.workspace = tmp_path
                self._local_store = MagicMock()

        manager = MockManager()

        task = MagicMock()
        task.id = "test-task"
        task.goal = "test goal"
        task.status = "PLANNING"
        task.verification_status = "NEEDS_VERIFY"
        task.channel_status = "OK"
        task.owner = "test-owner"
        task.supervisor = "test-supervisor"
        task.final_owner = "test-final-owner"
        task.parent_id = "parent-001"
        task.root_id = "root-001"
        task.thought = "test thought"
        task.plan = ["step1", "step2"]
        task.acceptance_checks = ["check1"]
        task.evidence = []
        task.capability_requests = []
        task.capability_gaps = []
        task.task_dir = str(tmp_path / "task_dir")
        task.output_json = str(tmp_path / "output.json")
        task.updated_at = 1234567890.0
        task.depth = 0

        manager._log_local_record = MagicMock()

        manager.index_task(task)
        manager._index_task(task)

        assert manager._log_local_record.call_count == 2

    def test_index_dispatch_record_delegates_to_private(self, tmp_path: Path):
        """验证 index_dispatch_record() 正确委托给 _index_dispatch_record。"""
        from agent_py_agent.agent.subagents.manager_indexing import SubAgentIndexingMixin

        class MockManager(SubAgentIndexingMixin):
            def __init__(self):
                self.workspace = tmp_path
                self._local_store = MagicMock()

        manager = MockManager()

        record = MagicMock()
        record.id = "dispatch-001"
        record.step = 1
        record.action = "execute"
        record.run_id = "run-001"

        manager._log_local_record = MagicMock()
        manager._index_dataclass_record = MagicMock()

        manager.index_dispatch_record(record)

        manager._index_dataclass_record.assert_called_once()

    def test_index_dispatch_watch_record_delegates_to_private(self, tmp_path: Path):
        """验证 index_dispatch_watch_record() 正确委托给 _index_dispatch_watch_record。"""
        from agent_py_agent.agent.subagents.manager_indexing import SubAgentIndexingMixin

        class MockManager(SubAgentIndexingMixin):
            def __init__(self):
                self.workspace = tmp_path
                self._local_store = MagicMock()

        manager = MockManager()

        record = MagicMock()
        record.id = "watch-001"
        record.cycle = 1

        manager._log_local_record = MagicMock()
        manager._index_dataclass_record = MagicMock()

        manager.index_dispatch_watch_record(record)

        manager._index_dataclass_record.assert_called_once()

    def test_index_parent_planner_record_delegates_to_private(self, tmp_path: Path):
        """验证 index_parent_planner_record() 正确委托给 _index_parent_planner_record。"""
        from agent_py_agent.agent.subagents.manager_indexing import SubAgentIndexingMixin

        class MockManager(SubAgentIndexingMixin):
            def __init__(self):
                self.workspace = tmp_path
                self._local_store = MagicMock()

        manager = MockManager()

        record = MagicMock()
        record.id = "planner-001"
        record.decision = "APPROVED"

        manager._log_local_record = MagicMock()
        manager._index_dataclass_record = MagicMock()

        manager.index_parent_planner_record(record)

        manager._index_dataclass_record.assert_called_once()

    def test_index_execution_context_delegates_to_private(self, tmp_path: Path):
        """验证 index_execution_context() 正确委托给 _index_execution_context。"""
        from agent_py_agent.agent.subagents.manager_indexing import SubAgentIndexingMixin

        class MockManager(SubAgentIndexingMixin):
            def __init__(self):
                self.workspace = tmp_path
                self._local_store = MagicMock()

        manager = MockManager()

        context = MagicMock()
        context.run_id = "run-001"

        manager._log_local_record = MagicMock()
        manager._index_dataclass_record = MagicMock()

        manager.index_execution_context(context)

        manager._index_dataclass_record.assert_called_once()

    def test_index_report_delegates_to_private(self, tmp_path: Path):
        """验证 index_report() 正确委托给 _index_report。"""
        from agent_py_agent.agent.subagents.manager_indexing import SubAgentIndexingMixin
        from agent_py_agent.agent.subagents.reports import DueCheckReport

        class MockManager(SubAgentIndexingMixin):
            def __init__(self):
                self.workspace = tmp_path
                self._local_store = MagicMock()

        manager = MockManager()

        report = DueCheckReport(
            generated_at=1234567890.0,
            summary={},
            issues=[],
        )

        manager._log_local_record = MagicMock()

        manager.index_report(
            source_type="test_type",
            source_id="test-id",
            title="Test Report",
            report=report,
            event_type="test_event",
        )

        manager._log_local_record.assert_called_once()

    def test_log_local_record_delegates_to_private(self, tmp_path: Path):
        """验证 log_local_record() 正确委托给 _log_local_record。"""
        from agent_py_agent.agent.subagents.manager_indexing import SubAgentIndexingMixin

        class MockManager(SubAgentIndexingMixin):
            def __init__(self):
                self.workspace = tmp_path
                self._local_store = MagicMock()

        manager = MockManager()

        manager._log_local_record = MagicMock()

        manager.log_local_record(
            source_type="test_type",
            source_id="test-id",
            title="Test",
            content="Test content",
            event_type="test_event",
        )

        manager._log_local_record.assert_called_once()


class TestPatchAliases:
    """测试 manager_patch.py 公开别名的委托行为。"""

    def test_resolve_patch_target_delegates_to_private(self, tmp_path: Path):
        """验证 resolve_patch_target() 正确委托给 _resolve_patch_target。"""
        from agent_py_agent.agent.subagents.manager_patch import SubAgentPatchMixin

        class MockManager(SubAgentPatchMixin):
            def __init__(self):
                self.workspace = tmp_path
                self.workspace_root = tmp_path

        manager = MockManager()

        raw_path = "subdir/file.txt"

        result_public = manager.resolve_patch_target(raw_path)
        result_private = manager._resolve_patch_target(raw_path)

        assert result_public == result_private
        assert result_public.is_absolute()

    def test_resolve_patch_target_absolute_path(self, tmp_path: Path):
        """验证 resolve_patch_target() 处理绝对路径。"""
        from agent_py_agent.agent.subagents.manager_patch import SubAgentPatchMixin

        class MockManager(SubAgentPatchMixin):
            def __init__(self):
                self.workspace = tmp_path
                self.workspace_root = tmp_path

        manager = MockManager()

        absolute_path = "/tmp/test/file.txt"
        result = manager.resolve_patch_target(absolute_path)

        assert result.is_absolute()

    def test_resolve_patch_target_relative_path(self, tmp_path: Path):
        """验证 resolve_patch_target() 处理相对路径（相对于 workspace_root）。"""
        from agent_py_agent.agent.subagents.manager_patch import SubAgentPatchMixin

        class MockManager(SubAgentPatchMixin):
            def __init__(self):
                self.workspace = tmp_path
                self.workspace_root = tmp_path

        manager = MockManager()

        relative_path = "subdir/file.txt"
        result = manager.resolve_patch_target(relative_path)

        assert result.is_absolute()
        assert str(result).endswith("subdir/file.txt")

    def test_resolve_patch_target_expanduser(self, tmp_path: Path):
        """验证 resolve_patch_target() 展开 ~ 用户目录。"""
        from agent_py_agent.agent.subagents.manager_patch import SubAgentPatchMixin

        class MockManager(SubAgentPatchMixin):
            def __init__(self):
                self.workspace = tmp_path
                self.workspace_root = tmp_path

        manager = MockManager()

        result = manager.resolve_patch_target("~/file.txt")

        assert "~" not in str(result)
        assert result.is_absolute()
