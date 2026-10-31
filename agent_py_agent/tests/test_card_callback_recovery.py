"""原卡片回调端口/凭据/HTTP失败与真实隔离回环回归，不连接飞书。"""
from __future__ import annotations

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from agent_py_agent.agent.adapter import private_controls
from agent_py_agent.agent.adapter.feishu import FeishuAdapter
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.gateway_parts import model_profile_service, private_interaction_service
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.tests.test_im_model_cards import callback, setup
from agent_py_agent.tests.test_model_profiles import add


def test_cli_adapter_uses_same_gateway_port_home_and_configured_token(tmp_path):
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.cli.adapter import _feishu_adapter_config
    base=SimpleNamespace(config=AgentConfig(gateway_port=18420,gateway_auth_token='synthetic-host-token'),
        home_paths=SimpleNamespace(root=tmp_path))
    adapter=FeishuAdapter(_feishu_adapter_config(base))
    assert adapter._gateway_port==18420
    assert adapter.my_agent_home==str(tmp_path)
    assert adapter._gateway_auth_token=='synthetic-host-token'
    assert FeishuAdapter({})._gateway_port==8420


@pytest.mark.parametrize('failure,expected', [('403','IM_CONTROL_HTTP_AUTH_REJECTED'),
    ('404','IM_CONTROL_HTTP_ROUTE_MISSING'), ('timeout','IM_CONTROL_TIMEOUT'),
    ('unreachable','IM_CONTROL_TRANSPORT_UNAVAILABLE'), ('malformed','IM_CONTROL_RESPONSE_INVALID'),
    ('invalid','IM_INTERACTION_INVALID')])
def test_control_http_failure_has_safe_stage_and_code(tmp_path,monkeypatch,failure,expected):
    def send(request,**kwargs):
        assert kwargs['timeout']==2
        if failure in {'403','404','invalid'}:
            raw=b'{"error_code":"IM_INTERACTION_INVALID","message":"secret-body"}' if failure=='invalid' else b'secret-body'
            raise HTTPError(request.full_url,403 if failure=='invalid' else int(failure),'secret-error',{},io.BytesIO(raw))
        if failure=='timeout':
            raise URLError(TimeoutError('secret-timeout'))
        if failure=='unreachable':
            raise URLError(OSError('secret-network'))
        return SimpleNamespace(__enter__=lambda self:self)
    class Response:
        def __enter__(self):
            return self
        def __exit__(self,*a):
            return False
        def read(self,*a):
            return b'not-json-secret'
    monkeypatch.setattr(private_controls,'open_authenticated_request',lambda *a,**kw:Response() if failure=='malformed' else send(*a,**kw))
    adapter=SimpleNamespace(my_agent_home=tmp_path,_gateway_port=18420,_gateway_auth_token='synthetic')
    result=private_controls._submit_control_request(adapter,'ou_actor',{'reference':'signed'})
    assert result['error_code']==expected
    toast=private_controls._control_failure_toast(result['error_code'],result['error_stage'])
    assert expected in str(toast) and 'secret-' not in str(toast)


@pytest.mark.parametrize('operation', ['select','set_default','add'])
@pytest.mark.parametrize('locked', [False,True])
def test_model_buttons_real_loopback_preserve_owner_and_private_lock(tmp_path,monkeypatch,operation,locked):
    base,_,scope,host,_=setup(tmp_path,monkeypatch,group=True)
    # 未配置的 default 不再是可选模型（e905 目录权威）；选择/设默认需先有一个真实档案。
    profile_id=add(host)[0] if operation!='add' else ''
    model_profile_service.execute_model_text_control(base,parse_conversation_control('/model'),scope)
    body=callback(base,decision=operation,fields={'profile_id':profile_id})
    if locked:
        monkeypatch.setattr(private_interaction_service,'private_interaction_lock_error',lambda *a:'IM_PASSWORD_SETUP_REQUIRED')
    received=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass
        def _read_json(self):
            return json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        def _send_json(self,status,payload):
            self.send_response(status)
            self.send_header('Content-Type','application/json')
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())
        def do_POST(self):
            received.append((self.path,self.headers.get('X-Gateway-Token')))
            self._auth_middleware=SimpleNamespace(has_gateway_credential=lambda headers:headers.get('X-Gateway-Token')=='host-token')
            private_interaction_service.handle_private_interaction(self,SimpleNamespace(agent=base, paths=gateway_paths_from_root(tmp_path / "gateway")))
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    password_cards=[]
    try:
        adapter=SimpleNamespace(my_agent_home=tmp_path,_gateway_port=server.server_port,_gateway_auth_token='host-token',
            _send_password_card=lambda actor,mode:password_cards.append((actor,mode)))
        result=private_controls.tool_approval_card_action(adapter,dict(operator_open_id='ou_actor',message_id=body['message_id'],
            chat_id=body['chat_id'],value=dict(kind='my_agent_model_configuration',reference=body['reference'],
            decision=operation,profile_id=body['fields']['profile_id'])))
        assert received==[('/client/private-interaction','host-token')]
        assert result['toast']['type']==('warning' if locked else 'success')
        assert password_cards==([('ou_actor','set')] if locked else [])
        if operation=='add' and not locked:
            assert base.delivery_service.sent[-1][0].target=='ou_actor'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize('port',[8420,18420])
def test_configured_ports_real_loopback_auth_failure_is_diagnostic(tmp_path,port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass
        def do_POST(self):
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b'{"error":"forbidden","message":"do-not-display-server-body"}')
    try:
        server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    except OSError:
        pytest.skip('目标端口已被占用，不连接既有服务')
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        adapter=FeishuAdapter({'gateway_port':port,'gateway_auth_token':'configured-token','my_agent_home':str(tmp_path),
            'feishu_session_lock_enabled':False})
        result=private_controls._submit_control_request(adapter,'ou_actor',{'reference':'signed'})
        assert result=={'ok':False,'error_code':'IM_CONTROL_HTTP_AUTH_REJECTED','error_stage':'http'}
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_missing_local_credential_does_not_send_or_accept_callback_token(tmp_path,monkeypatch):
    monkeypatch.setattr(private_controls,'open_authenticated_request',lambda *a,**kw:pytest.fail('缺凭据不得请求'))
    adapter=SimpleNamespace(my_agent_home=tmp_path,_gateway_port=18420)
    result=private_controls._submit_control_request(adapter,'ou_actor',{'gateway_auth_token':'forged','reference':'signed'})
    assert result['error_code']=='IM_CONTROL_CREDENTIAL_UNAVAILABLE'


@pytest.mark.parametrize('failure,status,code', [(OSError('secret-path'),503,'IM_INTERACTION_HOST_UNAVAILABLE'),
    (ValueError('secret-state'),403,'IM_INTERACTION_INVALID')])
def test_gateway_control_handler_keeps_safe_failure_stage(tmp_path,monkeypatch,failure,status,code):
    outputs=[]
    handler=SimpleNamespace(headers={'X-Channel':'feishu','X-User-Id':'ou_actor'},_read_json=lambda:{},
        _auth_middleware=SimpleNamespace(has_gateway_credential=lambda headers:True),
        _send_json=lambda status,payload:outputs.append((status,payload)))
    def fail(*args,**kwargs):
        raise failure
    monkeypatch.setattr(private_interaction_service,'resolve_private_interaction',fail)
    private_interaction_service.handle_private_interaction(handler,SimpleNamespace(agent=object(), paths=gateway_paths_from_root(tmp_path / "gateway")))
    assert outputs[0][0]==status and outputs[0][1]['error_code']==code
    assert 'secret-' not in str(outputs)
