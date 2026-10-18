"""原真实claim与P3/P5短门：只使用临时文件、规范HomePaths和可控Python线程。"""
import json
from contextvars import copy_context
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.request_execution_ports import (
    RequestExecutionOutcomeUncertainError,
)
from agent_py_agent.agent.core import configure_canonical_background_contracts
from agent_py_agent.agent.gateway_parts import multiprocess_mode as mode
from agent_py_agent.agent.gateway_parts.multiprocess_dispatch import WorkerDispatchError
from agent_py_agent.agent.gateway_parts.multiprocess_execution import worker_execution_scope
from agent_py_agent.agent.gateway_parts.request_binding import GatewayActiveTurnTransition
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    resolve_owner_home,
)
from agent_py_agent.tests.support.multiprocess_agent_cases import execution_case
from agent_py_agent.tests.test_gateway_prepared_route_immutability import context as context


@pytest.fixture
def actual(tmp_path, monkeypatch):
    target = (tmp_path / "isolated-home").resolve()
    monkeypatch.setenv("MY_AGENT_HOME", str(target))
    assert target != (Path.home() / ".my-agent").resolve()
    configure_canonical_background_contracts()
    binding, request = execution_case(tmp_path, monkeypatch)
    root = binding.snapshot.bootstrap.launch.owner_root
    owner = OwnerIdentity.provider_user("synthetic", "alice")
    # 只有业务对象为替身；scope消费的是原claim、P3字节、P5快照及真实typed HomePaths。
    agent = SimpleNamespace(home_paths=home_paths_with_owner(home_paths(root), resolve_owner_home(root, owner)))
    return binding, request, agent


def _transition(binding):
    return GatewayActiveTurnTransition(binding.claim.path, binding.claim.request_id, binding.claim.execution_attempt_id)


def test_real_worker_claim_preserves_original_fence_and_executes_only_in_bound_scope(actual):
    binding, request, agent = actual
    assert request["status"] == "processing"
    assert request["execution_attempt_id"] == binding.claim.execution_attempt_id
    assert request["lease_epoch"] == binding.claim.lease_epoch == 1
    assert not binding.snapshot.request_path.exists()
    with worker_execution_scope(binding, agent):
        transition = _transition(binding)
        assert transition("submit", lambda: "same-original-worker") == "same-original-worker"
    with pytest.raises(WorkerDispatchError):
        transition("submit", lambda: pytest.fail("closed scope executed"))


def test_worker_without_original_claim_snapshot_cannot_move_pending(actual):
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.gateway_parts.queue_service import claim_request

    binding, _request, _agent = actual
    paths = gateway_paths_from_root(binding.snapshot.bootstrap.launch.gateway_root)
    path = paths.inbox / "different-pending.json"
    path.write_bytes(binding.snapshot.request_bytes)
    before = path.read_bytes()
    with pytest.raises(WorkerDispatchError, match="claim_snapshot_required"):
        claim_request(paths, path)
    assert path.read_bytes() == before and not (paths.processing / path.name).exists()


@pytest.mark.parametrize("field", ["execution_attempt_id", "lease_epoch", "route"])
def test_callback_rechecks_actual_claim_before_original_side_effect(actual, field):
    binding, request, agent = actual
    with worker_execution_scope(binding, agent):
        transition = _transition(binding)
        if field == "route":
            request["route"]["c_run_id"] = "another-c"
        elif field == "lease_epoch":
            request[field] += 1
        else:
            request[field] = "another-attempt"
        binding.claim.path.write_text(json.dumps(request))
        with pytest.raises((WorkerDispatchError, InterruptedError)):
            transition("submit", lambda: pytest.fail("changed source executed"))


def test_cancel_keeps_original_ack_but_refuses_new_submit(actual):
    binding, request, agent = actual
    with worker_execution_scope(binding, agent):
        transition = _transition(binding)
        request.update(cancel_requested=True, turn_phase="closing")
        binding.claim.path.write_text(json.dumps(request))
        with pytest.raises(InterruptedError):
            transition("submit", lambda: pytest.fail("cancelled request submitted"))
        assert transition("acknowledge", lambda: "same-attempt-cleanup") == "same-attempt-cleanup"


def test_copied_context_does_not_extend_original_execution_lifetime(actual):
    binding, _request, agent = actual
    with worker_execution_scope(binding, agent):
        captured = copy_context()
        transition = _transition(binding)
        assert captured.run(transition, "submit", lambda: "live") == "live"
    with pytest.raises(WorkerDispatchError, match="execution_scope_unavailable"):
        captured.run(transition, "submit", lambda: pytest.fail("copied expired source executed"))


def test_default_count_zero_retains_original_transition_without_worker_scope(actual, monkeypatch):
    binding, _request, _agent = actual
    monkeypatch.setattr(mode, "_WORKER_PARTITION", None)
    assert _transition(binding)("submit", lambda: "original-count-zero") == "original-count-zero"


# LLM: 原control唯一Root先登记实际gate；原P3/C claim/执行scope仍真实，ack/terminal不得因Root closing被新准入门拦。
# 函数用途: 为同一临时C准备原合法claim并保留其唯一Root来源，供业务短事务回归消费。
@pytest.fixture
def c_actual(context):
    from agent_py_agent.agent.gateway_parts import input_delivery_service as delivery
    from agent_py_agent.agent.gateway_parts.multiprocess_admission import (
        coordinator_admission_gate,
        session_scope_for_request,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_claim import (
        freeze_coordinator_claim,
    )
    from agent_py_agent.agent.gateway_parts.queue_service import (
        claim_request,
        ensure_gateway_folders,
    )
    from agent_py_agent.agent.gateway_parts.request_worker import AdmissionLimits

    configure_canonical_background_contracts()
    ensure_gateway_folders(context.paths)
    payload = {"id": "actual-c-input", "kind": "ask", "goal": "合成C请求",
               "user_id": "local-agent", "metadata": {"channel": "chat", "user_id": "local-agent",
               "client_input_digest": "actual-c-digest"}}
    with delivery.gateway_input_transition(context.paths, payload["id"]):
        receipt, _ = delivery.load_or_prepare_gateway_input_locked(context.paths,
            request_id=payload["id"], client_input_digest="actual-c-digest", client_message_id="",
            guidance_dedupe_key="", prepared_request=payload)
        delivery.queue_gateway_input_locked(context.paths, receipt)
    # 业务对象仅提供原类型HomePaths；claim、P3、lease/controller和原快照全部走真实产品写者。
    agent = SimpleNamespace(config=context.config, home_paths=home_paths(context.home_root))
    gate = coordinator_admission_gate(agent, context.paths, SimpleNamespace(coordinator_worker_count=2))
    from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime

    # 正式gate须沿该原control登记唯一Root；原claim/业务对象及所有断言保持。
    runtime = getattr(context, "drain_runtime", None) or GatewayDrainRuntime(context)
    runtime.retain_admission(gate)
    decision = gate.admit(context.paths.inbox / (payload["id"] + ".json"),
        (OwnerIdentity.local_main(), session_scope_for_request(receipt.prepared_request)), AdmissionLimits(8, 8))
    assert decision["status"] == "admitted"
    snapshot = freeze_coordinator_claim(gate, decision)
    claim = claim_request(context.paths, context.paths.inbox / (payload["id"] + ".json"),
                          coordinator_snapshot=snapshot)
    assert claim is not None and claim.coordinator_snapshot is snapshot
    return gate, claim, agent


def test_actual_c_claim_and_scope_use_same_original_lease_and_snapshot(c_actual):
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_execution import (
        coordinator_execution_scope,
    )

    gate, claim, agent = c_actual
    with coordinator_execution_scope(gate, claim, agent):
        transition = _transition(SimpleNamespace(claim=claim))
        assert transition("submit", lambda: "same-C-claim") == "same-C-claim"
    with pytest.raises(WorkerDispatchError, match="execution_scope_unavailable"):
        transition("submit", lambda: pytest.fail("C scope exited"))


def test_actual_worker_background_gate_consumes_real_claim_scope(actual):
    from agent_py_agent.agent.gateway_parts import multiprocess_background_execution as bridge

    binding, request, agent = actual
    context = {"agent": agent, "request": request, "request_path": binding.claim.path,
               "request_id": binding.claim.request_id}
    with worker_execution_scope(binding, agent), bridge.background_request_scope(context):
        assert bridge.require_background_model_admission() is None
        request.update(cancel_requested=True, turn_phase="closing")
        binding.claim.path.write_text(json.dumps(request))
        with pytest.raises(RequestExecutionOutcomeUncertainError):
            bridge.require_background_model_admission()


def test_actual_c_background_gate_consumes_real_claim_scope(c_actual):
    from agent_py_agent.agent.gateway_parts import multiprocess_background_execution as bridge
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_execution import (
        coordinator_execution_scope,
    )

    gate, claim, agent = c_actual
    request = json.loads(claim.path.read_bytes())
    context = {"agent": agent, "request": request, "request_path": claim.path, "request_id": claim.request_id}
    with coordinator_execution_scope(gate, claim, agent), bridge.background_request_scope(context):
        assert bridge.require_background_model_admission() is None
