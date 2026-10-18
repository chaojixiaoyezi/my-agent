"""仅真实HTTP来源组件；测试装配原producer，发行dispatcher仍保持memory未就绪。"""
import copy
import threading
from contextlib import ExitStack
from contextvars import copy_context

import pytest

from agent_py_agent.tests.test_coordinator_agents_domain import submit
from agent_py_agent.tests.test_coordinator_control_forward import cold


# LLM: 测试只接待验组件，不替换G1/work/lease/原receipt写者，不能将这里的临时分派说成发行ready。
# 函数用途: 用真实HTTP线程运行一段本次原management source并保留完整异常对象。
def install(monkeypatch, exercise):
    from agent_py_agent.agent.gateway_parts import http_service
    from agent_py_agent.agent.gateway_parts.coordinator_owner_management import (
        complete_owner_management_operation,
        coordinator_owner_management_operation,
    )
    from agent_py_agent.agent.gateway_parts.http_handlers import require_trusted_source

    observed = []

    def handle(handler, server):
        if require_trusted_source(handler):
            return
        body = handler._read_json()
        try:
            with coordinator_owner_management_operation(handler, server, body) as (source, receipt, scope):
                if source is not None:
                    observed.append(source)
                    result, thread_id = exercise(source, scope)
                    receipt = complete_owner_management_operation(source, result, conversation_thread_id=thread_id)
            handler._send_json(200, receipt.to_dict())
        except BaseException as error:
            observed.append(error)
            handler._send_json(503, {'error_type': type(error).__name__})

    monkeypatch.setattr(http_service, 'handle_control', handle)
    return observed


def ready(source):
    from agent_py_agent.agent.conversation.control_commands import ConversationControlResult
    from agent_py_agent.agent.gateway_parts.memory_candidate_control import (
        memory_control_ingress_valid,
    )
    from agent_py_agent.agent.gateway_parts.owner_management_control import bind_management_resource
    from agent_py_agent.agent.user_space.group_authorization_context import bind_group_control_host

    scope = source.bind_management_memory_ingress()
    command = source.http_origin[3]
    assert memory_control_ingress_valid(scope, command)
    host, thread = source.prepare_actor_control_thread()
    assert host.home_paths is source._management_memory_binding.home
    original = source.http_origin[5]
    bind_group_control_host(host, host, original, thread.thread_id)
    bind_management_resource(None, host, host.home_paths, (source.http_origin[7], command, original), source)
    return scope, ConversationControlResult('memory', True, '仅组件结果'), thread.thread_id


def test_original_memory_seal_outlives_domain_and_parent_complete(cold, monkeypatch):  # noqa: F811
    seals = []

    def exercise(source, original):
        scope, result, thread_id = ready(source)
        seals.append(scope.memory_ingress)
        assert scope is not original and scope.memory_ingress.active
        assert source.perform(lambda: 17) == 17
        return result, thread_id

    observed = install(monkeypatch, exercise)
    status, receipt = submit(cold, '/memory candidates', 'component-completed')
    source = observed[0]
    assert status == 200 and receipt['state'] == 'completed', (receipt, observed)
    assert len(seals) == 1 and not seals[0].active
    assert not source.lease._commit_pins and source._receipt_state == 'completed'
    assert cold.constructions == [] and cold.server.agent is None
    from agent_py_agent.agent.common.owner_write_errors import LocalControlCoordinatorUnavailable
    with pytest.raises(LocalControlCoordinatorUnavailable):
        source.perform(lambda: pytest.fail('关闭后不得写'))


@pytest.mark.parametrize('unknown_fails', [False, True])
def test_ordinary_domain_error_retires_once_while_original_seal_active(cold, monkeypatch, unknown_fails):  # noqa: F811
    from agent_py_agent.agent.gateway_parts import control_operation_service as ops

    business, cleanup, cause = OSError('synthetic domain failure'), OSError('synthetic unknown failure'), ValueError('original cause')
    business.__cause__ = cause
    attempts, seals = [], []
    original = ops._write_control_operation_json

    def write(path, payload, **kwargs):
        if payload['state'] == 'terminal_unknown':
            source = kwargs['owner_management_source']
            attempts.append(payload)
            assert source._management_memory_binding.scope.memory_ingress.active
            if unknown_fails:
                raise cleanup
        return original(path, payload, **kwargs)

    monkeypatch.setattr(ops, '_write_control_operation_json', write)

    def exercise(source, scope):
        bound, _result, _thread_id = ready(source)
        seals.append(bound.memory_ingress)
        raise business

    observed = install(monkeypatch, exercise)
    status, _receipt = submit(cold, '/memory candidates', 'ordinary-retirement')
    source = observed[0]
    current = ops.read_gateway_control_operation(source.paths, source.receipt.operation_id, read_only=True)
    assert status == 503 and observed[-1] is business and business.__cause__ is cause
    assert len(attempts) == 1 and not seals[0].active and not source.lease._commit_pins
    assert current.state == ('executing' if unknown_fails else 'terminal_unknown')
    assert business.__context__ is None
    assert getattr(business, '__notes__', []) == (['control_management_unknown_write_failed:OSError'] if unknown_fails else [])


@pytest.mark.parametrize('change', ['holder', 'stack', 'binding', 'seal'])
def test_memory_fixed_current_first_native_cannot_be_erased(cold, monkeypatch, change):  # noqa: F811
    from agent_py_agent.agent.common.owner_write_errors import LocalControlCoordinatorUnavailable

    first = []

    def exercise(source, scope):
        _bound, result, thread_id = ready(source)
        if change == 'holder':
            target, name = source.http_origin[1], 'coordinator_memory_resources'
        elif change == 'stack':
            target, name = source, '_management_memory_stack'
        elif change == 'binding':
            target, name = source, '_management_memory_binding'
        else:
            target, name = source._management_memory_binding.evidence, 'active'
        previous = getattr(target, name)
        setattr(target, name, False if change == 'seal' else object())
        try:
            with pytest.raises(LocalControlCoordinatorUnavailable) as error:
                source.perform(lambda: pytest.fail('失活后不得进入写者'))
            first.append(error.value)
        finally:
            setattr(target, name, previous)
        with pytest.raises(LocalControlCoordinatorUnavailable) as retained:
            source.perform(lambda: pytest.fail('恢复条件不能抹掉首失败'))
        assert retained.value is first[0]
        return result, thread_id

    observed = install(monkeypatch, exercise)
    status, _receipt = submit(cold, '/memory candidates', 'source-first-fault')
    assert status == 503 and observed[-1] is first[0]
    source = observed[0]
    from agent_py_agent.agent.gateway_parts.control_operation_service import (
        read_gateway_control_operation,
    )
    assert read_gateway_control_operation(source.paths, source.receipt.operation_id, read_only=True).state == 'executing'
    assert not source.lease._commit_pins


def test_copy_and_other_thread_cannot_bind_or_poison_memory_source(cold, monkeypatch):  # noqa: F811
    from agent_py_agent.agent.common.owner_write_errors import LocalControlCoordinatorUnavailable

    def exercise(source, scope):
        with pytest.raises(LocalControlCoordinatorUnavailable):
            copy.copy(source).bind_management_memory_ingress()
        errors = []

        def call():
            try:
                source.bind_management_memory_ingress()
            except BaseException as error:
                errors.append(error)

        for target in (call, lambda: copy_context().run(call)):
            thread = threading.Thread(target=target)
            thread.start()
            thread.join(2)
            assert not thread.is_alive()
        assert len(errors) == 2 and all(isinstance(x, LocalControlCoordinatorUnavailable) for x in errors)
        assert source._native_source_failure is None
        _bound, result, thread_id = ready(source)
        return result, thread_id

    observed = install(monkeypatch, exercise)
    status, receipt = submit(cold, '/memory candidates', 'copy-thread')
    assert status == 200 and receipt['state'] == 'completed', (receipt, observed)


def test_memory_stack_cleanup_never_replaces_actual_outer(cold, monkeypatch):  # noqa: F811
    from agent_py_agent.agent.common.request_execution_ports import (
        RequestExecutionContextUnavailable,
    )

    outer = RequestExecutionContextUnavailable('synthetic actual source')
    original, selected = ExitStack.close, []

    def close(stack):
        original(stack)
        if selected and stack is selected[0]:
            raise OSError('synthetic close failure')

    monkeypatch.setattr(ExitStack, 'close', close)

    def exercise(source, scope):
        ready(source)
        selected.append(source._management_memory_stack)
        raise outer

    observed = install(monkeypatch, exercise)
    status, _receipt = submit(cold, '/memory candidates', 'cleanup-outer')
    assert status == 503 and observed[-1] is outer
    assert outer.__notes__ == ['control_memory_ingress_close_failed:OSError']
    source = observed[0]
    assert not source._management_memory_binding.evidence.active and not source.lease._commit_pins
