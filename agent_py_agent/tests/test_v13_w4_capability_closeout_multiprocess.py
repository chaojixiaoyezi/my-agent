"""P1-04: real coordinator/worker sources, canonical closeout and restart obligations."""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.request_execution_ports import (
    RequestExecutionOutcomeUncertainError,
)
from agent_py_agent.agent.core import SimpleAgent, configure_canonical_background_contracts
from agent_py_agent.agent.gateway_parts import input_delivery_service as delivery
from agent_py_agent.agent.gateway_parts import multiprocess_mode as mode
from agent_py_agent.agent.gateway_parts.control_plane import GatewayControlPlane
from agent_py_agent.agent.gateway_parts.io import read_json_file
from agent_py_agent.agent.gateway_parts.multiprocess_admission import (
    CoordinatorAdmissionScope,
    PersistentAdmission,
    session_scope_for_request,
)
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_claim import (
    freeze_coordinator_claim,
)
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_execution import (
    coordinator_execution_scope,
)
from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime
from agent_py_agent.agent.gateway_parts.multiprocess_takeover_runtime import (
    prepare_coordinator_takeover,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.gateway_parts.queue_service import claim_request, ensure_gateway_folders
from agent_py_agent.agent.gateway_parts.request_transactions import request_transaction_ports_scope
from agent_py_agent.agent.gateway_parts.request_worker import (
    AdmissionLimits,
    _finish_claimed_gateway_request,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.services.lifecycle import RecordCapabilityRequestParams
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity


@contextmanager
def coordinated(tmp_path, monkeypatch):
    monkeypatch.setattr(mode, "_WORKER_PARTITION", None)
    configure_canonical_background_contracts()
    agent = SimpleAgent(AgentConfig(my_agent_home=str(tmp_path / "home"), model_backend="echo",
                                    gateway_per_user_owner_scoping=False), tmp_path / "workspace")
    paths = gateway_paths(agent)
    ensure_gateway_folders(paths)
    gate = PersistentAdmission(paths, agent, CoordinatorAdmissionScope(2, "closeout-C"))
    with coordinator_guard(paths.root, gate.count):
        controller = prepare_coordinator_takeover(paths, gate.count, gate.c_run_id)
        gate._runtime_takeover, gate._runtime_takeover_required = controller, True
        control = GatewayControlPlane(agent.config, agent.home_paths.root, paths, gate.count, gate.c_run_id,
                                      agent_root=agent.root)
        GatewayDrainRuntime(control).retain_admission(gate)
        request_id = "closeout-request"
        payload = {"id": request_id, "kind": "ask", "goal": "parent turn", "user_id": "local-agent",
                   "metadata": {"channel": "chat", "user_id": "local-agent", "client_input_digest": "closeout-digest"}}
        with delivery.gateway_input_transition(paths, request_id):
            receipt, _ = delivery.load_or_prepare_gateway_input_locked(
                paths, request_id=request_id, client_input_digest="closeout-digest", client_message_id="",
                guidance_dedupe_key="", prepared_request=payload)
            delivery.queue_gateway_input_locked(paths, receipt)
        inbox = paths.inbox / f"{request_id}.json"
        decision = gate.admit(inbox, (OwnerIdentity.local_main(), session_scope_for_request(receipt.prepared_request)),
                              AdmissionLimits(8, 8))
        assert decision["status"] == "admitted", decision
        claim = claim_request(paths, inbox, coordinator_snapshot=freeze_coordinator_claim(gate, decision))
        assert claim is not None
        yield SimpleNamespace(agent=agent, gate=gate, paths=paths, claim=claim)


def _blocked(agent, parent):
    manager = agent.subagents
    task = manager.create_run(goal="exact duty", parent_id=parent, root_id=parent, depth=1, role="worker")
    request = manager.lifecycle.record_capability_request(task.id, RecordCapabilityRequestParams(
        problem="need permission", needed_capability="filesystem", requested_tools=["write_file"]))

    def mark(current):
        current.status = "BLOCKED"
        current.failure_type = "capability_request"
        current.runner_active_attempt_id = ""

    manager.mutate(task.id, mark)
    return task.id, request.id


@pytest.mark.parametrize("status", ["done", "interrupted", "stopped", "cancelled"])
def test_parent_terminal_settles_frozen_child_capability_requests(tmp_path, monkeypatch, status):
    with coordinated(tmp_path, monkeypatch) as case:
        claim = case.claim
        with coordinator_execution_scope(case.gate, claim, case.agent), request_transaction_ports_scope(case.agent):
            child, _ = _blocked(case.agent, claim.request_id)
            _finish_claimed_gateway_request(
                case.paths, claim.path, claim.request_id,
                {"id": claim.request_id, "status": status, "ok": status == "done", "response": "original result"},
                conversation_store=case.agent.conversation_store, execution_agent=case.agent,
                expected_execution_attempt_id=claim.execution_attempt_id, expected_lease_epoch=claim.lease_epoch)
        terminal = read_json_file(case.paths.terminal / claim.path.name)
        assert terminal["status"] == status and terminal["terminal_response"]["response"] == "original result"
        current = case.agent.subagents.load(child)
        assert current.status == "CANCELLED" and current.capability_requests[0].status == "CLOSED"


def _finish(case, status="done"):
    claim = case.claim
    _finish_claimed_gateway_request(
        case.paths, claim.path, claim.request_id,
        {"id": claim.request_id, "status": status, "ok": status == "done", "response": "original result"},
        conversation_store=case.agent.conversation_store, execution_agent=case.agent,
        expected_execution_attempt_id=claim.execution_attempt_id, expected_lease_epoch=claim.lease_epoch)


def _fd_count():
    import os

    return len(os.listdir("/dev/fd"))


@pytest.mark.parametrize("status", ["done", "cancelled"])
def test_release_window_descriptor_balance(tmp_path, monkeypatch, status):
    with coordinated(tmp_path, monkeypatch) as case:
        with coordinator_execution_scope(case.gate, case.claim, case.agent), request_transaction_ports_scope(case.agent):
            child, request_id = _blocked(case.agent, case.claim.request_id)
            before = _fd_count()
            _finish(case, status)
            assert _fd_count() - before == 0
        terminal = read_json_file(case.paths.terminal / case.claim.path.name)
        proof = terminal["capability_closeout"]
        assert proof["acknowledged"] is True and proof["requests"] == {child: [request_id]}


@pytest.mark.parametrize("after_commit", [False, True])
def test_one_child_io_error_reads_canonical_and_retries_in_original_window(tmp_path, monkeypatch, after_commit):
    import errno

    from agent_py_agent.agent.common.request_execution_ports import current_request_execution_source
    from agent_py_agent.agent.subagents.services.persistence import service

    with coordinated(tmp_path, monkeypatch) as case:
        with coordinator_execution_scope(case.gate, case.claim, case.agent), request_transaction_ports_scope(case.agent):
            child, request_id = _blocked(case.agent, case.claim.request_id)
            original = service.write_private_json_file_atomic
            calls, injected = [], []

            def write(path, payload):
                assert not (case.paths.terminal / case.claim.path.name).exists()
                current_request_execution_source("release").require_writer("release")
                frozen = read_json_file(case.claim.path)["capability_closeout"]
                assert frozen["acknowledged"] is False and frozen["requests"] == {child: [request_id]}
                calls.append(str(path))
                if not injected:
                    injected.append(True)
                    if after_commit:
                        original(path, payload)
                    raise OSError(errno.EIO, "transient filesystem write failure")
                return original(path, payload)

            monkeypatch.setattr(service, "write_private_json_file_atomic", write)
            before = _fd_count()
            _finish(case)
            assert _fd_count() - before == 0
        assert len(injected) == 1 and calls
        terminal = read_json_file(case.paths.terminal / case.claim.path.name)
        assert terminal["status"] == "done" and terminal["capability_closeout"]["acknowledged"] is True
        assert case.agent.subagents.load(child).capability_requests[0].status == "CLOSED"


def test_source_validation_rejection_is_not_io_retry(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import agent_activity

    with coordinated(tmp_path, monkeypatch) as case:
        with coordinator_execution_scope(case.gate, case.claim, case.agent), request_transaction_ports_scope(case.agent):
            _blocked(case.agent, case.claim.request_id)
            calls = []

            def rejected(*args, **kwargs):
                calls.append(True)
                raise RequestExecutionOutcomeUncertainError(read_json_file(case.claim.path), "release")

            monkeypatch.setattr(agent_activity, "close_parent_capability_requests", rejected)
            before = _fd_count()
            with pytest.raises(RequestExecutionOutcomeUncertainError):
                _finish(case)
            assert _fd_count() - before == 0 and calls == [True]
        proof = read_json_file(case.claim.path)["capability_closeout"]
        assert proof["acknowledged"] is False and proof["terminal_response"]["response"] == "original result"


def test_actual_worker_issued_source_closes_child_and_preserves_claim(worker):
    from agent_py_agent.tests.test_gateway_active_input_worker import real_worker_case

    with real_worker_case(worker) as (_source, _params, agent, claim, paths, _receipt, _p3):
        child, request_id = _blocked(agent, claim.request_id)
        before = _fd_count()
        _finish(SimpleNamespace(agent=agent, claim=claim, paths=paths))
        assert _fd_count() - before == 0
        terminal = read_json_file(paths.terminal / claim.path.name)
        assert terminal["status"] == "done" and terminal["execution_attempt_id"] == claim.execution_attempt_id
        assert terminal["capability_closeout"]["requests"] == {child: [request_id]}
        assert agent.subagents.load(child).capability_requests[0].status == "CLOSED"


from agent_py_agent.tests.test_gateway_multiprocess_worker_agent import (
    worker as worker,  # noqa: E402
)


def _crash_original_release(root, checkpoint):
    """Only executed in a disposable child process; os._exit bypasses all Python cleanup."""
    import os

    from agent_py_agent.agent.conversation import agent_activity
    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_execution import (
        current_coordinator_execution,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_executor_source import (
        CoordinatorExecutorSource,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_generation_metadata import (
        _publish_generation_once,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_route import owner_digest
    from agent_py_agent.agent.gateway_parts.request_binding import GatewayTaskBindingWriter
    from agent_py_agent.agent.subagents.services.persistence import service

    with pytest.MonkeyPatch.context() as patch, coordinated(root, patch) as case:
        with coordinator_execution_scope(case.gate, case.claim, case.agent), request_transaction_ports_scope(case.agent):
            store = case.agent.conversation_store
            thread = store.threads.get_or_create({"channel": "chat", "channel_conversation_id": "crash-chat",
                "channel_user_id": "local-agent", "canonical_user_id": "local-agent",
                "owner_id": case.agent.home_paths.owner_id, "owner_home": str(case.agent.home_paths.owner_home_dir)})
            binding = case.agent.subagents.runtime_db.record_run_creation(owner_id=case.agent.home_paths.owner_id,
                run_id="crash-parent-run", role="main", thread_id=thread.thread_id)
            writer = GatewayTaskBindingWriter(case.claim.path, case.claim.request_id,
                execution_attempt_id=case.claim.execution_attempt_id)
            assert writer.bind_conversation_claim(thread.thread_id)
            assert writer.bind_runtime_authority({**binding, "run_id": "crash-parent-run", "invocation_run_id": case.claim.request_id})
            _blocked(case.agent, binding["task_id"])
            # A real original C/P1 source reserves then abandons a not-started executor.
            # Recovery therefore observes a genuinely stopped domain without faking a P2 process table.
            # This component fixture has no P4 bootstrap; publish its actual old count via the original P0 producer.
            from agent_py_agent.agent.gateway_parts import daemon_metadata
            _publish_generation_once(case.paths.root, {"schema_version": "multiprocess_c_generation.v1",
                "c_run_id": case.gate.c_run_id, "worker_count": case.gate.count})
            source = CoordinatorExecutorSource(current_coordinator_execution())
            row = source.reserve({"schema_version": "multiprocess_executor_reservation.v1",
                "reservation_id": "crash-original-executor", "kind": "shell", "c_run_id": case.gate.c_run_id,
                "owner_sha": owner_digest(source.owner), "request_id": case.claim.request_id,
                "execution_attempt_id": case.claim.execution_attempt_id, "lease_epoch": case.claim.lease_epoch,
                "tool_call_id": "crash-original-call", "state": "reserved", "process_identity": None,
                "pgid": None, "sid": None})
            identity = {"schema_version": "multiprocess_process_identity.v1",
                        "host_id": daemon_metadata.process_host_id(),
                        "pid": os.getpid(), "start_time": f"controlled:{os.getpid()}"}
            source.advance(row, "registered", {"process_identity": identity, "pgid": os.getpgid(0), "sid": os.getsid(0)})
            write_json_file_atomic(root / "crash-locations.json", {
                "gateway": str(case.paths.root), "children": str(case.agent.subagents.workspace)})
            if checkpoint == "before_child_write":
                def crash(*args, **kwargs):
                    os._exit(71)
                patch.setattr(agent_activity, "close_parent_capability_requests", crash)
            else:
                original = service.write_private_json_file_atomic

                def crash(path, payload):
                    result = original(path, payload)
                    if payload.get("status") == "CANCELLED":
                        os._exit(71)
                    return result
                patch.setattr(service, "write_private_json_file_atomic", crash)
            _finish(case)
    raise AssertionError("crash checkpoint was not reached")


@pytest.mark.parametrize("checkpoint", ["before_child_write", "after_child_commit"])
def test_actual_crash_then_cold_restart_consumes_only_original_obligation(tmp_path, monkeypatch, checkpoint):
    import os
    import subprocess
    import sys
    import time
    from pathlib import Path

    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_for_root
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.gateway_parts.recovery import (
        _RecoveryContext,
        recover_gateway_processing_requests_report,
        repair_gateway_terminal_projections,
    )
    from agent_py_agent.agent.subagents.services.persistence.service import load_subagent_task
    from agent_py_agent.agent.user_space.home_layout import home_paths

    code = ("from pathlib import Path; from agent_py_agent.tests.test_v13_w4_capability_closeout_multiprocess "
            "import _crash_original_release; import sys; _crash_original_release(Path(sys.argv[1]), sys.argv[2])")
    repo_root = Path(__file__).resolve().parents[2]
    environment = dict(os.environ, PYTHONPATH=os.pathsep.join((str(repo_root), str(repo_root / "agent_py_agent"))))
    completed = subprocess.run([sys.executable, "-c", code, str(tmp_path), checkpoint],
        cwd=repo_root, env=environment, capture_output=True, text=True, timeout=45, start_new_session=True)
    assert completed.returncode == 71, completed.stdout + completed.stderr
    config = AgentConfig(my_agent_home=str(tmp_path / "home"), model_backend="echo", gateway_per_user_owner_scoping=False)
    home = home_paths(tmp_path / "home")
    locations = read_json_file(tmp_path / "crash-locations.json")
    paths = gateway_paths_from_root(Path(locations["gateway"]))
    path = paths.processing / "closeout-request.json"
    pending = read_json_file(path)
    proof = pending["capability_closeout"]
    assert proof["acknowledged"] is False and proof["terminal_response"]["response"] == "original result"
    child = next(iter(proof["requests"]))
    workspace = Path(locations["children"])
    assert load_subagent_task(workspace, child).capability_requests[0].status == (
        "OPEN" if checkpoint == "before_child_write" else "CLOSED")

    def forbidden(*args, **kwargs):
        pytest.fail("cold closeout recovery must not construct a business Agent or rerun a model")

    monkeypatch.setattr(SimpleAgent, "__init__", forbidden)
    with coordinator_guard(paths.root, 2):
        controller = prepare_coordinator_takeover(paths, 2, "restart-C")
        control = GatewayControlPlane(config, home.root, paths, 2, "restart-C", agent_root=tmp_path / "workspace")
        gate = PersistentAdmission(paths, control, CoordinatorAdmissionScope(2, "restart-C"))
        gate._runtime_takeover, gate._runtime_takeover_required = controller, True
        GatewayDrainRuntime(control).retain_admission(gate)
        assert coordinator_for_root(paths.root, context=paths) is not None
        context = _RecoveryContext(time.time() + 100, 3, 900, True, control, None, admission_gate=gate)
        before = _fd_count()
        report = recover_gateway_processing_requests_report(paths, params=context)
        assert report.load_errors == [], report.load_errors
        canonical = paths.terminal / path.name
        terminal = read_json_file(canonical)
        assert terminal["status"] == "done" and terminal["terminal_response"] == proof["terminal_response"]
        assert terminal["capability_closeout"]["acknowledged"] is True
        assert load_subagent_task(workspace, child).capability_requests[0].status == "CLOSED"
        original = canonical.read_bytes()
        assert recover_gateway_processing_requests_report(paths, params=context).load_errors == []
        assert repair_gateway_terminal_projections(paths, agent=control, recovery_context=context, full_scan=True)[1] == []
        assert canonical.read_bytes() == original and not path.exists()
        assert _fd_count() - before == 0


def test_child_snapshot_io_failure_keeps_success_and_unconfirmed_proof(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import agent_activity

    with coordinated(tmp_path, monkeypatch) as case:
        with coordinator_execution_scope(case.gate, case.claim, case.agent), request_transaction_ports_scope(case.agent):
            child, _ = _blocked(case.agent, case.claim.request_id)
            calls = []

            def unavailable(*args, **kwargs):
                calls.append(True)
                raise OSError("child snapshot temporarily unreadable")

            monkeypatch.setattr(agent_activity, "parent_capability_request_snapshot", unavailable)
            before = _fd_count()
            _finish(case)
            assert _fd_count() - before == 0
        terminal = read_json_file(case.paths.terminal / case.claim.path.name)
        assert terminal["status"] == "done" and terminal["terminal_response"]["response"] == "original result"
        assert terminal["capability_closeout"]["acknowledged"] is False
        assert terminal["capability_closeout"]["snapshot_pending"] is True and len(calls) == 2
        assert case.agent.subagents.load(child).capability_requests[0].status == "OPEN"


def test_locator_io_failure_retries_even_after_canonical_child_close(tmp_path, monkeypatch):
    from agent_py_agent.agent.subagents.services.persistence import service

    with coordinated(tmp_path, monkeypatch) as case:
        with coordinator_execution_scope(case.gate, case.claim, case.agent), request_transaction_ports_scope(case.agent):
            child, _ = _blocked(case.agent, case.claim.request_id)
            original = service.write_private_json_file_atomic
            locator_writes = []

            def fault(path, payload):
                if payload.get("status_mirror") == "CANCELLED":
                    locator_writes.append(str(path))
                    if len(locator_writes) == 1:
                        assert case.agent.subagents.load(child).status == "CANCELLED"
                        raise OSError("one locator persistence failure")
                return original(path, payload)

            monkeypatch.setattr(service, "write_private_json_file_atomic", fault)
            before = _fd_count()
            _finish(case)
            assert _fd_count() - before == 0
        assert len(locator_writes) > 1
        assert read_json_file(case.paths.terminal / case.claim.path.name)["capability_closeout"]["acknowledged"] is True
