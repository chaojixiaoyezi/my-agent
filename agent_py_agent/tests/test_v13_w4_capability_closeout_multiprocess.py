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
def test_release_window_descriptor_balance(tmp_path, monkeypatch, status, record_property):
    with coordinated(tmp_path, monkeypatch) as case:
        with coordinator_execution_scope(case.gate, case.claim, case.agent), request_transaction_ports_scope(case.agent):
            child, request_id = _blocked(case.agent, case.claim.request_id)
            before = _fd_count()
            _finish(case, status)
            delta = _fd_count() - before
            record_property("fd_delta", delta)
            assert delta == 0
        terminal = read_json_file(case.paths.terminal / case.claim.path.name)
        proof = terminal["capability_closeout"]
        assert proof["acknowledged"] is True and proof["requests"] == {child: [request_id]}


@pytest.mark.parametrize("after_commit", [False, True])
def test_one_child_io_error_reads_canonical_and_retries_in_original_window(tmp_path, monkeypatch, after_commit, record_property):
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
            delta = _fd_count() - before
            record_property("fd_delta", delta)
            assert delta == 0
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
            if checkpoint == "after_pending_proof":
                from agent_py_agent.agent.gateway_parts import request_worker

                original = request_worker.write_json_file_atomic

                def crash(path, payload):
                    original(path, payload)
                    proof = payload.get("capability_closeout", {})
                    if path == case.claim.path and proof.get("snapshot_pending") is True:
                        assert proof["requests"] == {} and proof["selection_boundary"] is not None
                        os._exit(71)
                patch.setattr(request_worker, "write_json_file_atomic", crash)
            elif checkpoint == "before_child_write":
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


@pytest.mark.parametrize("checkpoint,inject_io", [("after_pending_proof", False), ("after_pending_proof", True),
    ("before_child_write", False), ("after_child_commit", False)])
def test_actual_crash_then_cold_restart_consumes_only_original_obligation(tmp_path, monkeypatch, checkpoint, inject_io):
    import os
    import subprocess
    import sys
    from pathlib import Path

    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.subagents.services.persistence.service import load_subagent_task

    # 只为并发晚到输入保留原真实fixture Agent；恢复本身在另一个全新进程，禁止构造Agent。
    monkeypatch.setattr(mode, "_WORKER_PARTITION", None)
    configure_canonical_background_contracts()
    fixture_agent = SimpleAgent(AgentConfig(my_agent_home=str(tmp_path / "home"), model_backend="echo",
        gateway_per_user_owner_scoping=False), tmp_path / "workspace")
    code = ("from pathlib import Path; from agent_py_agent.tests.test_v13_w4_capability_closeout_multiprocess "
            "import _crash_original_release; import sys; _crash_original_release(Path(sys.argv[1]), sys.argv[2])")
    repo_root = Path(__file__).resolve().parents[2]
    environment = dict(os.environ, PYTHONPATH=os.pathsep.join((str(repo_root), str(repo_root / "agent_py_agent"))))
    completed = subprocess.run([sys.executable, "-c", code, str(tmp_path), checkpoint],
        cwd=repo_root, env=environment, capture_output=True, text=True, timeout=45, start_new_session=True)
    assert completed.returncode == 71, completed.stdout + completed.stderr
    locations = read_json_file(tmp_path / "crash-locations.json")
    paths = gateway_paths_from_root(Path(locations["gateway"]))
    path = paths.processing / "closeout-request.json"
    pending = read_json_file(path)
    proof = pending["capability_closeout"]
    assert proof["acknowledged"] is False and proof["terminal_response"]["response"] == "original result"
    child = next(iter(proof["selection_boundary"]["children"]))
    workspace = Path(locations["children"])
    assert load_subagent_task(workspace, child).capability_requests[0].status == (
        "CLOSED" if checkpoint == "after_child_commit" else "OPEN")
    selected_id = load_subagent_task(workspace, child).capability_requests[0].id
    later = None
    if checkpoint != "after_child_commit":
        later = fixture_agent.subagents.lifecycle.record_capability_request(child, RecordCapabilityRequestParams(
            problem="late same child", needed_capability="network", requested_tools=["web_fetch"]))
    later_child, _ = _blocked(fixture_agent, proof["parent_task_id"])
    foreign, _ = _blocked(fixture_agent, proof["parent_task_id"])
    fixture_agent.subagents.mutate(foreign, lambda task: setattr(task, "owner", "providers/test/users/other"))
    untouched = {run: fixture_agent.subagents.load(run) for run in (later_child, foreign)}
    cold = ("from pathlib import Path; from agent_py_agent.tests.test_v13_w4_capability_closeout_multiprocess "
            "import _cold_native_resume; import sys; _cold_native_resume(Path(sys.argv[1]), sys.argv[2] == '1')")
    resumed = subprocess.run([sys.executable, "-c", cold, str(tmp_path), str(int(inject_io))],
        cwd=repo_root, env=environment, capture_output=True, text=True, timeout=45, start_new_session=True)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    terminal = read_json_file(paths.terminal / path.name)
    assert terminal["status"] == "done" and terminal["terminal_response"] == proof["terminal_response"]
    assert terminal["capability_closeout"]["acknowledged"] is True
    assert terminal["capability_closeout"]["requests"] == {child: [selected_id]}
    assert load_subagent_task(workspace, child).capability_requests[0].status == "CLOSED"
    if later is not None:
        assert {r.id: r.status for r in load_subagent_task(workspace, child).capability_requests}[later.id] == "OPEN"
    assert {run: fixture_agent.subagents.load(run) for run in untouched} == untouched
    outcome = read_json_file(tmp_path / "native-recovery.json")
    assert outcome["fd_delta"] == 0 and len(outcome["io_errors"]) == int(inject_io)


# LLM: 真正fresh冷C只用ControlPlane/原P0/P1/P2/P3及恢复ports，不造Agent、进程身份或P2成功字典；前置不支持就真实失败。
# 函数用途: 在独立进程消费同一原claim的pending证明，并保存真实恢复报告和FD观察。
def _cold_native_resume(root, inject_io):
    import errno
    import time
    from dataclasses import asdict
    from pathlib import Path

    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_for_root
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.gateway_parts.recovery import (
        _RecoveryContext,
        recover_gateway_processing_requests_report,
        repair_gateway_terminal_projections,
    )
    from agent_py_agent.agent.subagents.services.persistence import service
    from agent_py_agent.agent.user_space.home_layout import home_paths

    def forbidden(*args, **kwargs):
        raise AssertionError("cold closeout recovery must not construct a business Agent or rerun a model")

    SimpleAgent.__init__ = forbidden
    config = AgentConfig(my_agent_home=str(root / "home"), model_backend="echo", gateway_per_user_owner_scoping=False)
    home = home_paths(root / "home")
    locations = read_json_file(root / "crash-locations.json")
    paths = gateway_paths_from_root(Path(locations["gateway"]))
    path = paths.processing / "closeout-request.json"
    proof = read_json_file(path)["capability_closeout"]
    original_writer = service.write_private_json_file_atomic
    errors = []

    def write(path, payload):
        if (inject_io and not errors and path.name == "canonical_state.json"
                and any(r["status"] == "CLOSED" for r in payload.get("capability_requests", []))):
            errors.append(errno.EIO)
            raise OSError(errno.EIO, "one ordinary canonical I/O error")
        return original_writer(path, payload)

    service.write_private_json_file_atomic = write
    with coordinator_guard(paths.root, 2):
        controller = prepare_coordinator_takeover(paths, 2, "restart-C")
        control = GatewayControlPlane(config, home.root, paths, 2, "restart-C", agent_root=root / "workspace")
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
        original = canonical.read_bytes()
        assert recover_gateway_processing_requests_report(paths, params=context).load_errors == []
        assert repair_gateway_terminal_projections(paths, agent=control, recovery_context=context, full_scan=True)[1] == []
        assert canonical.read_bytes() == original and not path.exists()
        delta = _fd_count() - before
        assert delta == 0 and len(errors) == int(inject_io), {"fd_delta": delta, "io_errors": errors}
        write_json_file_atomic(root / "native-recovery.json", {"report": asdict(report), "fd_delta": delta, "io_errors": errors})


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


# LLM: 真正的C/claim/ports保持原样；只注入两次普通读取错误，重进必须复用首份证明的边界，不扩大到晚到申请。
# 函数用途: 复现pending快照不能重试，并验证原孩子新增申请也不会被旧收尾误关。
def test_live_pending_snapshot_resumes_only_pre_boundary_requests(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import agent_activity
    from agent_py_agent.agent.gateway_parts.request_worker import _close_claimed_parent_capabilities

    with coordinated(tmp_path, monkeypatch) as case:
        claim = case.claim
        response = {"id": claim.request_id, "status": "done", "ok": True, "response": "original result"}
        with coordinator_execution_scope(case.gate, claim, case.agent), request_transaction_ports_scope(case.agent):
            child, original_request = _blocked(case.agent, claim.request_id)

            def close():
                _close_claimed_parent_capabilities(case.paths, claim.path, claim.request_id, response,
                                                  case.agent, claim.execution_attempt_id, claim.lease_epoch)

            with monkeypatch.context() as patch:
                def unavailable(*args, **kwargs):
                    raise OSError("one snapshot read is unavailable")
                patch.setattr(agent_activity, "parent_capability_request_snapshot", unavailable)
                close()
            pending = read_json_file(claim.path)["capability_closeout"]
            assert pending["snapshot_pending"] is True and pending["acknowledged"] is False
            later = case.agent.subagents.lifecycle.record_capability_request(child, RecordCapabilityRequestParams(
                problem="late independent request", needed_capability="network", requested_tools=["web_fetch"]))
            late_child, late_request = _blocked(case.agent, claim.request_id)
            before = _fd_count()
            close()
            assert _fd_count() - before == 0
            proof = read_json_file(claim.path)["capability_closeout"]
            assert proof["acknowledged"] is True and proof["snapshot_pending"] is False
            assert proof["requests"] == {child: [original_request]}
            assert proof["terminal_response"] == response
            requests = {r.id: r.status for r in case.agent.subagents.load(child).capability_requests}
            assert requests == {original_request: "CLOSED", later.id: "OPEN"}
            assert case.agent.subagents.load(late_child).capability_requests[0].id == late_request
            assert case.agent.subagents.load(late_child).capability_requests[0].status == "OPEN"


# LLM: 只用原C/claim/ports写首证明；读取临时不可用，不伪造成功来源、状态或孩子清理。
# 函数用途: 在真实原窗口留下可重复消费的pending快照。
def _pending_closeout(case, monkeypatch):
    from agent_py_agent.agent.conversation import agent_activity
    from agent_py_agent.agent.gateway_parts.request_worker import _close_claimed_parent_capabilities

    response = {"id": case.claim.request_id, "status": "done", "ok": True, "response": "原始结果\n保持字节"}
    with monkeypatch.context() as patch:
        def unavailable(*args, **kwargs):
            raise OSError("snapshot temporarily unreadable")
        patch.setattr(agent_activity, "parent_capability_request_snapshot", unavailable)
        _close_claimed_parent_capabilities(case.paths, case.claim.path, case.claim.request_id, response,
            case.agent, case.claim.execution_attempt_id, case.claim.lease_epoch)
    row = read_json_file(case.claim.path)
    assert row["capability_closeout"]["snapshot_pending"] is True
    return row, response


@pytest.mark.parametrize("key,value", [("owner_id", "providers/test/users/other"),
    ("parent_task_id", "other-parent"), ("request_id", "other-request"),
    ("execution_attempt_id", "other-attempt"), ("lease_epoch", 999), ("lease_owner", "other-lease")])
def test_pending_proof_wrong_scope_is_rejected_without_child_writes(tmp_path, monkeypatch, key, value):
    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
    from agent_py_agent.agent.gateway_parts.request_worker import _close_claimed_parent_capabilities
    from agent_py_agent.agent.runtime_errors import DataCorruptionError

    with coordinated(tmp_path, monkeypatch) as case:
        claim = case.claim
        with coordinator_execution_scope(case.gate, claim, case.agent), request_transaction_ports_scope(case.agent):
            child, _ = _blocked(case.agent, claim.request_id)
            row, response = _pending_closeout(case, monkeypatch)
            original = case.agent.subagents.load(child)
            row["capability_closeout"][key] = value
            write_json_file_atomic(claim.path, row)
            before = _fd_count()
            with pytest.raises(DataCorruptionError):
                _close_claimed_parent_capabilities(case.paths, claim.path, claim.request_id, response,
                    case.agent, claim.execution_attempt_id, claim.lease_epoch)
            assert _fd_count() - before == 0
            assert case.agent.subagents.load(child) == original
            assert read_json_file(claim.path) == row


def test_legacy_pending_proof_never_invents_a_selection_boundary(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import agent_activity
    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
    from agent_py_agent.agent.gateway_parts.request_worker import _close_claimed_parent_capabilities

    with coordinated(tmp_path, monkeypatch) as case:
        claim = case.claim
        with coordinator_execution_scope(case.gate, claim, case.agent), request_transaction_ports_scope(case.agent):
            child, _ = _blocked(case.agent, claim.request_id)
            row, response = _pending_closeout(case, monkeypatch)
            del row["capability_closeout"]["selection_boundary"]
            write_json_file_atomic(claim.path, row)
            monkeypatch.setattr(agent_activity, "parent_capability_request_boundary",
                                lambda *a, **k: pytest.fail("legacy pending cannot rescan current children"))
            _close_claimed_parent_capabilities(case.paths, claim.path, claim.request_id, response,
                case.agent, claim.execution_attempt_id, claim.lease_epoch)
            assert read_json_file(claim.path) == row
            assert case.agent.subagents.load(child).capability_requests[0].status == "OPEN"


@pytest.mark.parametrize("drift", ["owner_id", "runtime_attempt", "prefix", "revision"])
def test_narrow_writer_checks_original_selection_under_canonical_lock(tmp_path, monkeypatch, drift):
    import copy

    from agent_py_agent.agent.subagents.capability_request_expiry import (
        reduce_capability_request_closeout,
    )
    from agent_py_agent.agent.subagents.services.persistence.service import (
        mutate_existing_subagent_metadata,
    )

    with coordinated(tmp_path, monkeypatch) as case:
        claim = case.claim
        with coordinator_execution_scope(case.gate, claim, case.agent), request_transaction_ports_scope(case.agent):
            child, selected = _blocked(case.agent, claim.request_id)
            row, _ = _pending_closeout(case, monkeypatch)
            original = case.agent.subagents.load(child)
            metadata = copy.deepcopy(row["capability_closeout"]["selection_boundary"]["children"][child])
            if drift == "owner_id":
                metadata["owner_id"] = "providers/test/users/other"
            elif drift == "runtime_attempt":
                metadata["runtime_authority"]["attempt_id"] = "other-attempt"
            elif drift == "prefix":
                metadata["request_prefix_sha256"] = "0" * 64
            else:
                metadata["state_revision"] += 1
            with pytest.raises(ValueError, match="selection identity changed"):
                mutate_existing_subagent_metadata(case.agent.subagents.workspace, child,
                    lambda task: reduce_capability_request_closeout(task, claim.request_id,
                        "parent_turn_ended", None, (selected,), 1.0), selection=metadata)
            assert case.agent.subagents.load(child) == original


def test_pending_closeout_preserves_unknown_fields_and_revision_on_retry(tmp_path, monkeypatch):
    from pathlib import Path

    from agent_py_agent.agent.gateway_parts.request_worker import _close_claimed_parent_capabilities
    from agent_py_agent.agent.subagents.services import agent_run_state
    from agent_py_agent.agent.subagents.services.persistence.service import (
        write_private_json_file_atomic,
    )

    with coordinated(tmp_path, monkeypatch) as case:
        claim = case.claim
        with coordinator_execution_scope(case.gate, claim, case.agent), request_transaction_ports_scope(case.agent):
            child, _ = _blocked(case.agent, claim.request_id)
            task = case.agent.subagents.load(child)
            canonical = Path(task.attributes["canonical_state_ref"])
            raw = read_json_file(canonical)
            raw["future_field"] = {"kept": [1, 2, 3]}
            raw["capability_requests"][0]["future_request_field"] = {"kept": True}
            write_private_json_file_atomic(canonical, raw)
            _row, response = _pending_closeout(case, monkeypatch)
            before = raw["state_revision"]
            _close_claimed_parent_capabilities(case.paths, claim.path, claim.request_id, response,
                case.agent, claim.execution_attempt_id, claim.lease_epoch)
            final = agent_run_state.read_agent_state_payload(case.agent.subagents.workspace / child / "task.json")
            assert final["state_revision"] == before + 1 and final["status"] == "CANCELLED"
            assert final["future_field"] == raw["future_field"]
            assert final["capability_requests"][0]["future_request_field"] == {"kept": True}
            committed = canonical.read_bytes()
            _close_claimed_parent_capabilities(case.paths, claim.path, claim.request_id, response,
                case.agent, claim.execution_attempt_id, claim.lease_epoch)
            assert canonical.read_bytes() == committed


def test_live_and_cold_pending_share_durable_selection_transition():
    import ast
    import inspect

    from agent_py_agent.agent.gateway_parts import recovery, request_worker

    for function in (request_worker._close_claimed_parent_capabilities, recovery._recover_parent_capability_closeout):
        calls = [node for node in ast.walk(ast.parse(inspect.getsource(function))) if isinstance(node, ast.Call)]
        transitions = [node for node in calls if isinstance(node.func, ast.Name)
                       and node.func.id == "_reconcile_capability_closeout"]
        assert len(transitions) == 1 and any(k.arg == "publish" for k in transitions[0].keywords)
        assert not any(isinstance(node.func, ast.Name) and node.func.id in {"SimpleAgent", "SubAgentManager"} for node in calls)
    body = ast.parse(inspect.getsource(recovery._resume_capability_snapshot))
    calls = [node for node in ast.walk(body) if isinstance(node, ast.Call)]
    publication = next(node.lineno for node in calls if isinstance(node.func, ast.Name) and node.func.id == "publish")
    in_memory = next(node.lineno for node in calls if isinstance(node.func, ast.Attribute) and node.func.attr == "update")
    assert publication < in_memory
    assert not any(isinstance(node.func, ast.Attribute) and node.func.attr.startswith("list_runs") for node in calls)


# LLM: 此真实进程只验证Count0领域转换；不能替代上面的原P0/P1/P2冷C验收。退出只发生于首证明原子提交之后。
# 函数用途: 在没有硬链接前置的平台限制下，独立观察真实pending提交和进程退出。
def _crash_default_pending(root):
    import os

    from agent_py_agent.agent.gateway_parts import request_worker
    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic

    configure_canonical_background_contracts()
    agent = SimpleAgent(AgentConfig(my_agent_home=str(root / "home"), model_backend="echo",
                                   gateway_per_user_owner_scoping=False), root / "workspace")
    paths = gateway_paths(agent)
    ensure_gateway_folders(paths)
    request_id = "default-process-boundary"
    path = paths.inbox / (request_id + ".json")
    write_json_file_atomic(path, {"id": request_id, "kind": "ask", "goal": "component turn", "user_id": "local-agent"})
    claim = claim_request(paths, path)
    assert claim is not None
    write_json_file_atomic(root / "component-locations.json", {
        "gateway": str(paths.root), "workspace": str(agent.subagents.workspace), "request_id": request_id})
    original = request_worker.write_json_file_atomic

    def crash(path, payload):
        original(path, payload)
        proof = payload.get("capability_closeout", {})
        if path == claim.path and proof.get("snapshot_pending") is True:
            assert proof["requests"] == {} and proof["selection_boundary"] is not None
            os._exit(71)

    request_worker.write_json_file_atomic = crash
    _finish(SimpleNamespace(agent=agent, claim=claim, paths=paths))
    raise AssertionError("first proof checkpoint was not reached")


# LLM: 只在Count0原T/领域窄写口消费实际磁盘证明；没有伪造MP来源或业务Agent，不能算native恢复通过。
# 函数用途: 在第二个全新进程验证同一pending转换、普通EIO重试、读回和原终态投影。
def _default_cold_resume(root, inject_io):
    import errno
    import time
    from dataclasses import asdict
    from pathlib import Path

    from agent_py_agent.agent.gateway_parts.io import (
        gateway_turn_transition,
        write_json_file_atomic,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.gateway_parts.recovery import (
        _reconcile_capability_closeout,
        _RecoveryContext,
        _validated_capability_closeout,
        recover_gateway_processing_requests_report,
    )
    from agent_py_agent.agent.subagents.services.persistence import service

    def forbidden(*args, **kwargs):
        raise AssertionError("metadata recovery cannot construct a business Agent")

    SimpleAgent.__init__ = forbidden
    locations = read_json_file(root / "component-locations.json")
    paths = gateway_paths_from_root(Path(locations["gateway"]))
    rid = locations["request_id"]
    path = paths.processing / (rid + ".json")
    row = read_json_file(path)
    proof = _validated_capability_closeout(row, owner_id="local/main")
    original = service.write_private_json_file_atomic
    errors = []

    def write(path, payload):
        if (inject_io and not errors and path.name == "canonical_state.json"
                and any(r["status"] == "CLOSED" for r in payload.get("capability_requests", []))):
            errors.append(errno.EIO)
            raise OSError(errno.EIO, "one ordinary canonical I/O error")
        return original(path, payload)

    service.write_private_json_file_atomic = write
    before = _fd_count()
    with gateway_turn_transition(paths, rid):
        proof["acknowledged"] = _reconcile_capability_closeout(Path(locations["workspace"]), proof,
            publish=lambda selected: write_json_file_atomic(path, dict(row, capability_closeout=selected)))
        write_json_file_atomic(path, row)
    assert proof["acknowledged"] is True
    context = _RecoveryContext(time.time() + 100, 3, 900, True, None, None)
    report = recover_gateway_processing_requests_report(paths, params=context)
    assert report.load_errors == [] and (paths.terminal / path.name).is_file(), report
    delta = _fd_count() - before
    assert delta == 0 and len(errors) == int(inject_io), {"fd_delta": delta, "io_errors": errors}
    write_json_file_atomic(root / "component-recovery.json", {"report": asdict(report), "fd_delta": delta, "io_errors": errors})


@pytest.mark.parametrize("inject_io", [False, True])
def test_count_zero_real_exit_and_fresh_process_reconstruct_only_original_requests(tmp_path, monkeypatch, inject_io):
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    monkeypatch.setattr(mode, "_WORKER_PARTITION", None)
    configure_canonical_background_contracts()
    agent = SimpleAgent(AgentConfig(my_agent_home=str(tmp_path / "home"), model_backend="echo",
                                   gateway_per_user_owner_scoping=False), tmp_path / "workspace")
    rid = "default-process-boundary"
    child, original_request = _blocked(agent, rid)
    other_owner, _ = _blocked(agent, rid)
    agent.subagents.mutate(other_owner, lambda task: setattr(task, "owner", "providers/test/users/other"))
    other_parent, _ = _blocked(agent, "another-parent")
    repo_root = Path(__file__).resolve().parents[2]
    environment = dict(os.environ, PYTHONPATH=os.pathsep.join((str(repo_root), str(repo_root / "agent_py_agent"))))
    prefix = "from pathlib import Path; from agent_py_agent.tests.test_v13_w4_capability_closeout_multiprocess import "
    producer = prefix + "_crash_default_pending; import sys; _crash_default_pending(Path(sys.argv[1]))"
    crashed = subprocess.run([sys.executable, "-c", producer, str(tmp_path)], cwd=repo_root, env=environment,
                             capture_output=True, text=True, timeout=45, start_new_session=True)
    assert crashed.returncode == 71, crashed.stdout + crashed.stderr
    paths = gateway_paths(agent)
    original = read_json_file(paths.processing / (rid + ".json"))["capability_closeout"]
    assert original["snapshot_pending"] and original["requests"] == {}
    assert tuple(original["selection_boundary"]["children"]) == (child,)
    late = agent.subagents.lifecycle.record_capability_request(child, RecordCapabilityRequestParams(
        problem="late same child", needed_capability="network", requested_tools=["web_fetch"]))
    later_child, _ = _blocked(agent, rid)
    untouched = {run: agent.subagents.load(run) for run in (other_owner, other_parent, later_child)}
    consumer = prefix + "_default_cold_resume; import sys; _default_cold_resume(Path(sys.argv[1]), sys.argv[2] == '1')"
    resumed = subprocess.run([sys.executable, "-c", consumer, str(tmp_path), str(int(inject_io))],
        cwd=repo_root, env=environment, capture_output=True, text=True, timeout=45, start_new_session=True)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    terminal = read_json_file(paths.terminal / (rid + ".json"))
    assert terminal["status"] == "done" and terminal["capability_closeout"]["acknowledged"] is True
    assert terminal["capability_closeout"]["requests"] == {child: [original_request]}
    def encode(value):
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    assert encode(terminal["terminal_response"]) == encode(original["terminal_response"])
    assert {r.id: r.status for r in agent.subagents.load(child).capability_requests} == {
        original_request: "CLOSED", late.id: "OPEN"}
    assert {run: agent.subagents.load(run) for run in untouched} == untouched
    result = read_json_file(tmp_path / "component-recovery.json")
    assert result["fd_delta"] == 0 and len(result["io_errors"]) == int(inject_io)


def test_pending_proof_boolean_lease_cannot_alias_original_integer_generation(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
    from agent_py_agent.agent.gateway_parts.request_worker import _close_claimed_parent_capabilities
    from agent_py_agent.agent.runtime_errors import DataCorruptionError

    with coordinated(tmp_path, monkeypatch) as case:
        claim = case.claim
        with coordinator_execution_scope(case.gate, claim, case.agent), request_transaction_ports_scope(case.agent):
            child, _ = _blocked(case.agent, claim.request_id)
            row, response = _pending_closeout(case, monkeypatch)
            assert type(claim.lease_epoch) is int and claim.lease_epoch == 1
            row["capability_closeout"]["lease_epoch"] = True
            write_json_file_atomic(claim.path, row)
            with pytest.raises(DataCorruptionError):
                _close_claimed_parent_capabilities(case.paths, claim.path, claim.request_id, response,
                    case.agent, claim.execution_attempt_id, claim.lease_epoch)
            assert case.agent.subagents.load(child).capability_requests[0].status == "OPEN"


def test_retry_never_replaces_already_durable_original_terminal_response(tmp_path, monkeypatch):
    from agent_py_agent.agent.runtime_errors import DataCorruptionError

    with coordinated(tmp_path, monkeypatch) as case:
        claim = case.claim
        with coordinator_execution_scope(case.gate, claim, case.agent), request_transaction_ports_scope(case.agent):
            child, _ = _blocked(case.agent, claim.request_id)
            row, _ = _pending_closeout(case, monkeypatch)
            with pytest.raises(DataCorruptionError):
                _finish(case)
            assert read_json_file(claim.path) == row
            assert not (case.paths.terminal / claim.path.name).exists()
            assert case.agent.subagents.load(child).capability_requests[0].status == "OPEN"
