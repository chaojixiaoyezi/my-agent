"""接管关键边界：旧清单不能授权、清理仍持目标锁、无模型调用不能归为模型失败。"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_py_agent.agent.common.json_io import locked_json_path
from agent_py_agent.agent.memory_store import curator_commit as cc
from agent_py_agent.agent.memory_store.curator_run_log import (
    load_run_records_unlocked,
    run_log_path,
)
from agent_py_agent.tests import test_review18_contracts as original


def _locked_in_other_thread(path):
    def probe():
        try:
            with locked_json_path(Path(path), blocking=False):
                return False
        except BlockingIOError:
            return True
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(probe).result(timeout=5)


def test_cleanup_keeps_state_and_targets_locked(tmp_path, monkeypatch):
    service, _, _, manifest = original.crashed_case(tmp_path, 1)
    payload = cc._load_manifest(manifest, service.committer.memory_root)
    cleanup = cc._cleanup_transaction
    seen = []

    def inspecting(directory):
        held = [_locked_in_other_thread(item["path"]) for item in payload["targets"]]
        seen.append(held)
        return cleanup(directory)

    monkeypatch.setattr(cc, "_cleanup_transaction", inspecting)
    assert service.run(reason="admin").status == "succeeded"
    assert seen and all(seen[0])


def test_manifest_changed_after_discovery_is_not_used(tmp_path, monkeypatch):
    service, _, _, manifest = original.crashed_case(tmp_path, 1)
    load = cc._load_manifest

    def changed(path, memory_root):
        payload = load(path, memory_root)
        path.write_text(json.dumps({**payload, "targets": []}))
        return payload

    monkeypatch.setattr(cc, "_load_manifest", changed)
    result = service.run(reason="admin")
    assert result.failure_code == "CURATOR_COMMIT_RECOVERY_FAILED"
    assert service.backend.calls == 0
    assert manifest.exists()


def test_genuine_cleanup_failure_is_not_a_model_failure(tmp_path, monkeypatch):
    service, _, _, manifest = original.crashed_case(tmp_path, 1)

    def failed(directory):
        raise FileNotFoundError("fixture cleanup failure")

    monkeypatch.setattr(cc, "_cleanup_transaction", failed)
    result = service.run(reason="admin")
    assert result.failure_code == "CURATOR_COMMIT_RECOVERY_FAILED"
    assert service.backend.calls == 0
    assert manifest.exists()


def test_audit_and_acquire_keep_the_complete_lock_set(tmp_path, monkeypatch):
    service, _, _, manifest = original.crashed_case(tmp_path, 1)
    targets = [Path(item["path"]) for item in cc._load_manifest(manifest, service.committer.memory_root)["targets"]]
    append, acquire = service.run_log._append_unlocked, service.state_store._acquire_unlocked
    observations = []
    audits = []

    def auditing(record):
        if record.status == "recovered_rollback":
            path = run_log_path(service.run_log.runs_dir, record.finished_at)
            audits.append(path)
            observations.append(("audit", all(_locked_in_other_thread(p) for p in [*targets, path])))
        return append(record)

    def acquiring(request):
        observations.append(("acquire", all(_locked_in_other_thread(p) for p in [*targets, *audits])))
        assert not manifest.exists()
        assert len(load_run_records_unlocked(audits[0])) == 1
        return acquire(request)

    monkeypatch.setattr(service.run_log, "_append_unlocked", auditing)
    monkeypatch.setattr(service.state_store, "_acquire_unlocked", acquiring)
    assert service.run(reason="admin").status == "succeeded"
    assert observations == [("audit", True), ("acquire", True)]


def test_partial_nonblocking_lock_failure_releases_earlier_locks(tmp_path):
    first, last = tmp_path / "a.json", tmp_path / "z.json"
    def contend():
        with cc._locked_paths([first, last], blocking=False):
            pytest.fail("the busy second lock must prevent entry")
    with ThreadPoolExecutor(max_workers=1) as pool:
        with locked_json_path(last):
            error = pool.submit(contend).exception(timeout=5)
            assert isinstance(error, BlockingIOError)
        assert _locked_in_other_thread(first) is False
