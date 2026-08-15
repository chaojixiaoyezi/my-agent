"""FTS5 中文子串检索钉子(中文检索失灵修复)。

修复前根因:records_fts 用默认 unicode61 分词,一整段汉字=1 个 token,
中文子串/词永远 MATCH 不中(只能靠 LIKE 兜底,零排序)。修复:改用 trigram
分词器,_fts_query 把长中文串拆成重叠 3-gram OR。这组测试钉住:
① 纯 FTS(绕开 LIKE 兜底)能命中中文子串;② 部分重合也能召回;
③ 已有旧 unicode61 索引在重开时自动迁移重建为 trigram。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent_py_agent.agent.local_storage.schema import (
    _FTS_SCHEMA_VERSION,
    _FTS_SCHEMA_VERSION_KEY,
)
from agent_py_agent.agent.local_storage.store import LocalStore


def _store(tmp_path: Path) -> LocalStore:
    return LocalStore(tmp_path / "fts.db")


def _seed(store: LocalStore) -> None:
    store.upsert_record(
        source_type="memory",
        source_id="m1",
        title="标题无关",
        content="记忆推送模式已落地failure和planner注入点修复中文短goal检索缺陷",
    )
    store.upsert_record(
        source_type="memory",
        source_id="m2",
        title="网关",
        content="QQ Gateway 稳定性 Round 5 完成 scoped locks",
    )
    store.upsert_record(
        source_type="memory",
        source_id="m3",
        title="待办",
        content="推送模式触发条件结构化待做",
    )


class TestFtsTokenizerIsTrigram:
    def test_records_fts_uses_trigram_tokenizer(self, tmp_path):
        store = _store(tmp_path)
        with store._connection() as conn:
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='records_fts'"
            ).fetchone()["sql"]
        assert "trigram" in sql.lower()

    def test_schema_version_stamped(self, tmp_path):
        store = _store(tmp_path)
        with store._connection() as conn:
            row = conn.execute(
                "SELECT value FROM metadata WHERE key = ?",
                (_FTS_SCHEMA_VERSION_KEY,),
            ).fetchone()
        assert row is not None
        assert row["value"] == _FTS_SCHEMA_VERSION


class TestChineseSubstringHitsViaFts:
    """纯 FTS(_search_fts,绕开 search() 的 LIKE 兜底)必须命中中文子串。"""

    def test_exact_chinese_substring_hits(self, tmp_path):
        store = _store(tmp_path)
        _seed(store)
        hits = store._search_fts("推送模式", limit=10, source_type=None, visibility=None)
        titles = {h.title for h in hits}
        # m1 和 m3 都含 "推送模式"
        assert "标题无关" in titles
        assert "待办" in titles

    def test_partial_overlap_chinese_recalls(self, tmp_path):
        store = _store(tmp_path)
        _seed(store)
        # 查询 "记忆推送模式已落地",m3 只含 "推送模式"(无 "记忆"/"已落地"),
        # 旧实现整段当精确短语会漏掉 m3;trigram + 3-gram OR 能召回。
        hits = store._search_fts(
            "记忆推送模式已落地", limit=10, source_type=None, visibility=None
        )
        titles = {h.title for h in hits}
        assert "待办" in titles

    def test_inner_substring_not_at_boundary_hits(self, tmp_path):
        store = _store(tmp_path)
        _seed(store)
        # "注入点" 嵌在一长串汉字中间,unicode61 下完全无法命中。
        hits = store._search_fts("注入点", limit=10, source_type=None, visibility=None)
        assert any(h.title == "标题无关" for h in hits)

    def test_ascii_term_still_hits(self, tmp_path):
        store = _store(tmp_path)
        _seed(store)
        hits = store._search_fts("Gateway", limit=10, source_type=None, visibility=None)
        assert any(h.title == "网关" for h in hits)

    def test_search_public_api_hits_chinese(self, tmp_path):
        store = _store(tmp_path)
        _seed(store)
        # 公开 search() 端到端:推送模式 必须命中(此前 FTS=0,只能 LIKE 凑合)。
        hits = store.search("推送模式", limit=10)
        assert len(hits) >= 2


class TestShortChineseFallback:
    """1-2 字中文短于 trigram 粒度,由 search() 的 LIKE 兜底,不能因 trigram 回归丢失。"""

    def test_two_char_query_still_found_via_like(self, tmp_path):
        store = _store(tmp_path)
        _seed(store)
        # "网关" 是 2 字(trigram 命不中),但 search() 应经 LIKE 兜底命中标题。
        hits = store.search("网关", limit=10)
        assert any(h.title == "网关" for h in hits)


class TestFtsMigrationFromUnicode61:
    """旧 unicode61 索引(无版本键)重开时必须迁移重建为 trigram 并回填。"""

    def test_legacy_index_migrated_and_backfilled(self, tmp_path):
        db = tmp_path / "legacy.db"
        files = tmp_path / "files"
        files.mkdir()
        # 1) 手工搭一个旧版库:unicode61 的 records_fts + records 行 + 内容文件,无版本键。
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            """CREATE TABLE records(
                id TEXT PRIMARY KEY, source_type TEXT NOT NULL, source_id TEXT NOT NULL,
                title TEXT NOT NULL, content_path TEXT NOT NULL, content_hash TEXT NOT NULL,
                content_preview TEXT NOT NULL DEFAULT '', metadata_json TEXT NOT NULL DEFAULT '{}',
                visibility TEXT NOT NULL DEFAULT 'private', created_at REAL NOT NULL,
                updated_at REAL NOT NULL)"""
        )
        conn.execute(
            "CREATE VIRTUAL TABLE records_fts USING fts5(id UNINDEXED, title, content)"
        )
        (files / "rec-old.txt").write_text(
            "记忆推送模式已落地failure注入点", encoding="utf-8"
        )
        conn.execute(
            "INSERT INTO records VALUES('rec-old','memory','m1','旧记录','files/rec-old.txt',"
            "'h','记忆推送模式已落地failure注入点','{}','private',1.0,1.0)"
        )
        conn.execute(
            "INSERT INTO records_fts(id,title,content) "
            "VALUES('rec-old','旧记录','记忆推送模式已落地failure注入点')"
        )
        conn.commit()
        conn.close()

        # 2) 旧库下中文 FTS 命中=0(确认起点)。
        check = sqlite3.connect(db)
        pre = check.execute(
            'SELECT count(*) FROM records_fts WHERE records_fts MATCH ?', ('"推送模式"',)
        ).fetchone()[0]
        check.close()
        assert pre == 0

        # 3) 用新代码重开 -> 自动迁移 + 回填。
        store = LocalStore(db, files_dir=files)
        with store._connection() as conn2:
            ver = conn2.execute(
                "SELECT value FROM metadata WHERE key = ?", (_FTS_SCHEMA_VERSION_KEY,)
            ).fetchone()["value"]
            sql = conn2.execute(
                "SELECT sql FROM sqlite_master WHERE name='records_fts'"
            ).fetchone()["sql"]
        assert ver == _FTS_SCHEMA_VERSION
        assert "trigram" in sql.lower()

        # 4) 迁移后中文子串能命中(回填生效)。
        hits = store._search_fts("推送模式", limit=10, source_type=None, visibility=None)
        assert any(h.title == "旧记录" for h in hits)

    def test_reopen_same_version_does_not_remigrate(self, tmp_path):
        db = tmp_path / "stable.db"
        store = _store(db.parent if False else tmp_path)  # first open creates v2
        store = LocalStore(db)
        store.upsert_record(
            source_type="memory", source_id="x", title="t", content="推送模式内容样本"
        )
        # 重开同版本:不应触发重建(数据保留,版本不变)。
        store2 = LocalStore(db)
        assert store2._fts_needs_rebuild is False
        hits = store2._search_fts("推送模式", limit=5, source_type=None, visibility=None)
        assert len(hits) >= 1
