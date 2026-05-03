"""文件IO模块测试 - io/jsonl.py JSONL读写、文件锁、并发安全。"""
from __future__ import annotations

import json
import tempfile
import threading
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_py_agent.agent.io.jsonl import append_jsonl, append_line_locked, _LOCKS, _LOCKS_GUARD


@pytest.fixture
def temp_jsonl(tmp_path: Path) -> Path:
    """创建临时 JSONL 文件路径。"""
    return tmp_path / "test.jsonl"


class TestAppendJsonl:
    """append_jsonl 函数测试。"""

    def test_append_jsonl_creates_file(self, temp_jsonl: Path):
        """验证创建 JSONL 文件。"""
        append_jsonl(temp_jsonl, {"key": "value"})
        assert temp_jsonl.exists()

    def test_append_jsonl_single_record(self, temp_jsonl: Path):
        """验证追加单条记录。"""
        append_jsonl(temp_jsonl, {"id": 1, "data": "test"})
        lines = temp_jsonl.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["id"] == 1

    def test_append_jsonl_multiple_records(self, temp_jsonl: Path):
        """验证追加多条记录。"""
        for i in range(5):
            append_jsonl(temp_jsonl, {"index": i})
        lines = temp_jsonl.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 5

    def test_append_jsonl_creates_parent_dirs(self, temp_jsonl: Path):
        """验证自动创建父目录。"""
        nested = temp_jsonl.parent / "nested" / "deep" / "file.jsonl"
        append_jsonl(nested, {"data": 1})
        assert nested.exists()

    def test_append_jsonl_with_sort_keys(self, temp_jsonl: Path):
        """验证 sort_keys 参数。"""
        append_jsonl(temp_jsonl, {"z": 1, "a": 2}, sort_keys=True)
        content = temp_jsonl.read_text(encoding="utf-8")
        a_pos = content.find('"a"')
        z_pos = content.find('"z"')
        assert a_pos < z_pos

    def test_append_jsonl_creates_valid_jsonl(self, temp_jsonl: Path):
        """验证生成的 JSONL 格式正确（每行都是有效 JSON）。"""
        for i in range(3):
            append_jsonl(temp_jsonl, {"num": i, "str": f"val{i}"})
        lines = temp_jsonl.read_text(encoding="utf-8").splitlines()
        for line in lines:
            parsed = json.loads(line)
            assert isinstance(parsed, dict)


class TestAppendLineLocked:
    """append_line_locked 函数测试。"""

    def test_append_line_creates_file(self, temp_jsonl: Path):
        """验证创建文件。"""
        append_line_locked(temp_jsonl, "plain text line")
        assert temp_jsonl.exists()

    def test_append_line_single(self, temp_jsonl: Path):
        """追加单行。"""
        append_line_locked(temp_jsonl, "line content")
        content = temp_jsonl.read_text(encoding="utf-8")
        assert content == "line content\n"

    def test_append_line_removes_existing_newline(self, temp_jsonl: Path):
        """追加时去除原有换行符。"""
        append_line_locked(temp_jsonl, "line\n")
        content = temp_jsonl.read_text(encoding="utf-8")
        assert not content.endswith("\n\n")
        assert content == "line\n"

    def test_append_line_multiple_sequential(self, temp_jsonl: Path):
        """顺序追加多行。"""
        for i in range(3):
            append_line_locked(temp_jsonl, f"line{i}")
        content = temp_jsonl.read_text(encoding="utf-8")
        assert content == "line0\nline1\nline2\n"

    def test_append_line_concurrent(self, temp_jsonl: Path):
        """验证并发追加安全性。"""
        num_threads = 10
        lines_per_thread = 50

        def writer(thread_id: int):
            for i in range(lines_per_thread):
                append_line_locked(temp_jsonl, f"t{thread_id}-l{i}")

        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(writer, i) for i in range(num_threads)]
            for f in futures:
                f.result()

        lines = temp_jsonl.read_text(encoding="utf-8").splitlines()
        expected = num_threads * lines_per_thread
        assert len(lines) == expected

    def test_append_line_empty_content(self, temp_jsonl: Path):
        """追加空行。"""
        append_line_locked(temp_jsonl, "")
        content = temp_jsonl.read_text(encoding="utf-8")
        assert content == "\n"


class TestConcurrentSafety:
    """并发安全测试。"""

    def test_concurrent_jsonl_writes(self, temp_jsonl: Path):
        """并发写入 JSONL 的数据完整性。"""
        num_threads = 5
        records_per_thread = 20

        def jsonl_writer(thread_id: int):
            for i in range(records_per_thread):
                append_jsonl(temp_jsonl, {"thread": thread_id, "seq": i})

        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(jsonl_writer, i) for i in range(num_threads)]
            for f in futures:
                f.result()

        # 验证所有记录都能被正确解析
        lines = temp_jsonl.read_text(encoding="utf-8").splitlines()
        assert len(lines) == num_threads * records_per_thread

        parsed_lines = 0
        for line in lines:
            obj = json.loads(line)
            assert "thread" in obj
            assert "seq" in obj
            parsed_lines += 1
        assert parsed_lines == num_threads * records_per_thread

    def test_lock_per_file_isolation(self, tmp_path: Path):
        """验证不同文件的锁是隔离的。"""
        file1 = tmp_path / "file1.jsonl"
        file2 = tmp_path / "file2.jsonl"

        # 同时向两个文件写入
        append_line_locked(file1, "file1 content")
        append_line_locked(file2, "file2 content")

        assert file1.read_text() == "file1 content\n"
        assert file2.read_text() == "file2 content\n"


class TestJsonlContent:
    """JSONL 内容验证测试。"""

    def test_jsonl_read_back(self, temp_jsonl: Path):
        """验证写入后能正确读回。"""
        original_data = [
            {"id": 1, "name": "Alice"},
            {"id": 2, "name": "Bob"},
            {"id": 3, "name": "Charlie"},
        ]
        for item in original_data:
            append_jsonl(temp_jsonl, item)

        # 读回并验证
        lines = temp_jsonl.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3
        for i, line in enumerate(lines):
            parsed = json.loads(line)
            assert parsed["id"] == original_data[i]["id"]
            assert parsed["name"] == original_data[i]["name"]

    def test_jsonl_with_unicode(self, temp_jsonl: Path):
        """验证 Unicode 内容。"""
        unicode_data = {"text": "你好世界 🌍", "emoji": "🎉"}
        append_jsonl(temp_jsonl, unicode_data)
        lines = temp_jsonl.read_text(encoding="utf-8").splitlines()
        parsed = json.loads(lines[0])
        assert parsed["text"] == "你好世界 🌍"

    def test_jsonl_with_nested_structure(self, temp_jsonl: Path):
        """验证嵌套结构。"""
        nested = {
            "outer": {"inner": {"deep": "value"}},
            "list": [1, 2, {"nested": "item"}],
        }
        append_jsonl(temp_jsonl, nested)
        lines = temp_jsonl.read_text(encoding="utf-8").splitlines()
        parsed = json.loads(lines[0])
        assert parsed["outer"]["inner"]["deep"] == "value"
        assert parsed["list"][2]["nested"] == "item"
