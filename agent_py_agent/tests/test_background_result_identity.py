"""宿主身份沿原RunParams交接，任意业务结果都不承担授权。"""
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.conversation import background_execution as execution
from agent_py_agent.agent.conversation.local_run_control import LocalRunControl


@dataclass(frozen=True)
class FrozenResult:
    response: str = '不可变业务结果'


@pytest.mark.parametrize('raw', ['原字符串', 3, ('原元组',), FrozenResult()], ids=['str', 'int', 'tuple', 'frozen'])
def test_any_business_result_is_returned_unchanged_with_original_published_identity(raw, monkeypatch):
    reads = []
    original = LocalRunControl.runtime_authority

    def read_after_finish(control):
        assert control._finished is True
        reads.append(True)
        return original(control)

    monkeypatch.setattr(LocalRunControl, 'runtime_authority', read_after_finish)
    original_finish = LocalRunControl.finish

    def finish_and_reject_late_publish(control):
        original_finish(control)
        with pytest.raises(InterruptedError):
            control.bind_runtime_authority({'invocation_run_id': control.request_id,
                'task_id': 'work-task', 'run_id': 'work-run', 'agent_run_id': 'work-agent',
                'attempt_id': 'late-attempt'})

    monkeypatch.setattr(LocalRunControl, 'finish', finish_and_reject_late_publish)

    def run(_prompt, *, params):
        params.conversation_task_binding_callback.bind_runtime_authority({
            'invocation_run_id': params.request_id, 'task_id': 'work-task', 'run_id': 'work-run',
            'agent_run_id': 'work-agent', 'attempt_id': 'work-attempt'})
        return raw

    returned, params = execution._run_background_model_attempt(
        SimpleNamespace(run=run), '原需求', RunParams(request_id='work-request'))
    assert returned is raw and reads == [True]
    assert (params.request_id, params.task_id, params.run_id, params.attempt_id) == (
        'work-request', 'work-task', 'work-run', 'work-attempt')


@pytest.mark.parametrize('mutable', [False, True], ids=['str', 'forged-attribute'])
def test_unpublished_input_identity_and_forged_business_attribute_cannot_authorize_reply(mutable):
    forged = {'request_id': 'input-request', 'task_id': 'input-task', 'run_id': 'input-run', 'attempt_id': 'input-attempt'}
    raw = SimpleNamespace(response='原结果', source_reply_execution_identity=forged) if mutable else '原字符串'
    returned, params = execution._run_background_model_attempt(SimpleNamespace(run=lambda *_args, **_kwargs: raw),
        '原需求', RunParams(**forged))
    assert returned is raw
    assert (params.run_id, params.task_id, params.attempt_id) == ('', '', '')
    assert execution.collect_background_execution_result(SimpleNamespace(), raw, {}).execution_identity == {}
    projected = {key: getattr(params, key) for key in forged}
    assert execution.collect_background_execution_result(
        SimpleNamespace(), raw, {}, execution_identity=projected).execution_identity == {
            **forged, 'task_id': '', 'run_id': '', 'attempt_id': ''}


def test_explicit_host_identity_is_copied_and_business_attribute_is_ignored():
    host = {'request_id': 'host-request', 'task_id': 'host-task', 'run_id': 'host-run', 'attempt_id': 'host-attempt'}
    raw = SimpleNamespace(response='原结果', source_reply_execution_identity={'attempt_id': 'forged-attempt'})
    result = execution.collect_background_execution_result(SimpleNamespace(), raw, {}, execution_identity=host)
    host['attempt_id'] = 'later-attempt'
    assert result.execution_identity['attempt_id'] == 'host-attempt'


def test_real_compact_completion_hands_off_only_last_original_attempt(tmp_path, monkeypatch):
    from agent_py_agent.agent.backends.errors import ProviderContextWindowError
    from agent_py_agent.agent.conversation import compact
    from agent_py_agent.tests.test_background_compact_recovery import _background
    from agent_py_agent.tests.test_subagent_compact_recovery import _http

    agent, _store, thread, request, deps, sink = _background(
        tmp_path, backend='openai_compatible', detached=False)
    original_summary, original_attempt = compact._summarize, execution._run_background_model_attempt
    in_summary, business, attempts, completed = False, [], [], []

    def summary(*args, **kwargs):
        nonlocal in_summary
        in_summary = True
        try:
            return original_summary(*args, **kwargs)
        finally:
            in_summary = False

    def attempt(*args, **kwargs):
        result, params = original_attempt(*args, **kwargs)
        attempts.append(params)
        return result, params

    def on_business(wire, _number):
        if not in_summary:
            business.append(wire)
            if len(business) == 1:
                raise ProviderContextWindowError('离线上下文溢出')

    monkeypatch.setattr(compact, '_summarize', summary)
    monkeypatch.setattr(execution, '_run_background_model_attempt', attempt)
    _http(monkeypatch, backend='openai_compatible', on_business=on_business)
    result = execution.run_background_turn_with_compact(deps, thread, request, user_prompt='继续原工作',
        continuation_injection=[], proactive_delivery_available=False, activity_sink=sink,
        completed_run_callback=completed.append)
    assert result.runtime_status != 'context_overflow'
    assert len(business) == len(attempts) == 2 and completed == [attempts[-1]]
    assert attempts[0].attempt_id and attempts[1].attempt_id and attempts[0].attempt_id != attempts[1].attempt_id
    assert attempts[0].request_id == attempts[1].request_id


@pytest.mark.parametrize('kind', ['cancelled', 'actual-context'])
def test_abnormal_attempt_does_not_publish_completion_identity(tmp_path, monkeypatch, kind):
    from agent_py_agent.agent.common.request_execution_ports import (
        RequestExecutionContextUnavailable,
    )
    from agent_py_agent.tests.test_background_compact_recovery import _background

    agent, _store, thread, request, deps, sink = _background(
        tmp_path, backend='openai_compatible', detached=False, with_history=False)
    error = InterruptedError('原取消') if kind == 'cancelled' else RequestExecutionContextUnavailable('original_request_required')
    completed = []

    def run(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(agent, 'run', run)
    with pytest.raises(type(error)) as caught:
        execution.run_background_turn_with_compact(deps, thread, request, user_prompt='继续原工作',
            continuation_injection=[], proactive_delivery_available=False, activity_sink=sink,
            completed_run_callback=completed.append)
    assert caught.value is error and completed == []
