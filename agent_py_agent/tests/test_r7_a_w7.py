"""目录库存只处理成员身份，真正引用来源保持严格读取与原配置。"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent.common.json_io import write_private_json_file_atomic
from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.memory_store import retention_scan as scan
from agent_py_agent.agent.memory_store.retention_scan_budget import RetentionScanBudget
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space import owner_maintenance
from agent_py_agent.agent.user_space.owner_quota import owner_quota_enforcer_from_policy
from agent_py_agent.tests.test_memory_retention_wave5 import (
    NOW,
    _expired_pin_target,
    old_file,
    runtime,
)


@pytest.fixture(autouse=True)
def retire_projections():
    yield
    with scan._PIN_BUILDS_LOCK:
        remaining = list(scan._PIN_BUILDS.values())
        scan._PIN_BUILDS.clear()
    for snapshot in remaining:
        snapshot.close()


def _settle(service):
    actions, errors = [], []
    for _ in range(512):
        report = service.apply(now=NOW)
        actions.extend(report.actions)
        errors.extend(report.errors)
        if not report.scan_partial:
            return report, actions, errors
    pytest.fail(f"retention did not settle: {report.scan_cursor}")


@pytest.mark.parametrize("quota_mb", [100, 1024])
@pytest.mark.parametrize("shape", ["owner_root", "workspace_member"])
def test_inventory_link_keeps_real_derived_and_fast_lane_gates_open(tmp_path, quota_mb, shape):
    home, _candidates, _service = runtime(tmp_path)
    quota = json.loads(home.owner_quota_json.read_text())
    quota["max_disk_mb"] = quota_mb
    write_private_json_file_atomic(home.owner_quota_json, quota)
    assert owner_quota_enforcer_from_policy(home.owner_home_dir).max_bytes == quota_mb * 1024**2
    target = Path(home.owner_artifacts_dir) / "project"
    target.mkdir()
    sentinel = old_file(target / "keep.txt", "project")
    workspace = home.owner_workspace_dir / "runtime/workspaces/project"
    workspace.mkdir(parents=True)
    link = home.owner_home_dir / "link_vol15" if shape == "owner_root" else workspace / "stray-link"
    link.symlink_to(target, target_is_directory=True)
    old_file(home.owner_cache_dir / "expired.tmp", "cache")
    store = LocalStore(home.owner_home_dir / "local_store/local.db")
    record = store.upsert_record(source_type="subagent_run", source_id="old", title="old", content="old",
                                 metadata={"status": "done"})
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("UPDATE records SET created_at=?,updated_at=?", (1, 1))
    for tick in range(64):
        result = owner_maintenance.run_owner_retention_if_due(home, now=NOW.timestamp() + tick,
            include_global_indexes=False, quota_max_ms=5000)
        if result.retention is not None:
            assert owner_maintenance._retention_deletion_safe(result.retention)
        marker = json.loads((home.owner_data_dir / "maintenance.json").read_text())
        if not marker.get("maintenance_partial") and store.get_record(record.id) is None:
            break
    assert store.get_record(record.id) is None
    assert marker["derived_records_pruned"]["executed"]
    assert marker["workflow_maintenance"].get("reason") != "retention_not_safe"
    assert sentinel.read_text() == "project" and link.is_symlink()


@pytest.mark.parametrize("shape", ["file-link", "ancestor-link", "hardlink", "fifo", "malformed"])
def test_true_pin_sources_keep_the_deletion_gate_closed(tmp_path, shape):
    home, _candidates, service = runtime(tmp_path)
    source = old_file(home.owner_memory_curator_runs_dir / "pins.jsonl", '{"source_refs":["evidence/a"]}\n')
    if shape == "file-link":
        source.unlink()
        outside = old_file(tmp_path / "outside.jsonl", '{}\n')
        source.symlink_to(outside)
    elif shape == "ancestor-link":
        outside = tmp_path / "outside"
        source.parent.rename(outside)
        source.parent.symlink_to(outside, target_is_directory=True)
    elif shape == "hardlink":
        os.link(source, tmp_path / "second-link")
    elif shape == "fifo":
        source.unlink()
        os.mkfifo(source)
    else:
        source.write_text("{bad\n")
    report, _actions, errors = _settle(service)
    assert any(error.error_code == "MEMORY_RETENTION_PIN_SOURCE_UNREADABLE" for error in errors)
    assert not owner_maintenance._retention_deletion_safe(report)
    assert source.exists()


@pytest.mark.parametrize("history", [100, 1000, 10000])
def test_configured_stream_preserves_live_refs_across_historical_source_pages(tmp_path, monkeypatch, history):
    home, _candidates, service = runtime(tmp_path)
    config = AgentConfig(memory_reference_pin_chunk_bytes=4096)
    service.config = config
    assert not service.apply(now=NOW).scan_partial
    kept, control = (_expired_pin_target(home, name) for name in ("kept.jsonl", "control.jsonl"))
    history_path = old_file(home.owner_memory_curator_runs_dir / "history.jsonl", "".join(
        json.dumps({"source_refs": [f"evidence/{row}"]}) + "\n" for row in range(history))
        + json.dumps({"source_refs": [str(kept)]}) + "\n")
    settled = time.time_ns() + 3_000_000_000
    monkeypatch.setattr(scan.reference_pins.time, "time_ns", lambda: settled)
    original, sizes, snapshots = scan._fingerprint_source, [], []
    constructor = scan.reference_pins.ReferencePinSnapshot.__init__

    def configured(self, home, roots, now, builder, *, config=None):
        constructor(self, home, roots, now, builder, config=config)
        snapshots.append((config, self))

    class Stream:
        def __init__(self, raw, path):
            self.raw, self.path = raw, path

        def __getattr__(self, name):
            return getattr(self.raw, name)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.raw.__exit__(*args)

        def read1(self, size):
            if self.path == history_path:
                sizes.append(size)
            return self.raw.read1(size)

    monkeypatch.setattr(scan, "_fingerprint_source", lambda path: Stream(original(path), path))
    monkeypatch.setattr(scan.reference_pins.ReferencePinSnapshot, "__init__", configured)
    report, actions, errors = _settle(service)
    assert not errors and kept.exists() and not control.exists()
    assert not any(action.path == kept and action.status == "deleted" for action in actions)
    assert sizes and max(sizes) <= 4096
    assert snapshots and all(received is config and snapshot.contents["chunk_bytes"] == 4096
                             for received, snapshot in snapshots)
    assert any(isinstance(snapshot, scan._PinProjection) for _, snapshot in snapshots)
    assert any(type(snapshot) is scan.reference_pins.ReferencePinSnapshot for _, snapshot in snapshots)


def test_one_generation_change_cannot_authorize_an_old_projection(tmp_path):
    home, candidates, _service = runtime(tmp_path)
    source = old_file(home.owner_memory_curator_runs_dir / "source.jsonl", '{"source_refs":["evidence/old"]}\n')
    policy, error = scan._load_policy(home.owner_retention_json)
    assert error is None
    context = scan._RetentionScanContext(home, candidates, policy, (), NOW,
        scan_budget=RetentionScanBudget(1, 8388608, float("inf")))
    protection, _ = scan._budgeted_pin_projection(context)
    assert protection is None
    snapshot = scan._PIN_BUILDS[Path(home.owner_home_dir)]
    while source not in snapshot.sources:
        context = replace(context, scan_budget=RetentionScanBudget(1, 8388608, float("inf")), scan_partial=False)
        protection, _ = scan._budgeted_pin_projection(context)
        assert protection is None
    source.write_text('{"source_refs":["evidence/new"]}\n')
    for _ in range(128):
        context = replace(context, scan_budget=RetentionScanBudget(128, 8388608, float("inf")), scan_partial=False)
        protection, _ = scan._budgeted_pin_projection(context)
        if protection is not None:
            break
        assert context.scan_partial
    else:
        pytest.fail("valid new source did not converge")
    assert protection[1]
    assert protection[0].protects(home.owner_home_dir / "evidence/new", [])
    assert not protection[0].protects(home.owner_home_dir / "evidence/old", [])


def test_inventory_scope_uses_lexical_ancestors_without_scanning_roots(tmp_path, monkeypatch):
    home, candidates, _service = runtime(tmp_path)
    policy, error = scan._load_policy(home.owner_retention_json)
    assert error is None
    roots = tuple(home.owner_workspace_dir / f"project-{index}/conversations" for index in range(1000))
    context = scan._RetentionScanContext(home, candidates, policy, roots, NOW)

    class DirectRoots(set):
        def __iter__(self):
            raise AssertionError("inventory lookup scanned all pin roots")

    with closing(scan._PinProjection(context)) as snapshot, monkeypatch.context() as patch:
        snapshot.pin_roots = DirectRoots(snapshot.pin_roots)
        patch.setattr(Path, "resolve", lambda *args, **kwargs: pytest.fail("inventory followed a path"))
        assert not snapshot._pin_related(home.owner_home_dir / "stray-link")
        assert snapshot._pin_related(roots[-1] / "threads/pin.json")
        assert snapshot._pin_related(roots[-1])
