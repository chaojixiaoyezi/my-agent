"""migration 测试（测试 query/archive_helpers 模块）。

测试数据迁移辅助函数、字段推导、内容预览等。
注意：memory_archive 目录下没有 migration.py，这里测试 archive_helpers 模块。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestDerivedArchiveFields:
    """测试 _derived_archive_fields 字段推导函数。"""

    def test_derive_from_turn_range(self):
        """测试从 turn_range 推导字段。"""
        from agent_py_agent.agent.memory_archive.query.archive_helpers import (
            _derived_archive_fields,
        )

        payload = {
            "turn_range": {
                "request_id": "req_001",
                "run_id": "run_001",
                "task_id": "task_001",
                "source": "test",
                "status": "ok",
                "error_code": "",
            }
        }

        result = _derived_archive_fields(payload)

        assert result["request_id"] == "req_001"
        assert result["run_id"] == "run_001"
        assert result["task_id"] == "task_001"

    def test_derive_from_dispatch_events(self):
        """测试从 dispatch_events 推导字段。"""
        from agent_py_agent.agent.memory_archive.query.archive_helpers import (
            _derived_archive_fields,
        )

        payload = {
            "dispatch_events": [
                {
                    "request_id": "req_002",
                    "run_id": "run_002",
                    "task_id": "task_002",
                    "source": "dispatch",
                    "status": "ok",
                    "error_code": "ERR_001",
                }
            ]
        }

        result = _derived_archive_fields(payload)

        assert result["request_id"] == "req_002"
        assert result["run_id"] == "run_002"
        assert result["error_code"] == "ERR_001"

    def test_derive_prefers_turn_range(self):
        """测试 turn_range 优先于 dispatch_events。"""
        from agent_py_agent.agent.memory_archive.query.archive_helpers import (
            _derived_archive_fields,
        )

        payload = {
            "turn_range": {"request_id": "turn_range_req"},
            "dispatch_events": [{"request_id": "dispatch_req"}],
        }

        result = _derived_archive_fields(payload)

        assert result["request_id"] == "turn_range_req"

    def test_derive_empty_payload(self):
        """测试空 payload。"""
        from agent_py_agent.agent.memory_archive.query.archive_helpers import (
            _derived_archive_fields,
        )

        result = _derived_archive_fields({})
        assert result == {}

    def test_derive_ignores_non_dict(self):
        """测试忽略非字典类型的字段。"""
        from agent_py_agent.agent.memory_archive.query.archive_helpers import (
            _derived_archive_fields,
        )

        payload = {
            "turn_range": "not a dict",
            "dispatch_events": 123,
        }

        result = _derived_archive_fields(payload)
        assert result == {}


class TestArchivePreview:
    """测试 _archive_preview 内容预览函数。"""

    def test_preview_from_content_preview(self):
        """测试从 content_preview 获取预览。"""
        from agent_py_agent.agent.memory_archive.query.archive_helpers import _archive_preview

        payload = {"content_preview": "This is a test preview"}
        result = _archive_preview(payload)

        assert result == "This is a test preview"

    def test_preview_from_user_intents(self):
        """测试从 user_intents 拼接预览。"""
        from agent_py_agent.agent.memory_archive.query.archive_helpers import _archive_preview

        payload = {
            "user_intents": ["Intent 1", "Intent 2"],
            "assistant_actions": ["Action 1"],
        }

        result = _archive_preview(payload)

        assert "Intent 1" in result
        assert "Intent 2" in result

    def test_preview_from_multiple_fields(self):
        """测试从多个字段拼接预览。"""
        from agent_py_agent.agent.memory_archive.query.archive_helpers import _archive_preview

        payload = {
            "user_intents": ["Intent"],
            "assistant_actions": ["Action"],
            "decisions": ["Decision"],
            "open_questions": ["Question"],
            "next_actions": ["Next"],
            "task_refs": ["Ref"],
        }

        result = _archive_preview(payload)

        assert "Intent" in result
        assert "Action" in result
        assert "Decision" in result

    def test_preview_truncation(self):
        """测试预览文本截断到 500 字符。"""
        from agent_py_agent.agent.memory_archive.query.archive_helpers import _archive_preview

        payload = {
            "user_intents": ["x" * 600],
        }

        result = _archive_preview(payload)
        assert len(result) <= 500

    def test_preview_empty_payload(self):
        """测试空 payload。"""
        from agent_py_agent.agent.memory_archive.query.archive_helpers import _archive_preview

        result = _archive_preview({})
        assert result == ""


class TestArchiveErrorRecord:
    """测试 _archive_error_record 错误记录函数。"""

    def test_error_record_fields(self, tmp_path: Path):
        """测试错误记录包含所有必要字段。"""
        from agent_py_agent.agent.memory_archive.query.archive_helpers import _archive_error_record

        test_file = tmp_path / "test.jsonl"
        test_file.write_text("{}", encoding="utf-8")

        result = _archive_error_record("hook", test_file, line_no=5, message="Test error")

        assert result["layer"] == "hook"
        assert result["kind"] == "archive_error"
        assert result["line_no"] == 5
        assert "Test error" in result["content_preview"]
        assert result["status"] == "failed"

    def test_error_record_id_format(self, tmp_path: Path):
        """测试错误记录 ID 格式。"""
        from agent_py_agent.agent.memory_archive.query.archive_helpers import _archive_error_record

        test_file = tmp_path / "test.jsonl"
        test_file.write_text("{}", encoding="utf-8")

        result = _archive_error_record("raw", test_file, line_no=10, message="Error")

        assert "test.jsonl" in result["id"]
        assert "10" in result["id"]


class TestCreatedAtSort:
    """测试 _created_at_sort 时间排序函数。"""

    def test_sort_iso_format(self):
        """测试 ISO 格式时间解析。"""
        from agent_py_agent.agent.memory_archive.query.archive_helpers import _created_at_sort

        result = _created_at_sort("2026-05-03T10:00:00Z", fallback=0.0)

        assert result > 0

    def test_sort_date_only(self):
        """测试日期-only 格式解析。"""
        from agent_py_agent.agent.memory_archive.query.archive_helpers import _created_at_sort

        result = _created_at_sort("2026-05-03", fallback=0.0)

        assert result > 0

    def test_sort_empty_returns_fallback(self):
        """测试空字符串返回 fallback。"""
        from agent_py_agent.agent.memory_archive.query.archive_helpers import _created_at_sort

        result = _created_at_sort("", fallback=123.45)

        assert result == 123.45

    def test_sort_invalid_returns_fallback(self):
        """测试无效时间格式返回 fallback。"""
        from agent_py_agent.agent.memory_archive.query.archive_helpers import _created_at_sort

        result = _created_at_sort("not a date", fallback=999.0)

        assert result == 999.0

    def test_sort_appends_z_timezone(self):
        """测试末尾 Z 被正确处理。"""
        from agent_py_agent.agent.memory_archive.query.archive_helpers import _created_at_sort

        result = _created_at_sort("2026-05-03T10:00:00Z", fallback=0.0)

        assert result > 0


class TestIsDateOnly:
    """测试 _is_date_only 日期判断函数。"""

    def test_date_only_valid(self):
        """测试有效的 YYYY-MM-DD 格式。"""
        from agent_py_agent.agent.memory_archive.query.archive_helpers import _is_date_only

        assert _is_date_only("2026-05-03") is True

    def test_date_only_invalid_length(self):
        """测试错误长度。"""
        from agent_py_agent.agent.memory_archive.query.archive_helpers import _is_date_only

        assert _is_date_only("2026-5-3") is False

    def test_date_only_invalid_format(self):
        """测试无效格式（非日期）。"""
        from agent_py_agent.agent.memory_archive.query.archive_helpers import _is_date_only

        assert _is_date_only("2026-13-45") is False

    def test_date_only_with_time(self):
        """测试带时间的字符串返回 False。"""
        from agent_py_agent.agent.memory_archive.query.archive_helpers import _is_date_only

        assert _is_date_only("2026-05-03T10:00:00Z") is False


class TestListValue:
    """测试 _list_value 列表归一化函数。"""

    def test_list_value_already_list(self):
        """测试已经是列表的值。"""
        from agent_py_agent.agent.memory_archive.query.archive_helpers import _list_value

        result = _list_value(["a", "b", "c"])
        assert result == ["a", "b", "c"]

    def test_list_value_single_value(self):
        """测试单个值转换为列表。"""
        from agent_py_agent.agent.memory_archive.query.archive_helpers import _list_value

        result = _list_value("single")
        assert result == ["single"]

    def test_list_value_none(self):
        """测试 None 返回空列表。"""
        from agent_py_agent.agent.memory_archive.query.archive_helpers import _list_value

        result = _list_value(None)
        assert result == []

    def test_list_value_empty_string(self):
        """测试空字符串返回空列表。"""
        from agent_py_agent.agent.memory_archive.query.archive_helpers import _list_value

        result = _list_value("")
        assert result == []


class TestDedupeStrings:
    """测试 _dedupe_strings 字符串去重函数。"""

    def test_dedupe_basic(self):
        """测试基本去重。"""
        from agent_py_agent.agent.memory_archive.query.archive_helpers import _dedupe_strings

        result = _dedupe_strings(["a", "b", "a", "c"])
        assert result == ["a", "b", "c"]

    def test_dedupe_preserves_order(self):
        """测试保持首次出现顺序。"""
        from agent_py_agent.agent.memory_archive.query.archive_helpers import _dedupe_strings

        result = _dedupe_strings(["first", "second", "first", "third"])
        assert result == ["first", "second", "third"]

    def test_dedupe_strips_and_deduplicates(self):
        """测试去除空白字符并去重。"""
        from agent_py_agent.agent.memory_archive.query.archive_helpers import _dedupe_strings

        result = _dedupe_strings(["  a  ", "b", " a ", "c"])
        assert result == ["a", "b", "c"]


class TestAppendRunId:
    """测试 _append_run_id 子代理 ID 提取函数。"""

    def test_append_run_id_extracts_subagent(self):
        """测试提取 subagent-* ID。"""
        from agent_py_agent.agent.memory_archive.query.archive_helpers import _append_run_id

        items = []
        _append_run_id(items, "请看 subagent-abc123 的结果")

        assert "subagent-abc123" in items

    def test_append_run_id_ignores_non_subagent(self):
        """测试忽略非 subagent 内容。"""
        from agent_py_agent.agent.memory_archive.query.archive_helpers import _append_run_id

        items = []
        _append_run_id(items, "这是一段普通文本")

        assert len(items) == 0

    def test_append_run_id_handles_empty(self):
        """测试处理空值。"""
        from agent_py_agent.agent.memory_archive.query.archive_helpers import _append_run_id

        items = []
        _append_run_id(items, "")

        assert len(items) == 0


class TestArchiveSearchText:
    """测试 _archive_search_text 搜索文本构建函数。"""

    def test_search_text_includes_ids(self):
        """测试搜索文本包含各种 ID。"""
        from agent_py_agent.agent.memory_archive.query.archive_helpers import _archive_search_text

        record = {
            "id": "rec_001",
            "session_id": "sess_001",
            "request_id": "req_001",
            "run_id": "run_001",
            "task_id": "task_001",
            "speaker": "user",
            "target": "assistant",
            "action": "message",
            "status": "ok",
            "tool_name": "test_tool",
            "source": "test",
            "content_preview": "test content",
            "task_refs": ["ref1", "ref2"],
            "next_actions": ["action1"],
            "payload": {},
        }

        result = _archive_search_text(record)

        assert "rec_001" in result
        assert "sess_001" in result
        assert "req_001" in result
        assert "test content" in result

    def test_search_text_lowercase(self):
        """测试搜索文本转为小写。"""
        from agent_py_agent.agent.memory_archive.query.archive_helpers import _archive_search_text

        record = {"id": "TEST_ID", "content_preview": "TEST CONTENT"}
        result = _archive_search_text(record)

        assert result.islower() or "test_id" in result
