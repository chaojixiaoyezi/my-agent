"""记忆存储数据模型测试 - memory_store/models.py 记忆数据模型、类型定义。"""
from __future__ import annotations

import time

from agent_py_agent.agent.memory_store.jsonl import MemoryRecord


class TestMemoryRecordModel:
    """MemoryRecord 模型测试。"""

    def test_record_role_field(self):
        """验证 role 字段。"""
        record = MemoryRecord(role="user", content="你好")
        assert record.role == "user"

    def test_record_content_field(self):
        """验证 content 字段。"""
        record = MemoryRecord(role="user", content="测试内容")
        assert record.content == "测试内容"

    def test_record_kind_default(self):
        """验证 kind 默认值。"""
        record = MemoryRecord(role="user", content="hi")
        assert record.kind == "dialogue"

    def test_record_kind_custom(self):
        """验证自定义 kind。"""
        record = MemoryRecord(role="system", content="系统提示", kind="system")
        assert record.kind == "system"

    def test_record_tags_default_none(self):
        """验证 tags 默认 None。"""
        record = MemoryRecord(role="user", content="hi")
        assert record.tags is None

    def test_record_tags_custom_list(self):
        """验证自定义 tags 列表。"""
        record = MemoryRecord(role="user", content="hi", tags=["greeting", "test"])
        assert record.tags == ["greeting", "test"]

    def test_record_created_at_default_zero(self):
        """验证 created_at 默认 0。"""
        record = MemoryRecord(role="user", content="hi")
        assert record.created_at == 0.0

    def test_record_created_at_custom(self):
        """验证自定义 created_at。"""
        timestamp = 1234567890.0
        record = MemoryRecord(role="user", content="hi", created_at=timestamp)
        assert record.created_at == timestamp

    def test_record_to_json_basic(self):
        """验证基本 to_json 输出。"""
        record = MemoryRecord(role="assistant", content="回复内容")
        json_str = record.to_json()
        parsed = json.loads(json_str) if 'json' in dir() else None

        import json
        parsed = json.loads(json_str)
        assert parsed["role"] == "assistant"
        assert parsed["content"] == "回复内容"
        assert parsed["kind"] == "dialogue"
        assert parsed["tags"] is None

    def test_record_to_json_with_all_fields(self):
        """验证完整字段 to_json 输出。"""
        import json
        record = MemoryRecord(
            role="user",
            content="完整记录",
            kind="reflection",
            tags=["important", "review"],
            created_at=1234567890.5,
        )
        json_str = record.to_json()
        parsed = json.loads(json_str)
        assert parsed["role"] == "user"
        assert parsed["content"] == "完整记录"
        assert parsed["kind"] == "reflection"
        assert parsed["tags"] == ["important", "review"]
        assert parsed["created_at"] == 1234567890.5

    def test_record_to_json_auto_timestamp(self):
        """验证 to_json 自动设置时间戳。"""
        record = MemoryRecord(role="user", content="hi")
        assert record.created_at == 0.0
        record.to_json()
        assert record.created_at > 0

    def test_record_to_json_timestamp_not_overwritten(self):
        """验证已设置的时间戳不被覆盖。"""
        import json
        original_time = 1000000.0
        record = MemoryRecord(role="user", content="hi", created_at=original_time)
        record.to_json()
        assert record.created_at == original_time


class TestMemoryRecordKinds:
    """MemoryRecord kind 类型测试。"""

    def test_kind_dialogue(self):
        """对话类型记忆。"""
        record = MemoryRecord(role="user", content="hello", kind="dialogue")
        assert record.kind == "dialogue"

    def test_kind_rule(self):
        """规则类型记忆。"""
        record = MemoryRecord(role="system", content="always check inputs", kind="rule")
        assert record.kind == "rule"

    def test_kind_summary(self):
        """摘要类型记忆。"""
        record = MemoryRecord(role="assistant", content="用户想要实现登录功能", kind="summary")
        assert record.kind == "summary"

    def test_kind_note(self):
        """笔记类型记忆。"""
        record = MemoryRecord(role="user", content="记得买牛奶", kind="note")
        assert record.kind == "note"

    def test_kind_arbitrary(self):
        """支持任意自定义类型。"""
        record = MemoryRecord(role="user", content="custom type", kind="custom_kind_123")
        assert record.kind == "custom_kind_123"


class TestMemoryRecordRoles:
    """MemoryRecord role 类型测试。"""

    def test_role_user(self):
        """用户角色。"""
        record = MemoryRecord(role="user", content="用户说的话")
        assert record.role == "user"

    def test_role_assistant(self):
        """助手角色。"""
        record = MemoryRecord(role="assistant", content="助手回复")
        assert record.role == "assistant"

    def test_role_system(self):
        """系统角色。"""
        record = MemoryRecord(role="system", content="系统提示词")
        assert record.role == "system"

    def test_role_tool(self):
        """工具角色。"""
        record = MemoryRecord(role="tool", content="工具执行结果")
        assert record.role == "tool"

    def test_role_arbitrary(self):
        """支持任意自定义角色。"""
        record = MemoryRecord(role="custom_agent", content="自定义代理内容")
        assert record.role == "custom_agent"


class TestMemoryRecordTags:
    """MemoryRecord tags 测试。"""

    def test_tags_empty_list(self):
        """空标签列表。"""
        record = MemoryRecord(role="user", content="hi", tags=[])
        assert record.tags == []

    def test_tags_single_item(self):
        """单个标签。"""
        record = MemoryRecord(role="user", content="hi", tags=["important"])
        assert record.tags == ["important"]

    def test_tags_multiple_items(self):
        """多个标签。"""
        record = MemoryRecord(role="user", content="hi", tags=["a", "b", "c"])
        assert len(record.tags) == 3

    def test_tags_unicode(self):
        """Unicode 标签。"""
        record = MemoryRecord(role="user", content="hi", tags=["中文标签", "emoji 🏷️"])
        assert "中文标签" in record.tags


class TestMemoryRecordJsonlRoundtrip:
    """JSONL 往返测试。"""

    def test_roundtrip_basic(self):
        """基本往返。"""
        import json
        original = MemoryRecord(role="user", content="hello world")
        json_str = original.to_json()
        parsed = json.loads(json_str)
        restored = MemoryRecord(**parsed)
        assert restored.role == original.role
        assert restored.content == original.content
        assert restored.kind == original.kind

    def test_roundtrip_full(self):
        """完整字段往返。"""
        import json
        original = MemoryRecord(
            role="assistant",
            content="这是完整测试",
            kind="test",
            tags=["roundtrip", "test"],
            created_at=1234567890.123,
        )
        json_str = original.to_json()
        parsed = json.loads(json_str)
        restored = MemoryRecord(**parsed)
        assert restored.role == original.role
        assert restored.content == original.content
        assert restored.kind == original.kind
        assert restored.tags == original.tags
        assert restored.created_at == original.created_at
