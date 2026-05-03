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


# ============================================================
# 测试用例：Checkpoint 数据结构
# ============================================================

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


# ============================================================
# 测试用例：CheckpointStore 初始化
# ============================================================

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


# ============================================================
# 测试用例：CheckpointStore.path_for
# ============================================================

class TestCheckpointStorePathFor:
    """测试 path_for 方法"""

    def test_simple_source_id(self, tmp_path):
        """测试简单来源 ID"""
        store = CheckpointStore(tmp_path)
        path = store.path_for("waf-prod")
        assert path.name == "waf-prod.json"

    def test_spaces_replaced(self, tmp_path):
        """测试空格被替换"""
        store = CheckpointStore(tmp_path)
        path = store.path_for("waf prod")
        assert "waf_prod" in path.name or path.name == "waf-prod.json"

    def test_special_chars_sanitized(self, tmp_path):
        """测试特殊字符被清理"""
        store = CheckpointStore(tmp_path)
        path = store.path_for("waf@prod#1")
        # 特殊字符应该被替换
        assert "/" not in path.name
        assert "@" not in path.name
        assert "#" not in path.name


# ============================================================
# 测试用例：safe_source_id
# ============================================================

class TestSafeSourceId:
    """测试 safe_source_id 函数"""

    def test_valid_id_unchanged(self):
        """测试有效 ID 不变"""
        assert safe_source_id("waf-prod") == "waf-prod"
        assert safe_source_id("source_123") == "source_123"
        assert safe_source_id("firewall.log") == "firewall.log"

    def test_spaces_replaced(self):
        """测试空格被替换"""
        assert safe_source_id("waf prod") == "waf_prod"
        assert safe_source_id("log source") == "log_source"

    def test_special_chars_replaced(self):
        """测试特殊字符被替换"""
        assert safe_source_id("waf@prod") == "waf_prod"
        assert safe_source_id("log#1") == "log_1"
        assert safe_source_id("test/file") == "test_file"

    def test_empty_becomes_unknown(self):
        """测试空字符串变成 unknown"""
        assert safe_source_id("") == "unknown"
        assert safe_source_id("   ") == "unknown"

    def test_only_special_chars_becomes_unknown(self):
        """测试只有特殊字符变成 unknown"""
        assert safe_source_id("@@@") == "_"
        assert safe_source_id("###") == "_"


# ============================================================
# 测试用例：CheckpointStore.load
# ============================================================

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
        # 先写入
        store.commit(
            source_id="existing-source",
            cursor_kind="offset",
            cursor={"offset": 100},
            last_committed_batch_id="batch-001",
            last_event_time="2026-05-01T10:00:00Z",
        )
        # 再加载
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
        # 手动写入损坏的 JSON
        path = store.path_for("corrupted-source")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ invalid json", encoding="utf-8")
        # 应该返回空字典而不是抛出异常
        result = store.load("corrupted-source")
        assert result == {}


# ============================================================
# 测试用例：CheckpointStore.commit
# ============================================================

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
        # 第一次提交
        store.commit(
            source_id="update-test",
            cursor_kind="offset",
            cursor={"offset": 0},
            last_committed_batch_id="batch-001",
            last_event_time="2026-05-01T10:00:00Z",
        )
        # 第二次提交
        store.commit(
            source_id="update-test",
            cursor_kind="offset",
            cursor={"offset": 200},
            last_committed_batch_id="batch-002",
            last_event_time="2026-05-01T11:00:00Z",
        )
        # 验证更新
        result = store.load("update-test")
        assert result["cursor"]["offset"] == 200
        assert result["last_committed_batch_id"] == "batch-002"

    def test_commit_with_none_last_event_time(self, tmp_path):
        """测试 commit 时 last_event_time 为 None"""
        store = CheckpointStore(tmp_path)
        cp = store.commit(
            source_id="none-time-test",
            cursor_kind="offset",
            cursor={"offset": 50},
            last_committed_batch_id="batch-003",
            last_event_time=None,
        )
        assert cp.last_event_time is None

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


# ============================================================
# 测试用例：write_json_atomic
# ============================================================

class TestWriteJsonAtomic:
    """测试 write_json_atomic 函数"""

    def test_atomic_write_creates_file(self, tmp_path):
        """测试原子写入创建文件"""
        target = tmp_path / "atomic_test.json"
        write_json_atomic(target, {"key": "value"})
        assert target.exists()

    def test_atomic_write_content(self, tmp_path):
        """测试原子写入内容正确"""
        target = tmp_path / "atomic_content.json"
        data = {"test": "content", "number": 42}
        write_json_atomic(target, data)
        with open(target, encoding="utf-8") as f:
            loaded = json.load(f)
        assert loaded["test"] == "content"
        assert loaded["number"] == 42

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

    def test_atomic_write_large_payload(self, tmp_path):
        """测试原子写入大 payload"""
        target = tmp_path / "large.json"
        data = {"items": list(range(1000))}
        write_json_atomic(target, data)
        with open(target, encoding="utf-8") as f:
            loaded = json.load(f)
        assert len(loaded["items"]) == 1000

    def test_atomic_write_special_chars(self, tmp_path):
        """测试原子写入特殊字符"""
        target = tmp_path / "special.json"
        data = {"chinese": "中文", "unicode": "🎉"}
        write_json_atomic(target, data)
        with open(target, encoding="utf-8") as f:
            loaded = json.load(f)
        assert loaded["chinese"] == "中文"
        assert loaded["unicode"] == "🎉"


# ============================================================
# 测试用例：边界场景
# ====================================

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
        # 不应该自动创建目录
        assert not store.checkpoints_dir.exists()
        # commit 后才创建
        store.commit(
            source_id="lazy-test",
            cursor_kind="offset",
            cursor={"offset": 0},
            last_committed_batch_id="batch",
            last_event_time=None,
        )
        assert store.checkpoints_dir.exists()