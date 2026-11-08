# LLM: RES D-5 回归：崩溃留下的无 LF 残行不能与下一次锁内 JSONL 追加合成坏行；完整对象补 LF，
#   半行私有留档后截回最后 LF；按文件大小预记偏移的调用方（调度历史、用量账）先封再量。
#   插话队列保留 v14-R1 严格修复合同（残行报错留给运维），通用封口不得抢在其严格读之前改写。
# 模块用途: 钉住通用残尾封口的三种结局、符号链接不跟随、偏移型调用方崩溃后可自愈，及插话队列合同不变。

from __future__ import annotations

import errno
import json
import os
import stat
import time
from contextlib import nullcontext
from pathlib import Path

import pytest

from agent_py_agent.agent.collaboration.store import CollaborationEvidenceStore
from agent_py_agent.agent.common import json_io
from agent_py_agent.agent.common.json_io import append_private_jsonl_records_unlocked
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.agent.scheduler.history_index import SchedulerHistoryIndex


def _torn(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f".{path.name}.torn-*"))


# 函数用途: 半行残尾被私有留档并截掉，新记录独占一行，旧完整行字节不变。
def test_partial_tail_is_quarantined_before_append(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"a": 1}\n{"b": ')
    append_private_jsonl_records_unlocked(path, [{"c": 3}])
    assert path.read_bytes() == b'{"a": 1}\n{"c": 3}\n'
    [sidecar] = _torn(path)
    assert sidecar.read_bytes() == b'{"b": '
    assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600


# 函数用途: 残尾本身是完整对象时只补换行，不丢这条已写完的记录。
def test_complete_object_tail_only_gets_newline(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"a": 1}\n{"b": 2}')
    append_private_jsonl_records_unlocked(path, [{"c": 3}])
    assert [json.loads(line) for line in path.read_text().splitlines()] == [{"a": 1}, {"b": 2}, {"c": 3}]
    assert _torn(path) == []


# 函数用途: 正常、空、缺失文件与符号链接都不被封口改动。
def test_clean_missing_and_symlink_paths_are_untouched(tmp_path: Path) -> None:
    seal = json_io.seal_private_jsonl_tail_unlocked
    clean = tmp_path / "clean.jsonl"
    clean.write_bytes(b'{"a": 1}\n')
    empty = tmp_path / "empty.jsonl"
    empty.write_bytes(b"")
    target = tmp_path / "target.jsonl"
    target.write_bytes(b'{"a": 1}\n{"b": ')
    link = tmp_path / "link.jsonl"
    link.symlink_to(target)
    assert [seal(clean), seal(empty), seal(tmp_path / "missing.jsonl"), seal(link)] == ["", "", "", ""]
    assert clean.read_bytes() == b'{"a": 1}\n' and empty.read_bytes() == b""
    assert target.read_bytes() == b'{"a": 1}\n{"b": ' and _torn(target) == [] and _torn(link) == []
    append_private_jsonl_records_unlocked(tmp_path / "new.jsonl", [{"a": 1}])
    assert (tmp_path / "new.jsonl").read_bytes() == b'{"a": 1}\n'


def _parse_run(raw, *, terminal=True):
    if not isinstance(raw, dict) or not raw.get("run_id"):
        return None, "SCHEDULER_HISTORY_ROW_INVALID"
    return raw, None


# 函数用途: 调度历史崩溃残尾后再追加，回填完成后不留永久坏行错误且新 run 可查。
def test_scheduler_history_heals_torn_tail(tmp_path: Path) -> None:
    source = tmp_path / "history.jsonl"
    index = SchedulerHistoryIndex(source, {"owner": "o"}, _parse_run)
    index.append([{"run_id": "r1", "job_id": "j", "ended_at": 1.0}])
    with source.open("ab") as handle:
        handle.write(b'{"run_id": "r2", "job_')
    index.append([{"run_id": "r3", "job_id": "j", "ended_at": 3.0}])
    for _ in range(8):
        if index.backfill()["complete"]:
            break
    rows, errors = index.read(job_id="j")
    assert errors == []
    assert [row["run_id"] for row in rows] == ["r3", "r1"]


# 函数用途: 插话队列残尾仍按 v14-R1 合同报修复错误，残行字节原样保留且不产生封口旁车。
def test_guidance_queue_keeps_strict_repair_contract(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    store.guidance.append_once(
        {"target_type": "request", "target_id": "req", "message": "one", "metadata": {}},
        dedupe_key="t/one",
    )
    queue = store.storage.guidance_path("request", "req")
    with queue.open("ab") as handle:
        handle.write(b'{"guidance_id": "torn')
    before = queue.read_bytes()
    with pytest.raises(DataCorruptionError):
        store.guidance.append_once(
            {"target_type": "request", "target_id": "req", "message": "two", "metadata": {}},
            dedupe_key="t/two",
        )
    assert queue.read_bytes() == before and not _torn(queue)


@pytest.fixture(params=[False, True], ids=["public", "unlocked"])
def append_records(request):
    def append(path, records, **options):
        lock = json_io.locked_json_path(path) if request.param else nullcontext()
        writer = (json_io.append_private_jsonl_records_unlocked if request.param
                  else json_io.append_private_jsonl_records)
        with lock:
            writer(path, records, **options)
    return append


def _fd_count() -> int:
    directory = "/proc/self/fd" if Path("/proc/self/fd").is_dir() else "/dev/fd"
    with os.scandir(directory) as entries:
        return sum(1 for _ in entries)


@pytest.mark.parametrize("tail", [b'{"torn": ', b'{"tail": 2}'], ids=["partial", "complete"])
@pytest.mark.parametrize("sort_keys", [False, True])
def test_both_append_routes_seal_tail(tmp_path, append_records, tail, sort_keys):
    path = tmp_path / "rows.jsonl"
    prefix = b'{"keep": 1}\n'
    path.write_bytes(prefix + tail)
    record = {"z": "新记录", "a": 3}
    before_fd = _fd_count()
    append_records(path, [record], sort_keys=sort_keys)
    assert _fd_count() - before_fd == 0
    suffix = (json.dumps(record, ensure_ascii=False, sort_keys=sort_keys) + "\n").encode()
    completed = tail == b'{"tail": 2}'
    assert path.read_bytes() == prefix + (tail + b"\n" if completed else b"") + suffix
    report = json_io.read_jsonl_objects_report(path)
    assert report.load_errors == []
    assert report.records == [{"keep": 1}, *([{"tail": 2}] if completed else []), record]
    sidecars = _torn(path)
    if completed:
        assert sidecars == []
    else:
        assert len(sidecars) == 1
        assert sidecars[0].read_bytes() == tail
        assert stat.S_IMODE(sidecars[0].stat().st_mode) == 0o600


@pytest.mark.parametrize("error_code", [errno.EIO, errno.EACCES, errno.EMFILE, errno.ENFILE])
def test_source_open_error_is_original_and_normal_retry_heals(
    tmp_path, monkeypatch, append_records, error_code,
):
    path = tmp_path / "rows.jsonl"
    original_bytes = b'{"keep": 1}\n{"torn": '
    path.write_bytes(original_bytes)
    error = OSError(error_code, "injected source-open failure")
    original_open = os.open
    failed = []

    def fail_once(open_path, flags, *args, **kwargs):
        if (os.fspath(open_path) == os.fspath(path)
                and flags & os.O_ACCMODE == os.O_RDWR and not failed):
            failed.append(True)
            raise error
        return original_open(open_path, flags, *args, **kwargs)

    monkeypatch.setattr(json_io.os, "open", fail_once)
    before_fd = _fd_count()
    with pytest.raises(OSError) as caught:
        append_records(path, [{"new": 3}])
    assert caught.value is error
    assert caught.value.errno == error_code and caught.value.__cause__ is None
    assert failed == [True]
    assert _fd_count() - before_fd == 0
    assert path.read_bytes() == original_bytes and _torn(path) == []
    append_records(path, [{"new": 3}])
    assert _fd_count() - before_fd == 0
    report = json_io.read_jsonl_objects_report(path)
    assert report.load_errors == [] and report.records == [{"keep": 1}, {"new": 3}]
    assert _torn(path)[0].read_bytes() == b'{"torn": '


@pytest.mark.parametrize("error_code", [errno.ELOOP, errno.EINVAL])
@pytest.mark.parametrize("symlink", [False, True], ids=["regular", "symlink"])
def test_nofollow_errors_only_skip_actual_symlinks(tmp_path, monkeypatch, error_code, symlink):
    target = tmp_path / "target.jsonl"
    before = b'{"keep": 1}\n{"torn": '
    target.write_bytes(before)
    path = tmp_path / "link.jsonl" if symlink else target
    if symlink:
        path.symlink_to(target)
    error = OSError(error_code, "injected nofollow rejection")
    original_open = os.open

    def refuse_source(open_path, flags, *args, **kwargs):
        if os.fspath(open_path) == os.fspath(path) and flags & os.O_ACCMODE == os.O_RDWR:
            raise error
        return original_open(open_path, flags, *args, **kwargs)

    monkeypatch.setattr(json_io.os, "open", refuse_source)
    if symlink:
        assert json_io.seal_private_jsonl_tail_unlocked(path) == ""
    else:
        with pytest.raises(OSError) as caught:
            json_io.seal_private_jsonl_tail_unlocked(path)
        assert caught.value is error and caught.value.__cause__ is None
    assert target.read_bytes() == before and not _torn(path) and not _torn(target)


def test_seal_error_after_open_closes_real_descriptors(tmp_path, monkeypatch, append_records):
    path = tmp_path / "rows.jsonl"
    before = b'{"keep": 1}\n{"torn": '
    path.write_bytes(before)
    error = OSError(errno.EIO, "injected sidecar fsync failure")
    synced = []

    def refuse_sync(descriptor):
        assert stat.S_ISREG(os.fstat(descriptor).st_mode)
        synced.append(descriptor)
        raise error

    monkeypatch.setattr(json_io.os, "fsync", refuse_sync)
    before_fd = _fd_count()
    with pytest.raises(OSError) as caught:
        append_records(path, [{"new": 3}])
    assert caught.value is error and caught.value.__cause__ is None
    assert len(synced) == 1
    assert _fd_count() - before_fd == 0
    assert path.read_bytes() == before
    for descriptor in synced:
        with pytest.raises(OSError) as closed:
            os.fstat(descriptor)
        assert closed.value.errno == errno.EBADF


@pytest.mark.parametrize("writer", [json_io.append_private_jsonl_records, append_private_jsonl_records_unlocked])
def test_empty_records_neither_create_nor_seal(tmp_path, writer):
    missing = tmp_path / "not-created" / "empty.jsonl"
    writer(missing, [], deadline=0.0)
    assert not missing.parent.exists()
    existing = tmp_path / "rows.jsonl"
    before = b'{"torn": '
    existing.write_bytes(before)
    writer(existing, [], deadline=0.0)
    assert existing.read_bytes() == before and not _torn(existing)
    assert not existing.with_name(existing.name + ".lock").exists()


def test_expired_deadline_precedes_tail_repair(tmp_path, append_records):
    path = tmp_path / "rows.jsonl"
    before = b'{"torn": '
    path.write_bytes(before)
    before_fd = _fd_count()
    with pytest.raises(BlockingIOError):
        append_records(path, [{"new": 3}], deadline=time.monotonic() - 1)
    assert _fd_count() - before_fd == 0
    assert path.read_bytes() == before and not _torn(path)


def test_deadline_expiring_during_encoding_does_not_seal(tmp_path, monkeypatch, append_records):
    path = tmp_path / "rows.jsonl"
    before = b'{"torn": '
    path.write_bytes(before)
    deadline = time.monotonic() + 60
    original_dumps = json.dumps

    def encode_then_expire(*args, **kwargs):
        content = original_dumps(*args, **kwargs)
        monkeypatch.setattr(json_io.time, "monotonic", lambda: deadline + 1)
        return content

    monkeypatch.setattr(json_io.json, "dumps", encode_then_expire)
    before_fd = _fd_count()
    with pytest.raises(BlockingIOError):
        append_records(path, [{"new": 3}], deadline=deadline)
    assert _fd_count() - before_fd == 0
    assert path.read_bytes() == before and not _torn(path)


def test_public_append_preserves_nonblocking_lock_contention(tmp_path):
    path = tmp_path / "rows.jsonl"
    before = b'{"torn": '
    path.write_bytes(before)
    before_fd = _fd_count()
    with json_io.locked_json_path(path):
        with pytest.raises(BlockingIOError):
            json_io.append_private_jsonl_records(path, [{"new": 3}], blocking=False)
        assert path.read_bytes() == before and not _torn(path)
    assert _fd_count() - before_fd == 0
    json_io.append_private_jsonl_records(path, [{"new": 3}], blocking=False)
    assert json_io.read_jsonl_objects_report(path).records == [{"new": 3}]


def test_public_append_uses_one_original_lock(tmp_path, monkeypatch):
    path = tmp_path / "rows.jsonl"
    original_lock = json_io._locked_json_path
    calls = []

    def observe_lock(lock_path, **options):
        calls.append((lock_path, options))
        return original_lock(lock_path, **options)

    monkeypatch.setattr(json_io, "_locked_json_path", observe_lock)
    json_io.append_private_jsonl_records(path, [{"new": 3}], blocking=False)
    assert calls == [(path, {"blocking": False})]


def test_public_append_preserves_private_directory_file_and_lock_modes(tmp_path):
    path = tmp_path / "new-parent" / "rows.jsonl"
    json_io.append_private_jsonl_records(path, [{"one": 1}])
    lock = path.with_name(path.name + ".lock")
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == stat.S_IMODE(lock.stat().st_mode) == 0o600
    path.chmod(0o666)
    lock.chmod(0o666)
    with path.open("ab") as handle:
        handle.write(b'{"torn": ')
    json_io.append_private_jsonl_records(path, [{"two": 2}])
    assert stat.S_IMODE(path.stat().st_mode) == stat.S_IMODE(lock.stat().st_mode) == 0o600
    assert stat.S_IMODE(_torn(path)[0].stat().st_mode) == 0o600


def test_public_append_expired_deadline_does_not_prepare_directory(tmp_path):
    path = tmp_path / "not-created" / "rows.jsonl"
    with pytest.raises(BlockingIOError):
        json_io.append_private_jsonl_records(path, [{"new": 3}], deadline=0.0)
    assert not path.parent.exists()


def test_arbitrary_private_text_append_does_not_seal_jsonl(tmp_path):
    path = tmp_path / "text.log"
    path.write_bytes(b'{"not_json": ')
    json_io.append_private_text(path, "literal suffix")
    assert path.read_bytes() == b'{"not_json": literal suffix'
    assert not _torn(path)


def test_real_collaboration_participant_roundtrip_after_torn_tail(tmp_path):
    store = CollaborationEvidenceStore(tmp_path / "collaboration")
    first = store.add_participant({"case_id": "case-r1", "agent_id": "agent-1", "role": "reader"})
    path = store.participants_dir / "case-r1.jsonl"
    prefix = path.read_bytes()
    tail = b'{"case_id": "case-r1", "agent_id": '
    with path.open("ab") as handle:
        handle.write(tail)
    added = store.add_participant({"case_id": "case-r1", "agent_id": "agent-2", "role": "writer"})
    rows, errors = store.case_participants_report("case-r1")
    assert errors == []
    assert [row.to_dict() for row in rows] == [first.to_dict(), added.to_dict()]
    assert path.read_bytes().startswith(prefix)
    assert _torn(path)[0].read_bytes() == tail
