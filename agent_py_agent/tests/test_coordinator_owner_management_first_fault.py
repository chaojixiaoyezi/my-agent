"""真实HTTP source首个native仅防后续写，不替代领域或物理生命周期。"""
import contextvars
import copy
import threading

import pytest

from agent_py_agent.agent.conversation.control_commands import ConversationControlResult
from agent_py_agent.agent.gateway_parts import control_operation_service as ops
from agent_py_agent.agent.gateway_parts.multiprocess_local_control import (
    LocalControlCoordinatorUnavailable,
)
from agent_py_agent.tests.test_coordinator_model_card_delivery import delivery_case
from agent_py_agent.tests.test_coordinator_model_menu import post_model
from agent_py_agent.tests.test_coordinator_owner_management_source import (
    install_source_contract_fixture,
    request_body,
)


@pytest.mark.parametrize('fault', ['ingress', 'lifetime', 'body', 'current', 'global', 'coordinator'])
def test_first_original_native_fences_every_writer_and_result_validation(tmp_path, monkeypatch, fault):
    from agent_py_agent.agent.common.global_write_guard import GlobalWriteInWorkerError
    from agent_py_agent.agent.gateway_parts import multiprocess_http_transport

    errors, calls, actors = [], [], []

    def exercise(common, carrier):
        source = carrier[0]
        if fault == 'ingress':
            source.http_origin[7].active = False
        elif fault == 'lifetime':
            source.lifetime.close()
        elif fault == 'body':
            source.http_origin[2]['command'] = '/agents refresh'
        elif fault == 'current':
            source.lease._commit_pins.remove(source.pin)
        elif fault == 'global':
            def fail(*args):
                raise global_error
            global_error = GlobalWriteInWorkerError('synthetic original HTTP global guard')
            monkeypatch.setattr(multiprocess_http_transport, 'require_http_handler_work', fail)
            # _check_http_current imports this exact original boundary each time.
        elif fault == 'coordinator':
            source.bind_resource_owner(actors[0].home_paths, actor=actors[0],
                subject=(source.http_origin[7], source.http_origin[3], carrier[2]))
            original = source.controller.launch_owner_writer
            monkeypatch.setattr(source.controller, 'launch_owner_writer', lambda *args: {'status': 'blocked'})
            with pytest.raises(LocalControlCoordinatorUnavailable) as caught:
                source.perform(lambda: calls.append('first'))
            errors.append(caught.value)
            monkeypatch.setattr(source.controller, 'launch_owner_writer', original)
        if not errors:
            with pytest.raises((ValueError, LocalControlCoordinatorUnavailable, GlobalWriteInWorkerError)) as caught:
                source._require_current()
            errors.append(caught.value)
        first = errors[0]
        assert source._native_source_failure is first
        # 首错误后第二个不同条件也不能取代它；所有固定writer在callback/codec/result验证前拒绝。
        source.http_origin[7].active = False
        actions = [source._require_current, source._require_http_current, source._require_actor_current,
            lambda: source.perform(lambda: calls.append('perform')),
            lambda: source.commit_resource_file(lambda: calls.append('file'), owner_home=tmp_path),
            lambda: source.commit_control_receipt({'invalid': True}),
            lambda: common.complete_owner_management_operation(source, object())]
        for action in actions:
            with pytest.raises(type(first)) as caught:
                action()
            assert caught.value is first and source._native_source_failure is first
        assert calls == []
        return common.complete_owner_management_operation(source, ConversationControlResult('agents', False, '未执行'))

    observed = install_source_contract_fixture(monkeypatch, exercise)
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        server = case[5]
        actors.append(case[1])
        status, _ = post_model(server, request_body(), route='/control', actor=case[3]['actor_id'])
        assert status == 503 and observed[-1] is errors[0]
        source = observed[0][0]
        assert ops.read_gateway_control_operation(server.paths, source.receipt.operation_id, read_only=True).state == 'executing'
        assert not calls and not delivered


@pytest.mark.parametrize('kind', ['copy', 'thread', 'copied_context_thread'])
def test_foreign_attempt_does_not_poison_original_http_source(tmp_path, monkeypatch, kind):
    errors = []

    def exercise(common, carrier):
        source = carrier[0]
        if kind == 'copy':
            with pytest.raises(LocalControlCoordinatorUnavailable):
                copy.copy(source)._require_http_current()
        else:
            def other():
                try:
                    source._require_http_current()
                except BaseException as error:
                    errors.append(error)
            target = other if kind == 'thread' else lambda: contextvars.copy_context().run(other)
            if kind == 'copied_context_thread':
                captured = contextvars.copy_context()
                def target():
                    captured.run(other)
            thread = threading.Thread(target=target)
            thread.start()
            thread.join(2)
            assert not thread.is_alive() and type(errors[-1]) is LocalControlCoordinatorUnavailable
        assert source._native_source_failure is None
        return common.complete_owner_management_operation(source, ConversationControlResult('agents', True, '未污染'))

    install_source_contract_fixture(monkeypatch, exercise)
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        status, payload = post_model(case[5], request_body(), route='/control', actor=case[3]['actor_id'])
        assert status == 200 and payload['control_state'] == 'completed' and not delivered


def test_later_http_cannot_record_failure_in_previous_source(tmp_path, monkeypatch):
    sources = []

    def exercise(common, carrier):
        source = carrier[0]
        if sources:
            previous = sources[0]
            with pytest.raises(LocalControlCoordinatorUnavailable):
                previous._require_http_current()
            assert previous._native_source_failure is None
        sources.append(source)
        assert source._native_source_failure is None
        return common.complete_owner_management_operation(source, ConversationControlResult('agents', True, '原操作'))

    install_source_contract_fixture(monkeypatch, exercise)
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        for message in ['first-http', 'second-http']:
            assert post_model(case[5], request_body(message), route='/control', actor=case[3]['actor_id'])[0] == 200
        assert len(sources) == 2 and not delivered


@pytest.mark.parametrize('completed', [False, True])
def test_actual_outer_preserves_cause_context_and_last_durable_receipt(tmp_path, monkeypatch, completed):
    from agent_py_agent.agent.backends.bounded_call import BoundedCallExitUnconfirmedError

    first, outer, cause, context = [], BoundedCallExitUnconfirmedError('synthetic', 'worker_exit_unconfirmed'), OSError('original cause'), LookupError('original context')

    def exercise(common, carrier):
        source = carrier[0]
        if completed:
            common.complete_owner_management_operation(source, ConversationControlResult('agents', False, '已实际收口'))
        source.http_origin[7].active = False
        with pytest.raises(ValueError) as caught:
            source._require_http_current()
        first.append(caught.value)
        source.http_origin[7].active = True
        try:
            raise context
        except LookupError:
            raise outer from cause

    observed = install_source_contract_fixture(monkeypatch, exercise)
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        status, _ = post_model(case[5], request_body(), route='/control', actor=case[3]['actor_id'])
        assert status == 503 and observed[-1] is outer
        assert outer.__cause__ is cause and outer.__context__ is context
        assert observed[0][0]._native_source_failure is first[0] and outer is not first[0]
        receipt = ops.read_gateway_control_operation(case[5].paths, observed[0][0].receipt.operation_id, read_only=True)
        assert receipt.state == ('completed' if completed else 'executing')
        assert not delivered
