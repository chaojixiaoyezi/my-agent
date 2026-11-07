"""唤醒队列只清理未持有的孤儿锁，热扫描成本不随处理历史累积。"""

from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_py_agent.agent.common.json_io import locked_json_path
from agent_py_agent.agent.conversation import ConversationStore, store_index, store_wakes
from agent_py_agent.agent.conversation.models import WakeSignal
from agent_py_agent.agent.gateway_parts.io import read_json_file

posix_locks = pytest.mark.skipif(os.name == "nt", reason="即时锁清理仅使用 POSIX flock")


def _new_wake(store: ConversationStore, kind: str = "normal") -> WakeSignal:
    thread = store.threads.get_or_create({
        "canonical_user_id": "user-1", "channel": "internal",
        "channel_conversation_id": "thread-1", "channel_user_id": "user-1",
    })
    return store.wakes.raise_signal({
        "thread_id": thread.thread_id, "urgency": kind, "reason": "agent_event",
    })


@posix_locks
def test_first_pending_report_discards_history_before_bounded_scans(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversations")
    directory = store.storage.wake_queue_dir / "normal"
    for number in range(2000):
        (directory / f"old-{number}.json.lock").touch()
    for number in range(6):
        signal = WakeSignal(f"wake-{number}", "thread-1", urgency="normal", created_at=number + 1)
        store.storage.wake_signal_path(signal).write_text(json.dumps(signal.to_dict()), encoding="utf-8")

    scans = []
    sweeps = []
    original_scan = store_index._scan_dir_match_names
    def measured_scan(path, pattern, on_entry=None):
        if on_entry is not None:
            sweeps.append(path)
        if path != directory:
            return original_scan(path, pattern, on_entry)
        count = 0

        def measured_entry(entry):
            nonlocal count
            count += 1
            if on_entry is not None:
                on_entry(entry)

        names = original_scan(path, pattern, measured_entry)
        scans.append(count)
        return names

    monkeypatch.setattr(store_index, "_scan_dir_match_names", measured_scan)
    held_path = directory / "held.json"
    with locked_json_path(held_path):
        for _ in range(3):
            signals, errors = store.wakes.pending_report(limit=0)
            assert errors == []
            assert [signal.wake_signal_id for signal in signals] == [f"wake-{number}" for number in range(6)]
            assert {path.name for path in directory.iterdir()} == {
                *(f"wake-{number}.json" for number in range(6)), "held.json.lock",
            }
    # 首轮顺带回收 2000 个旧锁；目录变代必须再核对一次，后续热轮只枚举七个活跃入口。
    assert scans == [2007, 7, 7, 7]
    assert sweeps == [store.storage.wake_queue_dir / "urgent", directory]


@posix_locks
@pytest.mark.parametrize("kind", ["urgent", "normal"])
def test_mark_handled_removes_pending_record_and_lock(tmp_path, kind):
    store = ConversationStore(tmp_path / "conversations")
    signal = _new_wake(store, kind)
    path = store.storage.wake_signal_path(signal)
    lock_path = path.with_name(path.name + ".lock")
    assert path.exists() and lock_path.exists()

    handled = store.wakes.mark_handled(signal.wake_signal_id, now=30.0)

    assert handled is not None and handled.status == "handled"
    assert not path.exists() and not lock_path.exists()
    assert read_json_file(store.storage.wake_handled_dir / path.name) == handled.to_dict()
    assert store.wakes.pending_one(signal.wake_signal_id) is None
    assert store.wakes.mark_handled(signal.wake_signal_id) is None


@posix_locks
def test_mark_handled_preserves_held_lock_until_later_sweep(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversations")
    signal = _new_wake(store)
    path = store.storage.wake_signal_path(signal)
    lock_path = path.with_name(path.name + ".lock")
    acquire = threading.Event()
    acquired = threading.Event()
    recheck = threading.Event()
    rechecked = threading.Event()
    release = threading.Event()
    original_unlink = store_wakes.unlink_quietly

    def unlink_while_held(selected):
        # 两次原读取完成后才让另一线程持锁，避免读锁与测试事件互相等待。
        acquire.set()
        assert acquired.wait(5)
        original_unlink(selected)

    monkeypatch.setattr(store_wakes, "unlink_quietly", unlink_while_held)

    def holder():
        assert acquire.wait(5)
        with locked_json_path(path):
            acquired.set()
            assert recheck.wait(5)
            assert read_json_file(path) == {}
            assert store.wakes.pending_one(signal.wake_signal_id) is None
            rechecked.set()
            assert release.wait(5)
            assert read_json_file(path) == {}

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(holder)
        try:
            assert store.wakes.mark_handled(signal.wake_signal_id) is not None
            assert not path.exists() and lock_path.exists()
            assert store.wakes.pending_report() == ([], [])
            assert lock_path.exists()
            recheck.set()
            assert rechecked.wait(5)
        finally:
            acquire.set()
            recheck.set()
            release.set()
        future.result(timeout=5)

    # 同实例不重复扫历史；重新打开的存储用自己的首次清扫回收已释放锁。
    assert store.wakes.pending_report() == ([], [])
    assert lock_path.exists()
    reopened = ConversationStore(store.storage.root)
    assert reopened.wakes.pending_report() == ([], [])
    assert not lock_path.exists() and not path.exists()


@posix_locks
def test_symlink_lock_is_not_followed_or_removed(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    directory = store.storage.wake_queue_dir / "normal"
    target = tmp_path / "external.lock"
    target.write_bytes(b"preserve")
    link = directory / "orphan.json.lock"
    os.symlink(target, link)

    assert store.wakes.pending_report() == ([], [])

    assert link.is_symlink()
    assert target.read_bytes() == b"preserve"


def test_without_fcntl_does_not_open_or_remove_lock(tmp_path, monkeypatch):
    path = tmp_path / "orphan.json"
    lock_path = tmp_path / "orphan.json.lock"
    lock_path.write_bytes(b"preserve")

    def forbidden_open(*args, **kwargs):
        pytest.fail("没有 fcntl 时不能打开锁文件")

    with monkeypatch.context() as patch:
        patch.setattr(store_wakes, "fcntl", None)
        patch.delattr(os, "O_NOFOLLOW", raising=False)
        patch.setattr(os, "open", forbidden_open)
        store_wakes._discard_orphan_lock(path)

    assert lock_path.read_bytes() == b"preserve"


@posix_locks
def test_discard_preserves_record_and_does_not_create_missing_lock(tmp_path):
    path = tmp_path / "wake.json"
    lock_path = tmp_path / "wake.json.lock"
    store_wakes._discard_orphan_lock(path)
    assert not lock_path.exists()

    path.write_bytes(b"{}")
    lock_path.touch()
    store_wakes._discard_orphan_lock(path)
    assert lock_path.exists() and path.read_bytes() == b"{}"

    path.unlink()
    path.symlink_to(tmp_path / "missing.json")
    store_wakes._discard_orphan_lock(path)
    assert lock_path.exists() and path.is_symlink()


@posix_locks
def test_discard_rechecks_record_after_acquiring_lock(tmp_path, monkeypatch):
    path = tmp_path / "wake.json"
    lock_path = tmp_path / "wake.json.lock"
    lock_path.touch()
    original_flock = store_wakes.fcntl.flock

    def publish_after_acquire(descriptor, operation):
        original_flock(descriptor, operation)
        path.write_bytes(b"{}")

    monkeypatch.setattr(store_wakes.fcntl, "flock", publish_after_acquire)
    store_wakes._discard_orphan_lock(path)
    assert lock_path.exists() and path.read_bytes() == b"{}"


@posix_locks
def test_discard_preserves_replacement_lock_inode(tmp_path, monkeypatch):
    path = tmp_path / "wake.json"
    lock_path = tmp_path / "wake.json.lock"
    lock_path.touch()
    original_fstat = os.fstat
    replacement = []

    def replace_after_fstat(descriptor):
        opened = original_fstat(descriptor)
        lock_path.unlink()
        new_descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        replacement.append(new_descriptor)
        store_wakes.fcntl.flock(new_descriptor, store_wakes.fcntl.LOCK_EX | store_wakes.fcntl.LOCK_NB)
        return opened

    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "fstat", replace_after_fstat)
            store_wakes._discard_orphan_lock(path)
        assert len(replacement) == 1
        latest = lock_path.stat()
        opened = original_fstat(replacement[0])
        assert (latest.st_dev, latest.st_ino) == (opened.st_dev, opened.st_ino)
    finally:
        for descriptor in replacement:
            os.close(descriptor)


@posix_locks
@pytest.mark.parametrize("shape", ["directory", "fifo", "hardlink"])
def test_discard_preserves_nonregular_or_shared_lock(tmp_path, shape):
    path = tmp_path / "wake.json"
    lock_path = tmp_path / "wake.json.lock"
    if shape == "directory":
        lock_path.mkdir()
    elif shape == "fifo":
        os.mkfifo(lock_path)
    else:
        target = tmp_path / "shared.lock"
        target.write_bytes(b"preserve")
        os.link(target, lock_path)
    store_wakes._discard_orphan_lock(path)
    assert lock_path.exists()


@posix_locks
@pytest.mark.parametrize("failure", ["flock", "fstat", "unlink"])
def test_discard_closes_descriptor_on_io_error(tmp_path, monkeypatch, failure):
    path = tmp_path / "wake.json"
    lock_path = tmp_path / "wake.json.lock"
    lock_path.touch()
    original_fstat = os.fstat
    original_open = os.open
    descriptors = []

    def tracked_open(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        descriptors.append(descriptor)
        return descriptor

    def fail(*args, **kwargs):
        raise OSError("受控 I/O 失败")

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", tracked_open)
        patch.setattr(store_wakes.fcntl if failure == "flock" else os, failure, fail)
        store_wakes._discard_orphan_lock(path)
    assert len(descriptors) == 1
    with pytest.raises(OSError):
        original_fstat(descriptors[0])
    assert lock_path.exists()


@posix_locks
def test_first_sweep_only_visits_selected_queue_kinds(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    urgent = store.storage.wake_queue_dir / "urgent" / "old.json.lock"
    normal = store.storage.wake_queue_dir / "normal" / "old.json.lock"
    handled = store.storage.wake_handled_dir / "old.json.lock"
    unrelated = normal.with_name("other.lock")
    for path in (urgent, normal, handled, unrelated):
        path.touch()

    assert store.wakes.pending_report(include_normal=False) == ([], [])
    assert not urgent.exists() and normal.exists()
    assert store.wakes.pending_report() == ([], [])
    assert not normal.exists()
    assert handled.exists() and unrelated.exists()
