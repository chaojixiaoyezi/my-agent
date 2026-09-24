# LLM: 只在pytest临时canonical存储验证选择；真实逐行文件扫描，不调用模型、不手动调用Agent夹具。
# 模块用途: 锁定全来源锚点、范围/覆盖过滤、固定EOF一致性与低内存排除大正文的边界。
from __future__ import annotations

import json
import tracemalloc
from dataclasses import replace

import pytest

from agent_py_agent.agent.conversation.background_context import (
    TaskScopeDecision,
    detached_task_rows,
    history_scope_selector_factory,
)
from agent_py_agent.agent.conversation.message_selection import select_message_snapshot
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.runtime_errors import DataCorruptionError


@pytest.fixture
def conversation(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "user", "channel": "chat", "channel_conversation_id": "scope"})
    return store, thread.thread_id


# LLM: helper只追加临时canonical普通消息，使用显式任务元数据，不构造执行器。
# 函数用途: 为范围、锚点和覆盖测试生成具有原字节位置的消息。
def _append(store, tid, content, *, task="", now=1):
    return store.messages.append({"thread_id": tid, "role": "user", "content": content, "now": now,
                                  "metadata": {"task_id": task}})


def test_anchor_is_resolved_before_scope_and_coverage_filter(conversation):
    store, tid = conversation
    before = _append(store, tid, "创建前")
    anchor = _append(store, tid, "创建锚点")
    foreign = _append(store, tid, "后到其他任务", task="other")
    exact = _append(store, tid, "本任务后续", task="mine")
    decision = TaskScopeDecision("mine", frozenset({"mine"}), {
        "context_anchor_message_id": anchor.message_id, "created_at": 100,
    }, True)
    chosen = select_message_snapshot(store.messages, tid, selector_factory=history_scope_selector_factory(decision),
                                     exclude_message_ids=frozenset({anchor.message_id}))
    assert chosen == (before, exact) and foreign not in chosen


def test_missing_anchor_does_not_fall_back_to_creation_time(conversation):
    store, tid = conversation
    _append(store, tid, "虽然早于创建但锚点缺失")
    exact = _append(store, tid, "本任务", task="mine", now=200)
    decision = TaskScopeDecision("mine", frozenset({"mine"}), {"context_anchor_message_id": "missing", "created_at": 100}, True)
    assert select_message_snapshot(store.messages, tid, selector_factory=history_scope_selector_factory(decision)) == (exact,)


def test_legacy_prefix_and_scope_are_applied_to_full_positions(conversation):
    store, tid = conversation
    old = _append(store, tid, "前缀内")
    boundary = _append(store, tid, "旧摘要终点", task="foreign")
    retained = _append(store, tid, "前缀外")
    chosen = select_message_snapshot(store.messages, tid,
        selector_factory=lambda _positions: lambda row: row.metadata["task_id"] != "foreign",
        exclude_through_message_ids=(boundary.message_id,))
    assert chosen == (retained,) and old not in chosen
    with pytest.raises(OSError, match="boundary is missing"):
        select_message_snapshot(store.messages, tid, exclude_through_message_ids=("missing",))


def test_late_append_and_unfinished_tail_are_outside_frozen_eof(conversation):
    store, tid = conversation
    first = _append(store, tid, "原快照")
    path = store.storage.message_path(tid)
    late = replace(first, message_id="late", content="未完成半行")
    encoded = json.dumps(late.to_dict()).encode() + b"\n"
    with path.open("ab") as handle:
        handle.write(encoded[:-1])

    # LLM: 在两遍之间补完原尾行并追加，模拟普通writer；固定through不得吸收本次新数据。
    # 函数用途: 检验快照范围不因选择器准备期间的追加变化。
    def selector(_positions):
        with path.open("ab") as handle:
            handle.write(b"\n")
        _append(store, tid, "更晚")
        return lambda _row: True

    assert select_message_snapshot(store.messages, tid, selector_factory=selector) == (first,)
    assert len(select_message_snapshot(store.messages, tid)) == 3


def test_rewrite_between_scans_is_not_returned_as_frozen_source(conversation):
    store, tid = conversation
    _append(store, tid, "original")
    path = store.storage.message_path(tid)

    def selector(_positions):
        original = path.read_bytes()
        path.write_bytes(original.replace(b"original", b"tampered"))
        return lambda _row: True

    with pytest.raises(DataCorruptionError, match="source changed"):
        select_message_snapshot(store.messages, tid, selector_factory=selector)


@pytest.mark.parametrize("bad", ["broken", "duplicate", "foreign"])
def test_filter_and_coverage_cannot_hide_bad_rows(conversation, bad):
    store, tid = conversation
    first = _append(store, tid, "有效")
    path = store.storage.message_path(tid)
    value = first.to_dict()
    if bad == "foreign":
        value["thread_id"] = "other"
    with path.open("ab") as handle:
        handle.write(b"broken\n" if bad == "broken" else json.dumps(value).encode() + b"\n")
    with pytest.raises(DataCorruptionError):
        select_message_snapshot(store.messages, tid, selector_factory=lambda _: lambda row: False,
                                exclude_message_ids=frozenset({first.message_id}))


def test_selector_cannot_mutate_identity_map_or_return_substitute_row(conversation):
    store, tid = conversation
    _append(store, tid, "原文")

    def mutate(positions):
        positions["invented"] = 0
        return lambda _: True

    with pytest.raises(TypeError):
        select_message_snapshot(store.messages, tid, selector_factory=mutate)
    with pytest.raises(TypeError, match="return bool"):
        select_message_snapshot(store.messages, tid, selector_factory=lambda _: lambda row: row)


def test_large_covered_or_foreign_bodies_do_not_accumulate(conversation, monkeypatch):
    store, tid = conversation
    first = _append(store, tid, "seed")
    path = store.storage.message_path(tid)
    template = replace(first, content="x" * 8192, metadata={"task_id": "foreign"})
    covered = set()
    with path.open("wb") as handle:
        for index in range(1000):
            message_id = f"bulk-{index}"
            row = replace(template, message_id=message_id)
            if index % 2 == 0:
                covered.add(message_id)
                row = replace(row, metadata={"task_id": "mine"})
            handle.write(json.dumps(row.to_dict()).encode() + b"\n")
    final = _append(store, tid, "需要保留的小正文", task="mine")

    def reject_full(*args, **kwargs):
        pytest.fail("full transcript reader must not be used")

    monkeypatch.setattr(store.messages, "recent_report", reject_full)
    tracemalloc.start()
    try:
        selected = select_message_snapshot(store.messages, tid,
            selector_factory=lambda _: lambda row: row.metadata.get("task_id") == "mine",
            exclude_message_ids=frozenset(covered))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert selected == (final,)
    assert peak < path.stat().st_size // 3


def test_display_tail_limit_is_after_scope_not_before_anchor(conversation):
    store, tid = conversation
    first = _append(store, tid, "创建前")
    anchor = _append(store, tid, "锚点")
    for _ in range(4):
        _append(store, tid, "其它任务")
    own = _append(store, tid, "自己", task="mine")
    decision = TaskScopeDecision("mine", frozenset({"mine"}), {"context_anchor_message_id": anchor.message_id}, True)
    selected = select_message_snapshot(store.messages, tid, selector_factory=history_scope_selector_factory(decision), retain_limit=2)
    assert selected == (anchor, own) and first not in selected


@pytest.mark.parametrize("anchor", ["b", "missing", ""])
def test_operational_detached_scope_preserves_prefix_and_lineage(anchor):
    rows = [{"message_id": "a", "created_at": 1, "metadata": {}},
            {"message_id": "b", "created_at": 2, "metadata": {}},
            {"message_id": "c", "created_at": 3, "metadata": {}},
            {"message_id": "d", "created_at": 4, "metadata": {"task_attributes": {"task_id": "mine"}}}]
    expected = [rows[0], rows[1], rows[3]] if anchor != "missing" else [rows[3]]
    assert detached_task_rows(rows, {"context_anchor_message_id": anchor, "created_at": 2}, {"mine"}) == expected


def test_guidance_without_message_ids_uses_original_time_boundary():
    rows = [{"created_at": 1}, {"created_at": 3}, {"created_at": 4, "task_id": "mine"}]
    assert detached_task_rows(rows, {"context_anchor_message_id": "not a guidance ID", "created_at": 2}, {"mine"}) == [rows[0], rows[2]]


@pytest.mark.parametrize("task_id", ["", "ordinary-task"])
@pytest.mark.parametrize("limit", [0, 1])
def test_deferred_bundle_keeps_ordinary_history_limit_semantics(conversation, task_id, limit):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.background_context import (
        BackgroundContextLoad,
        load_context_bundle,
    )

    store, tid = conversation
    first = _append(store, tid, "第一条")
    last = _append(store, tid, "第二条")
    config = SimpleNamespace(conversation_context_recent_limit=limit)
    state = BackgroundContextLoad(SimpleNamespace(), store, store.threads.require(tid), task_id, config, None, [])
    bundle = load_context_bundle(state)
    assert not state.load_errors
    assert [row["message_id"] for row in bundle["messages"]] == ([first.message_id, last.message_id] if limit == 0 else [last.message_id])


def test_bundle_message_deferral_does_not_suppress_later_canonical_error(conversation):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.background_context import (
        BackgroundContextLoad,
        load_context_bundle,
    )

    store, tid = conversation
    _append(store, tid, "正常正文")
    path = store.storage.message_path(tid)
    with path.open("ab") as handle:
        handle.write(b"broken\n")
    deferred, errors = store.context_bundle_report(tid, recent_limit=0, include_messages=False)
    assert deferred["messages"] == [] and not errors
    state = BackgroundContextLoad(SimpleNamespace(), store, store.threads.require(tid), "ordinary-task",
                                  SimpleNamespace(conversation_context_recent_limit=0), None, [])
    load_context_bundle(state)
    assert state.load_errors


@pytest.mark.parametrize("blank", [" ", "\u00a0", "\u2028"])
def test_snapshot_preserves_original_unicode_blank_row_semantics(conversation, blank):
    store, tid = conversation
    row = _append(store, tid, "实际完整消息")
    path = store.storage.message_path(tid)
    before = (blank + "\n").encode() + path.read_bytes() + (blank + "\r\n").encode()
    path.write_bytes(before)
    assert select_message_snapshot(store.messages, tid) == (row,)
    assert path.read_bytes() == before


def test_selected_snapshot_replays_slices_and_isolates_metadata(conversation):
    store, tid = conversation
    rows = [_append(store, tid, f'完整行-{index}', task='mine') for index in range(4)]
    selected = select_message_snapshot(store.messages, tid)
    selected[0].metadata['task_id'] = 'changed'
    assert tuple(selected) == tuple(rows) == tuple(selected)
    assert selected[1:3] == tuple(rows[1:3])
    assert selected[::-1] == tuple(reversed(rows))
    assert selected[-1] == rows[-1]
    _append(store, tid, '迟到追加')
    assert tuple(selected) == tuple(rows)
    assert len(select_message_snapshot(store.messages, tid)) == 5


@pytest.mark.parametrize('mutation', ['rewrite', 'truncate', 'replace'])
def test_replay_rejects_source_change_after_selection(conversation, mutation):
    store, tid = conversation
    _append(store, tid, 'original')
    selected = select_message_snapshot(store.messages, tid)
    path = store.storage.message_path(tid)
    original = path.read_bytes()
    if mutation == 'replace':
        path.rename(path.with_suffix('.previous'))
        path.write_bytes(original)
    else:
        path.write_bytes(original.replace(b'original', b'tampered') if mutation == 'rewrite' else original[:-1])
    with pytest.raises(DataCorruptionError):
        tuple(selected)


def test_replay_cancel_and_early_close_release_file(conversation, monkeypatch):
    from pathlib import Path

    store, tid = conversation
    _append(store, tid, '第一条')
    _append(store, tid, '第二条')
    stop = [False]
    selected = select_message_snapshot(store.messages, tid, interrupt_check=lambda: stop[0])
    handles, original_open = [], Path.open

    def opened(path, *args, **kwargs):
        handle = original_open(path, *args, **kwargs)
        handles.append(handle)
        return handle

    monkeypatch.setattr(Path, 'open', opened)
    iterator = iter(selected)
    next(iterator)
    iterator.close()
    assert handles and all(handle.closed for handle in handles)
    iterator = iter(selected)
    next(iterator)
    stop[0] = True
    with pytest.raises(InterruptedError):
        next(iterator)
    assert all(handle.closed for handle in handles)


def test_selection_cancellation_is_checked_during_first_scan(conversation):
    store, tid = conversation
    for _ in range(3):
        _append(store, tid, '正文')
    calls = []

    def interrupted():
        calls.append(True)
        return len(calls) >= 3

    with pytest.raises(InterruptedError):
        select_message_snapshot(store.messages, tid, interrupt_check=interrupted)


def test_replacement_during_eof_read_cannot_rebind_old_boundary(conversation, monkeypatch):
    store, tid = conversation
    _append(store, tid, 'original')
    path = store.storage.message_path(tid)
    original = store.messages.complete_offset_report

    def replace_after_boundary(thread_id):
        report = original(thread_id)
        before = path.read_bytes()
        path.rename(path.with_suffix('.old'))
        path.write_bytes(before.replace(b'original', b'tampered'))
        return report

    monkeypatch.setattr(store.messages, 'complete_offset_report', replace_after_boundary)
    with pytest.raises(DataCorruptionError, match='source changed'):
        select_message_snapshot(store.messages, tid)
