"""真实SimpleAgent/W bootstrap/P3/claim的安全点组件，非真实W进程/native验收；fixture仅种合成输入，不新签运行权。"""

import copy
import json
from contextlib import contextmanager
from dataclasses import asdict, replace
from importlib import import_module

import pytest

from agent_py_agent.agent.common.request_execution_ports import (
    RequestExecutionOutcomeUncertainError,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import input_delivery_service as inputs
from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
from agent_py_agent.agent.gateway_parts.multiprocess_admission import reservation_file
from agent_py_agent.agent.gateway_parts.multiprocess_dispatch_binding import bind_claimed_request
from agent_py_agent.agent.gateway_parts.multiprocess_dispatch_claim import (
    ClaimCandidate,
    freeze_claim_snapshot,
)
from agent_py_agent.agent.gateway_parts.multiprocess_execution import worker_execution_scope
from agent_py_agent.agent.gateway_parts.multiprocess_route import RouteStamp, freeze_request_route
from agent_py_agent.agent.gateway_parts.multiprocess_worker_agent import create_worker_pool
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.queue_service import claim_request, ensure_gateway_folders
from agent_py_agent.agent.gateway_parts.request_binding import GatewayTaskBindingWriter
from agent_py_agent.agent.gateway_parts.request_owner_binding import REQUEST_OWNER_KEY
from agent_py_agent.agent.gateway_parts.request_transactions import request_transaction_ports_scope
from agent_py_agent.agent.user_space.owner_resolver import ensure_owner_home
from agent_py_agent.tests.support.multiprocess_dispatch_cases import (
    reservation_for,
    stamped_request,
)
from agent_py_agent.tests.test_gateway_multiprocess_worker_agent import worker as worker
from agent_py_agent.tests.test_gateway_prepared_route_immutability import _fingerprint
from agent_py_agent.tests.test_runtime_guidance import _tool_loop_params


# LLM: 原现有fixture先C初始化共享home再冻结W；使用实际池/Agent、原P3 bytes和正式claim构造，不改require/permission或假Agent。
# 函数用途: 建立可控制的真实W前台执行，transport仅是合成收件数据，实际writer仍过原short_scope。
@contextmanager
def real_worker_case(worker):
    frozen, config, root, owner, _wrong = worker
    ensure_owner_home(frozen.launch.owner_root, owner)
    agent = create_worker_pool(frozen, config, root).get(owner)
    assert isinstance(agent, SimpleAgent)
    raw = stamped_request(owner)
    raw.pop("route")
    request = freeze_request_route(
        raw, owner, RouteStamp(2, frozen.launch.partition.c_run_id, 100, "binding-v1")
    )
    paths = gateway_paths_from_root(frozen.launch.gateway_root)
    ensure_gateway_folders(paths)
    path = paths.inbox / (request["id"] + ".json")
    write_json_file_atomic(path, request)
    snapshot = freeze_claim_snapshot(
        frozen, ClaimCandidate(path, request, reservation_for(request))
    )
    p3 = reservation_file(paths, request["id"])
    p3.parent.mkdir(parents=True, exist_ok=True)
    p3.write_bytes(snapshot.reservation_bytes)
    claim = claim_request(paths, path, worker_snapshot=snapshot)
    assert claim
    binding = bind_claimed_request(snapshot, claim, read_json_file(claim.path))
    thread = agent.conversation_store.threads.get_or_create(
        {
            "channel": "synthetic",
            "channel_conversation_id": "chat-a",
            "channel_user_id": "synthetic-user",
            "canonical_user_id": "synthetic-user",
        }
    )
    lane = agent.conversation_store.claims.acquire(
        {
            "thread_id": thread.thread_id,
            "task_id": "gateway:" + claim.request_id,
            "reason": "gateway_foreground_turn",
        }
    )
    incoming = {
        "id": "w-incoming",
        "request_id": "w-incoming",
        "kind": "ask",
        "goal": "合成用户补充正文",
        "user_id": "synthetic-user",
        REQUEST_OWNER_KEY: asdict(owner),
        "owner_id": request.get("owner_id", ""),
        "conversation": {
            "enabled": True,
            "channel": "synthetic",
            "channel_conversation_id": "chat-a",
            "channel_user_id": "synthetic-user",
        },
        "metadata": {
            "channel": "synthetic",
            "user_id": "synthetic-user",
            "client_input_digest": "w-input-digest",
            "gateway_admin_binding_version": "binding-v1",
            REQUEST_OWNER_KEY: asdict(owner),
        },
    }
    _fingerprint(incoming)
    with inputs.gateway_input_transition(paths, incoming["id"]):
        receipt, _ = inputs.load_or_prepare_gateway_input_locked(
            paths,
            request_id=incoming["id"],
            client_input_digest="w-input-digest",
            client_message_id="w-message",
            guidance_dedupe_key="w-dedupe",
            prepared_request=incoming,
        )
    with worker_execution_scope(binding, agent), request_transaction_ports_scope(agent):
        GatewayTaskBindingWriter(
            claim.path, claim.request_id, execution_attempt_id=claim.execution_attempt_id
        ).bind_conversation_claim(thread.thread_id)
        target = {
            "schema_version": "gateway_execution_target.v1",
            "thread_id": thread.thread_id,
            "kind": "request",
            "turn_id": claim.request_id,
            "claim_id": lane["claim_id"],
            "execution_attempt_id": claim.execution_attempt_id,
            "runtime_authority": {},
            "task_id": "",
        }
        transport = {
            "schema_version": "gateway_input_transport.v1",
            "target": target,
            "gateway_claim": {
                key: getattr(claim, key)
                for key in ("request_id", "execution_attempt_id", "lease_epoch", "lease_owner")
            },
            "route": request["route"],
        }
        receipt = replace(receipt, transport=transport)
        inputs._write_active_turn_ref(paths, receipt, target_turn_id=claim.request_id)
        write_json_file_atomic(
            inputs.gateway_input_receipt_path(paths, receipt.request_id), receipt.to_dict()
        )
        module = import_module("agent_py_agent.agent.gateway_parts.multiprocess_active_input")
        source = module.pending_turn_input_source(
            agent, read_json_file(claim.path), thread.thread_id
        )
        params = _tool_loop_params(
            request_id=claim.request_id,
            attempt_id=claim.execution_attempt_id,
            task_id="",
            pending_turn_input_source=source,
        )
        yield source, params, agent, claim, paths, receipt, p3


def test_actual_worker_uses_original_source_and_same_p3_to_append_once(worker):
    with real_worker_case(worker) as (source, params, agent, claim, paths, receipt, p3):
        before = p3.read_bytes()
        assert source.probe(agent, params)
        source.drain(agent, params)
        rows = agent.conversation_store.guidance.pending("request", claim.request_id)
        assert len(rows) == 1 and rows[0].message == receipt.prepared_request["goal"]
        source.drain(agent, params)
        assert len(agent.conversation_store.guidance.pending("request", claim.request_id)) == 1
        assert (
            inputs.read_gateway_input_receipt(paths, receipt.request_id).state == "active_pending"
        )
        assert p3.read_bytes() == before and claim.path.exists()


def test_original_source_opaque_copy_and_scope_exit_cannot_drain(worker):
    with real_worker_case(worker) as (source, params, agent, _claim, paths, receipt, _p3):
        before = inputs.gateway_input_receipt_path(paths, receipt.request_id).read_bytes()
        with pytest.raises(RequestExecutionOutcomeUncertainError):
            copy.copy(source).drain(agent, params)
        assert inputs.gateway_input_receipt_path(paths, receipt.request_id).read_bytes() == before
    with pytest.raises(RequestExecutionOutcomeUncertainError):
        source.drain(agent, params)
    assert inputs.gateway_input_receipt_path(paths, receipt.request_id).read_bytes() == before


def test_actual_cancel_blocks_original_worker_append_without_release(worker):
    with real_worker_case(worker) as (source, params, agent, claim, paths, receipt, p3):
        before = p3.read_bytes()
        row = read_json_file(claim.path)
        row["cancel_requested"] = True
        row["turn_phase"] = "closing"
        write_json_file_atomic(claim.path, row)
        with pytest.raises(RequestExecutionOutcomeUncertainError):
            source.drain(agent, params)
        assert not agent.conversation_store.guidance.pending("request", claim.request_id)
        assert inputs.read_gateway_input_receipt(paths, receipt.request_id).state == "pending"
        assert p3.read_bytes() == before and claim.path.exists()


@pytest.mark.parametrize(
    "kind", ["common", "context", "coordinator", "bounded", "global", "ordinary_same_code"]
)
def test_actual_worker_keeps_typed_or_ordinary_error_same_object_without_fallback(
    worker, monkeypatch, kind
):
    with real_worker_case(worker) as (source, params, agent, claim, paths, receipt, p3):
        from agent_py_agent.agent.backends.bounded_call import BoundedCallExitUnconfirmedError
        from agent_py_agent.agent.common.request_execution_ports import (
            RequestExecutionContextUnavailable,
            RequestExecutionOutcomeUncertainError,
        )
        from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import (
            CoordinatorUnavailable,
        )
        from agent_py_agent.agent.gateway_parts.multiprocess_global_write import GlobalWriteError

        if kind == "common":
            error = RequestExecutionOutcomeUncertainError(
                read_json_file(claim.path), "injected-original-append"
            )
        elif kind == "context":
            error = RequestExecutionContextUnavailable("injected-origin")
        elif kind == "coordinator":
            error = CoordinatorUnavailable("injected-original")
        elif kind == "bounded":
            error = BoundedCallExitUnconfirmedError("synthetic-call", "worker_exit_unconfirmed")
        elif kind == "global":
            error = GlobalWriteError("injected-original", "fixture")
        else:
            error = RuntimeError("ordinary")
            error.error_code = "ACTIVE_TURN_OUTCOME_UNCERTAIN"

        def fail(*_args, **_kwargs):
            raise error

        monkeypatch.setattr(agent.conversation_store.guidance, "append_once", fail)
        before = p3.read_bytes()
        original = inputs.gateway_input_receipt_path(paths, receipt.request_id).read_bytes()
        with pytest.raises(type(error)) as caught:
            source.drain(agent, params)
        assert caught.value is error
        assert inputs.gateway_input_receipt_path(paths, receipt.request_id).read_bytes() == original
        assert (
            p3.read_bytes() == before
            and claim.path.exists()
            and not (paths.inbox / (receipt.request_id + ".json")).exists()
        )
