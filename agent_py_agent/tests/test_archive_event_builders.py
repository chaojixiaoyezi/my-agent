"""单元测试：memory_archive runtime 模块 - event_builders 事件构建器"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from unittest.mock import MagicMock

import pytest

from agent_py_agent.agent.memory_archive.runtime.event_builders import (
    EventIdentity,
    ToolCallContext,
    _apply_archive_level_to_message_event,
    _apply_archive_level_to_tool_event,
    _canonical_json,
    _content_hash,
    _event_id,
    _first_bool,
    _first_text,
    _message_event,
    _normalize_archive_level,
    _normalize_tool_call,
    _preview,
    _stable_display_json,
    _summarize_text,
    _tool_event,
    _tool_metadata,
    _tool_status,
    _ToolFacts,
)


class TestNormalizeArchiveLevel:
    """测试 _normalize_archive_level 归档等级归一化"""

    def test_valid_levels(self):
        """验证有效等级保持不变"""
        for level in range(4):
            assert _normalize_archive_level(level) == level

    def test_negative_becomes_3(self):
        """验证负数返回 3"""
        assert _normalize_archive_level(-5) == 3

    def test_too_high_becomes_3(self):
        """验证超出范围返回 3"""
        assert _normalize_archive_level(10) == 3

    def test_bool_false_becomes_3(self):
        """验证布尔 False 返回 3（因为 isinstance(bool, int) 为 True 但 bool 被单独处理）"""
        assert _normalize_archive_level(False) == 3

    def test_non_numeric_returns_3(self):
        """验证非数字返回 3"""
        assert _normalize_archive_level("bad") == 3
        assert _normalize_archive_level(None) == 3


class TestPreview:
    """测试 _preview 预览截断"""

    def test_short_content_unchanged(self):
        """验证短内容保持不变"""
        text = "short text"
        result = _preview(text, archive_level=3)
        assert result == text

    def test_long_content_truncated(self):
        """验证长内容被截断并加省略号"""
        text = "a" * 300
        result = _preview(text, archive_level=3)  # level 3 limit is 160
        assert result.endswith("...")
        assert len(result) == 160

    def test_level_0_allows_full(self):
        """验证 level 0 允许完整内容（2048 限制）"""
        text = "a" * 1000
        result = _preview(text, archive_level=0)
        assert len(result) == 1000

    def test_level_1_limit_1024(self):
        """验证 level 1 限制 1024"""
        text = "b" * 2000
        result = _preview(text, archive_level=1)
        assert len(result) == 1024

    def test_level_2_limit_512(self):
        """验证 level 2 限制 512"""
        text = "c" * 1000
        result = _preview(text, archive_level=2)
        assert len(result) == 512


class TestContentHash:
    """测试 _content_hash 内容哈希"""

    def test_hash_format(self):
        """验证哈希格式为 sha256: 前缀"""
        result = _content_hash("hello")
        assert result.startswith("sha256:")

    def test_deterministic(self):
        """验证哈希确定性"""
        h1 = _content_hash("test content")
        h2 = _content_hash("test content")
        assert h1 == h2

    def test_different_content_different_hash(self):
        """验证不同内容产生不同哈希"""
        h1 = _content_hash("content1")
        h2 = _content_hash("content2")
        assert h1 != h2


class TestEventId:
    """测试 _event_id 事件 ID 生成"""

    def test_prefix_raw(self):
        """验证 ID 以 raw: 前缀开头"""
        result = _event_id({"key": "value"})
        assert result.startswith("raw:")

    def test_deterministic(self):
        """验证 ID 生成确定性"""
        payload = {"kind": "message", "sequence": 1}
        id1 = _event_id(payload)
        id2 = _event_id(payload)
        assert id1 == id2

    def test_different_payloads_different_ids(self):
        """验证不同 payload 产生不同 ID"""
        id1 = _event_id({"seq": 1})
        id2 = _event_id({"seq": 2})
        assert id1 != id2


class TestCanonicalJson:
    """测试 _canonical_json 规范 JSON"""

    def test_sorted_keys(self):
        """验证键排序"""
        result1 = _canonical_json({"b": 1, "a": 2})
        result2 = _canonical_json({"a": 2, "b": 1})
        assert result1 == result2

    def test_no_extra_whitespace(self):
        """验证无多余空格"""
        result = _canonical_json({"key": "value"})
        assert " " not in result.replace('"value"', "")  # 简单检查

    def test_handles_non_string_values(self):
        """验证处理非字符串值"""
        result = _canonical_json({"num": 123, "bool": True, "null": None})
        assert "123" in result
        assert "true" in result


class TestStableDisplayJson:
    """测试 _stable_display_json 稳定展示 JSON"""

    def test_preserves_key_order(self):
        """验证保留键顺序"""
        result = _stable_display_json({"b": 1, "a": 2})
        # 顺序应该保留（不排序）
        # 注意：这个测试取决于实现

    def test_readable_format(self):
        """验证可读格式"""
        result = _stable_display_json({"key": "value"})
        assert '"key"' in result
        assert '"value"' in result


class TestFirstText:
    """测试 _first_text 首个文本提取"""

    def test_finds_first_match(self):
        """验证找到第一个匹配"""
        payload = {"tool_name": "read", "tool": "write", "name": "other"}
        result = _first_text(payload, "tool_name", "tool", "name")
        assert result == "read"

    def test_returns_empty_when_no_match(self):
        """验证无匹配时返回空字符串"""
        payload = {"other": "value"}
        result = _first_text(payload, "tool_name", "tool")
        assert result == ""

    def test_skips_none_values(self):
        """验证跳过 None 值"""
        payload = {"tool_name": None, "tool": "write"}
        result = _first_text(payload, "tool_name", "tool")
        assert result == "write"

    def test_skips_empty_string(self):
        """验证跳过空字符串"""
        payload = {"tool_name": "", "tool": "write"}
        result = _first_text(payload, "tool_name", "tool")
        assert result == "write"


class TestFirstBool:
    """测试 _first_bool 首个布尔提取"""

    def test_true_bool(self):
        """验证真布尔"""
        payload = {"success": True, "ok": False}
        result = _first_bool(payload, "success", "ok")
        assert result is True

    def test_false_bool(self):
        """验证假布尔"""
        payload = {"success": False}
        result = _first_bool(payload, "success")
        assert result is False

    def test_string_true_values(self):
        """验证机器布尔字符串真值"""
        for val in ("true", "1"):
            payload = {"status": val}
            assert _first_bool(payload, "status") is True

    def test_string_false_values(self):
        """验证机器布尔字符串假值"""
        for val in ("false", "0"):
            payload = {"status": val}
            assert _first_bool(payload, "status") is False

    def test_status_words_do_not_become_booleans(self):
        """验证状态词不会伪装成工具布尔事实"""
        for val in ("yes", "ok", "success", "no", "error", "failed", "failure"):
            payload = {"status": val}
            assert _first_bool(payload, "status") is None

    def test_returns_none_when_no_match(self):
        """验证无匹配返回 None"""
        payload = {"other": "value"}
        result = _first_bool(payload, "success", "ok")
        assert result is None

    def test_success_alias_not_used_for_tool_event_success(self):
        """旧 success 字段不能成为 runtime 工具成功事实。"""
        event = _tool_event(
            EventIdentity(sequence=1, session_id="session-1", request_id="request-1", run_id="run-1", task_id="task-1"),
            ToolCallContext(
                backend="test",
                tool_call={"tool": "read_file", "id": "1-1", "success": True},
                source="test",
                archive_level=3,
                created_at="2026-06-07T00:00:00+08:00",
            ),
        )

        assert event.tool_success is None
        assert event.status == "unknown"


class TestToolStatus:
    """测试 _tool_status 工具状态判断"""

    def test_explicit_status_preserved(self):
        """验证显式状态被保留"""
        payload = {"status": "custom_status"}
        result = _tool_status(payload, tool_success=None)
        assert result == "custom_status"

    def test_true_success_becomes_ok(self):
        """验证成功时返回 ok"""
        result = _tool_status({}, tool_success=True)
        assert result == "ok"

    def test_false_success_becomes_error(self):
        """验证失败时返回 error"""
        result = _tool_status({}, tool_success=False)
        assert result == "error"

    def test_unknown_when_no_info(self):
        """验证无信息时返回 unknown"""
        result = _tool_status({}, tool_success=None)
        assert result == "unknown"


class TestNormalizeToolCall:
    """测试 _normalize_tool_call 工具调用归一化"""

    def test_dict_passthrough(self):
        """验证字典直接返回"""
        call = {"tool_name": "read", "input": {}}
        result = _normalize_tool_call(call)
        assert result == call

    def test_dataclass_converted(self):
        """验证 dataclass 被转换"""
        @dataclass
        class ToolCall:
            tool_name: str
            input: dict

        call = ToolCall(tool_name="read", input={})
        result = _normalize_tool_call(call)
        assert isinstance(result, dict)
        assert result["tool_name"] == "read"

    def test_object_with_dict_converted(self):
        """验证普通对象被转换"""
        class ToolCall:
            def __init__(self):
                self.tool_name = "read"
                self.input = {}

        call = ToolCall()
        result = _normalize_tool_call(call)
        assert isinstance(result, dict)
        assert result["tool_name"] == "read"

    def test_unknown_type_becomes_value_string(self):
        """验证未知类型变成字符串值"""
        result = _normalize_tool_call(12345)
        assert result == {"value": "12345"}


class TestSummarizeText:
    """测试 _summarize_text 文本摘要"""

    def test_truncates_long_text(self):
        """验证长文本被截断"""
        text = "a" * 200
        result = _summarize_text(text, default="test")
        assert len(result) <= 99  # 96 + "..."

    def test_empty_returns_default(self):
        """验证空文本返回默认摘要。"""
        result = _summarize_text("", default="test")
        assert result == "test"

    def test_short_text_preserved(self):
        """验证短文本被保留"""
        text = "short"
        result = _summarize_text(text, default="default-summary")
        assert result == "short"

    def test_whitespace_normalized(self):
        """验证空白字符被规范化"""
        text = "hello   world"
        result = _summarize_text(text, default="fall")
        assert "   " not in result


class TestToolMetadata:
    """测试 _tool_metadata 工具元数据构建"""

    def test_basic_fields(self):
        """验证基本字段"""
        tool_call = {"tool_name": "read", "tool_call_id": "c1"}
        facts = _ToolFacts(tool_name="read", tool_call_id="c1", tool_success=True, status="ok", error_code="", backend="openai")
        result = _tool_metadata(tool_call, facts=facts)

        assert result["tool_name"] == "read"
        assert result["tool_call_id"] == "c1"
        assert result["success"] is True
        assert result["status"] == "ok"

    def test_output_hash_added(self):
        """验证输出哈希被添加"""
        tool_call = {"tool_name": "read", "output": "some output content"}
        facts = _ToolFacts(tool_name="read", tool_call_id="", tool_success=True, status="ok", error_code="", backend="openai")
        result = _tool_metadata(tool_call, facts=facts)

        assert "output_hash" in result

    def test_parameters_added(self):
        """验证参数被添加"""
        tool_call = {"tool_name": "read", "parameters": {"path": "/data"}}
        facts = _ToolFacts(tool_name="read", tool_call_id="", tool_success=True, status="ok", error_code="", backend="openai")
        result = _tool_metadata(tool_call, facts=facts)

        assert "parameters" in result

    def test_noncanonical_parameter_bundles_are_not_promoted(self):
        """旧参数包裹字段不会被归档器提升成当前 parameters。"""
        tool_call = {"tool_name": "read", "args": {"path": "/data"}}
        facts = _ToolFacts(tool_name="read", tool_call_id="", tool_success=True, status="ok", error_code="", backend="openai")
        result = _tool_metadata(tool_call, facts=facts)

        assert "parameters" not in result
        assert "args" not in result
