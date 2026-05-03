"""manager_patch 模块测试。

测试补丁审核、应用、回滚和版本控制功能。
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock
import pytest


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


class TestExtractPatchTestCommand:
    """测试补丁测试命令提取函数。"""

    def test_extract_with_command_prefix(self):
        """测试带 command: 前缀的提取。"""
        from agent_py_agent.agent.subagents.manager_patch import SubAgentPatchMixin

        result = SubAgentPatchMixin._extract_patch_test_command("command: pytest test.py")
        assert result == "pytest test.py"

    def test_extract_with_test_prefix(self):
        """测试带 test: 前缀的提取。"""
        from agent_py_agent.agent.subagents.manager_patch import SubAgentPatchMixin

        result = SubAgentPatchMixin._extract_patch_test_command("test: python -m pytest")
        assert result == "python -m pytest"

    def test_extract_with_run_prefix(self):
        """测试带 run: 前缀的提取。"""
        from agent_py_agent.agent.subagents.manager_patch import SubAgentPatchMixin

        result = SubAgentPatchMixin._extract_patch_test_command("run: pytest")
        assert result == "pytest"

    def test_extract_with_backticks(self):
        """测试带反引号的提取。"""
        from agent_py_agent.agent.subagents.manager_patch import SubAgentPatchMixin

        result = SubAgentPatchMixin._extract_patch_test_command("`pytest test.py`")
        assert result == "pytest test.py"

    def test_extract_no_prefix(self):
        """测试无前缀的原始命令。"""
        from agent_py_agent.agent.subagents.manager_patch import SubAgentPatchMixin

        result = SubAgentPatchMixin._extract_patch_test_command("pytest test.py")
        assert result == ""


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

        patch_item = {"path": "test.txt", "status": "applied", "content": "test"}
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

        with patch("agent_py_agent.agent.subagents.manager_patch._read_json_object", return_value=output):
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

        with patch("agent_py_agent.agent.subagents.manager_patch._read_json_object", return_value=output):
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

        with patch("agent_py_agent.agent.subagents.manager_patch._read_json_object", return_value=output):
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
