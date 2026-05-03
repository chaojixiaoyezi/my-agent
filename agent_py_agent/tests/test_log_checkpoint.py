"""测试 log_analysis ingest/checkpoint.py

测试检查点存储逻辑：
- Checkpoint: 检查点数据结构
- CheckpointStore: JSON 检查点存储
- safe_source_id: 来源 ID 安全化
- write_json_atomic: 原子写入
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_py_agent.agent.log_analysis.ingest.checkpoint import (
    Checkpoint,
    CheckpointStore,
    safe_source_id,
    write_json_atomic,
)


class TestCheckpoint:
    """测试 Checkpoint 数据结构"""

    def test_basic_init(self):
        """测试基本初始化"""
        cp = Checkpoint(
            source_id="waf-prod",
            cursor_kind="offset",
            cursor={"offset": 100},
            last_committed_batch_id="batch-001",
            last_event_time="2026-05-01T10:00:00Z",
            updated_at="2026-05-01T12:00:00Z",
        )
        assert cp.source_id == "waf-prod"
        assert cp.cursor_kind == "offset"
        assert cp.cursor["offset"] == 100
        assert cp.last_committed_batch_id == "batch-001"

    def test_to_dict(self):
        """测试 to_dict 方法"""
        cp = Checkpoint(
            source_id="test-source",
            cursor_kind="time",
            cursor={"time": "2026-05-01T10:00:00Z"},
            last_committed_batch_id="batch-002",
            last_event_time=None,
            updated_at="2026-05-01T12:00:00Z",
        )
        d = cp.to_dict()
        assert isinstance(d, dict)
        assert d["source_id"] == "test-source"
        assert d["cursor_kind"] == "time"
        assert d["last_event_time"] is None

    def test_cursor_as_dict(self):
        """测试 cursor 是字典"""
        cp = Checkpoint(
            source_id="cursor-test",
            cursor_kind="offset",
            cursor={"offset": 50, "line": 100},
            last_committed_batch_id="batch-003",
            last_event_time="2026-05-01T10:00:00Z",
            updated_at="2026-05-01T12:00:00Z",
        )
        assert isinstance(cp.cursor, dict)
        assert cp.cursor["offset"] == 50


class TestCheckpointStoreInit:
    """测试 CheckpointStore 初始化"""

    def test_init_creates_root(self):
        """测试初始化创建根目录"""
        store = CheckpointStore("/tmp/ckpt_test_root")
        assert store.root == Path("/tmp/ckpt_test_root")
        assert store.checkpoints_dir.name == "checkpoints"

    def test_init_with_pathlib(self, tmp_path):
        """测试使用 Path 对象初始化"""
        store = CheckpointStore(tmp_path)
        assert store.root == tmp_path
        assert store.checkpoints_dir.parent == tmp_path


class TestCheckpointStorePathFor:
    """测试 path_for 方法"""

    @pytest.mark.parametrize("source_id,expected_name", [
        ("waf-prod", "waf-prod.json"),
        ("waf prod", "waf_prod.json"),
        ("waf@prod#1", None),  # special chars sanitized
    ], ids=["simple", "spaces", "special_chars"])
    def test_path_for(self, tmp_path, source_id, expected_name):
        """测试路径生成"""
        store = CheckpointStore(tmp_path)
        path = store.path_for(source_id)
        if expected_name:
            assert path.name == expected_name
        else:
            assert "/" not in path.name
            assert "@" not in path.name
            assert "#" not in path.name


class TestSafeSourceId:
    """测试 safe_source_id 函数"""

    @pytest.mark.parametrize("input_val,expected", [
        ("waf-prod", "waf-prod"),
        ("source_123", "source_123"),
        ("firewall.log", "firewall.log"),
        ("waf prod", "waf_prod"),
        ("log source", "log_source"),
        ("waf@prod", "waf_prod"),
        ("log#1", "log_1"),
        ("test/file", "test_file"),
        ("", "unknown"),
        ("   ", "unknown"),
        ("@@@", "_"),
        ("###", "_"),
    ], ids=["valid_hyphen", "valid_underscore", "valid_dot", "spaces", "spaces2", "at_sign", "hash", "slash", "empty", "whitespace", "only_at", "only_hash"])
    def test_safe_source_id(self, input_val, expected):
        """测试来源 ID 安全化"""
        assert safe_source_id(input_val) == expected


class TestCheckpointStoreLoad:
    """测试 load 方法"""

    def test_load_nonexistent(self, tmp_path):
        """测试加载不存在的检查点"""
        store = CheckpointStore(tmp_path)
        result = store.load("nonexistent-source")
        assert result == {}

    def test_load_existing(self, tmp_path):
        """测试加载已存在的检查点"""
        store = CheckpointStore(tmp_path)
        store.commit(
            source_id="existing-source",
            cursor_kind="offset",
            cursor={"offset": 100},
            last_committed_batch_id="batch-001",
            last_event_time="2026-05-01T10:00:00Z",
        )
        result = store.load("existing-source")
        assert result["source_id"] == "existing-source"
        assert result["cursor"]["offset"] == 100

    def test_load_with_spaces_in_id(self, tmp_path):
        """测试加载带空格的来源 ID"""
        store = CheckpointStore(tmp_path)
        store.commit(
            source_id="source with spaces",
            cursor_kind="time",
            cursor={"time": "2026-05-01T10:00:00Z"},
            last_committed_batch_id="batch-002",
            last_event_time=None,
        )
        result = store.load("source with spaces")
        assert result["source_id"] == "source with spaces"

    def test_load_corrupted_json(self, tmp_path):
        """测试加载损坏的 JSON 文件"""
        store = CheckpointStore(tmp_path)
        path = store.path_for("corrupted-source")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ invalid json", encoding="utf-8")
        result = store.load("corrupted-source")
        assert result == {}


class TestCheckpointStoreCommit:
    """测试 commit 方法"""

    def test_commit_creates_checkpoint(self, tmp_path):
        """测试 commit 创建检查点"""
        store = CheckpointStore(tmp_path)
        cp = store.commit(
            source_id="commit-test",
            cursor_kind="offset",
            cursor={"offset": 50},
            last_committed_batch_id="batch-001",
            last_event_time="2026-05-01T10:00:00Z",
        )
        assert isinstance(cp, Checkpoint)
        assert cp.source_id == "commit-test"
        assert cp.cursor["offset"] == 50

    def test_commit_creates_file(self, tmp_path):
        """测试 commit 创建文件"""
        store = CheckpointStore(tmp_path)
        store.commit(
            source_id="file-test",
            cursor_kind="offset",
            cursor={"offset": 100},
            last_committed_batch_id="batch-002",
            last_event_time="2026-05-01T10:00:00Z",
        )
        path = store.path_for("file-test")
        assert path.exists()

    def test_commit_updates_existing(self, tmp_path):
        """测试 commit 更新已存在的检查点"""
        store = CheckpointStore(tmp_path)
        store.commit(
            source_id="update-test",
            cursor_kind="offset",
            cursor={"offset": 0},
            last_committed_batch_id="batch-001",
            last_event_time="2026-05-01T10:00:00Z",
        )
        store.commit(
            source_id="update-test",
            cursor_kind="offset",
            cursor={"offset": 200},
            last_committed_batch_id="batch-002",
            last_event_time="2026-05-01T11:00:00Z",
        )
        result = store.load("update-test")
        assert result["cursor"]["offset"] == 200
        assert result["last_committed_batch_id"] == "batch-002"

    @pytest.mark.parametrize("last_event_time,expected_none", [
        (None, True),
        ("2026-05-01T10:00:00Z", False),
    ], ids=["none_event_time", "valid_event_time"])
    def test_commit_last_event_time(self, tmp_path, last_event_time, expected_none):
        """测试 commit 时 last_event_time 处理"""
        store = CheckpointStore(tmp_path)
        cp = store.commit(
            source_id="time-test",
            cursor_kind="offset",
            cursor={"offset": 50},
            last_committed_batch_id="batch-003",
            last_event_time=last_event_time,
        )
        assert (cp.last_event_time is None) == expected_none

    def test_commit_timestamp_set(self, tmp_path):
        """测试 commit 设置 updated_at"""
        store = CheckpointStore(tmp_path)
        cp = store.commit(
            source_id="timestamp-test",
            cursor_kind="time",
            cursor={"time": "2026-05-01T12:00:00Z"},
            last_committed_batch_id="batch-004",
            last_event_time="2026-05-01T12:00:00Z",
        )
        assert cp.updated_at != ""


class TestWriteJsonAtomic:
    """测试 write_json_atomic 函数"""

    @pytest.mark.parametrize("data", [
        {"key": "value"},
        {"test": "content", "number": 42},
        {"nested": True},
        {"v": 1},
        {"items": list(range(1000))},
        {"chinese": "中文", "unicode": "🎉"},
    ], ids=["simple", "multi_field", "nested", "overwrite", "large_payload", "special_chars"])
    def test_atomic_write_content(self, tmp_path, data):
        """测试原子写入内容正确"""
        target = tmp_path / f"atomic_{id(data)}.json"
        write_json_atomic(target, data)
        assert target.exists()
        with open(target, encoding="utf-8") as f:
            loaded = json.load(f)
        assert loaded == data

    def test_atomic_write_creates_parent_dirs(self, tmp_path):
        """测试原子写入创建父目录"""
        target = tmp_path / "subdir" / "nested" / "file.json"
        write_json_atomic(target, {"nested": True})
        assert target.exists()

    def test_atomic_overwrite(self, tmp_path):
        """测试原子覆盖"""
        target = tmp_path / "overwrite.json"
        write_json_atomic(target, {"v": 1})
        write_json_atomic(target, {"v": 2})
        with open(target, encoding="utf-8") as f:
            data = json.load(f)
        assert data["v"] == 2


class TestBoundaryCases:
    """测试边界场景"""

    def test_checkpoint_none_values(self):
        """测试检查点包含 None 值"""
        cp = Checkpoint(
            source_id="none-test",
            cursor_kind="offset",
            cursor={"offset": 0},
            last_committed_batch_id=None,
            last_event_time=None,
            updated_at="2026-05-01T12:00:00Z",
        )
        d = cp.to_dict()
        assert d["last_committed_batch_id"] is None
        assert d["last_event_time"] is None

    def test_checkpoint_empty_cursor(self):
        """测试空 cursor"""
        cp = Checkpoint(
            source_id="empty-cursor",
            cursor_kind="none",
            cursor={},
            last_committed_batch_id="batch-001",
            last_event_time=None,
            updated_at="2026-05-01T12:00:00Z",
        )
        assert cp.cursor == {}

    def test_checkpoint_nested_cursor(self):
        """测试嵌套 cursor"""
        cp = Checkpoint(
            source_id="nested-cursor",
            cursor_kind="complex",
            cursor={
                "offset": 100,
                "metadata": {"source": "test", "extra": {"deep": "value"}},
            },
            last_committed_batch_id="batch-002",
            last_event_time="2026-05-01T10:00:00Z",
            updated_at="2026-05-01T12:00:00Z",
        )
        d = cp.to_dict()
        assert d["cursor"]["metadata"]["extra"]["deep"] == "value"

    def test_multiple_sources_independent(self, tmp_path):
        """测试多个来源相互独立"""
        store = CheckpointStore(tmp_path)
        store.commit(
            source_id="source-a",
            cursor_kind="offset",
            cursor={"offset": 100},
            last_committed_batch_id="a-batch",
            last_event_time=None,
        )
        store.commit(
            source_id="source-b",
            cursor_kind="offset",
            cursor={"offset": 200},
            last_committed_batch_id="b-batch",
            last_event_time=None,
        )
        result_a = store.load("source-a")
        result_b = store.load("source-b")
        assert result_a["cursor"]["offset"] == 100
        assert result_b["cursor"]["offset"] == 200

    def test_source_id_with_path_separators(self, tmp_path):
        """测试带路径分隔符的来源 ID"""
        store = CheckpointStore(tmp_path)
        store.commit(
            source_id="logs/2026/05/01",
            cursor_kind="time",
            cursor={"time": "2026-05-01T00:00:00Z"},
            last_committed_batch_id="daily-batch",
            last_event_time=None,
        )
        result = store.load("logs/2026/05/01")
        assert result["source_id"] == "logs/2026/05/01"

    def test_empty_checkpoint_dir_on_init(self, tmp_path):
        """测试初始化不创建 checkpoint 目录（直到第一次 commit）"""
        store = CheckpointStore(tmp_path)
        assert not store.checkpoints_dir.exists()
        store.commit(
            source_id="lazy-test",
            cursor_kind="offset",
            cursor={"offset": 0},
            last_committed_batch_id="batch",
            last_event_time=None,
        )
        assert store.checkpoints_dir.exists()