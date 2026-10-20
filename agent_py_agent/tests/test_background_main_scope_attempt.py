"""后台输入与审批共用原claim/core身份；仅合成owner与fake模型/运输。"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.tool_approval_scope import (
    tool_approval_scope,
    tool_approval_scope_is_current,
)
from agent_py_agent.tests.test_background_tool_approval import (
    _bind_background_authority,
    background,
)

_FIXTURES = (background,)


def test_background_scope_freezes_published_actual_attempt(background):
    params = _bind_background_authority(background)
    scope = tool_approval_scope(background.agent, params.run_id)
    assert scope.active
    assert scope.execution_attempt_id == params.attempt_id
    assert scope.claim_id == background.claim['claim_id']
    assert scope.root_task_id == background.task_id
    assert tool_approval_scope_is_current(background.agent, scope)


def test_cold_background_scope_reads_only_original_published_attempt(background):
    params = _bind_background_authority(background)
    scope = tool_approval_scope(background.agent, background.task_id)
    del background.agent._current_run_params
    cold = tool_approval_scope(background.agent, background.task_id)
    assert cold.active and cold == scope
    assert cold.execution_attempt_id == params.attempt_id
    assert tool_approval_scope_is_current(background.agent, scope)
    repo = background.agent.subagents.runtime_db
    run = repo.agent_run_for_run_id(params.run_id)
    successor = repo.create_attempt(run['agent_run_id'])
    assert successor['attempt_id'] != params.attempt_id
    assert not tool_approval_scope(background.agent, background.task_id).active
    assert not tool_approval_scope_is_current(background.agent, scope)


@pytest.mark.parametrize('fault', ['task', 'run', 'attempt', 'request', 'thread', 'callback', 'closed'])
def test_hot_background_scope_rejects_explicit_context_conflicts(background, fault):
    params = _bind_background_authority(background)
    if fault == 'closed':
        params.conversation_task_binding_callback.finish()
    else:
        changes = {
            'task': {'task_id': 'another-task'},
            'run': {'run_id': 'another-run'},
            'attempt': {'attempt_id': 'another-attempt'},
            'request': {'request_id': 'another-request'},
            'thread': {'task_attributes': {'conversation_thread_id': 'another-thread'}},
            'callback': {'conversation_task_binding_callback': SimpleNamespace(
                runtime_authority=params.conversation_task_binding_callback.runtime_authority,
                request_id=params.request_id)},
        }[fault]
        background.agent._current_run_params = replace(params, **changes)
    assert not tool_approval_scope(background.agent, background.task_id).active


def test_published_background_cannot_fall_back_to_legacy_without_attempt(background):
    params = _bind_background_authority(background)
    background.agent._current_run_params = replace(params, attempt_id='')
    assert not tool_approval_scope(background.agent, background.task_id).active


def test_legacy_unpublished_claim_keeps_only_original_empty_attempt(background):
    scope = tool_approval_scope(background.agent, background.task_id)
    assert scope.active and scope.execution_attempt_id == ''
    assert tool_approval_scope_is_current(background.agent, scope)


@pytest.mark.parametrize('field', ['claim_id', 'task_id', 'run_id', 'agent_run_id', 'attempt_id', 'schema_version'])
def test_cold_scope_rejects_mismatched_claim_authority(background, field):
    from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic

    _bind_background_authority(background)
    del background.agent._current_run_params
    path = background.store.claims.storage.background_claim_path(background.thread.thread_id)

    def corrupt(value):
        return {**value, 'runtime_authority': {**value['runtime_authority'], field: 'another-value'}}

    update_json_file_atomic(path, corrupt)
    assert not tool_approval_scope(background.agent, background.task_id).active


# LLM: 真实core绑定→原scope→原输入落盘→原等待结束事件，不从current补输入代次。
# 函数用途: 为准确冻结attempt的取消正负例建立同一真实运行与输入。
def _retired_background(ctx):
    import time

    from agent_py_agent.agent.conversation.input_dependencies import (
        arm_input_dependency,
        create_input_dependency,
        input_dependency_access,
    )
    params = _bind_background_authority(ctx)
    scope = tool_approval_scope(ctx.agent, ctx.task_id)
    row = create_input_dependency(ctx.agent, scope, input_id='bound-background-input',
                                  provider='synthetic', expires_at=time.time()+180)
    arm_input_dependency(ctx.agent, row['input_id'])
    access = input_dependency_access(ctx.agent)
    access.mutate(row['input_id'], lambda value: {**value, 'status':'cancelled',
        'armed':False, 'resolution_delivery':'retire'})
    repo = ctx.agent.subagents.runtime_db
    run = repo.agent_run_for_run_id(params.run_id)
    assert repo.settle_agent_attempt(agent_run_id=run['agent_run_id'], attempt_id=params.attempt_id,
        payload={'runtime_status':'needs_user_input','runtime_reason':'TASK_INPUT_WAIT',
                 'runtime_source':'input_dependency'})['settled']
    return params, access, repo, row['input_id'], run


def test_frozen_background_attempt_resolves_real_run_distinct_from_approval_task(background):
    from agent_py_agent.agent.conversation.task_run_closeout import settle_retired_input_run
    params, access, repo, input_id, run = _retired_background(background)
    assert params.run_id != background.task_id
    assert access.read(input_id)['scope']['execution_attempt_id'] == params.attempt_id
    assert settle_retired_input_run(access, repo, input_id,
                                   run_id=params.run_id, attempt_id=params.attempt_id)
    assert repo.get_agent_run(run['agent_run_id'])['status'] == 'cancelled'
    assert repo.get_task_run(run['task_run_id'])['status'] == 'cancelled'
    assert background.store.tasks.load(background.task_id).status == 'active'
    assert background.store.wakes.pending() == []


@pytest.mark.parametrize('fault', ['missing_attempt','old_attempt','callback_run','callback_attempt',
    'cross_task','cross_thread','cross_owner','generation','child_alias','unknown_run','unknown_attempt'])
def test_retired_frozen_attempt_cannot_select_another_execution(background, fault):
    from agent_py_agent.agent.conversation.task_run_closeout import settle_retired_input_run
    params, access, repo, input_id, run = _retired_background(background)
    callback = {'run_id':params.run_id, 'attempt_id':params.attempt_id}
    if fault in {'missing_attempt','cross_task','cross_thread','child_alias'}:
        changes = {'missing_attempt':{'execution_attempt_id':''},
            'cross_task':{'root_task_id':'another-task'}, 'cross_thread':{'thread_id':'another-thread'},
            'child_alias':{'agent_kind':'subagent'}}[fault]
        access.mutate(input_id, lambda row: {**row, 'scope':{**row['scope'], **changes}})
    elif fault == 'old_attempt':
        repo.create_attempt(run['agent_run_id'])
    elif fault.startswith('callback_'):
        callback[fault.removeprefix('callback_')+'_id'] = 'another-value'
    else:
        with repo.transaction() as conn:
            if fault == 'cross_owner':
                conn.execute("UPDATE tasks SET owner_id='user/other' WHERE task_id=?", (background.task_id,))
            elif fault == 'generation':
                conn.execute('UPDATE agent_runs SET current_attempt_generation=current_attempt_generation+1 WHERE agent_run_id=?', (run['agent_run_id'],))
            elif fault == 'unknown_run':
                conn.execute("UPDATE agent_runs SET status='unknown' WHERE agent_run_id=?", (run['agent_run_id'],))
            else:
                conn.execute("UPDATE agent_attempts SET status='unknown' WHERE attempt_id=?", (params.attempt_id,))
    before = dict(repo.get_task_run(run['task_run_id']))
    assert not settle_retired_input_run(access, repo, input_id, **callback)
    assert dict(repo.get_task_run(run['task_run_id'])) == before


def test_real_background_model_input_cancel_closes_exact_execution(background):
    import json

    from agent_py_agent.agent.backends import ModelResponse
    from agent_py_agent.agent.capability.config import CapabilityConfig
    from agent_py_agent.agent.common.json_io import read_json_object_report
    from agent_py_agent.agent.conversation.background_claim import run_claimed
    from agent_py_agent.agent.conversation.input_dependencies import input_dependency_path
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
        _background_claim_dependencies,
    )
    from agent_py_agent.agent.gateway_parts.private_interaction_service import (
        resolve_private_interaction,
    )
    from agent_py_agent.agent.user_space.task_credentials import credential_path
    from agent_py_agent.tests.test_persistent_task_input import _finish_claim
    from agent_py_agent.tests.test_task_credentials import _card
    from agent_py_agent.tests.test_task_progress_advisory import _CaptureBackend

    _, _, seed, _ = _card(background)
    seed = {key:value for key,value in seed.items() if key != 'fields'}
    assert resolve_private_interaction(background.agent, {**seed,'decision':'cancel'},
                                       channel='feishu', actor_id='ou_synthetic')['ok']
    background.store.messages.append({'thread_id':background.thread.thread_id,'role':'user',
        'content':'需要原接口的凭据，请发卡。','metadata':{'conversation_request_id':background.task_id}})
    _finish_claim(background)
    background.agent._capability_config_runtime_snapshot = SimpleNamespace(
        config=CapabilityConfig(im_task_credentials_enabled=True))

    class Backend(_CaptureBackend):
        def generate(self, prompt, on_chunk=None, **kwargs):
            self.prompts.append(prompt)
            assert len(self.prompts) <= 2
            if len(self.prompts) == 2:
                assert not kwargs.get('tools')
                return ModelResponse(text='请填写已发出的输入卡。', backend=self.name)
            return ModelResponse(text='', backend=self.name, tool_use_blocks=[{'id':'exact-background-input',
                'name':'request_credentials','input':{'url':'https://example.com/api','authentication':'bearer'}}])

    background.agent.backend = backend = Backend()
    runtime = BackgroundMainAgentRuntime(agent=background.agent, store=background.store)
    scheduler = BackgroundMainAgentScheduler({'runtime':runtime,'store':background.store})
    report = run_claimed(_background_claim_dependencies(scheduler), {
        'thread_id':background.thread.thread_id,'task_id':background.task_id,'reason':'task_input_resolved'})
    assert report.runtime_reason == 'TASK_INPUT_WAIT' and len(backend.prompts) == 2
    root = background.store.tasks.load(background.task_id)
    assert len(root.input_dependency_refs) == 1
    dep = read_json_object_report(input_dependency_path(background.agent,root.input_dependency_refs[0]),context='test').payload
    assert dep['scope']['execution_attempt_id']
    repo = background.agent.subagents.runtime_db
    attempt = repo.get_attempt(dep['scope']['execution_attempt_id'])
    run = repo.get_agent_run(attempt['agent_run_id'])
    assert run['current_attempt_id'] == dep['scope']['execution_attempt_id']
    assert attempt['status'] == 'done'
    card = json.loads(credential_path(background.agent.home_paths.owner_home_dir,dep['reference']).read_text())
    assert resolve_private_interaction(background.agent, {'reference':card['reference'],'decision':'cancel',
        'message_id':card['message_id'],'chat_id':card['chat_id']},channel='feishu',actor_id='ou_synthetic')['ok']
    assert repo.get_agent_run(run['agent_run_id'])['status'] == 'cancelled'
    assert repo.get_task_run(run['task_run_id'])['status'] == 'cancelled'
    assert background.store.tasks.load(background.task_id).status == 'active'
    assert background.store.wakes.pending() == []
    assert repo.list_pending_wakes() == []


@pytest.mark.parametrize('fault', ['none','duck','closed','unpublished','claim'])
def test_background_filled_attempt_still_requires_original_local_control_for_cache(background, fault):
    from agent_py_agent.agent.conversation.agent_tool_approval import (
        _main_approval_session_projection,
    )
    from agent_py_agent.agent.conversation.local_run_control import LocalRunControl
    from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic

    params = _bind_background_authority(background)
    scope = tool_approval_scope(background.agent, background.task_id)
    assert scope.execution_attempt_id == params.attempt_id
    assert _main_approval_session_projection(background.agent, scope) is not None
    if fault == 'closed':
        params.conversation_task_binding_callback.finish()
    elif fault == 'claim':
        path = background.store.storage.background_claim_path(background.thread.thread_id)
        update_json_file_atomic(path, lambda row:{**row,'runtime_authority':{
            **row['runtime_authority'],'attempt_id':'another-attempt'}})
    else:
        control = {'none':None, 'duck':SimpleNamespace(
            runtime_authority=params.conversation_task_binding_callback.runtime_authority,
            request_id=params.request_id), 'unpublished':LocalRunControl(params.request_id)}[fault]
        background.agent._current_run_params = replace(params, conversation_task_binding_callback=control)
    assert _main_approval_session_projection(background.agent, scope) is None


@pytest.mark.parametrize('fault', ['owner','generation','unknown','claim_done'])
def test_cold_published_scope_rejects_invalid_original_authority(background, fault):
    params = _bind_background_authority(background)
    del background.agent._current_run_params
    repo = background.agent.subagents.runtime_db
    run = repo.agent_run_for_run_id(params.run_id)
    if fault == 'claim_done':
        background.store.claims.finish({'thread_id':background.thread.thread_id,
            'claim_id':background.claim['claim_id'],'task_id':background.task_id,'status':'completed'})
    else:
        with repo.transaction() as conn:
            if fault == 'owner':
                conn.execute("UPDATE tasks SET owner_id='user/other' WHERE task_id=?", (background.task_id,))
            elif fault == 'generation':
                conn.execute('UPDATE agent_runs SET current_attempt_generation=current_attempt_generation+1 WHERE agent_run_id=?', (run['agent_run_id'],))
            else:
                conn.execute("UPDATE agent_attempts SET status='unknown' WHERE attempt_id=?", (params.attempt_id,))
    assert not tool_approval_scope(background.agent, background.task_id).active


def test_cold_approval_display_and_decision_keep_published_background_identity(background):
    from agent_py_agent.agent.contracts.tool_approval import ToolApprovalDecision
    from agent_py_agent.agent.conversation.agent_tool_approval import (
        list_pending_agent_tool_approvals,
        publish_agent_tool_approval,
        resolve_agent_tool_approval,
    )

    params = _bind_background_authority(background)
    handle = publish_agent_tool_approval(background.agent, run_id=background.task_id,
        thread_id=background.thread.thread_id, request_value=background.request)
    assert handle.scope.execution_attempt_id == params.attempt_id
    del background.agent._current_run_params
    pending = list_pending_agent_tool_approvals(background.agent, root_task_id=background.task_id)
    assert len(pending) == 1 and pending[0]['run_id'] == background.task_id
    result = resolve_agent_tool_approval(background.agent, run_id=background.task_id, request_value=background.request,
        decision_value=ToolApprovalDecision(background.request.permission_id, 'approved'))
    assert result['ok'] and result['decision'] == 'approved'


@pytest.mark.parametrize('fault', ['task','run','thread','attempt'])
def test_legacy_without_published_authority_rejects_explicit_current_conflict(background, fault):
    values = {'run_id':background.task_id,'task_id':background.task_id,'attempt_id':'',
              'task_attributes':{'conversation_thread_id':background.thread.thread_id}}
    if fault == 'thread':
        values['task_attributes'] = {'conversation_thread_id':'another-thread'}
    else:
        values[{'task':'task_id','run':'run_id','attempt':'attempt_id'}[fault]] = 'another-value'
    background.agent._current_run_params = SimpleNamespace(**values)
    if fault == 'task':
        from agent_py_agent.agent.conversation.tool_approval_scope import (
            ToolApprovalScopeUnavailable,
        )

        with pytest.raises(ToolApprovalScopeUnavailable):
            tool_approval_scope(background.agent, background.task_id)
    else:
        assert not tool_approval_scope(background.agent, background.task_id).active


@pytest.mark.parametrize('changed_child_attempt', [False, True])
def test_child_retirement_keeps_original_canonical_run_and_attempt(background, changed_child_attempt):
    import time

    from agent_py_agent.agent.conversation.input_dependencies import (
        arm_input_dependency,
        create_input_dependency,
        input_dependency_access,
    )
    from agent_py_agent.agent.conversation.task_run_closeout import settle_retired_input_run

    params = _bind_background_authority(background)
    child = background.agent.subagents.create_run(goal='原输入子片', parent_id=params.run_id,
        root_id=background.task_id, attributes={'conversation_task_id':background.task_id,
                                              'conversation_thread_id':background.thread.thread_id})
    child = background.agent.subagents.lifecycle.prepare_runner_attempt(child.id)
    scope = tool_approval_scope(background.agent, child.id)
    assert scope.agent_kind == 'subagent' and scope.execution_attempt_id == child.runner_active_attempt_id
    row = create_input_dependency(background.agent, scope, input_id='exact-child-input',
                                  provider='synthetic', expires_at=time.time()+180)
    arm_input_dependency(background.agent, row['input_id'])
    access = input_dependency_access(background.agent)
    access.mutate(row['input_id'], lambda value:{**value,'status':'cancelled','armed':False,'resolution_delivery':'retire'})
    repo = background.agent.subagents.runtime_db
    root_run = repo.agent_run_for_run_id(params.run_id)
    child_run = repo.agent_run_for_run_id(child.id)
    assert child_run['parent_agent_run_id'] == root_run['agent_run_id']
    for run in (root_run,child_run):
        assert repo.settle_agent_attempt(agent_run_id=run['agent_run_id'],attempt_id=run['current_attempt_id'],
            payload={'runtime_status':'needs_user_input','runtime_reason':'TASK_INPUT_WAIT',
                     'runtime_source':'input_dependency'})['settled']
    if changed_child_attempt:
        child.runner_active_attempt_id = 'another-attempt'
        background.agent.subagents.save(child)
    result = settle_retired_input_run(access,repo,row['input_id'])
    assert result is (not changed_child_attempt)
    assert repo.get_agent_run(child_run['agent_run_id'])['status'] == ('created' if changed_child_attempt else 'cancelled')
    assert repo.get_agent_run(root_run['agent_run_id'])['status'] == 'created'
    assert repo.get_task_run(root_run['task_run_id'])['closed_at'] == 0
