from __future__ import annotations

import json
import pytest
import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent_py_agent.agent.local_storage.schema import LocalStoreSchemaMixin
from agent_py_agent.agent.local_storage.records import LocalStoreRecordMixin
from agent_py_agent.agent.local_storage.search import LocalStoreSearchMixin
from agent_py_agent.agent.local_storage.events import LocalStoreEventMixin
from agent_py_agent.agent.local_storage.models import LocalSearchResult


class MinimalStore(
    LocalStoreSchemaMixin,
    LocalStoreRecordMixin,
    LocalStoreSearchMixin,
    LocalStoreEventMixin,
):
    """Minimal concrete store for testing records mixin."""

    def __init__(self, tmp_path: Path):
        self.root = tmp_path / "store"
        self.root.mkdir()
        self.files_dir = self.root / "files"
        self.files_dir.mkdir()
        self.events_path = self.root / "events.jsonl"
        self.db_path = self.root / "test.db"
        self.enable_fts = False
        self.fts_available = False
        self._fts_available = False
        self._init_schema()

    def _index_dispatch_record(self, record):
        pass

    def _index_report(self, source_type, source_id, title, report, event_type=None):
        pass


class TestMakeRecordId:
    def test_make_record_id_basic(self):
        result = LocalStoreRecordMixin.make_record_id("memory", "abc123")
        assert result.startswith("rec-")
        assert len(result) > 4

    def test_make_record_id_stable(self):
        id1 = LocalStoreRecordMixin.make_record_id("memory", "abc")
        id2 = LocalStoreRecordMixin.make_record_id("memory", "abc")
        assert id1 == id2

    def test_make_record_id_different_sources_different_ids(self):
        id1 = LocalStoreRecordMixin.make_record_id("memory", "abc")
        id2 = LocalStoreRecordMixin.make_record_id("gateway_request", "abc")
        assert id1 != id2

    def test_make_record_id_different_ids_different_content(self):
        id1 = LocalStoreRecordMixin.make_record_id("memory", "abc")
        id2 = LocalStoreRecordMixin.make_record_id("memory", "xyz")
        assert id1 != id2


class TestUpsertRecordBasic:
    def test_upsert_record_insert(self, tmp_path):
        store = MinimalStore(tmp_path)
        result = store.upsert_record(
            source_type="memory",
            source_id="test-001",
            title="Test Record",
            content="This is test content",
        )
        assert result.source_type == "memory"
        assert result.source_id == "test-001"
        assert result.title == "Test Record"
        assert result.content == "This is test content"

    def test_upsert_record_strips_source_type(self, tmp_path):
        store = MinimalStore(tmp_path)
        result = store.upsert_record(
            source_type="  memory  ",
            source_id="test",
            title="Test",
            content="content",
        )
        assert result.source_type == "memory"

    def test_upsert_record_empty_source_type_defaults(self, tmp_path):
        store = MinimalStore(tmp_path)
        result = store.upsert_record(
            source_type="   ",
            source_id="test",
            title="Test",
            content="content",
        )
        assert result.source_type == "unknown"

    def test_upsert_record_empty_source_id_generates_uuid(self, tmp_path):
        store = MinimalStore(tmp_path)
        result = store.upsert_record(
            source_type="memory",
            source_id="   ",
            title="Test",
            content="content",
        )
        assert result.source_id != ""

    def test_upsert_record_empty_title_uses_source_id(self, tmp_path):
        store = MinimalStore(tmp_path)
        result = store.upsert_record(
            source_type="memory",
            source_id="my-source-id",
            title="   ",
            content="content",
        )
        assert result.title == "my-source-id"

    def test_upsert_record_with_metadata(self, tmp_path):
        store = MinimalStore(tmp_path)
        result = store.upsert_record(
            source_type="gateway_request",
            source_id="req-123",
            title="Request",
            content="request body",
            metadata={"status": "pending", "priority": 1},
        )
        assert result.metadata["status"] == "pending"
        assert result.metadata["priority"] == 1

    def test_upsert_record_with_visibility(self, tmp_path):
        store = MinimalStore(tmp_path)
        result = store.upsert_record(
            source_type="test",
            source_id="id",
            title="T",
            content="c",
            visibility="public",
        )
        assert result.visibility == "public"


class TestUpsertRecordUpdate:
    def test_upsert_record_updates_existing(self, tmp_path):
        store = MinimalStore(tmp_path)
        store.upsert_record(
            source_type="memory",
            source_id="update-test",
            title="Original",
            content="original content",
        )
        result = store.upsert_record(
            source_type="memory",
            source_id="update-test",
            title="Updated",
            content="updated content",
        )
        assert result.title == "Updated"
        assert result.content == "updated content"

    def test_upsert_record_preserves_created_at(self, tmp_path):
        store = MinimalStore(tmp_path)
        first = store.upsert_record(
            source_type="memory",
            source_id="preserve-test",
            title="First",
            content="first",
        )
        time.sleep(0.01)
        second = store.upsert_record(
            source_type="memory",
            source_id="preserve-test",
            title="Second",
            content="second",
        )
        assert second.created_at == first.created_at
        assert second.updated_at >= second.created_at


class TestUpsertRecordContent:
    def test_upsert_record_saves_content_to_file(self, tmp_path):
        store = MinimalStore(tmp_path)
        result = store.upsert_record(
            source_type="test",
            source_id="file-test",
            title="T",
            content="file content here",
        )
        assert result.content == "file content here"
        assert result.content_path

    def test_upsert_record_long_content(self, tmp_path):
        store = MinimalStore(tmp_path)
        long_content = "x" * 50000
        result = store.upsert_record(
            source_type="test",
            source_id="long",
            title="Long",
            content=long_content,
        )
        assert len(result.content) == 50000


class TestGetRecord:
    def test_get_record_exists(self, tmp_path):
        store = MinimalStore(tmp_path)
        inserted = store.upsert_record(
            source_type="memory",
            source_id="get-test",
            title="Get Test",
            content="get content",
        )
        retrieved = store.get_record(inserted.id)
        assert retrieved is not None
        assert retrieved.id == inserted.id
        assert retrieved.title == "Get Test"

    def test_get_record_not_exists(self, tmp_path):
        store = MinimalStore(tmp_path)
        result = store.get_record("nonexistent-id")
        assert result is None


class TestLogRecord:
    def test_log_record_creates_record_and_event(self, tmp_path):
        store = MinimalStore(tmp_path)
        record = store.log_record(
            source_type="gateway_event",
            source_id="evt-001",
            title="Event Title",
            content="Event content",
            event_type="test_event",
        )
        assert record.source_type == "gateway_event"
        assert record.title == "Event Title"

    def test_log_record_with_metadata(self, tmp_path):
        store = MinimalStore(tmp_path)
        record = store.log_record(
            source_type="test",
            source_id="log-test",
            title="Log",
            content="content",
            metadata={"key": "value"},
        )
        assert record.metadata["key"] == "value"


class TestContentFilePath:
    def test_content_file_path_format(self, tmp_path):
        store = MinimalStore(tmp_path)
        store.upsert_record(
            source_type="test",
            source_id="path",
            title="T",
            content="c",
        )
        files = list(store.files_dir.glob("rec-*.txt"))
        assert len(files) == 1


class TestStoredPath:
    def test_stored_path_relative(self, tmp_path):
        store = MinimalStore(tmp_path)
        result = store.upsert_record(
            source_type="test",
            source_id="stored",
            title="T",
            content="c",
        )
        stored = result.content_path
        assert not stored.startswith("/")

    def test_resolve_content_path_absolute(self, tmp_path):
        store = MinimalStore(tmp_path)
        result = store.upsert_record(
            source_type="test",
            source_id="abs",
            title="T",
            content="c",
        )
        resolved = store._resolve_content_path(result.content_path)
        assert resolved.exists()
        assert resolved.read_text() == "c"


class TestEdgeCases:
    def test_upsert_record_empty_content(self, tmp_path):
        store = MinimalStore(tmp_path)
        result = store.upsert_record(
            source_type="test",
            source_id="empty",
            title="Empty",
            content="",
        )
        assert result.content == ""

    def test_upsert_record_unicode_content(self, tmp_path):
        store = MinimalStore(tmp_path)
        result = store.upsert_record(
            source_type="test",
            source_id="unicode",
            title="Unicode",
            content="你好 🌍 مرحبا",
        )
        assert "你好" in result.content

    def test_upsert_record_special_chars_in_source(self, tmp_path):
        store = MinimalStore(tmp_path)
        result = store.upsert_record(
            source_type="test",
            source_id="special!@#$%",
            title="Special",
            content="content",
        )
        assert result.source_id == "special!@#$%"
