"""原receipt冻结owner的完整性：不把可修改队列自洽字段当作重新归属依据。"""
import copy
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import input_delivery_service as delivery
from agent_py_agent.agent.gateway_parts import multiprocess_admission as admission
from agent_py_agent.agent.gateway_parts import multiprocess_route as route
from agent_py_agent.agent.gateway_parts.io import (
    gateway_request_fingerprint,
    write_json_file_atomic,
)
from agent_py_agent.agent.gateway_parts.request_owner_binding import (
    REQUEST_OWNER_KEY,
    frozen_request_owner,
)
from agent_py_agent.agent.gateway_parts.request_worker import AdmissionLimits
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from agent_py_agent.tests.test_gateway_prepared_route_immutability import (
    _prepare,
    _request,
)
from agent_py_agent.tests.test_gateway_prepared_route_immutability import context as context


def _changed_owner(context, payload):
    result = copy.deepcopy(payload)
    other = OwnerIdentity.provider_user("feishu", "fixture-bob")
    result[REQUEST_OWNER_KEY] = asdict(other)
    result["owner_id"] = resolve_owner_home(context.home_root, other).owner_id
    return result, other


def test_materializer_rejects_different_canonical_host_owner_before_first_route(context):
    receipt, _ = _prepare(context.paths)
    changed, _ = _changed_owner(context, receipt.prepared_request)
    write_json_file_atomic(context.paths.inbox / "fixture-input.json", changed)
    with pytest.raises(ValueError, match="different input"):
        delivery._materialize_prepared_request_locked(context.paths, receipt)


def test_route_rejects_different_prepared_owner_without_resigning_fingerprint(context):
    receipt, _ = _prepare(context.paths)
    changed, other = _changed_owner(context, receipt.prepared_request)
    assert changed["request_fingerprint"] == receipt.prepared_request["request_fingerprint"]
    with pytest.raises(route.OwnerRouteMismatchError):
        route.freeze_request_route(changed, other, route.RouteStamp(2, "fixture-c", 1.0, "none"))


@pytest.mark.parametrize("field", ["owner", "owner_id", "both_metadata_and_fingerprint"])
def test_actual_admission_checks_original_receipt_before_any_reservation(context, field):
    receipt, _ = _prepare(context.paths)
    with delivery.gateway_input_transition(context.paths, receipt.request_id):
        receipt, _ = delivery.queue_gateway_input_locked(context.paths, receipt)
    changed, other = _changed_owner(context, receipt.prepared_request)
    if field == "owner_id":
        changed[REQUEST_OWNER_KEY] = receipt.prepared_request[REQUEST_OWNER_KEY]
        other = frozen_request_owner(changed)
    elif field == "both_metadata_and_fingerprint":
        changed["metadata"][REQUEST_OWNER_KEY] = asdict(other)
        changed["request_fingerprint"] = gateway_request_fingerprint(changed, changed["id"])
    path = context.paths.inbox / (receipt.request_id + ".json")
    write_json_file_atomic(path, changed)
    before = path.read_bytes()
    gate = admission.coordinator_admission_gate(None, context.paths, SimpleNamespace(coordinator_worker_count=2))
    decision = gate.admit(path, (other, admission.session_scope_for_request(changed)), AdmissionLimits(8, 8),
                          snapshot=changed)
    assert decision["status"] == "blocked" and decision["reason"] == "prepared_input_invalid"
    assert path.read_bytes() == before
    assert not admission.reservation_file(context.paths, receipt.request_id).exists()
    assert delivery.read_gateway_input_receipt(context.paths, receipt.request_id) == receipt


def test_canonical_receipt_owner_corruption_is_not_accepted_as_valid_replay(context):
    receipt, _ = _prepare(context.paths)
    changed, _ = _changed_owner(context, receipt.prepared_request)
    corrupted = replace(receipt, prepared_request=changed)
    write_json_file_atomic(delivery.gateway_input_receipt_path(context.paths, receipt.request_id), corrupted.to_dict())
    with pytest.raises(DataCorruptionError):
        _prepare(context.paths)
    assert not list(context.paths.inbox.glob("*.json"))


def test_legitimate_receipt_replay_keeps_owner_assertion_and_same_observation(context):
    first, created = _prepare(context.paths)
    assert created
    owner = frozen_request_owner(first.prepared_request)
    assert first.prepared_request["metadata"][REQUEST_OWNER_KEY] == asdict(owner)
    second, created = _prepare(context.paths)
    assert not created and second == first
    with delivery.gateway_input_transition(context.paths, first.request_id):
        delivery.queue_gateway_input_locked(context.paths, second)
    gate = admission.coordinator_admission_gate(None, context.paths, SimpleNamespace(coordinator_worker_count=2))
    from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime

    GatewayDrainRuntime(context).retain_admission(gate)
    decision = gate.admit(context.paths.inbox / (first.request_id + ".json"),
        (owner, admission.session_scope_for_request(first.prepared_request)), AdmissionLimits(8, 8))
    assert decision["status"] == "admitted"
    assert decision["request"][REQUEST_OWNER_KEY] == asdict(owner)


def test_original_no_receipt_path_remains_explicit_without_synthetic_receipt(context):
    prepared = context.prepare_input(_request())
    # 这是明确旧无receipt输入合同，不声称缺断言的旧pure route证明新C完整身份。
    prepared["metadata"].pop(REQUEST_OWNER_KEY, None)
    prepared["request_fingerprint"] = gateway_request_fingerprint(prepared, prepared["id"])
    path = context.paths.inbox / (prepared["id"] + ".json")
    write_json_file_atomic(path, prepared)
    gate = admission.coordinator_admission_gate(None, context.paths, SimpleNamespace(coordinator_worker_count=2))
    from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime

    GatewayDrainRuntime(context).retain_admission(gate)
    decision = gate.admit(path, (frozen_request_owner(prepared), admission.session_scope_for_request(prepared)),
                          AdmissionLimits(8, 8))
    assert decision["status"] == "admitted"
    assert not delivery.gateway_input_receipt_path(context.paths, prepared["id"]).exists()
