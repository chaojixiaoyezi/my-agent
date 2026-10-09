"""completed 重放只收尾：活/未知/停止未确认拒绝，恢复证据不丢。"""
import json
import subprocess
import sys
from dataclasses import asdict, replace
from datetime import datetime, timedelta

import pytest

from agent_py_agent.agent.memory_store import agents_migration as migration
from agent_py_agent.tests.test_agents_cleanup_review33 import _apply, _setup
from agent_py_agent.tests.test_agents_cleanup_review33b import CHILD, business_bytes, directory


def _pending(tmp_path, monkeypatch):
    env = _setup(tmp_path)
    captured = []
    commit = env.store.commit_success

    def capture(request):
        captured.append(dict(env.store.load().active_lease))
        return commit(request)

    def refuse_record(_path, _payload):
        raise OSError("transient diagnostic storage failure")

    with monkeypatch.context() as patch:
        patch.setattr(env.store, "commit_success", capture)
        patch.setattr(migration, "write_private_json_file_atomic_unlocked", refuse_record)
        assert not _apply(env)["ok"]
    assert not env.store.load().active_lease
    return env, captured[0]


def _record(env):
    return json.loads((directory(env) / "record.json").read_text())


@pytest.mark.parametrize("holder", ["live", "foreign", "unconfirmed", "missing_id"])
def test_completed_reconciliation_rejects_unproven_cleanup_without_writes(tmp_path, monkeypatch, holder):
    env, lease = _pending(tmp_path, monkeypatch)
    if holder in {"live", "foreign"}:
        if holder == "foreign":
            lease = {**lease, "host": "foreign-synthetic-host"}
        env.store._write_unlocked(replace(env.store.load(), active_lease=lease))
    if holder in {"unconfirmed", "missing_id"}:
        record = _record(env)
        if holder == "unconfirmed":
            record["lease_cleanup"] = {"heartbeat_stop_error_class": "KeyboardInterrupt", "released": False}
        else:
            record.pop("cleanup_lease_id")
        migration._write_record(directory(env), record)
    state_before = env.store.path.read_bytes()
    record_before = (directory(env) / "record.json").read_bytes()
    business_before = business_bytes(env)
    replay = _apply(env)
    assert replay["ok"] is False
    assert env.store.path.read_bytes() == state_before
    assert (directory(env) / "record.json").read_bytes() == record_before
    assert business_bytes(env) == business_before


def test_reconcile_read_failure_remains_pending_and_reports_real_error(tmp_path, monkeypatch):
    env, _ = _pending(tmp_path, monkeypatch)
    before = _record(env)
    state_before = env.store.path.read_bytes()
    business_before = business_bytes(env)

    def refuse_read():
        raise OSError("synthetic canonical state read failure")

    monkeypatch.setattr(env.store, "_load_unlocked", refuse_read)
    result = _apply(env)
    assert not result["ok"]
    assert result["lease_cleanup"]["reconcile_error_class"] == "OSError"
    after = _record(env)
    assert after["lease_cleanup_pending"] is True
    assert after["lease_cleanup_history"][-1]["reconcile_error_class"] == "OSError"
    old_fields = {k: v for k, v in before.items() if k not in {"lease_cleanup", "lease_cleanup_pending", "lease_cleanup_history"}}
    assert {k: v for k, v in after.items() if k not in {"lease_cleanup", "lease_cleanup_pending", "lease_cleanup_history"}} == old_fields
    assert env.store.path.read_bytes() == state_before
    assert business_bytes(env) == business_before


def test_completed_reconciliation_retains_history_and_is_durably_idempotent(tmp_path, monkeypatch):
    env, lease = _pending(tmp_path, monkeypatch)
    business_before = business_bytes(env)
    assert _apply(env)["ok"]
    record = _record(env)
    assert not record["lease_cleanup_pending"] and record["lease_cleanup"] == {}
    audit = record["lease_cleanup_history"][-1]
    assert audit["lease_id"] == lease["lease_id"]
    assert audit["previous_pending"] is True
    assert audit["outcome"] == "old_lease_absent"
    assert audit["reconciled_at"]
    record_before = (directory(env) / "record.json").read_bytes()
    assert _apply(env)["ok"]
    assert (directory(env) / "record.json").read_bytes() == record_before
    assert business_bytes(env) == business_before


def test_replaced_lease_reconciliation_preserves_previous_failure_audit(tmp_path, monkeypatch):
    env = _setup(tmp_path)

    def refuse_release(_request):
        raise OSError("transient old release error")

    with monkeypatch.context() as patch:
        patch.setattr(env.store, "commit_success", refuse_release)
        first = _apply(env)
    old = env.store.load().active_lease
    future = datetime.fromisoformat(old["expires_at"]) + timedelta(hours=1)
    assert env.store.acquire(reason="admin", config_revision="new", lease_seconds=300, now=future)
    state_bytes = env.store.path.read_bytes()
    assert _apply(env)["ok"]
    assert env.store.path.read_bytes() == state_bytes
    audit = _record(env)["lease_cleanup_history"][-1]
    assert audit["lease_id"] == old["lease_id"]
    assert audit["previous_diagnostic"] == first["lease_cleanup"]
    assert audit["outcome"] == "old_lease_replaced"


@pytest.mark.parametrize("write_mode", ["normal", "all", "final_only"])
def test_dead_exact_holder_cleanup_preserves_counters_epoch_and_recovers_record_failure(tmp_path, monkeypatch, write_mode):
    env = _setup(tmp_path)
    args = {"owner": str(env.service.persona.owner_home), "candidates": str(env.service.candidates.path),
            "request": asdict(env.request), "stage": "after_completed"}
    child = subprocess.run([sys.executable, "-c", CHILD, json.dumps(args)], capture_output=True, text=True, timeout=20)
    assert child.returncode == 73, (child.stdout, child.stderr)
    state_before = asdict(env.store.load())
    business_before = business_bytes(env)
    epoch = env.service.persona.owner_home / "persona/thread_prefix_epoch.json"
    epoch_before = epoch.read_bytes()

    writer = migration.write_private_json_file_atomic_unlocked

    def refuse_record(path, payload):
        if write_mode == "all" or not payload.get("lease_cleanup_pending"):
            raise OSError("synthetic reconciliation diagnostic failure")
        return writer(path, payload)

    if write_mode != "normal":
        record_before = (directory(env) / "record.json").read_bytes()
        with monkeypatch.context() as patch:
            patch.setattr(migration, "write_private_json_file_atomic_unlocked", refuse_record)
            assert _apply(env)["ok"] is False
        if write_mode == "all":
            assert env.store.load().active_lease["lease_id"] == state_before["active_lease"]["lease_id"]
            assert (directory(env) / "record.json").read_bytes() == record_before
        else:
            assert not env.store.load().active_lease
            history = _record(env)["lease_cleanup_history"]
            assert any(row["outcome"] == "dead_holder_confirmed" for row in history)
            assert history[-1]["reconcile_error_class"] == "OSError"
    assert _apply(env)["ok"]
    state_after = asdict(env.store.load())
    assert not state_after.pop("active_lease")
    state_before.pop("active_lease")
    assert state_after == state_before
    assert business_bytes(env) == business_before
    assert epoch.read_bytes() == epoch_before


@pytest.mark.parametrize("detail", ["invalid", ["invalid"], True])
def test_malformed_cleanup_diagnostic_fails_closed_without_raw_type_error(tmp_path, monkeypatch, detail):
    env, _ = _pending(tmp_path, monkeypatch)
    record = _record(env)
    record["lease_cleanup"] = detail
    migration._write_record(directory(env), record)
    before = (directory(env) / "record.json").read_bytes()
    report = _apply(env)
    assert report["ok"] is False
    assert report["lease_cleanup"]["record_error_class"] == "invalid_cleanup_diagnostic"
    assert (directory(env) / "record.json").read_bytes() == before


@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("detail", [[], "", 0, None])
def test_falsey_non_dict_diagnostic_cannot_be_success(tmp_path, monkeypatch, pending, detail):
    env, _ = _pending(tmp_path, monkeypatch)
    record = _record(env)
    record.update(lease_cleanup=detail, lease_cleanup_pending=pending)
    migration._write_record(directory(env), record)
    before = (directory(env) / "record.json").read_bytes()
    report = _apply(env)
    assert report["ok"] is False
    assert report["lease_cleanup"]["record_error_class"] == "invalid_cleanup_diagnostic"
    assert (directory(env) / "record.json").read_bytes() == before


@pytest.mark.parametrize("field", ["migration_id", "cleanup_lease_id"])
def test_record_identity_swap_between_initial_and_locked_read_is_refused(tmp_path, monkeypatch, field):
    from agent_py_agent.agent.memory_store import agents_migration_reconcile as reconciliation

    env, _ = _pending(tmp_path, monkeypatch)
    original = reconciliation._reconcile_locked
    swapped = _record(env)
    swapped[field] += "-replacement"
    observed = []
    state_bytes = env.store.path.read_bytes()

    def replace_before_locked_read(*args):
        migration._write_record(directory(env), swapped)
        observed.append((directory(env) / "record.json").read_bytes())
        return original(*args)

    monkeypatch.setattr(reconciliation, "_reconcile_locked", replace_before_locked_read)
    assert _apply(env)["ok"] is False
    assert (directory(env) / "record.json").read_bytes() == observed[0]
    assert env.store.path.read_bytes() == state_bytes


def test_late_failure_audit_cannot_modify_replaced_migration_record(tmp_path, monkeypatch):
    env, _ = _pending(tmp_path, monkeypatch)
    swapped = _record(env)
    swapped["migration_id"] += "-replacement"
    observed = []

    def read_failure_after_record_swap():
        migration.write_private_json_file_atomic_unlocked(directory(env) / "record.json", swapped)
        observed.append((directory(env) / "record.json").read_bytes())
        raise OSError("read failed after a newer migration record")

    monkeypatch.setattr(env.store, "_load_unlocked", read_failure_after_record_swap)
    assert _apply(env)["ok"] is False
    assert (directory(env) / "record.json").read_bytes() == observed[0]
