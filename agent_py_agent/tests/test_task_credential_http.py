import hashlib
import hmac
import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.common.json_io import read_json_object_report
from agent_py_agent.agent.gateway_parts.private_interaction_service import (
    resolve_private_interaction,
)
from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
from agent_py_agent.agent.tooling.web_fetch_runtime import FetchRawRequest, RawResponseParts
from agent_py_agent.agent.user_space.task_credential_http import credential_authenticated_request
from agent_py_agent.agent.user_space.task_credential_schema import (
    credential_auth_config,
    credential_fields,
)
from agent_py_agent.agent.user_space.task_credentials import (
    CredentialCardOptions,
    credential_path,
    request_credential_card,
)
from agent_py_agent.tests.test_background_tool_approval import background
from agent_py_agent.tests.test_task_credentials import _card
from agent_py_agent.tests.test_web_fetch_ssrf import _tool
from agent_py_agent.tests.test_web_fetch_ssrf import server as _server_fixture

_FIXTURES = (background,)
server = _server_fixture


def _credential(ctx, url, *, authentication='login', config=None):
    _card(ctx)
    fields = [{'name':'username','label':'账号'}, {'name':'secret','label':'密码'}]
    reference, _scope = request_credential_card(ctx.agent, url=url, authentication=authentication,
        options=CredentialCardOptions(fields=fields, auth_config=config))
    path = credential_path(ctx.agent.home_paths.owner_home_dir, reference)
    row = read_json_object_report(path, context='test').payload
    body = {'reference':row['reference'],'decision':'submit','message_id':row['message_id'],'chat_id':row['chat_id'],
            'fields':{'username':'SyntheticLoginUser','secret':'SyntheticHTTPPassword-123'}}
    assert resolve_private_interaction(ctx.agent, body, channel='feishu', actor_id='ou_synthetic')['ok']
    return reference, path, body['fields']


def _config(url):
    return {'url':url, 'body_fields':{'user':'username','password':'secret'}, 'token_path':['access_token'],
            'cookies':True,'csrf_cookie':'csrf'}


def test_controlled_login_session_csrf_and_secret_variants(background):
    url = 'https://example.com/api'
    ref, path, values = _credential(background, url, config=_config('https://example.com/login'))
    calls = []
    def send(request):
        calls.append(request)
        if request.url.endswith('/login'):
            assert json.loads(request.data) == {'user':values['username'],'password':values['secret']}
            return RawResponseParts(200, {'Set-Cookie':'sid=SyntheticCookie-456; Path=/, csrf=SyntheticCSRF-123; Path=/'},
                                    b'{"access_token":"SyntheticToken-123"}', request.url)
        assert request.headers['Authorization'] == 'Bearer SyntheticToken-123'
        assert request.headers['X-CSRF-Token'] == 'SyntheticCSRF-123'
        assert 'sid=SyntheticCookie-456' in request.headers['Cookie']
        assert request.authorization_origin == 'https://example.com:443'
        return RawResponseParts(200, {}, b'ordinary-data', request.url)
    raw = FetchRawRequest('web_fetch', url, 'GET', {}, None, 5)
    for _ in range(2):
        result, secrets = credential_authenticated_request(background.agent, ref, raw, send=send)
        assert result.body == b'ordinary-data'
        assert {'SyntheticToken-123','SyntheticCookie-456','SyntheticCSRF-123',values['secret']} <= set(secrets)
    assert len(calls) == 3
    content = path.read_text()
    assert all(secret not in content for secret in secrets)
    assert 'session_ciphertext' in content


@pytest.mark.parametrize('method, expected_calls', [('GET',4), ('POST',2)])
def test_401_refresh_is_bounded_and_does_not_retry_changes(background, method, expected_calls):
    ref, _path, _values = _credential(background, 'https://example.com/api', config={
        'url':'https://example.com/login','body_fields':{'password':'secret'}, 'token_path':['token']})
    calls = []
    def send(request):
        calls.append(request)
        if request.url.endswith('/login'):
            return RawResponseParts(200, {}, b'{"token":"SyntheticSessionToken"}', request.url)
        return ToolHandlerOutcome('web_fetch',False,'unauthorized',error_code='NETWORK_REQUEST_FAILED',result_envelope={'http_status':401})
    result, _secrets = credential_authenticated_request(background.agent, ref,
        FetchRawRequest('web_fetch','https://example.com/api',method,{},b'{}',5),send=send)
    assert not result.ok and len(calls) == expected_calls


def test_hmac_signature_uses_request_body_and_never_exports_secret(background):
    ref, _path, values = _credential(background,'https://example.com/api',authentication='signature',config={
        'algorithm':'hmac-sha256','secret_field':'secret','header':'X-Request-Signature',
        'parts':[{'request':'method'},{'literal':'\n'},{'request':'path'},{'request':'body_sha256'}]})
    raw = FetchRawRequest('web_fetch','https://example.com/api','POST',{},b'{"ordinary":true}',5)
    def send(request):
        content = 'POST\n/api'+hashlib.sha256(raw.data).hexdigest()
        signature = hmac.digest(values['secret'].encode(),content.encode(),'sha256').hex()
        assert request.headers == {'X-Request-Signature':signature}
        assert values['secret'].encode() not in request.data
        return RawResponseParts(200, {}, b'ok', request.url)
    result, secrets = credential_authenticated_request(background.agent, ref, raw, send=send)
    assert result.body == b'ok' and values['secret'] in secrets


def test_real_pinned_http_login_redacts_individual_cookie_and_token(background, server):
    origin = f'http://127.0.0.1:{server.server_address[1]}'
    ref, _path, values = _credential(background, origin+'/page',config=_config(origin+'/login'))
    old_post, old_get = getattr(server.RequestHandlerClass,'do_POST',None), server.RequestHandlerClass.do_GET
    login_bodies = []
    def post(handler):
        login_bodies.append(json.loads(handler.rfile.read(int(handler.headers['Content-Length']))))
        handler.send_response(200)
        handler.send_header('Content-Type','application/json')
        handler.send_header('Set-Cookie','sid=SyntheticRealCookie; Path=/')
        handler.send_header('Set-Cookie','csrf=SyntheticRealCSRF; Path=/')
        body = b'{"access_token":"SyntheticRealToken"}'
        handler.send_header('Content-Length',str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    def get(handler):
        assert handler.headers['Authorization'] == 'Bearer SyntheticRealToken'
        assert handler.headers['Cookie'] == 'sid=SyntheticRealCookie; csrf=SyntheticRealCSRF'
        handler._send(200,'text/plain',b'SyntheticRealToken SyntheticRealCookie SyntheticRealCSRF SyntheticHTTPPassword-123')
    server.RequestHandlerClass.do_POST = post
    server.RequestHandlerClass.do_GET = get
    tool = _tool()
    tool.credential_authenticator = lambda reference, request, send: credential_authenticated_request(background.agent,reference,request,send=send)
    try:
        result = tool.execute({'url':origin+'/page','credential_ref':ref,'mode':'text'})
    finally:
        if old_post is None:
            delattr(server.RequestHandlerClass,'do_POST')
        else:
            server.RequestHandlerClass.do_POST = old_post
        server.RequestHandlerClass.do_GET = old_get
    assert result.ok and '[private]' in result.output
    assert all(secret not in result.output for secret in (*values.values(),'SyntheticRealToken','SyntheticRealCookie','SyntheticRealCSRF'))
    assert login_bodies == [{'user':values['username'],'password':values['secret']}]
    assert not tool._cache


def test_authorized_307_cross_origin_does_not_forward_login_body(server):
    from agent_py_agent.agent.tooling.web_fetch_runtime import fetch_raw_response
    tool = _tool()
    origin = f'http://127.0.0.1:{server.server_address[1]}'
    old_post = getattr(server.RequestHandlerClass,'do_POST',None)
    count = []
    def post(handler):
        count.append(handler.path)
        handler.rfile.read(int(handler.headers['Content-Length']))
        handler._send(307,'text/plain',b'',{'Location':f'http://localhost:{server.server_address[1]}/forwarded'})
    server.RequestHandlerClass.do_POST = post
    try:
        result = fetch_raw_response(FetchRawRequest('web_fetch',origin+'/login','POST',{},b'SyntheticSecret',5,
            authorization_origin=origin),format_http_error=tool.format_http_error,resolve_pin=tool._resolve_pin)
    finally:
        if old_post is None:
            delattr(server.RequestHandlerClass,'do_POST')
        else:
            server.RequestHandlerClass.do_POST = old_post
    assert isinstance(result,ToolHandlerOutcome) and not result.ok
    assert count == ['/login']


@pytest.mark.parametrize('config',[
    {'algorithm':'eval','parts':[]}, {'algorithm':'sha256','parts':[{'script':'os.environ'}]},
    {'algorithm':'hmac-sha256','secret_field':'missing','parts':[{'request':'method'}]},
])
def test_signature_schema_rejects_arbitrary_code_and_unknown_secret(config):
    with pytest.raises(ValueError):
        credential_auth_config('signature',config,credential_fields('signature'))


def test_disabled_credential_auth_never_decrypts_or_sends(background, monkeypatch):
    ref, _path, _values = _credential(background,'https://example.com/api',config={
        'url':'https://example.com/login','body_fields':{'password':'secret'},'token_path':['token']})
    from types import SimpleNamespace
    monkeypatch.setattr('agent_py_agent.agent.user_space.task_credential_http.capability_config_for_agent',
                        lambda _agent:SimpleNamespace(im_task_credentials_enabled=False))
    def forbidden(_request):
        raise AssertionError('关闭凭据开关不得发送任何请求')
    with pytest.raises(ValueError,match='未启用'):
        credential_authenticated_request(background.agent,ref,
            FetchRawRequest('web_fetch','https://example.com/api','GET',{},None,5),send=forbidden)
