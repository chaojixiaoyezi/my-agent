"""群共享择取必须进入真实HTTP工具及通知事实，只有末端网络和群官方响应用合成替身。"""
from contextlib import contextmanager

import pytest

from agent_py_agent.agent.common.json_io import read_json_object_report
from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
from agent_py_agent.agent.tooling.web_fetch_runtime import FetchRawRequest, RawResponseParts
from agent_py_agent.agent.user_space.task_credential_http import credential_authenticated_request
from agent_py_agent.agent.user_space.task_credentials import (
    CredentialCardOptions,
    credential_path,
    credential_rejection_current,
    manage_credential,
    request_credential_card,
)
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call
from agent_py_agent.tests.test_group_shared_credentials import _group_case, _issue, _switch_actor


# LLM: 只替代末端HTTP，群身份/私密存储/实际认证器仍走产品链；不得把合成secret写入工具参数。
# 函数用途: 对原凭据入口记录本次真正注入的头与调用次数，验证无隐式换账号重试。
def _fetch(ctx, reference, *, status=200):
    calls = []

    def send(request):
        calls.append(request)
        if status != 200:
            return ToolHandlerOutcome('web_fetch', False, 'synthetic rejected', error_code='HTTP_ERROR',
                result_envelope={'http_status': status})
        return RawResponseParts(200, {'Content-Type': 'text/plain'}, b'safe', request.url)

    result, secrets = credential_authenticated_request(ctx.agent, reference,
        FetchRawRequest('web_fetch', 'https://example.com/api', 'GET', {}, None, 5), send=send)
    return result, secrets, calls


def test_original_http_selects_own_once_even_when_model_passes_other_reference(tmp_path):
    ctx = _group_case(tmp_path)
    a = _issue(ctx, submitter='ou_actor_a')
    b = _issue(ctx, submitter='ou_actor_b')
    before = len(ctx.calls)
    result, secrets, calls = _fetch(ctx, a)
    assert len(ctx.calls) - before == 1 and len(calls) == 1
    assert calls[0].headers['Authorization'] == 'Bearer SyntheticGroupSecret-ou_actor_b'
    assert result.credential_selection['requested_credential_ref'] == a
    assert result.credential_selection['effective_credential_ref'] == b
    assert 'SyntheticGroupSecret' not in str(result.credential_selection)
    assert 'SyntheticGroupSecret-ou_actor_a' not in secrets


def test_actual_rejection_notifies_effective_key_and_explicit_revoke_restores_shared(tmp_path):
    ctx = _group_case(tmp_path)
    a = _issue(ctx, submitter='ou_actor_a')
    b = _issue(ctx, submitter='ou_actor_b')
    result, _secrets, calls = _fetch(ctx, a, status=401)
    assert len(calls) == 1
    auth = result.result_envelope['credential_authentication']
    assert auth['credential_ref'] == b and auth['user_action_required'] is True
    assert credential_rejection_current(ctx.agent, b, auth['generation'])
    assert read_json_object_report(credential_path(ctx.agent.home_paths.owner_home_dir, a), context='test').payload['verification_status'] == 'unverified'
    with pytest.raises(ValueError):
        _fetch(ctx, a)
    assert manage_credential(ctx.agent, b, operation='revoke')['status'] == 'revoked'
    result, _secrets, calls = _fetch(ctx, b)
    assert len(calls) == 1 and result.credential_selection['effective_credential_ref'] == a
    assert calls[0].headers['Authorization'] == 'Bearer SyntheticGroupSecret-ou_actor_a'


def test_permission_denial_keeps_own_key_without_retry_or_rejection(tmp_path):
    ctx = _group_case(tmp_path)
    a = _issue(ctx, submitter='ou_actor_a')
    b = _issue(ctx, submitter='ou_actor_b')
    result, _secrets, calls = _fetch(ctx, a, status=403)
    assert len(calls) == 1 and result.result_envelope['http_status'] == 403
    assert result.result_envelope['credential_selection']['effective_credential_ref'] == b
    row = read_json_object_report(credential_path(ctx.agent.home_paths.owner_home_dir, b), context='test').payload
    assert row['verification_status'] == 'unverified'
    assert not credential_rejection_current(ctx.agent, b, row['generation'])


def test_real_tool_receipt_and_secret_echo_use_effective_key(tmp_path, monkeypatch):
    ctx = _group_case(tmp_path)
    a = _issue(ctx, submitter='ou_actor_a')
    b = _issue(ctx, submitter='ou_actor_b')
    seen = []

    def network(request, **_kwargs):
        seen.append(request)
        return RawResponseParts(200, {'Content-Type': 'text/plain'},
            b'echo SyntheticGroupSecret-ou_actor_b', request.url)

    monkeypatch.setattr('agent_py_agent.agent.tooling.web_fetch_tools.fetch_raw_response', network)
    current = ctx.agent._current_run_params
    result = execute_registry_test_call(ctx.agent.tools, 'web_fetch',
        {'url': 'https://example.com/api', 'credential_ref': a}, run_id=current.run_id,
        register_with=ctx.agent, allowed_tools=['web_fetch'])
    assert result.ok and result.handler_executed and len(seen) == 1
    assert result.metadata['handler_details']['credential_selection']['effective_credential_ref'] == b
    assert 'SyntheticGroupSecret' not in result.output
    assert 'SyntheticGroupSecret' not in str(result.metadata)


def test_group_management_checks_members_outside_credential_locks(tmp_path, monkeypatch):
    from agent_py_agent.agent.user_space import task_credentials
    from agent_py_agent.agent.user_space.task_credential_forms import configure_credential

    ctx = _group_case(tmp_path)
    ref, _ = request_credential_card(ctx.agent, url='https://example.com/api', authentication='collected',
        options=CredentialCardOptions(collection_mode='password'))
    row = read_json_object_report(credential_path(ctx.agent.home_paths.owner_home_dir, ref), context='test').payload
    from agent_py_agent.agent.gateway_parts.private_interaction_service import (
        resolve_private_interaction,
    )
    body = {key: row[key] for key in ('reference', 'message_id', 'chat_id')}
    body.update(decision='submit', fields={'secret': 'SyntheticManagedSecret'},
        submitter={'actor_id': 'ou_actor_b', 'chat_id': 'oc_synthetic', 'current_group_member': True})
    assert resolve_private_interaction(ctx.agent, body, channel='feishu', actor_id='ou_actor_b')['ok']
    _switch_actor(ctx, 'ou_actor_b')
    held = []
    original = task_credentials._credential_mutation

    @contextmanager
    def mutation(*args, **kwargs):
        with original(*args, **kwargs) as admission:
            held.append(True)
            try:
                yield admission
            finally:
                held.pop()

    adapter = ctx.agent.delivery_service.registry.adapter_for('feishu')
    snapshot = adapter.group_authority_snapshot

    def members(chat):
        assert not held
        return snapshot(chat)

    monkeypatch.setattr(task_credentials, '_credential_mutation', mutation)
    monkeypatch.setattr(adapter, 'group_authority_snapshot', members)
    assert configure_credential(ctx.agent, ref, authentication='bearer')['authentication'] == 'bearer'
    assert manage_credential(ctx.agent, ref, operation='status')['status'] == 'ready'
    request_credential_card(ctx.agent, url='https://example.com/api', authentication='bearer',
        options=CredentialCardOptions(refresh_ref=ref))
    assert manage_credential(ctx.agent, ref, operation='revoke')['status'] == 'revoked'


def test_disabling_group_sharing_rejects_old_card_submission_but_cancel_works(tmp_path):
    from agent_py_agent.agent.gateway_parts.private_interaction_service import (
        resolve_private_interaction,
    )
    ctx = _group_case(tmp_path)
    ref, _ = request_credential_card(ctx.agent, url='https://example.com/api', authentication='bearer')
    row = read_json_object_report(credential_path(ctx.agent.home_paths.owner_home_dir, ref), context='test').payload
    body = {key: row[key] for key in ('reference', 'message_id', 'chat_id')}
    body.update(decision='submit', fields={'secret': 'SyntheticDisabledSecret'},
        submitter={'actor_id': 'ou_actor_b', 'chat_id': 'oc_synthetic', 'current_group_member': True})
    ctx.agent._capability_config_runtime_snapshot.config.im_group_shared_credentials_enabled = False
    with pytest.raises(ValueError, match='共享凭据已关闭'):
        resolve_private_interaction(ctx.agent, body, channel='feishu', actor_id='ou_actor_b')
    saved = read_json_object_report(credential_path(ctx.agent.home_paths.owner_home_dir, ref), context='test').payload
    assert saved['status'] == 'pending' and not saved.get('ciphertext')
    body.update(decision='cancel', fields={})
    assert resolve_private_interaction(ctx.agent, body, channel='feishu', actor_id='ou_actor_b')['ok']


@pytest.mark.parametrize('child', [False, True])
def test_monitor_and_its_child_reuse_group_key_from_different_original_task(tmp_path, child):
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.conversation.host_actor import background_host_actor
    from agent_py_agent.agent.scheduler.service import SchedulerService
    from agent_py_agent.tests.test_im_credentials_security_matrix import _child
    from agent_py_agent.tests.test_ingestion_task_credential_scope import _scheduled_execution

    ctx = _group_case(tmp_path, actor='ou_actor_a')
    shared = _issue(ctx)
    _switch_actor(ctx, 'ou_actor_c')
    job = _scheduled_execution(ctx.agent)
    run = ctx.agent.scheduler_repository.reserve_manual_run(job)
    service = SchedulerService(ctx.agent.scheduler_repository, conversation_store=ctx.agent.conversation_store)
    service.enqueue_ready_runs(now=run['dispatch_after'] + 1)
    signal = next(row for row in ctx.agent.conversation_store.wakes.pending() if row.root_task_id == run['run_id'])
    assert service.claim_wake(signal, now=run['dispatch_after'] + 2).status == 'claimed'
    ctx.agent.conversation_store.claims.acquire({'thread_id': signal.thread_id, 'task_id': run['run_id'],
        'reason': 'scheduled_job_due'})
    ctx.agent._current_run_params = RunParams(run_id=run['run_id'], task_id=run['run_id'],
        task_attributes={'conversation_thread_id': signal.thread_id},
        host_channel_actor=background_host_actor(ctx.agent, signal.thread_id, run['run_id']))
    if child:
        _child(ctx.agent)
    result, _secrets, calls = _fetch(ctx, shared)
    assert len(calls) == 1 and result.credential_selection['effective_credential_ref'] == shared
    assert calls[0].headers['Authorization'] == 'Bearer SyntheticGroupSecret-ou_actor_a'
