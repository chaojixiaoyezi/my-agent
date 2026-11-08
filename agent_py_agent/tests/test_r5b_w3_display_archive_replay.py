"""真实目录枚举覆盖冷恢复预算、尾部推进以及重放迭代器的资源寿命。"""
from __future__ import annotations

import os
import shutil
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import display_archive as archive
from agent_py_agent.agent.conversation import display_archive_maintenance as maintenance
from agent_py_agent.tests.test_display_archive_maintenance import (
    _fresh_process,
    _ordered_archives,
)

NOW = 2_000_000_000.0


@pytest.fixture(autouse=True)
def clean_scans():
    _fresh_process()
    yield
    _fresh_process()


def _seed_cursor(root, operation, offset):
    maintenance._persisted_cursor(root, root.stat(), operation.__name__ + ".<locals>.action", offset)


class EnumerationMeter:
    """透传真实 scandir，只计维护调用期间的原始 yield，并记录关闭事实。"""

    def __init__(self, monkeypatch):
        original = os.scandir
        self.counts, self.opened = [], []
        self.active = False
        self.fail_after = None
        meter = self

        class Observed:
            def __init__(self, target):
                self.entries = original(target)
                self.closed = False
                meter.opened.append(self)

            def __enter__(self):
                self.entries.__enter__()
                return self

            def __iter__(self):
                return self

            def __next__(self):
                if meter.active and meter.fail_after == meter.counts[-1]:
                    meter.fail_after = None
                    raise OSError("恢复前缀时读取中断")
                entry = next(self.entries)
                if meter.active:
                    meter.counts[-1] += 1
                return entry

            def __exit__(self, *args):
                self.closed = True
                return self.entries.__exit__(*args)

        monkeypatch.setattr(maintenance.os, "scandir", Observed)

    def step(self, operation, root, *, allow_errors=False, **kwargs):
        self.counts.append(0)
        self.active = True
        try:
            result = operation(root, now=NOW, max_archives=1, **kwargs)
        finally:
            self.active = False
        assert self.counts[-1] <= maintenance._DISPLAY_ARCHIVE_MIN_SCAN_BUDGET_ENTRIES
        if not allow_errors:
            assert result["errors"] == 0
        return result


@pytest.mark.parametrize("operation", [maintenance.compact_display_archives, maintenance.prune_expired_display_archives])
def test_cold_prefix_replay_counts_every_raw_entry(tmp_path, monkeypatch, operation):
    store, _ = _ordered_archives(tmp_path, 256)
    root = tmp_path / "display_archives"
    _seed_cursor(root, operation, 192)
    meter = EnumerationMeter(monkeypatch)
    result = meter.step(operation, store)
    assert meter.counts == [64]
    assert result["scanned"] == 0 and result["backlog"] == 1
    assert list(maintenance._scan_cursors.values()) == [64]


def test_tail_progress_survives_another_process_reset_and_preserves_pins(tmp_path, monkeypatch):
    store, ordered = _ordered_archives(tmp_path, 256)
    for _, directory in ordered:
        os.utime(directory / "manifest.json", (NOW - 16 * 86400,) * 2)
    pins = {reference["archive_id"] for reference, _ in ordered[:-2]}
    protected = {p.name: p.read_bytes() for p in ordered[0][1].iterdir()}
    operation = maintenance.prune_expired_display_archives
    _seed_cursor(tmp_path / "display_archives", operation, 192)
    meter = EnumerationMeter(monkeypatch)
    for _ in range(12):
        if meter.step(operation, store, pinned_archive_ids=pins)["expired"]:
            break
    else:
        pytest.fail("首次冷恢复没有推进到尾部")
    _fresh_process()
    for _ in range(12):
        if meter.step(operation, store, pinned_archive_ids=pins)["expired"]:
            break
    else:
        pytest.fail("再次清空进程状态后尾部被饿死")
    for reference, _ in ordered[-2:]:
        with pytest.raises(archive.DisplayArchiveError) as caught:
            archive.read_display_archive_page(store, reference, 0)
        assert caught.value.code == "DISPLAY_ARCHIVE_EXPIRED"
    assert {p.name: p.read_bytes() for p in ordered[0][1].iterdir()} == protected
    assert archive.read_display_archive_page(store, ordered[0][0], 0)["rows"]


@pytest.mark.parametrize("during_replay", [False, True], ids=["already-shortened", "delete-during-replay"])
def test_shortened_directory_eventually_reaches_its_tail(tmp_path, monkeypatch, during_replay):
    store, ordered = _ordered_archives(tmp_path, 256)
    operation = maintenance.compact_display_archives
    _seed_cursor(tmp_path / "display_archives", operation, 192)
    meter = EnumerationMeter(monkeypatch)
    if during_replay:
        meter.step(operation, store)
    for _, directory in ordered[:181]:
        shutil.rmtree(directory)
    for _ in range(90):
        meter.step(operation, store, min_age_days=0)
        if (ordered[-1][1] / "pages_bundle.json").is_file():
            break
    else:
        pytest.fail("目录缩短后没有完成尾部归档")
    assert archive.read_display_archive_page(store, ordered[-1][0], 0)["rows"]


def test_prefix_error_closes_owned_iterator_before_retry(tmp_path, monkeypatch):
    store, _ = _ordered_archives(tmp_path, 256)
    operation = maintenance.compact_display_archives
    _seed_cursor(tmp_path / "display_archives", operation, 192)
    meter = EnumerationMeter(monkeypatch)
    meter.fail_after = 17
    assert meter.step(operation, store, allow_errors=True)["errors"] == 1
    assert meter.counts == [17]
    assert not maintenance._live_scans and all(scan.closed for scan in meter.opened)
    assert meter.step(operation, store)["compacted"] == 1


@pytest.mark.parametrize("idle_seconds", [0, 17])
def test_idle_eviction_closes_partial_replay_and_return_is_bounded(tmp_path, monkeypatch, idle_seconds):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(maintenance, "time", SimpleNamespace(
        time=maintenance.time.time, monotonic=lambda: clock.now,
    ))
    owner = tmp_path / "owner"
    owner.mkdir()
    store, _ = _ordered_archives(owner, 256)
    operation = maintenance.compact_display_archives
    root = owner / "display_archives"
    _seed_cursor(root, operation, 192)
    other = tmp_path / "other" / "display_archives"
    other.mkdir(parents=True)
    meter = EnumerationMeter(monkeypatch)
    meter.step(operation, store, idle_seconds=idle_seconds)
    original = meter.opened[0]
    for _ in range(6):
        meter.step(operation, other, idle_seconds=idle_seconds)
    assert not original.closed
    clock.now += 2 * (idle_seconds or maintenance.MemoryRetentionPolicy.maintenance_interval_seconds) + 1
    meter.step(operation, other, idle_seconds=idle_seconds)
    assert original.closed
    meter.step(operation, store, idle_seconds=idle_seconds)
    assert meter.counts[-1] == 64
    _fresh_process()
    assert all(scan.closed for scan in meter.opened)


def test_capacity_competition_keeps_partial_replay_bounded_and_closes_every_scan(tmp_path, monkeypatch):
    roots = []
    operation = maintenance.compact_display_archives
    for index in range(129):
        root = tmp_path / str(index) / "display_archives"
        root.mkdir(parents=True)
        for number in range(80):
            (root / f"filler-{number}").touch()
        _seed_cursor(root, operation, 70)
        roots.append(root)
    meter = EnumerationMeter(monkeypatch)
    for root in roots:
        assert meter.step(operation, root)["backlog"] == 1
        assert len(maintenance._live_scans) <= 128
    assert len(maintenance._live_scans) == 128 and maintenance._scan_waiting
    for root in roots[:-1]:
        meter.step(operation, root)
    for _ in range(3):
        meter.step(operation, roots[-1])
    assert not maintenance._scan_waiting
    _fresh_process()
    assert all(scan.closed for scan in meter.opened)
