import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.json_io import read_json_object_report
from agent_py_agent.agent.conversation.input_dependencies import (
    arm_input_dependency,
    create_input_dependency,
    input_dependency_current,
    input_dependency_path,
    reconcile_input_dependencies,
    resolve_input_dependency,
    task_has_waiting_input,
)
from agent_py_agent.agent.conversation.input_tool_boundary import (
    input_wait_response,
    record_input_wait,
)
from agent_py_agent.agent.conversation.task_rejected_actions import sync_task_rejected_actions
from agent_py_agent.agent.conversation.tool_approval_scope import tool_approval_scope
from agent_py_agent.agent.gateway_parts.private_interaction_service import (
    resolve_private_interaction,
)
from agent_py_agent.agent.tooling.task_credential_tool import RequestCredentialsTool
from agent_py_agent.agent.user_space.task_credentials import (
    CredentialCardOptions,
    credential_headers,
    credential_path,
    manage_credential,
    request_credential_card,
)
from agent_py_agent.tests.test_background_tool_approval import background
from agent_py_agent.tests.test_task_credentials import _card
from agent_py_agent.tests.test_tool_loop_recovery_scope import _params

_FIXTURES = (background,)


def _dependency(ctx, input_id='test-input', expires_at=None):
    scope = tool_approval_scope(ctx.agent, ctx.task_id)
    return create_input_dependency(ctx.agent, scope, input_id=input_id, provider='test-provider',
                                   expires_at=expires_at or time.time()+60)


def _finish_claim(ctx):
    ctx.store.claims.finish({'thread_id':ctx.thread.thread_id,'task_id':ctx.task_id,
                            'claim_id':ctx.claim['claim_id'],'status':'completed'})


def test_callback_survives_finished_claim_and_replays_one_wake(background):
    ref, path, body, _sent = _card(background)
    row = read_json_object_report(path, context='test').payload
    arm_input_dependency(background.agent, row['input_id'])
    _finish_claim(background)
    assert not tool_approval_scope(background.agent, background.task_id).active
    assert resolve_private_interaction(background.agent, body, channel='feishu', actor_id='ou_synthetic')['ok']
    wakes = background.store.wakes.pending(limit=0)
    assert len(wakes) == 1 and wakes[0].reason == 'task_input_resolved'
    background.store.wakes.mark_handled(wakes[0].wake_signal_id)
    reconcile_input_dependencies(background.agent, task_id=background.task_id)
    assert background.store.wakes.pending(limit=0) == []
    assert resolve_private_interaction(background.agent, body, channel='feishu', actor_id='ou_synthetic')['ok']
    assert background.store.wakes.pending(limit=0) == []
    successor = background.store.claims.acquire({'thread_id':background.thread.thread_id,
        'task_id':background.task_id,'reason':'task_input_resolved'})
    assert successor['claim_id'] != background.claim['claim_id']
    headers, _secrets = credential_headers(background.agent, ref, 'https://example.com/api')
    assert headers['Authorization'].startswith('Basic ')


def test_callback_before_pair_archive_is_recorded_but_not_dispatched(background):
    row = _dependency(background)
    resolve_input_dependency(background.agent, row['input_id'], result_refs=('opaque-ref',))
    assert background.store.wakes.pending() == []
    arm_input_dependency(background.agent, row['input_id'])
    arm_input_dependency(background.agent, row['input_id'])
    assert len(background.store.wakes.pending()) == 1
    with pytest.raises(ValueError):
        resolve_input_dependency(background.agent, row['input_id'], status='cancelled')


def test_wait_tool_returns_immediately_with_structured_boundary(background, monkeypatch):
    _card(background)
    monkeypatch.setattr('agent_py_agent.agent.tooling.task_credential_tool.capability_config_for_agent',
                        lambda _agent: SimpleNamespace(im_task_credentials_enabled=True))
    outcome = RequestCredentialsTool(background.agent).execute({'url':'https://example.com/api','authentication':'bearer'})
    dependency = outcome.result_envelope['input_dependency']
    assert outcome.ok and dependency['status'] == 'waiting_input'
    params = _params(task_id=background.task_id, run_id=background.task_id)
    record_input_wait(background.agent, params, SimpleNamespace(ok=True, metadata={'handler_details':outcome.result_envelope}))
    result = input_wait_response(background.agent, params)
    assert result.text == '' and result.runtime_reason == 'TASK_INPUT_WAIT' and result.turn_end_reason == 'blocked'
    assert task_has_waiting_input(background.agent, background.task_id)
    assert background.store.wakes.pending() == []


def test_disabled_credentials_never_publishes_input(background, monkeypatch):
    monkeypatch.setattr('agent_py_agent.agent.tooling.task_credential_tool.capability_config_for_agent',
                        lambda _agent: SimpleNamespace(im_task_credentials_enabled=False))
    outcome = RequestCredentialsTool(background.agent).execute({'url':'https://example.com/api','authentication':'bearer'})
    assert outcome.error_code == 'TOOL_UNAVAILABLE'
    assert not list((background.store.session_tasks.storage.session_tasks_dir/'inputs').glob('*.json'))


@pytest.mark.parametrize('status', ['cancelled', 'expired'])
def test_terminal_input_produces_one_reasoned_wake(background, status):
    row = _dependency(background)
    arm_input_dependency(background.agent, row['input_id'])
    resolve_input_dependency(background.agent, row['input_id'], status=status)
    reconcile_input_dependencies(background.agent, task_id=background.task_id)
    assert len(background.store.wakes.pending()) == 1
    assert background.store.wakes.pending()[0].metadata['input_status'] == status
    assert not task_has_waiting_input(background.agent, background.task_id)


def test_absolute_expiry_releases_dependency_and_ready_cannot_revive(background):
    row = _dependency(background)
    arm_input_dependency(background.agent, row['input_id'])
    reconcile_input_dependencies(background.agent, task_id=background.task_id, now=row['expires_at']+1)
    with pytest.raises(ValueError):
        resolve_input_dependency(background.agent, row['input_id'])
    assert not task_has_waiting_input(background.agent, background.task_id)
    assert len(background.store.wakes.pending()) == 1


def test_cancelled_root_rejects_callback_and_does_not_resume(background):
    _ref, path, body, _sent = _card(background)
    row = read_json_object_report(path, context='test').payload
    arm_input_dependency(background.agent, row['input_id'])
    background.store.tasks.update_status({'task_id':background.task_id,'status':'cancelled'})
    with pytest.raises(ValueError):
        resolve_private_interaction(background.agent, body, channel='feishu', actor_id='ou_synthetic')
    reconcile_input_dependencies(background.agent, task_id=background.task_id)
    assert background.store.wakes.pending() == []
    assert not task_has_waiting_input(background.agent, background.task_id)


def test_input_binding_owner_and_parameter_conflicts_fail_closed(background):
    row = _dependency(background)
    assert not input_dependency_current(background.agent, {**row, 'owner':{'owner_kind':'other'}})
    scope = tool_approval_scope(background.agent, background.task_id)
    with pytest.raises(ValueError):
        create_input_dependency(background.agent, scope, input_id=row['input_id'], provider='other-provider', expires_at=time.time()+60)
    assert input_dependency_path(background.agent, 'x/y') != input_dependency_path(background.agent, 'x_y')


def test_credential_ready_ttl_update_and_revoke(background, monkeypatch):
    ref, path, body, sent = _card(background)
    resolve_private_interaction(background.agent, body, channel='feishu', actor_id='ou_synthetic')
    row = read_json_object_report(path, context='test').payload
    row['ready_expires_at'] = time.time()-1
    path.write_text(json.dumps(row))
    with pytest.raises(ValueError, match='过期'):
        credential_headers(background.agent, ref, 'https://example.com/api')
    updated, _scope = request_credential_card(background.agent, url='https://example.com/api', authentication='basic', options=CredentialCardOptions(refresh_ref=ref))
    assert updated != ref and len(sent) == 2
    updated_row = read_json_object_report(credential_path(background.agent.home_paths.owner_home_dir, updated), context='test').payload
    assert updated_row['replaces_ref'] == ref
    assert read_json_object_report(path, context='test').payload == row
    assert updated_row['generation'] == 2 and updated_row['input_id'] != row['input_id']
    assert manage_credential(background.agent, ref, operation='revoke')['status'] == 'revoked'
    assert 'ciphertext' not in path.read_text()
    with pytest.raises(ValueError):
        credential_headers(background.agent, ref, 'https://example.com/api')


def test_same_task_rejected_actions_survive_new_attempt(background):
    rejection = {'tool_name':'controlled_exec','args_hash':'hash','reason_code':'APPROVAL_EXPIRED', 'decision':'unavailable'}
    params = _params(task_id=background.task_id, runtime_rejected_actions=[rejection])
    sync_task_rejected_actions(background.agent, params)
    resumed = _params(task_id=background.task_id, attempt_id='another-attempt')
    sync_task_rejected_actions(background.agent, resumed)
    assert resumed.runtime_rejected_actions == [rejection]
    other = _params(task_id='other-task')
    sync_task_rejected_actions(background.agent, other)
    assert other.runtime_rejected_actions == []


def test_real_tool_fake_model_archives_pair_and_stops_after_one_model_request(background):
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.backends import ModelResponse
    from agent_py_agent.agent.capability.config import CapabilityConfig
    from agent_py_agent.tests.test_task_progress_advisory import _CaptureBackend

    _card(background)
    background.agent._capability_config_runtime_snapshot = SimpleNamespace(config=CapabilityConfig(im_task_credentials_enabled=True))
    class Backend(_CaptureBackend):
        def generate(self, prompt, on_chunk=None, **kwargs):
            self.prompts.append(prompt)
            assert len(self.prompts) == 1, '必要输入不得继续模型轮询'
            return ModelResponse(text='', backend=self.name, tool_use_blocks=[{'id':'request-private-input',
                'name':'request_credentials','input':{'url':'https://example.com/api','authentication':'bearer'}}])
    backend = Backend()
    background.agent.backend = backend
    result = background.agent.run('读取原接口，必要时向我申请凭据。', params=RunParams(save=False,
        source='background_main_agent', run_id=background.task_id, task_id=background.task_id,
        allowed_tools=['request_credentials'], task_attributes={'conversation_thread_id':background.thread.thread_id,
                                                              'conversation_task_id':background.task_id}))
    assert result.runtime_reason == 'TASK_INPUT_WAIT'
    assert result.turn_end_reason == 'blocked' and len(backend.prompts) == 1
    assert len(result.archive_tool_calls) == 1
    assert result.archive_tool_calls[0]['tool'] == 'request_credentials'
    assert result.archive_tool_calls[0]['tool_result_envelope']['input_dependency']['status'] == 'waiting_input'
    assert background.store.tasks.load(background.task_id).status == 'active'


def test_replayed_wait_result_after_ready_does_not_park_again(background):
    row = _dependency(background)
    arm_input_dependency(background.agent, row['input_id'])
    resolve_input_dependency(background.agent, row['input_id'], result_refs=('opaque-ref',))
    params = _params(task_id=background.task_id)
    record_input_wait(background.agent, params, SimpleNamespace(ok=True,metadata={'handler_details':{
        'input_dependency':{'input_id':row['input_id'],'status':'waiting_input'}}}))
    assert input_wait_response(background.agent, params) is None
    assert 'opaque-ref' in params.tool_context[-1]


@pytest.mark.parametrize('depth',[1,2])
def test_blocked_child_private_input_resolves_via_original_root(background, depth):
    _card(background)
    root_id, parent_id = background.task_id, background.task_id
    for _ in range(depth):
        child = background.agent.subagents.create_run(goal='合成必要输入',root_id=root_id,parent_id=parent_id)
        child = background.agent.subagents.lifecycle.prepare_runner_attempt(child.id)
        parent_id = child.id
    background.agent._current_run_params = SimpleNamespace(run_id=child.id,task_id=root_id,task_attributes={})
    ref, _scope = request_credential_card(background.agent,url='https://example.com/api',authentication='bearer')
    row = read_json_object_report(credential_path(background.agent.home_paths.owner_home_dir,ref),context='test').payload
    arm_input_dependency(background.agent,row['input_id'])
    background.agent.subagents.lifecycle.set_status(child.id,'BLOCKED')
    body = {'reference':row['reference'],'decision':'submit','message_id':row['message_id'],'chat_id':row['chat_id'],
            'fields':{'secret':'SyntheticChildToken'}}
    assert resolve_private_interaction(background.agent,body,channel='feishu',actor_id='ou_synthetic')['ok']
    wake = background.store.wakes.pending()[-1]
    assert wake.root_task_id == root_id and wake.thread_id == background.thread.thread_id
    assert wake.metadata['input_run_id'] == child.id


def test_real_background_claim_is_released_for_pending_input(background):
    from agent_py_agent.agent.backends import ModelResponse
    from agent_py_agent.agent.capability.config import CapabilityConfig
    from agent_py_agent.agent.conversation.background_claim import run_claimed
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
        _background_claim_dependencies,
    )
    from agent_py_agent.tests.test_task_progress_advisory import _CaptureBackend

    _card(background)
    background.store.messages.append({'thread_id':background.thread.thread_id,'role':'user',
        'content':'读取原接口，必要时向我申请凭据。','metadata':{'conversation_request_id':background.task_id}})
    _finish_claim(background)
    background.agent._capability_config_runtime_snapshot = SimpleNamespace(config=CapabilityConfig(im_task_credentials_enabled=True))
    class Backend(_CaptureBackend):
        def generate(self, prompt, on_chunk=None, **kwargs):
            self.prompts.append(prompt)
            assert len(self.prompts) <= 2
            if len(self.prompts) == 2:
                return ModelResponse(text='原任务已沿输入引用恢复。',backend=self.name)
            return ModelResponse(text='', backend=self.name, tool_use_blocks=[{'id':'bg-private-input',
                'name':'request_credentials','input':{'url':'https://example.com/api','authentication':'bearer'}}])
    background.agent.backend = backend = Backend()
    runtime = BackgroundMainAgentRuntime(agent=background.agent,store=background.store)
    scheduler = BackgroundMainAgentScheduler({'runtime':runtime,'store':background.store})
    report = run_claimed(_background_claim_dependencies(scheduler), {
        'thread_id':background.thread.thread_id,'task_id':background.task_id,'reason':'incoming_channel_message'})
    assert report.runtime_reason == 'TASK_INPUT_WAIT' and len(backend.prompts) == 1
    claim, error = background.store.claims.load_report(background.thread.thread_id)
    assert not error and claim['status'] != 'running'
    assert background.store.tasks.load(background.task_id).status == 'active'
    next_claim = background.store.claims.acquire({'thread_id':background.thread.thread_id,
        'task_id':'next-conversation-message','reason':'incoming_channel_message'})
    assert next_claim is not None
    background.store.claims.finish({'thread_id':background.thread.thread_id,
        'claim_id':next_claim['claim_id'],'task_id':'next-conversation-message','status':'completed'})
    root = background.store.tasks.load(background.task_id)
    waiting = next(read_json_object_report(input_dependency_path(background.agent,input_id),context='test').payload
                   for input_id in root.input_dependency_refs
                   if read_json_object_report(input_dependency_path(background.agent,input_id),context='test').payload.get('armed'))
    credential = read_json_object_report(credential_path(background.agent.home_paths.owner_home_dir,waiting['reference']),context='test').payload
    callback = {'reference':credential['reference'],'decision':'submit','message_id':credential['message_id'],
                'chat_id':credential['chat_id'],'fields':{'secret':'SyntheticResumeToken'}}
    assert resolve_private_interaction(background.agent,callback,channel='feishu',actor_id='ou_synthetic')['ok']
    scheduler.tick_thread(background.thread.thread_id)
    assert len(backend.prompts) == 2
    scheduler.tick_thread(background.thread.thread_id)
    assert len(backend.prompts) == 2
