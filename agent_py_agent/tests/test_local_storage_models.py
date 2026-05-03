"""本地存储数据模型测试 - local_storage/models.py 存储模型、字段映射。"""
from __future__ import annotations

import time
from agent_py_agent.agent.local_storage.models import (
    PREVIEW_CHARS,
    LocalStoreEvent,
    LocalTimelineItem,
    LocalSearchResult,
)


class TestConstants:
    """常量测试。"""

    def test_preview_chars_value(self):
        """验证预览字符数常量。"""
        assert PREVIEW_CHARS == 12000
        assert isinstance(PREVIEW_CHARS, int)


class TestLocalStoreEvent:
    """LocalStoreEvent 数据类测试。"""

    def test_event_required_fields(self):
        """验证必需字段。"""
        event = LocalStoreEvent(
            event_id="evt-1",
            event_type="record_created",
            record_id="rec-1",
            payload={"key": "value"},
            created_at=1234567890.0,
        )
        assert event.event_id == "evt-1"
        assert event.event_type == "record_created"
        assert event.record_id == "rec-1"
        assert event.payload == {"key": "value"}
        assert event.created_at == 1234567890.0

    def test_event_with_empty_payload(self):
        """验证空 payload。"""
        event = LocalStoreEvent(
            event_id="evt-1",
            event_type="test",
            record_id="rec-1",
            payload={},
            created_at=time.time(),
        )
        assert event.payload == {}

    def test_event_with_complex_payload(self):
        """验证复杂结构 payload。"""
        payload = {
            "nested": {"a": 1, "b": [1, 2, 3]},
            "list": [{"type": "item"}],
        }
        event = LocalStoreEvent(
            event_id="evt-1",
            event_type="complex",
            record_id="rec-1",
            payload=payload,
            created_at=time.time(),
        )
        assert event.payload == payload


class TestLocalTimelineItem:
    """LocalTimelineItem 数据类测试。"""

    def test_timeline_item_required_fields(self):
        """验证必需字段。"""
        item = LocalTimelineItem(
            event_id="evt-1",
            event_type="timeline_event",
            record_id="rec-1",
            source_type="memory",
            source_id="src-1",
            title="事件标题",
            payload={"action": "created"},
            created_at=1234567890.0,
        )
        assert item.event_id == "evt-1"
        assert item.source_type == "memory"
        assert item.title == "事件标题"

    def test_timeline_item_defaults(self):
        """验证可选字段默认值。"""
        item = LocalTimelineItem(
            event_id="evt-1",
            event_type="e",
            record_id="r",
            source_type="s",
            source_id="si",
            title="t",
            payload={},
            created_at=0.0,
        )
        assert item.event_id == "evt-1"


class TestLocalSearchResult:
    """LocalSearchResult 数据类测试。"""

    def test_search_result_required_fields(self):
        """验证必需字段。"""
        result = LocalSearchResult(
            id="rec-1",
            source_type="memory",
            source_id="src-1",
            title="搜索结果标题",
            content="这是搜索结果的完整内容",
            metadata={"role": "user"},
            visibility="private",
            created_at=1234567890.0,
            updated_at=1234567900.0,
        )
        assert result.id == "rec-1"
        assert result.source_type == "memory"
        assert result.title == "搜索结果标题"
        assert result.content == "这是搜索结果的完整内容"

    def test_search_result_defaults(self):
        """验证默认值。"""
        result = LocalSearchResult(
            id="rec-1",
            source_type="memory",
            source_id="src-1",
            title="title",
            content="content",
            metadata={},
            visibility="private",
            created_at=0.0,
            updated_at=0.0,
        )
        assert result.score == 0.0
        assert result.content_path == ""

    def test_search_result_with_score(self):
        """验证相关性分数。"""
        result = LocalSearchResult(
            id="rec-1",
            source_type="memory",
            source_id="src-1",
            title="title",
            content="content",
            metadata={},
            visibility="private",
            created_at=0.0,
            updated_at=0.0,
            score=0.95,
        )
        assert result.score == 0.95

    def test_search_result_with_content_path(self):
        """验证内容路径。"""
        result = LocalSearchResult(
            id="rec-1",
            source_type="memory",
            source_id="src-1",
            title="title",
            content="content",
            metadata={},
            visibility="private",
            created_at=0.0,
            updated_at=0.0,
            content_path="files/rec-1.txt",
        )
        assert result.content_path == "files/rec-1.txt"

    def test_search_result_metadata_access(self):
        """验证元数据访问。"""
        metadata = {
            "role": "user",
            "kind": "dialogue",
            "tags": ["tag1", "tag2"],
            "created_at": 1234567890.0,
        }
        result = LocalSearchResult(
            id="rec-1",
            source_type="memory",
            source_id="src-1",
            title="title",
            content="content",
            metadata=metadata,
            visibility="private",
            created_at=1234567890.0,
            updated_at=1234567900.0,
        )
        assert result.metadata["role"] == "user"
        assert result.metadata["tags"] == ["tag1", "tag2"]
