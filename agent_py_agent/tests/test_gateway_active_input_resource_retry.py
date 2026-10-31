"""独立普通锁竞争复核；只持有合法本机临时input T，不改变存储行或权限。"""

import json
import os
import threading
from pathlib import Path

from agent_py_agent.agent.backends import get_backend
from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.gateway_parts.multiprocess_active_input import OriginalPendingTurnInput
from agent_py_agent.agent.gateway_parts.multiprocess_admission import reservation_file
from agent_py_agent.agent.gateway_parts.request_worker import _process_claimed_gateway_request_path
from agent_py_agent.tests.test_gateway_active_input_transport import (
    AdmissionLimits,
    EchoBackend,
    OwnerIdentity,
    c_http,
    claim_request,
    coordinator_execution_scope,
    freeze_coordinator_claim,
    inputs,
    read_json_file,
    request,
    send,
    session_scope_for_request,
)


def test_actual_claimed_pipeline_resumes_after_input_lock_busy(tmp_path, monkeypatch):
    with c_http(tmp_path, monkeypatch) as (agent, backend, gate, control, runtime, server, close):
        # 有 Home 时目录未选模型即未配置；构造后显式注入的 echo 实例才在模型作用域里保留，下方再替换它的生成入口。
        agent.backend = get_backend("echo", agent.config)
        code, accepted = request(
            server,
            "POST",
            "/ask",
            {
                "goal": "合成正常前台工作",
                "conversation_id": "active-cid",
                "metadata": {"message_id": "review-resource-busy"},
            },
            headers={"X-User-Id": "local-agent", "X-Channel": "chat"},
        )
        assert code == 202 and accepted["input_state"] == "queued"
        receipt = inputs.read_gateway_input_receipt(gate.paths, accepted["request_id"])
        path = gate.paths.inbox / (receipt.request_id + ".json")
        decision = gate.admit(
            path,
            (OwnerIdentity.local_main(), session_scope_for_request(receipt.prepared_request)),
            AdmissionLimits(8, 8),
        )
        claim = claim_request(
            gate.paths, path, coordinator_snapshot=freeze_coordinator_claim(gate, decision)
        )
        assert claim
        p3 = reservation_file(gate.paths, claim.request_id)
        before = p3.read_bytes()
        entered, release = threading.Event(), threading.Event()
        holders = []
        calls = []
        ingress = []
        observed = []
        original = OriginalPendingTurnInput.drain

        def observe_drain(self, *args, **kwargs):
            try:
                return original(self, *args, **kwargs)
            except BlockingIOError as exc:
                observed.append(type(exc).__name__)
                release.set()
                for thread in holders:
                    thread.join(5)
                    assert not thread.is_alive()
                raise

        monkeypatch.setattr(OriginalPendingTurnInput, "drain", observe_drain)

        def generate(_backend, prompt, **options):
            calls.append(1)
            if len(calls) == 1:
                status, response = send(server, message="resource-busy-arrival")
                assert status == 202 and response["input_state"] == "pending"
                ingress.append(response["request_id"])

                def hold():
                    with inputs.gateway_input_transition(gate.paths, ingress[0]):
                        entered.set()
                        release.wait(15)

                thread = threading.Thread(target=hold)
                holders.append(thread)
                thread.start()
                assert entered.wait(5)
            return ModelResponse(text="正常合成模型回复。", backend="echo")

        monkeypatch.setattr(EchoBackend, "generate", generate)
        error = None
        finished = None
        try:
            with coordinator_execution_scope(gate, claim, agent):
                finished = _process_claimed_gateway_request_path(
                    agent, gate.paths, claim.path, claim.lease_owner, claim=claim
                )
        except Exception as exc:
            error = {
                "type": type(exc).__name__,
                "code": getattr(exc, "error_code", None),
                "message": str(exc),
            }
        finally:
            release.set()
            for thread in holders:
                thread.join(5)
                assert not thread.is_alive()
        response_path = gate.paths.responses / (claim.request_id + ".json")
        result = read_json_file(response_path) if response_path.exists() else None
        delivered = inputs.read_gateway_input_receipt(gate.paths, ingress[0])
        facts = {
            "observed_busy": observed,
            "model_calls": len(calls),
            "finished": finished,
            "escaped": error,
            "response": result,
            "input_state": delivered.state,
            "target_turn_id": delivered.target_turn_id,
            "processing_exists": claim.path.exists(),
            "terminal_exists": (gate.paths.terminal / (claim.request_id + ".json")).exists(),
            "p3_exists": p3.exists(),
            "p3_same": p3.read_bytes() == before if p3.exists() else False,
        }
        Path(os.environ.get("REVIEW_BUSY_FACTS", str(tmp_path / "busy-facts.json"))).write_text(
            json.dumps(facts, ensure_ascii=False, indent=2) + "\n"
        )
        assert observed == ["BlockingIOError"]
        assert result and result["ok"] is True, facts
        assert delivered.state == "consumed"
        assert len(calls) == 2
