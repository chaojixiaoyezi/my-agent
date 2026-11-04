"""临时库里的长期删除屏障与默认召回预算回归。"""
import json
import time
from dataclasses import replace

import pytest

from agent_py_agent.agent.memory_store.generation_manifest import MemoryGenerationStore
from agent_py_agent.agent.memory_store.jsonl import JsonlMemory, MemoryRecord, memory_content_hash


# LLM: 样本只在临时库；永久账来自升级前格式，迁移和搜索均走真实产品入口。
# 函数用途: 创建过期行和删除账挡在有效匹配前的可重现库存。
def _old_store(tmp_path, count, *, migrate=True):
    memory = JsonlMemory(tmp_path / "memory.jsonl")
    expired = [MemoryRecord("user", f"expired-{i}", entry_id=f"old-{i}", expires_at=1,
                            created_at=i + 1, updated_at=i + 1) for i in range(count)]
    live = [MemoryRecord("user", f"needle 持久事实 {i}", entry_id=f"live-{i:03}",
                         created_at=count + i + 1, updated_at=count + i + 1) for i in range(100)]
    memory.path.write_text("".join(row.to_json() + "\n" for row in [*expired, *live]))
    store = MemoryGenerationStore(memory.path)
    removed = [MemoryRecord("user", "", action="remove", entry_id=f"deleted-{i}", version=2,
                            removed_content_hashes=(memory_content_hash(f"deleted content {i}"),)) for i in range(count)]
    store.tombstones.path.write_text(json.dumps({"schema_version": "storage-tombstones.v1",
        "owner": store.tombstones.owner, "records": {row.entry_id: json.loads(row.to_json()) for row in removed}}))
    if migrate:
        memory.migrate_tiers()
    return memory, live


@pytest.mark.parametrize("count", [900, 5000])
@pytest.mark.parametrize("stale", [False, True])
def test_default_recall_survives_large_expired_and_deleted_prefix(tmp_path, count, stale):
    from agent_py_agent.agent.memory_store import jsonl
    from agent_py_agent.agent.memory_store.read_index import MemoryReadIndex

    memory, live = _old_store(tmp_path, count)
    if stale:
        MemoryReadIndex(memory.path).invalidate()
    rows, facts = memory.search_report("needle", 100)
    assert {row.entry_id for row in rows} == {row.entry_id for row in live}
    assert jsonl._AUTHORITY_SCAN_MAX_RECORDS == 1000
    assert jsonl._AUTHORITY_SCAN_MAX_BYTES == 2 * 1024 * 1024
    coverage = facts[-1]["coverage"]
    if "scanned_records" in coverage:
        assert coverage["scanned_records"] <= 1000
        assert coverage["scanned_bytes"] <= 2 * 1024 * 1024
    reopened = JsonlMemory(memory.path)
    assert len(reopened.search("needle", 100)) == 100
    assert reopened.search("deleted content", 100) == []


def test_read_index_excludes_expiry_before_candidate_limit(tmp_path):
    from agent_py_agent.agent.memory_store.read_index import MemoryReadIndex

    memory = JsonlMemory(tmp_path / "memory.jsonl")
    deadline = time.time() - 1
    rows = [MemoryRecord("user", "needle", entry_id=f"a-{i:04}", expires_at=deadline) for i in range(1100)]
    live = MemoryRecord("user", "needle", entry_id="z-live")
    memory.path.write_text("".join(row.to_json() + "\n" for row in [*rows, live]))
    memory.migrate_tiers()
    for _ in range(3):
        memory.rebuild_read_index()
    candidates, coverage = MemoryReadIndex(memory.path).query("needle", limit=10)
    assert coverage["status"] == "complete"
    assert [row["id"] for row in candidates] == ["z-live"]


def test_deleted_hash_and_identity_remain_blocked_after_restart_and_failed_root(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import jsonl
    from agent_py_agent.agent.memory_store._jsonl_indexing import RemovalCleanupError

    memory = JsonlMemory(tmp_path / "memory.jsonl")
    original = memory.add_record(MemoryRecord("user", "private to forget", entry_id="forgotten"))
    memory.migrate_tiers()
    store = MemoryGenerationStore(memory.path)
    writer = jsonl.write_private_text_file_atomic_unlocked

    def fail_root(path, text, **kwargs):
        if path == store.manifest_path:
            raise OSError("simulated I/O error")
        return writer(path, text, **kwargs)

    monkeypatch.setattr(jsonl, "write_private_text_file_atomic_unlocked", fail_root)
    with pytest.raises(RemovalCleanupError) as failure:
        memory.remove(original.entry_id)
    assert failure.value.formal_memory_changed is True
    monkeypatch.setattr(jsonl, "write_private_text_file_atomic_unlocked", writer)
    reopened = JsonlMemory(memory.path)
    assert reopened.search("private to forget", 10) == []
    for entry_id in (original.entry_id, "different-id"):
        with pytest.raises(ValueError, match="MEMORY_TOMBSTONED"):
            reopened.add_record(replace(original, entry_id=entry_id))
    reopened.restore_tier_records([original, replace(original, entry_id="backup-alias")])
    assert reopened.all() == []


# LLM: 正常召回和下一次普通mutation不能触发全账枚举；与调用次数相关而非D相关。
# 函数用途: 用真实5000条永久账约束热路径只做键查询。
def test_hot_lookup_and_mutation_never_enumerate_barrier_ledger(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store.storage_tombstone_index import _TombstoneSnapshot

    memory, _ = _old_store(tmp_path, 5000)
    def forbidden(*args, **kwargs):
        pytest.fail("hot path enumerated permanent barrier ledger")
    monkeypatch.setattr(_TombstoneSnapshot, "full_dict", forbidden)
    assert len(memory.search("needle", 100)) == 100
    memory.add("user", "needle 新事实")
    assert len(memory.search("needle", 200)) == 101


# LLM: 升级前JSON是迁移完成前的屏障，分批事务中断不允许不完整索引授权旧内容。
# 函数用途: 验证持久游标重启续作及完整load合同。
def test_migration_cursor_is_durable_and_incomplete_index_is_not_empty(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store.storage_tombstone_index import StorageTombstoneIndex

    memory = JsonlMemory(tmp_path / "memory.jsonl")
    store = MemoryGenerationStore(memory.path)
    removed = [MemoryRecord("user", "", action="remove", entry_id=f"gone-{i}") for i in range(2100)]
    store.tombstones.path.write_text(store.tombstones.prepare(removed)[1])
    migrate = StorageTombstoneIndex.migrate_step
    calls = []
    def interrupt(self, *args, **kwargs):
        if calls:
            raise OSError("simulated I/O error")
        result = migrate(self, *args, **kwargs)
        calls.append(result)
        return result
    with monkeypatch.context() as patch:
        patch.setattr(StorageTombstoneIndex, "migrate_step", interrupt)
        with pytest.raises(OSError):
            memory.migrate_tiers()
    assert calls[0]["cursor"] == 1000 and not calls[0]["complete"]
    with pytest.raises(ValueError, match="MEMORY_TOMBSTONES_MIGRATING"):
        with store.tombstones.index.snapshot():
            pytest.fail("incomplete migration admitted")
    assert len(store.tombstones.load()) == 2100
    JsonlMemory(memory.path).migrate_tiers()
    with MemoryGenerationStore(memory.path).tombstones.snapshot() as snapshot:
        assert len(snapshot.full_dict()) == 2100
        assert snapshot.blocked([("gone-0", ""), ("gone-2099", "")]) == {"gone-0", "gone-2099"}


@pytest.mark.parametrize("damage", ["marker_missing", "index_missing", "index_corrupt", "legacy_changed"])
def test_damaged_permanent_authority_cannot_fall_back_to_empty(tmp_path, damage):
    memory, _ = _old_store(tmp_path, 900)
    barrier = MemoryGenerationStore(memory.path).tombstones
    if damage == "marker_missing":
        barrier.path.unlink()
    elif damage == "index_missing":
        barrier.index.path.unlink()
    elif damage == "index_corrupt":
        barrier.index.path.write_bytes(b"damaged sqlite authority")
    else:
        from agent_py_agent.agent.memory_store.jsonl import _tombstone_path
        _tombstone_path(memory.path).write_text('{"action":"add","content":"not a valid deletion"}\n')
    with pytest.raises((ValueError, OSError)):
        JsonlMemory(memory.path).all()


# LLM: 过期replace是最终版本，不可在物化前丢掉而恢复旧add。
# 函数用途: 复现独立审查指出的legacy旧正文复活。
def test_expired_latest_replace_does_not_restore_old_add(tmp_path):
    memory = JsonlMemory(tmp_path / "memory.jsonl")
    old = MemoryRecord("user", "old private needle", entry_id="same-id", version=1)
    expired = replace(old, content="expired replacement", action="replace", version=2, expires_at=1)
    memory.path.write_text(old.to_json() + "\n" + expired.to_json() + "\n")
    assert memory.all() == []
    assert memory.search("old private needle", 10) == []


# LLM: 5000过期与100有效交错，不依赖样本恰好按寿命顺序成片；失效读索引不能丢活匹配。
# 函数用途: 复现混合分片下物理扫描预算仍被过期行挤占。
def test_interleaved_expiry_has_bounded_stale_recall(tmp_path):
    from agent_py_agent.agent.memory_store.read_index import MemoryReadIndex

    memory = JsonlMemory(tmp_path / "memory.jsonl")
    rows = []
    for index in range(5100):
        live = index % 51 == 50
        rows.append(MemoryRecord("user", f"needle-{index}", entry_id=f"r-{index}",
            created_at=index + 1, updated_at=index + 1, expires_at=None if live else 1))
    memory.path.write_text("".join(row.to_json() + "\n" for row in rows))
    memory.migrate_tiers()
    MemoryReadIndex(memory.path).invalidate()
    assert len(memory.search("needle", 100)) == 100


# LLM: SQL页有效不等于删除语义有效；合法JSON的add载荷不得借full load注入正式事件。
# 函数用途: 验证删除表损坏载荷失败关闭。
def test_semantically_corrupt_sql_ledger_is_not_a_live_fact(tmp_path):
    import sqlite3
    from contextlib import closing

    memory, _ = _old_store(tmp_path, 900)
    index = MemoryGenerationStore(memory.path).tombstones.index
    with closing(sqlite3.connect(index.path)) as conn, conn:
        conn.execute("UPDATE deleted_entries SET payload=? WHERE entry_id='deleted-0'",
            (MemoryRecord("user", "forged live", entry_id="deleted-0").to_json(),))
    with pytest.raises(ValueError):
        memory.all()


# LLM: 每轮真实search/SQL点查均关闭全部连接；热路径不保留跨请求全账或FD缓存。
# 函数用途: 对两种历史规模测固定候选访问和每轮fd_delta==0。
@pytest.mark.parametrize("count", [900, 5000])
def test_hot_barrier_lookup_is_fd_stable_for_each_loop(tmp_path, count):
    from agent_py_agent.tests.test_v14_length_resume_delivery import _fd_count

    memory, _ = _old_store(tmp_path, count)
    before = _fd_count()
    for _ in range(12):
        rows, facts = memory.search_report("needle", 100)
        assert len(rows) == 100
        assert facts[-1]["coverage"]["examined_records"] <= 1000
        assert _fd_count() - before == 0


# LLM: 不调用doctor/index_all/migrate_tiers；下一次普通写入须自动修复无根旧库的扫描不可达问题。
# 函数用途: 验证5000个过期最终版本不会让正常使用永远停在partial空召回。
def test_normal_write_recovers_large_unmigrated_expired_library(tmp_path):
    from agent_py_agent.agent.memory_store.read_index import MemoryReadIndex

    memory = JsonlMemory(tmp_path / "memory.jsonl")
    rows = [MemoryRecord("user", "x", entry_id=f"expired-{index}", expires_at=1) for index in range(5000)]
    rows.extend(MemoryRecord("user", f"needle live-{index}", entry_id=f"live-{index}") for index in range(100))
    memory.path.write_text("".join(row.to_json() + "\n" for row in rows))
    assert MemoryGenerationStore(memory.path).manifest() is None
    memory.add("user", "needle automatic recovery")
    assert MemoryGenerationStore(memory.path).manifest() is not None
    MemoryReadIndex(memory.path).invalidate()
    assert len(memory.search("needle", 101)) == 101


# LLM: 点查回执与冷全量load必须执行同一删除语义校验，不能把坏载荷解释为已提交删除。
# 函数用途: 钉住合法SQLite页内非remove载荷的失败关闭。
def test_corrupt_point_lookup_is_not_a_committed_deletion(tmp_path):
    import sqlite3
    from contextlib import closing

    memory, _ = _old_store(tmp_path, 900)
    index = MemoryGenerationStore(memory.path).tombstones.index
    with closing(sqlite3.connect(index.path)) as conn, conn:
        conn.execute("UPDATE deleted_entries SET payload=? WHERE entry_id='deleted-0'",
            (MemoryRecord("user", "forged live", entry_id="deleted-0").to_json(),))
    with pytest.raises(ValueError, match="MEMORY_TOMBSTONES_UNREADABLE"):
        memory.committed_removal_record("deleted-0")


# LLM: 首次删除的SQL已经提交而根未出生；损坏marker不能把现存SQL权威降级成空旧账。
# 函数用途: 覆盖无根故障窗口中的删除屏障降级。
@pytest.mark.parametrize("damage", ["missing", "legacy_empty"])
def test_first_delete_failed_root_cannot_downgrade_marker(tmp_path, monkeypatch, damage):
    from agent_py_agent.agent.memory_store import jsonl
    from agent_py_agent.agent.memory_store._jsonl_indexing import RemovalCleanupError

    memory = JsonlMemory(tmp_path / "memory.jsonl")
    original = memory.add("user", "private first deletion")
    store = MemoryGenerationStore(memory.path)
    writer = jsonl.write_private_text_file_atomic_unlocked
    def fail_root(path, text, **kwargs):
        if path == store.manifest_path:
            raise OSError("simulated I/O error")
        return writer(path, text, **kwargs)
    with monkeypatch.context() as fault:
        fault.setattr(jsonl, "write_private_text_file_atomic_unlocked", fail_root)
        with pytest.raises(RemovalCleanupError) as error:
            memory.remove(original.entry_id)
        assert error.value.formal_memory_changed
    assert store.manifest() is None
    if damage == "missing":
        store.tombstones.path.unlink()
    else:
        store.tombstones.path.write_text(json.dumps({"schema_version": "storage-tombstones.v1",
            "owner": store.tombstones.owner, "records": {}}))
    reopened = JsonlMemory(memory.path)
    with pytest.raises(ValueError, match="MEMORY_TOMBSTONES_UNREADABLE"):
        reopened.search("private first deletion")
    with pytest.raises(ValueError, match="MEMORY_TOMBSTONES_UNREADABLE"):
        reopened.add("user", "unrelated fact")


# LLM: 迁移已提交而改写小marker一次I/O失败；旧账未变时凭导入回执沿旧账可读，下一次维护写补齐marker，删除不复活。
# 函数用途: 复现单次marker写失败使整库永久不可读，并约束故障窗口及修复后热路径不枚举全账。
@pytest.mark.parametrize("repair", ["remove", "migrate_tiers"])
def test_failed_post_migration_marker_write_keeps_legacy_barrier_readable(tmp_path, monkeypatch, repair):
    from agent_py_agent.agent.memory_store import jsonl
    from agent_py_agent.agent.memory_store.storage_tombstone_index import _TombstoneSnapshot

    memory, live = _old_store(tmp_path, 100, migrate=False)
    # 旧正文残行只靠永久屏障挡住：同ID与同正文异ID各一条。
    stale = [MemoryRecord("user", "deleted content 0", entry_id="deleted-0", created_at=1, updated_at=1),
             MemoryRecord("user", "deleted content 1", entry_id="alias-1", created_at=1, updated_at=1)]
    with memory.path.open("a", encoding="utf-8") as handle:
        handle.write("".join(row.to_json() + "\n" for row in stale))
    store = MemoryGenerationStore(memory.path)
    writer = jsonl.write_private_text_file_atomic_unlocked
    failed = []

    def fail_marker_once(path, text, **kwargs):
        if path == store.tombstones.path and '"index_file"' in text and not failed:
            failed.append(path)
            raise OSError("simulated I/O error")
        return writer(path, text, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(jsonl, "write_private_text_file_atomic_unlocked", fail_marker_once)
        with pytest.raises(OSError, match="simulated I/O error"):
            memory.remove(live[0].entry_id)
    assert failed == [store.tombstones.path]
    assert "index_file" not in json.loads(store.tombstones.path.read_text())
    receipt = store.tombstones.index.migration_receipt()
    assert receipt is not None and receipt["migrated_revision"] == receipt["revision"]

    def forbidden(*args, **kwargs):
        pytest.fail("hot path enumerated permanent barrier ledger")

    reopened = JsonlMemory(memory.path)
    with monkeypatch.context() as hot:
        hot.setattr(_TombstoneSnapshot, "full_dict", forbidden)
        assert len(reopened.search("needle", 200)) == 100
        assert reopened.search("deleted content", 100) == []
        reopened.add("user", "needle 故障后新事实")
        assert len(reopened.search("needle", 200)) == 101
        visible = {row.entry_id for row in reopened.all()}
    assert {row.entry_id for row in live} < visible
    assert not visible & {"deleted-0", "alias-1"}
    for entry_id in ("deleted-0", "another-alias"):
        with pytest.raises(ValueError, match="MEMORY_TOMBSTONED"):
            reopened.add_record(MemoryRecord("user", "deleted content 0", entry_id=entry_id))
    assert "index_file" not in json.loads(store.tombstones.path.read_text())
    if repair == "remove":
        reopened.remove(live[0].entry_id)
    else:
        reopened.migrate_tiers()
    marker = json.loads(store.tombstones.path.read_text())
    assert marker["index_file"] == store.tombstones.index.path.name
    assert type(marker["index_revision"]) is int and marker["index_revision"] >= receipt["revision"]
    restarted = JsonlMemory(memory.path)
    with monkeypatch.context() as hot:
        hot.setattr(_TombstoneSnapshot, "full_dict", forbidden)
        assert len(restarted.search("needle", 200)) == (100 if repair == "remove" else 101)
        assert restarted.search("deleted content", 100) == []
        restarted.add("user", "needle 修复后新事实")
    assert not {row.entry_id for row in restarted.all()} & {"deleted-0", "alias-1"}


# LLM: 最终注入确认及访问flush也是正常回合的一部分，不能由all/严格默认路径枚举D。
# 函数用途: 将热路径禁枚举约束延伸至最终确认和访问落盘。
@pytest.mark.parametrize("operation", ["confirm", "flush"])
def test_final_access_path_does_not_enumerate_permanent_ledger(tmp_path, monkeypatch, operation):
    from agent_py_agent.agent.memory_store.storage_tombstone_index import _TombstoneSnapshot

    memory, _ = _old_store(tmp_path, 5000)
    selected = memory.search("needle", 10)
    reopened = JsonlMemory(memory.path)
    def forbidden(*args, **kwargs):
        pytest.fail("final access path enumerated permanent barrier ledger")
    monkeypatch.setattr(_TombstoneSnapshot, "full_dict", forbidden)
    if operation == "confirm":
        assert reopened.confirm_scoped_access(selected, lambda row: True) == selected
    else:
        for row in selected:
            reopened._note_access(row.entry_id)
        assert reopened.flush_access_events() == len(selected)
        assert reopened._pending_access == {}


# LLM: 迁移时尚未到期的TTL也必须与永久事实及不同到期边界分开定位；之后自然到期不依赖doctor重整。
# 函数用途: 复现自然过期后stale索引的混片漏召回。
@pytest.mark.parametrize("live_ttl", [False, True])
def test_natural_expiry_after_migration_keeps_stale_recall_bounded(tmp_path, monkeypatch, live_ttl):
    from agent_py_agent.agent.memory_store.read_index import MemoryReadIndex

    memory = JsonlMemory(tmp_path / "memory.jsonl")
    deadline = time.time() + 100000
    live_deadline = deadline + 100000 if live_ttl else None
    rows = [MemoryRecord("user", f"needle-{i}", entry_id=f"r-{i}", created_at=i + 1,
             updated_at=i + 1, expires_at=live_deadline if i % 51 == 50 else deadline) for i in range(5100)]
    memory.path.write_text("".join(row.to_json() + "\n" for row in rows))
    memory.migrate_tiers()
    MemoryReadIndex(memory.path).invalidate()
    monkeypatch.setattr(time, "time", lambda: deadline + 1)
    matches, facts = memory.search_report("needle", 100)
    assert len(matches) == 100
    assert facts[-1]["coverage"]["examined_records"] <= 1000
    assert facts[-1]["coverage"]["scanned_bytes"] <= 2 * 1024 * 1024


# LLM: 正文/行数很小但合法扩展属性很大，普通写入也必须离开不可召回的无根格式。
# 函数用途: 约束自动恢复以真实权威字节量判定并产生有界分片。
def test_normal_write_recovers_byte_heavy_legacy_metadata(tmp_path):
    from agent_py_agent.agent.memory_store import jsonl

    memory = JsonlMemory(tmp_path / "memory.jsonl")
    rows = [MemoryRecord("user", "other fact", entry_id=f"big-{i}",
                        attributes={"note": "a" * 600000}) for i in range(4)]
    memory.path.write_text("".join(row.to_json() + "\n" for row in rows))
    assert memory.path.stat().st_size > jsonl._AUTHORITY_SCAN_MAX_BYTES
    fresh = memory.add("user", "needle compact live fact")
    assert [row.entry_id for row in memory.search("needle", 1)] == [fresh.entry_id]
    root = MemoryGenerationStore(memory.path).manifest()
    assert root is not None
    assert all((memory.path.parent / part["path"]).stat().st_size <= jsonl._AUTHORITY_SCAN_MAX_BYTES // 2
               for part in root["segments"])


# LLM: SQL页中的合法JSON坏hash形状必须在点查、全量读取及合并前一致拒绝。
# 函数用途: 防止字符串hash被拆成字符而误导恢复清理。
@pytest.mark.parametrize("bad_hashes", ["a" * 64, {"hash": "a" * 64}, [1], None])
def test_corrupt_sql_hash_shape_fails_all_deletion_consumers(tmp_path, bad_hashes):
    import sqlite3
    from contextlib import closing

    memory, _ = _old_store(tmp_path, 900)
    barrier = MemoryGenerationStore(memory.path).tombstones
    with closing(sqlite3.connect(barrier.index.path)) as conn, conn:
        payload = json.loads(conn.execute("SELECT payload FROM deleted_entries WHERE entry_id='deleted-0'").fetchone()[0])
        payload["removed_content_hashes"] = bad_hashes
        conn.execute("UPDATE deleted_entries SET payload=? WHERE entry_id='deleted-0'", (json.dumps(payload),))
    for read in (lambda: memory.committed_removal_record("deleted-0"), barrier.load):
        with pytest.raises(ValueError, match="MEMORY_TOMBSTONES_UNREADABLE"):
            read()


# LLM: cursor是耐久协议字段；坏水位必须快速拒绝，而非负切片后无进展循环。
# 函数用途: 对损坏迁移水位验证失败关闭。
@pytest.mark.parametrize("cursor", [-1, 3000, True])
def test_corrupt_migration_cursor_is_rejected(tmp_path, cursor):
    import sqlite3
    from contextlib import closing

    barrier = MemoryGenerationStore(tmp_path / "memory.jsonl").tombstones
    rows = [MemoryRecord("user", "", action="remove", entry_id=f"gone-{i}") for i in range(2100)]
    state = barrier.index.migrate_step(rows, source_identity="test-source")
    state["cursor"] = cursor
    with closing(sqlite3.connect(barrier.index.path)) as conn, conn:
        conn.execute("UPDATE metadata SET value=? WHERE key='state'", (json.dumps(state),))
    with pytest.raises(ValueError, match="MEMORY_TOMBSTONES_UNREADABLE"):
        barrier.index.migrate_step(rows, source_identity="test-source")
