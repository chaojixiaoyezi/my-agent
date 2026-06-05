"""文件系统只读工具测试 - 文件读取、搜索、路径校验、权限检查。"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestFileSystemToolBase:
    """测试 FileSystemTool 基类的路径解析和安全边界。"""

    def test_resolve_path_within_workspace(self, tmp_path: Path):
        """路径在工作区内时应正确解析。"""
        from agent_py_agent.agent.tooling.filesystem import (
            FileSystemTool,
            filesystem_access_options,
        )

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = FileSystemTool(workspace)

        # 相对路径
        result = tool.resolve_path("subdir/file.txt")
        assert str(result).startswith(str(workspace))

        # 绝对路径在工作区内
        result = tool.resolve_path(workspace / "another/file.txt")
        assert str(result).startswith(str(workspace))

    def test_resolve_home_path(self, tmp_path: Path, monkeypatch):
        """~/ 路径按当前用户 home 解析，而不是当作工作区下的普通目录。"""
        from agent_py_agent.agent.tooling.filesystem import FileSystemTool

        home = tmp_path / "home"
        workspace = tmp_path / "workspace"
        home.mkdir()
        workspace.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        tool = FileSystemTool(workspace)

        result = tool.resolve_path("~/notes/report.md")

        assert result == (home / "notes" / "report.md").resolve(strict=False)

    def test_resolve_path_outside_workspace(self, tmp_path: Path):
        """普通工作区外路径不再默认拒绝。"""
        from agent_py_agent.agent.tooling.filesystem import FileSystemTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        outside = tmp_path / "outside"

        tool = FileSystemTool(workspace)

        result = tool.resolve_path(str(outside / "secret.txt"))
        assert result == (outside / "secret.txt").resolve(strict=False)

    def test_resolve_path_with_parent_traversal(self, tmp_path: Path):
        """../ 现在按真实目标走危险目录策略，不按工作区硬拦。"""
        from agent_py_agent.agent.tooling.filesystem import FileSystemTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = FileSystemTool(workspace)

        assert tool.resolve_path("../secret.txt") == (tmp_path / "secret.txt").resolve(strict=False)

        assert tool.resolve_path("subdir/../../etc/passwd") == (tmp_path / "etc/passwd").resolve(strict=False)

    def test_resolve_path_with_symlink_outside(self, tmp_path: Path):
        """符号链接指向普通外部目录时不再默认拒绝。"""
        import sys

        from agent_py_agent.agent.tooling.filesystem import FileSystemTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()

        tool = FileSystemTool(workspace)

        # 创建指向外部的符号链接
        secret_file = outside_dir / "secret.txt"
        secret_file.write_text("secret")

        symlink = workspace / "link_to_outside"
        try:
            symlink.symlink_to(secret_file)
        except OSError as e:
            if sys.platform == "win32" and e.winerror == 1314:
                pytest.skip("Symbolic links require admin privileges on Windows")
            raise

        assert tool.resolve_path("link_to_outside") == secret_file.resolve(strict=False)

    def test_resolve_path_blocks_configured_dangerous_root(self, tmp_path: Path):
        """危险目录仍会被统一策略拒绝。"""
        from agent_py_agent.agent.tooling.filesystem import (
            FileSystemTool,
            filesystem_access_options,
        )

        workspace = tmp_path / "workspace"
        danger = tmp_path / "danger"
        workspace.mkdir()
        danger.mkdir()
        tool = FileSystemTool(workspace, access_options=filesystem_access_options(path_dangerous_roots=[str(danger)]))

        with pytest.raises(ValueError, match="危险目录"):
            tool.resolve_path(str(danger / "secret.txt"))

    def test_display_path_within_workspace(self, tmp_path: Path):
        """工作区内的路径应显示相对路径。"""
        from agent_py_agent.agent.tooling.filesystem import FileSystemTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        subdir = workspace / "subdir"
        subdir.mkdir()

        tool = FileSystemTool(workspace)

        result = tool.display_path(subdir / "file.txt")
        assert result.replace("\\", "/") == "subdir/file.txt"

    def test_display_path_outside_workspace(self, tmp_path: Path):
        """工作区外路径显示绝对路径，方便模型按真实路径继续修。"""
        from agent_py_agent.agent.tooling.filesystem import FileSystemTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        tool = FileSystemTool(workspace)

        outside_path = tmp_path / "outside" / "file.txt"
        result = tool.display_path(outside_path)
        assert result == str(outside_path).replace("\\", "/")


class TestListFilesTool:
    """测试 ListFilesTool 列出目录内容。"""

    def test_list_files_basic(self, tmp_path: Path):
        """基本目录列表功能。"""
        from agent_py_agent.agent.tooling.filesystem import ListFilesTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "file1.txt").write_text("content1")
        (workspace / "file2.txt").write_text("content2")
        subdir = workspace / "subdir"
        subdir.mkdir()
        (subdir / "file3.txt").write_text("content3")

        tool = ListFilesTool(workspace, max_entries=100)
        result = tool.execute({})

        assert result.ok is True
        assert "file1.txt" in result.output
        assert "file2.txt" in result.output

    def test_list_files_nonexistent_path(self, tmp_path: Path):
        """列出不存在的目录。"""
        from agent_py_agent.agent.tooling.filesystem import ListFilesTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = ListFilesTool(workspace, max_entries=100)
        result = tool.execute({"path": "nonexistent_dir"})

        assert result.ok is False
        assert "不存在" in result.output

    def test_list_files_recursive(self, tmp_path: Path):
        """递归列出目录。"""
        from agent_py_agent.agent.tooling.filesystem import ListFilesTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "file1.txt").write_text("content1")
        subdir = workspace / "subdir"
        subdir.mkdir()
        (subdir / "file2.txt").write_text("content2")

        tool = ListFilesTool(workspace, max_entries=100)
        result = tool.execute({"path": ".", "recursive": True})

        assert result.ok is True
        assert "file1.txt" in result.output
        assert "file2.txt" in result.output

    def test_list_files_max_entries(self, tmp_path: Path):
        """最大条目数限制。"""
        from agent_py_agent.agent.tooling.filesystem import ListFilesTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        for i in range(20):
            (workspace / f"file{i}.txt").write_text(f"content{i}")

        tool = ListFilesTool(workspace, max_entries=5)
        result = tool.execute({})

        assert result.ok is True
        assert "已截断" in result.output
        assert "next_offset=5" in result.output
        assert "limit=5" in result.output

    def test_list_files_single_file(self, tmp_path: Path):
        """列出单个文件。"""
        from agent_py_agent.agent.tooling.filesystem import ListFilesTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        test_file = workspace / "test.txt"
        test_file.write_text("content")

        tool = ListFilesTool(workspace, max_entries=100)
        result = tool.execute({"path": "test.txt"})

        assert result.ok is True


class TestReadFileTool:
    """测试 ReadFileTool 读取文件内容。"""

    def test_read_file_basic(self, tmp_path: Path):
        """基本文件读取功能。"""
        from agent_py_agent.agent.tooling.filesystem import ReadFileTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        test_file = workspace / "test.txt"
        test_file.write_text("Hello, World!\nLine 2\nLine 3")

        tool = ReadFileTool(workspace, max_chars=10000)
        result = tool.execute({"path": "test.txt"})

        assert result.ok is True
        assert "Hello, World!" in result.output

    def test_read_file_nonexistent(self, tmp_path: Path):
        """读取不存在的文件。"""
        from agent_py_agent.agent.tooling.filesystem import ReadFileTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = ReadFileTool(workspace, max_chars=10000)
        result = tool.execute({"path": "nonexistent.txt"})

        assert result.ok is False
        assert "不存在" in result.output

    def test_read_file_missing_target_ignores_retired_write_session_state(self, tmp_path: Path):
        """旧 file_write_sessions 状态不再影响普通 read_file 缺失错误。"""
        import json

        from agent_py_agent.agent.tooling.filesystem import ReadFileTool

        workspace = tmp_path / "workspace"
        session_dir = workspace / ".agent_write_files" / "session-open"
        session_dir.mkdir(parents=True)
        (session_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "session_id": "session-open",
                    "status": "open",
                    "target_path": {
                        "display": "outputs/report.html",
                        "raw": "outputs/report.html",
                        "resolved": str((workspace / "outputs/report.html").resolve()),
                    },
                    "chunks": {"0": {}, "1": {}},
                }
            ),
            encoding="utf-8",
        )

        tool = ReadFileTool(workspace, max_chars=10000)
        result = tool.execute({"path": "outputs/report.html"})

        assert result.ok is False
        assert result.result_envelope["path_not_found"] is True
        assert result.result_envelope["candidate_paths"] == []
        assert "文件不存在" in result.output

    def test_read_file_with_line_range(self, tmp_path: Path):
        """按行范围读取文件。"""
        from agent_py_agent.agent.tooling.filesystem import ReadFileTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        test_file = workspace / "test.txt"
        test_file.write_text("Line 1\nLine 2\nLine 3\nLine 4\nLine 5")

        tool = ReadFileTool(workspace, max_chars=10000)
        result = tool.execute({"path": "test.txt", "start_line": 2, "end_line": 4})

        assert result.ok is True
        assert "2: Line 2" in result.output
        assert "3: Line 3" in result.output
        assert "4: Line 4" in result.output

    def test_read_file_invalid_line_range(self, tmp_path: Path):
        """无效的行列范围。"""
        from agent_py_agent.agent.tooling.filesystem import ReadFileTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        test_file = workspace / "test.txt"
        test_file.write_text("Line 1\nLine 2\nLine 3")

        tool = ReadFileTool(workspace, max_chars=10000)
        result = tool.execute({"path": "test.txt", "start_line": 5, "end_line": 2})

        assert result.ok is False
        assert "end_line 不能小于 start_line" in result.output

    def test_read_file_truncation(self, tmp_path: Path):
        """超长文件应被截断。"""
        from agent_py_agent.agent.tooling.filesystem import ReadFileTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        test_file = workspace / "test.txt"
        test_file.write_text("A" * 10000)

        tool = ReadFileTool(workspace, max_chars=100)
        result = tool.execute({"path": "test.txt"})

        assert result.ok is True
        assert "PARTIAL view only" in result.output

    def test_read_file_empty(self, tmp_path: Path):
        """读取空文件。"""
        from agent_py_agent.agent.tooling.filesystem import ReadFileTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        test_file = workspace / "empty.txt"
        test_file.write_text("")

        tool = ReadFileTool(workspace, max_chars=10000)
        result = tool.execute({"path": "empty.txt"})

        assert result.ok is True
        assert "(空文件)" in result.output

    def test_read_file_summarizes_subagent_continue_packet(self, tmp_path: Path):
        """子代理恢复包默认摘要读取，避免把大量 refs 直接塞回 prompt。"""
        import json

        from agent_py_agent.agent.tooling.filesystem import ReadFileTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        packet = workspace / "latest_continue_packet.json"
        packet.write_text(
            json.dumps(
                {
                    "schema_version": "subagent_continue_packet.v1",
                    "kind": "subagent_task_local_continue_packet",
                    "status": "RUNNING",
                    "ready_to_continue": True,
                    "latest_summary": "",
                    "work_progress": {},
                    "session_compact": {},
                    "recommended_read_paths": ["/tmp/packet", "/tmp/checkpoint"],
                    "restore_refs": {f"ref_{index}": "/tmp/" + ("x" * 200) for index in range(20)},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        result = ReadFileTool(workspace, max_chars=50000).execute({"path": "latest_continue_packet.json"})

        assert result.ok is True
        assert "structured_read_summary=true" in result.output
        assert "work_progress=none" in result.output
        assert "session_compact=none" in result.output
        assert "restore_refs" not in result.output

    def test_read_file_line_range_preserves_full_continue_packet_read(self, tmp_path: Path):
        """显式按行读取时保留普通 read_file 行号行为，方便调试原始 packet。"""
        import json

        from agent_py_agent.agent.tooling.filesystem import ReadFileTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        packet = workspace / "latest_continue_packet.json"
        packet.write_text(
            json.dumps(
                {"kind": "subagent_task_local_continue_packet", "restore_refs": {"a": "b"}},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        result = ReadFileTool(workspace, max_chars=50000).execute(
            {"path": "latest_continue_packet.json", "start_line": 1, "end_line": 3}
        )

        assert result.ok is True
        assert "1: {" in result.output
        assert "structured_read_summary" not in result.output


class TestSearchTextTool:
    """测试 SearchTextTool 文本搜索。"""

    def test_search_text_basic(self, tmp_path: Path):
        """基本文本搜索功能。"""
        from agent_py_agent.agent.tooling.filesystem import SearchTextTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "file1.txt").write_text("Hello World")
        (workspace / "file2.txt").write_text("Goodbye World")

        tool = SearchTextTool(workspace, max_matches=100)
        result = tool.execute({"query": "World"})

        assert result.ok is True
        assert "file1.txt" in result.output
        assert "file2.txt" in result.output

    def test_search_text_no_matches(self, tmp_path: Path):
        """无匹配结果。"""
        from agent_py_agent.agent.tooling.filesystem import SearchTextTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "file.txt").write_text("Hello World")

        tool = SearchTextTool(workspace, max_matches=100)
        result = tool.execute({"query": "NotFound"})

        assert result.ok is True
        assert "没有找到匹配项" in result.output

    def test_search_text_nonexistent_path(self, tmp_path: Path):
        """搜索不存在的路径。"""
        from agent_py_agent.agent.tooling.filesystem import SearchTextTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = SearchTextTool(workspace, max_matches=100)
        result = tool.execute({"query": "test", "path": "nonexistent"})

        assert result.ok is False
        assert "不存在" in result.output

    def test_search_text_max_matches(self, tmp_path: Path):
        """最大匹配数限制。"""
        from agent_py_agent.agent.tooling.filesystem import SearchTextTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        for i in range(50):
            (workspace / f"file{i}.txt").write_text("match")

        tool = SearchTextTool(workspace, max_matches=5)
        result = tool.execute({"query": "match"})

        assert result.ok is True
        assert "已截断" in result.output
        assert "next_offset=5" in result.output
        assert "limit=5" in result.output

    def test_search_text_path_traversal_blocked(self, tmp_path: Path):
        """搜索时防止路径穿越。"""
        from agent_py_agent.agent.tooling.filesystem import SearchTextTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tool = SearchTextTool(workspace, max_matches=100)
        result = tool.execute({"query": "test", "path": "../outside"})

        assert result.ok is False

    def test_search_text_with_subdirectory(self, tmp_path: Path):
        """搜索子目录中的文件。"""
        from agent_py_agent.agent.tooling.filesystem import SearchTextTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        subdir = workspace / "subdir"
        subdir.mkdir()
        (subdir / "inner.txt").write_text("found it")

        tool = SearchTextTool(workspace, max_matches=100)
        result = tool.execute({"query": "found", "path": "subdir"})

        assert result.ok is True
        assert "inner.txt" in result.output

    def test_search_text_accepts_filesystem_bundle_path(self, tmp_path: Path):
        """search_text 兼容模型常见的 filesystem.path bundle，并且不会退回全工作区。"""
        from agent_py_agent.agent.tooling.filesystem import SearchTextTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "outside.txt").write_text("needle")
        scoped = workspace / "scoped"
        scoped.mkdir()
        (scoped / "inside.txt").write_text("needle")

        tool = SearchTextTool(workspace, max_matches=100)
        result = tool.execute({"query": "needle", "filesystem": {"path": "scoped"}})

        assert result.ok is True
        assert "inside.txt" in result.output
        assert "outside.txt" not in result.output
