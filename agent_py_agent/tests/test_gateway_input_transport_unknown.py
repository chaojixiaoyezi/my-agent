"""输入结算原typed未知不改普通失败、空集合或游标进度。"""
import hashlib
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.request_execution_ports import (
    RequestExecutionOutcomeUncertainError,
)
from agent_py_agent.agent.gateway_parts import input_delivery_service as delivery
from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
from agent_py_agent.tests.test_gateway_claim_transition_scope import c_actual as c_actual
from agent_py_agent.tests.test_gateway_prepared_route_immutability import context as context


# LLM: 错误只由原真实claim载荷捕获；故障注入仅替代一个读取/结算抛点，不伪造或绕过任何许可门。
# 函数用途: 为输入层传递测试保留真实运输身份与同一异常对象。
def _fault(c_actual):
    gate, claim, _agent = c_actual
    payload = read_json_file(claim.path)
    error = RequestExecutionOutcomeUncertainError(payload, "synthetic_input_read")
    def fail(*_args, **_kwargs):
        raise error
    return gate, claim, error, fail


def test_settlement_keeps_original_typed_unknown_instead_of_error_count(c_actual):
    gate, claim, error, fail = _fault(c_actual)
    receipt = delivery.gateway_input_receipt_path(gate.paths, claim.request_id)
    before = receipt.read_bytes()
    store = SimpleNamespace(guidance=SimpleNamespace(recovery=SimpleNamespace(reject_pending=fail)))
    with pytest.raises(RequestExecutionOutcomeUncertainError) as caught:
        delivery.settle_gateway_inputs_for_turn(gate.paths, target_turn_id=claim.request_id, conversation_store=store)
    assert caught.value is error and receipt.read_bytes() == before
    assert gate.occupied_snapshot()["total"] == 1


def test_closeout_read_unknown_is_not_treated_as_empty_replay(c_actual):
    gate, claim, error, fail = _fault(c_actual)
    store = SimpleNamespace(guidance=SimpleNamespace(recovery=SimpleNamespace(closeout_replay_receipts=fail)))
    with pytest.raises(RequestExecutionOutcomeUncertainError) as caught:
        delivery._settle_steer_closeout(gate.paths, store, claim.request_id, {"errors": 0})
    assert caught.value is error and gate.occupied_snapshot()["total"] == 1


def test_unknown_input_read_does_not_advance_reconcile_cursor(c_actual, monkeypatch):
    gate, _claim, error, fail = _fault(c_actual)
    cursor = gate.paths.root / "input_receipts" / "state" / "reconcile_cursor.json"
    assert not cursor.exists()
    monkeypatch.setattr(delivery, "read_gateway_input_receipt", fail)
    with pytest.raises(RequestExecutionOutcomeUncertainError) as caught:
        delivery.reconcile_gateway_input_receipts(gate.paths, gate.agent)
    assert caught.value is error and not cursor.exists()
    assert gate.occupied_snapshot()["total"] == 1


def test_broken_turn_index_does_not_report_clean_input_settlement(c_actual):
    gate, claim, _agent = c_actual
    digest = hashlib.sha256(claim.request_id.encode()).hexdigest()
    path = gate.paths.root / "input_receipts" / "by_turn" / digest / "broken.json"
    write_json_file_atomic(path, {"schema_version": "synthetic_bad_index"})
    store = SimpleNamespace(guidance=SimpleNamespace(recovery=SimpleNamespace(
        reject_pending=lambda *_args, **_kwargs: None,
        closeout_replay_receipts=lambda *_args: [],
    )))
    summary = delivery.settle_gateway_inputs_for_turn(
        gate.paths, target_turn_id=claim.request_id, conversation_store=store)
    assert summary["errors"] == 1
    assert summary["consumed"] == summary["queued"] == 0
    assert gate.occupied_snapshot()["total"] == 1
