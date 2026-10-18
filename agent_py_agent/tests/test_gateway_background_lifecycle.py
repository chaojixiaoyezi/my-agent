"""规范后台来源→首次输入→真实C claim→原业务→重排/归档；只用临时home和假模型。"""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends.request_scope import foreground_model_scope
from agent_py_agent.agent.gateway_parts.input_delivery_service import read_gateway_input_receipt
from agent_py_agent.agent.gateway_parts.io import read_json_file
from agent_py_agent.agent.gateway_parts.multiprocess_admission import reservation_file
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_execution import (
    coordinator_execution_scope,
)
from agent_py_agent.agent.gateway_parts.request_owner_binding import (
    REQUEST_OWNER_KEY,
    frozen_request_owner,
)
from agent_py_agent.agent.gateway_parts.request_transactions import request_transaction_ports_scope
from agent_py_agent.agent.gateway_parts.request_worker import (
    _finish_claimed_gateway_request,
    _process_claimed_gateway_request_path,
)
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_gateway_background_curator_transactions import curator_case


# LLM: 业务仍走原worker入口和已冻结C来源；前台占用只用原endpoint事实，绝不替换消费者或权限门。
# 函数用途: 以原后台服务暂忙验证同一请求让槽，保留原来源和P3。
def test_busy_original_curator_requeues_same_claim_without_terminal_or_source_write(tmp_path, monkeypatch):
    with curator_case(tmp_path, monkeypatch) as (agent, service, backend, gate, claim, _):
        backend.api_base = "http://synthetic-model.invalid/v1"
        source_before = service.state_store.path.read_bytes()
        p3 = reservation_file(gate.paths, claim.request_id)
        before_p3 = p3.read_bytes()
        receipt_before = read_gateway_input_receipt(gate.paths, claim.request_id)
        original = read_json_file(claim.path)
        assert frozen_request_owner(original) == OwnerIdentity.local_main()
        assert original["metadata"][REQUEST_OWNER_KEY] == asdict(OwnerIdentity.local_main())
        with foreground_model_scope(SimpleNamespace(api_base=backend.api_base)):
            with coordinator_execution_scope(gate, claim, agent):
                assert _process_claimed_gateway_request_path(agent, gate.paths, claim.path, "original-C", claim=claim)
        queued = read_json_file(gate.paths.inbox / claim.path.name)
        assert not claim.path.exists() and not (gate.paths.terminal / claim.path.name).exists()
        assert queued["id"] == original["id"] and queued["status"] == "pending"
        assert queued["request_fingerprint"] == original["request_fingerprint"]
        assert queued["route"] == original["route"] and queued["metadata"] == original["metadata"]
        assert queued["not_before_at"] > time.time() and "execution_attempt_id" not in queued
        assert p3.read_bytes() == before_p3
        assert service.state_store.path.read_bytes() == source_before and backend.calls == 0
        assert read_gateway_input_receipt(gate.paths, claim.request_id) == receipt_before


@pytest.mark.parametrize("change", ["changed", "removed"])
def test_old_source_transport_does_not_consume_new_source_or_call_model(tmp_path, monkeypatch, change):
    with curator_case(tmp_path, monkeypatch) as (agent, service, backend, gate, claim, _):
        if change == "changed":
            # 新trigger也消费原C短写端口，不借本测试绕过正式writer门。
            with coordinator_execution_scope(gate, claim, agent), request_transaction_ports_scope(agent):
                service.request("admin")
            expected = service.state_store.path.read_bytes()
        else:
            service.state_store.path.unlink()
            expected = None
        with coordinator_execution_scope(gate, claim, agent):
            assert _process_claimed_gateway_request_path(agent, gate.paths, claim.path, "original-C", claim=claim)
        terminal = read_json_file(gate.paths.terminal / claim.path.name)
        assert terminal["terminal_response"]["ok"] is True
        assert terminal["terminal_response"]["background_result"]["outcome"] == "source_changed"
        assert not claim.path.exists() and not (gate.paths.inbox / claim.path.name).exists()
        assert backend.calls == 0 and service.candidate_service.list() == []
        actual = service.state_store.path.read_bytes() if service.state_store.path.exists() else None
        assert actual == expected
        assert read_json_file(reservation_file(gate.paths, claim.request_id))["state"] == "occupied"
        gate.release_settled()
        assert read_json_file(reservation_file(gate.paths, claim.request_id))["state"] == "released"


def test_pending_response_without_original_execution_agent_cannot_archive_or_release(tmp_path, monkeypatch):
    with curator_case(tmp_path, monkeypatch) as (agent, _, _, gate, claim, _):
        payload = read_json_file(claim.path)
        before, p3 = claim.path.read_bytes(), reservation_file(gate.paths, claim.request_id).read_bytes()
        ref = payload["metadata"]["background_source"]
        response = {"id": claim.request_id, "kind": "background", "ok": True, "status": "done",
                    "background_result": {"outcome": "pending", "source_kind": ref["kind"], "source_id": ref["source_id"]}}
        with coordinator_execution_scope(gate, claim, agent), pytest.raises(ValueError, match="background_original_worker_execution_required"):
            _finish_claimed_gateway_request(gate.paths, claim.path, claim.request_id, response,
                expected_execution_attempt_id=claim.execution_attempt_id, expected_lease_epoch=claim.lease_epoch)
        assert claim.path.read_bytes() == before
        assert reservation_file(gate.paths, claim.request_id).read_bytes() == p3
        assert not (gate.paths.terminal / claim.path.name).exists()


def test_original_ref_owner_is_preserved_before_full_prepared_digest(tmp_path, monkeypatch):
    with curator_case(tmp_path, monkeypatch) as (agent, _, _, gate, claim, _):
        receipt = read_gateway_input_receipt(gate.paths, claim.request_id)
        original = receipt.prepared_request
        ref = original["metadata"]["background_source"]
        assert ref["owner"] == asdict(OwnerIdentity.local_main())
        assert frozen_request_owner(original) == OwnerIdentity.local_main()
        assert original["metadata"][REQUEST_OWNER_KEY] == ref["owner"]
        assert original["metadata"]["gateway_admin_binding_version"] == "none"
        assert json.loads(claim.path.read_bytes())["metadata"] == original["metadata"]
