"""完整v3运输投影；旧v2原字节语义不变，投影不授执行权。"""

import copy
from dataclasses import replace

import pytest

from agent_py_agent.agent.gateway_parts import input_delivery_service as inputs
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.tests.test_gateway_cold_request_pump import queue_request
from agent_py_agent.tests.test_gateway_prepared_route_immutability import context as context


def transport():
    return {
        "schema_version": "gateway_input_transport.v1",
        "target": {
            "schema_version": "gateway_execution_target.v1",
            "thread_id": "thread",
            "kind": "request",
            "turn_id": "target",
            "claim_id": "claim",
            "execution_attempt_id": "attempt",
            "runtime_authority": {},
            "task_id": "",
        },
        "gateway_claim": {
            "request_id": "target",
            "execution_attempt_id": "attempt",
            "lease_epoch": 1,
            "lease_owner": "lease",
        },
        "route": {"worker": 0, "c_run_id": "fixture-c"},
    }


def test_v3_roundtrip_preserves_complete_transport_and_prepared(context):
    receipt = queue_request(context, "codec")
    row = receipt.to_dict()
    row["schema_version"] = "gateway_input_receipt.v3"
    row["transport"] = transport()
    decoded = inputs.GatewayInputReceipt.from_dict(row)
    assert decoded.to_dict() == row
    assert replace(decoded, state="active_pending").to_dict()["transport"] == row["transport"]
    row["transport"]["gateway_claim"]["lease_epoch"] = 99
    assert decoded.to_dict()["transport"]["gateway_claim"]["lease_epoch"] == 1


def test_v2_roundtrip_does_not_add_transport_or_change_schema(context):
    row = queue_request(context, "v2").to_dict()
    assert row["schema_version"] == "gateway_input_receipt.v2" and "transport" not in row
    assert inputs.GatewayInputReceipt.from_dict(row).to_dict() == row


@pytest.mark.parametrize(
    "fault",
    ["absent", "none", "empty", "bool_epoch", "wrong_target", "extra", "path", "v2_transport"],
)
def test_transport_malformed_cannot_be_silently_dropped(context, fault):
    row = queue_request(context, "bad").to_dict()
    row["schema_version"] = "gateway_input_receipt.v3"
    row["transport"] = transport()
    if fault == "absent":
        row.pop("transport")
    elif fault == "none":
        row["transport"] = None
    elif fault == "empty":
        row["transport"] = {}
    elif fault == "bool_epoch":
        row["transport"]["gateway_claim"]["lease_epoch"] = True
    elif fault == "wrong_target":
        row["transport"]["target"]["turn_id"] = "different"
    elif fault == "extra":
        row["transport"]["grant"] = True
    elif fault == "path":
        row["transport"]["gateway_claim"]["request_id"] = "../target"
    else:
        row["schema_version"] = "gateway_input_receipt.v2"
    with pytest.raises(DataCorruptionError):
        inputs.GatewayInputReceipt.from_dict(row)


def test_processing_recovery_identity_includes_optional_complete_transport(context):
    from agent_py_agent.agent.gateway_parts.multiprocess_processing_recovery import (
        _receipt_identity,
    )

    row = queue_request(context, "identity").to_dict()
    original = _receipt_identity(row)
    with_transport = {**row, "schema_version": "gateway_input_receipt.v3", "transport": transport()}
    changed = copy.deepcopy(with_transport)
    changed["transport"]["gateway_claim"]["lease_epoch"] = 2
    assert _receipt_identity(with_transport) != _receipt_identity(changed)
    assert _receipt_identity(row) == original
