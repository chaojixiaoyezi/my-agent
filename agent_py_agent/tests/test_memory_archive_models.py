"""单元测试：memory_archive models 数据模型"""

from __future__ import annotations

from dataclasses import asdict

import pytest

from agent_py_agent.agent.memory_archive.models import (
    CompressionSnapshot,
    RawMemoryEvent,
    utc_now_iso,
)


class TestUtcNowIso:
    """测试 utc_now_iso 函数"""

    def test_returns_iso_format_string(self):
        """验证返回 ISO 格式字符串"""
        result = utc_now_iso()
        assert isinstance(result, str)
        assert "T" in result
        assert "+" in result or "Z" in result

    def test_contains_timezone_info(self):
        """验证包含时区信息"""
        result = utc_now_iso()
        assert "+00:00" in result or "Z" in result

    def test_deterministic_per_call(self):
        """验证每次调用产生有效时间"""
        result1 = utc_now_iso()
        result2 = utc_now_iso()
        # 两次调用应该产生类似格式（不一定完全相同，因为可能有微小时间差）
        assert len(result1) > 10
        assert len(result2) > 10


class TestCompressionSnapshot:
    """测试 CompressionSnapshot 数据类"""

    def test_basic_creation(self):
        """验证基本创建"""
        snapshot = CompressionSnapshot(
            snapshot_id="snap-123",
            session_id="sess-456",
            compression_id="comp-789",
            turn_range={"turn": 1},
        )
        assert snapshot.snapshot_id == "snap-123"
        assert snapshot.session_id == "sess-456"
        assert snapshot.compression_id == "comp-789"

    def test_default_values(self):
        """验证默认值"""
        snapshot = CompressionSnapshot(
            snapshot_id="snap-1",
            session_id="sess-1",
            compression_id="comp-1",
            turn_range={},
        )
        assert snapshot.participants == []
        assert snapshot.user_intents == []
        assert snapshot.assistant_actions == []
        assert snapshot.archive_level == 3
        assert snapshot.token_estimate == 0

    def test_to_dict(self):
        """验证 to_dict 序列化"""
        snapshot = CompressionSnapshot(
            snapshot_id="snap-dict",
            session_id="sess-dict",
            compression_id="comp-dict",
            turn_range={"turn": 5},
            participants=["user1", "assistant1"],
            user_intents=["写代码", "测试"],
        )
        d = snapshot.to_dict()
        assert d["snapshot_id"] == "snap-dict"
        assert d["participants"] == ["user1", "assistant1"]
        assert d["user_intents"] == ["写代码", "测试"]

    def test_created_at_and_timestamp_sync(self):
        """验证 created_at 和 timestamp 同步"""
        snapshot = CompressionSnapshot(
            snapshot_id="snap-sync",
            session_id="sess-sync",
            compression_id="comp-sync",
            turn_range={},
            created_at="2026-05-01T10:00:00Z",
        )
        # post_init 应该同步 timestamp
        assert snapshot.timestamp == "2026-05-01T10:00:00Z"

    def test_timestamp_default_falls_back_to_created_at(self):
        """验证 timestamp 默认回退到 created_at"""
        snapshot = CompressionSnapshot(
            snapshot_id="snap-back",
            session_id="sess-back",
            compression_id="comp-back",
            turn_range={},
        )
        # 两者应该都使用默认值（utc_now_iso）
        assert snapshot.timestamp == snapshot.created_at

    def test_nested_fields(self):
        """验证嵌套字段"""
        snapshot = CompressionSnapshot(
            snapshot_id="snap-nested",
            session_id="sess-nested",
            compression_id="comp-nested",
            turn_range={"turn": 1, "request_id": "req-1"},
            tool_calls=[
                {"tool_name": "read", "status": "ok"},
            ],
            dispatch_events=[
                {"action": "spawn", "run_id": "sub-1"},
            ],
            task_refs=["subagent-1", "subagent-2"],
        )
        assert len(snapshot.tool_calls) == 1
        assert snapshot.tool_calls[0]["tool_name"] == "read"
        assert len(snapshot.task_refs) == 2

    def test_token_usage_field(self):
        """验证 token_usage 字段"""
        snapshot = CompressionSnapshot(
            snapshot_id="snap-token",
            session_id="sess-token",
            compression_id="comp-token",
            turn_range={},
            token_usage={"input": 1000, "output": 500},
        )
        assert snapshot.token_usage["input"] == 1000
        assert snapshot.token_usage["output"] == 500


class TestRawMemoryEvent:
    """测试 RawMemoryEvent 数据类"""

    def test_basic_creation(self):
        """验证基本创建"""
        event = RawMemoryEvent(
            event_id="raw-123",
            session_id="sess-456",
            request_id="req-789",
            run_id="run-000",
        )
        assert event.event_id == "raw-123"
        assert event.session_id == "sess-456"

    def test_default_values(self):
        """验证默认值"""
        event = RawMemoryEvent(
            event_id="raw-default",
            session_id="sess-default",
        )
        assert event.request_id == ""
        assert event.run_id == ""
        assert event.speaker == ""
        assert event.action == ""
        assert event.status == ""
        assert event.visibility == "private"
        assert event.archive_level == 3

    def test_to_dict(self):
        """验证 to_dict 序列化"""
        event = RawMemoryEvent(
            event_id="raw-dict",
            session_id="sess-dict",
            speaker="user",
            target="assistant",
            action="message",
            content_preview="Hello",
            content_hash="sha256:abc123",
        )
        d = event.to_dict()
        assert d["event_id"] == "raw-dict"
        assert d["speaker"] == "user"
        assert d["content_preview"] == "Hello"

    def test_tool_call_fields(self):
        """验证工具调用相关字段"""
        event = RawMemoryEvent(
            event_id="raw-tool",
            session_id="sess-tool",
            speaker="tool",
            target="assistant",
            action="tool_call",
            tool_name="read_file",
            tool_call_id="call-123",
            tool_success=True,
            status="ok",
        )
        assert event.tool_name == "read_file"
        assert event.tool_call_id == "call-123"
        assert event.tool_success is True

    def test_error_code_field(self):
        """验证错误码字段"""
        event = RawMemoryEvent(
            event_id="raw-err",
            session_id="sess-err",
            status="error",
            error_code="TOOL_FAILED",
        )
        assert event.status == "error"
        assert event.error_code == "TOOL_FAILED"

    def test_is_dispatch_flag(self):
        """验证 is_dispatch 标志"""
        event = RawMemoryEvent(
            event_id="raw-dispatch",
            session_id="sess-dispatch",
            is_dispatch=True,
        )
        assert event.is_dispatch is True

    def test_source_field(self):
        """验证 source 字段"""
        event = RawMemoryEvent(
            event_id="raw-src",
            session_id="sess-src",
            source="run",
        )
        assert event.source == "run"

    def test_content_path_field(self):
        """验证 content_path 字段"""
        event = RawMemoryEvent(
            event_id="raw-path",
            session_id="sess-path",
            content_path="/data/events/2026-05-01.jsonl",
        )
        assert event.content_path == "/data/events/2026-05-01.jsonl"


class TestModelSerializationRoundTrip:
    """测试模型序列化往返"""

    def test_compression_snapshot_roundtrip(self):
        """验证 CompressionSnapshot 往返序列化"""
        original = CompressionSnapshot(
            snapshot_id="snap-round",
            session_id="sess-round",
            compression_id="comp-round",
            turn_range={"turn": 10},
            participants=["user"],
            user_intents=["intent1"],
            assistant_actions=["action1"],
            decisions=["decision1"],
            open_questions=["question1"],
            next_actions=["next1"],
            token_usage={"input": 500, "output": 200},
            archive_level=2,
            content_paths=["/path1", "/path2"],
        )

        d = original.to_dict()
        restored = CompressionSnapshot(**d)

        assert restored.snapshot_id == original.snapshot_id
        assert restored.session_id == original.session_id
        assert restored.archive_level == original.archive_level
        assert restored.token_usage == original.token_usage

    def test_raw_memory_event_roundtrip(self):
        """验证 RawMemoryEvent 往返序列化"""
        original = RawMemoryEvent(
            event_id="raw-round",
            session_id="sess-round",
            request_id="req-round",
            run_id="run-round",
            task_id="task-round",
            speaker="assistant",
            target="user",
            action="response",
            created_at="2026-05-01T12:00:00Z",
            status="ok",
            tool_name="write",
            tool_success=True,
            content_preview="Result written",
            content_hash="sha256:def456",
            source="chat",
            archive_level=1,
        )

        d = original.to_dict()
        restored = RawMemoryEvent(**d)

        assert restored.event_id == original.event_id
        assert restored.speaker == original.speaker
        assert restored.tool_name == original.tool_name
        assert restored.content_hash == original.content_hash


class TestCompressionSnapshotEdgeCases:
    """测试 CompressionSnapshot 边界情况"""

    def test_empty_lists(self):
        """验证空列表字段"""
        snapshot = CompressionSnapshot(
            snapshot_id="snap-empty",
            session_id="sess-empty",
            compression_id="comp-empty",
            turn_range={},
            participants=[],
            user_intents=[],
            assistant_actions=[],
            tool_calls=[],
            dispatch_events=[],
            task_refs=[],
            decisions=[],
            open_questions=[],
            next_actions=[],
        )
        assert snapshot.participants == []

    def test_large_token_estimate(self):
        """验证大 token 估算值"""
        snapshot = CompressionSnapshot(
            snapshot_id="snap-big",
            session_id="sess-big",
            compression_id="comp-big",
            turn_range={},
            token_estimate=1000000,
        )
        assert snapshot.token_estimate == 1000000

    def test_custom_archive_level(self):
        """验证自定义归档等级"""
        for level in range(4):
            snapshot = CompressionSnapshot(
                snapshot_id=f"snap-{level}",
                session_id="sess-level",
                compression_id="comp-level",
                turn_range={},
                archive_level=level,
            )
            assert snapshot.archive_level == level