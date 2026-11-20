"""用真实来源凭据、线程和 SQLite 验证用量投影提交。"""
import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, get_ident
from time import monotonic, sleep

import pytest

from agent_py_agent.agent.common import json_io
from agent_py_agent.agent.common.json_io import locked_json_path
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
    _finish(index)
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
