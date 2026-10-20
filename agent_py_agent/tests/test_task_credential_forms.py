import pytest

from agent_py_agent.agent.common.json_io import read_json_object_report
from agent_py_agent.agent.user_space.task_credential_forms import (
    configure_credential,
    credential_submission_values,
)
from agent_py_agent.agent.user_space.task_credentials import (
    CredentialCardOptions,
    credential_card,
    credential_headers,
    credential_origin,
    credential_path,
    request_credential_card,
    submit_credential_card,
)
from agent_py_agent.tests.test_background_tool_approval import background
from agent_py_agent.tests.test_task_credentials import _card

_FIXTURES = (background,)


@pytest.mark.parametrize('url', ['', 'https://example.com/path'])
@pytest.mark.parametrize('mode', ['password', 'username_password'])
def test_simple_card_four_combinations(background, url, mode):
    _card(background)
    ref, _ = request_credential_card(background.agent, url=url, options=CredentialCardOptions(collection_mode=mode))
    row = read_json_object_report(credential_path(background.agent.home_paths.owner_home_dir, ref), context='test').payload
    inputs = credential_card(row)['elements'][1]['elements']
    assert ('target_url' in {item.get('name') for item in inputs}) == (not url)
    fields = {'secret':'synthetic-password', 'credential_name':'标签'}
    if mode == 'username_password':
        fields['username'] = 'synthetic-user'
    if not url:
        fields['target_url'] = 'https://example.com/path'
    body = {key:row[key] for key in ('reference','message_id','chat_id')}
    result = submit_credential_card(background.agent, row, {**body, 'decision':'submit', 'fields':fields})
    assert '尚未验证' in result['message']
    stored = read_json_object_report(credential_path(background.agent.home_paths.owner_home_dir, ref), context='test').payload
    assert stored['origin'] == 'https://example.com:443'
    assert stored['credential_name'] == '标签' and stored['verification_status'] == 'unverified'
    assert 'synthetic-password' not in str(stored)
    configure_credential(background.agent, ref, authentication='header') if mode == 'password' else configure_credential(background.agent, ref, authentication='basic')
    assert credential_headers(background.agent, ref, 'https://example.com/api')[0]


def test_form_target_and_fields_cannot_smuggle_secret():
    row = {'fields':[{'name':'secret'}], 'origin':''}
    with pytest.raises(ValueError):
        credential_submission_values(row, {'secret':'test','target_url':'https://user:password@example.com'}, credential_origin)
    with pytest.raises(ValueError):
        credential_submission_values(row, {'secret':'test','target_url':'https://example.com','actor':'forged'}, credential_origin)


def test_refresh_retains_old_ready_reference_and_invalidates_old_pending_card(background):
    ref, path, body, _ = _card(background)
    row = read_json_object_report(path, context='test').payload
    submit_credential_card(background.agent, row, body)
    new_ref, _ = request_credential_card(background.agent, url='https://example.com/api', authentication='basic', options=CredentialCardOptions(refresh_ref=ref))
    assert new_ref != ref
    assert credential_headers(background.agent, ref, 'https://example.com/api')[0]
    new_path = credential_path(background.agent.home_paths.owner_home_dir, new_ref)
    pending = read_json_object_report(new_path, context='test').payload
    request_credential_card(background.agent, url='https://example.com/api', authentication='basic', options=CredentialCardOptions(refresh_ref=new_ref))
    latest = read_json_object_report(new_path, context='test').payload
    assert latest['generation'] == pending['generation'] + 1 and latest['reference'] != pending['reference']
    with pytest.raises(ValueError):
        submit_credential_card(background.agent, pending, {**body, 'reference':pending['reference'], 'message_id':pending['message_id']})


def test_rejected_candidate_does_not_replace_old_and_never_sends_card(background):
    from agent_py_agent.agent.tooling.web_fetch_runtime import RawResponseParts
    from agent_py_agent.agent.user_space.task_credential_verification import (
        record_credential_verification,
    )
    ref, path, body, sent = _card(background)
    row = read_json_object_report(path, context='test').payload
    submit_credential_card(background.agent, row, body)
    new_ref, _ = request_credential_card(background.agent, url='https://example.com/api', authentication='basic', options=CredentialCardOptions(refresh_ref=ref))
    new_path = credential_path(background.agent.home_paths.owner_home_dir, new_ref)
    pending = read_json_object_report(new_path, context='test').payload
    submit_credential_card(background.agent, pending, {**body, **{key:pending[key] for key in ('reference','message_id','chat_id')}})
    candidate = read_json_object_report(new_path, context='test').payload
    before = len(sent)
    record_credential_verification(background.agent, new_path, candidate, RawResponseParts(403, {}, b'', 'https://example.com'))
    assert read_json_object_report(new_path, context='test').payload['verification_status'] == 'unverified'
    record_credential_verification(background.agent, new_path, candidate, RawResponseParts(401, {}, b'', 'https://example.com'))
    assert read_json_object_report(new_path, context='test').payload['verification_status'] == 'rejected'
    assert credential_headers(background.agent, ref, 'https://example.com/api')[0] and len(sent) == before
    with pytest.raises(ValueError):
        credential_headers(background.agent, new_ref, 'https://example.com/api')


def test_pending_reissue_replaces_input_at_capacity_one(background, monkeypatch):
    from agent_py_agent.agent.conversation import input_dependency_index
    from agent_py_agent.agent.conversation.input_dependencies import input_dependency_path
    monkeypatch.setattr(input_dependency_index, 'AGENT_TOOL_APPROVAL_MAX_PENDING_COUNT', 1)
    ref, path, _body, _sent = _card(background)
    old = read_json_object_report(path, context='test').payload
    request_credential_card(background.agent, url='https://example.com/api', authentication='basic', options=CredentialCardOptions(refresh_ref=ref))
    new = read_json_object_report(path, context='test').payload
    link, _ = background.agent.conversation_store.tasks.load_report(background.task_id)
    assert link.input_dependency_refs == (new['input_id'],)
    assert read_json_object_report(input_dependency_path(background.agent, old['input_id']), context='test').payload['status'] == 'cancelled'


def test_pending_reissue_registration_failure_restores_old_card(background, monkeypatch):
    from agent_py_agent.agent.conversation import input_dependencies
    ref, path, _body, sent = _card(background)
    old = read_json_object_report(path, context='test').payload
    count = len(sent)

    def unavailable(*args, **kwargs):
        raise OSError('synthetic unavailable')

    monkeypatch.setattr(input_dependencies, 'create_input_dependency', unavailable)
    with pytest.raises(ValueError):
        request_credential_card(background.agent, url='https://example.com/api', authentication='basic', options=CredentialCardOptions(refresh_ref=ref))
    assert read_json_object_report(path, context='test').payload == old
    link, _ = background.agent.conversation_store.tasks.load_report(background.task_id)
    assert old['input_id'] in link.input_dependency_refs and len(sent) == count


def _next_request(ctx, *, other_thread=False):
    from types import SimpleNamespace
    ctx.store.claims.finish({'thread_id':ctx.thread.thread_id, 'claim_id':ctx.claim['claim_id'], 'status':'done'})
    thread = ctx.thread
    if other_thread:
        thread = ctx.store.threads.get_or_create({'canonical_user_id':'local-agent','channel':'chat',
            'channel_conversation_id':'different-thread','channel_user_id':'local-agent'})
    task_id = 'next-request'
    ctx.store.tasks.bind({'thread_id':thread.thread_id,'task_id':task_id,'goal':'重新收集','status':'active'})
    ctx.store.claims.acquire({'thread_id':thread.thread_id,'task_id':task_id,'reason':'thread_goal_continue'})
    ctx.agent._current_run_params = SimpleNamespace(run_id=task_id,task_id=task_id,task_attributes={})
    return task_id


@pytest.mark.parametrize('ready', [False, True])
def test_next_request_same_thread_can_reissue_without_reading_old_credentials(background, ready):
    ref, path, body, _ = _card(background)
    old = read_json_object_report(path, context='test').payload
    if ready:
        submit_credential_card(background.agent, old, body)
        old = read_json_object_report(path, context='test').payload
    task_id = _next_request(background)
    with pytest.raises(ValueError):
        credential_headers(background.agent, ref, 'https://example.com/api')
    updated, _ = request_credential_card(background.agent, url='', options=CredentialCardOptions(refresh_ref=ref, collection_mode='password'))
    new = read_json_object_report(credential_path(background.agent.home_paths.owner_home_dir, updated), context='test').payload
    assert new['scope']['root_task_id'] == task_id
    assert new['authentication'] == 'collected' and new['origin'] == ''
    assert {field['name'] for field in new['fields']} == {'secret'}
    if ready:
        assert submit_credential_card(background.agent, old, body)['decision'] == 'ready'
    else:
        with pytest.raises(ValueError):
            submit_credential_card(background.agent, old, body)
    if ready:
        assert updated != ref and read_json_object_report(path, context='test').payload == old


def test_next_request_other_thread_cannot_reissue(background):
    ref, path, _body, sent = _card(background)
    old = read_json_object_report(path, context='test').payload
    _next_request(background, other_thread=True)
    count = len(sent)
    with pytest.raises(ValueError):
        request_credential_card(background.agent, options=CredentialCardOptions(refresh_ref=ref))
    assert read_json_object_report(path, context='test').payload == old and len(sent) == count


def test_reissue_rejects_forged_original_card(background):
    from agent_py_agent.agent.conversation.tool_approval_scope import tool_approval_scope
    from agent_py_agent.agent.user_space.task_credential_forms import (
        assert_credential_reissue_scope,
    )
    _ref, path, _body, _sent = _card(background)
    old = read_json_object_report(path, context='test').payload
    task_id = _next_request(background)
    scope = tool_approval_scope(background.agent, task_id)
    with pytest.raises(ValueError):
        assert_credential_reissue_scope(background.agent, {**old, 'reference':old['reference'][:-1]+'x'}, scope)


@pytest.mark.parametrize('name', ['target_url','credential_name','credential_note'])
def test_secret_schema_cannot_overlap_plain_metadata(name):
    from agent_py_agent.agent.user_space.task_credential_schema import credential_fields
    with pytest.raises(ValueError):
        credential_fields('collected', [{'name':name,'label':'秘密'}])


# LLM: 只建合成临时凭据并提交，不读秘密或访问网络；认证配置与幂等/结构化错误由真实工具入口验证。
# 函数用途: 为configure回归准备一张已保存的collected或已声明basic凭据。
def _ready_collected_card(background, *, secret='synthetic-password', authentication='collected'):
    ref, _ = request_credential_card(background.agent, url='https://example.com/api', authentication=authentication)
    path = credential_path(background.agent.home_paths.owner_home_dir, ref)
    row = read_json_object_report(path, context='test').payload
    body = {key: row[key] for key in ('reference', 'message_id', 'chat_id')}
    body.update(decision='submit', fields={'username': 'synthetic-user', 'secret': secret})
    submit_credential_card(background.agent, row, body)
    return ref, path


def test_configure_same_scheme_is_idempotent_and_other_schemes_are_structured_errors(background):
    from agent_py_agent.agent.tooling.task_credential_tool import RequestCredentialsTool
    _card(background)
    ref, path = _ready_collected_card(background)
    configure_credential(background.agent, ref, authentication='basic')
    before = path.read_bytes()
    before_inode = path.stat().st_ino
    tool = RequestCredentialsTool(background.agent)
    same = tool.execute({'operation': 'configure', 'credential_ref': ref, 'authentication': 'basic', 'header_name': 'authorization'})
    assert same.ok
    assert same.result_envelope['credential']['already_configured'] is True
    assert same.result_envelope['credential']['authentication_configured'] is True
    assert path.read_bytes() == before and path.stat().st_ino == before_inode
    same_scheme_diff_cfg = tool.execute({'operation': 'configure', 'credential_ref': ref, 'authentication': 'basic', 'auth_config': {'unused': True}})
    assert not same_scheme_diff_cfg.ok and same_scheme_diff_cfg.error_code == 'CREDENTIAL_BINDING_INVALID'
    assert same_scheme_diff_cfg.result_envelope['credential_error']['stage'] == 'record_management'
    assert same_scheme_diff_cfg.result_envelope['credential_error']['diagnostic']['reason'] == 'authentication_already_configured'
    direct_ref, direct_path = _ready_collected_card(background, authentication='basic')
    direct_before = direct_path.read_bytes()
    direct_same = tool.execute({'operation': 'configure', 'credential_ref': direct_ref, 'authentication': 'basic'})
    assert direct_same.ok and direct_same.result_envelope['credential']['already_configured'] is True
    assert direct_path.read_bytes() == direct_before
    changed = tool.execute({'operation': 'configure', 'credential_ref': ref, 'authentication': 'bearer'})
    assert not changed.ok and changed.error_code == 'CREDENTIAL_BINDING_INVALID'
    error = changed.result_envelope['credential_error']
    assert error['stage'] == 'record_management'
    assert error['diagnostic']['reason'] == 'authentication_already_configured'
    assert error['diagnostic']['current_authentication'] == 'basic'
    target_collected = tool.execute({'operation': 'configure', 'credential_ref': ref, 'authentication': 'collected'})
    assert not target_collected.ok and target_collected.error_code == 'TOOL_INVALID_ARGUMENTS'
    assert target_collected.result_envelope['credential_error']['diagnostic']['reason'] == 'authentication_target_collected'
