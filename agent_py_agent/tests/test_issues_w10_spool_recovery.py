"""W10: actual-file rotation recovery and real harvest/consumer entry regressions."""
from __future__ import annotations

import json
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest
from agent.common.json_io import write_private_json_file_atomic
from agent.ingestion import harvester as hv
from agent.ingestion import spool_maintenance as sm
from agent.ingestion import watch_state as ws


class _Crash(BaseException):
    pass


class _FakeSource:
    def __init__(self):
        self.events = []
        self.calls = 0

    def feed(self, count):
        base = len(self.events)
        self.events.extend({"seq": base + i, "note": "正文" * 8192} for i in range(count))

    def handle(self, request):
        from urllib.parse import parse_qs, urlsplit

        self.calls += 1
        query = parse_qs(urlsplit(request.url).query)
        since = int(query.get("since", ["0"])[0])
        limit = int(query.get("limit", ["50"])[0])
        items = self.events[since:since + limit]
        return True, {"items": items, "next_cursor": since + len(items)}, ""


@pytest.fixture
def watch(tmp_path, monkeypatch):
    # Local fixture copied from the guarantee suite, not imported from another test module.
    monkeypatch.setattr(ws, "registry", ws.WatchRegistry())
    monkeypatch.setattr(hv, "harvesters", hv._HarvesterRegistry())
    monkeypatch.setattr(hv, "_SPOOL_ROTATE_BYTES", 64 * 1024)
    monkeypatch.setattr(sm, "SPOOL_MAINTENANCE_SLICE_BYTES", 128 * 1024)
    state = ws.new_state(tmp_path / "owner", "http://src.example/pull", {
        "full_read_per_pull": 8, "guarantee_spool_max_mb": 1,
        "guarantee_min_disk_free_mb": 0,
    })
    state.source_envelope = {"mode": "cursor", "record_boundary": "array_item",
        "record_list_key": "items", "cursor_field": "next_cursor",
        "cursor_semantics": "next_position", "valid": True,
        "request": {"method": "GET", "cursor_binding": {"location": "query", "name": "since", "initial": 0},
                    "page_size_binding": {"location": "query", "name": "limit"}}}
    state.audit_guarantee = True
    ws.persist_state(state)
    source = _FakeSource()
    _renew(state)
    return state, source


def _renew(state):
    write_private_json_file_atomic(hv._lease_path(state), hv._lease_payload(state, "w10-test"))


def _rows(path):
    return [json.loads(line) for line in path.read_bytes().splitlines()] if path.exists() else []


def _inputs(tmp_path, count=12):
    spool = tmp_path / "ws.spool.ndjson"
    spool.write_bytes(b"".join(json.dumps({"spool_seq": seq, "payload": "x" * 32}).encode() + b"\n"
                              for seq in range(1, count + 1)))
    return sm.SpoolRotationInputs(spool, tmp_path / "ws.archive.ndjson",
                                 tmp_path / "ws.spool-rotation.json", 0, count - 1, False, 0)


def _slice(inputs, **kwargs):
    return sm.run_spool_rotation_slice(inputs, lease_current=lambda: True, max_ms=1000, **kwargs)


def _finish(inputs, max_bytes=180):
    reports = []
    for _ in range(100):
        report = _slice(inputs, max_bytes=max_bytes)
        reports.append(report)
        assert report["copied_bytes"] <= max_bytes
        assert report["status"] in {"copying", "switched"}, report
        if report["status"] == "switched":
            return reports
    pytest.fail("rotation did not make bounded forward progress")


def _settle(state):
    for _ in range(100):
        records, _ = hv.read_spool_records(state, max_candidates=8, consumer="w10")
        if not records:
            assert not isinstance(hv.read_spool_cursor(state).get("inflight"), dict)
            return
        verdicts = [{"ack_id": row["ack_id"], "verdict": "clear", "score": 1, "note": "逐条已核对"}
                    for record in records for row in record["candidates"]]
        result = hv.submit_verdicts(state, consumer="w10", verdicts=verdicts)
        assert result["ok"]
    pytest.fail("consumer failed to settle the actual queue")


def test_backpressure_catches_up_without_restart_and_fetch_resumes(watch):
    state, source = watch
    source.feed(20)
    assert hv._harvest_cycle(state, source.handle)
    spool = hv.spool_path(state)
    # Grow through the real append path until the actual byte watermark is crossed.
    for _ in range(8):
        if spool.stat().st_size >= 1024 * 1024:
            break
        source.feed(4)
        assert hv._harvest_cycle(state, source.handle)
    assert spool.stat().st_size >= 1024 * 1024
    original = _rows(spool)
    calls = source.calls
    assert hv._harvest_once(state, source.handle) == "ok"
    assert state.totals["disk_backpressure_skips"] == 1
    assert source.calls == calls
    assert _rows(spool) == original
    _settle(state)
    source.feed(1)
    for _ in range(100):
        _renew(state)
        assert hv._harvest_once(state, source.handle) == "ok"
        if state.spool_generation:
            break
        assert source.calls == calls
    assert state.spool_generation == 1
    archive = hv._spool_rotation_inputs(state).archive
    combined = _rows(archive) + _rows(spool)
    assert combined == original
    assert len({row["spool_seq"] for row in combined}) == len(original)
    assert _rows(spool) == [original[-1]]
    assert state.totals["spool_maintenance_slices"] > 1
    assert hv._harvest_once(state, source.handle) == "ok"
    assert source.calls > calls
    candidate, source_name, stored = hv._find_audit_candidate(state, original[0]["spool_seq"], 0)
    assert candidate == original[0]["candidates"][0]
    assert source_name == archive.name and stored == original[0]


def test_stale_inode_reopen_deduplicates_durable_archive_tail(tmp_path):
    inputs = _inputs(tmp_path)
    original = _rows(inputs.spool)
    first = _slice(inputs, max_bytes=180)
    assert first["status"] == "copying" and _rows(inputs.archive)
    substitute = tmp_path / "replacement"
    substitute.write_bytes(inputs.spool.read_bytes())
    os.replace(substitute, inputs.spool)
    assert _slice(inputs, max_bytes=180)["reason"] == "stale_progress"
    assert not inputs.progress.exists()
    _finish(inputs)
    assert _rows(inputs.archive) + _rows(inputs.spool) == original
    assert [row["spool_seq"] for row in _rows(inputs.archive)] == list(range(1, 12))


def test_crash_after_archive_fsync_before_progress_save(tmp_path, monkeypatch):
    inputs = _inputs(tmp_path)
    original = _rows(inputs.spool)
    save = sm.write_private_json_file_atomic
    crashed = False

    def interrupt(path, payload):
        nonlocal crashed
        if payload["copied"] and not crashed:
            crashed = True
            raise _Crash()
        return save(path, payload)

    monkeypatch.setattr(sm, "write_private_json_file_atomic", interrupt)
    with pytest.raises(_Crash):
        _slice(inputs, max_bytes=180)
    assert inputs.archive.stat().st_size > 0
    assert json.loads(inputs.progress.read_text())["copied"] == 0
    _finish(inputs)
    assert _rows(inputs.archive) + _rows(inputs.spool) == original


def test_switch_recovery_is_same_generation_and_rejects_foreign_replace(tmp_path, monkeypatch):
    inputs = _inputs(tmp_path)
    replace_file = sm.os.replace
    original = _rows(inputs.spool)

    def interrupted(source, target):
        replace_file(source, target)
        if target == inputs.spool:
            raise _Crash()

    monkeypatch.setattr(sm.os, "replace", interrupted)
    with pytest.raises(_Crash):
        _finish(inputs)
    assert _rows(inputs.spool) == [original[-1]]
    report = _slice(inputs, max_bytes=180)
    assert report["status"] == "switched" and report["new_generation"] == 1
    assert _slice(replace(inputs, generation=1), max_bytes=180)["new_generation"] == 1
    assert _rows(inputs.archive) + _rows(inputs.spool) == original


@pytest.mark.parametrize("condition,reason", [("inflight", "inflight"), ("lease", "lease_lost"), ("disk", "low_disk")])
def test_rotation_defers_without_deleting_hot_rows(tmp_path, monkeypatch, condition, reason):
    inputs = _inputs(tmp_path)
    before = inputs.spool.read_bytes()
    if condition == "disk":
        monkeypatch.setattr(sm.shutil, "disk_usage", lambda path: SimpleNamespace(free=0))
    report = sm.run_spool_rotation_slice(replace(inputs, inflight=condition == "inflight"),
        lease_current=lambda: condition != "lease", max_bytes=180)
    assert report["status"] == "deferred" and report["reason"] == reason
    assert inputs.spool.read_bytes() == before
    assert not inputs.archive.exists()


def test_archive_identity_change_never_switches(tmp_path):
    inputs = _inputs(tmp_path)
    _slice(inputs, max_bytes=180)
    original = inputs.spool.read_bytes()
    other = tmp_path / "other"
    other.write_bytes(inputs.archive.read_bytes())
    os.replace(other, inputs.archive)
    report = _slice(inputs, max_bytes=180)
    assert report["status"] == "failed" and report["reason"] == "archive_identity_changed"
    assert inputs.spool.read_bytes() == original


def test_unacked_and_bad_tail_stay_hot(tmp_path):
    inputs = _inputs(tmp_path)
    original = _rows(inputs.spool)
    _finish(replace(inputs, acked_seq=3))
    assert _rows(inputs.archive) == original[:3]
    assert _rows(inputs.spool) == original[3:]


@pytest.mark.parametrize("field,value", [("copied", -1), ("archive_base_size", -1), ("skipped", 99999)])
def test_invalid_checkpoint_cannot_truncate_confirmed_archive(tmp_path, field, value):
    inputs = _inputs(tmp_path)
    _slice(inputs, max_bytes=180)
    before = inputs.archive.read_bytes()
    progress = json.loads(inputs.progress.read_text())
    progress[field] = value
    write_private_json_file_atomic(inputs.progress, progress)
    report = _slice(inputs, max_bytes=180)
    assert report["status"] == "failed"
    assert inputs.archive.read_bytes() == before


def test_inflight_and_lease_loss_at_switch_defer_then_resume(tmp_path):
    inputs = _inputs(tmp_path)
    before = inputs.spool.read_bytes()
    calls = 0

    def lease():
        nonlocal calls
        calls += 1
        return calls == 1

    report = sm.run_spool_rotation_slice(inputs, lease_current=lease, max_bytes=4096, max_ms=1000)
    assert report["status"] == "deferred" and report["reason"] == "lease_lost"
    assert inputs.spool.read_bytes() == before
    assert json.loads(inputs.progress.read_text())["cut"] > 0
    assert _slice(replace(inputs, inflight=True), max_bytes=4096)["reason"] == "inflight"
    assert inputs.spool.read_bytes() == before
    assert _finish(inputs, 4096)[-1]["status"] == "switched"
    assert inputs.archive.read_bytes() + inputs.spool.read_bytes() == before


def test_harvest_tick_finishes_replace_before_persist_exactly_once(watch, monkeypatch):
    state, source = watch
    source.feed(4)
    assert hv._harvest_cycle(state, source.handle)
    _settle(state)
    original = _rows(hv.spool_path(state))
    persist = hv.persist_state

    def interrupted(current):
        if current.spool_generation:
            raise _Crash()
        return persist(current)

    monkeypatch.setattr(hv, "persist_state", interrupted)
    with pytest.raises(_Crash):
        for _ in range(100):
            _renew(state)
            assert hv._harvest_once(state, source.handle) == "ok"
    assert state.spool_generation == 0
    assert _rows(hv.spool_path(state)) == [original[-1]]
    assert hv._spool_rotation_inputs(state).progress.exists()
    calls = source.calls
    monkeypatch.setattr(hv, "persist_state", persist)
    assert hv._harvest_once(state, source.handle) == "ok"
    assert state.spool_generation == 1 and source.calls == calls
    assert not hv._spool_rotation_inputs(state).progress.exists()
    assert _rows(hv._spool_rotation_inputs(state).archive) + _rows(hv.spool_path(state)) == original
    assert hv._harvest_once(state, source.handle) == "ok"
    assert state.spool_generation == 1
    assert ws.load_state(state.owner_home, state.watch_id).spool_generation == 1


def test_archive_readback_mismatch_leaves_hot_spool_untouched(tmp_path, monkeypatch):
    inputs = _inputs(tmp_path)
    before = inputs.spool.read_bytes()
    fsync = sm.os.fsync
    changed = False

    def changed_after_sync(fd):
        nonlocal changed
        fsync(fd)
        if not changed and inputs.archive.exists():
            current, archive = os.fstat(fd), inputs.archive.stat()
            if current.st_ino == archive.st_ino and current.st_size:
                os.pwrite(fd, b"!", 0)
                changed = True

    monkeypatch.setattr(sm.os, "fsync", changed_after_sync)
    report = _slice(inputs, max_bytes=180)
    assert changed
    assert report["status"] == "failed" and report["reason"] == "archive_verify_failed"
    assert inputs.spool.read_bytes() == before
    _finish(inputs)
    assert inputs.archive.read_bytes() + inputs.spool.read_bytes() == before


def test_oversized_row_has_explicit_budget_deferral(tmp_path):
    inputs = _inputs(tmp_path)
    inputs.spool.write_text(json.dumps({"spool_seq": 1, "data": "x" * 1000}) + "\n" + '{"spool_seq":2}\n')
    before = inputs.spool.read_bytes()
    report = _slice(inputs, max_bytes=180)
    assert report["status"] == "deferred" and report["reason"] == "row_exceeds_slice"
    assert inputs.spool.read_bytes() == before


def test_bad_json_row_and_following_rows_stay_hot(tmp_path):
    inputs = _inputs(tmp_path)
    lines = inputs.spool.read_bytes().splitlines(keepends=True)
    lines[4] = b'{"spool_seq": invalid}\n'
    before = b"".join(lines)
    inputs.spool.write_bytes(before)
    _finish(inputs)
    assert inputs.archive.read_bytes() == b"".join(lines[:4])
    assert inputs.spool.read_bytes() == b"".join(lines[4:])


def test_rotation_switch_serializes_reader_claim_with_generation(watch, monkeypatch):
    import threading

    state, source = watch
    source.feed(4)
    assert hv._harvest_cycle(state, source.handle)
    batch, _ = hv.read_spool_records(state, max_candidates=1, consumer="first")
    assert batch
    result = hv.submit_verdicts(state, consumer="first", verdicts=[
        {"ack_id": row["ack_id"], "verdict": "clear", "score": 1, "note": "已核对"}
        for record in batch for row in record["candidates"]])
    assert result["ok"]
    assert not isinstance(hv.read_spool_cursor(state).get("inflight"), dict)
    readers, delivered = [], []
    stage = sm._stage_tail

    def claim_during_switch(inputs, progress, max_bytes):
        complete = stage(inputs, progress, max_bytes)
        if complete and not readers:
            def read():
                delivered.append(hv.read_spool_records(state, max_candidates=1, consumer="concurrent"))

            reader = threading.Thread(target=read)
            readers.append(reader)
            reader.start()
            # Unfenced code lets the claim finish against the old generation here.
            reader.join(0.2)
        return complete

    monkeypatch.setattr(sm, "_stage_tail", claim_during_switch)
    try:
        for _ in range(100):
            with state.lock:
                report = hv._run_guarantee_rotation_slice(state)
            if report["status"] == "switched":
                break
        assert state.spool_generation == 1
    finally:
        for reader in readers:
            reader.join(5)
    assert readers and all(not reader.is_alive() for reader in readers)
    assert delivered and delivered[0][0]
    inflight = hv.read_spool_cursor(state)["inflight"]
    assert inflight["generation"] == state.spool_generation


def test_cursor_lock_busy_defers_without_waiting_or_switching(watch):
    import threading

    from agent.common.json_io import locked_json_path

    state, source = watch
    source.feed(4)
    assert hv._harvest_cycle(state, source.handle)
    before = hv.spool_path(state).read_bytes()
    held, release = threading.Event(), threading.Event()

    def holder():
        with locked_json_path(hv._read_sidecar_path(state)):
            held.set()
            assert release.wait(10)

    reader = threading.Thread(target=holder)
    reader.start()
    assert held.wait(5)
    try:
        with state.lock:
            report = hv._run_guarantee_rotation_slice(state)
        assert report["status"] == "deferred" and report["reason"] == "cursor_busy"
        assert hv.spool_path(state).read_bytes() == before
        assert state.spool_generation == 0 and not release.is_set()
    finally:
        release.set()
        reader.join(5)
    assert not reader.is_alive()


def test_archive_reader_resolves_rows_as_archive_grows_between_slices(watch):
    state, source = watch
    source.feed(8)
    assert hv._harvest_cycle(state, source.handle)
    _settle(state)
    original = _rows(hv.spool_path(state))
    with state.lock:
        first = hv._run_guarantee_rotation_slice(state)
    assert first["status"] == "copying"
    archive = hv._spool_rotation_inputs(state).archive
    initial = archive.stat()
    first_rows = _rows(archive)
    candidate, source_name, stored = hv._find_audit_candidate(state, first_rows[0]["spool_seq"], 0)
    assert candidate == first_rows[0]["candidates"][0] and stored == first_rows[0]
    assert source_name == archive.name
    with state.lock:
        second = hv._run_guarantee_rotation_slice(state)
    assert second["status"] == "copying"
    assert archive.stat().st_ino == initial.st_ino and archive.stat().st_size > initial.st_size
    appended = _rows(archive)[len(first_rows)]
    candidate, source_name, stored = hv._find_audit_candidate(state, appended["spool_seq"], 0)
    assert candidate == appended["candidates"][0] and stored == appended
    assert source_name == archive.name
    assert _rows(archive) + _rows(hv.spool_path(state))[len(_rows(archive)):] == original


def test_default_byte_ceiling_explicitly_preserves_oversized_acked_row(tmp_path):
    from hashlib import sha256

    inputs = _inputs(tmp_path, count=2)
    large = json.dumps({"spool_seq": 1, "payload": "x" * (sm.SPOOL_MAINTENANCE_SLICE_BYTES + 1)}).encode() + b"\n"
    inputs.spool.write_bytes(large + b'{"spool_seq":2}\n')
    before = sha256(inputs.spool.read_bytes()).hexdigest()
    # Complete-line archival cannot cross the frozen per-slice byte ceiling.
    # Explicitly retain the source rather than allocate an unbounded line or claim progress.
    for _ in range(2):
        report = sm.run_spool_rotation_slice(inputs, lease_current=lambda: True, max_ms=1000)
        assert report["status"] == "deferred" and report["reason"] == "row_exceeds_slice"
        assert report["copied_bytes"] == 0
        assert sha256(inputs.spool.read_bytes()).hexdigest() == before
        assert inputs.archive.read_bytes() == b""


# LLM: 用真实归档和replace制造世代发表失败，不代写spool或读游标；返回时原游标仍指向旧文件偏移。
# 函数用途: 准备切盘已完成、检查点仍在switching的真实恢复窗口。
def _rotation_with_unpublished_generation(watch, monkeypatch):
    state, source = watch
    source.feed(4)
    assert hv._harvest_cycle(state, source.handle)
    path = hv.spool_path(state)
    original, inode = _rows(path), path.stat().st_ino
    batch, _ = hv.read_spool_records(state, max_candidates=1, consumer="w10")
    assert batch
    result = hv.submit_verdicts(state, consumer="w10", verdicts=[
        {"ack_id": row["ack_id"], "verdict": "clear", "score": 1, "note": "已核对"}
        for record in batch for row in record["candidates"]])
    assert result["ok"]
    cursor = hv.read_spool_cursor(state)
    assert cursor["offset"] > 0 and not isinstance(cursor.get("inflight"), dict)
    tail = [row for row in original if row["spool_seq"] > cursor["read_seq"]]
    assert tail
    previous, persist = state.spool_generation, hv.persist_state
    failure = OSError("injected rotation generation publication failure")

    def reject_generation(current):
        if current.spool_generation != previous:
            raise failure
        return persist(current)

    with monkeypatch.context() as patch:
        patch.setattr(hv, "persist_state", reject_generation)
        with pytest.raises(OSError) as caught:
            for _ in range(100):
                _renew(state)
                with state.lock:
                    hv._run_guarantee_rotation_slice(state)
            pytest.fail("real rotation never reached generation publication")
    assert caught.value is failure
    progress = hv._spool_rotation_inputs(state).progress
    assert json.loads(progress.read_text())["phase"] == "switching"
    assert path.stat().st_ino != inode and _rows(path) == tail
    assert path.stat().st_size < 1024 * 1024
    assert state.spool_generation == previous
    assert hv.read_spool_cursor(state) == cursor
    return original, tail, cursor


# LLM: 原轮转实现使用POSIX文件能力；直接枚举本测试进程的FD，不以业务回执冒充释放证明。
# 函数用途: 在失败、读取和恢复边界核对实际描述符数量。
def _open_fd_count():
    directory = "/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"
    return len(os.listdir(directory))


# LLM: 真实reader在replace与世代发表之间逐批签收；恢复前不运行生产者，保持原租约与签收门。
# 函数用途: 防止旧字节偏移漏读热盘尾部，并核对失败及恢复没有FD泄漏。
def test_failed_generation_persist_never_skips_unacknowledged_rows(watch, monkeypatch):
    before = _open_fd_count()
    original, tail, cursor = _rotation_with_unpublished_generation(watch, monkeypatch)
    assert _open_fd_count() - before == 0
    state, source = watch
    inputs = hv._spool_rotation_inputs(state)
    checkpoint, calls = inputs.progress.read_bytes(), source.calls
    delivered, previous = [], cursor["read_seq"]
    for expected in tail:
        records, _ = hv.read_spool_records(state, max_candidates=1, consumer="w10")
        assert [row["spool_seq"] for row in records] == [expected["spool_seq"]]
        assert expected["spool_seq"] == previous + 1
        inflight = hv.read_spool_cursor(state)["inflight"]
        assert inflight["from_offset"] == 0 and inflight["from_seq"] == previous
        assert inflight["generation"] == state.spool_generation == cursor["generation"]
        assert hv._spool_rotation_inputs(state).acked_seq == previous
        assert inputs.progress.read_bytes() == checkpoint
        assert _open_fd_count() - before == 0
        assert hv.submit_verdicts(state, consumer="w10", verdicts=[
            {"ack_id": row["ack_id"], "verdict": "clear", "score": 1, "note": "已核对"}
            for record in records for row in record["candidates"]])["ok"]
        previous = expected["spool_seq"]
        delivered.append(previous)
        settled = hv.read_spool_cursor(state)
        assert settled["read_seq"] == previous and not isinstance(settled.get("inflight"), dict)
        assert hv._spool_rotation_inputs(state).acked_seq == previous
    assert hv.read_spool_records(state, max_candidates=1, consumer="w10")[0] == []
    assert hv._harvest_once(state, source.handle) == "ok"
    assert _open_fd_count() - before == 0
    assert state.spool_generation == cursor["generation"] + 1
    assert not inputs.progress.exists() and source.calls == calls
    assert _rows(inputs.archive) + _rows(inputs.spool) == original
    assert [row["spool_seq"] for row in _rows(inputs.archive)] + delivered == [row["spool_seq"] for row in original]
    source.feed(1)
    assert hv._harvest_once(state, source.handle) == "ok"
    assert source.calls > calls and state.spool_seq > tail[-1]["spool_seq"]


# LLM: 损坏或读失败只使偏移失效，不清除或修复检查点；序号仍是去重和签收的唯一依据。
# 函数用途: 检查点不能判读时仍不遗漏已经换盘的未读记录。
@pytest.mark.parametrize("failure", ["json", "non_object", "unicode", "io", "stat"])
def test_reader_rescans_when_rotation_checkpoint_is_unreadable(watch, monkeypatch, failure):
    _original, tail, cursor = _rotation_with_unpublished_generation(watch, monkeypatch)
    state, _source = watch
    progress = hv._spool_rotation_inputs(state).progress
    if failure == "stat":
        exists = type(progress).exists

        def cannot_stat(path):
            if path == progress:
                raise PermissionError("injected checkpoint stat failure")
            return exists(path)

        monkeypatch.setattr(type(progress), "exists", cannot_stat)
    elif failure == "io":
        read_text = type(progress).read_text

        def unreadable(path, *args, **kwargs):
            if path == progress:
                raise PermissionError("injected checkpoint read failure")
            return read_text(path, *args, **kwargs)

        monkeypatch.setattr(type(progress), "read_text", unreadable)
    else:
        progress.write_bytes({"json": b"{", "non_object": b"[]", "unicode": b"\xff"}[failure])
    before = progress.read_bytes()
    records, _ = hv.read_spool_records(state, max_candidates=8, consumer="w10")
    assert [row["spool_seq"] for row in records] == [row["spool_seq"] for row in tail]
    updated = hv.read_spool_cursor(state)
    assert updated["inflight"]["from_offset"] == 0
    assert updated["read_seq"] == tail[-1]["spool_seq"]
    assert updated["inflight"]["from_seq"] == cursor["read_seq"]
    assert hv._spool_rotation_inputs(state).acked_seq == cursor["read_seq"]
    replayed, _ = hv.read_spool_records(state, max_candidates=8, consumer="takeover")
    assert [row["spool_seq"] for row in replayed] == [row["spool_seq"] for row in tail]
    assert hv._spool_rotation_inputs(state).acked_seq == cursor["read_seq"]
    assert progress.read_bytes() == before


# LLM: 没有待发表切盘证据时保留原偏移，不把正常增量读取变成从头全扫。
# 函数用途: 冻结无检查点和copying阶段的原读取行为及序号去重。
@pytest.mark.parametrize("phase", [None, "copying"])
def test_reader_preserves_offset_without_pending_switch(watch, phase):
    state, source = watch
    source.feed(4)
    assert hv._harvest_cycle(state, source.handle)
    original = _rows(hv.spool_path(state))
    records, _ = hv.read_spool_records(state, max_candidates=1, consumer="w10")
    assert hv.submit_verdicts(state, consumer="w10", verdicts=[
        {"ack_id": row["ack_id"], "verdict": "clear", "score": 1, "note": "已核对"}
        for record in records for row in record["candidates"]])["ok"]
    cursor = hv.read_spool_cursor(state)
    progress = hv._spool_rotation_inputs(state).progress
    if phase is not None:
        write_private_json_file_atomic(progress, {"phase": phase})
    records, _end, _taken, start = hv._scan_spool(state, cursor, max_candidates=8)
    assert start == cursor["offset"] > 0
    assert records == [row for row in original if row["spool_seq"] > cursor["read_seq"]]


# LLM: 仅替换维护回执来穷举调度分支；磁盘水位、采集入口、HTTP回调和spool文件保持真实。
# 函数用途: 仅成功切盘暂停一拍，复制及各种延期都不能替代原磁盘停采门。
@pytest.mark.parametrize("backpressured", [False, True])
@pytest.mark.parametrize("status,reason", [
    ("copying", ""), ("deferred", "inflight"), ("deferred", "lease_lost"),
    ("deferred", "cursor_busy"), ("deferred", "empty_budget"),
    ("deferred", "stale_progress"), ("not_ready", "nothing_acked"),
    ("idle", "missing_file"), ("failed", "rotation_io_error"), ("switched", ""),
])
def test_pending_rotation_preserves_fetch_gate(watch, monkeypatch, backpressured, status, reason):
    state, source = watch
    source.feed(40 if backpressured else 4)
    assert hv._harvest_cycle(state, source.handle)
    if backpressured:
        for _ in range(8):
            if hv.spool_path(state).stat().st_size >= 1024 * 1024:
                break
            assert hv._harvest_cycle(state, source.handle)
    assert hv._disk_backpressured(state) is backpressured
    progress = hv._spool_rotation_inputs(state).progress
    write_private_json_file_atomic(progress, {"phase": "copying"})
    slices, calls = [], source.calls

    def rotation(current):
        assert current is state
        slices.append(1)
        return sm._report(status, reason)

    monkeypatch.setattr(hv, "_run_guarantee_rotation_slice", rotation)
    assert hv._harvest_once(state, source.handle) == "ok"
    assert slices
    if backpressured or status == "switched":
        assert source.calls == calls
    else:
        assert source.calls > calls
    if backpressured:
        assert len(slices) == 1
        assert state.totals["disk_backpressure_skips"] == 1


# LLM: 抛出的原维护异常不是failed回执，不可吞掉后继续采集。
# 函数用途: 保留轮转异常传播和本拍零来源请求的合同。
def test_pending_rotation_preserves_original_exception(watch, monkeypatch):
    state, source = watch
    source.feed(4)
    assert hv._harvest_cycle(state, source.handle)
    write_private_json_file_atomic(hv._spool_rotation_inputs(state).progress, {"phase": "copying"})
    error, calls = OSError("injected slice failure"), source.calls

    def fail(_state):
        raise error

    monkeypatch.setattr(hv, "_run_guarantee_rotation_slice", fail)
    with pytest.raises(OSError) as caught:
        hv._harvest_once(state, source.handle)
    assert caught.value is error and source.calls == calls


# LLM: 维护、真实追加与最终replace交错，不用状态回执替身；复制中的新行仍须完整保留在热盘尾部。
# 函数用途: 验证允许继续采集后，原分片轮转没有丢行或重复归档。
def test_rotation_in_progress_keeps_fetching(watch, monkeypatch):
    state, source = watch
    monkeypatch.setattr(sm, "SPOOL_MAINTENANCE_SLICE_BYTES", 64 * 1024)
    source.feed(4)
    assert hv._harvest_cycle(state, source.handle)
    original = _rows(hv.spool_path(state))
    batch, _ = hv.read_spool_records(state, max_candidates=1, consumer="w10")
    assert hv.submit_verdicts(state, consumer="w10", verdicts=[
        {"ack_id": row["ack_id"], "verdict": "clear", "score": 1, "note": "已核对"}
        for record in batch for row in record["candidates"]])["ok"]
    with state.lock:
        first = hv._run_guarantee_rotation_slice(state)
    assert first["status"] == "copying" and state.spool_generation == 0
    held, _ = hv.read_spool_records(state, max_candidates=1, consumer="w10")
    assert held
    inflight = hv.read_spool_cursor(state)["inflight"]
    for _ in range(3):
        source.feed(1)
        calls, sequence = source.calls, state.spool_seq
        assert hv._harvest_once(state, source.handle) == "ok"
        assert source.calls > calls and state.spool_seq == sequence + 1
        assert hv.read_spool_cursor(state)["inflight"] == inflight
        assert state.spool_generation == 0 and not hv._disk_backpressured(state)
    expected = _rows(hv.spool_path(state))
    assert expected[:len(original)] == original
    assert hv.submit_verdicts(state, consumer="w10", verdicts=[
        {"ack_id": row["ack_id"], "verdict": "clear", "score": 1, "note": "已核对"}
        for record in held for row in record["candidates"]])["ok"]
    for _ in range(100):
        _renew(state)
        assert hv._harvest_once(state, source.handle) == "ok"
        if state.spool_generation == 1:
            break
    else:
        pytest.fail("rotation with concurrent append did not finish")
    inputs = hv._spool_rotation_inputs(state)
    assert state.spool_generation == 1 and not inputs.progress.exists()
    assert _rows(inputs.archive) + _rows(inputs.spool) == expected
