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
    prepare_input_wait_reply,
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
from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot
from agent_py_agent.tests.test_background_tool_approval import background
from agent_py_agent.tests.test_task_credentials import _card
from agent_py_agent.tests.test_tool_loop_recovery_scope import _params

_FIXTURES = (background,)


@pytest.mark.parametrize('delivery', [None, 'unknown', 'started', 'not_started',
    {'secret': 'Synthetic-Private-Canary'}, 'invalid', 'confirmed'])
def test_credential_delivery_public_fact_uses_only_host_row(background, monkeypatch, delivery):
    from agent_py_agent.agent.tooling import task_credential_tool as module

    _card(background)
    monkeypatch.setattr(module, 'capability_config_for_agent', lambda _: SimpleNamespace(im_task_credentials_enabled=True))
    read = module.read_credential_row

    # LLM: 只替换临时宿主记录的投递字段；模型同名参数和秘密扩展都不得成为公开确认事实。
    # 函数用途: 模拟旧记录缺字段、未知回执或损坏字段，不接触真实凭据。
    def host_row(*args, **kwargs):
        row = read(*args, **kwargs)
        row.pop('delivery_state', None)
        if delivery is not None:
            row['delivery_state'] = delivery
        row['untrusted_extra'] = 'Synthetic-Private-Canary'
        return row

    monkeypatch.setattr(module, 'read_credential_row', host_row)
    outcome = RequestCredentialsTool(background.agent).execute({'url': 'https://example.com/api',
        'request_delivery': {'status': 'confirmed', 'secret': 'Synthetic-Private-Canary'}})
    assert outcome.ok
    expected = delivery if isinstance(delivery, str) and delivery in {'confirmed', 'unknown', 'started', 'not_started'} else 'unknown'
    assert outcome.result_envelope['credential']['request_delivery'] == {'status': expected}
    assert 'Synthetic-Private-Canary' not in outcome.output
    params = _params(task_id=background.task_id, run_id=background.task_id)
    record_input_wait(background.agent, params, SimpleNamespace(ok=True, metadata={'handler_details': outcome.result_envelope}))
    assert prepare_input_wait_reply(background.agent, params)
    from agent_py_agent.agent.agent_core.tool_loop.natural_user_reply import (
        natural_user_reply_model_params,
    )
    expression = natural_user_reply_model_params(replace(params,
        tool_protocol_snapshot=make_test_protocol_snapshot(run_id=params.run_id, source_protocol='native')))
    assert 'Synthetic-Private-Canary' not in str(expression.tool_ir_history)
    assert f'"status": "{expected}"' in expression.user_prompt


@pytest.mark.parametrize('delivery', [None, {'status': {'secret': 'Synthetic-Private-Canary'}},
    {'status': 'invalid', 'secret': 'Synthetic-Private-Canary'}])
def test_generic_input_does_not_inherit_previous_or_untyped_delivery(background, delivery):
    from agent_py_agent.agent.agent_core.tool_loop.natural_user_reply import (
        pending_natural_user_reply,
    )
    row = _dependency(background)
    params = _params(task_id=background.task_id, run_id=background.task_id)
    params.live_archive_state['waiting_input_delivery'] = ('other-input', 'confirmed')
    record_input_wait(background.agent, params, SimpleNamespace(ok=True, metadata={'handler_details': {
        'input_dependency': {'input_id': row['input_id'], 'status': 'waiting_input', 'request_delivery': delivery}}}))
    assert prepare_input_wait_reply(background.agent, params)
    facts = pending_natural_user_reply(params)['facts']
    assert 'request_delivery' not in facts['input_dependency']
    assert 'Synthetic-Private-Canary' not in json.dumps(facts)


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


def test_wait_tool_queues_one_reply_from_canonical_input_facts(background, monkeypatch):
    from agent_py_agent.agent.agent_core.tool_loop.natural_user_reply import (
        natural_user_reply_model_params,
        pending_natural_user_reply,
    )
    from agent_py_agent.agent.backends.tool_ir import UserTurn

    _card(background)
    monkeypatch.setattr('agent_py_agent.agent.tooling.task_credential_tool.capability_config_for_agent',
                        lambda _agent: SimpleNamespace(im_task_credentials_enabled=True))
    outcome = RequestCredentialsTool(background.agent).execute({'url':'https://example.com/api','authentication':'bearer'})
    dependency = outcome.result_envelope['input_dependency']
    assert outcome.ok and dependency['status'] == 'waiting_input'
    params = _params(task_id=background.task_id, run_id=background.task_id)
    record_input_wait(background.agent, params, SimpleNamespace(ok=True, metadata={'handler_details':outcome.result_envelope}))
    assert prepare_input_wait_reply(background.agent, params)
    phase = pending_natural_user_reply(params)
    assert phase['kind'] == 'input_wait'
    assert phase['facts']['input_dependency']['input_id'] == dependency['input_id']
    assert phase['facts']['input_dependency']['status'] == 'pending'
    assert phase['facts']['input_dependency']['request_delivery'] == {'status': 'confirmed'}
    assert phase['facts']['user_input_required'] is True
    assert phase['facts']['turn_execution'] == {
        'presentation_only': True, 'tool_execution_observed': False,
        'executed_tool_count': 0, 'successful_operation_count': 0, 'failed_operation_count': 0,
    }
    assert 'operation_verification' not in phase['facts']
    expression = natural_user_reply_model_params(replace(params,
        tool_protocol_snapshot=make_test_protocol_snapshot(run_id=params.run_id, source_protocol='native')))
    assert expression.allowed_tools == [] and expression.tool_context == []
    assert len(expression.tool_ir_history) == 1
    assert isinstance(expression.tool_ir_history[-1], UserTurn)
    assert expression.tool_ir_history[-1].text == '# User Task\n' + expression.user_prompt
    assert not prepare_input_wait_reply(background.agent, params)
    assert task_has_waiting_input(background.agent, background.task_id)
    assert background.store.wakes.pending() == []


def test_disabled_credentials_never_publishes_input(background, monkeypatch):
    monkeypatch.setattr('agent_py_agent.agent.tooling.task_credential_tool.capability_config_for_agent',
                        lambda _agent: SimpleNamespace(im_task_credentials_enabled=False))
    outcome = RequestCredentialsTool(background.agent).execute({'url':'https://example.com/api','authentication':'bearer'})
    assert outcome.error_code == 'TOOL_UNAVAILABLE'
    assert not list((background.store.session_tasks.storage.session_tasks_dir/'inputs').glob('*.json'))


def test_input_wait_projects_safe_row_facts_and_precedes_dispatch_without_draining(background, monkeypatch):
    from agent_py_agent.agent.agent_core import _tool_loop_service as loop
    from agent_py_agent.agent.agent_core.tool_loop.natural_user_reply import (
        natural_user_reply_model_params,
        pending_natural_user_reply,
        queue_natural_user_reply,
    )
    from agent_py_agent.agent.agent_core.tool_model_generation import _native_provider_messages
    from agent_py_agent.agent.backends.tool_ir import AssistantTurn, UserTurn

    row = create_input_dependency(background.agent, tool_approval_scope(background.agent, background.task_id),
        input_id='generic-safe-projection', provider='test-provider', expires_at=time.time()+60,
        metadata={'untrusted_extra': 'Synthetic-Private-Canary'})
    params = _params(task_id=background.task_id, run_id=background.task_id)
    params.executed_tools.append('write_file')
    params.archive_tool_calls.append({
        'tool': 'write_file', 'ok': True, 'handler_executed': True,
        'tool_operation_status': 'succeeded', 'effect_outcome': 'confirmed',
        'parameters': {'path': 'Synthetic-Private-Canary', 'content': 'Synthetic-Private-Canary',
            'action': 'Synthetic-Private-Canary'},
        'output': 'Synthetic-Private-Canary',
        'metadata': {'secret': 'Synthetic-Private-Canary'},
        'tool_result_envelope': {'secret': 'Synthetic-Private-Canary'},
        'operation_id': 'Synthetic-Private-Canary', 'call_id': 'Synthetic-Private-Canary',
        'result_refs': ['Synthetic-Private-Canary'],
    })
    queue_natural_user_reply(params, kind='background_dispatch', facts={'dispatch_registered': True})
    record_input_wait(background.agent, params, SimpleNamespace(ok=True, metadata={
        'handler_details': {'input_dependency': {'input_id': row['input_id'], 'status': 'waiting_input'}}}))
    assert prepare_input_wait_reply(background.agent, params)
    phase = pending_natural_user_reply(params)
    assert phase['kind'] == 'input_wait'
    assert set(phase['facts']['input_dependency']) == {'input_id', 'provider', 'status', 'expires_at', 'result_available'}
    assert 'Synthetic-Private-Canary' not in json.dumps(phase)
    assert phase['facts']['turn_execution']['successful_operation_count'] == 1
    assert set(phase['facts']['operation_verification']) == {
        'schema', 'status', 'operation_count', 'omitted_operation_count', 'counts', 'groups',
    }
    assert set(phase['facts']['operation_verification']['groups'][0]) == {
        'tool', 'action', 'label', 'status', 'count', 'replayed',
    }
    params.tool_context.append('Synthetic-Private-Canary')
    params.tool_ir_history.extend([UserTurn('Synthetic-Private-Canary'),
        AssistantTurn(text='Synthetic-Private-Canary')])
    params.provider_history_messages.append({'role': 'user', 'content': 'Synthetic-Private-Canary'})
    expression = natural_user_reply_model_params(replace(params,
        tool_protocol_snapshot=make_test_protocol_snapshot(run_id=params.run_id, source_protocol='native')))
    assert expression.allowed_tools == [] and expression.tool_context == []
    assert expression.provider_history_messages == []
    assert len(expression.tool_ir_history) == 1
    assert isinstance(expression.tool_ir_history[-1], UserTurn)
    assert 'Synthetic-Private-Canary' not in str(expression.tool_ir_history)
    assert 'Synthetic-Private-Canary' not in expression.user_prompt
    wire = _native_provider_messages(background.agent, expression)
    assert wire and wire[-1]['role'] == 'user'
    assert 'Synthetic-Private-Canary' not in json.dumps(wire)
    monkeypatch.setattr(loop, '_drain_pending_deferred_tool_calls', lambda *args: pytest.fail('等待回执不能重执行业务'))
    assert loop._pending_drain_outcome(background.agent, params, 1) == (1, None, False, '')
    phase['kind'] = 'input_resume_pending'
    assert loop._pending_drain_outcome(background.agent, params, 1) == (1, None, False, '')


@pytest.mark.parametrize('status', ['ready', 'cancelled', 'expired'])
def test_input_settled_after_archive_does_not_queue_stale_wait(background, status):
    from agent_py_agent.agent.agent_core.tool_loop.natural_user_reply import (
        pending_natural_user_reply,
    )

    row = _dependency(background)
    params = _params(task_id=background.task_id, run_id=background.task_id)
    record_input_wait(background.agent, params, SimpleNamespace(ok=True, metadata={
        'handler_details': {'input_dependency': {'input_id': row['input_id'], 'status': 'waiting_input'}}}))
    resolve_input_dependency(background.agent, row['input_id'], status=status)
    assert not prepare_input_wait_reply(background.agent, params)
    assert pending_natural_user_reply(params) is None
    resolution = json.loads(params.tool_context[-1].split('\n', 1)[1])
    assert resolution['input_id'] == row['input_id'] and resolution['status'] == status
    assert len(background.store.wakes.pending()) == 1
    assert not task_has_waiting_input(background.agent, background.task_id)


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


def test_real_tool_fake_model_archives_pair_then_only_generates_no_tools_reply(background):
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.backends import ModelResponse
    from agent_py_agent.agent.capability.config import CapabilityConfig
    from agent_py_agent.tests.test_task_progress_advisory import _CaptureBackend

    _card(background)
    background.agent._capability_config_runtime_snapshot = SimpleNamespace(config=CapabilityConfig(im_task_credentials_enabled=True))
    class Backend(_CaptureBackend):
        def generate(self, prompt, on_chunk=None, **kwargs):
            self.prompts.append(prompt)
            assert len(self.prompts) <= 2, '必要输入只允许业务申请和一次零工具表达，不得轮询'
            if len(self.prompts) == 2:
                assert not kwargs.get('tools')
                assert '[natural-user-reply]' in json.dumps(kwargs.get('messages') or [])
                return ModelResponse(text='请填写已发出的凭据卡，提交后继续原任务。', backend=self.name,
                    turn_end_reason='completed')
            return ModelResponse(text='', backend=self.name, tool_use_blocks=[{'id':'request-private-input',
                'name':'request_credentials','input':{'url':'https://example.com/api','authentication':'bearer'}}])
    backend = Backend()
    background.agent.backend = backend
    from agent_py_agent.agent.conversation.background_execution import _run_background_model_attempt
    from agent_py_agent.agent.conversation.claim_execution_binding import (
        ClaimExecutionBinding,
        bind_claim_execution,
    )

    # 复用后台正式适配器与原claim publisher；模型仍经真实agent.run，scope不从测试JSON或当前指针补猜。
    with bind_claim_execution(ClaimExecutionBinding(
        background.store.claims, background.thread.thread_id, background.claim['claim_id'], '',
    )):
        result, _bound = _run_background_model_attempt(background.agent, '读取原接口，必要时向我申请凭据。', RunParams(save=False,
            source='background_main_agent', run_id=background.task_id, task_id=background.task_id,
            allowed_tools=['request_credentials'], task_attributes={'conversation_thread_id':background.thread.thread_id,
                                                                  'conversation_task_id':background.task_id}))
    assert result.runtime_reason == 'TASK_INPUT_WAIT'
    assert result.runtime_status == 'needs_user_input'
    assert result.response == '请填写已发出的凭据卡，提交后继续原任务。'
    assert result.turn_end_reason == 'blocked' and len(backend.prompts) == 2
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
    assert not prepare_input_wait_reply(background.agent, params)
    assert 'opaque-ref' in params.tool_context[-1]


@pytest.mark.parametrize('depth',[1,2])
def test_blocked_child_private_input_resolves_via_original_root(background, depth):
    from agent_py_agent.agent.agent_core.tool_loop.natural_user_reply import (
        pending_natural_user_reply,
    )
    from agent_py_agent.agent.backends import ModelResponse
    from agent_py_agent.agent.conversation.input_tool_boundary import reconcile_input_wait_reply

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
    params = _params(task_id=root_id, run_id=child.id)
    params.live_archive_state['waiting_input_id'] = row['input_id']
    assert prepare_input_wait_reply(background.agent, params)
    background.agent.subagents.lifecycle.set_status(child.id,'BLOCKED')
    body = {'reference':row['reference'],'decision':'submit','message_id':row['message_id'],'chat_id':row['chat_id'],
            'fields':{'secret':'SyntheticChildToken'}}
    assert resolve_private_interaction(background.agent,body,channel='feishu',actor_id='ou_synthetic')['ok']
    wake = background.store.wakes.pending()[-1]
    assert wake.root_task_id == root_id and wake.thread_id == background.thread.thread_id
    assert wake.metadata['input_run_id'] == child.id
    from dataclasses import asdict

    from agent_py_agent.agent.conversation.lifecycle_wake_event import lifecycle_wake_turn_trigger
    facts = json.loads(lifecycle_wake_turn_trigger('task_input_resolved', asdict(wake), []).event_facts)
    assert facts['input_dependency']['status'] == 'ready'
    assert facts['input_dependency']['result_available'] is True
    assert 'SyntheticChildToken' not in json.dumps(facts)
    verdict, _ = reconcile_input_wait_reply(background.agent, params, ModelResponse(text='旧草稿', backend='synthetic'))
    assert verdict == 'changed'
    assert pending_natural_user_reply(params)['kind'] == 'input_resume_pending'
    assert background.store.wakes.delivery_receipt(background.thread.thread_id, 'task-input:' + row['input_id']) == 'pending'


@pytest.mark.parametrize('receipt', ['', 'handled'])
def test_input_terminal_without_original_pending_wake_fails_explicitly(background, receipt, monkeypatch):
    from agent_py_agent.agent.backends import ModelResponse
    from agent_py_agent.agent.conversation.input_tool_boundary import reconcile_input_wait_reply

    row = _dependency(background)
    params = _params(task_id=background.task_id, run_id=background.task_id)
    params.live_archive_state['waiting_input_id'] = row['input_id']
    arm_input_dependency(background.agent, row['input_id'])
    assert prepare_input_wait_reply(background.agent, params)
    resolve_input_dependency(background.agent, row['input_id'])
    monkeypatch.setattr(background.store.wakes, 'delivery_receipt', lambda *args: receipt)
    verdict, response = reconcile_input_wait_reply(background.agent, params, ModelResponse(text='旧草稿', backend='synthetic'))
    assert verdict == 'finish' and response.runtime_status == 'failed'
    assert response.runtime_reason == 'TASK_INPUT_WAKE_UNAVAILABLE'
    assert not response.text and len(background.store.wakes.pending()) == 1


@pytest.mark.parametrize('wrong_run', [False, True])
def test_handled_input_requires_same_runtime_run_and_a_real_successor_attempt(background, wrong_run):
    from agent_py_agent.agent.conversation.input_tool_boundary import _consumed_input_reply_status

    db = background.agent.subagents.runtime_db
    record = db.record_run_creation(owner_id='local/main', goal='合成输入恢复',
        conversation_task_id=background.task_id, thread_id=background.thread.thread_id,
        run_id='business-main-run', role='main')
    row = _dependency(background)
    params = _params(task_id=background.task_id, run_id='business-main-run', attempt_id=record['attempt_id'])
    assert record['agent_run_id'] != params.run_id
    assert _consumed_input_reply_status(background.agent, params, row, 'handled') == ('failed', 'TASK_INPUT_WAKE_UNAVAILABLE')
    db.create_attempt(record['agent_run_id'])
    if wrong_run:
        params = replace(params, run_id='unrelated-run')
    assert _consumed_input_reply_status(background.agent, params, row, 'handled') == (
        ('failed', 'TASK_INPUT_WAKE_UNAVAILABLE') if wrong_run else ('cancelled', 'TASK_INPUT_SUPERSEDED'))


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
            assert len(self.prompts) <= 3
            if len(self.prompts) == 2:
                assert not kwargs.get('tools')
                return ModelResponse(text='请填写已发出的卡片，提交后继续。', backend=self.name)
            if len(self.prompts) == 3:
                return ModelResponse(text='原任务已沿输入引用恢复。',backend=self.name)
            return ModelResponse(text='', backend=self.name, tool_use_blocks=[{'id':'bg-private-input',
                'name':'request_credentials','input':{'url':'https://example.com/api','authentication':'bearer'}}])
    background.agent.backend = backend = Backend()
    runtime = BackgroundMainAgentRuntime(agent=background.agent,store=background.store)
    scheduler = BackgroundMainAgentScheduler({'runtime':runtime,'store':background.store})
    report = run_claimed(_background_claim_dependencies(scheduler), {
        'thread_id':background.thread.thread_id,'task_id':background.task_id,'reason':'incoming_channel_message'})
    assert report.runtime_reason == 'TASK_INPUT_WAIT' and len(backend.prompts) == 2
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
    assert len(backend.prompts) == 3
    scheduler.tick_thread(background.thread.thread_id)
    assert len(backend.prompts) == 3
