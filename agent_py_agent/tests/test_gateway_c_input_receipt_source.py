"""原C/Root/pool实际来源与完整receipt CAS；无W/模型或日常数据，HTTP节点仅本测试loopback。"""

import copy
from importlib import import_module
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.request_execution_ports import RequestExecutionContextUnavailable
from agent_py_agent.agent.gateway_parts import http_handlers, request_worker
from agent_py_agent.agent.gateway_parts import input_delivery_service as inputs
from agent_py_agent.agent.gateway_parts.control_service import (
    GatewayControlScope,
    active_turn_guidance_dedupe_key,
)
from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
from agent_py_agent.tests.test_gateway_c_http_wait import body, c_http, request
from agent_py_agent.tests.test_gateway_http_current_work import http_case, run_work
from agent_py_agent.tests.test_gateway_prepared_route_immutability import _fingerprint, _request
from agent_py_agent.tests.test_gateway_prepared_route_immutability import context as context


# LLM: 原pool按既有策略捕获Exception并丢finish返回值；测试桥仅收集原operation结果/同一异常，不能把被pool吞掉的断言当通过。
# 函数用途: 在实际原worker运行后让测试线程检查真实操作结果，finally仍由原pool关闭。
def run_receipt_work(lower, socket, operation):
    result, errors = [], []

    def invoke():
        try:
            result.append(operation())
        except BaseException as error:
            errors.append(error)

    run_work(lower, socket, invoke)
    if errors:
        raise errors[0]
    assert len(result) == 1
    return result[0]


def component():
    return import_module("agent_py_agent.agent.gateway_parts.multiprocess_input_receipt")


# LLM: 测试输入用原HTTP身份与guidance公式，不以手编ID绕source/replay核验；同一prepared原指纹随ID更新。
# 函数用途: 为原C固定写口构造彼此匹配的认证peer、消息与完整准备参数。
def arguments():
    prepared = _request()
    user_id = prepared["user_id"]
    channel = prepared["metadata"]["channel"]
    conversation_id = prepared["conversation"]["id"]
    message_id = prepared["metadata"]["channel_message_id"]
    request_id, _ = http_handlers.http_input_identity(
        user_id=user_id, channel=channel, conversation_id=conversation_id, message_id=message_id
    )
    prepared.update(id=request_id, request_id=request_id)
    _fingerprint(prepared)
    scope = GatewayControlScope(user_id, channel, conversation_id, metadata={"message_id": message_id})
    return dict(
        request_id=request_id,
        client_input_digest=prepared["metadata"]["client_input_digest"],
        client_message_id=message_id,
        guidance_dedupe_key=active_turn_guidance_dedupe_key(scope),
        prepared_request=prepared,
    )


def upper(lower, runtime, control):
    return SimpleNamespace(
        server=lower, control_plane=control, drain_runtime=runtime, paths=control.paths
    )


def test_actual_authenticated_c_http_never_routes_through_foreign_agent(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("ordinary C delegated to foreign business Agent")

    monkeypatch.setattr(http_handlers, "_route_ask_to_active_turn", forbidden)
    monkeypatch.setattr(request_worker, "_resolve_request_agent", forbidden)
    with c_http(tmp_path, monkeypatch) as (agent, backend, gate, control, runtime, server, _close):
        server.agent = agent
        code, payload = request(server, "POST", "/ask", body())
        assert code == 202 and payload["input_state"] == "queued"
        row = inputs.read_gateway_input_receipt(gate.paths, payload["request_id"])
        assert (
            row.transport is None
            and read_json_file(gate.paths.inbox / (row.request_id + ".json"))
            == row.prepared_request
        )
        assert backend.calls == 0


def test_original_work_prepare_queue_and_replay_preserve_same_prepared(context, monkeypatch):
    with http_case(context, monkeypatch) as (lower, runtime, _group, socket, handler):

        def finish():
            source = component().CoordinatorInputReceiptSource(
                handler, upper(lower, runtime, context)
            )
            receipt, created = source.prepare(inputs.GatewayInputPreparation(**arguments()))
            assert created
            queued = source.queue_unbound(receipt)
            assert queued.prepared_request == receipt.prepared_request
            replay, created = source.prepare(inputs.GatewayInputPreparation(**arguments()))
            assert not created and replay == queued
            return queued

        queued = run_receipt_work(lower, socket, finish)
        assert (
            read_json_file(context.paths.inbox / (queued.request_id + ".json"))
            == queued.prepared_request
        )


@pytest.mark.parametrize("drift", ["copy", "upper", "runtime", "closing", "late"])
def test_source_copy_swap_and_exit_do_not_write(context, monkeypatch, drift):
    with http_case(context, monkeypatch) as (lower, runtime, _group, socket, handler):
        saved = []

        def finish():
            server = upper(lower, runtime, context)
            source = component().CoordinatorInputReceiptSource(handler, server)
            receipt, _ = source.prepare(inputs.GatewayInputPreparation(**arguments()))
            saved.append((source, receipt))
            if drift == "late":
                return
            if drift == "copy":
                source = copy.copy(source)
            elif drift == "upper":
                server.server = copy.copy(lower)
            elif drift == "runtime":
                server.drain_runtime = copy.copy(runtime)
            else:
                runtime.stop_producers()
            with pytest.raises((ValueError, RequestExecutionContextUnavailable)):
                source.queue_unbound(receipt)

        run_receipt_work(lower, socket, finish)
        if drift == "late":
            with pytest.raises((ValueError, RequestExecutionContextUnavailable)):
                saved[0][0].queue_unbound(saved[0][1])
        assert not list(context.paths.inbox.glob("*.json"))


@pytest.mark.parametrize("field", ["goal", "state", "metadata", "media"])
def test_complete_receipt_cas_cannot_accept_partial_identity(context, monkeypatch, field):
    with http_case(context, monkeypatch) as (lower, runtime, _group, socket, handler):

        def finish():
            source = component().CoordinatorInputReceiptSource(
                handler, upper(lower, runtime, context)
            )
            receipt, _ = source.prepare(inputs.GatewayInputPreparation(**arguments()))
            row = receipt.to_dict()
            if field == "state":
                row["state"] = "terminal_unknown"
            elif field == "goal":
                row["prepared_request"]["goal"] = "different"
            elif field == "metadata":
                row["prepared_request"]["metadata"] = {"different": True}
            else:
                row["prepared_request"]["input_media"] = []
            write_json_file_atomic(
                inputs.gateway_input_receipt_path(context.paths, receipt.request_id), row
            )
            with pytest.raises((RequestExecutionContextUnavailable, inputs.DataCorruptionError)):
                source.queue_unbound(receipt)

        run_receipt_work(lower, socket, finish)
        assert not list(context.paths.inbox.glob("*.json"))


def test_old_complete_pending_receipt_cannot_claim_fresh_transport(context, monkeypatch):
    with http_case(context, monkeypatch) as (lower, runtime, _group, socket, handler):

        def finish():
            first = component().CoordinatorInputReceiptSource(
                handler, upper(lower, runtime, context)
            )
            receipt, _ = first.prepare(inputs.GatewayInputPreparation(**arguments()))
            second = component().CoordinatorInputReceiptSource(
                handler, upper(lower, runtime, context)
            )
            existing, created = second.prepare(inputs.GatewayInputPreparation(**arguments()))
            assert not created and existing.state == "queued" and second._fresh is None
            assert existing.prepared_request == receipt.prepared_request
            assert read_json_file(context.paths.inbox / (existing.request_id + ".json")) == receipt.prepared_request
            with pytest.raises(RequestExecutionContextUnavailable):
                second.route_fresh(existing, None, allow_active=False)
            assert inputs.read_gateway_input_receipt(context.paths, receipt.request_id) == existing

        run_receipt_work(lower, socket, finish)


def test_full_raw_cas_keeps_json_type_difference_and_mutable_expected_snapshot(
    context, monkeypatch
):
    with http_case(context, monkeypatch) as (lower, runtime, _group, socket, handler):

        def finish():
            with monkeypatch.context() as change:
                change.setattr(inputs, "time", SimpleNamespace(time=lambda: 1.0))
                source = component().CoordinatorInputReceiptSource(
                    handler, upper(lower, runtime, context)
                )
                receipt, _ = source.prepare(inputs.GatewayInputPreparation(**arguments()))
            path = inputs.gateway_input_receipt_path(context.paths, receipt.request_id)
            row = read_json_file(path)
            row["updated_at"] = True
            write_json_file_atomic(path, row)
            with pytest.raises(RequestExecutionContextUnavailable):
                source.queue_unbound(receipt)
            assert read_json_file(path)["updated_at"] is True
            row["updated_at"] = 1.0
            write_json_file_atomic(path, row)
            receipt.prepared_request["metadata"]["mutated_after_snapshot"] = True
            with pytest.raises(RequestExecutionContextUnavailable):
                source.queue_unbound(receipt)

        run_receipt_work(lower, socket, finish)
        assert not list(context.paths.inbox.glob("*.json"))


@pytest.mark.parametrize("where", ["receipt_read", "queue_write", "receipt_write"])
def test_actual_c_fixed_segments_do_not_wait_for_ordinary_json_locks(context, monkeypatch, where):
    from agent_py_agent.tests.test_gateway_c_queued_input_trylock import HeldResource

    with http_case(context, monkeypatch) as (lower, runtime, _group, socket, handler):

        def finish():
            source = component().CoordinatorInputReceiptSource(
                handler, upper(lower, runtime, context)
            )
            receipt, _ = source.prepare(inputs.GatewayInputPreparation(**arguments()))
            path = inputs.gateway_input_receipt_path(context.paths, receipt.request_id)
            before = path.read_bytes()
            held = HeldResource()
            target = (
                context.paths.inbox / (receipt.request_id + ".json")
                if where == "queue_write"
                else path
            )
            if where == "receipt_write":
                original_write = component().write_json_file_atomic

                def before_receipt_write(selected, payload, **options):
                    if selected == path:
                        held.hold(path, "common")
                    return original_write(selected, payload, **options)

                monkeypatch.setattr(component(), "write_json_file_atomic", before_receipt_write)
            else:
                held.hold(target, "gateway" if where == "receipt_read" else "common")
            try:
                if where == "receipt_read":
                    with pytest.raises(BlockingIOError):
                        source.prepare(inputs.GatewayInputPreparation(**arguments()))
                else:
                    from agent_py_agent.agent.conversation.input_resource_retry import (
                        input_resource_deadline,
                    )

                    deadline = input_resource_deadline()
                    with source._receipt_scope(receipt, deadline), pytest.raises(BlockingIOError):
                        source._queue_unbound_locked(receipt, deadline)
                held.finished.set()
                assert not held.holder_done.is_set() and not held.deadline_released.is_set()
                assert path.read_bytes() == before
                runtime.stop_producers()
                assert runtime._closing
                with pytest.raises((RequestExecutionContextUnavailable, ValueError)):
                    source.require_original()
            finally:
                held.close()

        run_receipt_work(lower, socket, finish)
