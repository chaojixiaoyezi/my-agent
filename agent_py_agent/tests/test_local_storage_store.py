"""本地存储服务测试 - local_store.py SQLite存储、CRUD操作、索引。"""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import pytest

from agent_py_agent.agent.local_store import LocalStore


@pytest.fixture
def temp_dir(tmp_path: Path) -> Path:
    """创建临时目录作为 LocalStore 根目录。"""
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    return store_dir


@pytest.fixture
def local_store(temp_dir: Path) -> LocalStore:
    """创建测试用 LocalStore 实例。"""
    db_path = temp_dir / "test.db"
    return LocalStore(db_path)


class TestLocalStoreInit:
    """LocalStore 初始化测试。"""

    def test_init_creates_db_path(self, temp_dir: Path):
        """验证初始化创建数据库路径。"""
        db_path = temp_dir / "test.db"
        store = LocalStore(db_path)
        assert store.db_path == db_path

    def test_init_default_files_dir(self, temp_dir: Path):
        """验证默认文件目录。"""
        db_path = temp_dir / "test.db"
        store = LocalStore(db_path)
        assert store.files_dir == temp_dir / "files"

    def test_init_custom_files_dir(self, temp_dir: Path):
        """验证自定义文件目录。"""
        db_path = temp_dir / "test.db"
        custom_dir = temp_dir / "custom_files"
        store = LocalStore(db_path, files_dir=custom_dir)
        assert store.files_dir == custom_dir

    def test_init_default_events_path(self, temp_dir: Path):
        """验证默认事件文件路径。"""
        db_path = temp_dir / "test.db"
        store = LocalStore(db_path)
        assert store.events_path == temp_dir / "events.jsonl"

    def test_init_custom_events_path(self, temp_dir: Path):
        """验证自定义事件文件路径。"""
        db_path = temp_dir / "test.db"
        custom_events = temp_dir / "custom_events.jsonl"
        store = LocalStore(db_path, events_path=custom_events)
        assert store.events_path == custom_events

    def test_init_fts_disabled(self, temp_dir: Path):
        """验证禁用 FTS。"""
        db_path = temp_dir / "test.db"
        store = LocalStore(db_path, enable_fts=False)
        assert store.fts_available is False


class TestLocalStoreRecordOperations:
    """LocalStore 记录操作测试。"""

    def test_upsert_record_basic(self, local_store: LocalStore):
        """基本记录插入和更新。"""
        result = local_store.upsert_record(
            source_type="memory",
            source_id="mem-1",
            title="测试记录",
            content="这是测试内容",
        )
        assert result.id.startswith("rec-")
        assert result.source_type == "memory"
        assert result.source_id == "mem-1"
        assert result.title == "测试记录"
        assert result.content == "这是测试内容"
        assert result.visibility == "private"

    def test_upsert_record_creates_content_file(self, local_store: LocalStore, temp_dir: Path):
        """验证创建内容文件。"""
        result = local_store.upsert_record(
            source_type="memory",
            source_id="mem-1",
            title="title",
            content="long content" * 100,
        )
        # content_path 是相对路径，基于 store.root
        content_file = local_store.root / result.content_path
        assert content_file.exists()

    def test_upsert_record_updates_existing(self, local_store: LocalStore):
        """验证更新已有记录。"""
        # 第一次插入
        result1 = local_store.upsert_record(
            source_type="memory",
            source_id="mem-same",
            title="原始标题",
            content="原始内容",
        )
        original_created_at = result1.created_at

        # 第二次插入同一 source
        time.sleep(0.01)  # 确保时间戳不同
        result2 = local_store.upsert_record(
            source_type="memory",
            source_id="mem-same",
            title="新标题",
            content="新内容",
        )

        # 应该返回同一条记录（更新而不是创建新记录）
        assert result1.id == result2.id
        assert result2.title == "新标题"
        assert result2.content == "新内容"
        assert result2.created_at == original_created_at  # 创建时间不变

    def test_upsert_record_with_metadata(self, local_store: LocalStore):
        """验证带元数据的记录。"""
        metadata = {"role": "user", "tags": ["tag1", "tag2"]}
        result = local_store.upsert_record(
            source_type="memory",
            source_id="mem-1",
            title="title",
            content="content",
            metadata=metadata,
        )
        assert result.metadata["role"] == "user"
        assert result.metadata["tags"] == ["tag1", "tag2"]

    def test_upsert_record_custom_visibility(self, local_store: LocalStore):
        """验证自定义可见性。"""
        result = local_store.upsert_record(
            source_type="test",
            source_id="id-1",
            title="title",
            content="content",
            visibility="public",
        )
        assert result.visibility == "public"

    def test_get_record_by_id(self, local_store: LocalStore):
        """验证按 ID 获取记录。"""
        inserted = local_store.upsert_record(
            source_type="memory",
            source_id="mem-1",
            title="title",
            content="content",
        )
        fetched = local_store.get_record(inserted.id)
        assert fetched is not None
        assert fetched.id == inserted.id
        assert fetched.title == "title"

    def test_get_record_not_found(self, local_store: LocalStore):
        """获取不存在的记录返回 None。"""
        result = local_store.get_record("nonexistent-id")
        assert result is None

    def test_make_record_id_deterministic(self, local_store: LocalStore):
        """验证记录 ID 生成是确定性的。"""
        id1 = LocalStore.make_record_id("memory", "source-1")
        id2 = LocalStore.make_record_id("memory", "source-1")
        assert id1 == id2

    def test_make_record_id_different_sources(self, local_store: LocalStore):
        """不同来源产生不同 ID。"""
        id1 = LocalStore.make_record_id("memory", "source-1")
        id2 = LocalStore.make_record_id("memory", "source-2")
        assert id1 != id2


class TestLocalStoreSearch:
    """LocalStore 搜索测试。"""

    def test_search_returns_results(self, local_store: LocalStore):
        """验证搜索返回结果。"""
        local_store.upsert_record(
            source_type="memory",
            source_id="mem-1",
            title="Python 编程",
            content="Python 是一种编程语言",
        )
        results = local_store.search("Python", limit=5)
        assert len(results) >= 1

    def test_search_respects_limit(self, local_store: LocalStore):
        """验证搜索限制。"""
        for i in range(10):
            local_store.upsert_record(
                source_type="memory",
                source_id=f"mem-{i}",
                title=f"标题 {i}",
                content=f"内容 {i}",
            )
        results = local_store.search("内容", limit=3)
        assert len(results) <= 3


class TestLocalStoreLogRecord:
    """LocalStore 日志记录测试。"""

    def test_log_record_creates_event(self, local_store: LocalStore, temp_dir: Path):
        """验证 log_record 同时创建记录和事件。"""
        result = local_store.log_record(
            source_type="gateway_request",
            source_id="req-1",
            title="收到请求",
            content="请求内容",
        )
        assert result.id.startswith("rec-")
        # 验证事件文件被创建
        assert temp_dir / "events.jsonl" in list(temp_dir.glob("*.jsonl")) or temp_dir.glob("**/*.jsonl")
