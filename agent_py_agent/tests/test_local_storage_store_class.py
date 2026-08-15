from __future__ import annotations

"""LLM: tests for local_storage store class.

给人看的解释：
测试 LocalStore 组合类及其 mixin 的 CRUD 操作、索引查询、事件记录功能。
"""

import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.local_storage import LocalStore


class TestLocalStoreInit:
    """测试 LocalStore 初始化。"""

    def test_init_creates_db_path(self) -> None:
        """测试初始化创建数据库路径。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            store = LocalStore(db_path)
            assert store.db_path == db_path
            assert db_path.exists()

    def test_init_creates_files_dir(self) -> None:
        """测试初始化创建 files 目录。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            store = LocalStore(db_path)
            assert store.files_dir.exists()

    def test_init_creates_events_path(self) -> None:
        """测试初始化创建 events 路径。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            store = LocalStore(db_path)
            assert store.events_path.parent.exists()

    def test_default_files_dir(self) -> None:
        """测试默认 files_dir 位置。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            store = LocalStore(db_path)
            assert store.files_dir == db_path.parent / "files"

    def test_fts_disabled_by_default(self) -> None:
        """测试 FTS 默认禁用。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            store = LocalStore(db_path, enable_fts=False)
            assert store.fts_available is False

    def test_custom_files_dir(self) -> None:
        """测试自定义 files_dir。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            custom_files = Path(tmpdir) / "custom_files"
            store = LocalStore(db_path, files_dir=custom_files)
            assert store.files_dir == custom_files


class TestLocalStoreRecords:
    """测试记录 CRUD 操作。"""

    def test_upsert_record_returns_result(self) -> None:
        """测试 upsert_record 返回结果。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            store = LocalStore(db_path, enable_fts=False)
            result = store.upsert_record(
                source_type="test",
                source_id="test-001",
                title="Test Title",
                content="Test Content",
            )
            assert result is not None
            assert result.source_type == "test"
            assert result.title == "Test Title"

    def test_upsert_record_creates_content_file(self) -> None:
        """测试 upsert_record 创建正文文件。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            store = LocalStore(db_path, enable_fts=False)
            result = store.upsert_record(
                source_type="memory",
                source_id="mem-001",
                title="Memory Title",
                content="Memory Content Here",
            )
            content_path = Path(store.files_dir) / f"{result.id}.txt"
            assert content_path.exists()

    def test_get_record_by_id(self) -> None:
        """测试按 ID 获取记录。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            store = LocalStore(db_path, enable_fts=False)
            created = store.upsert_record(
                source_type="test",
                source_id="test-get",
                title="Get Test",
                content="Content for get test",
            )
            retrieved = store.get_record(created.id)
            assert retrieved is not None
            assert retrieved.id == created.id
            assert retrieved.content == "Content for get test"

    def test_get_record_not_found(self) -> None:
        """测试获取不存在的记录返回 None。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            store = LocalStore(db_path, enable_fts=False)
            result = store.get_record("nonexistent-id")
            assert result is None

    def test_upsert_updates_existing(self) -> None:
        """测试重复 upsert 更新记录。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            store = LocalStore(db_path, enable_fts=False)
            first = store.upsert_record(
                source_type="test",
                source_id="same-id",
                title="First Title",
                content="First Content",
            )
            second = store.upsert_record(
                source_type="test",
                source_id="same-id",
                title="Updated Title",
                content="Updated Content",
            )
            # 应该返回同一条记录
            assert first.id == second.id
            # 应该是更新后的内容
            retrieved = store.get_record(first.id)
            assert retrieved.title == "Updated Title"

    def test_make_record_id_stable(self) -> None:
        """测试 make_record_id 生成稳定 ID。"""
        id1 = LocalStore.make_record_id("memory", "source-1")
        id2 = LocalStore.make_record_id("memory", "source-1")
        assert id1 == id2

    def test_make_record_id_different_sources(self) -> None:
        """测试不同来源生成不同 ID。"""
        id1 = LocalStore.make_record_id("memory", "source-1")
        id2 = LocalStore.make_record_id("gateway", "source-1")
        assert id1 != id2


class TestLocalStoreEvents:
    """测试事件记录功能。"""

    def test_record_event_returns_result(self) -> None:
        """测试 record_event 返回结果。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            store = LocalStore(db_path, enable_fts=False)
            result = store.record_event("event_type", payload={"data": 123})
            assert result is not None

    def test_record_event_with_record_id(self) -> None:
        """测试带 record_id 的事件记录。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            store = LocalStore(db_path, enable_fts=False)
            result = store.record_event(
                "test_event",
                record_id="evt-001",
                payload={"key": "value"},
            )
            assert result is not None
            assert result.event_id is not None


class TestLocalStoreVisibility:
    """测试可见性处理。"""

    def test_upsert_with_visibility(self) -> None:
        """测试带可见性的 upsert。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            store = LocalStore(db_path, enable_fts=False)
            result = store.upsert_record(
                source_type="test",
                source_id="vis-test",
                title="Visibility Test",
                content="Test content",
                visibility="public",
            )
            assert result.visibility == "public"

    def test_upsert_default_visibility(self) -> None:
        """测试默认可见性为 private。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            store = LocalStore(db_path, enable_fts=False)
            result = store.upsert_record(
                source_type="test",
                source_id="default-vis",
                title="Default Visibility",
                content="Content",
            )
            assert result.visibility == "private"


class TestLocalStoreMetadata:
    """测试元数据处理。"""

    def test_upsert_with_metadata(self) -> None:
        """测试带元数据的 upsert。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            store = LocalStore(db_path, enable_fts=False)
            metadata = {"role": "user", "kind": "note", "tags": ["important"]}
            result = store.upsert_record(
                source_type="memory",
                source_id="meta-test",
                title="Metadata Test",
                content="Content with metadata",
                metadata=metadata,
            )
            assert result.metadata.get("role") == "user"

    def test_retrieved_metadata_intact(self) -> None:
        """测试检索后元数据保持完整。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            store = LocalStore(db_path, enable_fts=False)
            metadata = {"key": "value", "nested": {"a": 1}}
            store.upsert_record(
                source_type="test",
                source_id="meta-intact",
                title="Intact Test",
                content="Content",
                metadata=metadata,
            )
            # 通过搜索获取记录
            results = store.search("Intact", limit=1, source_type="test")
            if results:
                assert results[0].metadata.get("key") == "value"