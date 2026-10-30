# LLM: FTS维护使用真实临时SQLite验证返回数、完成键和MATCH；其它既有维护单元测试保持不变。
# 模块用途: 验证分批FTS重建、统计、重置和完整性检查。
"""本地存储维护测试 - maintenance.py FTS 重建、统计、重置、完整性检查。"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_py_agent.agent.local_storage.store import LocalStore


# LLM: 不把旧单次commit次数当分批状态机的业务合同，保留真实结果和完成标记覆盖。
# 类用途: 检查FTS关闭、空库及有记录重建。
class TestRebuildFts:
    """rebuild_fts FTS5 索引重建测试。"""

    def test_rebuild_fts_when_disabled(self, make_fake_store_func):
        """验证 FTS 不可用时返回 0。"""
        store = make_fake_store_func(fts_available=False)
        assert store.rebuild_fts() == 0

    # LLM: 真实空库也须走影子发表并原子写完成键，而不是Mock返回0。
    # 函数用途: 验证空记录重建的业务结果及耐久完成状态。
    def test_rebuild_fts_empty_records(self, tmp_path):
        store = LocalStore(tmp_path / 'empty.sqlite3')
        assert store.fts_available
        result = store.rebuild_fts()
        assert result == 0
        with store._connection() as conn:
            assert conn.execute("SELECT value FROM metadata WHERE key='records_fts_rebuild_pending'").fetchone()[0] == 'false'
            assert conn.execute('SELECT phase FROM maintenance_fts_job WHERE slot=1').fetchone()[0] == 'complete'
            assert conn.execute('SELECT COUNT(*) FROM records_fts').fetchone()[0] == 0
        assert store._fts_needs_rebuild is False

    # LLM: 原两记录数量和完成键合同仍在，额外检查实际FTS MATCH及重开后的持久状态。
    # 函数用途: 验证真实正文和影子重建结果一致。
    def test_rebuild_fts_with_records(self, tmp_path):
        store = LocalStore(tmp_path / 'records.sqlite3')
        assert store.fts_available
        for index in range(2):
            store.upsert_record(source_type='memory', source_id=f'source-{index}',
                                title=f'Title {index}', content=f'needle body {index}')
        result = store.rebuild_fts()
        assert result == 2
        with store._connection() as conn:
            assert conn.execute("SELECT value FROM metadata WHERE key='records_fts_rebuild_pending'").fetchone()[0] == 'false'
            assert conn.execute("SELECT COUNT(*) FROM records_fts WHERE records_fts MATCH 'needle'").fetchone()[0] == 2
        assert store._fts_needs_rebuild is False
        reopened = LocalStore(store.db_path)
        assert len(reopened._search_fts('needle', limit=10, source_type=None, visibility=None)) == 2


class TestReset:
    """reset 重置测试。"""

    def test_reset_default_keeps_content_files(self, make_fake_store_func, tmp_path):
        """验证默认不删除正文文件。"""
        store = make_fake_store_func(fts_available=True, files_dir=tmp_path / "files", events_path=tmp_path / "events.jsonl")
        (store.files_dir).mkdir(parents=True, exist_ok=True)
        ((store.files_dir) / "test.txt").write_text("content")

        mock_conn = MagicMock()

        class ConnCtx:
            def __enter__(self):
                return mock_conn
            def __exit__(self, *args):
                pass

        store._connection = lambda: ConnCtx()
        store.reset()

        assert ((store.files_dir) / "test.txt").exists()

    def test_reset_clears_events(self, make_fake_store_func, tmp_path):
        """验证重置时清空 events 文件。"""
        store = make_fake_store_func(fts_available=True, files_dir=tmp_path / "files", events_path=tmp_path / "events.jsonl")
        (store.events_path).parent.mkdir(parents=True, exist_ok=True)
        (store.events_path).write_text("existing event", encoding="utf-8")

        mock_conn = MagicMock()

        class ConnCtx:
            def __enter__(self):
                return mock_conn
            def __exit__(self, *args):
                pass

        store._connection = lambda: ConnCtx()
        store.reset()

        assert (store.events_path).read_text(encoding="utf-8") == ""

    def test_reset_with_remove_content(self, make_fake_store_func, tmp_path):
        """验证 remove_content_files=True 时删除正文。"""
        store = make_fake_store_func(fts_available=True, files_dir=tmp_path / "files", events_path=tmp_path / "events.jsonl")
        (store.files_dir).mkdir(parents=True, exist_ok=True)
        test_file = (store.files_dir) / "test.txt"
        test_file.write_text("content")

        mock_conn = MagicMock()

        class ConnCtx:
            def __enter__(self):
                return mock_conn
            def __exit__(self, *args):
                pass

        store._connection = lambda: ConnCtx()
        store.reset(remove_content_files=True)
        assert not test_file.exists()


class TestCountRecords:
    """count_records 记录统计测试。"""

    def test_count_records_all(self, make_fake_store_func):
        """验证统计所有记录。"""
        from unittest.mock import MagicMock
        store = make_fake_store_func()
        mock_result = MagicMock()
        mock_result.fetchone.return_value = (5,)
        mock_conn = MagicMock()
        mock_conn.execute.return_value = mock_result

        class ConnCtx:
            def __enter__(self):
                return mock_conn
            def __exit__(self, *args):
                pass

        store._connection = lambda: ConnCtx()
        result = store.count_records()
        assert result == 5

    def test_count_records_with_source_type(self, make_fake_store_func):
        """验证按 source_type 过滤统计。"""
        from unittest.mock import MagicMock
        store = make_fake_store_func()
        mock_result = MagicMock()
        mock_result.fetchone.return_value = (3,)
        mock_conn = MagicMock()
        mock_conn.execute.return_value = mock_result

        class ConnCtx:
            def __enter__(self):
                return mock_conn
            def __exit__(self, *args):
                pass

        store._connection = lambda: ConnCtx()
        result = store.count_records(source_type="gateway_request")
        assert result == 3


class TestSourceCounts:
    """source_counts 来源统计测试。"""

    def test_source_counts_empty(self, make_fake_store_func):
        """验证空记录时返回空字典。"""
        store = make_fake_store_func()
        mock_result = MagicMock()
        mock_result.fetchall.return_value = []
        mock_conn = MagicMock()
        mock_conn.execute.return_value = mock_result

        class ConnCtx:
            def __enter__(self):
                return mock_conn
            def __exit__(self, *args):
                pass

        store._connection = lambda: ConnCtx()
        result = store.source_counts()
        assert result == {}

    def test_source_counts_with_data(self, make_fake_store_func):
        """验证正常返回各来源计数。"""
        from unittest.mock import MagicMock
        store = make_fake_store_func()
        mock_result = MagicMock()
        mock_result.fetchall.return_value = [
            {"source_type": "gateway_request", "count": 10},
            {"source_type": "memory", "count": 5},
        ]
        mock_conn = MagicMock()
        mock_conn.execute.return_value = mock_result

        class ConnCtx:
            def __enter__(self):
                return mock_conn
            def __exit__(self, *args):
                pass

        store._connection = lambda: ConnCtx()
        result = store.source_counts()
        assert result["gateway_request"] == 10
        assert result["memory"] == 5


class TestMissingContentFiles:
    """missing_content_files 缺失文件检查测试。"""

    def test_missing_content_files_none_missing(self, make_fake_store_func, tmp_path):
        """验证无缺失时返回空列表。"""
        from unittest.mock import MagicMock
        store = make_fake_store_func()

        class FakeStore:
            def _resolve_content_path(self, content_path):
                p = Path(content_path) if content_path else tmp_path / "none.txt"
                return p

        test_file = tmp_path / "existing.txt"
        test_file.write_text("content")

        mock_result = MagicMock()
        mock_result.fetchall.return_value = [
            {"id": "r1", "source_type": "mem", "source_id": "s1", "title": "T1", "content_path": str(test_file)}
        ]
        mock_conn = MagicMock()
        mock_conn.execute.return_value = mock_result

        class ConnCtx:
            def __enter__(self):
                return mock_conn
            def __exit__(self, *args):
                pass

        store._connection = lambda: ConnCtx()
        result = store.missing_content_files()
        assert result == []

    def test_missing_content_files_detects_missing(self, make_fake_store_func):
        """验证能检测到缺失文件。"""
        from pathlib import Path
        from unittest.mock import MagicMock
        store = make_fake_store_func()

        mock_result = MagicMock()
        mock_result.fetchall.return_value = [
            {"id": "r1", "source_type": "mem", "source_id": "s1", "title": "T1", "content_path": "/nonexistent/path.txt"}
        ]
        mock_conn = MagicMock()
        mock_conn.execute.return_value = mock_result

        class ConnCtx:
            def __enter__(self):
                return mock_conn
            def __exit__(self, *args):
                pass

        store._connection = lambda: ConnCtx()
        result = store.missing_content_files()
        assert len(result) == 1
        assert result[0]["id"] == "r1"

    def test_missing_content_files_respects_limit(self, make_fake_store_func):
        """验证 limit 参数限制返回数量。"""
        from pathlib import Path
        from unittest.mock import MagicMock
        store = make_fake_store_func()
        mock_result = MagicMock()
        mock_result.fetchall.return_value = [
            {"id": f"r{i}", "source_type": "mem", "source_id": f"s{i}", "title": f"T{i}", "content_path": f"/nonexistent/{i}.txt"}
            for i in range(5)
        ]
        mock_conn = MagicMock()
        mock_conn.execute.return_value = mock_result

        class ConnCtx:
            def __enter__(self):
                return mock_conn
            def __exit__(self, *args):
                pass

        store._connection = lambda: ConnCtx()
        result = store.missing_content_files(limit=2)
        assert len(result) == 2


class TestStats:
    """stats 状态统计测试。"""

    def test_stats_basic(self, make_fake_store_func, tmp_path):
        """验证返回基本状态信息。"""
        store = make_fake_store_func(fts_available=True, db_path=tmp_path / "test.db", files_dir=tmp_path / "files", events_path=tmp_path / "events.jsonl")

        mock_result = MagicMock()
        mock_result.fetchone.return_value = (10,)
        mock_conn = MagicMock()
        mock_conn.execute.return_value = mock_result

        class ConnCtx:
            def __enter__(self):
                return mock_conn
            def __exit__(self, *args):
                pass

        store._connection = lambda: ConnCtx()
        result = store.stats()
        assert "db_path" in result
        assert "record_count" in result
        assert "fts5_enabled" in result