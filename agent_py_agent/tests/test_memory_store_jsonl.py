"""记忆存储测试 - memory_store/jsonl.py 记忆存储、追加写入、查询。"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.memory_store.jsonl import JsonlMemory, MemoryRecord


@pytest.fixture
def temp_memory_path(tmp_path: Path) -> Path:
    """创建临时记忆文件路径。"""
    return tmp_path / "memory" / "test_memory.jsonl"


@pytest.fixture
def memory(temp_memory_path: Path) -> JsonlMemory:
    """创建 JsonlMemory 实例。"""
    return JsonlMemory(temp_memory_path)


class TestMemoryRecord:
    """MemoryRecord 数据类测试。"""

    def test_record_required_fields(self):
        """验证必需字段。"""
        record = MemoryRecord(role="user", content="你好")
        assert record.role == "user"
        assert record.content == "你好"

    def test_record_defaults(self):
        """验证默认值。"""
        record = MemoryRecord(role="user", content="hi")
        assert record.kind == "dialogue"
        assert record.tags is None
        assert record.created_at == 0.0

    def test_record_to_json(self):
        """验证转 JSON 字符串。"""
        record = MemoryRecord(role="assistant", content="回复", kind="response")
        json_str = record.to_json()
        parsed = json.loads(json_str)
        assert parsed["role"] == "assistant"
        assert parsed["content"] == "回复"
        assert parsed["kind"] == "response"

    def test_record_to_json_sets_timestamp(self):
        """验证 to_json 自动设置时间戳。"""
        record = MemoryRecord(role="user", content="hi")
        assert record.created_at == 0.0
        record.to_json()
        assert record.created_at > 0

    def test_record_to_json_with_tags(self):
        """验证带标签的记录。"""
        record = MemoryRecord(role="user", content="hi", tags=["greeting", "test"])
        json_str = record.to_json()
        parsed = json.loads(json_str)
        assert parsed["tags"] == ["greeting", "test"]


class TestJsonlMemoryInit:
    """JsonlMemory 初始化测试。"""

    def test_init_creates_parent_dir(self, temp_memory_path: Path):
        """验证初始化创建父目录。"""
        JsonlMemory(temp_memory_path)
        assert temp_memory_path.parent.exists()

    def test_init_with_local_store(self, temp_memory_path: Path):
        """验证带 LocalStore 初始化。"""
        mock_store = MagicMock()
        mem = JsonlMemory(temp_memory_path, local_store=mock_store)
        assert mem.local_store is mock_store


class TestJsonlMemoryAdd:
    """JsonlMemory.add 方法测试。"""

    def test_add_returns_record(self, memory: JsonlMemory):
        """验证添加返回记录。"""
        record = memory.add(role="user", content="测试内容")
        assert isinstance(record, MemoryRecord)
        assert record.role == "user"
        assert record.content == "测试内容"

    def test_add_creates_file(self, memory: JsonlMemory):
        """验证添加创建文件。"""
        memory.add(role="user", content="hello")
        assert memory.path.exists()

    def test_add_writes_valid_jsonl(self, memory: JsonlMemory):
        """验证写入有效 JSONL。"""
        memory.add(role="user", content="line 1")
        memory.add(role="assistant", content="line 2")
        lines = memory.path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        for line in lines:
            parsed = json.loads(line)
            assert isinstance(parsed, dict)

    def test_add_with_kind(self, memory: JsonlMemory):
        """验证添加带类型的记忆。"""
        memory.add(role="system", content="系统提示", kind="system")
        lines = memory.path.read_text(encoding="utf-8").splitlines()
        parsed = json.loads(lines[0])
        assert parsed["kind"] == "system"

    def test_add_with_tags(self, memory: JsonlMemory):
        """验证添加带标签的记忆。"""
        memory.add(role="user", content="tagged", tags=["important", "work"])
        lines = memory.path.read_text(encoding="utf-8").splitlines()
        parsed = json.loads(lines[0])
        assert "important" in parsed["tags"]

    def test_add_sets_timestamp(self, memory: JsonlMemory):
        """验证添加设置时间戳。"""
        before = time.time()
        record = memory.add(role="user", content="hi")
        after = time.time()
        assert before <= record.created_at <= after


class TestJsonlMemoryAll:
    """JsonlMemory.all 方法测试。"""

    def test_all_empty_file(self, memory: JsonlMemory):
        """空文件返回空列表。"""
        memory.path.parent.mkdir(parents=True, exist_ok=True)
        memory.path.touch()
        assert memory.all() == []

    def test_all_nonexistent_file(self, memory: JsonlMemory):
        """不存在的文件返回空列表。"""
        assert memory.all() == []

    def test_all_returns_records(self, memory: JsonlMemory):
        """验证返回所有记录。"""
        memory.add(role="user", content="first")
        memory.add(role="assistant", content="second")
        records = memory.all()
        assert len(records) == 2
        assert records[0].content == "first"
        assert records[1].content == "second"

    def test_all_skips_empty_lines(self, memory: JsonlMemory):
        """跳过空行。"""
        memory.path.write_text("\n\n\n", encoding="utf-8")
        assert memory.all() == []


class TestJsonlMemorySearch:
    """JsonlMemory.search 方法测试。"""

    def test_search_no_results(self, memory: JsonlMemory):
        """无结果时返回空列表。"""
        memory.add(role="user", content="python code")
        results = memory.search("java")
        assert results == []

    def test_search_finds_match(self, memory: JsonlMemory):
        """找到匹配结果。"""
        memory.add(role="user", content="Python 编程语言")
        results = memory.search("Python")
        assert len(results) >= 1
        assert "Python" in results[0].content

    def test_search_respects_top_k(self, memory: JsonlMemory):
        """验证 top_k 限制。"""
        for i in range(10):
            memory.add(role="user", content=f"内容 {i} Python")
        results = memory.search("Python", top_k=3)
        assert len(results) <= 3

    def test_search_empty_query(self, memory: JsonlMemory):
        """空查询返回时间排序结果。"""
        time.sleep(0.01)
        memory.add(role="user", content="first")
        memory.add(role="user", content="second")
        results = memory.search("")
        assert len(results) == 2


class TestJsonlMemoryIndexAll:
    """JsonlMemory.index_all 方法测试。"""

    def test_index_all_no_local_store(self, memory: JsonlMemory):
        """无 LocalStore 时返回 0。"""
        memory.add(role="user", content="test")
        count = memory.index_all()
        assert count == 0

    def test_index_all_with_local_store(self, memory: JsonlMemory):
        """有 LocalStore 时索引记录。"""
        mock_store = MagicMock()
        memory = JsonlMemory(memory.path, local_store=mock_store)
        memory.add(role="user", content="test1")
        memory.add(role="assistant", content="test2")

        # 重置 mock 以只统计 index_all 调用
        mock_store.reset_mock()
        count = memory.index_all()
        assert count == 2
        assert mock_store.upsert_record.call_count == 2


class TestJsonlMemoryIntegration:
    """集成测试。"""

    def test_full_workflow(self, memory: JsonlMemory):
        """完整工作流：添加、列出、搜索。"""
        # 添加记忆
        memory.add(role="user", content="今天天气很好", tags=["日常"])
        memory.add(role="assistant", content="是啊，适合出去走走")
        memory.add(role="user", content="想去爬山", tags=["计划"])

        # 验证添加
        all_records = memory.all()
        assert len(all_records) == 3

        # 搜索
        results = memory.search("天气")
        assert len(results) >= 1

        # 验证数据完整性
        for record in all_records:
            parsed = json.loads(record.to_json())
            assert "role" in parsed
            assert "content" in parsed
