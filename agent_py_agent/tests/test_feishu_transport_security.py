"""携带宿主凭据的请求拒绝第二跳；错误诊断不能包含供应商正文。只用本地 HTTP 和合成秘密。"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from threading import Thread
from urllib.error import HTTPError

import pytest

from agent_py_agent.agent.adapter import feishu
from agent_py_agent.agent.adapter.feishu import FeishuAdapter
from agent_py_agent.agent.artifacts.registry import ArtifactRegistration, register_artifact
from agent_py_agent.agent.delivery import ChannelCapabilities
from agent_py_agent.tests.test_channel_message_tool import _tool


# LLM: 两个不同 origin 的本地测试端点只记录凭据是否到达，关闭后不留服务或真实凭据。
# 函数用途: 重放真实 urllib 第二跳，验证拒绝跳转而不是靠假的 urlopen 掩盖行为。
@contextmanager
def _redirect_origins():
    received = []

    class Destination(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.headers.get('Authorization'))
            body = b'{"code":0,"data":{}}'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    Destination.do_POST = Destination.do_GET
    target = ThreadingHTTPServer(('127.0.0.1', 0), Destination)

    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header('Location', f'http://127.0.0.1:{target.server_port}/capture')
            self.end_headers()

        def log_message(self, *_):
            pass

    Redirect.do_POST = Redirect.do_GET
    origin = ThreadingHTTPServer(('127.0.0.1', 0), Redirect)
    for server in (target, origin):
        Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f'http://127.0.0.1:{origin.server_port}', received
    finally:
        for server in (origin, target):
            server.shutdown()
            server.server_close()


def test_fixed_api_does_not_forward_bearer_to_another_origin(monkeypatch):
    with _redirect_origins() as (base, received):
        monkeypatch.setattr(feishu, '_FEIHSU_API_BASE', base)
        with pytest.raises(HTTPError) as failure:
            feishu._feishu_msg_api('GET', '/synthetic', {}, 'OWNED_SYNTHETIC_TOKEN')
        assert failure.value.code == 302 and not received
        failure.value.close()


def test_render_failure_has_no_provider_msg_data_or_secret(caplog):
    assert not feishu._send_with_fallback('synthetic', lambda *_: {
        'code': 12345, 'msg': 'OWNED_SECRET_SENTINEL', 'data': {'body': 'OWNED_PRIVATE_SENTINEL'}})
    assert 'OWNED_SECRET_SENTINEL' not in caplog.text and 'OWNED_PRIVATE_SENTINEL' not in caplog.text
    assert '12345' in caplog.text


# LLM: 文件/注册/owner/hash/Delivery/Feishu multipart 均用原实现；攻击者只在原文字副作用后替换其自有路径。
# 函数用途: 复现校验后符号链接竞争，最终上传只能携带验证时的原文件字节，不能读到另一 owner。
def test_send_message_keeps_verified_bytes_when_text_side_effect_replaces_path(tmp_path, monkeypatch):
    owner = tmp_path / 'owner'
    owner.mkdir()
    foreign = tmp_path / 'other-owner-secret.txt'
    foreign.write_bytes(b'OWNED_FOREIGN_SENTINEL')
    artifact = owner / 'output.txt'
    artifact.write_bytes(b'OWNED_ORIGINAL_BYTES')
    register_artifact(ArtifactRegistration(workspace_root=owner, path=artifact, artifact_id='legit', status='ready', source='test'))
    tool, _ = _tool(owner)
    adapter = FeishuAdapter({'feishu_session_lock_enabled': False})
    adapter._get_tenant_access_token = lambda: 'OWNED_SYNTHETIC_TOKEN'

    def text_send(*_):
        artifact.unlink()
        artifact.symlink_to(foreign)
        return True

    adapter.send_message = text_send
    tool._delivery.registry.register_adapter('feishu', adapter, capabilities=ChannelCapabilities(
        text=True, reply=True, proactive=True, files=True, frozen_attachment_content=True))
    uploads = []

    def wire(request, timeout):
        uploads.append(bytes(request.data or b''))
        return BytesIO(b'{"code":0,"data":{"file_key":"file_synthetic"}}')

    monkeypatch.setattr(feishu, 'open_authenticated_request', wire)
    result = tool.execute({'message': 'synthetic', 'attachments': ['legit']})
    assert result.ok
    assert uploads and any(b'OWNED_ORIGINAL_BYTES' in body for body in uploads)
    assert all(b'OWNED_FOREIGN_SENTINEL' not in body for body in uploads)


@pytest.mark.parametrize('route', ['message', 'upload', 'resource', 'token', 'card', 'profile', 'reaction', 'qq_send', 'qq_token', 'qq_gateway', 'cloud'])
def test_all_fixed_provider_credential_routes_refuse_second_origin(monkeypatch, route):
    from types import SimpleNamespace
    from urllib.request import Request

    from agent_py_agent.agent.adapter import (
        feishu_card,
        feishu_cloud,
        feishu_profile,
        feishu_typing,
        qq,
    )

    token = 'OWNED_SYNTHETIC_TOKEN'
    with _redirect_origins() as (base, received):
        monkeypatch.setattr(feishu, '_FEIHSU_API_BASE', base)
        monkeypatch.setattr(feishu_card, '_API_BASE', base)
        monkeypatch.setattr(feishu_profile, '_OPEN_BASE', {'feishu': base})
        monkeypatch.setattr(feishu_typing, '_FEISHU_API_BASE', base)
        monkeypatch.setattr(qq, '_QQ_API_BASE', base)
        monkeypatch.setattr(qq, '_QQ_TOKEN_URL', base + '/token')
        monkeypatch.setattr(feishu_cloud, 'API_BASE', base)
        adapter = FeishuAdapter({'feishu_session_lock_enabled': False, 'feishu_app_id': 'owned-app', 'feishu_app_secret': 'OWNED_APP_SECRET'})
        calls = {
            'message': lambda: feishu._feishu_msg_api('GET', '/message', {}, token),
            'upload': lambda: feishu._upload_feishu_bytes(b'owned', 'safe.bin', 'file', {}, token),
            'resource': lambda: feishu._fetch_feishu_resource('om_safe', 'file_safe', 'file', token),
            'token': adapter._request_tenant_access_token,
            'card': lambda: feishu_card._post_json(base + '/card', {}, {'Authorization': 'Bearer ' + token}),
            'profile': lambda: feishu_profile.fetch_feishu_display_name('owned-app', 'OWNED_APP_SECRET', 'ou_owned'),
            'reaction': lambda: feishu_typing._add_reaction('om_owned', 'Typing', token),
            'qq_send': lambda: qq._qq_send_request(Request(base + '/send', headers={'Authorization': 'QQBot ' + token})),
            'qq_token': lambda: qq._qq_request_token('owned-app', 'OWNED_APP_SECRET'),
            'qq_gateway': lambda: qq._qq_fetch_gateway_url(None, token),
            'cloud': lambda: feishu_cloud.request_cloud_api(SimpleNamespace(_get_tenant_access_token=lambda: token), 'GET', '/cloud'),
        }
        try:
            result = calls[route]()
        except HTTPError as error:
            assert error.code == 302
            error.close()
        except feishu_cloud.FeishuCloudError as error:
            assert not error.unknown
        else:
            if route == 'reaction':
                assert result['http_status'] == 302
            else:
                assert result is None
        assert not received


def test_qq_token_debug_and_send_failure_never_log_provider_secrets(monkeypatch, caplog):
    import logging
    from types import SimpleNamespace

    from agent_py_agent.agent.adapter import qq
    from agent_py_agent.agent.adapter.protocol import OutgoingMessage

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(qq, '_qq_request_token', lambda *_: {'access_token': 'OWNED_SECRET_SENTINEL', 'expires_in': 7200})
    owner = SimpleNamespace(_access_token=None, _token_expires_at=0, app_id='owned', app_secret='OWNED_APP_SECRET')
    assert qq._qq_get_access_token(owner) == 'OWNED_SECRET_SENTINEL'
    owner._get_access_token = lambda: 'OWNED_SECRET_SENTINEL'
    monkeypatch.setattr(qq, '_qq_send_request', lambda *_: (False, {'code': 100, 'msg': 'OWNED_PRIVATE_SENTINEL', 'data': {'access_token': 'OWNED_SECRET_SENTINEL'}}))
    assert not qq._qq_send_message(owner, 'owned', OutgoingMessage('qq', 'owned', '合成测试'))
    assert 'OWNED_SECRET_SENTINEL' not in caplog.text and 'OWNED_PRIVATE_SENTINEL' not in caplog.text


def test_gateway_submission_diagnostic_does_not_echo_raw_result(tmp_path, monkeypatch, caplog):
    from agent_py_agent.agent.adapter.manager import ChannelManager
    from agent_py_agent.agent.adapter.protocol import IncomingMessage
    from agent_py_agent.agent.gateway_parts.client_credentials import GatewayClientCredentials
    manager = ChannelManager(delivery_state_dir=tmp_path)
    manager._reply_poll_client.credentials = GatewayClientCredentials(tmp_path, configured_token='OWNED_HOST_TOKEN')
    monkeypatch.setattr('agent_py_agent.agent.adapter.manager.open_authenticated_request', lambda *_args, **_kwargs: BytesIO(
        b'{"status":"queued","message":"OWNED_PRIVATE_SENTINEL","data":{"password":"OWNED_SECRET_SENTINEL"}}'))
    message = IncomingMessage('feishu', 'ou_owned', 'om_owned', '合成消息')
    manager._submit_gateway_payload({'goal': '合成消息'}, message)
    assert 'OWNED_PRIVATE_SENTINEL' not in caplog.text and 'OWNED_SECRET_SENTINEL' not in caplog.text


def test_ws_card_error_diagnostic_has_only_exception_type(monkeypatch, caplog):
    from agent_py_agent.agent.adapter.feishu_ws import FeishuWsClient

    client = FeishuWsClient(app_id='owned-app', app_secret='owned-secret', on_payload=lambda *_: None)
    monkeypatch.setattr('agent_py_agent.agent.adapter.feishu_card.extract_card_action',
        lambda _: {'value': {'session_lock_action': 'pwd_unlock'}, 'operator_open_id': 'ou_owned'})
    def callback(_):
        raise ValueError('OWNED_SECRET_SENTINEL')
    client.on_card_action = callback
    assert client._handle_card_action(object()) is None
    assert 'OWNED_SECRET_SENTINEL' not in caplog.text


def test_gateway_reply_watcher_error_diagnostic_has_no_raw_text(tmp_path, caplog):
    from agent_py_agent.agent.adapter.delivery import (
        GatewayReplyDeliveryStore,
        GatewayReplyDeliveryWorker,
        PendingGatewayReply,
    )

    store = GatewayReplyDeliveryStore(tmp_path)
    def fail(*_):
        raise ValueError('OWNED_PRIVATE_SENTINEL')
    worker = GatewayReplyDeliveryWorker(store, poll_response=lambda _: None, deliver_response=fail)
    worker._start_dispatch = lambda record, _text, _message: record
    pending = PendingGatewayReply('owned-request', 'feishu', 'ou_owned', 'om_owned')
    assert worker._dispatch_message(pending, '合成回复', ('final', 0))[1] is False
    assert 'OWNED_PRIVATE_SENTINEL' not in caplog.text


@pytest.mark.parametrize('route', ['gateway_submit', 'private_card', 'registration'])
def test_other_host_credential_routes_refuse_redirect(monkeypatch, tmp_path, route):
    from types import SimpleNamespace
    from urllib.parse import urlsplit

    from agent_py_agent.agent.adapter import feishu_app_registration, private_controls
    from agent_py_agent.agent.adapter.manager import ChannelManager
    from agent_py_agent.agent.adapter.protocol import IncomingMessage
    from agent_py_agent.agent.gateway_parts.client_credentials import GatewayClientCredentials
    from agent_py_agent.agent.gateway_parts.local_client_token import ensure_local_client_credential

    with _redirect_origins() as (base, received):
        port = urlsplit(base).port
        if route == 'gateway_submit':
            manager = ChannelManager(gateway_port=port, delivery_state_dir=tmp_path)
            manager._reply_poll_client.credentials = GatewayClientCredentials(tmp_path, configured_token='OWNED_HOST_TOKEN')
            with pytest.raises(HTTPError) as failure:
                manager._submit_gateway_payload({'goal': '合成消息'}, IncomingMessage('feishu', 'ou_owned', 'om_owned', '合成消息'))
            assert failure.value.code == 302
            failure.value.close()
        elif route == 'private_card':
            ensure_local_client_credential(tmp_path)
            adapter = SimpleNamespace(my_agent_home=str(tmp_path), _gateway_port=port)
            result = private_controls.tool_approval_card_action(adapter, {'operator_open_id': 'ou_owned',
                'message_id': 'om_owned_card', 'chat_id': 'oc_owned_private',
                'value': {'kind': 'my_agent_task_credential', 'reference': 'owned', 'decision': 'submit'},
                'form_value': {'secret': 'OWNED_PRIVATE_SECRET'}})
            assert result['toast']['type'] == 'error'
        else:
            monkeypatch.setattr(feishu_app_registration, '_accounts_base_url', lambda _: base)
            with pytest.raises(feishu_app_registration.FeishuRegistrationError, match='拒绝 HTTP 跳转'):
                feishu_app_registration._post_registration('feishu', {'client_secret': 'OWNED_APP_SECRET'})
        assert not received
