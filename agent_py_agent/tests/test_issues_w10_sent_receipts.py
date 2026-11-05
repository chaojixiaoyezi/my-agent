"""W10: periodic retention through real standalone/combined delivery entries."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.adapter import delivery as d
from agent_py_agent.agent.adapter.delivery import (
    GatewayReplyDeliveryStore,
    GatewayReplyDeliveryWorker,
    PendingGatewayReply,
)
from agent_py_agent.agent.adapter.ingress import GatewayAdapterDeliveryWorker, GatewayIngressStore
from agent_py_agent.agent.common.json_io import locked_json_path


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(d, "time", SimpleNamespace(time=lambda: clock.now, monotonic=lambda: d.monotonic()))
    return clock


def _worker(store, deliveries):
    def send(record, text):
        deliveries.append((record.stable_id, text))
        return True

    return GatewayReplyDeliveryWorker(store, poll_response=lambda record: "最终回复", deliver_response=send)


def _combined(root, worker):
    return GatewayAdapterDeliveryWorker(GatewayIngressStore(root), reply_worker=worker,
        prepare_payload=lambda record: {}, submit_payload=lambda record: {},
        advance_submission=lambda record: None, handoff_reply=lambda record: None)


def _delivered(root, count, clock, monkeypatch):
    # Separate tests cover the wall-clock guard; here each deterministic window is 128 files.
    monkeypatch.setattr(d, "monotonic", lambda: 0.0)
    store, deliveries = GatewayReplyDeliveryStore(root), []
    worker = _worker(store, deliveries)
    for i in range(count):
        worker.enqueue(PendingGatewayReply(f"request-{i}", "fixture", "user", f"message-{i}"))
    assert worker.run_once() == count
    assert len(deliveries) == count
    store.prune_sent_receipts(now=clock.now)
    for path in (root / "sent").glob("*.json"):
        os.utime(path, (clock.now, clock.now))
    return store, worker, deliveries


@pytest.mark.parametrize("combined", [False, True])
def test_periodic_worker_retention_and_replay_match_cold_restart(tmp_path, clock, monkeypatch, combined):
    store, worker, deliveries = _delivered(tmp_path / "reply", 1, clock, monkeypatch)
    path = next((store.root / "sent").glob("*.json"))
    lock = (store.root / "pending" / path.name).with_suffix(".json.lock")
    assert lock.exists()
    clock.now += 8 * 24 * 60 * 60
    entry = _combined(tmp_path / "ingress", worker) if combined else worker
    assert entry.run_once() == 0
    assert not path.exists() and not lock.exists()
    assert not store._volatile_sent and not store._volatile_receipts
    assert not store._volatile_durable_at
    assert not store.was_sent("request-0")
    assert not GatewayReplyDeliveryStore(store.root).was_sent("request-0")
    worker.enqueue(PendingGatewayReply("request-0", "fixture", "user", "message-0"))
    assert entry.run_once() == 1
    assert len(deliveries) == 2
    assert store.was_sent("request-0")


def test_shared_store_runs_one_file_pass_per_interval(tmp_path, clock, monkeypatch):
    store, first, _ = _delivered(tmp_path / "reply", 1, clock, monkeypatch)
    second = _worker(store, [])
    combined = _combined(tmp_path / "ingress", second)
    original = d.os.scandir
    calls = []

    def counted(directory):
        if Path(directory) == store.root / "sent":
            calls.append(directory)
        return original(directory)

    monkeypatch.setattr(d.os, "scandir", counted)
    clock.now += 61
    first.run_once()
    combined.run_once()
    first.run_once()
    assert len(calls) == 1
    assert store.was_sent("request-0")  # Recent receipts are still idempotency proof.


@pytest.mark.parametrize("writer", ["mark_sent", "mark_discarded"])
def test_failed_durable_write_never_ages_out_volatile_proof(tmp_path, clock, monkeypatch, writer):
    store = GatewayReplyDeliveryStore(tmp_path / "reply")
    deliveries = []
    worker = _worker(store, deliveries)
    worker.enqueue(PendingGatewayReply("failed-proof", "fixture", "user", "message"))

    write = d._atomic_write_json

    def fail(path, payload):
        if path.parent.name == "sent":
            raise OSError("simulated receipt I/O failure")
        return write(path, payload)

    def legacy_terminal(record, **kwargs):
        if writer == "mark_sent":
            store.mark_sent(record)
        else:
            store.mark_discarded(record, reason="interrupted")
        return True

    monkeypatch.setattr(store, "mark_terminal_claimed", legacy_terminal)
    monkeypatch.setattr(d, "_atomic_write_json", fail)
    assert worker.run_once() == 0
    assert len(deliveries) == 1
    assert store.was_sent("failed-proof")
    assert "failed-proof" not in store._volatile_durable_at
    clock.now += 8 * 24 * 60 * 60
    store.maintain_sent_receipts(now=clock.now, force=True)
    assert store.was_sent("failed-proof")
    assert "failed-proof" in store._volatile_receipts
    assert worker.run_once() == 0
    assert len(deliveries) == 1


def test_pending_unknown_and_quarantined_receipts_are_protected(tmp_path, clock, monkeypatch):
    store, _, _ = _delivered(tmp_path / "reply", 3, clock, monkeypatch)
    paths = sorted((store.root / "sent").glob("*.json"))
    for path, disposition in zip(paths[:2], ("unknown", "quarantined"), strict=True):
        payload = json.loads(path.read_text())
        payload["disposition"] = disposition
        path.write_text(json.dumps(payload))
        os.utime(path, (clock.now, clock.now))
    (store.root / "pending" / paths[2].name).write_text("{}")
    clock.now += 8 * 24 * 60 * 60
    report = store.maintain_sent_receipts(now=clock.now, force=True)
    assert report["deleted"] == 0
    assert report["kept"] == {"disposition_protected": 2, "pending_active": 1}
    assert all(path.exists() for path in paths)


def test_busy_lock_does_not_advance_cursor_past_undecided_receipt(tmp_path, clock, monkeypatch):
    store, _, _ = _delivered(tmp_path / "reply", 1, clock, monkeypatch)
    path = next((store.root / "sent").glob("*.json"))
    held, release = threading.Event(), threading.Event()

    def holder():
        with locked_json_path(store.root / "pending" / path.name):
            held.set()
            assert release.wait(10)

    thread = threading.Thread(target=holder)
    thread.start()
    assert held.wait(10)
    try:
        clock.now += 8 * 24 * 60 * 60
        report = store.maintain_sent_receipts(now=clock.now, force=True)
        assert report["kept"] == {"lock_busy": 1}
        assert not report["complete"] and store._sent_cursor == ""
        assert path.exists()
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()
    assert store.maintain_sent_receipts(now=clock.now)["deleted"] == 1


def test_three_hundred_receipts_progress_128_at_a_time(tmp_path, clock, monkeypatch):
    store, _, _ = _delivered(tmp_path / "reply", 300, clock, monkeypatch)
    clock.now += 8 * 24 * 60 * 60
    reports = [store.maintain_sent_receipts(now=clock.now) for _ in range(3)]
    assert [report["scanned"] for report in reports] == [128, 128, 44]
    assert [report["deleted"] for report in reports] == [128, 128, 44]
    assert [report["volatile_retired"] for report in reports] == [128, 128, 44]
    assert [report["complete"] for report in reports] == [False, False, True]
    assert not store._volatile_sent and not store._volatile_receipts
    assert list((store.root / "sent").glob("*.json")) == []
    assert store.maintain_sent_receipts(now=clock.now) == {"ran": False}


def test_missing_empty_and_volatile_stores_create_no_directories(tmp_path, clock):
    missing = tmp_path / "missing"
    store = GatewayReplyDeliveryStore(missing)
    assert store.maintain_sent_receipts(now=clock.now)["complete"]
    store.prune_sent_receipts(now=clock.now)
    assert not missing.exists()
    empty = tmp_path / "empty"
    (empty / "sent").mkdir(parents=True)
    GatewayReplyDeliveryStore(empty).prune_sent_receipts(now=clock.now)
    assert not (empty / "pending").exists()
    assert GatewayReplyDeliveryStore(None).maintain_sent_receipts() == {"ran": False, "reason": "not_durable"}


def test_roots_are_independent_and_recent_sent_at_is_kept(tmp_path, clock, monkeypatch):
    first, _, _ = _delivered(tmp_path / "first", 1, clock, monkeypatch)
    second, _, _ = _delivered(tmp_path / "second", 1, clock, monkeypatch)
    second_path = next((second.root / "sent").glob("*.json"))
    clock.now += 8 * 24 * 60 * 60
    assert first.maintain_sent_receipts(now=clock.now)["deleted"] == 1
    assert second.was_sent("request-0")
    payload = json.loads(second_path.read_text())
    payload["sent_at"] = clock.now
    second_path.write_text(json.dumps(payload))
    os.utime(second_path, (1000, 1000))
    assert second.maintain_sent_receipts(now=clock.now)["kept"] == {"young": 1}
    assert second_path.exists()


def test_slice_wall_budget_stops_and_keeps_progress_pending(tmp_path, clock, monkeypatch):
    store, _, _ = _delivered(tmp_path / "reply", 3, clock, monkeypatch)
    clock.now += 8 * 24 * 60 * 60
    ticks = iter(i * 0.030 for i in range(100))
    monkeypatch.setattr(d, "monotonic", lambda: next(ticks))
    report = store.maintain_sent_receipts(now=clock.now)
    assert report["scanned"] == 0 and not report["complete"]
    assert store._sent_maintenance_due_at == clock.now


@pytest.mark.parametrize("writer", ["mark_sent", "mark_discarded"])
def test_failed_rewrite_removes_prior_retirement_timestamp(tmp_path, clock, monkeypatch, writer):
    store, _, _ = _delivered(tmp_path / "reply", 1, clock, monkeypatch)
    clock.now += 8 * 24 * 60 * 60

    def fail(path, payload):
        raise OSError("simulated terminal rewrite failure")

    monkeypatch.setattr(d, "_atomic_write_json", fail)
    record = PendingGatewayReply("request-0", "fixture", "user", "message-0")
    with pytest.raises(OSError):
        if writer == "mark_sent":
            store.mark_sent(record)
        else:
            store.mark_discarded(record, reason="interrupted")
    assert "request-0" not in store._volatile_durable_at
    assert store.maintain_sent_receipts(now=clock.now)["deleted"] == 1
    assert store.was_sent("request-0")
    assert "request-0" in store._volatile_receipts


def test_non_scalar_disposition_is_protected_without_stalling_pass(tmp_path, clock, monkeypatch):
    store, _, _ = _delivered(tmp_path / "reply", 1, clock, monkeypatch)
    path = next((store.root / "sent").glob("*.json"))
    payload = json.loads(path.read_text())
    payload["disposition"] = {"unexpected": "state"}
    path.write_text(json.dumps(payload))
    os.utime(path, (clock.now, clock.now))
    clock.now += 8 * 24 * 60 * 60
    report = store.maintain_sent_receipts(now=clock.now)
    assert report["complete"]
    assert report["kept"] == {"disposition_protected": 1}
    assert path.exists()


def test_receipt_symlink_and_oversized_payload_are_not_followed_or_deleted(tmp_path, clock, monkeypatch):
    store, _, _ = _delivered(tmp_path / "reply", 1, clock, monkeypatch)
    path = next((store.root / "sent").glob("*.json"))
    path.write_bytes(b" " * (64 * 1024 + 1))
    os.utime(path, (clock.now, clock.now))
    target = tmp_path / "outside-sent.json"
    target.write_text('{"important":"keep"}')
    link = store.root / "sent" / "symlink.json"
    link.symlink_to(target)
    clock.now += 8 * 24 * 60 * 60
    report = store.maintain_sent_receipts(now=clock.now)
    assert report["deleted"] == 0
    assert report["kept"] == {"oversized": 1, "not_regular": 1}
    assert link.is_symlink() and path.exists()
    assert target.read_text() == '{"important":"keep"}'


def test_pending_lock_is_removed_only_while_owned_after_receipt_unlink(tmp_path, clock, monkeypatch):
    store, _, _ = _delivered(tmp_path / "reply", 1, clock, monkeypatch)
    receipt = next((store.root / "sent").glob("*.json"))
    pending = store.root / "pending" / receipt.name
    lock = pending.with_suffix(".json.lock")
    unlink = Path.unlink
    observed = []

    def checked_unlink(path, *args, **kwargs):
        if path == lock:
            assert not receipt.exists() and not pending.exists()

            def contender():
                try:
                    with locked_json_path(pending, blocking=False):
                        observed.append("unlocked")
                except BlockingIOError:
                    observed.append("owned")

            thread = threading.Thread(target=contender)
            thread.start()
            thread.join(10)
            assert not thread.is_alive()
            assert observed == ["owned"]
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", checked_unlink)
    clock.now += 8 * 24 * 60 * 60
    assert store.maintain_sent_receipts(now=clock.now)["deleted"] == 1
    assert observed == ["owned"] and not lock.exists()


@pytest.mark.parametrize("value", [{}, [], False, 0, "", None])
def test_explicit_invalid_disposition_never_becomes_legacy_sent(tmp_path, clock, monkeypatch, value):
    store, _, _ = _delivered(tmp_path / "reply", 1, clock, monkeypatch)
    path = next((store.root / "sent").glob("*.json"))
    payload = json.loads(path.read_text())
    payload["disposition"] = value
    path.write_text(json.dumps(payload))
    os.utime(path, (clock.now, clock.now))
    clock.now += 8 * 24 * 60 * 60
    report = store.maintain_sent_receipts(now=clock.now)
    assert report["deleted"] == 0
    assert report["kept"] == {"disposition_protected": 1}
    assert path.exists()


def test_missing_disposition_still_supports_legacy_sent_receipts(tmp_path, clock, monkeypatch):
    store, _, _ = _delivered(tmp_path / "reply", 1, clock, monkeypatch)
    path = next((store.root / "sent").glob("*.json"))
    payload = json.loads(path.read_text())
    payload.pop("disposition", None)
    path.write_text(json.dumps(payload))
    os.utime(path, (clock.now, clock.now))
    clock.now += 8 * 24 * 60 * 60
    report = store.maintain_sent_receipts(now=clock.now)
    assert report["deleted"] == 1
    assert not path.exists() and not store.was_sent("request-0")


def test_slow_directory_discovery_resumes_instead_of_restarting_forever(tmp_path, clock, monkeypatch):
    store, _, _ = _delivered(tmp_path / "reply", 5, clock, monkeypatch)
    clock.now += 8 * 24 * 60 * 60
    scan = d.os.scandir
    elapsed = SimpleNamespace(value=0.0, opens=0)

    class SlowScan:
        def __init__(self, directory):
            self.inner = scan(directory)
            elapsed.opens += 1

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

        def __iter__(self):
            return self

        def __next__(self):
            try:
                entry = next(self.inner)
            except StopIteration:
                elapsed.value += 0.030
                raise
            elapsed.value += 0.010
            return entry

        def close(self):
            self.inner.close()

    monkeypatch.setattr(d.os, "scandir", SlowScan)
    monkeypatch.setattr(d, "monotonic", lambda: elapsed.value)
    reports = []
    for _ in range(10):
        report = store.maintain_sent_receipts(now=clock.now)
        reports.append(report)
        if report["complete"]:
            break
    assert reports[0]["scanned"] == 0 and not reports[0]["complete"]
    assert sum(report["deleted"] for report in reports) == 5
    assert reports[-1]["complete"]
    assert elapsed.opens == 1
    assert list((store.root / "sent").glob("*.json")) == []


def test_late_old_inode_waiter_cannot_enter_beside_new_pending_lock(tmp_path, clock, monkeypatch):
    from agent_py_agent.agent.common import json_io

    store, _, _ = _delivered(tmp_path / "reply", 1, clock, monkeypatch)
    receipt = next((store.root / "sent").glob("*.json"))
    pending = store.root / "pending" / receipt.name
    lock = pending.with_suffix(".json.lock")
    stale = os.open(lock, os.O_RDWR)
    clock.now += 8 * 24 * 60 * 60
    assert store.maintain_sent_receipts(now=clock.now)["deleted"] == 1
    assert not lock.exists()
    fresh = json_io._open_private_lock_descriptor(lock)
    json_io._flock_descriptor(fresh, blocking=False)
    original_open = json_io._open_private_lock_descriptor
    attempts = []

    def delayed_open(path):
        attempts.append(path)
        if len(attempts) == 1:
            return stale  # B opened the old inode before A unlinked its name.
        return original_open(path)

    try:
        monkeypatch.setattr(json_io, "_open_private_lock_descriptor", delayed_open)
        with pytest.raises(BlockingIOError):
            json_io._acquire_verified_lock_descriptor(pending, blocking=False)
        assert len(attempts) == 2  # Reject old inode, retry current name, see C's lock.
        assert json_io._descriptor_matches_lock_path(fresh, lock)
    finally:
        json_io._funlock_descriptor(fresh)
        os.close(fresh)


def test_maintenance_preserves_host_interrupt_signal(tmp_path, clock, monkeypatch):
    store, worker, _ = _delivered(tmp_path / "reply", 1, clock, monkeypatch)

    def interrupted(*, now=None, force=False):
        raise InterruptedError("host stop signal")

    monkeypatch.setattr(store, "maintain_sent_receipts", interrupted)
    with pytest.raises(InterruptedError):
        worker.run_once()


def test_maintenance_io_error_keeps_delivery_count_and_worker_usable(tmp_path, clock, monkeypatch):
    store, worker, deliveries = _delivered(tmp_path / "reply", 1, clock, monkeypatch)

    def failed(*, now=None, force=False):
        raise OSError("simulated maintenance failure")

    monkeypatch.setattr(store, "maintain_sent_receipts", failed)
    for index in range(2):
        worker.enqueue(PendingGatewayReply(f"after-maintenance-{index}", "fixture", "user", f"message-{index}"))
        assert worker.run_once() == 1
        assert store.was_sent(f"after-maintenance-{index}")
    assert len(deliveries) == 3
