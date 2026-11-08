"""单线程账本分段、费用结转、待处理前缀与确定性长跑的真实文件回归。"""
from __future__ import annotations

import hashlib
import json
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from functools import partial
from pathlib import Path

import pytest

from agent_py_agent.agent.conversation import (
    ConversationStore,
    store_observations,
    store_usage,
    usage_index,
)
from agent_py_agent.agent.conversation import thread_ledger_segments as segments
from agent_py_agent.agent.conversation.models import ObservationEvent
from agent_py_agent.agent.runtime_errors import DataCorruptionError


@pytest.fixture
def ledger_store(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "user", "now": 1})
    return store, thread.thread_id


def _request(thread_id, number, *, amount=7):
    bucket = {"logical_model_turn_count": 1, "physical_model_attempt_count": 1,
              "accounted_input_tokens": amount, "output_tokens": 2, "total_tokens": amount + 2,
              "status_counts": {"finished": 1}, "backends": ["test"], "models": ["test"],
              "backend_attempt_counts": {"test": 1}, "model_attempt_counts": {"test": 1},
              "usage_breakdown": {"provider": {"input_tokens": amount, "output_tokens": 2, "call_count": 1},
                                  "estimated": {"input_tokens": 3, "call_count": 1}}}
    return {"thread_id": thread_id, "event_id": f"usage-{number}", "request_id": "request",
            "run_id": "run", "task_id": "task", "source": "main", "now": number + 2,
            "model_calls": {"schema": "model_call_summary.v1", **bucket,
                            "purpose_breakdown": {"schema": "model_call_purpose_breakdown.v1", "main": bucket}}}


def _wire_bytes(event):
    return (json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True) + "\n").encode()


def _inject_limit(monkeypatch, store, maximum):
    # 只通过真实函数参数缩小阈值，不增加配置、不替换封段或账本实现。
    monkeypatch.setattr(store.model_usage, "_append_indexed",
                        partial(store.model_usage._append_indexed, max_bytes=maximum))


def _active_size(path):
    return path.stat().st_size if path.exists() else 0


def test_usage_three_seals_exact_totals_and_byte_cursor_pages(ledger_store, monkeypatch):
    store, thread = ledger_store
    maximum = 3500
    _inject_limit(monkeypatch, store, maximum)
    path = store.model_usage._path(thread)
    events = [store.model_usage.append_once(_request(thread, number, amount=number + 1)) for number in range(10)]
    manifest = segments.read_manifest(segments.archive_dir_for(path))
    assert len(manifest["segments"]) == 3
    assert manifest["coverage"] == "complete"
    assert _active_size(path) < maximum
    for entry, sealed in zip(manifest["segments"], segments.iter_segment_paths(segments.archive_dir_for(path)), strict=True):
        raw = sealed.read_bytes()
        assert entry["bytes"] == len(raw)
        assert entry["rows"] == raw.count(b"\n")
        assert entry["sha256"] == hashlib.sha256(raw).hexdigest()
        assert entry["carry"]["totals"]
    summary = store.model_usage.summary(thread)
    assert summary == store.model_usage._exact_summary(thread)
    assert summary["event_count"] == len(events)
    assert summary["provider"]["input_tokens"] == sum(event.model_calls["usage_breakdown"]["provider"]["input_tokens"] for event in events)
    assert summary["purpose_totals_known"] is True
    cursor, paged = 0, []
    while True:
        page, errors = store.model_usage.events_report(thread, limit=2, after=cursor)
        assert not errors
        if not page:
            break
        paged.extend(page)
        cursor += sum(len(_wire_bytes(event)) for event in page)
    assert paged == events
    assert ConversationStore(store.storage.root).model_usage.summary(thread) == summary


def test_usage_cold_index_seeds_carry_without_reading_archived_rows(ledger_store, monkeypatch):
    store, thread = ledger_store
    _inject_limit(monkeypatch, store, 4000)
    for number in range(11):
        store.model_usage.append_once(_request(thread, number))
    expected = store.model_usage.summary(thread)
    store.model_usage._index(thread).path.unlink()
    reader = usage_index._reader

    def active_only(path):
        assert path.parent == store.storage.model_usage_dir, "summary 不应读取封存正文"
        return reader(path)

    with monkeypatch.context() as patch:
        patch.setattr(usage_index, "_reader", active_only)
        assert store.model_usage.summary(thread) == expected
    assert store.model_usage.append_once(_request(thread, 0)).event_id == "usage-0"
    assert store.model_usage.summary(thread) == expected


def test_usage_snapshot_prior_and_conflicts_survive_rotation_and_index_loss(ledger_store, monkeypatch):
    store, thread = ledger_store
    _inject_limit(monkeypatch, store, 1)
    requests, events = [], []
    for number in range(1, 5):
        request = _request(thread, number, amount=number * 7)
        calls = request["model_calls"]
        calls["usage_scope_id"] = "scope"
        for key in ("logical_model_turn_count", "physical_model_attempt_count"):
            calls[key] = number
        for key in ("output_tokens", "total_tokens"):
            calls[key] = 2 * number if key == "output_tokens" else 9 * number
        calls["status_counts"] = {"finished": number}
        calls["backend_attempt_counts"] = calls["model_attempt_counts"] = {"test": number}
        calls["usage_breakdown"] = {"provider": {"input_tokens": 7 * number, "output_tokens": 2 * number, "call_count": number},
                                    "estimated": {"input_tokens": 3 * number, "call_count": number}}
        calls.pop("purpose_breakdown")
        requests.append(request)
        events.append(store.model_usage.append_snapshot_once(request))
    assert [event.model_calls["usage_breakdown"]["provider"]["input_tokens"] for event in events] == [7] * 4
    store.model_usage._index(thread).path.unlink()
    reopened = ConversationStore(store.storage.root).model_usage
    assert reopened.summary(thread)["provider"]["input_tokens"] == 28
    assert reopened.append_snapshot_once(requests[0]) == events[0]
    conflict = {**requests[0], "model_calls": {**requests[0]["model_calls"], "output_tokens": 999}}
    with pytest.raises(DataCorruptionError, match="reused"):
        reopened.append_snapshot_once(conflict)
    assert reopened.summary(thread)["event_count"] == 4


@pytest.mark.parametrize("damage", ["segment", "manifest"])
def test_missing_archive_data_stays_partial_and_does_not_fabricate_zero(ledger_store, monkeypatch, damage):
    store, thread = ledger_store
    _inject_limit(monkeypatch, store, 1)
    events = [store.model_usage.append_once(_request(thread, number)) for number in range(3)]
    directory = segments.archive_dir_for(store.model_usage._path(thread))
    if damage == "segment":
        segments.iter_segment_paths(directory)[1].unlink()
    else:
        (directory / "manifest.json").unlink()
    manifest = segments.read_manifest(directory)
    assert manifest["coverage"] == "partial"
    if damage == "manifest":
        assert all(entry["carry"] is None for entry in manifest["segments"])
        assert (directory / "manifest.json").exists()
    rows, errors = store.model_usage.events_report(thread)
    assert rows == ([events[0], events[2]] if damage == "segment" else events)
    assert any(error.get("coverage") == "partial" for error in errors)
    with pytest.raises(DataCorruptionError, match="coverage=partial"):
        store.model_usage.summary(thread)


def test_truncated_archive_rebuild_finishes_at_eof_with_partial_coverage(ledger_store, monkeypatch):
    store, thread = ledger_store
    _inject_limit(monkeypatch, store, 2000)
    for number in range(2):
        store.model_usage.append_once(_request(thread, number))
    index = store.model_usage._index(thread)
    path = segments.iter_segment_paths(segments.archive_dir_for(index.source))[0]
    raw = path.read_bytes()
    path.write_bytes(raw[:raw.index(b"\n") + 1])
    index.path.unlink()
    reader, calls = usage_index._reader, []

    def bounded_reader(source):
        calls.append(source)
        assert len(calls) <= 2, "截短段在 EOF 处重复打开，回填没有终止"
        return reader(source)

    monkeypatch.setattr(usage_index, "_reader", bounded_reader)
    with index._connection() as conn:
        assert index.prepare(conn)
        assert index._get(conn, "coverage") == "partial"
        with pytest.raises(DataCorruptionError, match="coverage=partial"):
            index.summary(conn)


def test_observation_seals_only_confirmed_prefix_and_recent_spans_segments(ledger_store):
    store, thread = ledger_store
    known = [store.observations.append({"thread_id": thread, "_observation_id": f"known-{number}",
                                       "requires_main_agent": True, "summary": "已处理", "now": number + 2}) for number in range(3)]
    store.observations.mark_handled([event.observation_id for event in known], now=20)
    pending = store.observations.append({"thread_id": thread, "_observation_id": "pending",
                                         "requires_main_agent": True, "summary": "待处理", "now": 10})
    path = store.storage.observation_path(thread)
    tail_limit = len(_wire_bytes(pending)) * 2 + 100
    before = store.observations.unhandled_requiring_main(limit=0)
    assert before == [pending]
    newest = store.observations.append({"thread_id": thread, "_observation_id": "newest", "summary": "新观察", "now": 11},
                                       max_bytes=tail_limit)
    manifest = segments.read_manifest(segments.archive_dir_for(path))
    assert len(manifest["segments"]) == 1
    assert manifest["segments"][0]["rows"] == 3
    assert _active_size(path) <= tail_limit
    active_ids = [json.loads(raw)["observation_id"] for raw in path.read_bytes().split(b"\n") if raw]
    assert active_ids == [pending.observation_id, newest.observation_id]
    assert store.observations.unhandled_requiring_main(limit=0) == before
    recent, errors = store.observations.recent_report(thread, limit=4)
    assert not errors
    assert [event.observation_id for event in recent] == [event.observation_id for event in [*known[1:], pending, newest]]
    assert all(event.handled_at == 20 for event in recent[:2])
    assert store.observations.recent_report(thread, include_handled=False, limit=0) == ([pending, newest], [])
    full, errors = store.observations.recent_report(thread, limit=0)
    assert not errors and len(full) == 5
    reopened = ConversationStore(store.storage.root)
    assert reopened.observations.unhandled_requiring_main(limit=0) == before
    assert reopened.observations.recent_report(thread, limit=4) == (recent, [])


def test_observation_pending_first_and_oversize_tail_are_never_archived(ledger_store):
    store, thread = ledger_store
    for number in range(10):
        store.observations.append({"thread_id": thread, "_observation_id": f"pending-{number}",
                                   "requires_main_agent": True, "now": number + 2}, max_bytes=100)
    path = store.storage.observation_path(thread)
    assert not segments.archive_dir_for(path).exists()
    assert len(store.observations.unhandled_requiring_main(limit=0)) == 10


def test_all_handled_observations_can_seal_whole_active_and_missing_files_report_partial(ledger_store, monkeypatch):
    store, thread = ledger_store
    for number in range(3):
        key = f"handled-{number}"
        store.observations.mark_handled([key], now=20)
        store.observations.append({"thread_id": thread, "_observation_id": key}, max_bytes=1)
    path = store.storage.observation_path(thread)
    directory = segments.archive_dir_for(path)
    assert not path.exists()
    with monkeypatch.context() as patch:
        patch.setattr(store_observations, "read_manifest", lambda *_args: pytest.fail("有限近期窗口不应解析全部历史清单"))
        recent, errors = store.observations.recent_report(thread, limit=2)
    assert not errors and [event.observation_id for event in recent] == ["handled-1", "handled-2"]
    assert store.observations.unhandled_requiring_main(limit=0) == []
    segments.iter_segment_paths(directory)[-1].unlink()
    rows, errors = store.observations.recent_report(thread, limit=5)
    assert [event.observation_id for event in rows] == ["handled-0", "handled-1"]
    assert any(error.get("coverage") == "partial" for error in errors)
    (directory / "manifest.json").unlink()
    assert segments.read_manifest(directory)["coverage"] == "partial"


@pytest.mark.parametrize("cut", ["before_copy", "after_copy", "after_tail"])
def test_prepared_prefix_recovery_does_not_duplicate_or_drop_rows(tmp_path, monkeypatch, cut):
    path = tmp_path / "thread.jsonl"
    raw = b'{"id":1}\n{"id":2}\n{"id":3}\n'
    path.write_bytes(raw)
    boundary = len(b'{"id":1}\n{"id":2}\n')
    copy_prefix, write_tail, publish = segments._copy_prefix, segments.write_bytes_atomic_beneath, segments._publish

    def copy(*args):
        if cut == "before_copy":
            raise OSError("interrupted")
        copy_prefix(*args)

    def tail(*args):
        if cut == "after_copy":
            raise OSError("interrupted")
        return write_tail(*args)

    def finish(directory, manifest):
        if cut == "after_tail" and not manifest.get("pending"):
            raise OSError("interrupted")
        return publish(directory, manifest)

    with monkeypatch.context() as patch:
        patch.setattr(segments, "_copy_prefix", copy)
        patch.setattr(segments, "write_bytes_atomic_beneath", tail)
        # 跳过空清单的首次发表，仅在完成封段后注入失败。
        patch.setattr(segments, "_publish", lambda directory, manifest:
                      finish(directory, manifest) if manifest["segments"] or manifest.get("pending") else publish(directory, manifest))
        from agent_py_agent.agent.common.json_io import locked_json_path
        with locked_json_path(path), pytest.raises(OSError, match="interrupted"):
            segments.seal_if_due(path, max_bytes=20, sealable_prefix_end=boundary)
    with locked_json_path(path):
        segments.recover_pending(path)
    paths = segments.iter_segment_paths(segments.archive_dir_for(path))
    assert b"".join(item.read_bytes() for item in [*paths, path]) == raw
    assert not segments.read_manifest(segments.archive_dir_for(path)).get("pending")


def test_usage_filesystem_commit_survives_projection_rollback(ledger_store, monkeypatch):
    store, thread = ledger_store
    _inject_limit(monkeypatch, store, 1)
    request = _request(thread, 1)

    def fail(*_args):
        raise OSError("projection interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(usage_index.UsageIndex, "sealed", fail)
        with pytest.raises(OSError, match="projection interrupted"):
            store.model_usage.append_once(request)
    reopened = ConversationStore(store.storage.root).model_usage
    assert reopened.summary(thread)["provider"]["input_tokens"] == 7
    assert reopened.append_once(request).event_id == request["event_id"]
    assert reopened.summary(thread)["event_count"] == 1


@pytest.mark.parametrize("kind", ["usage", "observations"])
def test_sealing_and_second_writer_share_the_original_lock(ledger_store, monkeypatch, kind):
    store, thread = ledger_store
    held, trying, release = threading.Event(), threading.Event(), threading.Event()

    def append(number):
        if kind == "usage":
            return store.model_usage.append_once(_request(thread, number))
        return store.observations.append({"thread_id": thread, "_observation_id": f"obs-{number}"}, max_bytes=1)

    if kind == "usage":
        _inject_limit(monkeypatch, store, 1)
        module, lock_name = store_usage, "locked_file_transition"
    else:
        module, lock_name = store_observations, "locked_json_path"
        store.observations.mark_handled(["obs-0", "obs-1"], now=20)
    original_lock, original_seal = getattr(module, lock_name), module.seal_if_due

    @contextmanager
    def observed_lock(*args, **kwargs):
        if threading.current_thread().name.endswith("_1"):
            trying.set()
        with original_lock(*args, **kwargs):
            yield

    def paused_seal(*args, **kwargs):
        result = original_seal(*args, **kwargs)
        if not held.is_set():
            held.set()
            assert release.wait(10)
        return result

    monkeypatch.setattr(module, lock_name, observed_lock)
    monkeypatch.setattr(module, "seal_if_due", paused_seal)
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="sealing") as pool:
        first = pool.submit(append, 0)
        try:
            assert held.wait(5)
            second = pool.submit(append, 1)
            assert trying.wait(5)
            assert not second.done()
        finally:
            release.set()
        first.result(timeout=10)
        second.result(timeout=10)
    if kind == "usage":
        events, errors = store.model_usage.events_report(thread)
        assert not errors and [event.event_id for event in events] == ["usage-0", "usage-1"]
        assert store.model_usage.summary(thread)["event_count"] == 2
    else:
        events, errors = store.observations.recent_report(thread, limit=0)
        assert not errors and [event.observation_id for event in events] == ["obs-0", "obs-1"]


def test_steady_usage_summary_and_append_do_not_scan_archives(ledger_store, monkeypatch):
    store, thread = ledger_store
    _inject_limit(monkeypatch, store, 4000)
    for number in range(9):
        store.model_usage.append_once(_request(thread, number))
    expected = store.model_usage.summary(thread)

    def forbidden(*_args, **_kwargs):
        pytest.fail("稳态不得读取段列表或扫描归档正文")

    monkeypatch.setattr(usage_index, "read_manifest", forbidden)
    monkeypatch.setattr(segments, "read_manifest", forbidden)
    for _ in range(10):
        assert store.model_usage.summary(thread) == expected
    store.model_usage.append_once(_request(thread, 10))
    assert store.model_usage.summary(thread)["event_count"] == 10


def test_hundred_day_deterministic_simulation_keeps_active_bounded(ledger_store, monkeypatch):
    store, thread = ledger_store
    maximum = 4000
    _inject_limit(monkeypatch, store, maximum)
    rng = random.Random(20261107)
    total_bytes, largest_row, token_sum, count = 0, 0, 0, 0
    previous = None
    for day in range(100):
        for _ in range(1 + rng.randrange(3)):
            amount = rng.randrange(1, 50)
            event = store.model_usage.append_once(_request(thread, count, amount=amount))
            size = len(_wire_bytes(event))
            total_bytes += size
            largest_row = max(largest_row, size)
            token_sum += amount
            count += 1
            assert _active_size(store.model_usage._path(thread)) < maximum
        if previous:
            store.observations.mark_handled([previous.observation_id], now=day + 200)
        previous = store.observations.append({"thread_id": thread, "_observation_id": f"day-{day}",
                                              "summary": "确定性观察", "requires_main_agent": True, "now": day + 2},
                                             max_bytes=maximum)
        assert _active_size(store.storage.observation_path(thread)) < maximum
    manifest = segments.read_manifest(segments.archive_dir_for(store.model_usage._path(thread)))
    sealed = len(manifest["segments"])
    assert total_bytes // (maximum + largest_row) <= sealed <= total_bytes // maximum
    summary = store.model_usage.summary(thread)
    assert summary["event_count"] == count
    assert summary["provider"]["input_tokens"] == token_sum
    assert summary == store.model_usage._exact_summary(thread)
    assert store.observations.unhandled_requiring_main(limit=0) == [previous]
    assert len(store.observations.recent(thread, limit=0)) == 100
    assert list(Path(store.storage.model_usage_dir).glob("archive/*/seg-*.jsonl"))


def test_segment_paths_are_ordered_and_rebuilt_with_unknown_carry(tmp_path):
    directory = tmp_path / "archive" / "thread"
    directory.mkdir(parents=True)
    for sequence in (3, 1, 2):
        (directory / f"seg-{sequence:06d}.jsonl").write_bytes(b"{}\n")
    assert [path.name for path in segments.iter_segment_paths(directory)] == [f"seg-{sequence:06d}.jsonl" for sequence in (1, 2, 3)]
    manifest = segments.read_manifest(directory)
    assert manifest["coverage"] == "partial"
    assert all(entry["carry"] is None for entry in manifest["segments"])


def test_unknown_observation_ack_coverage_never_seals(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "user"}).thread_id
    from agent_py_agent.agent.conversation.observation_index import ObservationIndexIncomplete

    def partial_ack(*_args, **_kwargs):
        raise ObservationIndexIncomplete("partial")

    monkeypatch.setattr(store.observations._index, "handled", partial_ack)
    event = store.observations.append({"thread_id": thread, "_observation_id": "unknown"}, max_bytes=1)
    assert event.observation_id == "unknown"
    assert not segments.archive_dir_for(store.storage.observation_path(thread)).exists()
    assert ObservationEvent.from_dict(json.loads(store.storage.observation_path(thread).read_text())) == event


@pytest.mark.parametrize("kind", ["usage", "observations"])
def test_writer_past_thread_precheck_cannot_recreate_retired_ledger(tmp_path, monkeypatch, kind):
    """真实写者在线程预检查后排队，退休提交后不能重新创建无主账本。"""
    from agent_py_agent.agent.memory_store import retention_apply
    from agent_py_agent.tests.test_rework_r9_conversation_transcript_retention import (
        NOW,
        OLD,
        _owner,
    )

    home, service = _owner(tmp_path)
    store = ConversationStore(Path(home.owner_home_dir) / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "queued-ledger", "now": OLD}).thread_id
    if kind == "usage":
        writer, module, lock_name = store.model_usage, store_usage, "locked_file_transition"
        path = writer._path(thread)

        def append(number):
            return writer.append_once(_request(thread, number))
    else:
        writer, module, lock_name = store.observations, store_observations, "locked_json_path"
        path = store.storage.observation_path(thread)

        def append(number):
            return writer.append({"thread_id": thread, "_observation_id": f"queued-{number}", "now": OLD})

    append(0)
    for name in ("channel_bindings.json", "user_latest_threads.json"):
        index = store.storage.root / name
        values = json.loads(index.read_text())
        index.write_text(json.dumps({key: value for key, value in values.items() if value != thread}))
    action, = [item for item in service.plan(now=NOW).actions
               if item.category == "conversation" and item.record_id == thread]
    attempted, release = threading.Event(), threading.Event()
    original_lock = getattr(module, lock_name)
    outcomes, errors = [], []

    @contextmanager
    def queued_lock(*args, **kwargs):
        if threading.current_thread().name == "queued-ledger-writer":
            attempted.set()
            assert release.wait(10)
        with original_lock(*args, **kwargs):
            yield

    def run():
        try:
            outcomes.append(append(1))
        except Exception as error:
            errors.append(error)

    monkeypatch.setattr(module, lock_name, queued_lock)
    worker = threading.Thread(target=run, name="queued-ledger-writer", daemon=True)
    worker.start()
    try:
        assert attempted.wait(5)
        retired = retention_apply._trash_conversation(home=home, action=action, now=NOW)
        assert retired.status == "trashed"
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive()
    assert not store.storage.thread_path(thread).exists()
    assert not path.exists(), {"kind": kind, "outcomes": outcomes, "errors": errors}
    assert not segments.archive_dir_for(path).exists()
    assert not outcomes
    assert len(errors) == 1 and isinstance(errors[0], (KeyError, FileNotFoundError))
