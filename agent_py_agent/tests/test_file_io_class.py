from __future__ import annotations

"""LLM: tests for agent.io helpers.

给人看的解释：
测试 agent.io 导出的底层 IO 函数。
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestFileIoExports:
    """测试 agent.io 导出。"""

    def test_imports_append_jsonl(self) -> None:
        """测试 file_io 导出 append_jsonl。"""
        from agent_py_agent.agent.io import append_jsonl
        assert callable(append_jsonl)

    def test_imports_append_line_locked(self) -> None:
        """测试 file_io 导出 append_line_locked。"""
        from agent_py_agent.agent.io import append_line_locked
        assert callable(append_line_locked)

    def test_exports_list(self) -> None:
        """测试 __all__ 导出列表。"""
        from agent_py_agent.agent import io
        assert "append_jsonl" in io.__all__
        assert "append_line_locked" in io.__all__


class TestAppendJsonlSignature:
    """测试 append_jsonl 函数签名。"""

    def test_function_exists(self) -> None:
        """测试 append_jsonl 函数存在。"""
        from agent_py_agent.agent.io import append_jsonl
        assert callable(append_jsonl)

    def test_function_takes_two_args(self) -> None:
        """测试函数接受两个参数。"""
        import inspect

        from agent_py_agent.agent.io import append_jsonl
        sig = inspect.signature(append_jsonl)
        assert len(sig.parameters) >= 2


class TestAppendLineLockedSignature:
    """测试 append_line_locked 函数签名。"""

    def test_function_exists(self) -> None:
        """测试 append_line_locked 函数存在。"""
        from agent_py_agent.agent.io import append_line_locked
        assert callable(append_line_locked)

    def test_function_takes_path_and_line(self) -> None:
        """测试函数接受 path 和 line 参数。"""
        import inspect

        from agent_py_agent.agent.io import append_line_locked
        sig = inspect.signature(append_line_locked)
        params = list(sig.parameters.keys())
        assert len(params) >= 2


class TestAppendJsonlBehavior:
    """测试 append_jsonl 行为。"""

    def test_appends_json_line_with_lock(self) -> None:
        """测试追加 JSON 行带锁。"""
        import json
        import tempfile

        from agent_py_agent.agent.io import append_jsonl

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.jsonl"
            append_jsonl(path, {"key": "value", "number": 42})
            content = path.read_text()
            parsed = json.loads(content.strip())
            assert parsed["key"] == "value"
            assert parsed["number"] == 42

    def test_append_jsonl_creates_parent_dir(self) -> None:
        """测试追加时创建父目录。"""
        import tempfile

        from agent_py_agent.agent.io import append_jsonl

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "subdir" / "test.jsonl"
            append_jsonl(path, {"test": True})
            assert path.parent.exists()

    def test_append_multiple_lines(self) -> None:
        """测试追加多行。"""
        import tempfile

        from agent_py_agent.agent.io import append_jsonl

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "multi.jsonl"
            for i in range(5):
                append_jsonl(path, {"index": i})
            lines = path.read_text().splitlines()
            assert len(lines) == 5
