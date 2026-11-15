# LLM: 纯运行时验证 LocalStore LIKE 扫描时间片预算、单次 FTS 降级、progress handler 清理与 session_search 中断契约。
# 模块用途: 验证 NW-11 有界回退扫描与结构化超时报错行为。
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.session_search_tool import SessionSearchTool
from agent_py_agent.agent.local_storage.schema import (
    _FTS_ACTIVE_COMPLETE_KEY,
    _LOCAL_CONNECTION_TIMEOUT,
)
from agent_py_agent.agent.local_storage.search import ThreadSearch, search_records_in_thread
from agent_py_agent.agent.local_storage.store import LocalStore
from agent_py_agent.agent.memory_store.owner_resources import build_owner_local_store
from agent_py_agent.agent.settings.config import AgentConfig


def _seed_large_store(
    db_path: Path,
    *,
    row_count: int = 20_000,
    preview_chars: int = 4_096,
    scan_slice_ms: int = 1,
    enable_fts: bool = False,
    thread_id: str = "thr-heavy",
    source_type: str = "conversation_message",
) -> LocalStore:
    """用单事务写入宽表记录，用于真实触发 SQLite LIKE 全表扫描预算中断。"""
    store = LocalStore(db_path, enable_fts=enable_fts, scan_slice_ms=scan_slice_ms)
    filler = "a" * preview_chars
    metadata_json = json.dumps({"thread_id": thread_id}, ensure_ascii=False)
    rows = [
        (
            f"rec-{idx:05d}",
            source_type,
            f"src-{idx:05d}",
            f"record title {idx}",
            "",
            f"hash-{idx:05d}",
            filler,
            metadata_json,
            "private",
            float(idx),
            float(idx),
        )
        for idx in range(row_count)
    ]
    with store._connection() as conn:
        conn.executemany(
            """
            INSERT INTO records(
                id, source_type, source_id, title, content_path, content_hash,
                content_preview, metadata_json, visibility, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
    return store


def _assert_interrupt_error(exc: sqlite3.OperationalError) -> None:
    sqlite_code = getattr(exc, "sqlite_errorcode", None)
    primary = sqlite_code & 0xFF if isinstance(sqlite_code, int) else None
    assert primary == sqlite3.SQLITE_INTERRUPT or "interrupted" in str(exc).lower()


def test_local_store_scan_slice_ms_default_validation_and_owner_wiring(tmp_path: Path):
    default_store = LocalStore(tmp_path / "default.db")
    assert default_store.scan_slice_ms == 25

    for invalid in (0, -5, None, False, True, float("nan"), float("inf"), "bad"):
        coerced = LocalStore(tmp_path / f"coerced-{type(invalid).__name__}.db", scan_slice_ms=invalid)  # type: ignore[arg-type]
        assert coerced.scan_slice_ms == 25

    assert LocalStore(tmp_path / "half.db", scan_slice_ms=0.5).scan_slice_ms == 1  # type: ignore[arg-type]
    assert LocalStore(tmp_path / "float.db", scan_slice_ms=12.5).scan_slice_ms == 12  # type: ignore[arg-type]
    assert LocalStore(tmp_path / "tight.db", scan_slice_ms=7).scan_slice_ms == 7

    paths = {
        "local_store_path": tmp_path / "owner.db",
        "local_store_files_dir": tmp_path / "files",
        "local_store_events_path": tmp_path / "events.jsonl",
    }
    config = AgentConfig(retention_scan_slice_ms=9)
    owner_store = build_owner_local_store(config, paths)
    assert owner_store.scan_slice_ms == 9
    assert not hasattr(owner_store, "config")
    assert not hasattr(owner_store, "_config")
    assert not hasattr(owner_store, "agent")

    fallback_store = build_owner_local_store(object(), {
        "local_store_path": tmp_path / "fallback.db",
        "local_store_files_dir": tmp_path / "files2",
        "local_store_events_path": tmp_path / "events2.jsonl",
    })
    assert fallback_store.scan_slice_ms == 25


def test_incomplete_fts_and_short_token_run_single_like_pass_only(tmp_path: Path, monkeypatch):
    store = LocalStore(tmp_path / "single_pass.db", scan_slice_ms=2_000)
    store.upsert_record(
        source_type="memory",
        source_id="src-77",
        title="xy 短词标题",
        content="普通中文正文内容用于测试全文索引和回退逻辑",
        metadata={"thread_id": "t-once"},
    )

    like_calls = 0
    real_search_like = LocalStore._search_like

    def tracked_search_like(self, query, *, limit, source_type, visibility):
        nonlocal like_calls
        like_calls += 1
        return real_search_like(self, query, limit=limit, source_type=source_type, visibility=visibility)

    monkeypatch.setattr(LocalStore, "_search_like", tracked_search_like)

    short_hits = store.search("xy", limit=5)
    assert [hit.source_id for hit in short_hits] == ["src-77"]
    assert like_calls == 1

    like_calls = 0
    source_hits = store.search("src-77", limit=5)
    assert [hit.source_id for hit in source_hits] == ["src-77"]
    assert like_calls <= 1

    with store._connection() as conn:
        conn.execute(
            "INSERT INTO metadata(key, value) VALUES (?, 'false') "
            "ON CONFLICT(key) DO UPDATE SET value='false'",
            (_FTS_ACTIVE_COMPLETE_KEY,),
        )

    like_calls = 0
    assert store.search("unmatched_long_query", limit=5) == []
    assert like_calls == 1

    like_calls = 0
    assert store._safe_search_fts("unmatched_long_query", limit=5, source_type=None, visibility=None) == []
    assert like_calls == 0

    thread_like_calls = 0
    import agent_py_agent.agent.local_storage.search as search_mod

    real_thread_like = search_mod._search_thread_like

    def tracked_thread_like(target_store, query, scope):
        nonlocal thread_like_calls
        thread_like_calls += 1
        return real_thread_like(target_store, query, scope)

    monkeypatch.setattr(search_mod, "_search_thread_like", tracked_thread_like)
    assert search_records_in_thread(store, "unmatched_long_query", ThreadSearch("t-once", None, 5)) == []
    assert thread_like_calls == 1

    def raise_interrupt(*_args, **_kwargs):
        raise sqlite3.OperationalError("interrupted")

    monkeypatch.setattr(LocalStore, "_search_fts", raise_interrupt)
    like_calls = 0
    with pytest.raises(sqlite3.OperationalError, match="interrupted"):
        store.search("somequery", limit=5)
    assert like_calls == 0

    monkeypatch.setattr(search_mod, "_search_thread_fts", raise_interrupt)
    thread_like_calls = 0
    with pytest.raises(sqlite3.OperationalError, match="interrupted"):
        search_records_in_thread(store, "somequery", ThreadSearch("t-once", None, 5))
    assert thread_like_calls == 0


def test_bounded_like_search_interrupts_tight_budget_and_clears_progress_handler(tmp_path: Path):
    store = _seed_large_store(tmp_path / "tight_budget.db", scan_slice_ms=1, enable_fts=False)
    fd_dir = Path("/dev/fd")
    fd_before = len(os.listdir(fd_dir)) if fd_dir.exists() else None

    started = time.monotonic()
    with pytest.raises(sqlite3.OperationalError) as exc_info:
        store.search("zz", limit=5)
    elapsed = time.monotonic() - started
    _assert_interrupt_error(exc_info.value)
    assert elapsed < 2.0

    # 中断退出后同一个 connection_batch 连接上的后续多步查询不得被残留 progress handler 误杀。
    with store.connection_batch():
        with pytest.raises(sqlite3.OperationalError) as batch_exc:
            search_records_in_thread(store, "zz", ThreadSearch("thr-heavy", None, 5))
        _assert_interrupt_error(batch_exc.value)
        time.sleep(0.01)
        with store._connection() as conn:
            count_row = conn.execute("SELECT COUNT(*) FROM records WHERE content_preview NOT LIKE '%zz%'").fetchone()
            assert count_row[0] == 20_000

    # 正常完成 LIKE 后也必须清空 handler，避免同连接后续耗时查询被旧截止时间打断。
    ok_store = LocalStore(tmp_path / "normal_clear.db", enable_fts=False, scan_slice_ms=20)
    ok_store.upsert_record(source_type="memory", source_id="m1", title="short", content="needle xy")
    with ok_store.connection_batch():
        hits = ok_store.search("xy", limit=5)
        assert len(hits) == 1
        time.sleep(0.03)
        with ok_store._connection() as conn:
            total = conn.execute("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 5000) SELECT MAX(x) FROM c").fetchone()
            assert total[0] == 5_000

    if fd_before is not None:
        fd_after = len(os.listdir(fd_dir))
        assert fd_after - fd_before == 0


def test_session_search_tool_maps_interrupt_and_preserves_busy(tmp_path: Path):
    store = _seed_large_store(tmp_path / "tool_interrupt.db", scan_slice_ms=1, enable_fts=False, thread_id="thr-heavy")
    agent = SimpleNamespace(
        local_store=store,
        _current_task_attributes={"conversation_thread_id": "thr-heavy"},
    )
    tool = SessionSearchTool(agent)

    for params in (
        {"mode": "search", "query": "zz"},
        {"mode": "thread_search", "query": "zz"},
    ):
        outcome = tool.execute(params)
        assert outcome.ok is False
        assert outcome.error_code == "HISTORY_QUERY_UNAVAILABLE"
        assert outcome.retryable is False
        assert outcome.recommended_action == "change_strategy"
        assert outcome.effect_outcome == "not_started"
        assert outcome.next_action == '{"mode": "thread_browse"}'
        body = json.loads(outcome.output)
        assert body["error_code"] == "HISTORY_QUERY_UNAVAILABLE"
        assert body["retryable"] is False
        assert body["effect_outcome"] == "not_started"
        assert body["scan_budget_exhausted"] is True
        assert body["next_action"] == {"tool": "session_search", "arguments": {"mode": "thread_browse"}}
        assert body.get("sqlite_primary_code") == sqlite3.SQLITE_INTERRUPT

    # 默认 enable_fts=True 时，2 字符查询直接走有界 _search_like，同样受预算约束并映射结构化超时报错。
    fts_heavy = _seed_large_store(tmp_path / "fts_short.db", scan_slice_ms=1, enable_fts=True)
    fts_outcome = SessionSearchTool(SimpleNamespace(local_store=fts_heavy)).execute({"mode": "search", "query": "zz"})
    assert fts_outcome.ok is False
    assert fts_outcome.error_code == "HISTORY_QUERY_UNAVAILABLE"
    assert json.loads(fts_outcome.output)["scan_budget_exhausted"] is True

    # 合理预算下短词与 source_id 仍能正常命中返回。
    small_store = LocalStore(tmp_path / "small_ok.db", scan_slice_ms=2_000)
    small_store.upsert_record(
        source_type="memory",
        source_id="src-99",
        title="xy 命中",
        content="正常文本",
        metadata={"thread_id": "thr-ok"},
    )
    small_tool = SessionSearchTool(SimpleNamespace(
        local_store=small_store,
        _current_task_attributes={"conversation_thread_id": "thr-ok"},
    ))
    ok_short = small_tool.execute({"mode": "search", "query": "xy"})
    assert ok_short.ok is True and len(json.loads(ok_short.output)["results"]) == 1
    ok_src = small_tool.execute({"mode": "search", "query": "src-99"})
    assert ok_src.ok is True and len(json.loads(ok_src.output)["results"]) == 1

    # 原生 SQLITE_BUSY 仍必须保持 retryable=True / HISTORY_STORAGE_BUSY，不得被误判为中断。
    locker = sqlite3.connect(str(small_store.db_path), isolation_level=None)
    token = _LOCAL_CONNECTION_TIMEOUT.set(0.01)
    try:
        locker.execute("PRAGMA locking_mode=EXCLUSIVE")
        locker.execute("BEGIN EXCLUSIVE")
        locker.execute("CREATE TABLE IF NOT EXISTS _lk(x INT)")
        busy_outcome = small_tool.execute({"mode": "search", "query": "xy"})
        assert busy_outcome.ok is False
        assert busy_outcome.error_code == "HISTORY_STORAGE_BUSY"
        assert busy_outcome.retryable is True
        assert busy_outcome.recommended_action == "retry"
        busy_body = json.loads(busy_outcome.output)
        assert "scan_budget_exhausted" not in busy_body
        assert busy_body.get("sqlite_primary_code") == sqlite3.SQLITE_BUSY
    finally:
        _LOCAL_CONNECTION_TIMEOUT.reset(token)
        locker.execute("ROLLBACK")
        locker.execute("PRAGMA locking_mode=NORMAL")
        locker.execute("BEGIN EXCLUSIVE")
        locker.execute("COMMIT")
        locker.close()


def test_minimal_duck_typed_store_thread_like_defaults_budget(tmp_path: Path):
    backing = LocalStore(tmp_path / "duck.db", enable_fts=False)
    backing.upsert_record(
        source_type="memory",
        source_id="d1",
        title="ab duck",
        content="duck content",
        metadata={"thread_id": "thr-duck"},
    )

    class MinimalStore:
        fts_available = False

        @staticmethod
        def _connection():
            return backing._connection()

        @staticmethod
        def _row_to_result(row, *, score=0.0):
            return backing._row_to_result(row, score=score)

    minimal = MinimalStore()
    assert not hasattr(minimal, "scan_slice_ms")
    hits = search_records_in_thread(minimal, "ab", ThreadSearch("thr-duck", None, 5))
    assert len(hits) == 1 and hits[0].source_id == "d1"
