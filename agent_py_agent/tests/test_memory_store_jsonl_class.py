from __future__ import annotations

"""LLM: tests for memory_store jsonl module.

给人看的解释：
测试 JsonlMemory 类的记忆存储、追加写入、查询过滤功能。
"""

import json
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.memory_store.jsonl import JsonlMemory, MemoryRecord


class TestMemoryRecord:
    """测试 MemoryRecord 数据类。"""

    def test_create_record(self) -> None:
        """测试创建记忆记录。"""
        record = MemoryRecord(
            role="user",
            content="测试内容",
            kind="dialogue",
            tags=["tag1"],
            created_at=time.time(),
        )
        assert record.role == "user"
        assert record.content == "测试内容"
        assert record.kind == "dialogue"

    def test_record_default_kind(self) -> None:
        """测试默认 kind 为 dialogue。"""
        record = MemoryRecord(role="user", content="test")
        assert record.kind == "dialogue"

    def test_record_default_tags(self) -> None:
        """测试默认 tags 为 None。"""
        record = MemoryRecord(role="user", content="test")
        assert record.tags is None

    def test_record_to_json(self) -> None:
        """测试记录转 JSON。"""
        record = MemoryRecord(
            role="user",
            content="test content",
            kind="note",
            tags=["important"],
            created_at=1234567890.0,
        )
        json_str = record.to_json()
        assert '"role"' in json_str or '"content"' in json_str

    def test_record_to_json_sets_created_at(self) -> None:
        """测试 to_json 自动设置 created_at。"""
        record = MemoryRecord(role="user", content="test", created_at=0.0)
        record.to_json()
        assert record.created_at > 0

    def test_record_to_json_no_newline(self) -> None:
        """测试 JSON 字符串不包含换行符。"""
        record = MemoryRecord(role="user", content="multi\nline\ncontent")
        json_str = record.to_json()
        assert "\n" not in json_str

    def test_record_with_empty_content(self) -> None:
        """测试空内容。"""
        record = MemoryRecord(role="user", content="")
        assert record.content == ""

    def test_record_with_special_chars(self) -> None:
        """测试特殊字符。"""
        record = MemoryRecord(role="user", content='特殊字符 <>&"\\')
        json_str = record.to_json()
        parsed = json.loads(json_str)
        assert parsed["content"] == '特殊字符 <>&"\\'


class TestJsonlMemoryInit:
    """测试 JsonlMemory 初始化。"""

    def test_init_creates_path(self) -> None:
        """测试初始化创建路径。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "memory.jsonl"
            memory = JsonlMemory(path)
            assert memory.path == path

    def test_init_creates_parent_dir(self) -> None:
        """测试初始化创建父目录。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "subdir" / "memory.jsonl"
            JsonlMemory(path)
            assert path.parent.exists()

    def test_init_with_local_store(self) -> None:
        """测试带 local_store 初始化。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "memory.jsonl"
            mock_store = MagicMock()
            memory = JsonlMemory(path, local_store=mock_store)
            assert memory.local_store is mock_store

    def test_init_without_local_store(self) -> None:
        """测试不带 local_store 初始化。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "memory.jsonl"
            memory = JsonlMemory(path)
            assert memory.local_store is None


class TestJsonlMemoryAdd:
    """测试 JsonlMemory 添加记忆。"""

    @pytest.fixture
    def memory_path(self) -> Path:
        """创建临时记忆文件路径。"""
        tmpdir = tempfile.mkdtemp()
        return Path(tmpdir) / "memory.jsonl"

    def test_add_returns_record(self, memory_path: Path) -> None:
        """测试 add 返回记录。"""
        memory = JsonlMemory(memory_path)
        record = memory.add("user", "测试记忆")
        assert isinstance(record, MemoryRecord)
        assert record.content == "测试记忆"

    def test_add_writes_to_jsonl(self, memory_path: Path) -> None:
        """测试 add 写入 JSONL 文件。"""
        memory = JsonlMemory(memory_path)
        memory.add("user", "写入测试")
        assert memory_path.exists()
        content = memory_path.read_text(encoding="utf-8")
        assert "写入测试" in content

    def test_add_multiple_records(self, memory_path: Path) -> None:
        """测试添加多条记录。"""
        memory = JsonlMemory(memory_path)
        memory.add("user", "第一条")
        memory.add("assistant", "第二条")
        memory.add("user", "第三条")
        records = memory.all()
        assert len(records) == 3

    def test_add_with_kind(self, memory_path: Path) -> None:
        """测试添加带 kind 的记录。"""
        memory = JsonlMemory(memory_path)
        record = memory.add("user", "规则记忆", kind="rule")
        assert record.kind == "rule"

    def test_add_with_tags(self, memory_path: Path) -> None:
        """测试添加带 tags 的记录。"""
        memory = JsonlMemory(memory_path)
        record = memory.add("user", "带标签的记忆", tags=["重要", "工作"])
        assert record.tags == ["重要", "工作"]

    def test_add_empty_tags_becomes_empty_list(self, memory_path: Path) -> None:
        """测试空 tags 变成空列表。"""
        memory = JsonlMemory(memory_path)
        record = memory.add("user", "test", tags=None)
        assert record.tags == []


class TestJsonlMemoryAll:
    """测试 JsonlMemory 读取所有记录。"""

    @pytest.fixture
    def memory_path(self) -> Path:
        """创建临时记忆文件路径。"""
        tmpdir = tempfile.mkdtemp()
        return Path(tmpdir) / "memory.jsonl"

    def test_all_empty_file(self, memory_path: Path) -> None:
        """测试空文件返回空列表。"""
        memory = JsonlMemory(memory_path)
        assert memory.all() == []

    def test_all_reads_records(self, memory_path: Path) -> None:
        """测试读取记录。"""
        memory = JsonlMemory(memory_path)
        memory.add("user", "first")
        memory.add("assistant", "second")
        records = memory.all()
        assert len(records) == 2

    def test_all_preserves_order(self, memory_path: Path) -> None:
        """测试保持顺序。"""
        memory = JsonlMemory(memory_path)
        contents = ["first", "second", "third"]
        for c in contents:
            memory.add("user", c)
        records = memory.all()
        assert [r.content for r in records] == contents

    def test_all_skips_empty_lines(self, memory_path: Path) -> None:
        """测试跳过空行。"""
        memory = JsonlMemory(memory_path)
        # 先写入两条有效记录
        memory.add("user", "first")
        memory.add("user", "second")
        # 再追加空行文件
        with open(memory_path, "a") as f:
            f.write("\n")
        records = memory.all()
        assert len(records) == 2


class TestJsonlMemorySearch:
    """测试 JsonlMemory 搜索功能。"""

    @pytest.fixture
    def memory_path(self) -> Path:
        """创建临时记忆文件路径。"""
        tmpdir = tempfile.mkdtemp()
        return Path(tmpdir) / "memory.jsonl"

    def test_search_empty_query(self, memory_path: Path) -> None:
        """测试空查询返回最近记录。"""
        memory = JsonlMemory(memory_path)
        memory.add("user", "first")
        memory.add("assistant", "second")
        results = memory.search("", top_k=1)
        assert len(results) == 1

    def test_search_finds_matches(self, memory_path: Path) -> None:
        """测试搜索找到匹配。"""
        memory = JsonlMemory(memory_path)
        memory.add("user", "苹果和香蕉")
        memory.add("user", "今天天气好")
        results = memory.search("苹果", top_k=5)
        assert len(results) >= 1
        assert "苹果" in results[0].content

    def test_search_top_k_limit(self, memory_path: Path) -> None:
        """测试 top_k 限制。"""
        memory = JsonlMemory(memory_path)
        for i in range(10):
            memory.add("user", f"内容{i} 苹果")
        results = memory.search("苹果", top_k=3)
        assert len(results) == 3

    def test_search_with_local_store_fallback(self, memory_path: Path) -> None:
        """测试有 local_store 时的降级。"""
        memory = JsonlMemory(memory_path, local_store=MagicMock())
        memory.add("user", "test content")
        # local_store 搜索返回空时应该降级到 JSONL
        memory.local_store.search.return_value = []
        results = memory.search("test")
        assert len(results) >= 1

    def test_search_scores_by_relevance(self, memory_path: Path) -> None:
        """测试按相关性评分。"""
        memory = JsonlMemory(memory_path)
        memory.add("user", "苹果是水果")
        memory.add("user", "苹果手机")
        results = memory.search("苹果")
        # 精确匹配应该分数更高
        assert results[0].content == "苹果手机"


class TestJsonlMemoryIndexAll:
    """测试 JsonlMemory 索引功能。"""

    @pytest.fixture
    def memory_path(self) -> Path:
        """创建临时记忆文件路径。"""
        tmpdir = tempfile.mkdtemp()
        return Path(tmpdir) / "memory.jsonl"

    def test_index_all_no_local_store(self, memory_path: Path) -> None:
        """测试没有 local_store 时返回 0。"""
        memory = JsonlMemory(memory_path)
        count = memory.index_all()
        assert count == 0

    def test_index_all_with_local_store(self, memory_path: Path) -> None:
        """测试有 local_store 时索引记录。"""
        mock_store = MagicMock()
        memory = JsonlMemory(memory_path, local_store=mock_store)
        memory.add("user", "test1")
        memory.add("user", "test2")
        count = memory.index_all()
        assert count == 2

    def test_index_all_only_counts_valid(self, memory_path: Path) -> None:
        """测试 index_all 只计数有效记录。"""
        mock_store = MagicMock()
        memory = JsonlMemory(memory_path, local_store=mock_store)
        memory.add("user", "valid record")
        count = memory.index_all()
        assert count == 1


class TestJsonlMemorySourceId:
    """测试 source_id 生成。"""

    @pytest.fixture
    def memory(self) -> JsonlMemory:
        """创建测试用 JsonlMemory。"""
        tmpdir = tempfile.mkdtemp()
        return JsonlMemory(Path(tmpdir) / "memory.jsonl")

    def test_source_id_format(self, memory: JsonlMemory) -> None:
        """测试 source_id 格式。"""
        record = MemoryRecord(role="user", content="test", kind="note", created_at=1234567890.0)
        source_id = memory._source_id(record)
        assert ":" in source_id
        parts = source_id.split(":")
        assert len(parts) == 4
        assert parts[0] == "1234567890.000000"
        assert parts[1] == "user"
        assert parts[2] == "note"

    def test_source_id_stable(self, memory: JsonlMemory) -> None:
        """测试 source_id 稳定性。"""
        record = MemoryRecord(role="user", content="stable", kind="note", created_at=1234567890.0)
        id1 = memory._source_id(record)
        id2 = memory._source_id(record)
        assert id1 == id2
