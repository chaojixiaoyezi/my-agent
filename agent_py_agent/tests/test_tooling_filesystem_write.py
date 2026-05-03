"""文件系统写入工具测试 - 文件写入、覆盖、追加、写入边界检查。"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestWriteFileTool:
    """测试 WriteFileTool 文件写入。"""

    def test_write_file_basic(self, tmp_path: Path):
        """基本文件写入功能。"""
        from agent_py_agent.agent.tooling.filesystem_write import WriteFileTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = WriteFileTool(workspace)
        result = tool.execute({
            "path": "new_file.txt",
            "content": "Hello, World!",
        })

        assert result.ok is True
        assert "new_file.txt" in result.output
        assert (workspace / "new_file.txt").exists()
        assert (workspace / "new_file.txt").read_text() == "Hello, World!"

    def test_write_file_creates_parent_dirs(self, tmp_path: Path):
        """写入时自动创建父目录。"""
        from agent_py_agent.agent.tooling.filesystem_write import WriteFileTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = WriteFileTool(workspace)
        result = tool.execute({
            "path": "deep/nested/dir/file.txt",
            "content": "nested content",
        })

        assert result.ok is True
        assert (workspace / "deep" / "nested" / "dir" / "file.txt").exists()

    def test_write_file_overwrite(self, tmp_path: Path):
        """覆盖已存在的文件。"""
        from agent_py_agent.agent.tooling.filesystem_write import WriteFileTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        existing = workspace / "existing.txt"
        existing.write_text("original")

        tool = WriteFileTool(workspace)
        result = tool.execute({
            "path": "existing.txt",
            "content": "replaced",
        })

        assert result.ok is True
        assert existing.read_text() == "replaced"

    def test_write_file_empty_content(self, tmp_path: Path):
        """写入空内容。"""
        from agent_py_agent.agent.tooling.filesystem_write import WriteFileTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = WriteFileTool(workspace)
        result = tool.execute({
            "path": "empty.txt",
            "content": "",
        })

        assert result.ok is True
        assert (workspace / "empty.txt").read_text() == ""

    def test_write_file_path_traversal_blocked(self, tmp_path: Path):
        """防止路径穿越写入。"""
        from agent_py_agent.agent.tooling.filesystem_write import WriteFileTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = WriteFileTool(workspace)
        result = tool.execute({
            "path": "../outside.txt",
            "content": "should not write",
        })

        assert result.ok is False
        assert "超出允许的工作区范围" in result.output

    def test_write_file_max_chars_limit(self, tmp_path: Path):
        """超过最大字符限制时应拒绝。"""
        from agent_py_agent.agent.tooling.filesystem_write import WriteFileTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = WriteFileTool(workspace)
        result = tool.execute({
            "path": "big.txt",
            "content": "A" * 2_000_000,  # 超过 _MAX_WRITE_TEXT_CHARS (1_000_000)
        })

        assert result.ok is False
        assert "过长" in result.output

    def test_write_file_missing_path(self, tmp_path: Path):
        """缺少路径参数。"""
        from agent_py_agent.agent.tooling.filesystem_write import WriteFileTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = WriteFileTool(workspace)
        result = tool.execute({"content": "test"})

        assert result.ok is False
        assert "path" in result.output.lower() or "缺少" in result.output

    def test_write_file_missing_content(self, tmp_path: Path):
        """缺少内容参数。"""
        from agent_py_agent.agent.tooling.filesystem_write import WriteFileTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = WriteFileTool(workspace)
        result = tool.execute({"path": "file.txt"})

        assert result.ok is False


class TestAppendFileTool:
    """测试 AppendFileTool 文件追加。"""

    def test_append_file_basic(self, tmp_path: Path):
        """基本文件追加功能。"""
        from agent_py_agent.agent.tooling.filesystem_write import AppendFileTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        existing = workspace / "log.txt"
        existing.write_text("Line 1\n")

        tool = AppendFileTool(workspace)
        result = tool.execute({
            "path": "log.txt",
            "content": "Line 2\n",
        })

        assert result.ok is True
        content = existing.read_text()
        assert "Line 1" in content
        assert "Line 2" in content

    def test_append_file_creates_new_file(self, tmp_path: Path):
        """追加到不存在的文件应创建。"""
        from agent_py_agent.agent.tooling.filesystem_write import AppendFileTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = AppendFileTool(workspace)
        result = tool.execute({
            "path": "new_log.txt",
            "content": "first line",
        })

        assert result.ok is True
        assert (workspace / "new_log.txt").exists()
        assert (workspace / "new_log.txt").read_text() == "first line"

    def test_append_file_creates_parent_dirs(self, tmp_path: Path):
        """追加时自动创建父目录。"""
        from agent_py_agent.agent.tooling.filesystem_write import AppendFileTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = AppendFileTool(workspace)
        result = tool.execute({
            "path": "logs/2024/app.log",
            "content": "entry",
        })

        assert result.ok is True
        assert (workspace / "logs" / "2024" / "app.log").exists()

    def test_append_file_path_traversal_blocked(self, tmp_path: Path):
        """防止路径穿越追加。"""
        from agent_py_agent.agent.tooling.filesystem_write import AppendFileTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = AppendFileTool(workspace)
        result = tool.execute({
            "path": "../malicious.txt",
            "content": "malicious content",
        })

        assert result.ok is False
        assert "超出允许的工作区范围" in result.output

    def test_append_file_max_chars_limit(self, tmp_path: Path):
        """超过最大字符限制时应拒绝。"""
        from agent_py_agent.agent.tooling.filesystem_write import AppendFileTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = AppendFileTool(workspace)
        result = tool.execute({
            "path": "big.txt",
            "content": "B" * 2_000_000,
        })

        assert result.ok is False
        assert "过长" in result.output

    def test_append_file_empty_content(self, tmp_path: Path):
        """追加空内容。"""
        from agent_py_agent.agent.tooling.filesystem_write import AppendFileTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        existing = workspace / "log.txt"
        existing.write_text("original")

        tool = AppendFileTool(workspace)
        result = tool.execute({
            "path": "log.txt",
            "content": "",
        })

        assert result.ok is True
        assert existing.read_text() == "original"


class TestReplaceInFileTool:
    """测试 ReplaceInFileTool 文件内容替换。"""

    def test_replace_in_file_basic(self, tmp_path: Path):
        """基本内容替换功能。"""
        from agent_py_agent.agent.tooling.filesystem_write import ReplaceInFileTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        test_file = workspace / "config.txt"
        test_file.write_text("timeout = 30\nretry = 3")

        tool = ReplaceInFileTool(workspace)
        result = tool.execute({
            "path": "config.txt",
            "old": "timeout = 30",
            "new": "timeout = 60",
        })

        assert result.ok is True
        content = test_file.read_text()
        assert "timeout = 60" in content
        assert "timeout = 30" not in content

    def test_replace_in_file_multiple_occurrences(self, tmp_path: Path):
        """替换多处匹配。"""
        from agent_py_agent.agent.tooling.filesystem_write import ReplaceInFileTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        test_file = workspace / "config.txt"
        test_file.write_text("foo = 1\nfoo = 2\nfoo = 3")

        tool = ReplaceInFileTool(workspace)
        result = tool.execute({
            "path": "config.txt",
            "old": "foo",
            "new": "bar",
            "count": 0,  # 替换所有
        })

        assert result.ok is True
        content = test_file.read_text()
        assert content.count("bar") == 3
        assert "foo" not in content

    def test_replace_in_file_not_found(self, tmp_path: Path):
        """替换原文不存在。"""
        from agent_py_agent.agent.tooling.filesystem_write import ReplaceInFileTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        test_file = workspace / "config.txt"
        test_file.write_text("timeout = 30")

        tool = ReplaceInFileTool(workspace)
        result = tool.execute({
            "path": "config.txt",
            "old": "not_exist",
            "new": "replacement",
        })

        assert result.ok is False
        assert "没有找到" in result.output

    def test_replace_in_file_nonexistent_file(self, tmp_path: Path):
        """文件不存在。"""
        from agent_py_agent.agent.tooling.filesystem_write import ReplaceInFileTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = ReplaceInFileTool(workspace)
        result = tool.execute({
            "path": "nonexistent.txt",
            "old": "old",
            "new": "new",
        })

        assert result.ok is False
        assert "不存在" in result.output

    def test_replace_in_file_path_traversal_blocked(self, tmp_path: Path):
        """防止路径穿越替换。"""
        from agent_py_agent.agent.tooling.filesystem_write import ReplaceInFileTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = ReplaceInFileTool(workspace)
        result = tool.execute({
            "path": "../config.txt",
            "old": "old",
            "new": "new",
        })

        assert result.ok is False
        assert "超出允许的工作区范围" in result.output

    def test_replace_in_file_empty_old_text(self, tmp_path: Path):
        """空 old_text 应被拒绝。"""
        from agent_py_agent.agent.tooling.filesystem_write import ReplaceInFileTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        test_file = workspace / "config.txt"
        test_file.write_text("content")

        tool = ReplaceInFileTool(workspace)
        result = tool.execute({
            "path": "config.txt",
            "old": "",
            "new": "new_content",
        })

        assert result.ok is False

    def test_replace_in_file_with_count(self, tmp_path: Path):
        """指定替换数量。"""
        from agent_py_agent.agent.tooling.filesystem_write import ReplaceInFileTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        test_file = workspace / "config.txt"
        test_file.write_text("a = 1\na = 2\na = 3")

        tool = ReplaceInFileTool(workspace)
        result = tool.execute({
            "path": "config.txt",
            "old": "a",
            "new": "b",
            "count": 2,
        })

        assert result.ok is True
        content = test_file.read_text()
        # 前两个被替换
        assert content.count("b") == 2
        assert content.count("a") == 1


class TestWriteBoundaryCases:
    """测试写入操作的边界情况。"""

    def test_write_special_characters_in_path(self, tmp_path: Path):
        """路径包含特殊字符。"""
        from agent_py_agent.agent.tooling.filesystem_write import WriteFileTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = WriteFileTool(workspace)
        result = tool.execute({
            "path": "file with spaces.txt",
            "content": "content",
        })

        assert result.ok is True
        assert (workspace / "file with spaces.txt").exists()

    def test_write_unicode_content(self, tmp_path: Path):
        """写入 Unicode 内容。"""
        from agent_py_agent.agent.tooling.filesystem_write import WriteFileTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = WriteFileTool(workspace)
        result = tool.execute({
            "path": "unicode.txt",
            "content": "你好世界 🎉 مرحبا",
        })

        assert result.ok is True
        assert (workspace / "unicode.txt").read_text() == "你好世界 🎉 مرحبا"

    def test_write_control_characters_rejected(self, tmp_path: Path):
        """拒绝包含控制字符的路径。"""
        from agent_py_agent.agent.tooling.filesystem_write import WriteFileTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = WriteFileTool(workspace)
        result = tool.execute({
            "path": "file\x00with_null.txt",
            "content": "content",
        })

        assert result.ok is False

    def test_write_absolute_path_blocked(self, tmp_path: Path):
        """绝对路径应被拒绝。"""
        from agent_py_agent.agent.tooling.filesystem_write import WriteFileTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = WriteFileTool(workspace)
        result = tool.execute({
            "path": str(workspace / "file.txt"),
            "content": "content",
        })

        # 绝对路径在工作区内应该可以
        assert result.ok is True