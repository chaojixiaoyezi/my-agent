from types import SimpleNamespace

import pytest

from agent_py_agent.agent.delivery import ChannelCapabilities
from agent_py_agent.agent.gateway_parts.private_interaction_service import (
    resolve_private_interaction,
)
from agent_py_agent.agent.user_space.admin_channel_identity import bind_admin_channel_identity
from agent_py_agent.agent.user_space.task_credentials import (
    credential_headers,
    credential_path,
    request_credential_card,
)
from agent_py_agent.tests.test_background_tool_approval import background
from agent_py_agent.tests.test_web_fetch_ssrf import server as _server_fixture

_FIXTURES=(background,)
server = _server_fixture


def _card(ctx):
    ctx.agent.config.feishu_session_lock_enabled=False
    ctx.agent._current_run_params=SimpleNamespace(run_id=ctx.task_id,task_id=ctx.task_id,task_attributes={})
    bind_admin_channel_identity(ctx.agent.home_paths.root,'feishu','ou_synthetic')
    sent=[]
    def send(target,card,**kwargs):
        sent.append(card)
        return {'message_id':'om_'+__import__('uuid').uuid4().hex,'chat_id':'oc_private'}
    ctx.agent.channel_registry.register_adapter('feishu',SimpleNamespace(send_control_card=send),capabilities=ChannelCapabilities(control_cards=True,proactive=True))
    reference,_=request_credential_card(ctx.agent,url='https://example.com/api',authentication='basic')
    from agent_py_agent.agent.common.json_io import read_json_object_report
    path=credential_path(ctx.agent.home_paths.owner_home_dir,reference)
    row=read_json_object_report(path,context='test').payload
    body={'reference':row['reference'],'decision':'submit','message_id':row['message_id'],'chat_id':row['chat_id'],
          'fields':{'username':'synthetic-user','secret':'Synthetic-API-Secret-123'}}
    return reference,path,body,sent


def test_private_card_encrypted_bound_and_http_injection(background):
    ref,path,body,sent=_card(background)
    assert 'Synthetic-API-Secret' not in str(sent)
    assert resolve_private_interaction(background.agent,body,channel='feishu',actor_id='ou_synthetic')['ok']
    assert body['fields']['secret'] not in path.read_text()
    headers,secrets=credential_headers(background.agent,ref,'https://example.com/other')
    assert headers['Authorization'].startswith('Basic ') and body['fields']['secret'] in secrets
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        credential_headers(background.agent,ref,'https://other.example.com/api')
    saved = path.read_text()
    assert resolve_private_interaction(background.agent,body,channel='feishu',actor_id='ou_synthetic')['ok']
    assert path.read_text() == saved


@pytest.mark.parametrize('change',[{'message_id':'forwarded'},{'chat_id':'other'},{'reference':'bad'}])
def test_forward_or_invalid_reference_does_not_save(background,change):
    _ref,path,body,_sent=_card(background)
    with pytest.raises(ValueError):
        resolve_private_interaction(background.agent,{**body,**change},channel='feishu',actor_id='ou_synthetic')
    assert 'ciphertext' not in path.read_text()


def test_other_actor_password_setup_and_unlock_gate(background):
    _ref,path,body,_sent=_card(background)
    with pytest.raises(ValueError):
        resolve_private_interaction(background.agent,body,channel='feishu',actor_id='ou_other')
    background.agent.config.feishu_session_lock_enabled=True
    assert resolve_private_interaction(background.agent,body,channel='feishu',actor_id='ou_synthetic')['error_code']=='IM_PASSWORD_SETUP_REQUIRED'
    assert 'ciphertext' not in path.read_text()
    from agent_py_agent.agent.session_lock.service import UnlockService
    from agent_py_agent.agent.session_lock.store import SessionLockStore
    service=UnlockService(SessionLockStore(background.agent.home_paths.root/'session_lock/private_chat_locks.db'),idle_limit_seconds=1)
    service.set_password('ou_synthetic','Synthetic-Test-Password-123')
    service.store.record_activity('ou_synthetic', now=1)
    assert resolve_private_interaction(background.agent,body,channel='feishu',actor_id='ou_synthetic')['error_code']=='IM_UNLOCK_REQUIRED'


def test_concurrent_cards_cannot_cross_submit(background):
    ref1,path1,body1,_=_card(background)
    ref2,path2,body2,_=_card(background)
    assert ref1!=ref2
    with pytest.raises(ValueError):
        resolve_private_interaction(background.agent,{**body1,'message_id':body2['message_id']},channel='feishu',actor_id='ou_synthetic')
    assert 'ciphertext' not in path1.read_text() and 'ciphertext' not in path2.read_text()


def test_same_host_call_reuses_original_card_and_parameter_conflict_fails(background):
    _ref,_path,_body,sent=_card(background)
    first,_=request_credential_card(background.agent,url='https://example.com/api',authentication='bearer',call_id='same-host-call')
    second,_=request_credential_card(background.agent,url='https://example.com/api',authentication='bearer',call_id='same-host-call')
    assert first==second and len(sent)==2
    with pytest.raises(ValueError,match='参数冲突'):
        request_credential_card(background.agent,url='https://other.example/api',authentication='bearer',call_id='same-host-call')


def test_common_secret_echo_variants_are_redacted_before_formatting():
    from agent_py_agent.agent.tooling.web_fetch_tools import _secret_echo_variants
    secret='合成密钥" value'
    values=_secret_echo_variants((secret,))
    assert secret in values and '\\u5408\\u6210\\u5bc6\\u94a5\\" value' in values
    assert '%E5%90%88%E6%88%90%E5%AF%86%E9%92%A5%22%20value' in values


@pytest.mark.parametrize('failed', [False, True])
def test_real_http_with_private_reference_injects_and_redacts_echo(server, failed):
    from agent_py_agent.agent.tooling.web_fetch_tools import _secret_echo_variants
    from agent_py_agent.tests.test_web_fetch_ssrf import _tool
    secret = 'SyntheticPrivateSecret-123'
    tool = _tool()
    tool.credential_resolver = lambda ref, url: ({'Authorization': 'Bearer '+secret}, (secret,))
    original = server.RequestHandlerClass._send
    def echo(handler, status, ctype, body, extra=None):
        original(handler, 401 if failed else status, ctype,
                 ('secret='+secret).encode(), {'X-Private-Echo': secret})
    server.RequestHandlerClass._send = echo
    try:
        result = tool.execute({'url': f'http://127.0.0.1:{server.server_address[1]}/page',
                               'credential_ref': 'synthetic-ref', 'mode': 'text'})
    finally:
        server.RequestHandlerClass._send = original
    assert result.ok is not failed
    assert server.request_headers[-1]['Authorization'] == 'Bearer '+secret
    assert all(value not in result.output for value in _secret_echo_variants((secret,)))
    assert '[private]' in result.output
    assert not tool._cache
