"""单次原预算覆盖正常读、末写及全部refs；真实临时C/claim/source与测试回环。"""
import sys
import time

import pytest

from agent_py_agent.agent.backends.request_scope import provider_request_budget
from agent_py_agent.agent.common.request_execution_ports import (
    RequestExecutionOutcomeUncertainError,
)
from agent_py_agent.agent.conversation.input_resource_retry import run_input_safe_point
from agent_py_agent.agent.gateway_parts import input_delivery_service as inputs
from agent_py_agent.agent.gateway_parts import multiprocess_input_receipt as producer
from agent_py_agent.agent.gateway_parts.input_guidance_payload import original_input_guidance
from agent_py_agent.agent.gateway_parts.multiprocess_admission import reservation_file
from agent_py_agent.tests.test_gateway_active_input_transport import foreground, params_for, send


@pytest.mark.parametrize('action', ['probe', 'drain', 'finish'])
def test_normal_read_cannot_start_next_fixed_step_after_same_deadline(tmp_path, monkeypatch, action):
    with foreground(tmp_path, monkeypatch) as (agent, backend, gate, _c, _r, server, claim, thread, _lane):
        code, incoming = send(server, message='budget-reader-'+action)
        assert code == 202
        receipt = inputs.read_gateway_input_receipt(gate.paths, incoming['request_id'])
        path = inputs.gateway_input_receipt_path(gate.paths, receipt.request_id)
        original_receipt, original_p3 = path.read_bytes(), reservation_file(gate.paths, claim.request_id).read_bytes()
        params = params_for(agent, claim, thread)
        source = params.pending_turn_input_source
        reader = inputs.read_gateway_input_receipt
        delayed, entered = [], []

        def slow_read(paths, request_id, **options):
            result = reader(paths, request_id, **options)
            if request_id == receipt.request_id and not delayed:
                delayed.append(True)
                time.sleep(0.10)
            return result

        monkeypatch.setattr(inputs, 'read_gateway_input_receipt', slow_read)
        for name in ('_append', '_queue_unseen'):
            method = getattr(source, name)

            def observe(*args, _name=name, _method=method, **options):
                entered.append(_name)
                return _method(*args, **options)

            monkeypatch.setattr(source, name, observe)
        with provider_request_budget(0.04), pytest.raises(RequestExecutionOutcomeUncertainError) as caught:
            run_input_safe_point(source, agent, action, params)
        assert caught.value.phase == 'input.transport.retry_admission' and delayed and not entered
        assert path.read_bytes() == original_receipt
        assert reservation_file(gate.paths, claim.request_id).read_bytes() == original_p3
        assert original_input_guidance(agent.conversation_store, receipt) is None
        assert not (gate.paths.inbox / (receipt.request_id+'.json')).exists()
        assert not (gate.paths.terminal / (claim.request_id+'.json')).exists() and backend.calls == 0


def test_http_expired_source_is_same_object_until_safe_503_boundary(tmp_path, monkeypatch):
    original_route = producer.CoordinatorInputReceiptSource.route_fresh
    inner, boundary, published = [], [], []
    original_publish = producer.CoordinatorInputReceiptSource._publish_transport

    def route(self, *args, **options):
        with provider_request_budget(0):
            try:
                return original_route(self, *args, **options)
            except BaseException as error:
                inner.append(error)
                raise

    def publish(self, *args, **options):
        published.append(True)
        return original_publish(self, *args, **options)

    monkeypatch.setattr(producer.CoordinatorInputReceiptSource, 'route_fresh', route)
    monkeypatch.setattr(producer.CoordinatorInputReceiptSource, '_publish_transport', publish)
    with foreground(tmp_path, monkeypatch) as (_agent, backend, gate, _c, _r, server, claim, _thread, _lane):
        handler = server.server.RequestHandlerClass
        original_send = handler._send_json

        def observe_send(self, code, payload):
            if code == 503:
                boundary.append(sys.exception())
            return original_send(self, code, payload)

        monkeypatch.setattr(handler, '_send_json', observe_send)
        p3 = reservation_file(gate.paths, claim.request_id).read_bytes()
        code, status = send(server, message='http-budget-same-error')
        assert code == 503 and status['terminal'] is False and status['status'] == 'waiting'
        assert len(inner) == len(boundary) == 1 and boundary[0] is inner[0]
        assert status['error_code'] == inner[0].error_code and status['request_id'] and not published
        receipt = inputs.read_gateway_input_receipt(gate.paths, status['request_id'])
        assert receipt.state == 'pending' and receipt.transport is None and not receipt.target_turn_id
        assert not (gate.paths.inbox / (receipt.request_id+'.json')).exists()
        assert reservation_file(gate.paths, claim.request_id).read_bytes() == p3 and backend.calls == 0


def test_one_deadline_covers_all_refs_and_keeps_earlier_partial_commit(tmp_path, monkeypatch):
    with foreground(tmp_path, monkeypatch) as (agent, backend, gate, _c, _r, server, claim, thread, _lane):
        ids = []
        for i in range(2):
            code, status = send(server, message='shared-deadline-ref-'+str(i))
            assert code == 202
            ids.append(status['request_id'])
        refs = inputs.transport_turn_input_references(gate.paths, claim.request_id)
        ordered = [row['request_id'] for row in refs if row['request_id'] in ids]
        assert len(ordered) == 2
        reader = inputs.read_gateway_input_receipt
        delayed, entered = [], []
        params = params_for(agent, claim, thread)
        source = params.pending_turn_input_source
        append = source._append

        def slow(paths, request_id, **options):
            result = reader(paths, request_id, **options)
            if request_id in ids and request_id not in delayed:
                delayed.append(request_id)
                time.sleep(0.10)
            return result

        def observe(receipt, actual_agent, **options):
            entered.append(receipt.request_id)
            return append(receipt, actual_agent, **options)

        monkeypatch.setattr(inputs, 'read_gateway_input_receipt', slow)
        monkeypatch.setattr(source, '_append', observe)
        with provider_request_budget(0.17), pytest.raises(RequestExecutionOutcomeUncertainError) as caught:
            run_input_safe_point(source, agent, 'drain', params)
        assert caught.value.phase == 'input.transport.retry_admission'
        assert delayed == ordered and entered == ordered[:1]
        first, second = (reader(gate.paths, item) for item in ordered)
        assert first.state == 'active_pending' and second.state == 'pending'
        assert original_input_guidance(agent.conversation_store, first) is not None
        assert original_input_guidance(agent.conversation_store, second) is None and backend.calls == 0


def test_http_boundary_keeps_typed_mi_priority_and_never_swallows_interrupt(monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.common.request_execution_ports import (
        RequestExecutionContextUnavailable,
    )
    from agent_py_agent.agent.gateway_parts import http_input_receipt

    class TypedConflict(RequestExecutionContextUnavailable, ValueError):
        pass

    error = TypedConflict('synthetic original source')
    observed = []

    def fail(*_args):
        raise error

    def response(code, payload):
        observed.append((sys.exception(), code, payload))

    monkeypatch.setattr(http_input_receipt, '_handle_coordinator_input', fail)
    arguments = dict(prepared_request={}, scope=None, request_id='synthetic-own-input', client_input_digest='', client_message_id='', guidance_key='', allow_active_turn=True)
    http_input_receipt.handle_coordinator_input(SimpleNamespace(_send_json=response), None,
        http_input_receipt.CoordinatorInputRequest(**arguments))
    assert observed[0][0] is error and observed[0][1] == 503
    assert observed[0][2]['error_code'] == error.error_code and observed[0][2]['request_id'] == arguments['request_id']
    assert observed[0][2]['terminal'] is False and 'operation_entered' not in observed[0][2]
    stopped = KeyboardInterrupt()

    def interrupt(*_args):
        raise stopped

    monkeypatch.setattr(http_input_receipt, '_handle_coordinator_input', interrupt)
    with pytest.raises(KeyboardInterrupt) as caught:
        http_input_receipt.handle_coordinator_input(SimpleNamespace(_send_json=response), None,
            http_input_receipt.CoordinatorInputRequest(**arguments))
    assert caught.value is stopped and len(observed) == 1


def test_queue_partial_commit_survives_expiry_and_repairs_same_input_without_rewrite(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import multiprocess_active_input as active

    with foreground(tmp_path, monkeypatch) as (agent, backend, gate, _c, _r, server, claim, thread, _lane):
        code, incoming = send(server, message='partial-live-closeout-queue')
        assert code == 202
        receipt = inputs.read_gateway_input_receipt(gate.paths, incoming['request_id'])
        inbox = gate.paths.inbox / (receipt.request_id+'.json')
        p3 = reservation_file(gate.paths, claim.request_id).read_bytes()
        params = params_for(agent, claim, thread)
        source = params.pending_turn_input_source
        writer = active.write_json_file_atomic
        committed = []

        def delayed(path, payload, **options):
            result = writer(path, payload, **options)
            if path == inbox:
                committed.append(path.read_bytes())
                time.sleep(0.10)
            return result

        monkeypatch.setattr(active, 'write_json_file_atomic', delayed)
        with provider_request_budget(0.05), pytest.raises(RequestExecutionOutcomeUncertainError) as caught:
            run_input_safe_point(source, agent, 'finish', params)
        assert caught.value.phase == 'input.transport.retry_admission' and len(committed) == 1
        assert inputs.read_gateway_input_receipt(gate.paths, receipt.request_id).state == 'pending'
        assert inbox.read_bytes() == committed[0] and original_input_guidance(agent.conversation_store, receipt) is None
        assert reservation_file(gate.paths, claim.request_id).read_bytes() == p3
        assert not (gate.paths.terminal / (claim.request_id+'.json')).exists()
        run_input_safe_point(source, agent, 'finish', params)
        assert inputs.read_gateway_input_receipt(gate.paths, receipt.request_id).state == 'queued'
        assert inbox.read_bytes() == committed[0] and len(committed) == 1 and backend.calls == 0
