"""真实临时scheduler/claim/core通知轮；模型、末端HTTP和外送使用原合成夹具。"""
import copy
import threading
from contextvars import copy_context
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.host_turn_scope import UNSETTLED_HOST_ERRORS
from agent_py_agent.agent.conversation import host_notification_scope as scopes
from agent_py_agent.agent.conversation.claim_execution_binding import (
    ClaimExecutionBinding,
    bind_claim_execution,
    current_claim_execution,
)
from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest
from agent_py_agent.agent.conversation.tool_approval_scope import tool_approval_scope
from agent_py_agent.tests.test_scheduler_user_action_notice import real_notice_case


# LLM: scheduler和会话claim都由原API领取，TaskLink由原claim_wake物化，不拼runtime authority或猜当前attempt。
# 函数用途: 在无模型/外网的真实临时后台领取范围验证通知scope本身。
@pytest.fixture
def notification_case(tmp_path, monkeypatch):
    case = real_notice_case(tmp_path, monkeypatch)
    claimed = case.agent.scheduler_service.claim_wake(case.signal, now=case.now)
    assert claimed.status == 'claimed'
    row = case.store.claims.acquire({'thread_id': case.signal.thread_id,
        'task_id': case.signal.root_task_id, 'reason': 'scheduled_job_due', 'lease_seconds': 60})
    assert row is not None
    request = BackgroundRunRequest(thread_id=case.signal.thread_id, task_id=case.signal.root_task_id,
        reason=case.signal.reason, wake_signal=case.signal.to_dict(), now=case.now)
    with bind_claim_execution(ClaimExecutionBinding(case.store.claims, row['thread_id'], row['claim_id'], '')):
        yield case, request


def test_one_hot_scope_is_shared_until_delivery_and_closed_after_exit(notification_case, monkeypatch):
    from agent_py_agent.agent.agent_core import runtime_mixin as core

    case, request = notification_case
    original = core._bind_main_agent_turn_params
    bindings = []
    def bind(*args, **kwargs):
        result = original(*args, **kwargs)
        bindings.append(result)
        return result
    monkeypatch.setattr(core, '_bind_main_agent_turn_params', bind)
    with scopes.notification_scope(case.agent, request) as turn:
        assert turn is not None
        params = turn.params
        scope = tool_approval_scope(case.agent, params.run_id)
        assert scope.active and scope.execution_attempt_id == params.attempt_id
        with scopes.notification_scope(case.agent, replace(request)) as nested:
            assert nested is turn and scopes.current_notification_turn(case.agent, request) is turn
        assert len(bindings) == 1 and turn.active
        row = case.store.claims.load(request.thread_id)
        assert row['runtime_authority']['attempt_id'] == params.attempt_id
    assert not turn.active and scopes.current_notification_turn(case.agent, request) is None
    assert not params.conversation_task_binding_callback(None)
    assert case.calls == case.backend.prompts == case.channels.attempts == []


@pytest.mark.parametrize('fault', ['copy_holder', 'copy_context', 'copy_turn', 'closed', 'request', 'task', 'attempt', 'owner', 'actor', 'claim'])
def test_notification_scope_does_not_lend_identity_to_replaced_or_copied_context(notification_case, fault):
    case, request = notification_case
    with scopes.notification_scope(case.agent, request) as turn:
        holder = scopes._CURRENT.get()
        if fault == 'copy_context':
            with pytest.raises(ValueError):
                copy_context().run(scopes.current_notification_turn, case.agent, request)
            assert scopes.current_notification_turn(case.agent, request) is turn
        elif fault == 'copy_turn':
            with pytest.raises(ValueError):
                copy.copy(turn).require(case.agent, turn.params)
        elif fault == 'copy_holder':
            token = scopes._CURRENT.set(copy.copy(holder))
            try:
                with pytest.raises(ValueError):
                    scopes.current_notification_turn(case.agent, request)
            finally:
                scopes._CURRENT.reset(token)
        elif fault == 'claim':
            claim = current_claim_execution()
            with bind_claim_execution(replace(claim)):
                with pytest.raises(ValueError):
                    scopes.current_notification_turn(case.agent, request)
        elif fault == 'closed':
            turn.params.conversation_task_binding_callback.finish()
            with pytest.raises(ValueError):
                scopes.current_notification_turn(case.agent, request)
        elif fault == 'request':
            with pytest.raises(ValueError):
                scopes.current_notification_turn(case.agent, replace(request, task_id='foreign-task'))
        else:
            if fault == 'owner':
                target, field, replacement = case.agent.config, 'my_agent_owner_id', 'foreign-owner'
            elif fault == 'actor':
                target, field = turn.params, 'host_channel_actor'
                replacement = replace(turn.params.host_channel_actor, actor_id='foreign-actor')
            else:
                target, field, replacement = turn.params, fault + '_id', 'foreign-' + fault
            original = getattr(target, field)
            try:
                setattr(target, field, replacement)
                with pytest.raises(ValueError):
                    scopes.current_notification_turn(case.agent, request)
            finally:
                setattr(target, field, original)


def test_copied_context_cannot_enter_notification_scope_on_another_thread(notification_case):
    case, request = notification_case
    errors = []
    with scopes.notification_scope(case.agent, request):
        ctx = copy_context()
        def read():
            try:
                scopes.current_notification_turn(case.agent, request)
            except ValueError as error:
                errors.append(error)
        thread = threading.Thread(target=lambda: ctx.run(read))
        thread.start()
        thread.join(2)
        assert not thread.is_alive() and len(errors) == 1


# LLM: 用原异常类型和合成原运输tuple建反例，断言同一异常对象穿透；不按error_code或正文判未知。
# 函数用途: 覆盖入口、scope进入与业务退出时的四类未结责任。
def actual_error(index):
    error_type = UNSETTLED_HOST_ERRORS[index]
    if index == 0:
        return error_type({'id': 'synthetic-request', 'execution_attempt_id': 'synthetic-attempt',
            'lease_epoch': 1, 'lease_owner': 'synthetic-lease'}, 'notification-test')
    if index == 1:
        return error_type('synthetic-call', 'worker_exit_unconfirmed')
    return error_type('synthetic-source-unavailable')


@pytest.mark.parametrize('index', range(4))
@pytest.mark.parametrize('stage', ['before_bind', 'prompt_scope', 'body'])
def test_actual_unknown_is_not_settled_or_relabelled(notification_case, monkeypatch, index, stage):
    from contextlib import contextmanager

    from agent_py_agent.agent.agent_core import runtime_mixin as core

    case, request = notification_case
    failure = actual_error(index)
    settled = []
    original_settle = core._settle_main_agent_run_status
    def settle(*args, **kwargs):
        settled.append(kwargs)
        return original_settle(*args, **kwargs)
    monkeypatch.setattr(core, '_settle_main_agent_run_status', settle)
    if stage == 'before_bind':
        def fail(*args, **kwargs):
            raise failure
        monkeypatch.setattr(core, '_bind_main_agent_turn_params', fail)
    elif stage == 'prompt_scope':
        @contextmanager
        def failed_scope(*args, **kwargs):
            raise failure
            yield
        monkeypatch.setattr(core, 'current_prompt_scope', failed_scope)
    with pytest.raises(type(failure)) as caught:
        with scopes.notification_scope(case.agent, request):
            if stage == 'body':
                raise failure
    assert caught.value is failure and not settled
    assert scopes._CURRENT.get() is None
    assert case.store.claims.load(request.thread_id)['status'] == 'running'
    assert not case.calls and not case.backend.prompts and not case.channels.attempts


@pytest.mark.parametrize('disabled', [False, True])
def test_absent_notice_or_disabled_capability_does_not_create_core_work(notification_case, monkeypatch, disabled):
    from agent_py_agent.agent.agent_core import runtime_mixin as core
    from agent_py_agent.agent.capability.config import CapabilityConfig

    case, request = notification_case
    if disabled:
        case.agent._capability_config_runtime_snapshot = SimpleNamespace(config=CapabilityConfig(
            background_user_action_notice_enabled=False))
    else:
        case.store.tasks.update_status({'task_id': request.task_id, 'status': 'cancelled', 'expected_status': 'active'})
    def forbidden(*args, **kwargs):
        raise AssertionError('ineligible notification must not bind a core attempt')
    monkeypatch.setattr(core, '_bind_main_agent_turn_params', forbidden)
    with scopes.notification_scope(case.agent, request) as turn:
        assert turn is None
    assert not case.calls and not case.backend.prompts and not case.channels.attempts


def test_ordinary_work_has_no_notification_phase_or_extra_model_call(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_scheduler_execution import execution_case

    case = execution_case(tmp_path)
    case.channels.status = 'sent'
    def forbidden(*args, **kwargs):
        raise AssertionError('ordinary work must not enter notification phase')
    monkeypatch.setattr(scopes, 'notification_scope', forbidden)
    case.scheduler.tick(now=1000)
    assert len(case.backend.prompts) == len(case.channels.attempts) == 1


def test_disabled_initial_notice_preserves_one_http_zero_expression_cost(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core import runtime_mixin as core
    from agent_py_agent.agent.capability.config import CapabilityConfig

    case = real_notice_case(tmp_path, monkeypatch)
    case.agent._capability_config_runtime_snapshot = SimpleNamespace(config=CapabilityConfig(
        background_user_action_notice_enabled=False))
    original = core._bind_main_agent_turn_params
    calls = []
    def bind(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(core, '_bind_main_agent_turn_params', bind)
    case.scheduler.tick(now=case.now)
    assert len(calls) == len(case.calls) == 1
    assert not case.backend.prompts and not case.channels.attempts


@pytest.mark.parametrize('already_sent', [False, True])
def test_real_notice_phase_keeps_one_binding_and_sent_retry_creates_none(tmp_path, monkeypatch, already_sent):
    from agent_py_agent.agent.agent_core import runtime_mixin as core
    from agent_py_agent.tests import test_scheduler_user_action_notice as original_tests

    original = core._bind_main_agent_turn_params
    bindings = []
    def bind(*args, **kwargs):
        bound = original(*args, **kwargs)
        bindings.append(bound)
        return bound
    monkeypatch.setattr(core, '_bind_main_agent_turn_params', bind)
    if already_sent:
        original_tests.test_already_sent_notice_completes_canonical_after_generation_change_without_resend(
            tmp_path, monkeypatch)
    else:
        original_tests.test_real_credential_notice_overrides_local_only_to_exact_source(
            tmp_path, monkeypatch, 'user', 'precheck')
    # 原工具工作一次，随后整个freeze/表达/投递只共享一个通知执行轮；已sent重投不再绑定。
    assert len(bindings) == 2
    assert bindings[0].attempt_id != bindings[1].attempt_id
    assert bindings[0].run_id == bindings[1].run_id
    assert bindings[1].source == 'background_user_action_notice' and bindings[1].allowed_tools == []
