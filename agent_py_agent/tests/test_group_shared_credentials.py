"""群凭据共享只使用临时原Store、真实卡回调和合成provider事实，不访问真实系统。"""
from __future__ import annotations

import json
import uuid
from copy import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.json_io import read_json_object_report
from agent_py_agent.agent.conversation.tool_approval_scope import tool_approval_scope
from agent_py_agent.agent.delivery import ChannelCapabilities
from agent_py_agent.agent.gateway_parts.private_interaction_service import (
    resolve_private_interaction,
)
from agent_py_agent.agent.user_space.task_credentials import (
    CredentialCardOptions,
    _credential_mutation,
    _save_credential_row,
    credential_path,
    request_credential_card,
)
from agent_py_agent.tests.test_ingestion_task_credential_scope import _active, _agent


# LLM: 本夹具使用实际owner/TaskLink/claim/HostActor，只替换卡运输和完整官方群成员响应；可供发现工具线复用。
# 函数用途: 建立A/B/C同群的隔离执行者，记录provider调用，不读任何本机配置或秘密。
def _group_case(tmp_path, *, actor='ou_actor_b'):
    agent = _agent(tmp_path, 'group')
    agent.config.feishu_session_lock_enabled = False
    from agent_py_agent.agent.capability.runtime_config_reload import capability_config_for_agent
    config = copy(capability_config_for_agent(agent))
    config.im_group_shared_credentials_enabled = True
    agent._capability_config_runtime_snapshot = SimpleNamespace(config=config)
    raw = {'provider': 'feishu', 'chat_id': 'oc_synthetic', 'app_id': 'app_synthetic',
        'tenant_key': 'tenant_synthetic', 'owner_id': 'ou_actor_a',
        'member_ids': ['ou_actor_a', 'ou_actor_b', 'ou_actor_c'], 'members_complete': True, 'bot_is_member': True}
    calls, sent = [], []

    def snapshot(chat):
        calls.append(chat)
        return dict(raw)

    def send(target, card, **kwargs):
        sent.append((target, card))
        return {'message_id': 'om_' + uuid.uuid4().hex, 'chat_id': 'oc_synthetic'}

    agent.channel_registry.register_adapter('feishu', SimpleNamespace(
        send_control_card=send, group_authority_snapshot=snapshot),
        capabilities=ChannelCapabilities(control_cards=True, proactive=True))
    ctx = SimpleNamespace(agent=agent, raw=raw, calls=calls, sent=sent)
    _switch_actor(ctx, actor)
    return ctx


# LLM: 换人只通过新TaskLink与typed入口，不把显示名或模型attrs升格为身份；旧claim按原接口结束。
# 函数用途: 在同一真实临时群建立下一次普通请求，返回准确活动scope。
def _switch_actor(ctx, actor, *, conversation_id=''):
    agent = ctx.agent
    old = getattr(agent, '_current_run_params', None)
    if old and old.task_attributes.get('conversation_thread_id'):
        tid = old.task_attributes['conversation_thread_id']
        claim = agent.conversation_store.claims.load(tid)
        if claim:
            agent.conversation_store.claims.finish({'thread_id': tid, 'claim_id': claim['claim_id'], 'status': 'done'})
    if conversation_id:
        from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
        from agent_py_agent.agent.conversation.host_actor import actor_from_scope

        root = 'gwreq-' + uuid.uuid4().hex
        thread = agent.conversation_store.threads.get_or_create({'channel': 'feishu',
            'channel_conversation_id': conversation_id, 'channel_user_id': actor, 'canonical_user_id': actor,
            'owner_id': agent.home_paths.owner_id, 'owner_home': str(agent.home_paths.owner_home_dir)})
        host = actor_from_scope(agent, SimpleNamespace(channel='feishu', channel_user_id=actor,
            channel_conversation_id=conversation_id), thread.thread_id, root)
        agent.conversation_store.tasks.bind({'thread_id': thread.thread_id, 'task_id': root,
            'goal': '合成话题请求', 'status': 'active'}, host_channel_actor=host)
        agent.conversation_store.claims.acquire({'thread_id': thread.thread_id, 'task_id': root,
            'reason': 'thread_goal_continue'})
        agent._current_run_params = RunParams(request_id=root, task_id=root, run_id=root,
            task_attributes={'conversation_thread_id': thread.thread_id}, host_channel_actor=host)
    else:
        _active(agent, actor_id=actor)
    return _scope(agent)


# LLM: 当前scope仍由产品ToolApprovalScope读取原claim/child，不在测试里合成active资格。
# 函数用途: 取得当前临时任务的准确审批作用域。
def _scope(agent):
    return tool_approval_scope(agent, agent._current_run_params.run_id)


# LLM: 只在合成测试中改原受保护row，沿原quota+锁保存；不引入第二记录，也不用于生产迁移。
# 函数用途: 构造旧schema、损坏和终态矩阵，供原读取/使用门验证。
def _change(ctx, credential_ref, **changes):
    path = credential_path(ctx.agent.home_paths.owner_home_dir, credential_ref)
    with _credential_mutation(ctx.agent, path) as admission:
        row = read_json_object_report(path, context='synthetic.group').payload
        row.update(changes)
        _save_credential_row(admission, path, row)
    return row


# LLM: 原request/签名/提交/加密全走产品入口；access_scope仅为本测试明确建立的宿主共享事实，不伪造表单或成员授权。
# 函数用途: 保存指定实际填写人的合成凭据，返回opaque引用；秘密不进入工具参数或公开摘要。
def _issue(ctx, *, submitter='ou_actor_a', url='https://example.com/api', name='合成接口', access_scope='group', **changes):
    reference, _ = request_credential_card(ctx.agent, url=url, authentication='bearer',
        options=CredentialCardOptions(access_scope=access_scope))
    path = credential_path(ctx.agent.home_paths.owner_home_dir, reference)
    row = read_json_object_report(path, context='synthetic.group').payload
    body = {key: row[key] for key in ('reference', 'message_id', 'chat_id')}
    body.update(decision='submit', fields={'secret': 'SyntheticGroupSecret-' + submitter,
        'credential_name': name, 'credential_note': 'SyntheticPrivateNote'},
        submitter={'actor_id': submitter, 'chat_id': 'oc_synthetic', 'current_group_member': True})
    assert resolve_private_interaction(ctx.agent, body, channel='feishu', actor_id=submitter)['ok']
    _change(ctx, reference, access_scope=access_scope, **changes)
    return reference


# LLM: 延迟导入只为保留新增API未落地时的原红测，运行期仍调用真实模块而非替身。
# 函数用途: 取得本线共享凭据入口。
def _api():
    from agent_py_agent.agent.user_space import task_credential_groups
    return task_credential_groups


def test_new_group_scope_needs_typed_actor_and_live_complete_members(tmp_path):
    ctx = _group_case(tmp_path)
    assert _api().new_credential_access_scope(ctx.agent, _scope(ctx.agent)) == 'group'
    ctx.raw['members_complete'] = False
    with pytest.raises(ValueError):
        _api().new_credential_access_scope(ctx.agent, _scope(ctx.agent))


def test_actual_submitter_beats_requester_and_shared_ref_use_is_not_single_recommendation(tmp_path):
    ctx = _group_case(tmp_path)
    a1 = _issue(ctx, submitter='ou_actor_a')
    a2 = _issue(ctx, submitter='ou_actor_a')
    found = _api().discover_group_credentials(ctx.agent, url='https://example.com/next', operation='find')
    assert found['own_credential_present'] is False
    assert found['selected_credential_ref'] in {a1, a2}
    for ref in (a1, a2):
        row = read_json_object_report(credential_path(ctx.agent.home_paths.owner_home_dir, ref), context='test').payload
        assert _api().group_credential_access(ctx.agent, row, _scope(ctx.agent))
    b = _issue(ctx, submitter='ou_actor_b')
    found = _api().discover_group_credentials(ctx.agent, url='https://example.com/next', operation='find')
    assert found['selected_credential_ref'] == b and found['own_credential_present'] is True
    with pytest.raises(ValueError):
        _api().group_credential_access(ctx.agent,
            read_json_object_report(credential_path(ctx.agent.home_paths.owner_home_dir, a1), context='test').payload,
            _scope(ctx.agent))


@pytest.mark.parametrize('changes', [{'verification_status': 'rejected'}, {'ready_expires_at': 1},
    {'authentication': 'collected'}, {'status': 'expired'}])
def test_unavailable_own_never_silently_selects_other_even_with_name_filter(tmp_path, changes):
    ctx = _group_case(tmp_path)
    _issue(ctx, name='别人的接口')
    _issue(ctx, submitter='ou_actor_b', name='自己的接口', **changes)
    found = _api().discover_group_credentials(ctx.agent, url='https://example.com/api', name='别人的接口', operation='find')
    assert found['own_credential_present'] and not found['selected_credential_ref']
    assert found['selection_reason'] == 'own_credential_unavailable'


@pytest.mark.parametrize('status', ['revoked', 'cancelled', 'failed'])
def test_explicit_retirement_releases_own_priority(tmp_path, status):
    ctx = _group_case(tmp_path)
    a = _issue(ctx)
    _issue(ctx, submitter='ou_actor_b', status=status)
    found = _api().discover_group_credentials(ctx.agent, url='https://example.com/api', operation='find')
    assert not found['own_credential_present'] and found['selected_credential_ref'] == a


def test_same_group_new_task_and_child_keep_original_actor_and_no_new_grant(tmp_path):
    from agent_py_agent.tests.test_im_credentials_security_matrix import _child

    ctx = _group_case(tmp_path)
    ref = _issue(ctx)
    _switch_actor(ctx, 'ou_actor_c')
    assert _api().discover_group_credentials(ctx.agent, url='https://example.com', operation='find')['selected_credential_ref'] == ref
    _child(ctx.agent)
    assert _api().discover_group_credentials(ctx.agent, url='https://example.com', operation='find')['selected_credential_ref'] == ref


@pytest.mark.parametrize('operation', ['configure', 'update', 'revoke', 'share'])
def test_management_only_requester_or_actual_submitter_not_other_member(tmp_path, operation):
    ctx = _group_case(tmp_path)
    ref = _issue(ctx)
    row = read_json_object_report(credential_path(ctx.agent.home_paths.owner_home_dir, ref), context='test').payload
    assert _api().group_credential_access(ctx.agent, row, _scope(ctx.agent), operation=operation)
    _switch_actor(ctx, 'ou_actor_a')
    assert _api().group_credential_access(ctx.agent, row, _scope(ctx.agent), operation=operation)
    _switch_actor(ctx, 'ou_actor_c')
    with pytest.raises(ValueError):
        _api().group_credential_access(ctx.agent, row, _scope(ctx.agent), operation=operation)
    assert _api().group_credential_access(ctx.agent, row, _scope(ctx.agent), operation='status')


@pytest.mark.parametrize('fault', ['departed', 'incomplete', 'wrong_group', 'no_bot', 'spoof_actor', 'missing_actor'])
def test_membership_and_host_identity_fail_closed_without_pending_or_send(tmp_path, fault):
    ctx = _group_case(tmp_path)
    _issue(ctx)
    if fault == 'departed':
        ctx.raw['member_ids'].remove('ou_actor_b')
    elif fault == 'incomplete':
        ctx.raw['members_complete'] = False
    elif fault == 'wrong_group':
        ctx.raw['chat_id'] = 'oc_other'
    elif fault == 'no_bot':
        ctx.raw['bot_is_member'] = False
    elif fault == 'spoof_actor':
        ctx.agent._current_run_params.host_channel_actor = replace(ctx.agent._current_run_params.host_channel_actor, actor_id='ou_actor_a')
    else:
        ctx.agent._current_run_params.host_channel_actor = None
        path = ctx.agent.conversation_store.tasks.storage.task_path(ctx.agent._current_run_params.task_id)
        original = json.loads(path.read_text())
        original.pop('host_channel_actor', None)
        path.write_text(json.dumps(original))
    before = len(ctx.sent)
    with pytest.raises(ValueError):
        _api().discover_group_credentials(ctx.agent, url='https://example.com', operation='find')
    assert len(ctx.sent) == before


def test_safe_summaries_never_copy_secrets_urls_notes_or_arbitrary_metadata(tmp_path):
    ctx = _group_case(tmp_path)
    ref = _issue(ctx, url='https://example.com/api?query=SyntheticQueryCanary',
        metadata={'password': 'SyntheticNestedCanary'}, username='SyntheticUsernameCanary')
    result = _api().discover_group_credentials(ctx.agent)
    public = json.dumps(result, ensure_ascii=False)
    assert result['selected_credential_ref'] == ''
    assert any(row['credential_ref'] == ref for row in result['credentials'])
    for value in ('SyntheticGroupSecret', 'SyntheticPrivateNote', 'SyntheticQueryCanary', 'SyntheticNestedCanary', 'SyntheticUsernameCanary', 'ciphertext', 'header_name'):
        assert value not in public


def test_corrupt_or_unknown_scope_row_never_looks_like_no_own_fallback(tmp_path):
    ctx = _group_case(tmp_path)
    _issue(ctx)
    ref = _issue(ctx, submitter='ou_actor_b')
    _change(ctx, ref, access_scope='unknown')
    with pytest.raises(ValueError):
        _api().discover_group_credentials(ctx.agent, url='https://example.com', operation='find')
    path = credential_path(ctx.agent.home_paths.owner_home_dir, ref)
    path.write_text('{')
    with pytest.raises(ValueError):
        _api().discover_group_credentials(ctx.agent, url='https://example.com', operation='find')


def test_old_task_scope_is_not_implicitly_shared_and_unknown_is_rejected(tmp_path):
    ctx = _group_case(tmp_path)
    ref = _issue(ctx)
    row = _change(ctx, ref, access_scope='task')
    assert not _api().group_credential_access(ctx.agent, row, _scope(ctx.agent))
    row.pop('access_scope')
    assert not _api().group_credential_access(ctx.agent, row, _scope(ctx.agent))
    with pytest.raises(ValueError):
        _api().group_credential_access(ctx.agent, {**row, 'access_scope': 'future_unknown'}, _scope(ctx.agent))


def test_resolver_uses_own_before_requested_ref_and_after_revoke_uses_shared_without_rewriting_handle(tmp_path):
    ctx = _group_case(tmp_path)
    a = _issue(ctx)
    b = _issue(ctx, submitter='ou_actor_b')
    arow = read_json_object_report(credential_path(ctx.agent.home_paths.owner_home_dir, a), context='test').payload
    assert _api().resolve_group_credential(ctx.agent, arow, _scope(ctx.agent), origin='https://example.com')['credential_ref'] == b
    _change(ctx, b, verification_status='rejected')
    with pytest.raises(ValueError):
        _api().resolve_group_credential(ctx.agent, arow, _scope(ctx.agent), origin='https://example.com')
    brow = _change(ctx, b, status='revoked')
    before = credential_path(ctx.agent.home_paths.owner_home_dir, b).read_bytes()
    assert _api().resolve_group_credential(ctx.agent, brow, _scope(ctx.agent), origin='https://example.com')['credential_ref'] == a
    assert credential_path(ctx.agent.home_paths.owner_home_dir, b).read_bytes() == before
    with pytest.raises(ValueError):
        _api().resolve_group_credential(ctx.agent, brow, _scope(ctx.agent), origin='https://other.example.com')


@pytest.mark.parametrize('changes', [{}, {'status': 'revoked'}, {'verification_status': 'rejected'}])
def test_submitted_replacement_retires_old_own_without_reactivating_old_secret(tmp_path, changes):
    ctx = _group_case(tmp_path)
    a = _issue(ctx)
    old = _issue(ctx, submitter='ou_actor_b')
    replacement = _issue(ctx, submitter='ou_actor_b', replaces_ref=old, replaces_generation=1, **changes)
    found = _api().discover_group_credentials(ctx.agent, url='https://example.com', operation='find')
    if changes.get('status') == 'revoked':
        assert found['selected_credential_ref'] == a and not found['own_credential_present']
    elif changes.get('verification_status') == 'rejected':
        assert not found['selected_credential_ref'] and found['own_credential_present']
    else:
        assert found['selected_credential_ref'] == replacement
    old_summary = next(row for row in found['credentials'] if row['credential_ref'] == old)
    assert old_summary['superseded'] and not old_summary['usable']


@pytest.mark.parametrize('changes', [{'status': 'pending'}, {'status': 'cancelled'},
    {'submitted_at': 0}, {'replaces_generation': 2}, {'actor_id': 'ou_actor_c'},
    {'origin': 'https://other.example.com:443'}, {'ready_expires_at': None}])
def test_unproven_replacement_cannot_retire_own_and_trigger_shared_fallback(tmp_path, changes):
    ctx = _group_case(tmp_path)
    _issue(ctx)
    old = _issue(ctx, submitter='ou_actor_b', verification_status='rejected')
    new = _issue(ctx, submitter='ou_actor_b')
    _change(ctx, new, **{'status': 'revoked', 'replaces_ref': old, 'replaces_generation': 1, **changes})
    found = _api().discover_group_credentials(ctx.agent, url='https://example.com', operation='find')
    assert found['own_credential_present'] and not found['selected_credential_ref']


def test_old_shared_handle_prefers_current_same_submitter_and_all_member_refs_remain_usable(tmp_path):
    ctx = _group_case(tmp_path, actor='ou_actor_c')
    old = _issue(ctx)
    other = _issue(ctx, submitter='ou_actor_b')
    fresh = _issue(ctx, replaces_ref=old, replaces_generation=1)
    old_row = read_json_object_report(credential_path(ctx.agent.home_paths.owner_home_dir, old), context='test').payload
    assert _api().resolve_group_credential(ctx.agent, old_row, _scope(ctx.agent), origin='https://example.com')['credential_ref'] == fresh
    other_row = read_json_object_report(credential_path(ctx.agent.home_paths.owner_home_dir, other), context='test').payload
    assert _api().resolve_group_credential(ctx.agent, other_row, _scope(ctx.agent), origin='https://example.com')['credential_ref'] == other


def test_missing_ephemeral_actor_uses_original_task_link_proof(tmp_path):
    ctx = _group_case(tmp_path)
    ref = _issue(ctx)
    ctx.agent._current_run_params.host_channel_actor = None
    assert _api().discover_group_credentials(ctx.agent, url='https://example.com', operation='find')['selected_credential_ref'] == ref


@pytest.mark.parametrize('changes', [{'owner': {'provider': 'feishu', 'owner_kind': 'group', 'owner_id': 'oc_other'}},
    {'channel': 'other'}, {'presentation': {'kind': 'group', 'chat_id': 'oc_other'}},
    {'presentation': {'kind': 'private'}}, {'submitter_actor_id': {'actor': 'ou_actor_b'}}])
def test_cross_scope_or_malformed_rows_are_not_usable_group_handles(tmp_path, changes):
    ctx = _group_case(tmp_path)
    ref = _issue(ctx)
    row = _change(ctx, ref, **changes)
    with pytest.raises(ValueError):
        _api().resolve_group_credential(ctx.agent, row, _scope(ctx.agent), origin='https://example.com')


def test_disabled_new_scope_is_task_but_explicit_shared_use_fails_closed(tmp_path):
    ctx = _group_case(tmp_path)
    ref = _issue(ctx)
    ctx.agent._capability_config_runtime_snapshot.config.im_group_shared_credentials_enabled = False
    calls = len(ctx.calls)
    assert _api().new_credential_access_scope(ctx.agent, _scope(ctx.agent)) == 'task'
    row = read_json_object_report(credential_path(ctx.agent.home_paths.owner_home_dir, ref), context='test').payload
    with pytest.raises(ValueError):
        _api().group_credential_access(ctx.agent, row, _scope(ctx.agent))
    assert len(ctx.calls) == calls
    assert _api().group_credential_access(ctx.agent, row, _scope(ctx.agent), operation='status')
    assert _api().group_credential_access(ctx.agent, row, _scope(ctx.agent), operation='revoke')
    assert not _api().discover_group_credentials(ctx.agent, url='https://example.com')['selected_credential_ref']


def test_private_owner_never_borrows_group_share_or_creates_group_scope(tmp_path):
    agent = _agent(tmp_path, 'user')
    _active(agent)
    assert _api().new_credential_access_scope(agent, _scope(agent)) == 'task'
    with pytest.raises(ValueError):
        _api().discover_group_credentials(agent, url='https://example.com', operation='find')


def test_old_child_attempt_cannot_claim_latest_group_authority(tmp_path):
    from agent_py_agent.tests.test_im_credentials_security_matrix import _child

    ctx = _group_case(tmp_path)
    _issue(ctx)
    _child(ctx.agent)
    ctx.agent._current_run_params.attempt_id = 'old-attempt'
    with pytest.raises(ValueError):
        _api().discover_group_credentials(ctx.agent, url='https://example.com', operation='find')


def test_shareable_old_task_refs_visible_only_to_requester_or_submitter_and_valid_original_mac(tmp_path):
    ctx = _group_case(tmp_path)
    ref = _issue(ctx, access_scope='task')
    result = _api().discover_group_credentials(ctx.agent)
    assert [row['credential_ref'] for row in result['shareable_credentials']] == [ref]
    assert not result['credentials'] and not result['selected_credential_ref']
    _switch_actor(ctx, 'ou_actor_c')
    assert not _api().discover_group_credentials(ctx.agent)['shareable_credentials']
    _switch_actor(ctx, 'ou_actor_a')
    assert _api().discover_group_credentials(ctx.agent)['shareable_credentials'][0]['credential_ref'] == ref
    _change(ctx, ref, reference='bad-original-mac')
    assert not _api().discover_group_credentials(ctx.agent)['shareable_credentials']


def test_own_401_revoke_then_original_handle_uses_shared_once_and_audits_effective_ref(tmp_path):
    from agent_py_agent.agent.tooling.web_fetch_runtime import FetchRawRequest, RawResponseParts
    from agent_py_agent.agent.user_space.task_credential_http import (
        credential_authenticated_request,
    )
    from agent_py_agent.agent.user_space.task_credentials import manage_credential

    ctx = _group_case(tmp_path)
    a = _issue(ctx)
    b = _issue(ctx, submitter='ou_actor_b')
    sends = []
    code = [401]

    def send(request):
        sends.append(request)
        return RawResponseParts(code[0], {}, b'ok', request.url)

    request = FetchRawRequest(tool='web_fetch', url='https://example.com/api', method='POST', headers={}, data=b'', timeout=5)
    original_root = ctx.agent._current_run_params.task_id
    result, _ = credential_authenticated_request(ctx.agent, a, request, send=send)
    assert len(sends) == 1 and sends[0].headers['Authorization'].endswith('ou_actor_b')
    assert result.result_envelope['credential_authentication']['credential_ref'] == b
    assert result.result_envelope['credential_selection']['effective_credential_ref'] == b
    with pytest.raises(ValueError):
        credential_authenticated_request(ctx.agent, a, request, send=send)
    assert len(sends) == 1
    assert manage_credential(ctx.agent, b, operation='revoke')['status'] == 'revoked'
    code[0] = 200
    result, _ = credential_authenticated_request(ctx.agent, b, request, send=send)
    assert len(sends) == 2 and sends[-1].headers['Authorization'].endswith('ou_actor_a')
    assert result.credential_selection == {'schema': 'credential-selection.v1',
        'requested_credential_ref': b, 'effective_credential_ref': a, 'access_scope': 'group'}
    assert ctx.agent._current_run_params.task_id == original_root and len(ctx.sent) == 2
    assert not ctx.agent.scheduler_repository.list_jobs()[0]


@pytest.mark.parametrize('code', [403, 'timeout'])
def test_own_permission_or_transport_failure_does_not_retry_other_member(tmp_path, code):
    from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
    from agent_py_agent.agent.tooling.web_fetch_runtime import FetchRawRequest, RawResponseParts
    from agent_py_agent.agent.user_space.task_credential_http import (
        credential_authenticated_request,
    )

    ctx = _group_case(tmp_path)
    a = _issue(ctx)
    b = _issue(ctx, submitter='ou_actor_b')
    sends = []

    def send(request):
        sends.append(request)
        return (ToolHandlerOutcome('web_fetch', False, '合成超时', error_code='TOOL_TIMEOUT') if code == 'timeout'
            else RawResponseParts(code, {}, b'no', request.url))

    credential_authenticated_request(ctx.agent, a, FetchRawRequest(tool='web_fetch', url='https://example.com/api',
        method='GET', headers={}, data=None, timeout=5), send=send)
    assert len(sends) == 1 and sends[0].headers['Authorization'].endswith('ou_actor_b')
    assert _api().discover_group_credentials(ctx.agent, url='https://example.com', operation='find')['selected_credential_ref'] == b


def test_group_topics_share_keys_but_do_not_rewrite_original_actor_or_topic(tmp_path):
    ctx = _group_case(tmp_path)
    ref = _issue(ctx)
    _switch_actor(ctx, 'ou_actor_c', conversation_id='oc_synthetic:thread:om_topic')
    actor = ctx.agent._current_run_params.host_channel_actor
    assert _api().discover_group_credentials(ctx.agent, url='https://example.com', operation='find')['selected_credential_ref'] == ref
    assert ctx.agent._current_run_params.host_channel_actor == actor
    assert actor.conversation_id == 'oc_synthetic:thread:om_topic'
    ctx.agent._current_run_params.host_channel_actor = replace(actor, conversation_id='oc_synthetic:thread:om_other')
    with pytest.raises(ValueError):
        _api().discover_group_credentials(ctx.agent, url='https://example.com', operation='find')


@pytest.mark.parametrize('conversation', ['oc_other', 'oc_synthetic_extra:thread:om_topic'])
def test_other_group_or_prefix_cannot_borrow_same_owner_directory(tmp_path, conversation):
    ctx = _group_case(tmp_path)
    _issue(ctx)
    _switch_actor(ctx, 'ou_actor_c', conversation_id=conversation)
    calls = len(ctx.calls)
    with pytest.raises(ValueError):
        _api().discover_group_credentials(ctx.agent, url='https://example.com', operation='find')
    assert len(ctx.calls) == calls


@pytest.mark.parametrize('fault', ['symlink', 'invalid_utf8'])
def test_corrupt_own_file_never_falls_back_or_logs_values(tmp_path, caplog, fault):
    ctx = _group_case(tmp_path)
    _issue(ctx)
    ref = _issue(ctx, submitter='ou_actor_b')
    path = credential_path(ctx.agent.home_paths.owner_home_dir, ref)
    if fault == 'symlink':
        target = tmp_path / 'untrusted.json'
        target.write_text('{"secret":"SyntheticNoFollowCanary"}')
        path.unlink()
        path.symlink_to(target)
    else:
        path.write_bytes(b'\xffSyntheticNoFollowCanary')
    with pytest.raises(ValueError) as error:
        _api().discover_group_credentials(ctx.agent, url='https://example.com', operation='find')
    assert 'SyntheticNoFollowCanary' not in str(error.value) + caplog.text


def test_replacement_cycle_cannot_erase_own_pool(tmp_path):
    ctx = _group_case(tmp_path)
    _issue(ctx)
    first = _issue(ctx, submitter='ou_actor_b')
    second = _issue(ctx, submitter='ou_actor_b', replaces_ref=first, replaces_generation=1)
    _change(ctx, first, replaces_ref=second, replaces_generation=1)
    with pytest.raises(ValueError):
        _api().discover_group_credentials(ctx.agent, url='https://example.com', operation='find')


def test_changed_requested_row_is_not_a_transparent_alias_or_stale_authorization(tmp_path):
    ctx = _group_case(tmp_path)
    ref = _issue(ctx)
    before = read_json_object_report(credential_path(ctx.agent.home_paths.owner_home_dir, ref), context='test').payload
    _change(ctx, ref, generation=2)
    with pytest.raises(ValueError):
        _api().resolve_group_credential(ctx.agent, before, _scope(ctx.agent), origin='https://example.com')


@pytest.mark.parametrize('replacement_status,expected', [('ready', False), ('pending', True), ('cancelled', True)])
def test_notification_keeps_only_exact_unsuperseded_rejected_generation(tmp_path, replacement_status, expected):
    from agent_py_agent.agent.user_space.task_credentials import credential_rejection_current

    ctx = _group_case(tmp_path)
    old = _issue(ctx, verification_status='rejected')
    _issue(ctx, replaces_ref=old, replaces_generation=1, status=replacement_status)
    row = read_json_object_report(credential_path(ctx.agent.home_paths.owner_home_dir, old), context='test').payload
    assert _api().group_credential_access(ctx.agent, row, _scope(ctx.agent), operation='status')
    if expected:
        assert _api().group_credential_access(ctx.agent, row, _scope(ctx.agent), operation='notification')
    else:
        with pytest.raises(ValueError):
            _api().group_credential_access(ctx.agent, row, _scope(ctx.agent), operation='notification')
    assert credential_rejection_current(ctx.agent, old, 1) is expected
    ctx.agent._capability_config_runtime_snapshot.config.im_group_shared_credentials_enabled = False
    with pytest.raises(ValueError):
        _api().group_credential_access(ctx.agent, row, _scope(ctx.agent), operation='notification')


@pytest.mark.parametrize('fault', ['finished_claim', 'changed_child_attempt'])
def test_slow_membership_result_cannot_authorize_cancelled_or_changed_execution(tmp_path, fault):
    from agent_py_agent.agent.tooling.web_fetch_runtime import FetchRawRequest, RawResponseParts
    from agent_py_agent.agent.user_space.task_credential_http import (
        credential_authenticated_request,
    )
    from agent_py_agent.tests.test_im_credentials_security_matrix import _child

    ctx = _group_case(tmp_path)
    ref = _issue(ctx)
    if fault == 'changed_child_attempt':
        _child(ctx.agent)
    adapter = ctx.agent.channel_registry.adapter_for('feishu')
    original = adapter.group_authority_snapshot
    calls, sends = [], []

    def snapshot(chat):
        calls.append(chat)
        if fault == 'finished_claim':
            tid = ctx.agent._current_run_params.task_attributes['conversation_thread_id']
            claim = ctx.agent.conversation_store.claims.load(tid)
            ctx.agent.conversation_store.claims.finish({'thread_id': tid, 'claim_id': claim['claim_id'], 'status': 'done'})
        else:
            ctx.agent._current_run_params.attempt_id = 'old-after-membership'
        return original(chat)

    def send(request):
        sends.append(request)
        return RawResponseParts(200, {}, b'ok', request.url)

    adapter.group_authority_snapshot = snapshot
    with pytest.raises(ValueError):
        credential_authenticated_request(ctx.agent, ref, FetchRawRequest(tool='web_fetch', url='https://example.com',
            method='GET', headers={}, data=None, timeout=5), send=send)
    assert len(calls) == 1 and not sends
