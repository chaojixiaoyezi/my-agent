"""manager_patch 模块测试。

测试补丁审核、应用、回滚和版本控制功能。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from agent_py_agent.agent.subagents.services.patch_apply.test_commands import PatchApplyTestCommands


class TestBuildUnifiedDiff:
    """测试统一 diff 构建函数。"""

    def test_build_unified_diff_basic(self):
        """测试基本 diff 生成。"""
        from agent_py_agent.agent.subagents.manager_patch import SubAgentPatchMixin

        before = "line1\nline2\nline3\n"
        after = "line1\nmodified\nline3\n"

        result = SubAgentPatchMixin._build_unified_diff("test.txt", before, after)

        assert "---" in result
        assert "+++" in result
        assert "-line2" in result
        assert "+modified" in result

    def test_build_unified_diff_empty_before(self):
        """测试空文件到有内容的 diff。"""
        from agent_py_agent.agent.subagents.manager_patch import SubAgentPatchMixin

        before = ""
        after = "new content\n"

        result = SubAgentPatchMixin._build_unified_diff("new.txt", before, after)

        assert "+new content" in result

    def test_build_unified_diff_empty_after(self):
        """测试有内容到空文件的 diff。"""
        from agent_py_agent.agent.subagents.manager_patch import SubAgentPatchMixin

        before = "old content\n"
        after = ""

        result = SubAgentPatchMixin._build_unified_diff("delete.txt", before, after)

        assert "-old content" in result


class TestValidatePatchTestCommand:
    """测试补丁测试命令验证函数。"""

    def test_validate_empty_command(self):
        """测试空命令被拒绝。"""
        from agent_py_agent.agent.subagents.manager_patch import SubAgentPatchMixin

        result = SubAgentPatchMixin._validate_patch_test_command("")
        assert "空测试命令" in result

    def test_validate_whitespace_command(self):
        """测试纯空白命令被拒绝。"""
        from agent_py_agent.agent.subagents.manager_patch import SubAgentPatchMixin

        result = SubAgentPatchMixin._validate_patch_test_command("   ")
        assert "空测试命令" in result

    def test_validate_blocked_chars(self):
        """测试高风险 shell 字符被拒绝。"""
        from agent_py_agent.agent.subagents.manager_patch import SubAgentPatchMixin

        result = SubAgentPatchMixin._validate_patch_test_command("pytest; rm -rf")
        assert "高风险" in result or "已阻止" in result

    def test_validate_not_in_allowlist(self):
        """测试不在白名单的命令被拒绝。"""
        from agent_py_agent.agent.subagents.manager_patch import SubAgentPatchMixin

        result = SubAgentPatchMixin._validate_patch_test_command("ruby test.rb")
        assert "不在 allowlist" in result or "已阻止" in result

    def test_validate_valid_python_command(self):
        """测试有效的 python 命令。"""
        from agent_py_agent.agent.subagents.manager_patch import SubAgentPatchMixin

        result = SubAgentPatchMixin._validate_patch_test_command("python test.py")
        assert result == ""

    def test_validate_valid_pytest_command(self):
        """测试有效的 pytest 命令。"""
        from agent_py_agent.agent.subagents.manager_patch import SubAgentPatchMixin

        result = SubAgentPatchMixin._validate_patch_test_command("pytest tests/")
        assert result == ""

    def test_validate_valid_python3_command(self):
        """测试有效的 python3 命令。"""
        from agent_py_agent.agent.subagents.manager_patch import SubAgentPatchMixin

        result = SubAgentPatchMixin._validate_patch_test_command("python3 -m pytest")
        assert result == ""

    def test_patch_apply_tests_normalize_python3_on_windows(self, tmp_path, monkeypatch):
        """LLM: Windows patch tests should avoid the python3 Store shim."""
        from agent_py_agent.agent.subagents.patch import patch_apply_helpers as helpers

        captured = {}

        class Completed:
            returncode = 0
            stdout = "ok"
            stderr = ""

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return Completed()

        monkeypatch.setattr(helpers.os, "name", "nt")
        monkeypatch.setattr(helpers.sys, "executable", r"C:\Python312\python.exe")
        monkeypatch.setattr(helpers.subprocess, "run", fake_run)

        results = helpers.run_patch_apply_tests(["python3 -c \"print('ok')\""], tmp_path)

        assert results[0]["ok"] is True
        assert captured["argv"][0] == r"C:\Python312\python.exe"
        assert captured["argv"][1:] == ["-c", "print('ok')"]

    def test_patch_apply_tests_keep_python3_on_posix(self, tmp_path, monkeypatch):
        """LLM: macOS/Linux patch tests should keep the caller's python3 command."""
        from agent_py_agent.agent.subagents.patch import patch_apply_helpers as helpers

        captured = {}

        class Completed:
            returncode = 0
            stdout = "ok"
            stderr = ""

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            return Completed()

        monkeypatch.setattr(helpers.os, "name", "posix")
        monkeypatch.setattr(helpers.subprocess, "run", fake_run)

        results = helpers.run_patch_apply_tests(["python3 -m pytest"], tmp_path)

        assert results[0]["ok"] is True
        assert captured["argv"][:3] == ["python3", "-m", "pytest"]

    def test_patch_apply_test_commands_read_structured_attributes_only(self):
        """补丁测试命令只能来自 attributes，不从 acceptance_checks 普通文案里抽 shell。"""

        task = SimpleNamespace(
            acceptance_checks=["command: pytest tests/from_acceptance.py"],
            attributes={"patch_test_commands": ["pytest tests/from_attributes.py"]},
        )

        commands, blocked = PatchApplyTestCommands.extract(task, output={})

        assert commands == ["pytest tests/from_attributes.py"]
        assert blocked == []

    def test_patch_apply_test_commands_keep_output_report_commands(self):
        """子代理 output.json 里的结构化 tests[].command 仍可作为补丁验证命令。"""

        task = SimpleNamespace(acceptance_checks=["test: pytest ignored.py"], attributes={})

        commands, blocked = PatchApplyTestCommands.extract(
            task,
            output={"tests": [{"command": "python -m pytest tests/from_output.py"}]},
        )

        assert commands == ["python -m pytest tests/from_output.py"]
        assert blocked == []

    def test_patch_apply_record_includes_owner_policy_and_batch_validation(self, tmp_path: Path):
        from agent_py_agent.agent.subagents.manager import SubAgentManager
        from agent_py_agent.agent.subagents.patch.patch_apply_task import (
            ApplyPatchTaskParams,
            apply_patch_task,
        )

        manager = SubAgentManager(tmp_path / "subagents", workspace_root=tmp_path)
        task = manager.create_run(
            goal="应用补丁",
            thought="记录 owner 策略和批量验证。",
            plan=["写文件"],
            extra_write_roots=[str(tmp_path)],
            attributes={"patch_test_commands": ["python -c \"print('ok')\""]},
        )
        output = {
            "patches": [
                {
                    "path": "target.txt",
                    "status": "planned",
                    "type": "write_file",
                    "content": "hello\n",
                }
            ]
        }

        record = apply_patch_task(
            manager,
            task,
            params=ApplyPatchTaskParams(output=output, patches=output["patches"], apply=False, applier="owner-a", note=""),
        )

        assert record.owner_policy["applier"] == "owner-a"
        assert record.owner_policy["run_id"] == task.id
        assert record.batch_validation["requested_patch_count"] == 1
        assert record.batch_validation["validated_patch_count"] == 1
        assert record.batch_validation["ready_for_apply"] is True
        assert record.failure_recovery["rollback_performed"] is False


class TestRollbackPatchApply:
    """测试补丁回滚函数。"""

    def test_rollback_restores_original_file(self, tmp_path: Path):
        """测试回滚恢复原文件。"""
        from agent_py_agent.agent.subagents.manager_patch import SubAgentPatchMixin

        test_file = tmp_path / "test.txt"
        test_file.write_text("original content", encoding="utf-8")

        touched_files = {
            test_file: {
                "before_exists": True,
                "before_text": "original content",
            }
        }

        SubAgentPatchMixin._rollback_patch_apply(touched_files)

        assert test_file.exists()
        assert test_file.read_text(encoding="utf-8") == "original content"

    def test_rollback_removes_new_file(self, tmp_path: Path):
        """测试回滚删除新文件。"""
        from agent_py_agent.agent.subagents.manager_patch import SubAgentPatchMixin

        test_file = tmp_path / "new.txt"
        test_file.write_text("new content", encoding="utf-8")

        touched_files = {
            test_file: {
                "before_exists": False,
                "before_text": "",
            }
        }

        SubAgentPatchMixin._rollback_patch_apply(touched_files)

        assert not test_file.exists()


class TestNormalizePatchApplySpec:
    """测试补丁规格归一化函数。"""

    def test_normalize_missing_path(self, tmp_path: Path):
        """测试缺少 path 的补丁被阻止。"""
        from agent_py_agent.agent.subagents.manager_patch import SubAgentPatchMixin
        from agent_py_agent.agent.subagents.models import SubAgentTask

        class MockManager(SubAgentPatchMixin):
            def __init__(self):
                self.workspace_root = tmp_path

        manager = MockManager()
        task = MagicMock(spec=SubAgentTask)
        task.allowed_write_roots = []
        task.forbidden_write_roots = []
        task.locked_files = []

        patch_item = {"status": "planned", "content": "test content"}
        result = manager._normalize_patch_apply_spec(task, patch_item)

        assert result["ok"] is False
        assert "BLOCKED" in result["audit"]["apply_status"]
        assert "缺少 path" in result["audit"]["message"]

    def test_normalize_invalid_status(self, tmp_path: Path):
        """测试无效状态的补丁被阻止。"""
        from agent_py_agent.agent.subagents.manager_patch import SubAgentPatchMixin
        from agent_py_agent.agent.subagents.models import SubAgentTask

        class MockManager(SubAgentPatchMixin):
            def __init__(self):
                self.workspace_root = tmp_path

        manager = MockManager()
        task = MagicMock(spec=SubAgentTask)
        task.allowed_write_roots = []
        task.forbidden_write_roots = []
        task.locked_files = []

        patch_item = {"path": "test.txt", "status": "rejected", "content": "test"}
        result = manager._normalize_patch_apply_spec(task, patch_item)

        assert result["ok"] is False
        assert "BLOCKED" in result["audit"]["apply_status"]

    def test_normalize_missing_content(self, tmp_path: Path):
        """测试缺少 content 的补丁被阻止。"""
        from agent_py_agent.agent.subagents.manager_patch import SubAgentPatchMixin
        from agent_py_agent.agent.subagents.models import SubAgentTask

        class MockManager(SubAgentPatchMixin):
            def __init__(self):
                self.workspace_root = tmp_path

        manager = MockManager()
        task = MagicMock(spec=SubAgentTask)
        task.allowed_write_roots = []
        task.forbidden_write_roots = []
        task.locked_files = []

        patch_item = {"path": "test.txt", "status": "planned"}
        result = manager._normalize_patch_apply_spec(task, patch_item)

        assert result["ok"] is False
        assert "缺少完整 content" in result["audit"]["message"]


class TestPatchReviewTask:
    """测试补丁审核任务函数。"""

    def test_review_no_patches(self, tmp_path: Path):
        """测试无补丁时的审核结果。"""
        from agent_py_agent.agent.subagents.manager_patch import SubAgentPatchMixin
        from agent_py_agent.agent.subagents.models import SubAgentTask

        class MockManager(SubAgentPatchMixin):
            def __init__(self):
                self.workspace_root = tmp_path

        manager = MockManager()
        task = MagicMock(spec=SubAgentTask)
        task.id = "test_task"
        task.output_json = str(tmp_path / "output.json")
        task.work_log_file = str(tmp_path / "work.log")

        output = {"patches": []}
        patches = []

        result = manager._review_patch_task(
            task,
            output=output,
            patches=patches,
            apply=False,
            reviewer="test",
            note="",
        )

        assert result.decision == "NO_PATCHES"
        assert result.ok is False

    def test_review_blocked_patches(self, tmp_path: Path):
        """测试被阻止的补丁。"""
        from agent_py_agent.agent.subagents.manager_patch import SubAgentPatchMixin
        from agent_py_agent.agent.subagents.models import SubAgentTask

        class MockManager(SubAgentPatchMixin):
            def __init__(self):
                self.workspace_root = tmp_path

        manager = MockManager()
        task = MagicMock(spec=SubAgentTask)
        task.id = "test_task"
        task.output_json = str(tmp_path / "output.json")
        task.work_log_file = str(tmp_path / "work.log")

        output = {"patches": [{"status": "planned"}]}
        patches = [{"status": "planned"}]

        result = manager._review_patch_task(
            task,
            output=output,
            patches=patches,
            apply=False,
            reviewer="test",
            note="",
        )

        assert result.decision == "REJECT"
        assert result.ok is False

    def test_review_report_exposes_dirty_output_json_when_auto_scanning(self, tmp_path: Path):
        """坏 output.json 不能在自动扫描时被吞成“没有 patch”。"""
        from agent_py_agent.agent.subagents.patch import PatchReviewService

        task = SimpleNamespace(
            id="dirty_review_task",
            output_json=str(tmp_path / "output.json"),
            work_log_file=str(tmp_path / "work.log"),
        )
        Path(task.output_json).write_text("{bad json", encoding="utf-8")

        manager = SimpleNamespace(
            workspace=tmp_path,
            workspace_root=tmp_path,
            _select_runs=lambda run_ids=None: [task],
        )

        report = PatchReviewService(manager).review_patches(run_ids=None)

        assert len(report.records) == 1
        record = report.records[0]
        assert record.decision == "OUTPUT_LOAD_ERROR"
        assert record.ok is False
        assert record.load_errors
        assert record.load_errors[0]["context"] == "patch_review.output_json"


class TestResolvePatchTarget:
    """测试补丁目标路径解析函数。"""

    def test_resolve_absolute_path(self, tmp_path: Path):
        """测试绝对路径解析。"""
        from agent_py_agent.agent.subagents.manager_patch import SubAgentPatchMixin

        class MockManager(SubAgentPatchMixin):
            def __init__(self):
                self.workspace_root = tmp_path

        manager = MockManager()
        abs_path = "/tmp/test.txt"
        result = manager._resolve_patch_target(abs_path)

        assert result.is_absolute()

    def test_resolve_relative_path(self, tmp_path: Path):
        """测试相对路径解析。"""
        from agent_py_agent.agent.subagents.manager_patch import SubAgentPatchMixin

        class MockManager(SubAgentPatchMixin):
            def __init__(self):
                self.workspace_root = tmp_path

        manager = MockManager()
        rel_path = "subdir/test.txt"
        result = manager._resolve_patch_target(rel_path)

        assert result.is_absolute()
        assert str(tmp_path) in str(result)


class TestPatchApplyReport:
    """测试补丁应用报告生成。"""

    def test_apply_report_exposes_dirty_output_json_when_auto_scanning(self, tmp_path: Path):
        """坏 output.json 不能在自动扫描时被吞成“没有 patch”。"""
        from agent_py_agent.agent.subagents.patch import PatchApplyService

        task = SimpleNamespace(
            id="dirty_apply_task",
            output_json=str(tmp_path / "output.json"),
            work_log_file=str(tmp_path / "work.log"),
        )
        Path(task.output_json).write_text("{bad json", encoding="utf-8")

        manager = SimpleNamespace(
            workspace=tmp_path,
            workspace_root=tmp_path,
            _select_runs=lambda run_ids=None: [task],
        )

        report = PatchApplyService(manager).apply_patches(run_ids=None)

        assert len(report.records) == 1
        record = report.records[0]
        assert record.decision == "OUTPUT_LOAD_ERROR"
        assert record.ok is False
        assert record.load_errors
        assert record.load_errors[0]["context"] == "patch_apply.output_json"

    def test_real_apply_preserves_output_load_error_after_success_rewrite(self, tmp_path: Path):
        """真实 apply 成功后，也不能抹掉 apply 前读到的坏 output.json 诊断。"""
        from agent_py_agent.agent.subagents.patch import PatchApplyService

        task = SimpleNamespace(
            id="dirty_apply_success_task",
            output_json=str(tmp_path / "output.json"),
            work_log_file=str(tmp_path / "work.log"),
            allowed_write_roots=[],
            forbidden_write_roots=[],
            locked_files=[],
            acceptance_checks=[],
        )
        Path(task.output_json).write_text("{bad json", encoding="utf-8")
        target = tmp_path / "target.txt"

        class MockManager:
            workspace = tmp_path
            workspace_root = tmp_path

            @staticmethod
            def _append_task_work_log(task, message):
                Path(task.work_log_file).write_text(message, encoding="utf-8")

        record = PatchApplyService(MockManager())._apply_patch_task(
            task,
            output={},
            patches=[
                {
                    "path": str(target),
                    "status": "planned",
                    "tool": "write_file",
                    "content": "fixed\n",
                }
            ],
            apply=True,
            applier="test",
        )

        payload = json.loads(Path(task.output_json).read_text(encoding="utf-8"))
        assert record.ok is True
        assert target.read_text(encoding="utf-8") == "fixed\n"
        assert payload["load_errors"][0]["context"] == "patch_apply.success_output_json"

    def test_apply_dry_run_mode(self, tmp_path: Path):
        """测试 dry-run 模式不实际写入文件。"""
        from agent_py_agent.agent.subagents.manager_patch import SubAgentPatchMixin
        from agent_py_agent.agent.subagents.models import SubAgentTask

        class MockManager(SubAgentPatchMixin):
            def __init__(self):
                self.workspace_root = tmp_path
                self.workspace = tmp_path

        manager = MockManager()
        task = MagicMock(spec=SubAgentTask)
        task.id = "test_task"
        task.output_json = str(tmp_path / "output.json")
        task.work_log_file = str(tmp_path / "work.log")
        task.allowed_write_roots = []
        task.forbidden_write_roots = []
        task.locked_files = []
        task.acceptance_checks = []

        test_file = tmp_path / "test.txt"
        test_file.write_text("original", encoding="utf-8")

        output = {"patches": []}
        patches = [
            {
                "path": str(test_file),
                "status": "planned",
                "tool": "write_file",
                "content": "modified content",
            }
        ]

        result = manager._apply_patch_task(
            task,
            output=output,
            patches=patches,
            apply=False,
            applier="test",
            note="",
        )

        # dry-run 模式
        assert result.dry_run is True
        # 文件不应被修改
        assert test_file.read_text(encoding="utf-8") == "original"
