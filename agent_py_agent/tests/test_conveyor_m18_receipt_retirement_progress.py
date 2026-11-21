from __future__ import annotations

import hashlib
import os
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import control_operation_service as ops
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.io import fcntl
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import (
    bind_coordinator_generation,
    coordinator_guard,
)
from agent_py_agent.agent.gateway_parts.multiprocess_executor_identity import process_birth
from agent_py_agent.agent.gateway_parts.multiprocess_generation_metadata import (
    generation_metadata_parts,
)
from agent_py_agent.agent.gateway_parts.multiprocess_records import serialize_record
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.standalone_lifecycle import standalone_lifecycle_guard
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

DAY = 86400
NOW = 20 * DAY
_IDENTITY = {"schema_version": "multiprocess_process_identity.v1", "host_id": "fixture",
             "pid": 999999990, "start_time": "fixture-birth"}


def _receipt(paths, name, *, state="completed", updated_at=DAY):
    owner = OwnerIdentity("local", "user", "main")
    scope = GatewayControlScope("main", "local", "chat", {"message_id": name}, resolved_owner=owner)
    operation, digest = ops.gateway_control_operation_identity(scope, "/stop")
    receipt = ops._new_control_operation_receipt(scope, "stop", "/stop", operation, digest)
    receipt = replace(receipt, state=state, updated_at=updated_at,
                      result={"kind": "stop", "ok": True, "message": "done"} if state == "completed" else {})
    path = ops.gateway_control_operation_path(paths, operation)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 走生产回执写者，读回同样走生产读者，保证退役对象与真实产物同形。
    ops._write_control_operation_json(path, receipt.to_dict())
    assert ops.read_gateway_control_operation(paths, operation) == receipt
    return path


def _registry(c_run_id, identity):
    return {"schema_version": "multiprocess_worker_registry.v1", "c_run_id": c_run_id, "worker_count": 1,
            "process_identity": identity, "http_port": 1, "workers": [
                {"worker": 0, "instance_id": f"{c_run_id}-w0", "process_identity": {**_IDENTITY, "pid": 999999991},
                 "executor_ledger_ref": "workers/executors/w0"}]}


def _generation(paths, c_run_id, *, refs=(), sealed=True):
    closure = None
    if sealed:
        registry = _registry(c_run_id, _IDENTITY)
        closure = {"registry": registry, "registry_sha256": hashlib.sha256(serialize_record(registry)).hexdigest(),
                   "role_markers": {"gateway_state.json": "a" * 64, "workers/w0.json": "b" * 64},
                   "source_files": {f"control_operations/{path.name}": hashlib.sha256(path.read_bytes()).hexdigest()
                                    for path in refs},
                   "tree_evidence": {}}
    target = paths.root.joinpath(*generation_metadata_parts(c_run_id))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(serialize_record({"schema_version": "multiprocess_c_generation.v2", "c_run_id": c_run_id,
                                         "worker_count": 1, "closure": closure}))
    return target


def _serve(paths, c_run_id):
    """Bind the held lease and publish its registry plus the initial unsealed v2 record, as startup does."""
    bind_coordinator_generation(paths.root, 1, c_run_id)
    identity = {**_IDENTITY, "pid": os.getpid(), "start_time": process_birth(os.getpid())}
    registry = paths.root / "workers" / "registry.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_bytes(serialize_record(_registry(c_run_id, identity)))
    _generation(paths, c_run_id, sealed=False)


def _prune(paths, **budget):
    return ops.prune_terminal_control_operations(paths, now=NOW, retention_days=7, **budget)


@pytest.mark.skipif(fcntl is None, reason="needs the descriptor-based reference reader")
@pytest.mark.parametrize("clock", ["wall", "stepped"])
def test_retirement_progresses_beyond_one_page_of_closed_generations(tmp_path, monkeypatch, clock):
    paths = gateway_paths_from_root(tmp_path.resolve())
    expired = _receipt(paths, "expired")
    protected = _receipt(paths, "protected")
    for index in range(129):
        _generation(paths, f"gen-{index:03d}", refs=(protected,) if index == 64 else ())
    if clock == "stepped":
        # Each clock read advances 2 ms, so the 25 ms slice stops in the middle of a generation page.
        ticks = SimpleNamespace(value=0.0)

        def monotonic():
            ticks.value += 0.002
            return ticks.value

        monkeypatch.setattr(ops, "time", SimpleNamespace(monotonic=monotonic))
    results = []
    for _ in range(200):
        results.append(_prune(paths))
        if not expired.exists():
            break
    assert results[0]["budget_exhausted"] and results[0]["examined"] == 0
    assert not expired.exists() and protected.exists()
    assert sum(result["kept_referenced"] for result in results) >= 1


@pytest.mark.skipif(fcntl is None, reason="needs the descriptor-based reference reader")
def test_reference_published_between_passes_retains_its_receipt(tmp_path):
    paths = gateway_paths_from_root(tmp_path.resolve())
    expired = _receipt(paths, "expired")
    late = _receipt(paths, "late-reference")
    for index in range(129):
        _generation(paths, f"gen-{index:03d}")
    first = _prune(paths)
    assert first["budget_exhausted"] and first["examined"] == 0
    _generation(paths, "late-c", refs=(late,))
    for _ in range(50):
        _prune(paths)
        if not expired.exists():
            break
    assert not expired.exists() and late.exists()
    assert _prune(paths)["kept_referenced"] == 1 and late.exists()


@pytest.mark.skipif(fcntl is None, reason="needs the descriptor-based reference reader")
def test_reference_published_before_unlink_keeps_the_receipt(tmp_path, monkeypatch):
    paths = gateway_paths_from_root(tmp_path.resolve())
    racing = _receipt(paths, "racing")
    _generation(paths, "old-c")
    original = ops._control_retention_raw
    reads = []

    def read(root, name):
        payload = original(root, name)
        reads.append(name)
        if len(reads) == 2:
            # The locked re-read is the last step before the final decision; a new closure lands here.
            _generation(paths, "late-c", refs=(racing,))
        return payload

    monkeypatch.setattr(ops, "_control_retention_raw", read)
    result = _prune(paths, max_ms=500)
    assert len(reads) == 2 and result["removed"] == 0 and racing.exists()
    monkeypatch.setattr(ops, "_control_retention_raw", original)
    again = _prune(paths, max_ms=500)
    assert again["kept_referenced"] == 1 and again["removed"] == 0 and racing.exists()


@pytest.mark.skipif(fcntl is None, reason="needs the coordinator file lease")
def test_serving_coordinator_retires_until_drain_admission(tmp_path):
    paths = gateway_paths_from_root(tmp_path.resolve())
    before_drain = _receipt(paths, "before-drain")
    with coordinator_guard(paths.root, 1) as lease:
        _serve(paths, "serving-c")
        result = _prune(paths, max_ms=500)
        assert result["removed"] == 1 and not before_drain.exists()

        # An unsealed record that is not the serving generation is ambiguous: keep every receipt.
        crashed = _generation(paths, "crashed-c", sealed=False)
        ambiguous = _receipt(paths, "ambiguous")
        assert _prune(paths, max_ms=500)["kept_referenced"] == 1 and ambiguous.exists()
        crashed.unlink()
        assert _prune(paths, max_ms=500)["removed"] == 1 and not ambiguous.exists()

        from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import (
            admit_coordinator_drain,
        )

        after_drain = _receipt(paths, "after-drain")
        admit_coordinator_drain(lease)
        assert _prune(paths, max_ms=500) == dict(examined=0, removed=0, kept_nonterminal=0,
                                                 kept_referenced=0, budget_exhausted=False)
        assert after_drain.exists()


@pytest.mark.skipif(fcntl is None, reason="needs the coordinator file lease")
def test_drain_admitted_during_final_check_keeps_the_receipt(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import admit_coordinator_drain

    paths = gateway_paths_from_root(tmp_path.resolve())
    racing = _receipt(paths, "racing")
    with coordinator_guard(paths.root, 1) as lease:
        _serve(paths, "serving-c")
        original = ops._control_retention_raw
        reads = []

        def read(root, name):
            reads.append(name)
            if len(reads) == 2:
                admit_coordinator_drain(lease)
            return original(root, name)

        monkeypatch.setattr(ops, "_control_retention_raw", read)
        result = _prune(paths, max_ms=500)
    assert len(reads) == 2 and result["removed"] == 0 and racing.exists()


@pytest.mark.skipif(fcntl is None, reason="needs the coordinator file lease")
def test_drain_admission_waits_for_an_inflight_retirement(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import admit_coordinator_drain

    paths = gateway_paths_from_root(tmp_path.resolve())
    racing = _receipt(paths, "racing")
    with coordinator_guard(paths.root, 1) as lease:
        _serve(paths, "serving-c")
        captured, waited = [], []

        def drain():
            admit_coordinator_drain(lease)
            captured.append(sorted(path.name for path in racing.parent.iterdir()))

        admission = threading.Thread(target=drain)
        original = ops._control_reference_identity
        calls = []

        def identity(root, held):
            calls.append(held)
            if len(calls) == 2:
                # The second identity check runs inside the final lifecycle window, before the unlink.
                admission.start()
                admission.join(0.2)
                waited.append(admission.is_alive())
            return original(root, held)

        monkeypatch.setattr(ops, "_control_reference_identity", identity)
        result = _prune(paths, max_ms=5000)
        admission.join(5)
    assert result["removed"] == 1 and not racing.exists()
    assert waited == [True] and captured and racing.name not in captured[0]


@pytest.mark.skipif(fcntl is None, reason="needs the coordinator file lease")
def test_stopping_producers_ends_serving_retirement(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.control_plane import GatewayControlPlane
    from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime
    from agent_py_agent.agent.gateway_parts.multiprocess_takeover_runtime import (
        prepare_coordinator_takeover,
    )
    from agent_py_agent.agent.settings import AgentConfig

    home = tmp_path / "home"
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    paths = gateway_paths_from_root(tmp_path.resolve() / "gateway")
    with coordinator_guard(paths.root, 1):
        prepare_coordinator_takeover(paths, 1, "fixture-c")
        control = GatewayControlPlane(AgentConfig(my_agent_home=str(home)), home, paths, 1, "fixture-c",
                                      agent_root=tmp_path, workspace_roots=(tmp_path,))
        runtime = GatewayDrainRuntime(control)
        _serve(paths, "fixture-c")
        serving = _receipt(paths, "serving")
        assert _prune(paths, max_ms=500)["removed"] == 1 and not serving.exists()
        runtime.stop_producers()
        stopped = _receipt(paths, "stopped")
        assert _prune(paths, max_ms=500)["removed"] == 0 and stopped.exists()


def test_standalone_service_retires_expired_receipts(tmp_path):
    paths = gateway_paths_from_root(tmp_path.resolve())
    expired = _receipt(paths, "expired")
    recent = _receipt(paths, "recent", updated_at=NOW - DAY)
    pending = _receipt(paths, "pending", state="executing")
    sidecar = expired.with_name(expired.name + ".lock")
    sidecar.touch(mode=0o600)
    with standalone_lifecycle_guard(paths.root):
        result = _prune(paths, max_ms=500)
    assert result == dict(examined=3, removed=1, kept_nonterminal=1, kept_referenced=0, budget_exhausted=False)
    assert not expired.exists() and recent.exists() and pending.exists() and sidecar.exists()


@pytest.mark.parametrize("multiprocess_sources", [False, True], ids=["standalone", "multiprocess-sources"])
def test_retirement_without_dir_fd_verifies_paths_and_keeps_ambiguous_objects(tmp_path, monkeypatch,
                                                                              multiprocess_sources):
    monkeypatch.setattr(os, "supports_dir_fd", set())
    paths = gateway_paths_from_root(tmp_path.resolve() / "gateway")
    expired = _receipt(paths, "expired")
    recent = _receipt(paths, "recent", updated_at=NOW - DAY)
    pending = _receipt(paths, "pending", state="executing")
    linked = _receipt(paths, "linked")
    os.link(linked, tmp_path / "second-link.json")
    sidecar = expired.with_name(expired.name + ".lock")
    sidecar.touch(mode=0o600)
    outside = _receipt(gateway_paths_from_root(tmp_path.resolve() / "elsewhere"), "outside")
    alias = expired.parent / outside.name
    if os.name != "nt":
        alias.symlink_to(outside)
    if multiprocess_sources:
        (paths.root / "workers" / "generations").mkdir(parents=True)
    zero = ops.prune_terminal_control_operations(paths, now=NOW, retention_days=0)
    assert zero["examined"] == zero["removed"] == 0 and not list(paths.root.glob("*.lock"))
    with standalone_lifecycle_guard(paths.root):
        result = _prune(paths, max_ms=500)
    assert result["kept_nonterminal"] == 1 and not result["budget_exhausted"]
    assert recent.exists() and pending.exists() and linked.exists() and sidecar.exists() and outside.exists()
    assert os.name == "nt" or alias.is_symlink()
    if multiprocess_sources:
        assert result["removed"] == 0 and result["kept_referenced"] == 1 and expired.exists()
    else:
        assert result["removed"] == 1 and result["kept_referenced"] == 0 and not expired.exists()
    assert ops.read_gateway_control_operation(paths, recent.stem).operation_id == recent.stem


@pytest.mark.skipif(fcntl is None, reason="needs the descriptor-based reference reader")
def test_unreadable_generation_is_retried_without_changing_directory_identity(tmp_path, monkeypatch):
    paths = gateway_paths_from_root(tmp_path.resolve())
    expired = _receipt(paths, "unreferenced")
    protected = _receipt(paths, "referenced")
    source = _generation(paths, "gen-fixture", refs=(protected,))
    read = ops.read_bytes_beneath
    attempts = []

    def read_with_outage(root, parts, **kwargs):
        if parts[-1] == source.name:
            attempts.append(parts)
            if len(attempts) <= 2:
                raise OSError("simulated generation read interruption")
        return read(root, parts, **kwargs)

    monkeypatch.setattr(ops, "read_bytes_beneath", read_with_outage)
    for expected_attempts in (1, 2):
        _prune(paths, max_entries=128, max_ms=500)
        assert expired.exists() and protected.exists()
        assert len(attempts) == expected_attempts
    for _ in range(4):
        _prune(paths, max_entries=128, max_ms=500)
        if not expired.exists():
            break
    assert len(attempts) >= 3
    assert not expired.exists() and protected.exists()
