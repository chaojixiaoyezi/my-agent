"""原凭据 HTTP 错误必须保留结构事实；只用临时 owner、合成输入和假运输。"""
from __future__ import annotations

import pytest

from agent_py_agent.agent.common.json_io import read_json_object_report
from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
from agent_py_agent.agent.tooling.web_fetch_runtime import FetchRawRequest, RawResponseParts
from agent_py_agent.agent.user_space.task_credential_http import credential_authenticated_request
from agent_py_agent.tests.test_background_tool_approval import background
from agent_py_agent.tests.test_task_credential_http import _credential
from agent_py_agent.tests.test_web_fetch_ssrf import _tool

_FIXTURES = (background,)


# LLM: 用原卡回调建立bearer/header的单秘密字段，不放宽产品字段schema；仅临时owner与假运输。
# 函数用途: 补齐通用HTTP认证矩阵的合法单字段夹具，不把不兼容的双字段登录夹具误当产品故障。
def _single_field_credential(ctx, authentication):
    from agent_py_agent.agent.gateway_parts.private_interaction_service import (
        resolve_private_interaction,
    )
    from agent_py_agent.agent.user_space.task_credentials import (
        credential_path,
        request_credential_card,
    )
    from agent_py_agent.tests.test_task_credentials import _card

    _card(ctx)
    reference, _ = request_credential_card(ctx.agent, url='https://example.com/api', authentication=authentication)
    path = credential_path(ctx.agent.home_paths.owner_home_dir, reference)
    row = read_json_object_report(path, context='test').payload
    values = {'secret': 'SyntheticSingleFieldSecret'}
    body = {**{key: row[key] for key in ('reference', 'message_id', 'chat_id')}, 'decision': 'submit', 'fields': values}
    assert resolve_private_interaction(ctx.agent, body, channel='feishu', actor_id='ou_synthetic')['ok']
    return reference, path, values


@pytest.mark.parametrize('status,code', [(401, 'PERMISSION_BLOCKED'), (403, 'PERMISSION_BLOCKED'),
    (None, 'TOOL_TIMEOUT'), (None, 'NETWORK_REQUEST_FAILED')])
def test_login_error_keeps_real_http_or_transport_fact(background, monkeypatch, status, code):
    reference, path, values = _credential(background, 'https://example.com/api', config={
        'url': 'https://example.com/login', 'body_fields': {'password': 'secret'}, 'token_path': ['token']})
    sends = []

    # LLM: 只替换逐跳网络的最末运输返回，完整凭据 scope/解密/错误投影/工具净化仍由生产入口执行。
    # 函数用途: 返回真实网络层会产生的 ToolHandlerOutcome，不把 HTTP 错误伪造为成功 RawResponseParts。
    def send(request, **kwargs):
        sends.append(request)
        if len(sends) == 1:
            return ToolHandlerOutcome('web_fetch', False, values['secret'] + ' UnexpectedLoginResponseCanary', error_code=code,
                result_envelope={'http_status': status} if status is not None else {})
        return RawResponseParts(200, {}, b'{"token":"SyntheticSession"}' if request.url.endswith('/login') else b'ok', request.url)

    monkeypatch.setattr('agent_py_agent.agent.tooling.web_fetch_tools.fetch_raw_response', send)
    tool = _tool(gateway_port=1)
    tool.credential_authenticator = lambda ref, request, transport: credential_authenticated_request(
        background.agent, ref, request, send=transport)
    result = tool.execute({'url': 'https://example.com/api', 'credential_ref': reference, 'mode': 'text'})
    assert not result.ok and result.error_code == code
    assert result.result_envelope.get('http_status') == status
    assert len(sends) == 1 and sends[0].method == 'POST'
    assert all(value not in result.output for value in values.values())
    assert 'UnexpectedLoginResponseCanary' not in result.output
    row = read_json_object_report(path, context='test').payload
    assert row['verification_status'] == ('rejected' if status == 401 else 'unverified')
    if status == 401:
        assert result.result_envelope['credential_authentication'] == {
            'status': 'rejected', 'http_status': 401, 'record_updated': True, 'user_action_required': True,
            'credential_ref': reference, 'generation': row['generation']}
    else:
        assert 'credential_authentication' not in result.result_envelope
    second = tool.execute({'url': 'https://example.com/api', 'credential_ref': reference, 'mode': 'text'})
    if status == 401:
        assert not second.ok and len(sends) == 1
    else:
        assert second.ok and len(sends) == 3


@pytest.mark.parametrize('authentication', ['basic', 'bearer', 'header', 'signature'])
def test_direct_authentication_401_exposes_safe_rejection_fact(background, authentication):
    config = ({'algorithm': 'hmac-sha256', 'secret_field': 'secret', 'header': 'X-Signature',
        'parts': [{'request': 'method'}]} if authentication == 'signature' else None)
    reference, path, values = (_single_field_credential(background, authentication) if authentication in {'bearer', 'header'}
        else _credential(background, 'https://example.com/api', authentication=authentication, config=config))
    calls = []

    def send(request):
        calls.append(request)
        return ToolHandlerOutcome('web_fetch', False, values['secret'], error_code='PERMISSION_BLOCKED',
            result_envelope={'http_status': 401})

    response, _ = credential_authenticated_request(background.agent, reference,
        FetchRawRequest('web_fetch', 'https://example.com/api', 'POST', {}, b'{}', 5), send=send)
    assert len(calls) == 1 and response.error_code == 'PERMISSION_BLOCKED'
    assert response.result_envelope['credential_authentication']['status'] == 'rejected'
    assert response.result_envelope['credential_authentication']['user_action_required'] is True
    assert values['secret'] not in response.output
    assert read_json_object_report(path, context='test').payload['verification_status'] == 'rejected'


@pytest.mark.parametrize('method, expected', [('GET', 3), ('POST', 2)])
def test_login_refresh_failure_does_not_repeat_business_write(background, method, expected):
    reference, path, _ = _credential(background, 'https://example.com/api', config={
        'url': 'https://example.com/login', 'body_fields': {'password': 'secret'}, 'token_path': ['token']})
    calls = []

    def send(request):
        calls.append(request)
        if len(calls) == 1:
            return RawResponseParts(200, {}, b'{"token":"Synthetic-Session"}', request.url)
        return ToolHandlerOutcome('web_fetch', False, 'HTTP 401', error_code='PERMISSION_BLOCKED',
            result_envelope={'http_status': 401})

    result, _ = credential_authenticated_request(background.agent, reference,
        FetchRawRequest('web_fetch', 'https://example.com/api', method, {}, b'{}', 5), send=send)
    assert not result.ok and len(calls) == expected
    assert sum(call.url.endswith('/api') for call in calls) == 1
    assert result.result_envelope['credential_authentication']['status'] == 'rejected'
    assert read_json_object_report(path, context='test').payload['verification_status'] == 'rejected'


@pytest.mark.parametrize('changed', ['generation', 'ciphertext'])
def test_late_401_does_not_reject_changed_credential_record(background, changed):
    from agent_py_agent.agent.user_space.task_credentials import (
        _credential_mutation,
        _save_credential_row,
    )
    reference, path, _ = _credential(background, 'https://example.com/api', authentication='basic')

    def send(request):
        with _credential_mutation(background.agent, path) as admission:
            row = read_json_object_report(path, context='test').payload
            if changed == 'generation':
                row['generation'] += 1
            else:
                row['ciphertext'] = 'Synthetic-Replaced-Ciphertext'
            _save_credential_row(admission, path, row)
        return RawResponseParts(401, {}, b'', request.url)

    response, _ = credential_authenticated_request(background.agent, reference,
        FetchRawRequest('web_fetch', 'https://example.com/api', 'GET', {}, None, 5), send=send)
    assert isinstance(response, ToolHandlerOutcome)
    assert response.result_envelope['credential_authentication']['record_updated'] is False
    assert response.result_envelope['credential_authentication']['user_action_required'] is False
    assert 'credential_ref' not in response.result_envelope['credential_authentication']
    assert read_json_object_report(path, context='test').payload['verification_status'] == 'unverified'


@pytest.mark.parametrize('response', [
    RawResponseParts(401.0, {}, b'', 'https://example.com'),
    ToolHandlerOutcome('web_fetch', False, '', result_envelope={'http_status': 401.0}),
    ToolHandlerOutcome('web_fetch', False, '', result_envelope={'http_status': True}),
    {'http_status': 401},
])
def test_authentication_status_does_not_accept_untyped_or_float_facts(response):
    from agent_py_agent.agent.user_space.task_credential_verification import credential_http_status
    assert credential_http_status(response) is None


@pytest.mark.parametrize('body', [b'{}', b'{"token":false}', b'not-json UnexpectedLoginResponseCanary'])
def test_http_200_without_declared_session_is_not_password_rejection(background, body):
    reference, path, _ = _credential(background, 'https://example.com/api', config={
        'url': 'https://example.com/login', 'body_fields': {'password': 'secret'}, 'token_path': ['token']})
    result, _ = credential_authenticated_request(background.agent, reference,
        FetchRawRequest('web_fetch', 'https://example.com/api', 'GET', {}, None, 5),
        send=lambda request: RawResponseParts(200, {}, body, request.url))
    assert not result.ok and result.error_code == 'CREDENTIAL_INPUT_UNAVAILABLE'
    assert result.result_envelope == {'http_status': 200}
    assert 'UnexpectedLoginResponseCanary' not in result.output
    row = read_json_object_report(path, context='test').payload
    assert row['verification_status'] == 'unverified' and 'session_ciphertext' not in row


def test_same_generation_rejected_while_login_does_not_restore_session(background):
    from agent_py_agent.agent.user_space.task_credentials import (
        _credential_mutation,
        _save_credential_row,
    )
    reference, path, _ = _credential(background, 'https://example.com/api', config={
        'url': 'https://example.com/login', 'body_fields': {'password': 'secret'}, 'token_path': ['token']})
    calls = []

    def send(request):
        calls.append(request)
        with _credential_mutation(background.agent, path) as admission:
            latest = read_json_object_report(path, context='test').payload
            latest['verification_status'] = 'rejected'
            _save_credential_row(admission, path, latest)
        return RawResponseParts(200, {}, b'{"token":"SyntheticLateSession"}', request.url)

    with pytest.raises(ValueError, match='失效'):
        credential_authenticated_request(background.agent, reference,
            FetchRawRequest('web_fetch', 'https://example.com/api', 'GET', {}, None, 5), send=send)
    row = read_json_object_report(path, context='test').payload
    assert len(calls) == 1 and 'session_ciphertext' not in row and row['verification_status'] == 'rejected'


@pytest.mark.parametrize('unlimited', [True, False])
def test_login_session_stays_finite_with_unlimited_or_finite_credential(background, unlimited):
    import time

    from agent_py_agent.agent.user_space.task_credentials import (
        _credential_mutation,
        _save_credential_row,
    )
    reference, path, _ = _credential(background, 'https://example.com/api', config={
        'url': 'https://example.com/login', 'body_fields': {'password': 'secret'},
        'token_path': ['token'], 'session_seconds': 300})
    with _credential_mutation(background.agent, path) as admission:
        row = read_json_object_report(path, context='test').payload
        row.update(ttl_seconds=0 if unlimited else 60, ready_expires_at=0 if unlimited else time.time()+60)
        _save_credential_row(admission, path, row)
    before = time.time()
    result, _ = credential_authenticated_request(background.agent, reference,
        FetchRawRequest('web_fetch', 'https://example.com/api', 'GET', {}, None, 5),
        send=lambda request: RawResponseParts(200, {}, b'{"token":"SyntheticSession"}' if request.url.endswith('/login') else b'ok', request.url))
    saved = read_json_object_report(path, context='test').payload
    assert result.status == 200 and before < saved['session_expires_at'] <= time.time()+300
    if unlimited:
        assert saved['ready_expires_at'] == 0 and saved['session_expires_at'] >= before+300
    else:
        assert saved['session_expires_at'] == saved['ready_expires_at'] == row['ready_expires_at']


@pytest.mark.parametrize('concurrent', [False, True])
def test_same_credential_generation_only_first_401_requests_notification(background, concurrent):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from agent_py_agent.agent.user_space.task_credential_verification import (
        credential_response_outcome,
    )
    _, path, _ = _credential(background, 'https://example.com/api', authentication='basic')
    row = read_json_object_report(path, context='test').payload
    barrier = Barrier(2) if concurrent else None

    # LLM: 同时使用原配额/记录锁处理两个已在途的401，不能用调用先后或模型正文去重。
    # 函数用途: 证明同凭据同代只有一次真实状态转换和一次用户处理义务。
    def reject():
        if barrier is not None:
            barrier.wait(timeout=5)
        result = credential_response_outcome(background.agent, path, row,
            ToolHandlerOutcome('web_fetch', False, 'HTTP 401', error_code='PERMISSION_BLOCKED', result_envelope={'http_status': 401}))
        return result.result_envelope['credential_authentication']

    if concurrent:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: reject(), range(2)))
    else:
        results = [reject(), reject()]
    assert sum(item['record_updated'] for item in results) == 1
    assert sum(item['user_action_required'] for item in results) == 1
    first, duplicate = sorted(results, key=lambda item: not item['record_updated'])
    assert (first['credential_ref'], first['generation']) == (row['credential_ref'], row['generation'])
    assert 'credential_ref' not in duplicate and 'generation' not in duplicate
    before = path.read_bytes()
    if barrier is None:
        assert reject()['record_updated'] is False
        assert path.read_bytes() == before
