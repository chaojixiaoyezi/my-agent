# LLM: 完整 C HTTP/原 claim/guidance 的临时存储验证，不访问商业模型或生产 Gateway。
# 模块用途: 核对原活动输入运输、恢复、幂等及不等价选项排队合同。
"""完整C HTTP→原前台claim→guidance安全点；仅测试回环HTTP/真实临时Store/假模型，不是native/Count2验收。"""

import json
from contextlib import contextmanager
from dataclasses import replace
from importlib import import_module

import pytest

from agent_py_agent.agent.agent_core.runtime import guidance
from agent_py_agent.agent.backends.base import EchoBackend
from agent_py_agent.agent.gateway_parts import input_delivery_service as inputs
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
from agent_py_agent.agent.gateway_parts.multiprocess_admission import session_scope_for_request
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_claim import (
    freeze_coordinator_claim,
)
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_execution import (
    coordinator_execution_scope,
)
from agent_py_agent.agent.gateway_parts.queue_service import claim_request
from agent_py_agent.agent.gateway_parts.request_binding import (
    GatewayActiveTurnTransition,
    GatewayTaskBindingWriter,
)
from agent_py_agent.agent.gateway_parts.request_transactions import request_transaction_ports_scope
from agent_py_agent.agent.gateway_parts.request_worker import AdmissionLimits
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_gateway_c_http_wait import c_http, request
from agent_py_agent.tests.test_gateway_prepared_route_immutability import _fingerprint
from agent_py_agent.tests.test_runtime_guidance import _tool_loop_params


# LLM: 原真实Agent/C/Root和Store不替换；普通输入通过原prepare/queue/P3/claim，前台scope只包装实际原claim，不伪造lane或权限。
# 函数用途: 准备一个可由HTTP定位的真实本机主用户前台请求，不运行生产CLI或模型网络。
@contextmanager
def foreground(tmp_path, monkeypatch):
    with c_http(tmp_path, monkeypatch) as (agent, backend, gate, control, runtime, server, _close):
        thread = agent.conversation_store.threads.get_or_create(
            {
                "canonical_user_id": "local-agent",
                "channel": "chat",
                "channel_conversation_id": "active-cid",
                "channel_user_id": "local-agent",
            }
        )
        payload = {
            "id": "active-original",
            "request_id": "active-original",
            "kind": "ask",
            "goal": "处理原任务",
            "user_id": "local-agent",
            "conversation": {
                "enabled": True,
                "channel": "chat",
                "channel_conversation_id": "active-cid",
                "channel_user_id": "local-agent",
                "canonical_user_id": "local-agent",
            },
            "metadata": {
                "channel": "chat",
                "user_id": "local-agent",
                "client_input_digest": "active-original-digest",
            },
        }
        _fingerprint(payload)
        with inputs.gateway_input_transition(gate.paths, payload["id"]):
            receipt, _ = inputs.load_or_prepare_gateway_input_locked(
                gate.paths,
                request_id=payload["id"],
                client_input_digest="active-original-digest",
                client_message_id="",
                guidance_dedupe_key="",
                prepared_request=payload,
            )
            inputs.queue_gateway_input_locked(gate.paths, receipt)
        path = gate.paths.inbox / (payload["id"] + ".json")
        decision = gate.admit(
            path,
            (OwnerIdentity.local_main(), session_scope_for_request(receipt.prepared_request)),
            AdmissionLimits(8, 8),
        )
        assert decision["status"] == "admitted", decision
        claim = claim_request(
            gate.paths, path, coordinator_snapshot=freeze_coordinator_claim(gate, decision)
        )
        assert claim
        lane = agent.conversation_store.claims.acquire(
            {
                "thread_id": thread.thread_id,
                "task_id": "gateway:" + claim.request_id,
                "reason": "gateway_foreground_turn",
            }
        )
        assert lane
        with (
            coordinator_execution_scope(gate, claim, agent),
            request_transaction_ports_scope(agent),
        ):
            GatewayTaskBindingWriter(
                claim.path, claim.request_id, execution_attempt_id=claim.execution_attempt_id
            ).bind_conversation_claim(thread.thread_id)
            yield agent, backend, gate, control, runtime, server, claim, thread, lane


def send(server, *, message="active-new-message", **options):
    body = {
        "goal": "补充：保留全部证据再说明结果",
        "conversation_id": "active-cid",
        "metadata": {"message_id": message},
        **options,
    }
    return request(
        server, "POST", "/ask", body, headers={"X-User-Id": "local-agent", "X-Channel": "chat"}
    )


def source_for(agent, claim, thread):
    module = import_module("agent_py_agent.agent.gateway_parts.multiprocess_active_input")
    return module.pending_turn_input_source(agent, read_json_file(claim.path), thread.thread_id)


def params_for(agent, claim, thread):
    return _tool_loop_params(
        request_id=claim.request_id,
        attempt_id=claim.execution_attempt_id,
        task_id="",
        task_attributes={"conversation_thread_id": thread.thread_id},
        active_turn_transition_callback=GatewayActiveTurnTransition(
            claim.path, claim.request_id, claim.execution_attempt_id
        ),
        pending_turn_input_source=source_for(agent, claim, thread),
    )


def test_real_http_fresh_active_text_freezes_once_instead_of_foreign_route_or_queue(
    tmp_path, monkeypatch
):
    with foreground(tmp_path, monkeypatch) as (
        agent,
        backend,
        gate,
        _c,
        _r,
        server,
        claim,
        thread,
        lane,
    ):
        code, status = send(server)
        assert code == 202 and status["input_state"] == "pending", status
        receipt = inputs.read_gateway_input_receipt(gate.paths, status["request_id"])
        assert receipt.to_dict()["schema_version"] == "gateway_input_receipt.v3"
        assert receipt.transport["gateway_claim"] == {
            key: getattr(claim, key)
            for key in ("request_id", "execution_attempt_id", "lease_epoch", "lease_owner")
        }
        assert receipt.transport["target"]["claim_id"] == lane["claim_id"]
        assert (
            not (gate.paths.inbox / (receipt.request_id + ".json")).exists() and backend.calls == 0
        )
        before = inputs.gateway_input_receipt_path(gate.paths, receipt.request_id).read_bytes()
        assert send(server)[0] == 202
        assert (
            inputs.gateway_input_receipt_path(gate.paths, receipt.request_id).read_bytes() == before
        )
        assert agent.conversation_store.guidance.pending("request", claim.request_id) == []


def test_probe_then_original_safe_point_appends_and_original_provider_ack_consumes_once(
    tmp_path, monkeypatch
):
    with foreground(tmp_path, monkeypatch) as (
        agent,
        backend,
        gate,
        _c,
        _r,
        server,
        claim,
        thread,
        _lane,
    ):
        code, status = send(server)
        assert code == 202
        params = params_for(agent, claim, thread)
        path = inputs.gateway_input_receipt_path(gate.paths, status["request_id"])
        before = path.read_bytes()
        assert guidance.has_pending_turn_input(agent, params)
        assert (
            path.read_bytes() == before
            and agent.conversation_store.guidance.pending("request", claim.request_id) == []
        )
        assert guidance.inject_pending_turn_input(agent, params)
        receipt = inputs.read_gateway_input_receipt(gate.paths, status["request_id"])
        assert receipt.state == "active_pending" and receipt.target_turn_id == claim.request_id
        assert len(params.active_turn_user_inputs) == 1
        assert not guidance.has_pending_turn_input(agent, params)
        assert not guidance.inject_pending_turn_input(agent, params)
        assert len(params.active_turn_user_inputs) == 1
        assert (
            guidance.mark_injected_turn_input_submitted(agent, params, provider_call_id="fake-call")
            == 1
        )
        response = EchoBackend().generate("仅测试已构造的补充输入")
        assert response.text
        assert guidance.acknowledge_injected_turn_input(agent, params) == 1
        assert (
            inputs.reconcile_gateway_input_request(
                gate.paths,
                request_id=receipt.request_id,
                conversation_store=agent.conversation_store,
            )
            == "consumed"
        )
        assert len(agent.conversation_store.messages.recent(thread.thread_id)) >= 1
        assert not guidance.has_pending_turn_input(agent, params) and backend.calls == 0


# LLM: 媒体本身已可插话，此处带媒体同时改 prompt_files 仍必须完整排队，不能丢图变活动纯文。
# 函数用途: 锁定不等价执行选项的原队列行为及字段完整性。
@pytest.mark.parametrize(
    "options",
    [
        {"input_media": [{"kind": "image", "ref": "synthetic-ref"}], "prompt_files": ["a.txt"]},
        {"prompt_files": ["a.txt"]},
        {"inject": ["保持格式"]},
        {"save": False},
        {"include_prompt": True},
        {"client_capabilities": {"tool_approval": True}},
        {"workspace": {"root": "somewhere"}},
    ],
)
def test_non_text_or_inequivalent_options_keep_complete_original_queue(
    tmp_path, monkeypatch, options
):
    with foreground(tmp_path, monkeypatch) as (
        _agent,
        _b,
        gate,
        _c,
        _r,
        server,
        _claim,
        _thread,
        _lane,
    ):
        code, status = send(server, **options)
        assert code == 202 and status["input_state"] == "queued", status
        receipt = inputs.read_gateway_input_receipt(gate.paths, status["request_id"])
        assert getattr(receipt, "transport", None) is None
        assert (
            read_json_file(gate.paths.inbox / (receipt.request_id + ".json"))
            == receipt.prepared_request
        )
        for key, value in options.items():
            if key == "client_capabilities":
                assert all(
                    receipt.prepared_request[key][name] == setting
                    for name, setting in value.items()
                )
            else:
                assert receipt.prepared_request[key] == value


def test_explicit_defaults_remain_active_eligible(tmp_path, monkeypatch):
    with foreground(tmp_path, monkeypatch) as (_a, _b, _g, _c, _r, server, _claim, _thread, _lane):
        code, status = send(
            server,
            save=True,
            include_prompt=False,
            inject=[],
            prompt_files=[],
            input_media=[],
            client_capabilities={"tool_approval": False, "rich_transcript": False},
        )
        assert code == 202 and status["input_state"] == "pending", status


def test_frozen_pending_transport_is_not_queued_by_legacy_authoritative_miss(tmp_path, monkeypatch):
    with foreground(tmp_path, monkeypatch) as (
        agent,
        _b,
        gate,
        _c,
        _r,
        server,
        _claim,
        _thread,
        _lane,
    ):
        code, status = send(server)
        assert code == 202
        path = inputs.gateway_input_receipt_path(gate.paths, status["request_id"])
        before = path.read_bytes()
        assert (
            inputs.reconcile_gateway_input_request(
                gate.paths,
                request_id=status["request_id"],
                conversation_store=agent.conversation_store,
            )
            == "pending"
        )
        assert path.read_bytes() == before and not (gate.paths.inbox / path.name).exists()


@pytest.mark.parametrize(
    "failure", ["corrupt", "missing", "legacy", "target_claim", "transport_epoch"]
)
def test_probe_never_reports_idle_for_unobservable_or_replaced_transport(
    tmp_path, monkeypatch, failure
):
    with foreground(tmp_path, monkeypatch) as (
        agent,
        _b,
        gate,
        _c,
        _r,
        server,
        claim,
        thread,
        _lane,
    ):
        code, status = send(server)
        assert code == 202
        params = params_for(agent, claim, thread)
        path = inputs.gateway_input_receipt_path(gate.paths, status["request_id"])
        if failure == "corrupt":
            path.write_text("{")
        elif failure == "legacy":
            row = read_json_file(path)
            row["schema_version"] = "gateway_input_receipt.v2"
            write_json_file_atomic(path, row)
        elif failure == "missing":
            original = inputs.read_gateway_input_receipt

            def absent(paths, identifier, **kwargs):
                return (
                    None
                    if identifier == status["request_id"]
                    else original(paths, identifier, **kwargs)
                )

            monkeypatch.setattr(inputs, "read_gateway_input_receipt", absent)
        elif failure == "target_claim":
            cp = agent.conversation_store.storage.background_claim_path(thread.thread_id)
            row = read_json_file(cp)
            row["claim_id"] = "different-original-lane"
            write_json_file_atomic(cp, row)
        else:
            row = read_json_file(path)
            row["transport"]["gateway_claim"]["lease_epoch"] = True
            write_json_file_atomic(path, row)
        before = path.read_bytes()
        with pytest.raises(inputs.DataCorruptionError):
            guidance.has_pending_turn_input(agent, params)
        assert path.read_bytes() == before


def test_presentation_only_turn_does_not_drain_or_claim_new_input(tmp_path, monkeypatch):
    with foreground(tmp_path, monkeypatch) as (
        agent,
        _b,
        gate,
        _c,
        _r,
        server,
        claim,
        thread,
        _lane,
    ):
        code, status = send(server)
        assert code == 202
        params = replace(params_for(agent, claim, thread), consume_pending_turn_input=False)
        path = inputs.gateway_input_receipt_path(gate.paths, status["request_id"])
        before = path.read_bytes()
        assert not guidance.has_pending_turn_input(agent, params)
        assert not guidance.inject_pending_turn_input(agent, params)
        assert path.read_bytes() == before and not agent.conversation_store.guidance.pending(
            "request", claim.request_id
        )


def test_append_then_receipt_busy_replays_original_guidance_without_duplicate(
    tmp_path, monkeypatch
):
    with foreground(tmp_path, monkeypatch) as (
        agent,
        _b,
        gate,
        _c,
        _r,
        server,
        claim,
        thread,
        _lane,
    ):
        code, status = send(server)
        assert code == 202
        params = params_for(agent, claim, thread)
        module = import_module("agent_py_agent.agent.gateway_parts.multiprocess_active_input")
        writer = module.write_json_file_atomic
        error = BlockingIOError("synthetic busy")
        with monkeypatch.context() as change:
            change.setattr(
                module,
                "write_json_file_atomic",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
            )
            with pytest.raises(BlockingIOError) as caught:
                params.pending_turn_input_source.drain(agent, params)
            assert caught.value is error
        receipt = inputs.read_gateway_input_receipt(gate.paths, status["request_id"])
        assert receipt.state == "pending"
        original = agent.conversation_store.guidance.receipt(receipt.guidance_dedupe_key)
        assert (
            original
            and len(agent.conversation_store.guidance.pending("request", claim.request_id)) == 1
        )
        assert guidance.inject_pending_turn_input(agent, params)
        assert (
            agent.conversation_store.guidance.receipt(receipt.guidance_dedupe_key).entry.guidance_id
            == original.entry.guidance_id
        )
        assert len(params.active_turn_user_inputs) == 1


def test_count0_no_source_queries_do_not_touch_input_files(monkeypatch):
    module = import_module("agent_py_agent.agent.gateway_parts.multiprocess_active_input")

    def forbidden(*_args, **_kwargs):
        pytest.fail("Count0 input IO")

    monkeypatch.setattr(module.inputs, "read_gateway_input_receipt", forbidden)
    monkeypatch.setattr(module, "target_record", forbidden)
    assert module.pending_turn_input_source(None, {}, "") is None
    from agent_py_agent.agent.common.request_execution_ports import current_request_execution_source

    assert current_request_execution_source("count0.read") is None


@pytest.mark.parametrize("status", ["finished", "failed", "cancelled"])
def test_original_canonical_finished_lane_proves_idle_and_keeps_queue_options(
    tmp_path, monkeypatch, status
):
    with foreground(tmp_path, monkeypatch) as (
        agent,
        _b,
        gate,
        _c,
        _r,
        server,
        _claim,
        thread,
        lane,
    ):
        result = agent.conversation_store.claims.finish(
            {"thread_id": thread.thread_id, "claim_id": lane["claim_id"], "status": status}
        )
        assert result["status"] == status
        code, payload = send(server)
        assert code == 202 and payload["input_state"] == "queued", payload
        receipt = inputs.read_gateway_input_receipt(gate.paths, payload["request_id"])
        assert getattr(receipt, "transport", None) is None


def test_expired_running_lane_remains_unknown_without_rebinding_or_queue(tmp_path, monkeypatch):
    with foreground(tmp_path, monkeypatch) as (
        agent,
        _b,
        gate,
        _c,
        _r,
        server,
        _claim,
        thread,
        _lane,
    ):
        path = agent.conversation_store.storage.background_claim_path(thread.thread_id)
        row = read_json_file(path)
        row["expires_at"] = 1.0
        write_json_file_atomic(path, row)
        code, status = send(server)
        assert code == 202 and status["input_state"] == "pending", status
        receipt = inputs.read_gateway_input_receipt(gate.paths, status["request_id"])
        assert (
            getattr(receipt, "transport", None) is None
            and not (gate.paths.inbox / (receipt.request_id + ".json")).exists()
        )


def test_whitespace_text_uses_original_guidance_normalization_and_preserves_full_prepared(
    tmp_path, monkeypatch
):
    with foreground(tmp_path, monkeypatch) as (
        agent,
        _b,
        gate,
        _c,
        _r,
        server,
        claim,
        thread,
        _lane,
    ):
        raw = "  合成补充，保留结构化证据。\n "
        code, status = request(
            server,
            "POST",
            "/ask",
            {
                "goal": raw,
                "conversation_id": "active-cid",
                "metadata": {"message_id": "space-input"},
            },
            headers={"X-User-Id": "local-agent", "X-Channel": "chat"},
        )
        assert code == 202
        params = params_for(agent, claim, thread)
        assert guidance.inject_pending_turn_input(agent, params)
        receipt = inputs.read_gateway_input_receipt(gate.paths, status["request_id"])
        assert receipt.prepared_request["goal"] == raw
        row = agent.conversation_store.guidance.receipt(receipt.guidance_dedupe_key)
        assert row.entry.message == raw.strip()
        assert row.entry.metadata["gateway_input_transport"] == receipt.transport


def test_c_and_safe_point_reject_changed_guidance_entry_even_when_digest_is_unchanged(
    tmp_path, monkeypatch
):
    with foreground(tmp_path, monkeypatch) as (
        agent,
        _b,
        gate,
        _c,
        _r,
        server,
        claim,
        thread,
        _lane,
    ):
        code, status = send(server)
        assert code == 202
        params = params_for(agent, claim, thread)
        params.pending_turn_input_source.drain(agent, params)
        receipt = inputs.read_gateway_input_receipt(gate.paths, status["request_id"])
        path = agent.conversation_store.storage.guidance_dedupe_path(receipt.guidance_dedupe_key)
        row = read_json_file(path)
        digest = row["input_digest"]
        row["entry"]["metadata"]["gateway_input_transport"]["gateway_claim"]["lease_epoch"] += 1
        write_json_file_atomic(path, row)
        assert read_json_file(path)["input_digest"] == digest
        with pytest.raises(inputs.DataCorruptionError):
            guidance.has_pending_turn_input(agent, params)
        code, payload = send(server)
        assert code == 503 and payload["terminal"] is False


def test_probe_reads_all_own_refs_beyond_consumed_prefix_without_shared_history_scan(
    tmp_path, monkeypatch
):
    with foreground(tmp_path, monkeypatch) as (
        agent,
        _b,
        gate,
        _control,
        _r,
        server,
        claim,
        thread,
        _lane,
    ):
        ids = []
        for index in range(65):
            code, status = send(server, message="prior-own-" + str(index).zfill(3))
            assert code == 202
            ids.append(status["request_id"])
        params = params_for(agent, claim, thread)
        assert guidance.inject_pending_turn_input(agent, params)
        accepted = 0
        while accepted < 65:
            submitted = guidance.mark_injected_turn_input_submitted(
                agent, params, provider_call_id="prior-fake-" + str(accepted)
            )
            assert submitted > 0
            assert guidance.acknowledge_injected_turn_input(agent, params) == submitted
            accepted += submitted
            if accepted < 65:
                assert guidance.inject_pending_turn_input(agent, params)
        assert accepted == 65
        for request_id in ids:
            assert (
                inputs.reconcile_gateway_input_request(
                    gate.paths, request_id=request_id, conversation_store=agent.conversation_store
                )
                == "consumed"
            )
        import hashlib

        from agent_py_agent.agent.gateway_parts.http_handlers import (
            _http_idempotent_request_identity,
        )

        boundary = max(hashlib.sha256(item.encode()).hexdigest() for item in ids)
        for candidate in range(100000):
            message = "own-pending-after-prefix-" + str(candidate)
            predicted, _digest = _http_idempotent_request_identity(
                {
                    "goal": "补充：保留全部证据再说明结果",
                    "conversation_id": "active-cid",
                    "metadata": {"message_id": message},
                },
                goal="补充：保留全部证据再说明结果",
                user_id="local-agent",
                channel="chat",
            )
            if hashlib.sha256(predicted.encode()).hexdigest() > boundary:
                break
        else:
            pytest.fail("no deterministic tail fixture found")
        code, status = send(server, message=message)
        assert status["request_id"] == predicted
        assert hashlib.sha256(status["request_id"].encode()).hexdigest() > boundary
        assert code == 202
        reads = []
        reader = inputs.read_gateway_input_receipt

        def observe(*args, **kwargs):
            reads.append(args[1])
            return reader(*args, **kwargs)

        monkeypatch.setattr(inputs, "read_gateway_input_receipt", observe)
        assert guidance.has_pending_turn_input(agent, params)
        assert len(reads) == 66 and set(reads) == set(ids + [status["request_id"]])
        assert guidance.inject_pending_turn_input(agent, params)
        latest = inputs.read_gateway_input_receipt(gate.paths, status["request_id"])
        guidance_id = agent.conversation_store.guidance.receipt(
            latest.guidance_dedupe_key
        ).entry.guidance_id
        assert any(guidance_id in row["input_ids"] for row in params.active_turn_user_inputs)


@pytest.mark.parametrize("kind", ["common", "context", "coordinator", "bounded"])
def test_original_typed_mi_parse_failures_survive_probe_and_input_settlement(
    tmp_path, monkeypatch, kind
):
    with foreground(tmp_path, monkeypatch) as (
        agent,
        _b,
        gate,
        _c,
        _r,
        server,
        claim,
        thread,
        _lane,
    ):
        code, status = send(server)
        assert code == 202
        params = params_for(agent, claim, thread)
        params.pending_turn_input_source.drain(agent, params)
        from agent_py_agent.agent.backends.bounded_call import BoundedCallExitUnconfirmedError
        from agent_py_agent.agent.common.request_execution_ports import (
            RequestExecutionContextUnavailable,
            RequestExecutionOutcomeUncertainError,
        )
        from agent_py_agent.agent.conversation.store_guidance_records import GuidanceOnceReceipt
        from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import (
            CoordinatorUnavailable,
        )

        base = {
            "common": RequestExecutionOutcomeUncertainError,
            "context": RequestExecutionContextUnavailable,
            "coordinator": CoordinatorUnavailable,
            "bounded": BoundedCallExitUnconfirmedError,
        }[kind]
        cls = type("CanonicalOriginalParseFailure", (base, ValueError), {})
        error = (
            cls(read_json_file(claim.path), "original-parse")
            if kind == "common"
            else cls("synthetic-call", "worker_exit_unconfirmed")
            if kind == "bounded"
            else cls("original-parse")
        )

        def reject(*_args, **_kwargs):
            raise error

        monkeypatch.setattr(GuidanceOnceReceipt, "from_dict", reject)
        path = inputs.gateway_input_receipt_path(gate.paths, status["request_id"])
        before = path.read_bytes()
        with pytest.raises(base) as caught:
            guidance.has_pending_turn_input(agent, params)
        assert caught.value is error
        with pytest.raises(base) as caught:
            inputs.reconcile_gateway_input_request(
                gate.paths,
                request_id=status["request_id"],
                conversation_store=agent.conversation_store,
            )
        assert caught.value is error and path.read_bytes() == before


def test_native_quote_references_stay_in_complete_queue_instead_of_text_only_packet(
    tmp_path, monkeypatch
):
    with foreground(tmp_path, monkeypatch) as (
        _agent,
        _b,
        gate,
        _c,
        _r,
        server,
        _claim,
        _thread,
        _lane,
    ):
        quoted = {
            "schema": "quoted_message_references.v1",
            "channel": "feishu",
            "chat_id": "synthetic-chat",
            "current_message_id": "synthetic-message",
            "references": [{"ref": "synthetic-ref", "message_id": "om_synthetic"}],
        }
        code, status = send(
            server,
            metadata={"message_id": "native-quote-input", "quoted_message_references": quoted},
        )
        assert code == 202 and status["input_state"] == "queued", status
        receipt = inputs.read_gateway_input_receipt(gate.paths, status["request_id"])
        assert getattr(receipt, "transport", None) is None
        assert receipt.prepared_request["metadata"]["quoted_message_references"] == quoted
        assert (
            read_json_file(gate.paths.inbox / (receipt.request_id + ".json"))
            == receipt.prepared_request
        )


def test_original_claimed_request_pipeline_installs_source_and_consumes_arrival_during_fake_model(
    tmp_path, monkeypatch
):
    from agent_py_agent.agent.backends.base import ModelResponse
    from agent_py_agent.agent.gateway_parts.request_worker import (
        _process_claimed_gateway_request_path,
    )

    with c_http(tmp_path, monkeypatch) as (
        agent,
        backend,
        gate,
        _control,
        _runtime,
        server,
        _close,
    ):
        initial = {
            "goal": "原始合成前台工作，请只说明结果",
            "conversation_id": "active-cid",
            "metadata": {"message_id": "original-pipeline"},
        }
        code, accepted = request(
            server,
            "POST",
            "/ask",
            initial,
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
        assert decision["status"] == "admitted", decision
        claim = claim_request(
            gate.paths, path, coordinator_snapshot=freeze_coordinator_claim(gate, decision)
        )
        assert claim
        calls, ingress = [], []

        def generate(_backend, prompt, **options):
            calls.append((prompt, options.get("messages")))
            if len(calls) == 1:
                status, response = send(server, message="arrives-during-original-provider")
                assert status == 202 and response["input_state"] == "pending", response
                ingress.append(response["request_id"])
                return ModelResponse(text="原回复尚未采用。", backend="echo")
            return ModelResponse(text="已按补充要求保留证据并说明结果。", backend="echo")

        monkeypatch.setattr(EchoBackend, "generate", generate)
        with coordinator_execution_scope(gate, claim, agent):
            finished = _process_claimed_gateway_request_path(
                agent, gate.paths, claim.path, claim.lease_owner, claim=claim
            )
        assert finished and len(calls) == 2 and len(ingress) == 1
        result = read_json_file(gate.paths.responses / (claim.request_id + ".json"))
        assert result["ok"], result
        assert "补充：保留全部证据再说明结果" in json.dumps(calls[1], ensure_ascii=False)
        delivered = inputs.read_gateway_input_receipt(gate.paths, ingress[0])
        assert delivered.state == "consumed" and delivered.target_turn_id == claim.request_id
        assert not (gate.paths.inbox / (delivered.request_id + ".json")).exists()
        assert (gate.paths.terminal / (claim.request_id + ".json")).exists()
        assert backend.calls == 0
