"""前台审批范围复用准确 Gateway 车道和 RuntimeDB，不把请求、任务和 attempt 混为一个编号。"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.agent_core.runtime_mixin import _bind_main_agent_authority
from agent_py_agent.agent.conversation.tool_approval_scope import (
    tool_approval_scope,
    tool_approval_scope_is_current,
)
from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.request_binding import GatewayTaskBindingWriter
from agent_py_agent.agent.tooling.task_credential_tool import RequestCredentialsTool
from agent_py_agent.tests.test_ingestion_task_credential_scope import _active, _agent


# LLM: 原 TaskLink 和 RuntimeDB 都真建；请求、run、task、运输 attempt 故意分开，队列也不在 scoped owner 下。
# 函数用途: 创建与真实前台同形的 claim、原 writer 及持久执行链，不发卡或模型请求。
def _foreground(tmp_path, *, kind='group'):
    agent = _agent(tmp_path, kind)
    thread, actor, old_claim = _active(agent, root_id='durable-task')
    store = agent.conversation_store
    store.claims.finish({'thread_id': thread.thread_id, 'claim_id': old_claim['claim_id'], 'status': 'finished'})
    paths = gateway_paths_from_root(tmp_path / 'base-gateway')
    path = paths.processing / 'message.json'
    write_json_file(path, {'id': 'message', 'status': 'processing', 'turn_phase': 'open',
        'execution_attempt_id': 'transport-attempt'})
    writer = GatewayTaskBindingWriter(path, 'message', execution_attempt_id='transport-attempt')
    writer.bind_conversation_claim(thread.thread_id)
    claim = store.claims.acquire({'thread_id': thread.thread_id, 'task_id': 'gateway:message',
        'reason': 'gateway_foreground_turn'})
    params = _bind_main_agent_authority(agent, RunParams(request_id='message', run_id='main-run', task_id='durable-task',
        source='gateway', context_scope='conversation', task_attributes={'conversation_thread_id': thread.thread_id,
            'conversation_task_id': 'durable-task'}, host_channel_actor=actor,
        conversation_task_binding_callback=writer))
    agent._current_run_params = params
    return SimpleNamespace(agent=agent, store=store, paths=paths, path=path, params=params, claim=claim, thread=thread)


def test_foreground_scope_uses_original_queue_and_exact_runtime_identity(tmp_path):
    world = _foreground(tmp_path)
    scope = tool_approval_scope(world.agent, 'durable-task')
    assert scope.active
    assert scope.root_task_id == scope.run_id == 'durable-task'
    assert scope.claim_id == world.claim['claim_id']
    assert scope.execution_attempt_id == world.params.attempt_id
    assert tool_approval_scope(world.agent, 'main-run') == scope
    world.agent._current_run_params = replace(world.params, attempt_id='obsolete-attempt')
    assert not tool_approval_scope(world.agent, 'main-run').active


@pytest.mark.parametrize('fault', ['closed', 'cancelled', 'terminal', 'wrong_binding', 'transport',
    'runtime_missing', 'runtime_task', 'runtime_run', 'runtime_agent', 'runtime_attempt'])
def test_changed_or_unbound_original_request_is_not_foreground_authority(tmp_path, fault):
    world = _foreground(tmp_path)
    payload = read_json_file(world.path)
    if fault == 'closed':
        payload['turn_phase'] = 'closed'
    elif fault == 'cancelled':
        payload['cancel_requested'] = True
    elif fault == 'terminal':
        write_json_file(world.paths.terminal / 'message.json', {'id': 'message'})
    elif fault == 'wrong_binding':
        payload['conversation_claim']['thread_id'] = 'other-thread'
    elif fault == 'transport':
        payload['execution_attempt_id'] = 'new-transport-attempt'
        payload['runtime_authority']['gateway_execution_attempt_id'] = 'new-transport-attempt'
    elif fault == 'runtime_missing':
        payload.pop('runtime_authority')
    else:
        key = {'runtime_task': 'task_id', 'runtime_run': 'run_id', 'runtime_agent': 'agent_run_id',
            'runtime_attempt': 'attempt_id'}[fault]
        payload['runtime_authority'][key] = 'another-' + key
    write_json_file(world.path, payload)
    assert not tool_approval_scope(world.agent, 'main-run').active


@pytest.mark.parametrize('fault', ['no_writer', 'duck_writer', 'other_request', 'other_task', 'wrong_path', 'old_attempt'])
def test_untrusted_or_obsolete_current_context_does_not_acquire_scope(tmp_path, fault):
    world = _foreground(tmp_path)
    writer = world.params.conversation_task_binding_callback
    if fault == 'old_attempt':
        run = world.agent.subagents.runtime_db.agent_run_for_run_id('main-run')
        world.agent.subagents.runtime_db.create_attempt(run['agent_run_id'])
    else:
        changes = {
            'no_writer': {'conversation_task_binding_callback': None},
            'duck_writer': {'conversation_task_binding_callback': SimpleNamespace(**vars(writer))},
            'other_request': {'request_id': 'another-request'},
            'other_task': {'task_id': 'another-task'},
            'wrong_path': {'conversation_task_binding_callback': replace(writer,
                request_path=world.paths.inbox / 'message.json')},
        }[fault]
        world.agent._current_run_params = replace(world.params, **changes)
    assert not tool_approval_scope(world.agent, 'durable-task').active


def test_same_claim_new_core_attempt_cannot_reuse_old_scope(tmp_path):
    world = _foreground(tmp_path)
    old = tool_approval_scope(world.agent, 'main-run')
    current = _bind_main_agent_authority(world.agent, world.params)
    world.agent._current_run_params = current
    fresh = tool_approval_scope(world.agent, 'main-run')
    assert fresh.active and fresh.claim_id == old.claim_id
    assert fresh.execution_attempt_id != old.execution_attempt_id
    assert not tool_approval_scope_is_current(world.agent, old)


def test_foreign_owner_writer_and_runtime_projection_cannot_authorize_local_task(tmp_path):
    world = _foreground(tmp_path / 'first')
    foreign = _foreground(tmp_path / 'foreign', kind='user')
    assert world.params.attempt_id != foreign.params.attempt_id
    world.agent._current_run_params = foreign.params
    assert not tool_approval_scope(world.agent, 'durable-task').active


def test_missing_current_writer_does_not_guess_owner_local_gateway_path(tmp_path):
    world = _foreground(tmp_path)
    del world.agent._current_run_params
    assert not tool_approval_scope(world.agent, 'durable-task').active


@pytest.mark.parametrize('unreadable', [False, True])
def test_credentials_scope_failure_is_not_misreported_as_storage(tmp_path, monkeypatch, unreadable):
    agent = _agent(tmp_path, 'group')
    agent._current_run_params = RunParams(run_id='missing-run', task_id='missing-task')
    if unreadable:
        monkeypatch.setattr(agent.conversation_store.tasks, 'load_report', lambda _task: (None, {'error': 'Synthetic-Private-Error'}))
    result = RequestCredentialsTool(agent).execute({})
    assert not result.ok and result.error_code == 'CREDENTIAL_TASK_NOT_ACTIVE'
    assert result.effect_outcome == 'not_started'
    assert result.result_envelope['credential_error']['stage'] == 'execution_scope'
    assert 'Synthetic-Private-Error' not in str(result)
