"""用真实 SQLite、SQL trace 和查询计划验证 FTS 对齐证明及单行寻址。"""
from __future__ import annotations

import pytest

from agent_py_agent.agent.common.global_write_commit import global_write_commit_scope
from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.local_storage import schema as schema_module
from agent_py_agent.agent.local_storage.schema import _FTS_REBUILD_PENDING_KEY
from agent_py_agent.agent.local_storage.search import FTS_ROWID_ALIGNED_KEY, fts_rowid_aligned


@pytest.fixture(params=[False, True], ids=["local", "coordinator"])
def store(tmp_path, request):
    # 两种写入策略均经过真实事务；测试只提供协调器要求的本地提交来源。
    with global_write_commit_scope(lambda operation: operation()):
        instance = LocalStore(tmp_path / "index.db", coordinator_io=request.param)
        assert instance.fts_available
        yield instance


@pytest.fixture
def trace(store, monkeypatch):
    statements = []
    connect = schema_module.sqlite3.connect

    def traced_connect(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(schema_module.sqlite3, "connect", traced_connect)
    return statements


def _upsert(store, source_id="target", content="oldneedle"):
    return store.upsert_record(source_type="memory", source_id=source_id, title=source_id, content=content)


def _metadata(store, key=FTS_ROWID_ALIGNED_KEY):
    with store._connection() as conn:
        row = conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _rowids(store, record_id):
    with store._connection() as conn:
        record_rowid = conn.execute("SELECT rowid FROM records WHERE id = ?", (record_id,)).fetchone()[0]
        fts_rowids = [row[0] for row in conn.execute("SELECT rowid FROM records_fts WHERE id = ?", (record_id,))]
    return record_rowid, fts_rowids


def _fts_ids(store, query):
    # 直接使用 FTS 读口，避免 LIKE 兜底掩盖索引缺失或旧正文残留。
    return {row.id for row in store._search_fts(query, limit=20, source_type=None, visibility=None)}


def _legacy_store_rows(store):
    item = _upsert(store)
    neighbor = _upsert(store, "neighbor", "neighborneedle")
    with store._connection() as conn:
        conn.execute("DELETE FROM metadata WHERE key = ?", (FTS_ROWID_ALIGNED_KEY,))
        # 旧 SQL 在仍有更大 rowid 的索引里替换首条记录，真实产生错位。
        store._replace_fts_row(conn, item.id, item.title, item.content)
        conn.commit()
    record_rowid, fts_rowids = _rowids(store, item.id)
    assert fts_rowids != [record_rowid]
    return item, neighbor


def test_first_upsert_establishes_alignment_and_indexes_body(store):
    assert _metadata(store) is None
    item = _upsert(store)
    assert _metadata(store) == "true"
    record_rowid, fts_rowids = _rowids(store, item.id)
    assert fts_rowids == [record_rowid]
    assert _fts_ids(store, "oldneedle") == {item.id}


def test_upsert_preserves_rowid_and_replaces_old_body(store):
    item = _upsert(store)
    neighbor = _upsert(store, "neighbor", "neighborneedle")
    original_rowid, _ = _rowids(store, item.id)
    updated = _upsert(store, content="newneedle")
    assert updated.id == item.id
    assert _rowids(store, item.id) == (original_rowid, [original_rowid])
    assert _fts_ids(store, "newneedle") == {item.id}
    assert _fts_ids(store, "oldneedle") == set()
    assert _fts_ids(store, "neighborneedle") == {neighbor.id}


def test_aligned_upsert_and_delete_use_only_rowid_addressing(store, trace):
    item = _upsert(store)
    with store._connection() as conn:
        rowid = conn.execute("SELECT rowid FROM records WHERE id = ?", (item.id,)).fetchone()[0]
    _upsert(store, content="newneedle")
    assert store.delete_record(item.id)
    assert store.get_record(item.id) is None
    with store._connection() as conn:
        assert conn.execute("SELECT count(*) FROM records_fts").fetchone()[0] == 0
    deletes = [sql for sql in trace if sql.startswith("DELETE FROM records_fts WHERE")]
    assert deletes == [f"DELETE FROM records_fts WHERE rowid = {rowid}"] * 3
    assert any(sql.startswith("INSERT INTO records_fts(rowid, id, title, content)") for sql in trace)
    assert all("records_fts WHERE id" not in sql for sql in trace)


def test_nonempty_legacy_index_keeps_id_addressing_without_alignment_stamp(store, trace):
    item, neighbor = _legacy_store_rows(store)
    trace.clear()
    _upsert(store, content="newneedle")
    assert _metadata(store) is None
    assert _fts_ids(store, "newneedle") == {item.id}
    assert _fts_ids(store, "oldneedle") == set()
    with store._connection() as conn:
        assert conn.execute("SELECT count(*) FROM records_fts WHERE id = ?", (item.id,)).fetchone()[0] == 1
    # sqlite trace 展开绑定值，因此用完整展开语句核对旧参数化 SQL 的执行。
    legacy_delete = f"DELETE FROM records_fts WHERE id = '{item.id}'"
    assert legacy_delete in trace
    assert any(sql.startswith("INSERT INTO records_fts(id, title, content)") for sql in trace)
    assert store.delete_record(item.id)
    assert trace.count(legacy_delete) == 2
    assert _metadata(store) is None
    assert _fts_ids(store, "newneedle") == set()
    assert _fts_ids(store, "neighborneedle") == {neighbor.id}


@pytest.mark.parametrize("populated", [False, True], ids=["empty", "aligned"])
def test_pending_rebuild_overrides_alignment_and_uses_legacy_path(store, trace, populated):
    if populated:
        _upsert(store)
        assert _metadata(store) == "true"
    with store._connection() as conn:
        conn.execute("INSERT INTO metadata(key, value) VALUES (?, 'true') "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (_FTS_REBUILD_PENDING_KEY,))
        conn.commit()
    trace.clear()
    item = _upsert(store, content="pendingneedle")
    assert _metadata(store) == ("true" if populated else None)
    assert _fts_ids(store, "pendingneedle") == {item.id}
    assert store.delete_record(item.id)
    assert trace.count(f"DELETE FROM records_fts WHERE id = '{item.id}'") == 2
    assert not any(sql.startswith("DELETE FROM records_fts WHERE rowid") for sql in trace)
    assert _metadata(store, _FTS_REBUILD_PENDING_KEY) == "true"


@pytest.mark.parametrize("value", ["false", "TRUE", "1", ""])
def test_existing_non_true_alignment_marker_is_not_overwritten(store, trace, value):
    with store._connection() as conn:
        conn.execute("INSERT INTO metadata(key, value) VALUES (?, ?)", (FTS_ROWID_ALIGNED_KEY, value))
        conn.commit()
    trace.clear()
    item = _upsert(store)
    assert _metadata(store) == value
    assert f"DELETE FROM records_fts WHERE id = '{item.id}'" in trace
    assert _fts_ids(store, "oldneedle") == {item.id}


def test_alignment_proof_is_committed_with_caller_transaction(store):
    with store._connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        assert fts_rowid_aligned(conn)
        assert conn.execute("SELECT value FROM metadata WHERE key = ?", (FTS_ROWID_ALIGNED_KEY,)).fetchone()[0] == "true"
        conn.rollback()
    assert _metadata(store) is None


def test_missing_record_rowid_preserves_legacy_fts_deletion(store, trace):
    item = _upsert(store)
    trace.clear()
    with store._connection() as conn:
        conn.execute("DELETE FROM records WHERE id = ?", (item.id,))
        store._delete_fts_row(conn, item.id, None)
        conn.commit()
    assert f"DELETE FROM records_fts WHERE id = '{item.id}'" in trace
    with store._connection() as conn:
        assert conn.execute("SELECT count(*) FROM records_fts").fetchone()[0] == 0


def test_rowid_delete_query_plan_uses_virtual_table_index(store):
    with store._connection() as conn:
        old_plan = conn.execute("EXPLAIN QUERY PLAN DELETE FROM records_fts WHERE id = ?", ("target",)).fetchall()
        plan = conn.execute("EXPLAIN QUERY PLAN DELETE FROM records_fts WHERE rowid = ?", (1,)).fetchall()
    assert any("INDEX 0:" in row[3] and "INDEX 0:=" not in row[3] for row in old_plan)
    assert any("INDEX 0:=" in row[3] for row in plan)


def test_aligned_index_can_still_rebuild_through_original_writer(store):
    items = [_upsert(store, str(index), f"rebuildneedle{index}") for index in range(3)]
    assert _metadata(store) == "true"
    before = {item.id for item in items}
    assert _fts_ids(store, "rebuildneedle") == before
    assert store.rebuild_fts() == len(items)
    assert _fts_ids(store, "rebuildneedle") == before
    for item in items:
        rowid, fts_rowids = _rowids(store, item.id)
        assert fts_rowids == [rowid]


@pytest.mark.parametrize("bounded", [False, True], ids=["batch", "incremental"])
def test_derived_pruning_uses_rowid_and_preserves_neighbor(store, trace, bounded):
    item = store.upsert_record(source_type="subagent_run", source_id="expired", title="expired",
                               content="expiredneedle", metadata={"status": "done"})
    neighbor = _upsert(store, "neighbor", "neighborneedle")
    with store._connection() as conn:
        conn.execute("UPDATE records SET created_at=1, updated_at=1 WHERE id = ?", (item.id,))
        conn.commit()
    rowid, _ = _rowids(store, item.id)
    trace.clear()
    deleted = []
    for _ in range(20 if bounded else 1):
        result = store.prune_derived_records(before=100, protected_refs=(), **({"max_scan": 10} if bounded else {}))
        deleted.extend(result["deleted_ids"])
        if item.id in deleted:
            break
    assert deleted == [item.id]
    assert f"DELETE FROM records_fts WHERE rowid = {rowid}" in trace
    assert all("records_fts WHERE id" not in sql for sql in trace)
    assert store.get_record(item.id) is None
    assert _fts_ids(store, "expiredneedle") == set()
    assert _fts_ids(store, "neighborneedle") == {neighbor.id}
