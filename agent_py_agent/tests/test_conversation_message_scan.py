# LLM: 仅在pytest隔离HOME及临时canonical文件验证扫描；真实文件与原Store，不调用模型或修改正式历史。
# 模块用途: 验证冻结尾界、字节预算、损坏回滚和幂等全扫描的内存/兼容边界。
from __future__ import annotations

import json
import tracemalloc
from dataclasses import replace

import pytest

from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.runtime_errors import DataCorruptionError


# LLM: 原Store创建临时线程，不构造Agent或读取正式owner配置。
# 函数用途: 给各测试独立canonical路径及唯一线程身份。
@pytest.fixture
def conversation(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({
        "canonical_user_id": "test-user", "channel": "chat", "channel_user_id": "test-user",
        "channel_conversation_id": "scan",
    })
    return store, thread.thread_id


# LLM: 只通过原append入口增加测试消息，返回精确消息ID用于覆盖与游标断言。
# 函数用途: 在临时会话写入普通用户消息，不调用工具或模型。
def _append(store, thread_id, content="中文\u0085\u2028完整内容"):
    return store.messages.append({"thread_id": thread_id, "role": "user", "content": content})


def test_fixed_end_excludes_later_rows_and_partial_tail(conversation):
    store, tid = conversation
    first = _append(store, tid)
    path = store.storage.message_path(tid)
    first_end = path.stat().st_size
    late = replace(first, message_id="late", content="下一轮")
    encoded = json.dumps(late.to_dict(), ensure_ascii=False).encode() + b"\n"
    with path.open("ab") as handle:
        handle.write(encoded[:-2])
    end, errors = store.messages.complete_offset_report(tid)
    assert not errors and end == first_end
    with path.open("ab") as handle:
        handle.write(encoded[-2:])
    newest = _append(store, tid)
    rows, cursor, errors = store.messages.page_after_offset_report(tid, through=end, max_bytes=end)
    assert not errors and rows == [first] and cursor == end
    assert store.messages.page_after_offset_report(tid, after=end, through=end, max_bytes=1) == ([], end, [])
    rows, _, errors = store.messages.page_after_offset_report(tid, after=end)
    assert not errors and rows == [late, newest]


def test_byte_pages_keep_unicode_exact_and_do_not_drop_next_row(conversation):
    store, tid = conversation
    rows = [_append(store, tid, content="中\u0085\u2028\u2029" * 30) for _ in range(5)]
    sizes = [len(line) for line in store.storage.message_path(tid).read_bytes().splitlines(keepends=True)]
    budget = max(sizes) + 10
    end, errors = store.messages.complete_offset_report(tid)
    assert not errors
    cursor = 0
    found = []
    while cursor < end:
        page, next_cursor, errors = store.messages.page_after_offset_report(tid, after=cursor, through=end, max_bytes=budget)
        assert not errors and len(page) == 1 and 0 < next_cursor - cursor <= budget
        cursor = next_cursor
        found.extend(page)
    assert found == rows


def test_exact_budget_and_oversized_row_are_distinct(conversation):
    store, tid = conversation
    row = _append(store, tid)
    size = store.storage.message_path(tid).stat().st_size
    assert store.messages.page_after_offset_report(tid, max_bytes=size) == ([row], size, [])
    rows, cursor, errors = store.messages.page_after_offset_report(tid, max_bytes=size - 1)
    assert not rows and cursor == 0
    assert errors[0]["error_type"] == "MessagePageBudgetExceeded"
    assert errors[0]["category"] == "resource_limit"
    assert errors[0]["recoverable"] is True


@pytest.mark.parametrize("mutation", ["truncate", "middle_end", "end_before_cursor", "corrupt_second", "foreign_second"])
def test_frozen_page_failure_preserves_start_cursor(conversation, mutation):
    store, tid = conversation
    first = _append(store, tid)
    start = store.messages.byte_offset_after(tid, first.message_id)
    second = _append(store, tid)
    _append(store, tid)
    path = store.storage.message_path(tid)
    end, errors = store.messages.complete_offset_report(tid)
    assert not errors
    if mutation == "truncate":
        with path.open("r+b") as handle:
            handle.truncate(end - 5)
    elif mutation == "middle_end":
        end -= 2
    elif mutation == "end_before_cursor":
        end = start - 1
    else:
        with path.open("ab") as handle:
            handle.write(b"broken\n" if mutation == "corrupt_second" else json.dumps(replace(second, thread_id="foreign").to_dict()).encode() + b"\n")
        end = path.stat().st_size
    rows, cursor, errors = store.messages.page_after_offset_report(tid, after=start, through=end, max_bytes=100_000)
    assert not rows and cursor == start and errors


@pytest.mark.parametrize("max_bytes", [0, -1, True, 1.5, "100"])
def test_invalid_byte_budget_is_reported_without_ack(conversation, max_bytes):
    store, tid = conversation
    _append(store, tid)
    rows, cursor, errors = store.messages.page_after_offset_report(tid, max_bytes=max_bytes)
    assert not rows and cursor == 0 and errors[0]["error_type"] == "ValueError"


def test_complete_end_uses_bounded_reads_for_huge_partial_tail(conversation, monkeypatch):
    store, tid = conversation
    row = _append(store, tid)
    path = store.storage.message_path(tid)
    end = path.stat().st_size
    with path.open("ab") as handle:
        handle.write(b"x" * (1024 * 1024))
    from agent_py_agent.agent.conversation import message_scan

    reads = []
    original = type(path).open

    # LLM: 包装真实文件只观察读取大小；不得替代真实内容、位置或EOF。
    # 类用途: 使测试能拒绝无界read并保留真实seek/read行为。
    class ObservedFile:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.handle.close()

        def seek(self, *args):
            return self.handle.seek(*args)

        def read(self, size=-1):
            assert 0 <= size <= 64 * 1024
            reads.append(size)
            return self.handle.read(size)

    def observed_open(target, *args, **kwargs):
        handle = original(target, *args, **kwargs)
        return ObservedFile(handle) if target == path else handle

    monkeypatch.setattr(type(path), "open", observed_open)
    assert message_scan.complete_message_offset(path) == end
    assert len(reads) > 1 and row.message_id


def test_append_once_streams_all_rows_and_replays_first_key(conversation, monkeypatch):
    store, tid = conversation
    request = {"thread_id": tid, "role": "user", "content": "原文\u0085\u2028"}
    first = store.messages.append_once(request, dedupe_key="same")
    path = store.storage.message_path(tid)
    template = replace(first, content="x" * 4096, metadata={})
    with path.open("ab") as handle:
        for index in range(2000):
            handle.write(json.dumps(replace(template, message_id=f"extra-{index}").to_dict()).encode() + b"\n")
    original_size = path.stat().st_size

    def reject_full_load(*_args, **_kwargs):
        pytest.fail("append_once must not load full transcript")

    monkeypatch.setattr(store.messages, "recent_report", reject_full_load)
    tracemalloc.start()
    try:
        result = store.messages.append_once(request, dedupe_key="same")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result == first and path.stat().st_size == original_size
    assert peak < original_size // 3
    with pytest.raises(DataCorruptionError, match="different input"):
        store.messages.append_once({**request, "content": "改文"}, dedupe_key="same")
    appended = store.messages.append_once(request, dedupe_key="new")
    assert appended.message_id != first.message_id
    assert store.messages.append_once(request, dedupe_key="new") == appended


@pytest.mark.parametrize("bad_suffix", [b"broken\n", b"[]\n", b"{", b"\xff\n"])
def test_dedupe_match_does_not_hide_later_corruption(conversation, bad_suffix):
    store, tid = conversation
    request = {"thread_id": tid, "role": "user", "content": "same"}
    store.messages.append_once(request, dedupe_key="same")
    path = store.storage.message_path(tid)
    with path.open("ab") as handle:
        handle.write(bad_suffix)
    before = path.read_bytes()
    with pytest.raises(DataCorruptionError, match="unreadable"):
        store.messages.append_once(request, dedupe_key="same")
    assert path.read_bytes() == before


@pytest.mark.parametrize("blank", [" ", "\u00a0", "\u2028"])
def test_dedupe_keeps_blank_crlf_and_unicode_semantics(conversation, blank):
    store, tid = conversation
    row = _append(store, tid)
    original = replace(row, metadata={"dedupe_key": "same"})
    path = store.storage.message_path(tid)
    path.write_bytes((blank + "\r\n").encode() + json.dumps(original.to_dict(), ensure_ascii=False).encode() + b"\r\n")
    assert store.messages.append_once({"thread_id": tid, "role": row.role, "content": row.content}, dedupe_key="same") == original


@pytest.mark.parametrize("ending", ["valid_json", "whitespace"])
@pytest.mark.parametrize("same_key", [False, True])
def test_dedupe_rejects_uncommitted_tail_before_any_append(conversation, ending, same_key):
    store, tid = conversation
    first = _append(store, tid)
    path = store.storage.message_path(tid)
    row = replace(first, metadata={"dedupe_key": "same"})
    with path.open("ab") as handle:
        handle.write(json.dumps(row.to_dict()).encode() if ending == "valid_json" else b"   ")
    before = path.read_bytes()
    with pytest.raises(DataCorruptionError, match="unreadable"):
        store.messages.append_once({"thread_id": tid, "role": first.role, "content": first.content}, dedupe_key="same" if same_key else "new")
    assert path.read_bytes() == before


@pytest.mark.parametrize("raw", [b"[]\n", b"null\n", b"1\n", b"broken\n", b"{\"created_at\": []}\n", b"\xff\n", (json.dumps({"created_at": 10**400}) + "\n").encode()])
def test_page_bad_row_is_data_error_not_programmer_bug(conversation, raw):
    store, tid = conversation
    path = store.storage.message_path(tid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    rows, cursor, errors = store.messages.page_after_offset_report(tid, max_bytes=max(200, len(raw)))
    assert not rows and cursor == 0
    assert errors[0]["category"] == "data_corruption"


def test_dedupe_ignores_display_but_checks_it_and_keeps_first_key(conversation):
    from agent_py_agent.agent.conversation.display_checkpoint import DISPLAY_CHECKPOINT_ROLE

    store, tid = conversation
    first = _append(store, tid)
    path = store.storage.message_path(tid)
    display = replace(first, role=DISPLAY_CHECKPOINT_ROLE, content="not a real match", metadata={"dedupe_key": "same"})
    match = replace(first, metadata={"dedupe_key": "same"})
    duplicate = replace(match, content="later duplicate must not replace first")
    with path.open("wb") as handle:
        for row in (display, match, duplicate):
            handle.write(json.dumps(row.to_dict()).encode() + b"\n")
    before = path.read_bytes()
    assert store.messages.append_once({"thread_id": tid, "role": first.role, "content": first.content}, dedupe_key="same") == match
    assert path.read_bytes() == before


def test_large_row_uses_byte_budget_without_character_split(conversation):
    store, tid = conversation
    row = _append(store, tid, "中" * 30_000)
    size = store.storage.message_path(tid).stat().st_size
    assert size > 65_536
    assert store.messages.complete_offset_report(tid) == (size, [])
    assert store.messages.page_after_offset_report(tid, max_bytes=size) == ([row], size, [])
    rows, cursor, errors = store.messages.page_after_offset_report(tid, max_bytes=size - 1)
    assert not rows and cursor == 0 and errors[0]["error_type"] == "MessagePageBudgetExceeded"


def test_bounded_live_page_waits_for_oversized_incomplete_tail(conversation):
    store, tid = conversation
    row = _append(store, tid)
    path = store.storage.message_path(tid)
    end = path.stat().st_size
    with path.open("ab") as handle:
        handle.write(b"x" * 200_000)
    assert store.messages.page_after_offset_report(tid, after=end, max_bytes=20) == ([], end, [])
    rows, cursor, errors = store.messages.page_after_offset_report(tid, max_bytes=end)
    assert rows == [row] and cursor == end and not errors


def test_concurrent_idempotent_appends_share_original_lock(conversation):
    from concurrent.futures import ThreadPoolExecutor

    store, tid = conversation
    request = {"thread_id": tid, "role": "user", "content": "同一次消息"}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(store.messages.append_once, request, dedupe_key="one") for _ in range(12)]
        entries = [future.result(timeout=10) for future in futures]
    assert len({entry.message_id for entry in entries}) == 1
    rows, _, errors = store.messages.page_after_offset_report(tid)
    assert not errors and rows == [entries[0]]


def test_truncation_during_bounded_page_snapshot_does_not_ack(conversation, monkeypatch):
    from agent_py_agent.agent.conversation import message_scan

    store, tid = conversation
    _append(store, tid)
    path = store.storage.message_path(tid)
    after = path.stat().st_size
    _append(store, tid)
    original = message_scan.complete_jsonl_end

    # LLM: 在after边界校验后真实截短临时文件，仍执行原EOF读取，不伪造解析或存储结果。
    # 函数用途: 模拟冻结期间的违规截断，确保返回错误而不是正常空页。
    def truncate_before_snapshot(handle, size):
        with path.open("r+b") as writer:
            writer.truncate(0)
        return original(handle, size)

    monkeypatch.setattr(message_scan, "complete_jsonl_end", truncate_before_snapshot)
    rows, cursor, errors = store.messages.page_after_offset_report(tid, after=after, max_bytes=1000)
    assert not rows and cursor == after and errors
