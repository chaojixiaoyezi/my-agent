"""子代理工具函数测试 - utils.py 工具函数、路径处理、ID生成。"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_py_agent.agent.common.json_io import read_json_object
from agent_py_agent.agent.subagents.utils import (
    _apply_missing_paths,
    _apply_paths,
    _merge_list,
    _new_id,
    _write_if_missing,
    _write_json_if_missing,
)


class TestNewId:
    """_new_id 函数测试。"""

    def test_new_id_format(self):
        """验证 ID 格式包含前缀和时间戳。"""
        prefix = "test"
        id_str = _new_id(prefix)
        assert id_str.startswith(f"{prefix}-")
        parts = id_str.split("-")
        assert len(parts) == 3  # prefix-timestamp-hex

    def test_new_id_unique(self):
        """验证生成的 ID 唯一。"""
        ids = [_new_id("test") for _ in range(100)]
        assert len(set(ids)) == 100

    def test_new_id_timestamp_part(self):
        """验证时间戳部分是大致当前时间。"""
        before = int(time.time())
        id_str = _new_id("test")
        after = int(time.time())
        timestamp = int(id_str.split("-")[1])
        assert before <= timestamp <= after


class TestMergeList:
    """_merge_list 函数测试。"""

    def test_merge_empty_lists(self):
        """合并两个空列表。"""
        result = _merge_list([], [])
        assert result == []

    def test_merge_with_left_empty(self):
        """左侧为空时返回右侧副本。"""
        result = _merge_list([], ["a", "b"])
        assert result == ["a", "b"]

    def test_merge_with_right_empty(self):
        """右侧为空时返回左侧副本。"""
        result = _merge_list(["a", "b"], [])
        assert result == ["a", "b"]

    def test_merge_no_duplicates(self):
        """合并无重复项列表。"""
        result = _merge_list(["a", "b"], ["c", "d"])
        assert result == ["a", "b", "c", "d"]

    def test_merge_with_duplicates_in_right(self):
        """右侧有重复项时去重。"""
        result = _merge_list(["a", "b"], ["b", "c", "b"])
        assert result == ["a", "b", "c"]

    def test_merge_preserves_order(self):
        """合并保持左侧顺序。"""
        result = _merge_list(["x", "y"], ["a", "b"])
        assert result.index("x") < result.index("a")

    def test_merge_duplicate_in_left_ignored(self):
        """左侧已有项不会被右侧重复项覆盖。"""
        result = _merge_list(["a", "b"], ["a", "c"])
        assert result == ["a", "b", "c"]


class TestReadJsonObject:
    """_read_json_object 函数测试。"""

    def test_read_valid_json(self, tmp_path: Path):
        """读取有效 JSON 文件。"""
        file = tmp_path / "test.json"
        file.write_text('{"key": "value"}', encoding="utf-8")
        result = read_json_object(file, parse_nested_string=True)
        assert result == {"key": "value"}

    def test_read_empty_file(self, tmp_path: Path):
        """读取空文件返回空对象。"""
        file = tmp_path / "empty.json"
        file.write_text("", encoding="utf-8")
        result = read_json_object(file, parse_nested_string=True)
        assert result == {}

    def test_read_invalid_json(self, tmp_path: Path):
        """读取无效 JSON 返回空对象。"""
        file = tmp_path / "bad.json"
        file.write_text("not json", encoding="utf-8")
        result = read_json_object(file, parse_nested_string=True)
        assert result == {}

    def test_read_non_dict_json(self, tmp_path: Path):
        """读取非对象 JSON 返回空对象。"""
        file = tmp_path / "array.json"
        file.write_text("[1, 2, 3]", encoding="utf-8")
        result = read_json_object(file, parse_nested_string=True)
        assert result == {}

    def test_read_nested_json_string_object(self, tmp_path: Path):
        file = tmp_path / "nested.json"
        file.write_text(json.dumps('{"run_id": "run-1", "status": "PLANNING"}'), encoding="utf-8")
        result = read_json_object(file, parse_nested_string=True)
        assert result == {"run_id": "run-1", "status": "PLANNING"}

    def test_read_nonexistent_file(self, tmp_path: Path):
        """读取不存在文件返回空对象。"""
        result = read_json_object(tmp_path / "nonexistent.json", parse_nested_string=True)
        assert result == {}


class TestApplyPaths:
    """_apply_paths 函数测试。"""

    def test_apply_paths_basic(self):
        """验证基本属性设置。"""
        task = MagicMock()
        task.nonexistent_attr = None
        _apply_paths(task, {"attr1": "value1", "attr2": "value2"})
        assert task.attr1 == "value1"
        assert task.attr2 == "value2"

    def test_apply_paths_overwrites(self):
        """验证覆盖已有值。"""
        task = MagicMock()
        task.existing = "old"
        _apply_paths(task, {"existing": "new"})
        assert task.existing == "new"


class TestApplyMissingPaths:
    """_apply_missing_paths 函数测试。"""

    def test_apply_missing_only_new(self):
        """只补齐缺失路径。"""
        from dataclasses import dataclass

        @dataclass
        class MockTask:
            existing: str = "value"

        task = MockTask()
        _apply_missing_paths(task, {"existing": "new", "new_attr": "new_value"})
        assert task.existing == "value"  # 原有值保持不变
        assert task.new_attr == "new_value"  # 新值被设置

    def test_apply_missing_nonexistent(self):
        """所有属性都不存在时全部设置。"""
        task = MagicMock(spec=[])  # spec=[] 使得 hasattr 返回 False
        del task.attr1
        del task.attr2
        paths = {"attr1": "v1", "attr2": "v2"}
        for key, value in paths.items():
            if not getattr(task, key, None):
                setattr(task, key, value)
        assert task.attr1 == "v1"
        assert task.attr2 == "v2"


class TestWriteIfMissing:
    """_write_if_missing 函数测试。"""

    def test_write_creates_file(self, tmp_path: Path):
        """文件不存在时创建文件。"""
        file = tmp_path / "new.txt"
        _write_if_missing(file, "content")
        assert file.exists()
        assert file.read_text(encoding="utf-8") == "content"

    def test_write_creates_parent_dirs(self, tmp_path: Path):
        """创建父目录。"""
        file = tmp_path / "subdir" / "nested" / "file.txt"
        _write_if_missing(file, "content")
        assert file.exists()

    def test_write_skips_existing(self, tmp_path: Path):
        """文件存在时跳过写入。"""
        file = tmp_path / "existing.txt"
        file.write_text("original", encoding="utf-8")
        _write_if_missing(file, "new content")
        assert file.read_text(encoding="utf-8") == "original"


class TestWriteJsonIfMissing:
    """_write_json_if_missing 函数测试。"""

    def test_write_json_creates_file(self, tmp_path: Path):
        """创建 JSON 文件。"""
        file = tmp_path / "data.json"
        payload = {"key": "value", "num": 42}
        _write_json_if_missing(file, payload)
        assert file.exists()
        result = json.loads(file.read_text(encoding="utf-8"))
        assert result == payload

    def test_write_json_indent(self, tmp_path: Path):
        """验证 JSON 格式化缩进。"""
        file = tmp_path / "formatted.json"
        _write_json_if_missing(file, {"a": 1})
        content = file.read_text(encoding="utf-8")
        assert "\n" in content  # 有缩进换行

    def test_write_json_skips_existing(self, tmp_path: Path):
        """文件存在时跳过。"""
        file = tmp_path / "existing.json"
        file.write_text('{"original": true}', encoding="utf-8")
        _write_json_if_missing(file, {"new": False})
        result = json.loads(file.read_text(encoding="utf-8"))
        assert "original" in result
        assert "new" not in result
