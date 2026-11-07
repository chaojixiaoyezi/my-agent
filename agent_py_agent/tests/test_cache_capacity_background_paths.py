"""后台缓存随槽位扩容；真实配额锁与Event交错验证跨用户并行和同用户串行。"""

import json
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common import cache_capacity
from agent_py_agent.agent.gateway_parts import request_worker
from agent_py_agent.agent.plugin_installation_state import encode_installation_state
from agent_py_agent.agent.scheduler import repository as scheduler
from agent_py_agent.agent.tooling import plugin_registration
from agent_py_agent.agent.user_space import owner_quota as quota
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home


@pytest.fixture(autouse=True)
def isolated_caches(monkeypatch):
    cache_capacity.reset_for_tests()
    for module, names in (
        (quota, ("_QUOTA_SCANS", "_QUOTA_ROOT_STAMPS", "_QUOTA_FINITE_ROOTS", "_DISPLAY_USAGE_CACHE")),
        (scheduler, ("_WAITING_PROJECTION_CACHE", "_STORE_PROJECTION_CACHE")),
        (request_worker, ("_PENDING_SCAN_CACHE",)),
        (plugin_registration, ("_INSTALLATION_SNAPSHOTS",)),
    ):
        for name in names:
            monkeypatch.setattr(module, name, OrderedDict())
    for name in ("_WAITING_PROJECTION_STATS", "_STORE_PROJECTION_STATS"):
        monkeypatch.setattr(scheduler, name, dict.fromkeys(getattr(scheduler, name), 0))
    try:
        yield
    finally:
        for scan in quota._QUOTA_SCANS.values():
            scan["iterator"].close()
        quota._QUOTA_SCANS.clear()
        cache_capacity.reset_for_tests()


def _configure(*, owners=0, inflight=0):
    cache_capacity.configure_from_config(SimpleNamespace(
        gateway_owner_inflight_limit=owners,
        gateway_global_inflight_limit=inflight,
    ))


def _owner_root(tmp_path, name):
    root = tmp_path / name
    root.mkdir()
    for index in range(4):
        (root / f"part-{index}").write_bytes(b"abc")
    return root


def _scan_key(root):
    return os.getpid(), str(root)


def test_64_owner_quota_rotation_keeps_the_same_scanners(tmp_path):
    _configure(owners=64)
    roots = [_owner_root(tmp_path, f"owner-{index}") for index in range(64)]
    scans = {}
    for root in roots:
        report = quota.reconcile_owner_quota_slice(root, max_entries=1, max_ms=5000)
        assert report["scanned_entries"] == 1 and not report["complete"]
        scans[root] = quota._QUOTA_SCANS[_scan_key(root)]
    assert len(quota._QUOTA_SCANS) == 64
    for root in roots:
        original = scans[root]
        report = quota.reconcile_owner_quota_slice(root, max_entries=1, max_ms=5000)
        assert quota._QUOTA_SCANS[_scan_key(root)] is original
        assert report["cursor"]["epoch"] == original["epoch"]
        assert report["cursor"]["scanned"] == 2
        # 首片先越过新建的控制锁；第二片计入第一个真实载荷文件。
        assert report["cursor"]["partial_used_bytes"] == 3


def test_other_owners_finish_slices_while_a_scan_is_running(tmp_path, monkeypatch):
    monkeypatch.setattr(quota, "_QUOTA_SCAN_CACHE_SIZE", 1)
    roots = [_owner_root(tmp_path, name) for name in ("a", "b", "c")]
    started, release, pause = threading.Event(), threading.Event(), threading.Event()
    original = quota._sorted_quota_entries

    def gated(root, **kwargs):
        for entry in original(root, **kwargs):
            if root == roots[0] and pause.is_set():
                started.set()
                assert release.wait(10)
            yield entry

    monkeypatch.setattr(quota, "_sorted_quota_entries", gated)
    quota.reconcile_owner_quota_slice(roots[0], max_entries=1, max_ms=5000)
    active = quota._QUOTA_SCANS[_scan_key(roots[0])]
    pause.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(quota.reconcile_owner_quota_slice, roots[0], max_entries=1, max_ms=5000)
        try:
            assert started.wait(5)
            assert _scan_key(roots[0]) not in quota._QUOTA_SCANS
            for root in roots[1:]:
                other = pool.submit(quota.reconcile_owner_quota_slice, root,
                                    max_entries=1, max_ms=5000, blocking=False)
                assert other.result(timeout=5)["scanned_entries"] == 1
                assert not first.done()
            assert list(quota._QUOTA_SCANS) == [_scan_key(roots[2])]
        finally:
            release.set()
        assert first.result(timeout=5)["cursor"]["scanned"] == 2
    assert quota._QUOTA_SCANS[_scan_key(roots[0])] is active


def test_same_owner_slices_stay_serial_under_the_file_lock(tmp_path, monkeypatch):
    root = _owner_root(tmp_path, "owner")
    entered, release, second_attempt, second_step = (threading.Event() for _ in range(4))
    original_scan, original_lock = quota._sorted_quota_entries, quota._locked_owner_quota
    scans, attempts = [], []

    def gated(*args, **kwargs):
        scans.append(1)
        for index, entry in enumerate(original_scan(*args, **kwargs)):
            if index == 0:
                entered.set()
                assert release.wait(10)
            else:
                second_step.set()
            yield entry

    @contextmanager
    def observed_lock(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 2:
            second_attempt.set()
        with original_lock(*args, **kwargs):
            yield

    monkeypatch.setattr(quota, "_sorted_quota_entries", gated)
    monkeypatch.setattr(quota, "_locked_owner_quota", observed_lock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(quota.reconcile_owner_quota_slice, root, max_entries=1, max_ms=5000)
        try:
            assert entered.wait(5)
            second = pool.submit(quota.reconcile_owner_quota_slice, root, max_entries=1, max_ms=5000)
            assert second_attempt.wait(5)
            assert not second_step.is_set() and not second.done()
            with pytest.raises(quota.OwnerQuotaLockContention):
                quota.reconcile_owner_quota_slice(root, blocking=False)
        finally:
            release.set()
        first_report, second_report = first.result(timeout=5), second.result(timeout=5)
    assert scans == [1] and second_step.is_set()
    assert first_report["cursor"]["epoch"] == second_report["cursor"]["epoch"]
    assert first_report["cursor"]["scanned"] == 1
    assert second_report["cursor"]["scanned"] == 2


@pytest.mark.parametrize("phase", ["scan", "checkpoint", "checkin"])
def test_checked_out_scanner_closes_on_failure(tmp_path, monkeypatch, phase):
    root = _owner_root(tmp_path, "owner")
    closed = threading.Event()
    original_scan, original_cache_lock = quota._sorted_quota_entries, quota._locked_quota_scan_cache
    lock_calls = []

    def observed(*args, **kwargs):
        try:
            if phase == "scan":
                raise OSError("scan failed")
            yield from original_scan(*args, **kwargs)
        finally:
            closed.set()

    @contextmanager
    def cache_lock(**kwargs):
        lock_calls.append(1)
        if phase == "checkin" and len(lock_calls) == 2:
            raise quota.OwnerQuotaLockContention("cache busy")
        with original_cache_lock(**kwargs):
            yield

    def failed_write(*_args):
        raise OSError("checkpoint failed")

    monkeypatch.setattr(quota, "_sorted_quota_entries", observed)
    monkeypatch.setattr(quota, "_locked_quota_scan_cache", cache_lock)
    if phase == "checkpoint":
        monkeypatch.setattr(quota, "_write_quota_ledger", failed_write)
    with pytest.raises((OSError, quota.OwnerQuotaLockContention)):
        quota.reconcile_owner_quota_slice(root, max_entries=1, max_ms=5000)
    assert closed.is_set() and not quota._QUOTA_SCANS
    with quota._locked_owner_quota(root / quota._QUOTA_LOCK_BASENAME, blocking=False):
        pass


def _scheduler_repository(tmp_path, index):
    root = tmp_path / str(index) / "scheduler"
    root.mkdir(parents=True)
    repository = scheduler.SchedulerRepository(
        root, owner_provider="test", owner_kind="user", owner_id=str(index),
    )
    run = {"schema_version": "scheduler_run.v1", "owner": repository.owner,
           "run_id": "srun_1", "job_id": "job_1", "thread_id": "thread-1",
           "status": "waiting", "attempt": 1, "scheduled_for": 1000.0,
           "waiting_since": 1000.0, "updated_at": 1000.0}
    repository.store_path.write_text(json.dumps({
        "schema_version": "scheduler_store.v1", "owner": repository.owner,
        "jobs": {}, "runs": {"srun_1": run}, "updated_at": 1000.0,
    }), encoding="utf-8")
    old = time.time_ns() - 10_000_000_000
    os.utime(repository.store_path, ns=(old, old))
    return repository


@pytest.mark.parametrize("kind", ["waiting", "store"])
@pytest.mark.parametrize("owners", [64, 128])
def test_scheduler_owner_rotation_hits_after_warmup(tmp_path, kind, owners):
    _configure(owners=owners)
    repositories = [_scheduler_repository(tmp_path, index) for index in range(owners)]

    def read(repository):
        if kind == "waiting":
            projection = repository._waiting_runs_projection_unlocked(
                scheduler._probe_store_bytes(repository.store_path))
            assert projection.rows[0]["run_id"] == "srun_1" and not projection.errors
        else:
            rows, errors = repository.waiting_runs()
            assert rows[0]["run_id"] == "srun_1" and not errors

    for repository in repositories:
        read(repository)
    stats = (scheduler._WAITING_PROJECTION_STATS if kind == "waiting"
             else scheduler._STORE_PROJECTION_STATS)
    before = dict(stats)
    for _ in range(3):
        for repository in repositories:
            read(repository)
    hits = sum(stats.get(key, 0) - before.get(key, 0) for key in ("hit", "cheap_hit"))
    assert hits / (3 * owners) >= 0.99
    assert stats["miss"] == before["miss"]


def test_unconfigured_capacity_keeps_every_original_floor():
    for floor in (scheduler._WAITING_PROJECTION_MAX_ENTRIES_COUNT,
                  scheduler._STORE_PROJECTION_MAX_ENTRIES_COUNT, quota._QUOTA_SCAN_CACHE_SIZE,
                  quota._QUOTA_ROOT_STAMP_CACHE_SIZE, quota._DISPLAY_USAGE_CACHE_MAX_ENTRIES,
                  plugin_registration._PLUGIN_CACHE_MAX_ENTRIES):
        assert cache_capacity.resident_owners(floor) == floor
    assert cache_capacity.global_inflight(request_worker._PENDING_SCAN_CACHE_MAX_ENTRIES) == (
        request_worker._PENDING_SCAN_CACHE_MAX_ENTRIES)


@pytest.mark.parametrize("owners", [0, 4])
def test_quota_auxiliary_caches_follow_owner_capacity(tmp_path, monkeypatch, owners):
    monkeypatch.setattr(quota, "_QUOTA_ROOT_STAMP_CACHE_SIZE", 2)
    monkeypatch.setattr(quota, "_DISPLAY_USAGE_CACHE_MAX_ENTRIES", 2)
    _configure(owners=owners)
    capacity = max(2, owners)
    roots = []
    for index in range(capacity + 1):
        root = tmp_path / str(index)
        root.mkdir()
        roots.append(root)
        quota._write_quota_ledger(root, quota._read_quota_ledger(root))
        quota._remember_quota_finite_root(root)
        assert quota._directory_usage_bytes(root, root, [], cached=True) == 0
    assert list(quota._QUOTA_ROOT_STAMPS) == [str(root) for root in roots[1:]]
    assert list(quota._QUOTA_FINITE_ROOTS) == roots[1:]
    assert list(quota._DISPLAY_USAGE_CACHE) == roots[1:]


@pytest.mark.parametrize("inflight", [0, 4])
def test_pending_scan_entry_limit_follows_global_inflight(tmp_path, monkeypatch, inflight):
    monkeypatch.setattr(request_worker, "_PENDING_SCAN_CACHE_MAX_ENTRIES", 2)
    monkeypatch.setattr(request_worker, "_PENDING_SCAN_CACHE_MAX_BYTES", 1024)
    _configure(inflight=inflight)
    capacity = max(2, inflight)
    for index in range(capacity + 1):
        path = tmp_path / f"request-{index}.json"
        payload = {"request_id": str(index)}
        path.write_text(json.dumps(payload), encoding="utf-8")
        request_worker._cache_verified_pending_payload(path, payload)
    assert len(request_worker._PENDING_SCAN_CACHE) == capacity
    assert [key[0] for key in request_worker._PENDING_SCAN_CACHE] == [
        str(tmp_path / f"request-{index}.json") for index in range(1, capacity + 1)]


@pytest.mark.parametrize("inflight", [0, 4])
def test_pending_scan_byte_budget_scales_with_the_entry_limit(tmp_path, monkeypatch, inflight):
    payload = {"prompt": "x" * 20}
    raw = json.dumps(payload)
    monkeypatch.setattr(request_worker, "_PENDING_SCAN_CACHE_MAX_ENTRIES", 2)
    monkeypatch.setattr(request_worker, "_PENDING_SCAN_CACHE_MAX_BYTES", len(raw))
    _configure(inflight=inflight)
    for index in range(3):
        path = tmp_path / f"request-{index}.json"
        path.write_text(raw, encoding="utf-8")
        request_worker._cache_verified_pending_payload(path, payload)
    expected = 2 if inflight else 1
    assert len(request_worker._PENDING_SCAN_CACHE) == expected
    assert sum(len(key[-1]) for key in request_worker._PENDING_SCAN_CACHE) == expected * len(raw)


@pytest.mark.parametrize("owners", [0, 4])
def test_plugin_snapshots_are_owner_scoped_and_follow_capacity(tmp_path, monkeypatch, owners):
    monkeypatch.setattr(plugin_registration, "_PLUGIN_CACHE_MAX_ENTRIES", 2)
    _configure(owners=owners)
    homes, reads = [], []
    original = plugin_registration.PluginInstallStore.snapshot

    def counted(store):
        reads.append(1)
        return original(store)

    monkeypatch.setattr(plugin_registration.PluginInstallStore, "snapshot", counted)
    for index in range(max(2, owners) + 1):
        owner = resolve_owner_home(tmp_path, OwnerIdentity.provider_user("test", str(index)))
        owner.plugins_dir.mkdir(parents=True)
        (owner.plugins_dir / "installations.json").write_text(
            encode_installation_state((), owner.identity, "null"), encoding="utf-8")
        homes.append(owner)
        assert plugin_registration._discover_plugin_manifests(owner)[0] == ()
    before = len(reads)
    for owner in homes[1:]:
        assert plugin_registration._discover_plugin_manifests(owner)[0] == ()
    assert len(reads) == before
    assert len(plugin_registration._INSTALLATION_SNAPSHOTS) == max(2, owners)
    plugin_registration._discover_plugin_manifests(homes[0])
    assert len(reads) == before + 1
