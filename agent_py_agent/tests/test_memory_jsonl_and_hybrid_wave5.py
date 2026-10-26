# LLM: Wave5回归只操作pytest临时库及受控嵌入替身；不削弱旧同步检索、隐私删除或来源边界断言。
# 模块用途: 验证M3正式记忆安全/压实与M6显式低延迟API，不代替真实前台装配或Windows原生验收。
"""Wave5 M3/M6：严格写入、压实、恢复及显式低延迟检索的回归。"""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common import json_io
from agent_py_agent.agent.memory_store import daily, jsonl
from agent_py_agent.agent.memory_store.jsonl import JsonlMemory, MemoryRecord
from agent_py_agent.agent.retrieval.hybrid import HybridRetriever
from agent_py_agent.agent.retrieval.vector_store import VectorStore


# LLM: 样本只写pytest临时目录，允许模拟旧版本事件与坏行；不接触用户库。
# 函数用途: 生成指定记录的JSONL前像。
def _write(path, records):
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records), encoding="utf-8")


# LLM: Wave5独立回归只使用临时样本与受控替身，保留真实入口及失败断言。
# 函数用途: 验证普通及协调批写遇到损坏前像时保持原字节。
@pytest.mark.parametrize("coordinator", [False, True])
@pytest.mark.parametrize("bad", [b"{broken\n", b"[]\n", b'{}\n', b'\xff\n', b'{"role":"user","content":5}\n'])
def test_strict_batch_preserves_corrupt_preimage(tmp_path, monkeypatch, coordinator, bad):
    mem = JsonlMemory(tmp_path / "memory.jsonl")
    mem.add("user", "good")
    mem.path.write_bytes(mem.path.read_bytes() + bad)
    before = mem.path.read_bytes()
    mem.coordinator_io = coordinator
    monkeypatch.setattr(jsonl, "_coordinator_memory_write", lambda operation: operation())
    with pytest.raises(ValueError, match="MEMORY_AUTHORITY_UNREADABLE"):
        mem.add("user", "new")
    assert mem.path.read_bytes() == before


# LLM: Wave5独立回归只使用临时样本与受控替身，保留真实入口及失败断言。
# 函数用途: 验证未知顶层字段往返保留且删除后不泄露正文。
def test_unknown_fields_roundtrip_and_remove_scrubs_them(tmp_path):
    mem = JsonlMemory(tmp_path / "memory.jsonl")
    _write(mem.path, [{"role": "user", "content": "private", "entry_id": "a", "future": {"quote": "private"}}])
    mem._pending_access["a"] = 10
    mem.flush_access_events()
    row = json.loads(mem.path.read_text())
    assert row["future"] == {"quote": "private"}
    assert "_extra_fields" not in row
    mem.replace("a", "updated", expected_version=1)
    assert json.loads(mem.path.read_text().splitlines()[-1])["future"] == {"quote": "private"}
    mem.remove("a", source="candidate:test", expected_version=2)
    for path in tmp_path.glob("*.jsonl"):
        assert "private" not in path.read_text()


# LLM: Wave5独立回归只使用临时样本与受控替身，保留真实入口及失败断言。
# 函数用途: 验证写失败保留pending、成功不吞并发新访问。
@pytest.mark.parametrize("flush", [False, True])
def test_failed_write_keeps_pending_and_success_preserves_concurrent_touch(tmp_path, monkeypatch, flush):
    mem = JsonlMemory(tmp_path / "memory.jsonl")
    rec = mem.add("user", "fact")
    mem._pending_access[rec.entry_id] = 10
    writer = jsonl.write_private_text_file_atomic_unlocked

    # LLM: 测试helper只负责所述观察或注入，不改变生产断言或绕过授权。
    # 函数用途: 在指定原子写入点注入失败。
    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(jsonl, "write_private_text_file_atomic_unlocked", fail)
    with pytest.raises(OSError):
        mem.flush_access_events() if flush else mem.add("user", "next")
    assert mem._pending_access == {rec.entry_id: 10}

    # LLM: 测试helper只负责所述观察或注入，不改变生产断言或绕过授权。
    # 函数用途: 写入期间加入更新的访问信号。
    def concurrent(*args, **kwargs):
        mem._pending_access[rec.entry_id] = 20
        return writer(*args, **kwargs)

    monkeypatch.setattr(jsonl, "write_private_text_file_atomic_unlocked", concurrent)
    mem.flush_access_events() if flush else mem.add("user", "next")
    assert mem._pending_access == {rec.entry_id: 20}
    assert next(r for r in mem.all() if r.entry_id == rec.entry_id).last_accessed_at == 10


# LLM: Wave5独立回归只使用临时样本与受控替身，保留真实入口及失败断言。
# 函数用途: 验证版本与访问压实并保持墓碑恢复身份及hash。
def test_compaction_folds_versions_touches_and_keeps_removal_recovery(tmp_path):
    mem = JsonlMemory(tmp_path / "memory.jsonl")
    rows = [dict(role="user", content="old", entry_id="a", version=1)]
    rows += [dict(role="user", content="current", entry_id="a", action="replace", version=2)]
    rows += [dict(role="system", content="", entry_id="a", action="touch", last_accessed_at=i) for i in range(30)]
    rows += [dict(role="user", content="", entry_id="gone", action="remove", version=i,
                  source="candidate:gone", removed_content_hashes=[str(i)]) for i in range(2, 20)]
    _write(mem.path, rows)
    before = mem.committed_removal_hashes("gone", 18, "candidate:gone")
    mem._pending_access["a"] = 7
    mem.flush_access_events()
    assert len(mem.path.read_text().splitlines()) <= 2
    assert all(row["action"] != "touch" for row in map(json.loads, mem.path.read_text().splitlines()))
    active = mem.all()[0]
    assert (active.version, active.last_accessed_at) == (2, 29)
    recovered = JsonlMemory(mem.path).committed_removal_record("gone")
    assert (recovered.version, recovered.source) == (19, "candidate:gone")
    assert set(before) <= set(recovered.removed_content_hashes)
    assert {str(i) for i in range(2, 20)} <= set(recovered.removed_content_hashes)
    assert (tmp_path / "memory_tombstones.jsonl").exists()


# LLM: Wave5独立回归只使用临时样本与受控替身，保留真实入口及失败断言。
# 函数用途: 验证缓存不重复解析且重新检查过期、返回值隔离。
def test_active_cache_rechecks_expiry_and_does_not_share_mutable_rows(tmp_path, monkeypatch):
    mem = JsonlMemory(tmp_path / "memory.jsonl")
    _write(mem.path, [dict(role="user", content="expires", expires_at=200)])
    monkeypatch.setattr(jsonl.time, "time", lambda: 100)
    assert mem.all()[0].content == "expires"
    original = mem._read_memory_events
    calls = []

    # LLM: 测试helper只负责所述观察或注入，不改变生产断言或绕过授权。
    # 函数用途: 记录读取调用并委托真实读口。
    def spy(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(mem, "_read_memory_events", spy)
    mem.all()[0].content = "polluted"
    assert mem.all()[0].content == "expires"
    assert calls == []
    monkeypatch.setattr(jsonl.time, "time", lambda: 201)
    assert mem.all() == []


# LLM: Wave5独立回归只使用临时样本与受控替身，保留真实入口及失败断言。
# 函数用途: 验证文件刷盘、替换与父目录刷盘的实际顺序。
def test_private_writer_syncs_file_before_replace_and_parent_after(tmp_path, monkeypatch):
    path = tmp_path / "private.txt"
    events = []
    real_fsync, real_replace = json_io.os.fsync, json_io._replace_with_retry

    # LLM: 测试helper只负责所述观察或注入，不改变生产断言或绕过授权。
    # 函数用途: 记录真实fsync调用顺序。
    def sync(fd):
        events.append("sync")
        return real_fsync(fd)

    # LLM: 测试helper只负责所述观察或注入，不改变生产断言或绕过授权。
    # 函数用途: 记录替换顺序并调用真实原子替换。
    def replace_file(*args, **kwargs):
        events.append("replace")
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(json_io.os, "fsync", sync)
    monkeypatch.setattr(json_io, "_replace_with_retry", replace_file)
    json_io.write_private_text_file_atomic_unlocked(path, "durable")
    assert events == ["sync", "replace", "sync"]


# LLM: Wave5独立回归只使用临时样本与受控替身，保留真实入口及失败断言。
# 函数用途: 模拟Windows分支验证同一首字节锁及非阻塞模式。
def test_windows_lock_uses_same_byte_and_nonblocking_mode(tmp_path, monkeypatch):
    calls = []
    fake = SimpleNamespace(LK_LOCK=1, LK_NBLCK=2, LK_UNLCK=3,
                           locking=lambda fd, mode, count: calls.append((fd, mode, count, json_io.os.lseek(fd, 0, 1))))
    monkeypatch.setattr(json_io, "fcntl", None)
    monkeypatch.setattr(json_io, "msvcrt", fake, raising=False)
    with (tmp_path / "lock").open("a+b") as handle:
        json_io._flock_descriptor(handle.fileno(), blocking=False)
        json_io._funlock_descriptor(handle.fileno())
    assert [row[1:] for row in calls] == [(2, 1, 0), (3, 1, 0)]


# LLM: Wave5独立回归只使用临时样本与受控替身，保留真实入口及失败断言。
# 函数用途: 用比较计数证明Daily合并不再线性扫描每个旧ID。
def test_daily_load_and_merge_use_linear_id_lookup(tmp_path):
    # LLM: 仅限本测试的替身，不接触真实账号、用户记忆或外部网络。
    # 类用途: 统计ID比较次数以检验查重复杂度。
    class CountedId(str):
        comparisons = 0

        # LLM: 测试helper只负责所述观察或注入，不改变生产断言或绕过授权。
        # 函数用途: 统计ID相等比较并保留字符串比较语义。
        def __eq__(self, other):
            type(self).comparisons += 1
            return super().__eq__(other)

        __hash__ = str.__hash__

    existing = [daily.DailyMemoryEvent("summary", "one", "system", event_id=CountedId(f"e{i}"),
                                     sequence=i + 1, previous_event_id=f"e{i - 1}" if i else "") for i in range(200)]
    rows, _ = daily.merge_daily_events(existing, [replace(existing[-1], event_id="fresh")])
    assert len(rows) == 201
    assert CountedId.comparisons < 10


# LLM: Wave5独立回归只使用临时样本与受控替身，保留真实入口及失败断言。
# 函数用途: 验证向量装载及插入时预转换浮点元组。
def test_vector_store_keeps_prevalidated_tuples(tmp_path):
    path = tmp_path / "vectors.json"
    store = VectorStore(path)
    store.upsert("a", [1, 0], text="a")
    assert store._items["a"]["vector"] == (1.0, 0.0)
    reloaded = VectorStore(path)
    assert reloaded._items["a"]["vector"] == (1.0, 0.0)
    assert reloaded.search([1, 0])[0].id == "a"


# LLM: Wave5独立回归只使用临时样本与受控替身，保留真实入口及失败断言。
# 函数用途: 验证显式缓存模式零前台嵌入并报告词面降级。
def test_latency_safe_retrieval_never_embeds_and_reports_fallback():
    calls = []
    embedder = SimpleNamespace(embed=lambda texts: calls.append(texts))
    retriever = HybridRetriever(embedder)
    assert retriever.rank("fact", [("a", "fact")], latency_safe=True)
    assert calls == []
    assert retriever.last_retrieval_mode == "bm25_fallback"


# LLM: Wave5独立回归只使用临时样本与受控替身，保留真实入口及失败断言。
# 函数用途: 验证扩展字段不能覆盖canonical正文。
def test_unknown_fields_cannot_override_canonical_payload():
    record = MemoryRecord("user", "fact")
    record._extra_fields = {"content": "forged", "future": 1}
    assert json.loads(record.to_json())["content"] == "fact"


# LLM: Wave5独立回归只使用临时样本与受控替身，保留真实入口及失败断言。
# 函数用途: 验证只给scope内记录评分及排队且前台零网络。
def test_scoped_latency_safe_mode_prefilters_and_queues_bounded_backfill(tmp_path, monkeypatch):
    mem = JsonlMemory(tmp_path / "memory.jsonl")
    keep = mem.add("user", "visible fact")
    mem.add("user", "other scope")
    mem._embedder = SimpleNamespace(model="test", dim=2, embed=lambda texts: pytest.fail("foreground network"))
    queued = []
    monkeypatch.setattr(jsonl, "_queue_vector_backfill", lambda *args: queued.append(args))
    got, facts = mem.search_scoped_candidates_report("visible", 3, lambda r: r.entry_id == keep.entry_id,
                                                     latency_safe=True)
    assert [r.entry_id for r in got] == [keep.entry_id]
    assert facts["mode"] == "bm25_fallback"
    assert queued and [r.entry_id for r in queued[0][2]] == [keep.entry_id]


# LLM: Wave5独立回归只使用临时样本与受控替身，保留真实入口及失败断言。
# 函数用途: 验证每批删除只执行一次孤儿向量全库回收。
def test_one_orphan_scan_per_remove_batch(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import _jsonl_indexing as indexing

    mem = JsonlMemory(tmp_path / "memory.jsonl")
    a, b = mem.add("user", "a"), mem.add("user", "b")
    scans = []
    monkeypatch.setattr(indexing, "_purge_text_vector_orphans", lambda memory: scans.append(memory))
    mem.apply_batch([{"action": "remove", "entry_id": r.entry_id} for r in (a, b)])
    assert scans == [mem]


# LLM: Wave5独立回归只使用临时样本与受控替身，保留真实入口及失败断言。
# 函数用途: 验证补齐期间版本变化后不发布旧文档向量。
def test_backfill_does_not_publish_after_version_changes(tmp_path, monkeypatch):
    mem = JsonlMemory(tmp_path / "memory.jsonl")
    before = mem.add("user", "original")
    calls = []

    # LLM: 仅限本测试的替身，不接触真实账号、用户记忆或外部网络。
    # 类用途: 在嵌入期间改变事实以检验发布CAS。
    class Embedder:
        dim = 2
        model = "fake"

        # LLM: 测试helper只负责所述观察或注入，不改变生产断言或绕过授权。
        # 函数用途: 返回确定性向量并按用例触发版本变化。
        def embed(self, texts):
            calls.append(texts)
            if len(calls) == 1:
                mem._embedder = None
                mem.replace(before.entry_id, "changed")
                mem._embedder = self
            return [[1.0, 0.0] for _ in texts]

    mem._embedder = Embedder()
    job = jsonl._vector_backfill_job(mem, "query", [before], {})
    jsonl._run_vector_backfill(job)
    assert mem._text_vector_cache().keys() == []


# LLM: Wave5独立回归只使用临时样本与受控替身，保留真实入口及失败断言。
# 函数用途: 验证词面候选与重排复用同次记录得分。
def test_lexical_query_and_scores_reused_between_search_and_rerank(tmp_path, monkeypatch):
    mem = JsonlMemory(tmp_path / "memory.jsonl")
    mem.add("user", "中文长期记忆检索")
    calls = []
    original = jsonl._memory_search_score

    # LLM: 测试helper只负责所述观察或注入，不改变生产断言或绕过授权。
    # 函数用途: 记录评分调用并委托真实评分。
    def score(*args, **kwargs):
        calls.append(args[0].entry_id)
        return original(*args, **kwargs)

    monkeypatch.setattr(jsonl, "_memory_search_score", score)
    assert mem.search_report("中文长期记忆", 2)[0]
    assert len(calls) == 1


# LLM: Wave5独立回归只使用临时样本与受控替身，保留真实入口及失败断言。
# 函数用途: 在归档及热文件发表处注入失败验证恢复与待刷信号。
@pytest.mark.parametrize("failure_stage", ["archive", "main"])
def test_archive_first_failure_keeps_committed_recovery_and_no_phantom_delete(tmp_path, monkeypatch, failure_stage):
    mem = JsonlMemory(tmp_path / "memory.jsonl")
    old = mem.add("user", "removed content")
    gone = mem.remove(old.entry_id, source="candidate:old", expected_version=1)
    active = mem.add("user", "live content")
    mem._pending_access[active.entry_id] = 25
    writer = jsonl.write_private_text_file_atomic_unlocked

    # LLM: 测试helper只负责所述观察或注入，不改变生产断言或绕过授权。
    # 函数用途: 在指定原子写入点注入失败。
    def fail(path, raw, **kwargs):
        if (failure_stage == "archive" and path == jsonl._tombstone_path(mem.path)) or (
                failure_stage == "main" and path == mem.path):
            raise OSError("interrupted publication")
        return writer(path, raw, **kwargs)

    # 把已归档墓碑放回热库，模拟升级前历史，再在两次原子发表处注入失败。
    archive = jsonl._tombstone_path(mem.path)
    if archive.exists():
        mem.path.write_text(archive.read_text() + mem.path.read_text())
        archive.write_text("")
    monkeypatch.setattr(jsonl, "write_private_text_file_atomic_unlocked", fail)
    with pytest.raises(OSError):
        mem.remove(active.entry_id, source="candidate:new", expected_version=1)
    reopened = JsonlMemory(mem.path)
    assert [r.entry_id for r in reopened.all()] == [active.entry_id]
    assert reopened.committed_removal_record(active.entry_id) is None
    assert reopened.committed_removal_record(old.entry_id).source == gone.source
    assert mem._pending_access[active.entry_id] == 25


# LLM: Wave5独立回归只使用临时样本与受控替身，保留真实入口及失败断言。
# 函数用途: 验证访问刷写拒绝损坏前像且不清pending。
@pytest.mark.parametrize("bad", ["{bad\n", "[]\n", '{"role":"user","content":[] }\n'])
def test_flush_refuses_corrupt_rows_without_consuming_pending(tmp_path, bad):
    mem = JsonlMemory(tmp_path / "memory.jsonl")
    rec = mem.add("user", "fact")
    mem.path.write_text(mem.path.read_text() + bad)
    original = mem.path.read_bytes()
    mem._pending_access[rec.entry_id] = 10
    with pytest.raises(ValueError, match="MEMORY_AUTHORITY_UNREADABLE"):
        mem.flush_access_events()
    assert mem.path.read_bytes() == original
    assert mem._pending_access[rec.entry_id] == 10


# LLM: Wave5独立回归只使用临时样本与受控替身，保留真实入口及失败断言。
# 函数用途: 验证容量拒绝先于工具账本隐私脱敏副作用。
def test_quota_refusal_happens_before_tool_ledger_redaction(tmp_path, monkeypatch):
    mem = JsonlMemory(tmp_path / "memory.jsonl")
    rec = mem.add("user", "fact")
    redactions = []
    monkeypatch.setattr(mem, "_redact_local_tool_ledgers", lambda contents: redactions.append(contents))

    # LLM: 仅限本测试的替身，不接触真实账号、用户记忆或外部网络。
    # 类用途: 模拟配额拒绝以验证副作用顺序。
    class Admission:
        # LLM: 测试helper只负责所述观察或注入，不改变生产断言或绕过授权。
        # 函数用途: 拒绝模拟配额准入。
        def check(self, changes):
            raise ValueError("quota denied")

    with pytest.raises(ValueError, match="quota denied"):
        mem.apply_batch([{"action": "remove", "entry_id": rec.entry_id}], _admission=Admission())
    assert redactions == []
    assert mem.all()[0].content == "fact"


# LLM: Wave5独立回归只使用临时样本与受控替身，保留真实入口及失败断言。
# 函数用途: 验证Daily加载及重复ID冲突拒绝。
def test_daily_loader_uses_hash_set_and_keeps_duplicate_rejection(tmp_path, monkeypatch):
    rows = []
    for i in range(100):
        event = daily.DailyMemoryEvent("summary", "event", "system", event_id=f"e{i}",
                                      sequence=i + 1, previous_event_id=f"e{i - 1}" if i else "")
        rows.append(event.to_record())
    path = tmp_path / "daily.jsonl"
    _write(path, rows)
    assert len(daily._load_daily_unlocked(path)) == 100
    rows[-1]["event_id"] = "e0"
    _write(path, rows)
    with pytest.raises(daily.DailyMemoryStoreCorruptError, match="duplicate"):
        daily._load_daily_unlocked(path)


# LLM: Wave5独立回归只使用临时样本与受控替身，保留真实入口及失败断言。
# 函数用途: 验证后台补齐当前记录后显式缓存模式可走语义臂。
def test_backfill_writes_only_current_rows_and_semantic_cache_can_be_reused(tmp_path):
    mem = JsonlMemory(tmp_path / "memory.jsonl")
    a, b = mem.add("user", "fact one"), mem.add("user", "fact two")
    mem._embedder = SimpleNamespace(model="fake", dim=2, embed=lambda texts: [[1.0, 0.5] for _ in texts])
    jsonl._run_vector_backfill(jsonl._vector_backfill_job(mem, "fact", [a, b], {}))
    assert len(mem._text_vector_cache().keys()) == 2
    result, facts = mem.search_scoped_candidates_report("fact", 2, lambda r: True, latency_safe=True)
    assert result and facts["mode"] == "semantic"


# LLM: Wave5独立回归只使用临时样本与受控替身，保留真实入口及失败断言。
# 函数用途: 验证错误维度缓存作为缺失条目重新补齐。
def test_backfill_requeues_wrong_dimension_cached_docs(tmp_path, monkeypatch):
    mem = JsonlMemory(tmp_path / "memory.jsonl")
    rec = mem.add("user", "fact")
    mem._embedder = SimpleNamespace(model="fake", dim=2, embed=lambda texts: [[1.0, 0.0] for _ in texts])
    jsonl._query_vector(mem, "fact", [1.0, 0.0])
    job = jsonl._vector_backfill_job(mem, "fact", [rec], {rec.entry_id: [1.0]})
    assert [r.entry_id for r in job[-1]] == [rec.entry_id]


# LLM: Wave5独立回归只使用临时样本与受控替身，保留真实入口及失败断言。
# 函数用途: 验证压实保留全部active身份含已过期记录。
def test_expired_and_managed_records_are_not_evicted_by_compaction(tmp_path):
    mem = JsonlMemory(tmp_path / "memory.jsonl")
    _write(mem.path, [dict(role="user", content="expired", entry_id="expired", expires_at=1),
                      dict(role="user", content="locked", entry_id="locked")])
    mem._pending_access["locked"] = 10
    mem.flush_access_events()
    assert {r.entry_id for r in mem.all_including_expired()} == {"expired", "locked"}
    assert [r.entry_id for r in mem.all()] == ["locked"]


# LLM: Wave5独立回归只使用临时样本与受控替身，保留真实入口及失败断言。
# 函数用途: 验证同批删除重加不能把删除屏障前正文写回热库。
def test_small_batch_remove_then_readd_never_keeps_deleted_plaintext(tmp_path):
    mem = JsonlMemory(tmp_path / "memory.jsonl")
    mem.apply_batch([{"action": "add", "entry_id": "same", "content": "private old"}])
    mem.apply_batch([{"action": "remove", "entry_id": "same"},
                     {"action": "add", "entry_id": "same", "content": "fresh"}])
    assert [r.content for r in mem.all()] == ["fresh"]
    assert "private old" not in mem.path.read_text()


# LLM: Wave5独立回归只使用临时样本与受控替身，保留真实入口及失败断言。
# 函数用途: 验证重新激活条目的旧归档hash不因热库失败丢失。
def test_reactivated_archive_hashes_survive_main_publication_failure(tmp_path, monkeypatch):
    mem = JsonlMemory(tmp_path / "memory.jsonl")
    _write(jsonl._tombstone_path(mem.path), [dict(role="user", content="", entry_id="a", action="remove",
        version=2, source="first", removed_content_hashes=["oldhash"]),
        dict(role="user", content="", entry_id="b", action="remove", version=2, source="b")])
    _write(mem.path, [dict(role="user", content="new life", entry_id="a")])
    writer = jsonl.write_private_text_file_atomic_unlocked

    # LLM: 测试helper只负责所述观察或注入，不改变生产断言或绕过授权。
    # 函数用途: 仅阻断热文件发表以观察归档证据。
    def fail_main(path, raw, **kwargs):
        if path == mem.path:
            raise OSError("main interrupted")
        return writer(path, raw, **kwargs)

    mem._pending_access["a"] = 25
    monkeypatch.setattr(jsonl, "write_private_text_file_atomic_unlocked", fail_main)
    with pytest.raises(OSError):
        mem.flush_access_events()
    archive = jsonl._strict_memory_events(mem.path)
    assert any("oldhash" in r.removed_content_hashes for r in archive)
    assert mem.all()[0].content == "new life"
