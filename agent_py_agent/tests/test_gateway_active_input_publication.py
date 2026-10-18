"""原C/W输入投影、正常live封口和普通资源竞争；只用临时真实对象与测试回环。"""

import hashlib
import threading
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.request_execution_ports import (
    RequestExecutionOutcomeUncertainError,
)
from agent_py_agent.agent.gateway_parts import input_delivery_service as inputs
from agent_py_agent.agent.gateway_parts import multiprocess_input_receipt as producer
from agent_py_agent.agent.gateway_parts.io import (
    _locked_json_path,
    read_json_file,
    write_json_file_atomic,
)
from agent_py_agent.agent.gateway_parts.multiprocess_admission import reservation_file
from agent_py_agent.agent.gateway_parts.multiprocess_input_reconcile import (
    reconcile_coordinator_queued_inputs,
)
from agent_py_agent.tests.test_gateway_active_input_transport import (
    foreground,
    guidance,
    params_for,
    send,
    source_for,
)
from agent_py_agent.tests.test_gateway_cold_request_pump import queue_request


def ref_path(paths, receipt, turn_id):
    return (
        paths.root
        / "input_receipts/by_turn"
        / hashlib.sha256(turn_id.encode()).hexdigest()
        / (hashlib.sha256(receipt.request_id.encode()).hexdigest() + ".json")
    )


# LLM: 只持本测试合成路径的原合法锁，finally释放并确认原Thread退出；不修改canonical或伪造权限。
# 函数用途: 让断言在普通资源仍被占有时执行，避免watchdog先释放造成假通过。
@contextmanager
def held_lock(lock):
    entered, release = threading.Event(), threading.Event()
    errors = []

    def hold():
        try:
            with lock:
                entered.set()
                release.wait(10)
        except BaseException as error:
            errors.append(error)
            entered.set()

    thread = threading.Thread(target=hold)
    thread.start()
    assert entered.wait(5)
    assert not errors
    try:
        yield release
    finally:
        release.set()
        thread.join(5)
        assert not thread.is_alive()
        assert not errors


def test_c_publishes_locator_before_transport_without_claiming_binding(tmp_path, monkeypatch):
    with foreground(tmp_path, monkeypatch) as (
        agent,
        _b,
        gate,
        _c,
        _r,
        server,
        claim,
        _thread,
        _lane,
    ):
        original = producer.write_json_file_atomic
        observed = []

        def observe(path, payload, **options):
            if path.parent.name == "input_receipts" and payload.get("transport"):
                receipt = inputs.GatewayInputReceipt.from_dict(payload)
                hint = read_json_file(ref_path(gate.paths, receipt, claim.request_id))
                previous = read_json_file(path)
                observed.append((hint, previous))
                assert hint["target_turn_id"] == claim.request_id
                assert not payload["target_turn_id"] and "transport" not in previous
                assert not agent.conversation_store.guidance.receipt(receipt.guidance_dedupe_key)
            return original(path, payload, **options)

        monkeypatch.setattr(producer, "write_json_file_atomic", observe)
        code, status = send(server)
        assert code == 202 and status["input_state"] == "pending"
        assert len(observed) == 1


def test_live_finish_queues_never_appended_input_without_releasing_original_p3(
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
        receipt = inputs.read_gateway_input_receipt(gate.paths, status["request_id"])
        p3 = reservation_file(gate.paths, claim.request_id)
        before = p3.read_bytes()
        source = source_for(agent, claim, thread)
        source.finish(agent)
        after = inputs.read_gateway_input_receipt(gate.paths, receipt.request_id)
        assert after.state == "queued" and after.prepared_request == receipt.prepared_request
        assert (
            read_json_file(gate.paths.inbox / (receipt.request_id + ".json"))
            == receipt.prepared_request
        )
        assert read_json_file(claim.path)["turn_phase"] == "closing"
        assert (
            p3.read_bytes() == before
            and not (gate.paths.terminal / (claim.request_id + ".json")).exists()
        )
        params = params_for(agent, claim, thread)
        with pytest.raises(RequestExecutionOutcomeUncertainError):
            source.probe(agent, params)
        with pytest.raises(RequestExecutionOutcomeUncertainError):
            source.drain(agent, params)
        code, later = send(server, message="after-real-closing")
        assert code == 202 and later["input_state"] == "queued"
        assert inputs.read_gateway_input_receipt(gate.paths, later["request_id"]).transport is None


@pytest.mark.parametrize("stage", ["appended", "reserved", "submitted", "consumed"])
def test_finish_never_requeues_guidance_that_may_have_entered_model(tmp_path, monkeypatch, stage):
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
        if stage in {"reserved", "submitted", "consumed"}:
            assert guidance.inject_pending_turn_input(agent, params)
        if stage in {"submitted", "consumed"}:
            assert (
                guidance.mark_injected_turn_input_submitted(
                    agent, params, provider_call_id="fake-submission"
                )
                == 1
            )
        if stage == "consumed":
            assert guidance.acknowledge_injected_turn_input(agent, params) == 1
        path = inputs.gateway_input_receipt_path(gate.paths, status["request_id"])
        before = path.read_bytes()
        source_for(agent, claim, thread).finish(agent)
        assert path.read_bytes() == before
        assert not (gate.paths.inbox / (status["request_id"] + ".json")).exists()


def test_own_scope_does_not_read_other_turn_receipts_even_while_they_are_locked(
    tmp_path, monkeypatch
):
    with foreground(tmp_path, monkeypatch) as (
        agent,
        _b,
        gate,
        control,
        _r,
        server,
        claim,
        thread,
        _lane,
    ):
        others = [
            queue_request(control, "other-turn-input-" + str(i), local=True) for i in range(65)
        ]
        for item in others:
            inputs._write_active_turn_ref(gate.paths, item, target_turn_id="another-turn")
        other = others[0]
        other_path = inputs.gateway_input_receipt_path(gate.paths, other.request_id)
        inputs._write_active_turn_ref(gate.paths, other, target_turn_id="another-turn")
        code, status = send(server)
        assert code == 202
        reads = []
        reader = inputs.read_gateway_input_receipt

        def observe(paths, request_id, **options):
            reads.append(request_id)
            assert request_id != other.request_id
            return reader(paths, request_id, **options)

        monkeypatch.setattr(inputs, "read_gateway_input_receipt", observe)
        with held_lock(_locked_json_path(other_path)) as release:
            params = params_for(agent, claim, thread)
            assert guidance.has_pending_turn_input(agent, params)
            assert guidance.inject_pending_turn_input(agent, params)
            assert not release.is_set()
        assert reads and set(reads) == {status["request_id"]}


def test_original_c_scan_repairs_existing_v3_hint_without_settling_or_reading_agent(
    tmp_path, monkeypatch
):
    with foreground(tmp_path, monkeypatch) as (
        agent,
        _b,
        gate,
        control,
        runtime,
        server,
        claim,
        _thread,
        _lane,
    ):
        code, status = send(server)
        assert code == 202
        receipt = inputs.read_gateway_input_receipt(gate.paths, status["request_id"])
        hint = ref_path(gate.paths, receipt, claim.request_id)
        hint.unlink()  # 仅合成自己的lookup投影中断，不碰canonical或他人数据。
        original = inputs.gateway_input_receipt_path(gate.paths, receipt.request_id).read_bytes()
        assert gate._ordinary_admission_origin[0] is runtime
        context = SimpleNamespace(
            agent=agent,
            paths=gate.paths,
            control_plane=control,
            drain_runtime=runtime,
            coordinator_worker_count=control.worker_count,
        )
        result = reconcile_coordinator_queued_inputs(context, gate.paths, gate)
        assert result["deferred"] == 1 and hint.exists()
        assert (
            inputs.gateway_input_receipt_path(gate.paths, receipt.request_id).read_bytes()
            == original
        )
        assert not agent.conversation_store.guidance.pending("request", claim.request_id)
        assert not (gate.paths.inbox / (receipt.request_id + ".json")).exists()


def test_pre_locator_then_transient_canonical_busy_retries_same_frozen_target(
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
        original = producer.write_json_file_atomic
        attempts = []

        def busy_once(path, payload, **options):
            if path.parent.name == "input_receipts" and payload.get("transport"):
                attempts.append(payload["transport"])
                receipt = inputs.GatewayInputReceipt.from_dict(payload)
                assert ref_path(gate.paths, receipt, claim.request_id).exists()
                if len(attempts) == 1:
                    raise BlockingIOError("ordinary synthetic contention before canonical write")
            return original(path, payload, **options)

        monkeypatch.setattr(producer, "write_json_file_atomic", busy_once)
        code, incoming = send(server)
        assert code == 202 and incoming["input_state"] == "pending"
        assert len(attempts) == 2 and attempts[0] == attempts[1]
        params = params_for(agent, claim, thread)
        assert guidance.inject_pending_turn_input(agent, params)
        row = inputs.read_gateway_input_receipt(gate.paths, incoming["request_id"])
        assert (
            agent.conversation_store.guidance.receipt(row.guidance_dedupe_key).status == "reserved"
        )
        assert len(params.active_turn_user_inputs) == 1


def test_c_publishes_before_w_closes_under_the_same_original_target_lock(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import request_binding

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
        published, release, close_attempt = threading.Event(), threading.Event(), threading.Event()
        original_write = producer.write_json_file_atomic
        original_turn = request_binding.gateway_turn_transition
        caller = threading.get_ident()
        results, errors = [], []

        def pause(path, payload, **options):
            if path.parent.name == "input_receipts" and payload.get("transport"):
                published.set()
                assert release.wait(5)
            return original_write(path, payload, **options)

        @contextmanager
        def observe_turn(paths, request_id):
            if threading.get_ident() == caller and request_id == claim.request_id:
                close_attempt.set()
            with original_turn(paths, request_id):
                yield

        monkeypatch.setattr(producer, "write_json_file_atomic", pause)
        monkeypatch.setattr(request_binding, "gateway_turn_transition", observe_turn)

        def sender():
            try:
                results.append(send(server, message="c-first-publication"))
            except BaseException as error:
                errors.append(error)

        def allow_close():
            assert close_attempt.wait(5)
            release.set()

        sender_thread = threading.Thread(target=sender)
        releaser = threading.Thread(target=allow_close)
        sender_thread.start()
        try:
            assert published.wait(5)
            releaser.start()
            source_for(agent, claim, thread).finish(agent)
        finally:
            release.set()
            close_attempt.set()
            sender_thread.join(5)
            if releaser.ident is not None:
                releaser.join(5)
        assert not sender_thread.is_alive() and not releaser.is_alive() and not errors
        assert len(results) == 1 and results[0][0] == 202
        receipt = inputs.read_gateway_input_receipt(gate.paths, results[0][1]["request_id"])
        assert (
            receipt.state == "queued"
            and receipt.transport["gateway_claim"]["request_id"] == claim.request_id
        )
        assert (
            read_json_file(gate.paths.inbox / (receipt.request_id + ".json"))
            == receipt.prepared_request
        )


@pytest.mark.parametrize("with_media", [False, True])
def test_w_closes_before_c_selects_active_so_fresh_input_is_complete_queue(
    tmp_path, monkeypatch, with_media
):
    from agent_py_agent.agent.gateway_parts import multiprocess_active_input as active

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
        closed, entered = threading.Event(), threading.Event()
        results, errors = [], []
        original_update = active.update_json_file_atomic
        def hold_closing(path, updater, **options):
            result = original_update(path, updater, **options)
            if path == claim.path and result.get("turn_phase") == "closing":
                closed.set()
                assert entered.wait(5)
            return result

        monkeypatch.setattr(active, "update_json_file_atomic", hold_closing)

        def sender():
            try:
                assert closed.wait(5)
                # 请求已准备发起；不等待持Root门阻塞的HTTP准入，封口释放后仍走完整真实HTTP链。
                entered.set()
                media = [{"kind": "image", "ref": "synthetic-original"}] if with_media else []
                options = {"input_media": media} if with_media else {}
                results.append(send(server, message="w-first-closing", **options))
            except BaseException as error:
                errors.append(error)

        sender_thread = threading.Thread(target=sender)
        sender_thread.start()
        try:
            source_for(agent, claim, thread).finish(agent)
        finally:
            closed.set()
            entered.set()
            sender_thread.join(5)
        assert not sender_thread.is_alive() and not errors
        assert (
            len(results) == 1 and results[0][0] == 202 and results[0][1]["input_state"] == "queued"
        )
        receipt = inputs.read_gateway_input_receipt(gate.paths, results[0][1]["request_id"])
        assert receipt.transport is None
        expected_media = [{"kind": "image", "ref": "synthetic-original"}] if with_media else []
        assert receipt.prepared_request.get("input_media", []) == expected_media
        assert (
            read_json_file(gate.paths.inbox / (receipt.request_id + ".json"))
            == receipt.prepared_request
        )
