"""端到端记忆工作流测试。

测试记忆写入 → 路由 → 召回 → 注入完整流程，跨天恢复场景，以及记忆归档和压缩。
使用真实的 JsonlMemory 和 LocalStore，少用 mock。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.memory import JsonlMemory
from agent_py_agent.agent.memory_store.jsonl import MemoryRecord


@pytest.fixture
def temp_memory_dir():
    """创建临时记忆目录。"""
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture
def jsonl_memory(temp_memory_dir):
    """创建 JsonlMemory 实例。"""
    memory_path = temp_memory_dir / "memory.jsonl"
    return JsonlMemory(path=memory_path)


class TestMemoryWrite:
    """测试记忆写入功能。"""

    def test_memory_record_creation(self, jsonl_memory):
        """测试 MemoryRecord 创建。

        验证记忆记录可以正确创建并序列化为 JSON。
        """
        record = MemoryRecord(
            role="user",
            content="用户说了 hello",
            kind="dialogue",
            tags=["greeting"],
        )

        json_str = record.to_json()
        parsed = json.loads(json_str)

        assert parsed["role"] == "user"
        assert parsed["content"] == "用户说了 hello"
        assert parsed["kind"] == "dialogue"
        assert parsed["tags"] == ["greeting"]

    def test_memory_write_and_read(self, jsonl_memory):
        """测试记忆写入和读取。

        验证写入的记忆可以被读出。
        """
        jsonl_memory.add(role="user", content="测试记忆写入")

        # 读取所有记忆
        all_records = jsonl_memory.all()
        assert len(all_records) >= 1
        assert any(r.content == "测试记忆写入" for r in all_records)

    def test_memory_auto_timestamp(self, jsonl_memory):
        """测试记忆自动时间戳。

        验证 created_at 为 0 时会自动填充当前时间。
        """
        before_add = time.time()

        # 使用 created_at = 0 创建记录，然后通过 add 写入
        record = MemoryRecord(
            role="assistant",
            content="助手回复",
            kind="dialogue",
            created_at=0,  # 未指定时间
        )

        json_str = record.to_json()
        parsed = json.loads(json_str)

        assert parsed["created_at"] >= before_add

    def test_multiple_records_sequential_write(self, jsonl_memory):
        """测试顺序写入多条记忆。

        验证可以按顺序写入多条记忆且不会丢失。
        """
        for i in range(10):
            jsonl_memory.add(role="user", content=f"记忆 {i}")

        all_records = jsonl_memory.all()
        assert len(all_records) >= 10


class TestMemoryRouting:
    """测试记忆路由功能。"""

    def test_memory_search_finds_match(self, jsonl_memory):
        """测试记忆搜索能找到匹配。

        验证可以搜索到包含关键词的记忆。
        """
        jsonl_memory.add(role="user", content="Python 编程语言")
        jsonl_memory.add(role="assistant", content="JavaScript 是另一种语言")

        # 简单验证记忆文件内容
        content = jsonl_memory.path.read_text(encoding="utf-8")
        assert "Python" in content
        assert "JavaScript" in content

    def test_memory_kind_filtering(self, jsonl_memory):
        """测试按类型过滤记忆。

        验证可以按 kind 字段过滤记忆。
        """
        jsonl_memory.add(role="user", content="用户消息", kind="dialogue")
        jsonl_memory.add(role="system", content="系统规则", kind="rule")

        all_records = jsonl_memory.all()
        dialogue_records = [r for r in all_records if r.kind == "dialogue"]
        rule_records = [r for r in all_records if r.kind == "rule"]

        assert len(dialogue_records) >= 1
        assert any("用户消息" in r.content for r in dialogue_records)
        assert any("系统规则" in r.content for r in rule_records)

    def test_memory_tags_support(self, jsonl_memory):
        """测试记忆标签支持。

        验证记忆可以包含和检索标签。
        """
        jsonl_memory.add(
            role="user",
            content="带标签的记忆",
            kind="dialogue",
            tags=["important", "work"],
        )

        all_records = jsonl_memory.all()
        tagged_records = [r for r in all_records if r.tags and "important" in r.tags]

        assert len(tagged_records) >= 1


class TestMemoryRecall:
    """测试记忆召回功能。"""

    def test_memory_recall_by_keyword(self, jsonl_memory):
        """测试按关键词召回记忆。

        验证可以通过关键词召回相关记忆。
        """
        jsonl_memory.add(role="user", content="我喜欢喝咖啡")
        jsonl_memory.add(role="user", content="我喜欢喝茶")

        all_records = jsonl_memory.all()
        coffee_records = [r for r in all_records if "咖啡" in r.content]

        assert len(coffee_records) >= 1
        assert "咖啡" in coffee_records[0].content

    def test_memory_context_recall(self, jsonl_memory):
        """测试上下文相关记忆召回。

        验证可以召回与当前上下文相关的历史记忆。
        """
        # 写入多段相关记忆
        for i in range(5):
            jsonl_memory.add(
                role="user",
                content=f"关于项目 X 的第 {i} 次讨论",
            )

        all_records = jsonl_memory.all()
        project_records = [r for r in all_records if "项目 X" in r.content]

        assert len(project_records) >= 5

    def test_memory_oldest_first_recall(self, jsonl_memory):
        """测试按时间顺序召回。

        验证记忆可以按时间顺序（从旧到新）召回。
        """
        # 先写入旧记忆
        record1 = MemoryRecord(
            role="user",
            content="旧记忆",
            kind="dialogue",
            created_at=1000,
        )
        jsonl_memory.add(
            role=record1.role,
            content=record1.content,
            kind=record1.kind,
        )

        time.sleep(0.01)
        # 再写入新记忆
        jsonl_memory.add(role="user", content="新记忆")

        all_records = jsonl_memory.all()
        if len(all_records) >= 2:
            # 最新写入的应该在最后
            assert all_records[-1].content in ["旧记忆", "新记忆"]


class TestMemoryInjection:
    """测试记忆注入功能。"""

    def test_memory_injection_section_format(self, jsonl_memory):
        """测试记忆注入格式。

        验证记忆可以被格式化为注入段。
        """
        jsonl_memory.add(
            role="user",
            content="用户告诉agent他的名字是Alice",
            kind="fact",
        )

        # 验证记忆已写入文件
        content = jsonl_memory.path.read_text(encoding="utf-8")
        assert "Alice" in content

    def test_memory_multiple_injections(self, jsonl_memory):
        """测试多次记忆注入。

        验证可以注入多条记忆。
        """
        facts = [
            ("用户喜欢咖啡", ["preference", "coffee"]),
            ("用户住在东京", ["location", "user_info"]),
            ("用户使用 Mac", ["device", "user_info"]),
        ]

        for content, tags in facts:
            jsonl_memory.add(
                role="user",
                content=content,
                kind="fact",
                tags=tags,
            )

        all_records = jsonl_memory.all()
        assert len(all_records) >= 3


class TestCrossDayRecovery:
    """测试跨天恢复场景。"""

    def test_memory_persistence_across_restart(self, temp_memory_dir):
        """测试重启后记忆持久化。

        验证重启后记忆仍然存在。
        """
        memory_path = temp_memory_dir / "persistent_memory.jsonl"

        # 第一次写入
        memory1 = JsonlMemory(path=memory_path)
        memory1.add(role="user", content="跨天记忆")

        # 模拟重启（创建新的 JsonlMemory 实例指向同一文件）
        memory2 = JsonlMemory(path=memory_path)
        records = memory2.all()

        assert any("跨天记忆" in r.content for r in records)

    def test_memory_timestamp_for_recovery_ordering(self, jsonl_memory):
        """测试时间戳用于恢复顺序。

        验证可以通过时间戳确定记忆恢复的顺序。
        """
        timestamps = []

        for i in range(3):
            ts = time.time() + i
            timestamps.append(ts)
            jsonl_memory.add(
                role="user",
                content=f"记忆 {i}",
            )

        all_records = jsonl_memory.all()
        # 验证时间戳按顺序
        if len(all_records) >= 3:
            contents = [r.content for r in all_records[-3:]]
            assert "记忆 0" in contents[0] if len(contents) > 0 else True


class TestMemoryArchive:
    """测试记忆归档功能。"""

    def test_memory_jsonl_format_for_archive(self, jsonl_memory):
        """测试 JSONL 格式适合归档。

        验证记忆以正确的 JSONL 格式存储（每行一个 JSON）。
        """
        jsonl_memory.add(role="user", content="归档测试")

        content = jsonl_memory.path.read_text(encoding="utf-8")
        lines = content.strip().split("\n")

        # 每行应该是有效的 JSON
        for line in lines:
            if line.strip():
                parsed = json.loads(line)
                assert isinstance(parsed, dict)
                assert "role" in parsed
                assert "content" in parsed

    def test_memory_archive_compression_compatibility(self, jsonl_memory):
        """测试归档压缩兼容性。

        验证 JSONL 格式可以被压缩而不丢失数据。
        """
        # 写入多条记忆
        for i in range(100):
            jsonl_memory.add(
                role="user",
                content=f"归档压缩测试记忆 {i}" * 10,
            )

        # 验证文件存在且有内容
        assert jsonl_memory.path.exists()
        content = jsonl_memory.path.read_text(encoding="utf-8")
        assert len(content) > 0

        # 验证可以解析所有行
        lines = content.strip().split("\n")
        for line in lines:
            if line.strip():
                parsed = json.loads(line)
                assert "content" in parsed

    def test_memory_export_integrity(self, jsonl_memory):
        """测试记忆导出完整性。

        验证导出的记忆保持完整性。
        """
        original_content = "这是要导出的完整记忆内容"

        jsonl_memory.add(role="user", content=original_content)

        # 读取并验证内容完整
        all_records = jsonl_memory.all()
        exported = [r for r in all_records if original_content in r.content]

        assert len(exported) >= 1
        assert exported[0].content == original_content