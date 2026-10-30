# LLM: 只用临时canonical文件验证M4性能路径和完整性，不调用模型、网络或改变原测试门禁。
# 模块用途: 锁定单遍增量索引、recent/Bloom去重、线程预算和Compact两层缓存。
from __future__ import annotations

import hashlib
import json
import os
import time
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.session_history_read import (
    SessionHistoryRefusal,
    read_conversation_message,
)
from agent_py_agent.agent.conversation import compact_checkpoint_scan as checkpoints
from agent_py_agent.agent.conversation import compact_summary_view as summaries
from agent_py_agent.agent.conversation import message_scan, message_selection, store_threads
from agent_py_agent.agent.conversation.compact_checkpoint import scoped_compact_checkpoint_id
from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE, CompactScope
from agent_py_agent.agent.conversation.message_selection import select_message_snapshot
from agent_py_agent.agent.conversation.models import ConversationThread, MessageLogEntry
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.runtime_errors import DataCorruptionError


# LLM: 夹具清理进程缓存，不接触正式owner；所有用例独立建立真实JSONL。
# 函数用途: 隔离本文件的缓存状态，避免inode复用或顺序依赖影响断言。
@pytest.fixture(autouse=True)
def clean_caches():
    message_selection._POSITION_CACHE.clear()
    message_scan._DEDUPE_INDEX_CACHE.clear()
    checkpoints._CHECKPOINT_OFFSETS.clear()
    summaries._VERIFIED_CHAINS.clear()
    summaries._SUMMARY_VIEWS.clear()
    store_threads._clear_cached_threads()
    yield
    store_threads._clear_cached_threads()


# LLM: 仅创建临时owner的原store，不构造Agent或调用宿主服务。
# 函数用途: 为消息索引与读取测试提供唯一线程。
@pytest.fixture
def conversation(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.create()
    return store, thread.thread_id


# LLM: 行字节来自原model序列化，编号和去重键显式可控，保留真实UTF8编码。
# 函数用途: 构造带独立身份的完整canonical消息行。
def encoded(tid, index, *, content="原文", key=None, **kwargs):
    row = MessageLogEntry(f"m-{index}", tid, "user", content,
                          metadata={} if key is None else {"dedupe_key": key}, **kwargs)
    return (json.dumps(row.to_dict(), ensure_ascii=False) + "\n").encode()


# LLM: 观察真实from_dict调用，不替换任何JSONL读取结果。
# 函数用途: 验证冷读每行只解析一次，尾增量和热精确读取不解析旧行。
def test_position_index_single_parse_incremental_and_by_id(conversation, monkeypatch):
    store, tid = conversation
    path = store.storage.message_path(tid)
    path.write_bytes(b"".join(encoded(tid, i) for i in range(200)))
    calls, original = [], MessageLogEntry.from_dict.__func__

    # LLM: 仅计数真实model解析，不伪造解析结果。
    # 函数用途: 记录单遍及增量解析的消息身份。
    def observed(cls, row):
        calls.append(row.get("message_id"))
        return original(cls, row)

    monkeypatch.setattr(MessageLogEntry, "from_dict", classmethod(observed))
    selected = select_message_snapshot(store.messages, tid, selector_factory=lambda _: lambda row: row.role == "user")
    assert len(selected) == 200 and len(calls) == 200
    calls.clear()
    with path.open("ab") as handle:
        handle.write(encoded(tid, 200))
    assert len(select_message_snapshot(store.messages, tid)) == 201
    assert calls == ["m-200"]
    calls.clear()
    row, errors = store.messages.by_id_report(tid, "m-100")
    assert not errors and row.message_id == "m-100" and calls == ["m-100"]
    calls.clear()
    assert len(select_message_snapshot(store.messages, tid)) == 201 and not calls


# LLM: 任意predicate可以读取完整正文及metadata；代理不能把懒字段替换为空串或污染缓存。
# 函数用途: 保证索引优化仍兼容正文选择及嵌套对象隔离。
def test_selector_reads_real_body_and_cannot_poison_cache(conversation):
    store, tid = conversation
    row = store.messages.append({"thread_id": tid, "role": "user", "content": "原话",
                                 "metadata": {"task_id": "mine"}})

    # LLM: 只改变predicate收到的独立metadata对象。
    # 函数用途: 检查懒读正文及缓存隔离。
    def selector(_positions):
        # LLM: 访问完整原话并修改临时投影，不改canonical。
        # 函数用途: 选择需要的正文，探测嵌套对象泄漏。
        def predicate(item):
            assert isinstance(item, MessageLogEntry)
            item.metadata["task_id"] = "changed"
            return item.content == "原话"
        return predicate

    assert select_message_snapshot(store.messages, tid, selector_factory=selector) == (row,)
    assert select_message_snapshot(store.messages, tid, selector_factory=lambda _: lambda item: item == row) == (row,)
    assert select_message_snapshot(store.messages, tid,
        selector_factory=lambda _: lambda item: item.metadata["task_id"] == "mine") == (row,)


# LLM: 回调真实改写原路径；即使未选中改写行或尾锚不变，也必须拒绝本次选择。
# 函数用途: 覆盖factory/predicate/interrupt中的等长改写、截断及换inode。
@pytest.mark.parametrize("callback", ["factory", "predicate", "interrupt"])
@pytest.mark.parametrize("mutation", ["rewrite", "replace", "truncate", "delete"])
def test_mid_selection_corruption_guard(conversation, callback, mutation):
    store, tid = conversation
    path = store.storage.message_path(tid)
    path.write_bytes(encoded(tid, 0, content="original") + encoded(tid, 1, content="x" * 70000))
    before, called = path.read_bytes(), []

    # LLM: 仅真实改写本用例临时消息文件，首次调用后不再修改。
    # 函数用途: 在选择回调中制造可重复的来源变化。
    def mutate():
        if called:
            return False
        called.append(True)
        if mutation in {"replace", "delete"}:
            path.rename(path.with_suffix(".old"))
            if mutation == "replace":
                path.write_bytes(before)
        else:
            path.write_bytes(before.replace(b"original", b"tampered") if mutation == "rewrite" else before[:30])
        return False

    # LLM: 保留原factory调用形状，变化只发生在指定测试观察点。
    # 函数用途: 生成选择器或在编译阶段改写文件。
    def factory(_positions):
        if callback == "factory":
            mutate()
        return (lambda _row: not mutate()) if callback == "predicate" else lambda _row: True

    with pytest.raises(DataCorruptionError, match="canonical message source changed during selection"):
        select_message_snapshot(store.messages, tid, selector_factory=factory if callback != "interrupt" else None,
                                interrupt_check=mutate if callback == "interrupt" else None)


# LLM: 64KiB锚仍相同的前缀改写必须使位置索引冷扫；过滤不能掩盖损坏。
# 函数用途: 验证中段改写加追加既刷新合法内容，也拒绝坏行。
@pytest.mark.parametrize("bad", [False, True])
def test_rewrite_before_tail_anchor_invalidates_index(conversation, bad):
    store, tid = conversation
    path = store.storage.message_path(tid)
    before = encoded(tid, 0, content="original") + encoded(tid, 1, content="x" * 80000)
    path.write_bytes(before)
    assert len(select_message_snapshot(store.messages, tid)) == 2
    changed = (b"!" + before[1:]) if bad else before.replace(b"original", b"tampered")
    assert changed[-65536:] == before[-65536:]
    path.write_bytes(changed + encoded(tid, 2))
    if bad:
        with pytest.raises(DataCorruptionError):
            select_message_snapshot(store.messages, tid)
    else:
        assert select_message_snapshot(store.messages, tid)[0].content == "tampered"


# LLM: 精确ID读取沿首匹配合同，只报告目标之前的物理坏行；目标后的坏行不影响已找到证据。
# 函数用途: 验证冷/热索引损坏报告顺序、行号、重复ID和空白行。
def test_by_id_preserves_pre_target_errors(conversation):
    store, tid = conversation
    path = store.storage.message_path(tid)
    path.write_bytes(encoded(tid, 0) + b"\ninvalid\n" + encoded(tid, 1) + b"bad-tail\n")
    for _ in range(2):
        first, errors = store.messages.by_id_report(tid, "m-0")
        assert first.message_id == "m-0" and not errors
        row, errors = store.messages.by_id_report(tid, "m-1")
        assert row is None and [error["line_number"] for error in errors] == [3]
    path.write_bytes(encoded(tid, 0, content="first") + encoded(tid, 0, content="second"))
    assert store.messages.by_id_report(tid, "m-0")[0].content == "first"


# LLM: owner/thread校验须先于索引，字节seek不得改变Unicode字符游标。
# 函数用途: 验证显式线程拒绝和中文原话的跨页读取。
def test_session_history_scope_before_seek_and_character_offsets(conversation, monkeypatch):
    from agent_py_agent.agent.capability import session_history_read

    store, tid = conversation
    body = "中🙂文" * 160
    row = store.messages.append({"thread_id": tid, "role": "user", "content": body})
    agent = SimpleNamespace(conversation_store=store)
    monkeypatch.setattr(session_history_read, "trusted_thread", lambda _agent: (tid, "runtime"))
    result = read_conversation_message(agent, row.message_id, {"thread_id": tid, "offset": 7, "max_chars": 200})
    assert result["content"] == body[7:207] and result["next_offset"] == 207
    assert result["total_chars"] == len(body)
    monkeypatch.setattr(store.messages, "by_id_report", lambda *_: pytest.fail("must reject before reading"))
    with pytest.raises(SessionHistoryRefusal):
        read_conversation_message(agent, row.message_id, {"thread_id": "../other"})


# LLM: >16384条真实键不得销毁缓存；热recent和Bloom阴性只解析新尾，历史阳性仍返回首条且校验后续损坏。
# 函数用途: 覆盖16K阈值、Bloom历史回查、false-positive和增量去重。
def test_bloom_dedupe_over_16384_keeps_incremental_cache(tmp_path, monkeypatch):
    path = tmp_path / "messages.jsonl"
    with path.open("wb") as handle:
        for i in range(16400):
            handle.write(encoded("thread", i, key=f"key-{i}"))
    assert message_scan.find_message_dedupe(path, "absent") is None
    index = message_scan._DEDUPE_INDEX_CACHE[str(path)]
    assert len(index.key_offsets) == 16384 and any(index.historical_seen)
    calls, original = [], message_scan._dedupe_message_from_line

    # LLM: 真实解析器的计数包装，不减少扫描范围或过滤损坏。
    # 函数用途: 观察热去重是否重复解析旧前缀。
    def parsed(raw):
        calls.append(True)
        return original(raw)

    monkeypatch.setattr(message_scan, "_dedupe_message_from_line", parsed)
    assert message_scan.find_message_dedupe(path, "key-16399").message_id == "m-16399"
    assert len(calls) == 1
    calls.clear()
    negative = next(f"novel-{i}" for i in range(100) if not message_scan._dedupe_seen(index.historical_seen, f"novel-{i}"))
    with path.open("ab") as handle:
        handle.write(encoded("thread", 16400, key=negative))
    assert message_scan.find_message_dedupe(path, negative).message_id == "m-16400" and len(calls) == 1
    with path.open("ab") as handle:
        handle.write(encoded("thread", 16401, key="key-0", content="later duplicate"))
    assert message_scan.find_message_dedupe(path, "key-0").message_id == "m-0"
    assert message_scan._DEDUPE_INDEX_CACHE[str(path)].key_offsets["key-0"][0] == 0
    message_scan._DEDUPE_INDEX_CACHE[str(path)].historical_seen = b"\xff" * 65536
    assert message_scan.find_message_dedupe(path, "not-actually-present") is None
    with path.open("ab") as handle:
        handle.write(b"broken\n")
    with pytest.raises(json.JSONDecodeError):
        message_scan.find_message_dedupe(path, "key-0")


# LLM: 尾块必须64KiB且全前缀仍验证，旧中段改写后增长不能被Bloom阴性漏过。
# 函数用途: 验证去重尾锚范围及中段改写后的首次匹配。
def test_dedupe_64k_anchor_and_full_prefix_integrity(tmp_path):
    path = tmp_path / "messages.jsonl"
    before = encoded("thread", 0, key="old-key") + encoded("thread", 1, content="x" * 100000)
    path.write_bytes(before)
    message_scan.find_message_dedupe(path, "missing")
    index = message_scan._DEDUPE_INDEX_CACHE[str(path)]
    assert index.tail_anchor_offset == len(before) - 65536
    assert index.tail_anchor_digest == hashlib.blake2b(before[-65536:], digest_size=16).digest()
    changed = before.replace(b"old-key", b"new-key")
    assert changed[-65536:] == before[-65536:]
    path.write_bytes(changed + encoded("thread", 2, key="old-key"))
    assert message_scan.find_message_dedupe(path, "new-key").message_id == "m-0"
    assert message_scan.find_message_dedupe(path, "old-key").message_id == "m-2"


# LLM: 强制Bloom假阳性，验证不存在键和重新活跃历史键均可进入真实recent且首地址不漂移。
# 函数用途: 锁住独立审查发现的recent不能回温问题。
def test_bloom_false_positive_promotes_recent_first_address(tmp_path, monkeypatch):
    monkeypatch.setattr(message_scan, "_MAX_DEDUPE_KEYS_PER_FILE", 2)
    path = tmp_path / "messages.jsonl"
    path.write_bytes(b"".join(encoded("thread", i, key=f"k{i}") for i in range(3)))
    message_scan.find_message_dedupe(path, "missing")
    index = message_scan._DEDUPE_INDEX_CACHE[str(path)]
    assert "k0" not in index.key_offsets
    assert message_scan.find_message_dedupe(path, "k0").message_id == "m-0"
    index = message_scan._DEDUPE_INDEX_CACHE[str(path)]
    assert tuple(index.key_offsets)[-1] == "k0" and index.key_offsets["k0"][0] == 0
    index.historical_seen = b"\xff" * len(index.historical_seen)
    with path.open("ab") as handle:
        handle.write(encoded("thread", 3, key="new-false-positive"))
    assert message_scan.find_message_dedupe(path, "new-false-positive").message_id == "m-3"
    assert tuple(message_scan._DEDUPE_INDEX_CACHE[str(path)].key_offsets)[-1] == "new-false-positive"


# LLM: 禁止values遍历而不是只比较结果，所有插入/覆盖/删除/超大跳过/淘汰/clear均核实字节总额。
# 函数用途: 保证线程缓存预算维护为O(1)，不存在全表求和。
def test_thread_cache_constant_time_accounting(tmp_path, monkeypatch):
    # LLM: 不改变cache操作，只拒绝全表values求和。
    # 类用途: 让O(N)预算回归立即失败。
    class NoValues(OrderedDict):
        # LLM: 预算维护不得枚举所有缓存值。
        # 函数用途: 报告意外全表遍历。
        def values(self):
            pytest.fail("cache insertion must not sum all rows")

    monkeypatch.setattr(store_threads, "_THREAD_RECORD_CACHE", NoValues())
    monkeypatch.setattr(store_threads, "_THREAD_RECORD_CACHE_MAX_ENTRIES_COUNT", 2)
    monkeypatch.setattr(store_threads, "_THREAD_CACHE_MAX_TOTAL_BYTES", 250)
    row = ConversationThread(thread_id="thread", canonical_user_id="owner")
    a, b, c = [tmp_path / name for name in ("a", "b", "c")]
    store_threads._store_cached_thread(a, (1, 100, 2, 3), row)
    store_threads._store_cached_thread(a, (1, 80, 2, 3), row)
    store_threads._store_cached_thread(b, (1, 100, 2, 3), row)
    assert store_threads._THREAD_RECORD_CACHE_TOTAL_BYTES == 180
    store_threads._store_cached_thread(c, (1, 100, 2, 3), row)
    assert store_threads._THREAD_RECORD_CACHE_TOTAL_BYTES == 200 and str(a) not in store_threads._THREAD_RECORD_CACHE
    store_threads._drop_cached_thread(c)
    store_threads._drop_cached_thread(c)
    assert store_threads._THREAD_RECORD_CACHE_TOTAL_BYTES == 100
    store_threads._store_cached_thread(b, (1, 100000, 2, 3), row)
    assert store_threads._THREAD_RECORD_CACHE_TOTAL_BYTES == 0
    store_threads._store_cached_thread(a, (1, 100, 2, 3), row)
    store_threads._clear_cached_threads()
    assert store_threads._THREAD_RECORD_CACHE_TOTAL_BYTES == 0


# LLM: 热线程列表只对返回切片克隆，冷缓存仍保独立快照，不把内部dict暴露给调用者。
# 函数用途: 验证workspace过滤与排序在limit前且最终对象隔离。
def test_workspace_listing_only_clones_returned_slice(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversations")
    for i in range(8):
        store.threads.write(ConversationThread(thread_id=f"t-{i}", canonical_user_id="owner", workspace_root=str(tmp_path),
                                               updated_at=i + 1, metadata={"value": {"x": i}}))
    store.threads.list_report(limit=0)
    calls, original = [], store_threads._clone_cached_thread

    # LLM: 保留真实深拷贝行为，只观察调用对象。
    # 函数用途: 统计列表结果切片实际克隆数量。
    def cloned(row):
        calls.append(row.thread_id)
        return original(row)

    monkeypatch.setattr(store_threads, "_clone_cached_thread", cloned)
    rows, errors = store.threads.list_report(limit=2, workspace_dir=str(tmp_path))
    assert not errors and calls == ["t-6", "t-7"]
    rows[0].metadata["value"]["x"] = -1
    assert store.threads.require("t-6").metadata["value"]["x"] == 6


# LLM: 使用真实v3内容封印与原读取链，不mock提交校验；mtime显式移出共享粗时间窗口。
# 函数用途: 为Compact缓存测试建立两代、不同scope的原始检查点账本。
def compact_case(tmp_path):
    path = tmp_path / "compact" / "conversations" / "thread.jsonl"
    path.parent.mkdir(parents=True)
    rows = []
    local = CompactScope(kind="task", task_id="mine", created_at=20)
    for i, scope in enumerate((THREAD_COMPACT_SCOPE, local), 1):
        row = {"schema": "conversation_compact_checkpoint.v3", "thread_id": "thread", "generation": i,
               "previous_checkpoint_id": rows[-1]["checkpoint_id"] if rows else "",
               "summary_base_checkpoint_id": rows[-1]["checkpoint_id"] if rows else "",
               "summary": f"summary-{i}", "summary_sha256": hashlib.sha256(f"summary-{i}".encode()).hexdigest(),
               "scope": scope.to_dict(), "created_at": i * 5, "source_kind": "transcript",
               "source_message_ids": [f"m-{i}"], "source_tool_refs": [], "operation_evidence": {"nested": {"n": i}}}
        row["checkpoint_id"] = scoped_compact_checkpoint_id(row)
        rows.append(row)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    old = time.time() - 10
    os.utime(path, (old, old))
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_compact_dir=tmp_path / "compact"),
                            backend=SimpleNamespace(name="fake", model_name="one"))
    thread = ConversationThread(thread_id="thread", canonical_user_id="owner", compact_generation=2,
                                compact_checkpoint_id=rows[-1]["checkpoint_id"])
    return agent, thread, path, local, rows


# LLM: L1独立于scope/backend复用，L2命中返回deepcopy；base链覆盖不能把任务摘要当全线程摘要。
# 函数用途: 验证两个缓存层分别命中及嵌套数据独立。
def test_compact_two_layers_scope_backend_and_deepcopy(tmp_path, monkeypatch):
    agent, thread, path, scope, rows = compact_case(tmp_path)
    calls, original = [], summaries.iter_committed_compact_checkpoints

    # LLM: 生成器完整转交原reader，坏行和验链均不绕过。
    # 函数用途: 计数L1冷读次数。
    def scanned(*args, hydrate: bool = False):
        calls.append(True)
        yield from original(*args, hydrate=hydrate)

    monkeypatch.setattr(summaries, "iter_committed_compact_checkpoints", scanned)
    view = summaries.resolve_compact_summary_view(agent, thread, scope)
    assert view.summary == "summary-2" and view.source_message_ids == frozenset({"m-1", "m-2"})
    view.operation_evidence["nested"]["n"] = -1
    assert summaries.resolve_compact_summary_view(agent, thread, scope).operation_evidence["nested"]["n"] == 2
    assert len(calls) == 1
    assert summaries.resolve_compact_summary_view(agent, thread, THREAD_COMPACT_SCOPE).checkpoint_id == rows[0]["checkpoint_id"]
    agent.backend.model_name = "two"
    assert summaries.resolve_compact_summary_view(agent, thread, scope).checkpoint_id == rows[1]["checkpoint_id"]
    assert len(calls) == 1 and path.exists()


# LLM: 原head不变而账本有追加也必须重验所有JSON，不能用L1或L2掩盖坏orphan。
# 函数用途: 验证Compact缓存按完整文件版本而非仅head失效。
def test_compact_append_bad_orphan_invalidates_both_layers(tmp_path):
    agent, thread, path, scope, _rows = compact_case(tmp_path)
    assert summaries.resolve_compact_summary_view(agent, thread, scope).summary
    with path.open("ab") as handle:
        handle.write(b"bad orphan\n")
    with pytest.raises(OSError, match="unreadable"):
        summaries.resolve_compact_summary_view(agent, thread, scope)


# LLM: 稳定偏移缓存省去首次全文件解析，但每次lookup仍hash；近期文件不读取旧缓存。
# 函数用途: 验证偏移层命中、读中改写防护及粗时间窗口。
def test_checkpoint_offsets_reuse_hash_and_freshness(tmp_path, monkeypatch):
    _agent, _thread, path, _scope, rows = compact_case(tmp_path)
    calls, original = [], checkpoints._parse_row

    # LLM: 真实checkpoint解析的计数包装。
    # 函数用途: 区分全账本解析与单条hash后解析。
    def parsed(raw):
        calls.append(True)
        return original(raw)

    monkeypatch.setattr(checkpoints, "_parse_row", parsed)
    with checkpoints.checkpoint_row_lookup(path) as lookup:
        assert lookup(rows[0]["checkpoint_id"])["summary"] == "summary-1"
    assert len(calls) == 3
    calls.clear()
    with checkpoints.checkpoint_row_lookup(path) as lookup:
        assert lookup(rows[0]["checkpoint_id"])["summary"] == "summary-1"
        assert len(calls) == 1
        before = path.read_bytes()
        path.write_bytes(before.replace(b"summary-1", b"tampered!"))
        with pytest.raises(OSError, match="source changed"):
            lookup(rows[0]["checkpoint_id"])
    calls.clear()
    path.write_bytes(before)
    with checkpoints.checkpoint_row_lookup(path) as lookup:
        assert lookup(rows[1]["checkpoint_id"])
    assert len(calls) == 3
