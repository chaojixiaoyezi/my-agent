from __future__ import annotations

"""LLM: tests for local_storage models.

给人看的解释：
测试 LocalStore 数据模型：LocalStoreEvent、LocalTimelineItem、LocalSearchResult。
"""

import time
from dataclasses import asdict

import pytest

from agent_py_agent.agent.local_storage.models import (
    PREVIEW_CHARS,
    LocalSearchResult,
    LocalStoreEvent,
    LocalTimelineItem,
)


class TestLocalStoreEvent:
    """测试 LocalStoreEvent 模型。"""

    def test_create_event(self) -> None:
        """测试创建事件。"""
        event = LocalStoreEvent(
            event_id="evt-001",
            event_type="test_event",
            record_id="rec-001",
            payload={"key": "value"},
            created_at=time.time(),
        )
        assert event.event_id == "evt-001"
        assert event.event_type == "test_event"
        assert event.payload["key"] == "value"

    def test_event_is_dataclass(self) -> None:
        """测试事件是 dataclass。"""
        event = LocalStoreEvent(
            event_id="id",
            event_type="type",
            record_id="rec",
            payload={},
            created_at=1.0,
        )
        assert asdict(event) is not None

    def test_event_payload_mutable(self) -> None:
        """测试 payload 可变。"""
        event = LocalStoreEvent(
            event_id="evt",
            event_type="type",
            record_id="rec",
            payload={},
            created_at=1.0,
        )
        event.payload["new_key"] = "new_value"
        assert event.payload["new_key"] == "new_value"

    def test_event_created_at_default(self) -> None:
        """测试 created_at 可以是 0。"""
        event = LocalStoreEvent(
            event_id="evt",
            event_type="type",
            record_id="rec",
            payload={},
            created_at=0.0,
        )
        assert event.created_at == 0.0


class TestLocalTimelineItem:
    """测试 LocalTimelineItem 模型。"""

    def test_create_timeline_item(self) -> None:
        """测试创建时间线条目。"""
        item = LocalTimelineItem(
            event_id="evt-001",
            event_type="record_created",
            record_id="rec-001",
            source_type="memory",
            source_id="mem-001",
            title="New Memory",
            payload={"content": "test"},
            created_at=time.time(),
        )
        assert item.event_id == "evt-001"
        assert item.source_type == "memory"
        assert item.title == "New Memory"

    def test_timeline_item_all_fields(self) -> None:
        """测试时间线条目所有字段。"""
        item = LocalTimelineItem(
            event_id="e1",
            event_type="t1",
            record_id="r1",
            source_type="s",
            source_id="sid",
            title="title",
            payload={"k": "v"},
            created_at=123.456,
        )
        assert item.event_id == "e1"
        assert item.source_id == "sid"
        assert item.created_at == 123.456

    def test_timeline_item_asdict(self) -> None:
        """测试时间线条目转 dict。"""
        item = LocalTimelineItem(
            event_id="evt",
            event_type="type",
            record_id="rec",
            source_type="src",
            source_id="sid",
            title="title",
            payload={},
            created_at=1.0,
        )
        d = asdict(item)
        assert "event_id" in d
        assert "source_type" in d


class TestLocalSearchResult:
    """测试 LocalSearchResult 模型。"""

    def test_create_search_result(self) -> None:
        """测试创建搜索结果。"""
        result = LocalSearchResult(
            id="rec-001",
            source_type="memory",
            source_id="mem-001",
            title="Memory Title",
            content="Memory content here",
            metadata={"role": "user"},
            visibility="private",
            created_at=time.time(),
            updated_at=time.time(),
        )
        assert result.id == "rec-001"
        assert result.content == "Memory content here"

    def test_search_result_with_score(self) -> None:
        """测试带分数的搜索结果。"""
        result = LocalSearchResult(
            id="rec-002",
            source_type="gateway",
            source_id="req-002",
            title="Gateway Request",
            content="Request content",
            metadata={},
            visibility="private",
            created_at=1.0,
            updated_at=1.0,
            score=0.95,
        )
        assert result.score == 0.95

    def test_search_result_default_score(self) -> None:
        """测试搜索结果默认分数为 0。"""
        result = LocalSearchResult(
            id="rec-003",
            source_type="test",
            source_id="test",
            title="Test",
            content="content",
            metadata={},
            visibility="private",
            created_at=1.0,
            updated_at=1.0,
        )
        assert result.score == 0.0

    def test_search_result_with_content_path(self) -> None:
        """测试带 content_path 的搜索结果。"""
        result = LocalSearchResult(
            id="rec-004",
            source_type="test",
            source_id="test",
            title="Test",
            content="content",
            metadata={},
            visibility="private",
            created_at=1.0,
            updated_at=1.0,
            content_path="/path/to/file.txt",
        )
        assert result.content_path == "/path/to/file.txt"

    def test_search_result_metadata_access(self) -> None:
        """测试元数据访问。"""
        result = LocalSearchResult(
            id="rec-005",
            source_type="memory",
            source_id="mem",
            title="Title",
            content="Content",
            metadata={"tags": ["tag1", "tag2"]},
            visibility="private",
            created_at=1.0,
            updated_at=1.0,
        )
        assert result.metadata["tags"] == ["tag1", "tag2"]


class TestPreviewChars:
    """测试 PREVIEW_CHARS 常量。"""

    def test_preview_chars_is_int(self) -> None:
        """测试 PREVIEW_CHARS 是整数。"""
        assert isinstance(PREVIEW_CHARS, int)

    def test_preview_chars_positive(self) -> None:
        """测试 PREVIEW_CHARS 为正数。"""
        assert PREVIEW_CHARS > 0

    def test_preview_chars_reasonable_size(self) -> None:
        """测试 PREVIEW_CHARS 大小合理。"""
        assert PREVIEW_CHARS >= 1000


class TestModelImmutability:
    """测试模型字段特性。"""

    def test_local_store_event_fields(self) -> None:
        """测试 LocalStoreEvent 字段。"""
        fields = ["event_id", "event_type", "record_id", "payload", "created_at"]
        event = LocalStoreEvent(
            event_id="e",
            event_type="t",
            record_id="r",
            payload={},
            created_at=1.0,
        )
        for field in fields:
            assert hasattr(event, field)

    def test_local_timeline_item_fields(self) -> None:
        """测试 LocalTimelineItem 字段。"""
        fields = ["event_id", "event_type", "record_id", "source_type", "source_id", "title", "payload", "created_at"]
        item = LocalTimelineItem(
            event_id="e",
            event_type="t",
            record_id="r",
            source_type="s",
            source_id="sid",
            title="title",
            payload={},
            created_at=1.0,
        )
        for field in fields:
            assert hasattr(item, field)

    def test_local_search_result_fields(self) -> None:
        """测试 LocalSearchResult 字段。"""
        fields = ["id", "source_type", "source_id", "title", "content", "metadata", "visibility", "created_at", "updated_at", "score", "content_path"]
        result = LocalSearchResult(
            id="id",
            source_type="s",
            source_id="sid",
            title="t",
            content="c",
            metadata={},
            visibility="v",
            created_at=1.0,
            updated_at=1.0,
        )
        for field in fields:
            assert hasattr(result, field)