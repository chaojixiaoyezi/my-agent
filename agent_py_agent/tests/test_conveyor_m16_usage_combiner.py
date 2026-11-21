"""用真实来源凭据、线程和 SQLite 验证用量投影提交。"""
import json
import multiprocessing
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Thread, get_ident
from time import monotonic, sleep

import pytest

from agent_py_agent.agent.common import json_io
from agent_py_agent.agent.common.json_io import locked_json_path
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.conversation.store_usage import ModelUsageStore
from agent_py_agent.agent.conversation.usage_sources import UsageIntent


def _store(directory):
    directory.mkdir(parents=True, exist_ok=True)
    store = ModelUsageStore(directory, require_thread=lambda _identifier: None, update_thread_atomic=None)
    store._usage_source_snapshot = lambda _identifier, **_options: (1, 2, (2,), True)
    store.revision = lambda _identifier: (2,)
    return store


def _tokens(index):
    return list(index.pending_directory.glob("*.json"))


def _finish(index, timeout=1):
    until = monotonic() + timeout
    while monotonic() < until:
        with os.scandir(index.pending_directory) as entries:
            if not any(entry.name.endswith('.json') for entry in entries):
                return
        sleep(0.005)
    remaining = len(_tokens(index))
    assert remaining == 0, f"{remaining} durable intents remain after {timeout}s"


def _rows(index):
    with index.read_connection() as connection:
        return [tuple(row) for row in connection.execute(
            "SELECT thread_id, minimum, maximum, revision, complete FROM sources ORDER BY thread_id"
        )]


def test_contended_submissions_leave_no_refused_projection(tmp_path, caplog):
    store = _store(tmp_path / "usage")
    index = store._source_index()
    for position in range(128):
        (index.directory / f"thread-{position}.jsonl").touch()
    ready = Barrier(128)

    def submit(position):
        identifier = f"thread-{position}"
        before, token = store._begin_usage_source(identifier)
        ready.wait(timeout=20)
        store._refresh_usage_source(identifier, before, token)

    with ThreadPoolExecutor(max_workers=128) as pool, locked_json_path(index.path):
        list(pool.map(submit, range(128)))
    _finish(index, timeout=10)
    assert not [record for record in caplog.records if "USAGE_SOURCE_PROJECTION_PARTIAL" in record.message]
    # 清理之后仍可能有一轮覆盖维护。取得同一索引锁再验证最终行。
    with locked_json_path(index.path, deadline_s=monotonic() + 5):
        assert len(_rows(index)) == 128


def test_128_publishers_at_20_per_second(tmp_path, caplog, record_property):
    store = _store(tmp_path / "usage")
    index = store._source_index()
    identifiers = [f"thread-{position}" for position in range(128)]
    index.pending_directory.mkdir(parents=True)
    for identifier in identifiers:
        (index.directory / f"{identifier}.jsonl").touch()
    for _ in range(8):
        if index.prepare(store, limit=128)["coverage"] == "complete":
            break
    else:
        pytest.fail("initial source coverage did not settle")
    with index.read_connection() as connection:
        initial_generation = int(index._get(connection, "generation"))
    # 测量投影服务速率。凭据发布的同步开销单独验证，定时环节只提交真实已存在的凭据。
    for position, identifier in enumerate(identifiers):
        for step in range(200):
            token = f"{position * 200 + step:032x}"
            (index.pending_directory / f"{token}.json").write_text(f'{{"thread_id":"{identifier}"}}', encoding="utf-8")
    before = index.stamps()
    begun = []
    ready = Barrier(len(identifiers), action=lambda: begun.append(monotonic()))

    def submit(position):
        identifier = identifiers[position]
        ready.wait(timeout=20)
        for step in range(200):
            due = begun[0] + step / 20
            remaining = due - monotonic()
            if remaining > 0:
                sleep(remaining)
            token = f"{position * 200 + step:032x}"
            store._refresh_usage_source(identifier, before, token)
        return 200

    with ThreadPoolExecutor(max_workers=128) as pool:
        assert sum(pool.map(submit, range(128))) == 25_600
    record_property("seconds_to_submit_25600", round(monotonic() - begun[0], 4))
    # Publisher pacing is measured separately from asynchronous settlement on a shared host.
    assert store.wait_window_sources(timeout=60), "the shared drainer did not retire"
    _finish(index)
    assert not [record for record in caplog.records if "USAGE_SOURCE_PROJECTION_PARTIAL" in record.message]
    with locked_json_path(index.path, deadline_s=monotonic() + 5):
        assert _rows(index) == [(identifier, 1.0, 2.0, "[2]", 1) for identifier in sorted(identifiers)]
        with index.read_connection() as connection:
            assert int(index._get(connection, "generation")) - initial_generation == 25_600


def test_reordered_intents_read_latest_snapshot_and_keep_epoch_mismatch(tmp_path):
    store = _store(tmp_path / "usage")
    index = store._source_index()
    revision = [1]
    store.revision = lambda _identifier: (revision[0],)
    store._usage_source_snapshot = lambda _identifier: (1, revision[0], (revision[0],), True)
    old = UsageIntent(index.begin("thread"), "thread", index.stamps(), store)
    revision[0] = 2
    new = UsageIntent(index.begin("thread"), "thread", index.stamps(), store)
    later = UsageIntent(index.begin("thread"), "thread", index.stamps(), store, epoch=-1)
    assert index.apply_intents([new, old, new, later], deadline=monotonic() + 2) == [new.token, old.token]
    assert _rows(index) == [("thread", 1.0, 2.0, "[2]", 1)]
    assert [path.stem for path in _tokens(index)] == [later.token]


def test_reader_cannot_pair_old_sqlite_snapshot_with_clean_token_directory(tmp_path):
    store = _store(tmp_path / "usage")
    index = store._source_index()
    token = index.begin("thread")
    index.apply_intents([UsageIntent(token, "thread", index.stamps(), store)], deadline=monotonic() + 2)

    def publish():
        new_token = index.begin("next")
        index.apply_intents([UsageIntent(new_token, "next", index.stamps(), store)], deadline=monotonic() + 2)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with pytest.raises(ValueError, match="intents changed during read"), index.read_connection() as old:
            assert old.execute("SELECT count(*) FROM sources").fetchone()[0] == 1
            pool.submit(publish).result(timeout=5)
            assert not _tokens(index)
            assert old.execute("SELECT count(*) FROM sources").fetchone()[0] == 1
    assert len(_rows(index)) == 2


def _exit_during_batch(directory, token, cutoff):
    store = _store(directory)
    index = store._source_index()
    install = index._install
    unlink = index._unlink_committed_tokens

    def exit_before_commit(*args, **kwargs):
        install(*args, **kwargs)
        os._exit(17)

    def exit_after_commit(tokens, *, parent):
        if cutoff == "after_cleanup":
            unlink(tokens, parent=parent)
        os._exit(17)

    if cutoff == "before_commit":
        index._install = exit_before_commit
    else:
        index._unlink_committed_tokens = exit_after_commit
    index.apply_intents([UsageIntent(token, "thread", index.stamps(), store)], deadline=monotonic() + 2)


@pytest.mark.parametrize("cutoff", ["before_commit", "after_commit", "after_cleanup"])
def test_process_exit_replays_canonical_bounds(tmp_path, cutoff):
    store = _store(tmp_path / "usage")
    index = store._source_index()
    token = index.begin("thread")
    child = multiprocessing.get_context("spawn").Process(target=_exit_during_batch, args=(index.directory, token, cutoff))
    child.start()
    try:
        child.join(20)
        assert child.exitcode == 17
    finally:
        if child.is_alive():
            child.kill()
            child.join()
        child.close()
    assert bool(_tokens(index)) == (cutoff != "after_cleanup")
    assert len(_rows(index)) == (cutoff != "before_commit")
    if _tokens(index):
        assert not index.prepare(store, deadline=monotonic() + 2)["errors"]
    assert not _tokens(index)
    assert _rows(index) == [("thread", 1.0, 2.0, "[2]", 1)]


def test_batch_preserves_token_fsync_count(tmp_path, monkeypatch):
    store = _store(tmp_path / "usage")
    index = store._source_index()
    warmup = index.begin("thread")
    index.update("thread", store._usage_source_snapshot("thread"), before_stamp=index.stamps(),
                 token=warmup, store=store)
    fsync = os.fsync
    calls = []
    caller = get_ident()

    def counted(fd):
        if get_ident() == caller:
            calls.append(True)
        return fsync(fd)

    monkeypatch.setattr(os, "fsync", counted)
    token = index.begin("thread")
    publication = len(calls)
    calls.clear()
    index.update("thread", store._usage_source_snapshot("thread"), before_stamp=index.stamps(), token=token, store=store)
    cleanup = len(calls)
    calls.clear()
    tokens = [index.begin("thread") for _ in range(3)]
    assert publication > 0 and len(calls) == 3 * publication
    calls.clear()
    index.apply_intents([UsageIntent(token, "thread", index.stamps(), store) for token in tokens],
                        deadline=monotonic() + 2)
    assert len(calls) == 3 * cleanup


@pytest.mark.skipif(json_io.fcntl is None, reason="independent file-descriptor flock")
def test_deadline_includes_file_lock_contention(tmp_path):
    path = tmp_path / "owner.json"
    handle = json_io._open_private_lock_descriptor(path.with_suffix(".json.lock"))
    try:
        json_io._flock_descriptor(handle)
        expiry = monotonic() + 0.025
        with pytest.raises(BlockingIOError), locked_json_path(path, deadline_s=expiry):
            pytest.fail("competing file lock admitted")
        assert monotonic() >= expiry
    finally:
        os.close(handle)
    with locked_json_path(path, deadline_s=monotonic() + 1):
        pass


def test_append_returns_while_its_projection_snapshot_is_held(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.usage_index import UsageIndex

    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "owner", "channel": "internal"})
    usage = store.model_usage
    source_bounds = UsageIndex.source_bounds
    entered, release, returned = Event(), Event(), Event()
    readers, failures = [], []

    def held(index):
        readers.append(get_ident())
        entered.set()
        assert release.wait(10)
        return source_bounds(index)

    def append():
        try:
            usage.append_once({"event_id": "held", "thread_id": thread.thread_id, "request_id": "held",
                               "model_calls": {"schema": "model_call_summary.v1", "total_tokens": 1}, "now": 1000})
        except BaseException as error:
            failures.append(error)
        finally:
            returned.set()

    monkeypatch.setattr(UsageIndex, "source_bounds", held)
    caller = Thread(target=append)
    caller.start()
    try:
        assert entered.wait(10), "the projection read never started"
        # The canonical append is durable; the caller must not wait for the projection read it elected.
        assert returned.wait(5), "append_once waited for projection I/O"
        assert not release.is_set() and not failures
        assert caller.ident not in readers
    finally:
        release.set()
        caller.join(10)
    index = usage._source_index()
    _finish(index, timeout=10)
    lines = usage._path(thread.thread_id).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["event_id"] for line in lines] == ["held"]
    with locked_json_path(index.path, deadline_s=monotonic() + 5):
        assert [(row[0], row[4]) for row in _rows(index)] == [(thread.thread_id, 1)]


def _append(usage, thread, event_id):
    return usage.append_once({"event_id": event_id, "thread_id": thread.thread_id, "request_id": event_id,
                              "model_calls": {"schema": "model_call_summary.v1", "total_tokens": 1}, "now": 1000})


def _held_snapshots(usage):
    """Hold the first projection snapshot on the drain worker until released; later reads pass through."""
    snapshot = usage._usage_source_snapshot
    entered, release = Event(), Event()

    def held(identifier, **options):
        if not release.is_set():
            entered.set()
            assert release.wait(10)
        return snapshot(identifier, **options)

    usage._usage_source_snapshot = held
    return entered, release


def test_reader_waits_for_the_drain_of_a_completed_append(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "owner", "channel": "internal"})
    usage = store.model_usage
    _append(usage, thread, "first")
    assert usage.wait_window_sources(10)
    # A fresh owner index needs a full inventory; the maintenance lane completes it before the measured append.
    _settle(usage._source_index(), usage)
    assert [row[0] for row in usage.window_source_plan().rows()] == [thread.thread_id]

    entered, release = _held_snapshots(usage)
    _append(usage, thread, "second")
    assert entered.wait(10), "the projection read never started"
    rows, failures = [], []

    def read():
        try:
            rows.extend(usage.window_source_plan().rows())
        except BaseException as error:
            failures.append(error)

    reader = Thread(target=read)
    reader.start()
    try:
        # The append returned with its intent still pending; the reader waits for that drain instead of failing.
        reader.join(0.2)
        assert reader.is_alive() and not failures
    finally:
        release.set()
        reader.join(10)
    assert not failures
    assert rows == [(thread.thread_id, tuple(usage.revision(thread.thread_id)))]


def test_back_to_back_appends_restore_coverage_with_bounded_repair(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.usage_sources import UsageSourceIndex

    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "owner", "channel": "internal"})
    usage = store.model_usage
    _append(usage, thread, "first")
    assert usage.wait_window_sources(10)
    _settle(usage._source_index(), usage)
    slices = []
    prepare = UsageSourceIndex.prepare

    def counted(self, *args, **kwargs):
        slices.append(kwargs.get("limit"))
        return prepare(self, *args, **kwargs)

    monkeypatch.setattr(UsageSourceIndex, "prepare", counted)
    entered, release = _held_snapshots(usage)
    _append(usage, thread, "second")
    assert entered.wait(10), "the projection read never started"
    # The next append starts before the drainer has read anything for the previous one.
    _append(usage, thread, "third")
    release.set()
    assert usage.wait_window_sources(10)
    # SQLite sidecar churn can invalidate the writer's stamp. Repair can spend only the two queued slices.
    assert len(slices) <= 2
    assert [row[0] for row in usage.window_source_plan().rows()] == [thread.thread_id]


@pytest.mark.parametrize("first_slice", ["examines_nothing", "truncated"])
def test_repair_budget_survives_a_drain_window_that_ends_early(tmp_path, monkeypatch, first_slice):
    from agent_py_agent.agent.conversation.usage_sources import UsageSourceIndex

    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "owner", "channel": "internal"})
    usage = store.model_usage
    prepare, reports = UsageSourceIndex.prepare, []

    def first_window_ends_early(self, source, *, limit, deadline=None):
        if reports:
            report = prepare(self, source, limit=limit, deadline=deadline)
        elif first_slice == "examines_nothing":
            # The window ends inside the slice's own setup, before any source is examined.
            report = {"coverage": "partial", "sources_examined": 0, "cursor": {"phase": 0, "epoch": 1}, "errors": []}
        else:
            # The window ends after part of the inventory.
            report = prepare(self, source, limit=min(limit, 4), deadline=deadline)
        reports.append(report)
        return report

    monkeypatch.setattr(UsageSourceIndex, "prepare", first_window_ends_early)
    # The fresh owner index needs a full inventory, and this single intent carries one repair slice of budget.
    _append(usage, thread, "first")
    assert usage.wait_window_sources(10)
    assert reports[0]["coverage"] == "partial" and reports[0]["sources_examined"] < 16
    assert len(reports) >= 2 and reports[-1]["coverage"] == "complete", reports
    assert [row[0] for row in usage.window_source_plan().rows()] == [thread.thread_id]


def test_repair_that_never_examines_a_source_returns_to_maintenance(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.usage_sources import UsageSourceIndex

    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "owner", "channel": "internal"})
    usage = store.model_usage
    reports = []

    def examines_nothing(self, source, *, limit, deadline=None):
        reports.append(limit)
        return {"coverage": "partial", "sources_examined": 0, "cursor": {"phase": 0, "epoch": 1}, "errors": []}

    with monkeypatch.context() as patch:
        patch.setattr(UsageSourceIndex, "prepare", examines_nothing)
        _append(usage, thread, "first")
        # Slices that examine nothing are not progress: the drainer retires after one retry window.
        assert usage.wait_window_sources(5), "the drainer kept a pool worker for a repair that never advanced"
        assert reports
        with pytest.raises(ValueError):
            usage.window_source_plan()
    # The durable intent and the partial coverage stay for the maintenance lane, which completes them.
    _settle(usage._source_index(), usage)
    assert [row[0] for row in usage.window_source_plan().rows()] == [thread.thread_id]


def test_coalesced_appends_keep_one_repair_slice_each(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    threads = [store.threads.get_or_create({"canonical_user_id": "owner", "channel": "internal",
                                            "channel_conversation_id": f"conversation-{n}"}) for n in range(6)]
    assert len({thread.thread_id for thread in threads}) == 6
    usage = store.model_usage
    entered, release = _held_snapshots(usage)
    _append(usage, threads[0], "event-0")
    assert entered.wait(10), "the projection read never started"
    # The fresh owner index needs a full inventory; these intents queue behind the held drain as one batch.
    for number, thread in enumerate(threads[1:], start=1):
        _append(usage, thread, f"event-{number}")
    release.set()
    assert usage.wait_window_sources(10)
    assert sorted(row[0] for row in usage.window_source_plan().rows()) == sorted(t.thread_id for t in threads)


def test_repair_yields_to_an_inflight_writer_without_spending_its_budget(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import store_usage

    store = ConversationStore(tmp_path / "conversations")
    settled, added, inflight = (store.threads.get_or_create({
        "canonical_user_id": "owner", "channel": "internal", "channel_conversation_id": name,
    }) for name in ("settled", "added", "inflight"))
    usage = store.model_usage
    _append(usage, settled, "settled")
    assert usage.wait_window_sources(10)
    _settle(usage._source_index(), usage)
    held_path = usage._path(inflight.thread_id)
    entered, release = Event(), Event()
    recover = store_usage.recover_pending

    def held(path, *args, **kwargs):
        if path == held_path and not release.is_set():
            entered.set()
            assert release.wait(10)
        return recover(path, *args, **kwargs)

    monkeypatch.setattr(store_usage, "recover_pending", held)
    writer = Thread(target=_append, args=(usage, inflight, "inflight"))
    writer.start()
    try:
        # This writer holds its ledger lock with a published intent while the next append forces a rescan.
        assert entered.wait(10)
        _append(usage, added, "added")
        sleep(0.2)
    finally:
        release.set()
        writer.join(10)
    assert usage.wait_window_sources(10)
    expected = sorted(thread.thread_id for thread in (settled, added, inflight))
    assert sorted(row[0] for row in usage.window_source_plan().rows()) == expected


def _settle(index, store, *, limit=128, attempts=64):
    reports = []
    for _ in range(attempts):
        reports.append(index.prepare(store, limit=limit, deadline=monotonic() + 2))
        if reports[-1]["coverage"] == "complete":
            return reports
    pytest.fail(f"usage source coverage did not settle: {reports[-3:]}")


def _sources(tmp_path, identifiers):
    store = _store(tmp_path / "usage")
    index = store._source_index()
    for identifier in identifiers:
        (index.directory / f"{identifier}.jsonl").touch()
    _settle(index, store)
    return store, index


def _canonical_rows(tmp_path, directory):
    """Rows of a fresh sequential rebuild over a copy of the same canonical inventory."""
    copy = tmp_path / "canonical" / "usage"
    shutil.copytree(directory, copy, ignore=shutil.ignore_patterns(".*"))
    store = _store(copy)
    index = store._source_index()
    _settle(index, store)
    return _rows(index)


def _damage(path, raw):
    """Rewrite a published token in place, as a torn write would: same file identity, unreadable payload."""
    with open(path, "r+b") as handle:
        handle.truncate(0)
        handle.write(raw)
    info = path.stat()
    return raw, (info.st_dev, info.st_ino)


def _assert_quarantined(index, token, evidence):
    raw, identity = evidence
    moved = index.quarantine_directory / f"{token}.json"
    assert not (index.pending_directory / moved.name).exists()
    assert moved.read_bytes() == raw and (moved.stat().st_dev, moved.stat().st_ino) == identity


@pytest.mark.parametrize("damage", ["truncated", "other_thread"])
def test_mixed_batch_isolates_one_damaged_token_and_full_rebuild_restores_coverage(tmp_path, damage):
    store, index = _sources(tmp_path, ("thread-a", "thread-b", "thread-x"))
    first = UsageIntent(index.begin("thread-a"), "thread-a", index.stamps(), store)
    bad = UsageIntent(index.begin("thread-x"), "thread-x", index.stamps(), store)
    raw = b'{"thread_id": "thr' if damage == "truncated" else b'{"thread_id": "thread-a"}'
    evidence = _damage(index.pending_directory / f"{bad.token}.json", raw)
    last = UsageIntent(index.begin("thread-b"), "thread-b", index.stamps(), store)
    # The damaged member neither rolls back nor blocks its valid siblings.
    assert index.apply_intents([first, bad, last], deadline=monotonic() + 2) == [first.token, last.token]
    assert not _tokens(index)
    _assert_quarantined(index, bad.token, evidence)
    # Its proof obligation now belongs to a full canonical rebuild; coverage stays unknown until that completes.
    with pytest.raises(ValueError, match="incomplete"):
        index.plan(since=0, until=float("inf"))
    reports = _settle(index, store, limit=1)
    assert len(reports) > 3 and not [report for report in reports if report["errors"]]
    assert _rows(index) == _canonical_rows(tmp_path, index.directory)
    assert index.plan(since=0, until=float("inf")).generation
    _assert_quarantined(index, bad.token, evidence)


def test_maintenance_isolates_a_damaged_token_and_settles_its_valid_siblings(tmp_path):
    store, index = _sources(tmp_path, ("thread-a", "thread-b", "thread-x"))
    # Publishers that exited before submitting leave valid tokens around one torn token.
    valid = [index.begin("thread-a"), index.begin("thread-b")]
    bad = index.begin("thread-x")
    evidence = _damage(index.pending_directory / f"{bad}.json", b"")
    with pytest.raises(ValueError, match="incomplete"):
        index.plan(since=0, until=float("inf"))
    reports = _settle(index, store, limit=1)
    assert [report["cursor"] for report in reports].count({"isolated_intent": True}) == 1
    assert not [report for report in reports if report["errors"]]
    assert not _tokens(index)
    assert sorted(path.stem for path in index.quarantine_directory.iterdir()) == [bad]
    assert all(not (index.pending_directory / f"{token}.json").exists() for token in valid)
    _assert_quarantined(index, bad, evidence)
    assert _rows(index) == _canonical_rows(tmp_path, index.directory)


def _exit_after_isolation_commit(directory):
    store = _store(directory)
    index = store._source_index()

    def exit_before_move(_tokens):
        os._exit(17)

    index._quarantine_tokens = exit_before_move
    index.prepare(store, limit=1, deadline=monotonic() + 10)


def test_restart_between_isolation_commit_and_move_isolates_again_and_rebuilds(tmp_path):
    store, index = _sources(tmp_path, ("thread-a", "thread-x"))
    bad = index.begin("thread-x")
    evidence = _damage(index.pending_directory / f"{bad}.json", b'{"thread_id": ')
    child = multiprocessing.get_context("spawn").Process(target=_exit_after_isolation_commit, args=(index.directory,))
    child.start()
    try:
        child.join(30)
        assert child.exitcode == 17
    finally:
        if child.is_alive():
            child.kill()
            child.join()
        child.close()
    # The rebuild obligation was committed before the move; the damaged token is still the visible evidence.
    assert [path.stem for path in _tokens(index)] == [bad]
    with index.read_connection() as connection:
        assert (index._get(connection, "complete"), index._get(connection, "scan")) == ("0", "{}")
    with pytest.raises(ValueError, match="incomplete"):
        index.plan(since=0, until=float("inf"))
    restarted = _store(index.directory)
    _settle(restarted._source_index(), restarted, limit=1)
    _assert_quarantined(index, bad, evidence)
    assert _rows(index) == _canonical_rows(tmp_path, index.directory)


@pytest.mark.parametrize("name_kind", ["oversized_thread", "backup"])
def test_cold_usage_repair_isolates_unusable_pending_entries(tmp_path, name_kind):
    store, index = _sources(tmp_path, ("thread-a", "thread-b"))
    index.begin("thread-a")
    if name_kind == "backup":
        name = "restored.json.bak"
        (index.pending_directory / name).touch()
    else:
        name = index.begin("thread-b") + ".json"
    raw, identity = _damage(index.pending_directory / name, json.dumps({"thread_id": "x" * 400}).encode())

    reopened = _store(index.directory)
    repaired = reopened._source_index()
    reports = _settle(repaired, reopened, limit=1)
    assert all(not result["errors"] for result in reports)
    assert not (index.pending_directory / name).exists()
    moved = index.quarantine_directory / name
    assert moved.read_bytes() == raw and (moved.stat().st_dev, moved.stat().st_ino) == identity
    assert _rows(repaired) == _canonical_rows(tmp_path, repaired.directory)
    assert repaired.plan(since=0, until=float("inf")).generation
