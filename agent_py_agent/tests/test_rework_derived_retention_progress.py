"""R2-1: real derived retention makes bounded progress without an idle write window."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.history_index import index_conversation_message
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.local_storage import LocalStore, records
from agent_py_agent.agent.user_space.owner_maintenance import _prune_derived_store

NOW = 2_000_000_000.0
OLD = NOW - 40 * 86400
CUTOFF = NOW - 30 * 86400


# LLM: Seed through the real writer/FTS/event path; only deterministic fixture timestamps use SQL.
# 函数用途: 准备普通大小、已结束40天的真实派生记录。
def _seed(store, count=1):
    items = []
    with store.connection_batch():
        for index in range(count):
            items.append(store.upsert_record(source_type="subagent_run", source_id=f"old-{index}",
                title=f"old-{index}", content=f"ordinary body {index}: " + "body " * 60,
                metadata={"status": "done"}))
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE records SET created_at=?,updated_at=?", (OLD, OLD))
    return items


# LLM: Each tick reopens the same real database, preserving production options and bounded quotas.
# 函数用途: 跨实例推进一拍，并核实扫描和删除都没有超预算。
def _step(store, budget=3, **options):
    options = {"before": CUTOFF, "protected_refs": (), "max_delete": budget, **options}
    result = LocalStore(store.db_path, enable_fts=store.enable_fts).prune_derived_records(
        max_scan=budget, **options)
    assert 0 <= result["scanned"] <= budget, result
    assert len(result["deleted_ids"]) <= options["max_delete"], result
    return result


# LLM: 只读同一测试库的持久状态，不缓存跨tick对象。
# 函数用途: 回读游标、升级标记或正文欠账。
def _meta(store, key):
    with sqlite3.connect(store.db_path) as conn:
        row = conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


# LLM: 仅测试断言读取全量ID，生产扫描仍须受每拍预算约束。
# 函数用途: 核对真实保留和删除的记录集合。
def _ids(store):
    with sqlite3.connect(store.db_path) as conn:
        return {row[0] for row in conn.execute("SELECT id FROM records")}


# LLM: No sleeps or global quota changes; a finite bound fails explicitly if the real cursor stalls.
# 函数用途: 走到指定持久阶段，供晚到关联和文件清理的回归复用。
def _phase(store, phase, budget=3, **options):
    for _ in range(100):
        report = _step(store, budget, **options)
        if report["cursor"]["phase"] == phase:
            return report
    pytest.fail(f"did not reach {phase}: {report}")


# LLM: 使用真实重开入口和固定拍数，不靠睡眠或放大清理预算收敛。
# 函数用途: 等待一轮扫描及文件责任真正结算。
def _settle(store, budget=3, **options):
    for _ in range(200):
        report = _step(store, budget, **options)
        if not report["partial"]:
            return report
    pytest.fail(f"did not settle: {report}")


# LLM: 会话、线程、消息均用canonical API，索引写入原LocalStore。
# 函数用途: 准备持续聊天的真实索引调用上下文。
def _conversation(tmp_path, store):
    conversation = ConversationStore(tmp_path / "conversation")
    thread = conversation.threads.get_or_create({"canonical_user_id": "test-owner", "channel": "tui",
        "channel_user_id": "test-owner", "channel_conversation_id": "retention", "now": NOW})
    return SimpleNamespace(local_store=store), conversation, thread.thread_id


# LLM: 先发表权威消息，再调用产品索引器；不以SQL插行伪造正常写入。
# 函数用途: 每拍新增一条可回读的真实会话消息。
def _message(case, tick):
    agent, conversation, thread_id = case
    row = conversation.messages.append({"thread_id": thread_id, "role": "user", "content": f"real message {tick}"})
    index_conversation_message(agent, conversation, row)
    return agent.local_store.make_record_id("conversation_message", f"{thread_id}:{row.message_id}")


# LLM: 固定4096条/40天与256单位生产配额，写入不停，正文和消息原文都必须核对。
# 函数用途: 复现并锁住正常聊天造成的清理饥饿问题。
def test_owner_cleanup_finishes_4096_expired_records_while_conversation_writes_continue(tmp_path):
    # Import identity is checked in the actual pytest process, not inferred from the shell cwd.
    assert Path(records.__file__).resolve().parents[3] == Path(__file__).resolve().parents[2]
    store = LocalStore(tmp_path / "local_store" / "local.db")
    expired = _seed(store, 4096)
    case = _conversation(tmp_path, store)
    messages, deleted, cursors = [], [], []
    # Scan/delete/files each need about 17 ticks at 256 units; 160 permits bounded reset overhead too.
    for tick in range(160):
        messages.append(_message(case, tick))
        report = _prune_derived_store(store.db_path, now=datetime.fromtimestamp(NOW, timezone.utc), protected_refs=())
        assert report["executed"], report
        assert report["scanned"] <= 256 and len(report["deleted_ids"]) <= 256
        deleted.extend(report["deleted_ids"])
        cursors.append(report["cursor"])
        if not report["partial"]:
            break
    else:
        pytest.fail(f"cleanup starved under ordinary writes: deleted={len(deleted)}, cursor={cursors[-1]}")
    assert set(deleted) == {item.id for item in expired}
    assert len(deleted) == 4096 and report["pending_files"] == 0
    assert _ids(store) == set(messages)
    assert all(not (store.root / item.content_path).exists() for item in expired)
    assert len(conversation_rows := case[1].messages.recent_report(case[2], limit=0)[0]) == len(messages)
    assert all(row.content == f"real message {index}" for index, row in enumerate(conversation_rows))


# LLM: 每拍只引入一条变更，余下预算须同拍推进原游标且冻结上界不变。
# 函数用途: 防止脏队列虽被清空却直接返回或重置扫描。
def test_dirty_conversation_inserts_use_remainder_without_resetting_the_scan(tmp_path):
    store = LocalStore(tmp_path / "local.db")
    _seed(store, 30)
    first = _phase(store, "scan", budget=3)
    position = first["cursor"]["position"]
    boundary = _meta(store, "derived_scan_cursor")["upper_rowid"]
    case = _conversation(tmp_path, store)
    for tick in range(4):
        _message(case, tick)
        report = _step(store, 3)
        assert report["cursor"]["phase"] == "scan"
        assert report["cursor"]["position"] > position, report
        assert _meta(store, "derived_scan_cursor")["upper_rowid"] == boundary
        position = report["cursor"]["position"]


# LLM: 覆盖边界外插入、已扫描行更新、跨组桥接和不产生record事件的引用变化。
# 函数用途: 保证晚到保护先于受影响记录的删除生效。
@pytest.mark.parametrize("association", ["insert", "update", "bridge", "reference"])
def test_late_group_protection_is_reconciled_before_delete(tmp_path, association):
    store = LocalStore(tmp_path / "local.db")
    target, *others = _seed(store, 8)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE records SET metadata_json=? WHERE id=?",
            (json.dumps({"status": "done", "run_id": "held-run"}), target.id))
    active = store.upsert_record(source_type="subagent_run", source_id="active", title="active", content="active",
        metadata={"status": "running", "task_id": "active-task"})
    _phase(store, "delete", budget=3)
    refs = ()
    if association == "insert":
        store.upsert_record(source_type="subagent_work_log", source_id="late", title="late", content="late",
            metadata={"status": "running", "run_id": "held-run"})
    elif association == "update":
        with sqlite3.connect(store.db_path) as conn:
            conn.execute("UPDATE records SET metadata_json=? WHERE id=?",
                (json.dumps({"status": "running", "run_id": "held-run"}), active.id))
    elif association == "bridge":
        store.upsert_record(source_type="subagent_run", source_id="late-bridge", title="bridge", content="bridge",
            metadata={"status": "done", "run_id": "held-run", "task_id": "active-task"})
    else:
        refs = {"held-run"}  # Not a records mutation: signature changes must still invalidate old protection.
    _settle(store, protected_refs=refs)
    assert target.id in _ids(store) and (store.root / target.content_path).is_file()
    assert not {item.id for item in others} & _ids(store)


# LLM: 先真实提交记录删除，再引入保护；解除后必须有新有限pass释放旧unsafe并清账。
# 函数用途: 防止正文保护失效或保守标记造成永久欠账。
@pytest.mark.parametrize("hold", ["active", "reference"])
def test_pending_body_survives_late_protection_then_settles_after_release(tmp_path, hold):
    store = LocalStore(tmp_path / "local.db")
    [target] = _seed(store)
    _phase(store, "files", budget=2)
    assert target.id not in _ids(store) and (store.root / target.content_path).is_file()
    refs = {target.source_id} if hold == "reference" else ()
    active = store.upsert_record(source_type="subagent_run", source_id="late-run", title="late", content="late",
        metadata={"status": "running", "task_id": target.source_id}) if hold == "active" else None
    for _ in range(16):
        _step(store, 2, protected_refs=refs)
        assert (store.root / target.content_path).is_file()
        assert _meta(store, "derived_prune_pending_files")
    if active:
        store.upsert_record(source_type="subagent_run", source_id=active.source_id, title="finished", content="late",
            metadata={"status": "done", "task_id": target.source_id})
    _settle(store, budget=2)
    assert not (store.root / target.content_path).exists()
    assert _meta(store, "derived_prune_pending_files") is None


# LLM: 所有来源均参与路径保护，直接SQL只用于构造共享路径，DELETE事件必须清旧路径投影。
# 函数用途: 覆盖新共享引用及游标后方路径更新与引用移除。
@pytest.mark.parametrize("publication", ["append", "update"])
def test_new_shared_path_before_files_is_kept_then_deleted_reference_is_reconciled(tmp_path, publication):
    store = LocalStore(tmp_path / "local.db")
    [target] = _seed(store)
    reference = store.upsert_record(source_type="memory", source_id="permanent", title="reference", content="reference")
    if publication == "append":
        with sqlite3.connect(store.db_path) as conn:
            conn.execute("DELETE FROM records WHERE id=?", (reference.id,))
    _phase(store, "files", budget=2)
    if publication == "append":
        reference = store.upsert_record(source_type="memory", source_id="permanent", title="reference", content="reference")
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE records SET content_path=? WHERE id=?", (target.content_path, reference.id))
    for _ in range(10):
        _step(store, 2)
        assert (store.root / target.content_path).is_file()
    # Publication of DELETE must remove the stale path projection, without erasing the shared body itself.
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("DELETE FROM records WHERE id=?", (reference.id,))
    _settle(store, budget=2)
    assert not (store.root / target.content_path).exists()


# LLM: 积压多于单拍配额时只能延期删除，不得重扫或越过未对账的保护状态。
# 函数用途: 核对脏队列分拍收费与删除游标持久不变。
def test_more_dirty_rows_than_budget_defer_delete_without_losing_scan_progress(tmp_path):
    store = LocalStore(tmp_path / "local.db")
    items = _seed(store, 8)
    report = _phase(store, "delete", budget=2)
    with sqlite3.connect(store.db_path) as conn:
        for item in items:
            conn.execute("UPDATE records SET title=title||' changed' WHERE id=?", (item.id,))
    for _ in range(3):
        next_report = _step(store, 2)
        assert next_report["deleted_ids"] == []
        assert next_report["cursor"] == report["cursor"]
    _settle(store, budget=2)
    assert _ids(store) == set()


# LLM: 合成unlink故障保留原账，恢复后跨实例结算；观察实际hash/删除叶而不替代数据库。
# 函数用途: 验证扫描重开和已提交文件欠账恢复及SQL事务边界。
def test_reopen_resumes_scan_and_file_debt_and_keeps_hash_io_outside_transactions(tmp_path, monkeypatch):
    store = LocalStore(tmp_path / "local.db")
    items = _seed(store, 12)
    first = _phase(store, "scan", budget=2)
    second = _step(store, 2)
    assert second["cursor"]["position"] > first["cursor"]["position"]
    original_transaction = records._derived_transaction
    original_hash = records._derived_file_hash
    original_unlink = records.unlink_file_beneath
    inside, hashed, attempted = [], [], []

    # LLM: 只记录原事务入口的动态范围，不改变提交、回滚或异常。
    # 函数用途: 标出不能执行文件IO的短SQL作用域。
    def transaction(actual, operation):
        inside.append(True)
        try:
            return original_transaction(actual, operation)
        finally:
            inside.pop()

    # LLM: 调用原hash，仅检查它没有进入SQL事务。
    # 函数用途: 观察正文读取时点。
    def file_hash(*args):
        assert not inside, "hashing entered the SQL commit scope"
        hashed.append(True)
        return original_hash(*args)

    # LLM: 只在文件末叶注入可重试故障，不能伪造SQL删除成功。
    # 函数用途: 留下一条真实持久正文欠账。
    def unlink(*args):
        assert not inside, "unlink entered the SQL commit scope"
        attempted.append(True)
        raise OSError("synthetic interruption after committed row deletion")

    monkeypatch.setattr(records, "_derived_transaction", transaction)
    monkeypatch.setattr(records, "_derived_file_hash", file_hash)
    monkeypatch.setattr(records, "unlink_file_beneath", unlink)
    for _ in range(60):
        _step(store, 2)
        if attempted:
            break
    assert hashed and attempted and _meta(store, "derived_prune_pending_files")
    monkeypatch.setattr(records, "unlink_file_beneath", original_unlink)
    _settle(store, budget=2)
    assert _ids(store) == set()
    assert all(not (store.root / item.content_path).exists() for item in items)
    assert _meta(store, "derived_prune_pending_files") is None


# LLM: 精确安装旧触发器定义，再以产品升级入口重复初始化，canonical与pending账必须保留。
# 函数用途: 防止同名旧触发器被IF NOT EXISTS永久沿用。
def test_upgrade_replaces_old_triggers_and_repeated_init_preserves_dirty_work_and_pending_files(tmp_path):
    store = LocalStore(tmp_path / "local.db")
    items = _seed(store, 6)
    # Model the already deployed projection, old definitions and a retained pending body.
    entry = {"id": "retained-debt", "content_path": "files/retained-debt.txt", "content_hash": "unchanged",
        "source_ids": ["retained-debt"]}
    with store._connection() as conn:
        records._derived_step_schema(conn)
        for action in ("INSERT", "UPDATE", "DELETE"):
            conn.execute(f"DROP TRIGGER maintenance_derived_{action.lower()}")
            conn.execute(f"CREATE TRIGGER maintenance_derived_{action.lower()} AFTER {action} ON records BEGIN "
                "UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='derived_scan_epoch'; END")
        conn.execute("DELETE FROM metadata WHERE key='derived_projection_revision'")
        conn.execute("INSERT OR REPLACE INTO metadata VALUES('derived_scan_cursor', ?)",
            (json.dumps({"phase": "files", "position": 2, "epoch": 0, "cutoff": CUTOFF, "signature": "old"}),))
        conn.execute("INSERT OR REPLACE INTO metadata VALUES('derived_prune_pending_files', ?)", (json.dumps([entry]),))
        conn.execute("INSERT OR REPLACE INTO maintenance_derived_paths VALUES('obsolete', 'old-path')")
        conn.commit()
    # Initialization upgrades only the rebuildable projection; no unbounded canonical rewrite or debt loss.
    with store._connection() as conn:
        records._derived_step_schema(conn)
        conn.commit()
        triggers = [row[0] for row in conn.execute("SELECT sql FROM sqlite_master WHERE name LIKE 'maintenance_derived_%' AND type='trigger'")]
        assert len(triggers) == 3 and all("maintenance_derived_dirty" in sql for sql in triggers)
    assert _ids(store) == {item.id for item in items}
    assert _meta(store, "derived_prune_pending_files") == [entry]
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE records SET title='updated twice' WHERE id=?", (items[0].id,))
        conn.execute("UPDATE records SET title='latest' WHERE id=?", (items[0].id,))
        conn.execute("DELETE FROM records WHERE id=?", (items[-1].id,))
    with store._connection() as conn:
        for _ in range(3):
            records._derived_step_schema(conn)
        conn.commit()
        dirty = [row[0] for row in conn.execute("SELECT record_id FROM maintenance_derived_dirty")]
    assert sorted(dirty) == sorted([items[0].id, items[-1].id])
    assert _meta(store, "derived_prune_pending_files") == [entry]
    _settle(store, budget=2)
    assert _ids(store) == set() and _meta(store, "derived_prune_pending_files") is None


# LLM: 使用真实SQLite事务验证去重、回滚和改ID双边发表，不依赖mock触发器。
# 函数用途: 锁住脏队列与canonical变更同事务的契约。
def test_dirty_publication_is_deduplicated_transactional_and_tracks_both_sides_of_id_change(tmp_path):
    store = LocalStore(tmp_path / "local.db")
    [item] = _seed(store)
    with store._connection() as conn:
        records._derived_step_schema(conn)
        conn.commit()
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='maintenance_derived_dirty'").fetchone()
        conn.execute("UPDATE records SET title='rolled back' WHERE id=?", (item.id,))
        conn.rollback()
        assert not conn.execute("SELECT 1 FROM maintenance_derived_dirty").fetchone()
        conn.execute("UPDATE records SET id='renamed-record' WHERE id=?", (item.id,))
        conn.execute("UPDATE records SET title='latest' WHERE id='renamed-record'")
        conn.commit()
        assert {row[0] for row in conn.execute("SELECT record_id FROM maintenance_derived_dirty")} == {item.id, "renamed-record"}
    _settle(store, budget=2)
    assert _ids(store) == set()


# LLM: Door仅模拟原提交门，实际C连接/SQL/文件算法不替换；unlink须自己的末写门而非SQL事务门。
# 函数用途: 验证分拍清理没有丢失C原来源或混入长文件读取。
def test_bounded_coordinator_path_retains_original_scope_and_hashes_outside_short_gate(tmp_path, monkeypatch):
    from agent_py_agent.agent.common.global_write_commit import (
        GlobalWriteCommitUnavailable,
        global_write_commit_scope,
    )
    from agent_py_agent.tests.test_local_store_coordinator_io import Door

    store = LocalStore(tmp_path / "local.db")
    expired = _seed(store, 12)
    _phase(store, "scan", budget=2)  # The unchanged owner helper must resume this bounded cursor.
    door = Door()
    with global_write_commit_scope(door):
        coordinator = LocalStore(store.db_path, coordinator_io=True)
    with pytest.raises(GlobalWriteCommitUnavailable, match="original_global_write_scope_required"):
        coordinator.prune_derived_records(before=CUTOFF, protected_refs=(), max_scan=2)
    original_hash, original_unlink = records._derived_file_hash, records.unlink_file_beneath
    original_transaction = records._derived_transaction
    observed, sql_scope = [], []

    # LLM: 不修改原事务算法，只观察它的作用域。
    # 函数用途: 区分SQL提交门和C独立文件末写门。
    def transaction(actual, operation):
        sql_scope.append(True)
        try:
            return original_transaction(actual, operation)
        finally:
            sql_scope.pop()

    # LLM: 保留原hash读取，要求处于两个提交门之外。
    # 函数用途: 观察C正文读取没有占用短门。
    def file_hash(*args):
        assert not sql_scope and not door.inside
        observed.append("hash")
        return original_hash(*args)

    # LLM: 原unlink仍消费当前来源的文件末写门，不能被包进SQL事务。
    # 函数用途: 验证C文件末写的授权和SQL边界同时成立。
    def unlink(*args):
        # C's existing unlink has its own authorized write gate, but must never inherit a SQL transaction.
        assert not sql_scope and door.inside
        observed.append("unlink")
        return original_unlink(*args)

    monkeypatch.setattr(records, "_derived_file_hash", file_hash)
    monkeypatch.setattr(records, "unlink_file_beneath", unlink)
    monkeypatch.setattr(records, "_derived_transaction", transaction)
    case = _conversation(tmp_path, store)
    messages = []
    for tick in range(30):
        messages.append(_message(case, tick))
        result = _prune_derived_store(store.db_path, now=datetime.fromtimestamp(NOW, timezone.utc),
            protected_refs=(), commit_transition=door)
        assert result["executed"], result
        assert result["scanned"] <= 256 and len(result["deleted_ids"]) <= 256
        if not result["partial"]:
            break
    else:
        pytest.fail(f"coordinator did not finish: {result}")
    assert door.calls and observed.count("hash") == observed.count("unlink") == len(expired)
    assert _ids(store) == set(messages) and _meta(store, "derived_prune_pending_files") is None
