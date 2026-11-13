"""原BusinessClient→SessionTask→消耗正文→真实child通知，恢复不假造用户历史或重报结果。"""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.guidance import (
    acknowledge_injected_turn_input,
    inject_pending_guidance,
    mark_injected_turn_input_submitted,
)
from agent_py_agent.agent.conversation.runtime import (
    BackgroundRunRequest,
    _background_request_objective,
    _business_context_from_wake,
    _goal_runtime_context,
    _run_params,
)
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot
from agent_py_agent.tests.test_business_v12_background_scheduling import native as native
from agent_py_agent.tests.test_business_v12_background_scheduling import ok


def _case(native, *, consume=True, finished=True, assigned=True):
    _, agent = native
    store = agent.conversation_store
    space = ok(native, 'spaces.create', {'name': 'continuation'}, idempotency_key='continuation-space-1')['space_id']
    submitted = ok(native, 'tasks.submit', {'space_id': space if assigned else None, 'instruction': '原始独立任务'},
                   idempotency_key='continuation-submit-1')
    task = store.session_tasks.require(submitted['task_id'])
    store.session_tasks.bind_turn(task.task_id, turn_id=task.task_id)
    store.tasks.bind({'thread_id': task.target_thread_id, 'task_id': task.task_id,
                      'goal': 'Structured session_task host event', 'status': 'active'})
    if consume:
        params = _run_params(task.target_thread_id, BackgroundRunRequest(thread_id=task.target_thread_id,
            task_id=task.task_id, reason='session_task', wake_signal={'metadata': {'session_task_id': task.task_id}}), agent)
        params.tool_protocol_snapshot = make_test_protocol_snapshot(run_id=task.task_id)
        params.live_archive_state, params.tool_context, params.runtime_injections = {}, [], []
        assert inject_pending_guidance(agent, params)
        assert mark_injected_turn_input_submitted(agent, params, provider_call_id='original-main-provider') == 1
        assert acknowledge_injected_turn_input(agent, params) == 1
    child = agent.subagents.create_run(goal='独立完成', thought='', plan=['核对'], owner=agent.home_paths.owner_id,
        parent_id=task.task_id, root_id=task.task_id, attributes={
            'conversation_thread_id': task.target_thread_id, 'conversation_request_id': task.task_id,
            'conversation_task_id': task.task_id})
    agent.subagents.lifecycle.set_status(child.id, 'DONE' if finished else 'RUNNING')
    child = agent.subagents.load(child.id)
    store.tasks.bind({'thread_id': task.target_thread_id, 'task_id': child.id,
                      'goal': child.goal, 'task_path': child.agent_run_workspace_dir})
    return agent, task, child, _publish(agent, task, child) if finished else None


def _publish(agent, task, child):
    from agent_py_agent.agent.subagents.runner_completion_wake import RunnerCompletionNotifier
    store = agent.conversation_store
    notifier = RunnerCompletionNotifier(store.tasks, store.wakes, agent.subagents.load, agent.subagents.save)
    assert notifier.notify_result(child, SimpleNamespace(status='DONE', run_id=child.id, dry_run=False,
        message='independent complete', turn_end_reason='completed'), {}, attempt_id='original-child-attempt') == 'delivered'
    [signal] = [value for value in store.wakes.pending(limit=20) if value.source_agent_id == child.id]
    return BackgroundRunRequest(thread_id=task.target_thread_id, task_id=task.task_id,
                                reason=signal.reason, wake_signal=signal.to_dict())


@pytest.mark.parametrize('assigned', [True, False])
def test_native_controller_child_reuses_original_consumed_task_and_context_without_user_row(native, assigned):
    agent, task, child, request = _case(native, assigned=assigned)
    store = agent.conversation_store
    from agent_py_agent.agent.conversation.session_task_report import (
        TaskTurnOutcome,
        close_out_turn,
    )
    assert close_out_turn(agent, task.task_id, TaskTurnOutcome(ok=True, summary='original cycle')) is not None
    before = store.session_tasks.require(task.task_id).to_dict()
    context = _goal_runtime_context(agent, store, request)
    assert context.task_objective == task.goal
    assert context.durable_task_id == task.task_id and context.durable_task_objective == task.goal
    frozen = _business_context_from_wake(agent, request.thread_id, request)
    assert frozen.to_dict() == task.metadata['business_context']
    params = _run_params(request.thread_id, request, agent, goal_context=context)
    assert params.business_context == frozen and params.request_id == task.task_id
    assert params.task_id == task.task_id
    assert not any(row.role == 'user' for row in store.messages.recent(request.thread_id, limit=20))
    # 恢复读取不消费第二次正文，也不复活/覆盖已结案的SessionTask或重发同状态结果。
    assert store.guidance.receipt(task.body_dedupe_key).status == 'consumed'
    assert close_out_turn(agent, task.task_id, TaskTurnOutcome(ok=True, summary='must not overwrite')) is None
    after = store.session_tasks.require(task.task_id).to_dict()
    assert after == before
    assert request.wake_signal['source_agent_id'] == child.id


@pytest.mark.parametrize('ordinary', ['present', 'missing'])
def test_session_task_origin_does_not_hide_coalesced_ordinary_input(native, ordinary, monkeypatch):
    agent, task, _child, request = _case(native)
    request.wake_signal['metadata']['events'] = [{'metadata': {'conversation_request_id': 'ordinary-request'}}]
    if ordinary == 'missing':
        with pytest.raises(DataCorruptionError, match='background request input is missing'):
            _background_request_objective(agent.conversation_store, request)
    else:
        agent.conversation_store.messages.append({'thread_id': request.thread_id, 'role': 'user',
            'content': '真实额外输入', 'metadata': {'conversation_request_id': 'ordinary-request'}})
        assert _background_request_objective(agent.conversation_store, request) == task.goal + '\n\n真实额外输入'
        from agent_py_agent.agent.conversation import execution_threads
        original = execution_threads.lifecycle_session_task_origin
        reads = []

        def observe(*args, **kwargs):
            reads.append(1)
            return original(*args, **kwargs)

        monkeypatch.setattr(execution_threads, 'lifecycle_session_task_origin', observe)
        context = _goal_runtime_context(agent, agent.conversation_store, request)
        params = _run_params(request.thread_id, request, agent, goal_context=context)
        assert len(reads) == 1
        assert params.root_user_prompt == task.goal + '\n\n真实额外输入'
        assert params.business_context.to_dict() == task.metadata['business_context']
        assert params.task_id == task.task_id



@pytest.mark.parametrize('corruption', ['root', 'source', 'parent', 'owner', 'cancelled', 'failed', 'body', 'completed_link'])
def test_session_task_lifecycle_rejects_wrong_source_or_terminal_order(native, corruption):
    agent, task, child, request = _case(native, consume=corruption != 'body')
    from dataclasses import replace

    from agent_py_agent.agent.conversation.session_tasks import SessionTaskUpdate
    if corruption == 'root':
        request.wake_signal['root_task_id'] = 'different-root'
    elif corruption == 'source':
        request.wake_signal['source_agent_id'] = 'missing-child'
    elif corruption == 'parent':
        request.wake_signal['parent_agent_id'] = 'another-parent'
    elif corruption == 'owner':
        sender = agent.conversation_store.threads.require(task.sender_thread_id)
        agent.conversation_store.threads.write(replace(sender, owner_id='foreign-owner'))
    elif corruption == 'completed_link':
        agent.conversation_store.tasks.update_status({'task_id': task.task_id, 'status': 'completed'})
    elif corruption in {'cancelled', 'failed'}:
        agent.conversation_store.session_tasks.advance(task.task_id, SessionTaskUpdate(status=corruption))
    with pytest.raises((DataCorruptionError, ValueError)):
        _goal_runtime_context(agent, agent.conversation_store, request)
    assert agent.conversation_store.session_tasks.require(task.task_id).status == (
        corruption if corruption in {'cancelled', 'failed'} else 'accepted')


def test_native_submit_main_cycle_then_real_child_event_continues_without_repeating_result(native, monkeypatch):
    from agent_py_agent.agent.backends import ModelResponse
    from agent_py_agent.agent.backends.base import EchoBackend
    from agent_py_agent.agent.conversation import FakeDeliveryService
    from agent_py_agent.agent.conversation.session_task_report import result_report_key
    from agent_py_agent.cli.gateway_loops import _build_background_scheduler
    from agent_py_agent.tests.test_business_v12_background_scheduling import add_echo_profile
    add_echo_profile(native[1])
    agent, task, child, _ = _case(native, finished=False)
    store = agent.conversation_store
    seen, contexts = [], []
    original_run = agent.run
    def observe_run(prompt, *, params):
        contexts.append(params.business_context)
        return original_run(prompt, params=params)
    monkeypatch.setattr(agent, 'run', observe_run)
    def generate(backend, prompt, **kwargs):
        seen.append(prompt)
        return ModelResponse(text='one bounded main cycle', backend=backend.name)
    monkeypatch.setattr(EchoBackend, 'generate', generate)
    scheduler = _build_background_scheduler(agent, FakeDeliveryService())
    scheduler.tick_thread(task.target_thread_id)
    saved = store.session_tasks.require(task.task_id)
    assert saved.status == 'done'
    assert store.tasks.load_report(task.task_id)[0].status == 'active'
    original_report = store.guidance.receipt(result_report_key(saved)).entry.guidance_id
    agent.subagents.lifecycle.set_status(child.id, 'DONE')
    child = agent.subagents.load(child.id)
    request = _publish(agent, task, child)
    import time

    from agent_py_agent.agent.conversation.runtime import COMPLETION_COALESCE_WINDOW_SECONDS
    scheduler.tick_thread(request.thread_id, now=time.time() + COMPLETION_COALESCE_WINDOW_SECONDS + 1)
    assert len(seen) == 2
    assert contexts[-1].to_dict() == task.metadata['business_context']
    assert task.goal in seen[-1]
    assert store.session_tasks.require(task.task_id).status == 'done'
    assert store.guidance.receipt(result_report_key(saved)).entry.guidance_id == original_report
