"""本人真实OAuth密文/刷新→工具→私有CLI进程→本地HTTP供应商，零真实用户资料。"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from agent_py_agent.agent.adapter import feishu_cli, feishu_user_history
from agent_py_agent.agent.common.json_io import write_json_file_atomic
from agent_py_agent.agent.tooling.channel_messages_tool import ChannelMessagesTool
from agent_py_agent.agent.user_space import feishu_oauth as oauth
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call
from agent_py_agent.tests.test_background_tool_approval import background
from agent_py_agent.tests.test_feishu_oauth import _api, _authorize, _row, ctx
from agent_py_agent.tests.test_v14_feishu_message_history import message

_FIXTURES = (background, ctx)


@pytest.fixture
def authorized(ctx, monkeypatch, tmp_path):
    scopes = ['offline_access', 'im:chat:read', 'im:message:readonly',
              'im:message.p2p_msg:get_as_user', 'im:message.group_msg:get_as_user']
    ctx.oauth_config.feishu_oauth_scopes = scopes
    monkeypatch.setattr(feishu_user_history, 'capability_config_for_agent', lambda agent: ctx.oauth_config)
    monkeypatch.setattr(feishu_user_history, 'deployment_capability_config', lambda home: ctx.oauth_config)
    _dependency, state, payload = _authorize(ctx)
    _api(ctx, monkeypatch, {'access_token': 'Synthetic-Access-Token', 'expires_in': 3600,
        'refresh_token': 'Synthetic-Refresh-Token', 'refresh_token_expires_in': 86400, 'scope': ' '.join(scopes)})
    assert oauth.complete_oauth(ctx.agent, payload, state, 'Synthetic-Code')['ok']
    calls, replies = [], []
    ctx.history_hook = None
    ctx.history_provider_code = 0
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path == '/authen/v1/user_info':
                data = {'open_id': 'ou_synthetic'}
            else:
                calls.append((parsed.path, parse_qs(parsed.query), self.headers.get('Authorization')))
                data = replies.pop(0) if replies else {'items': [], 'has_more': False, 'page_token': ''}
                if ctx.history_hook is not None:
                    ctx.history_hook()
            raw = json.dumps({'code': 0 if parsed.path == '/authen/v1/user_info' else ctx.history_provider_code, 'data': data}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    binary = tmp_path / 'private-cli.py'
    binary.write_text('''import json,sys,urllib.request
p=json.load(sys.stdin)
assert p['identity']=='user' and p['actor_id']=='ou_synthetic'
base='http://127.0.0.1:''' + str(server.server_port) + ''''
def read(path):
 with urllib.request.urlopen(urllib.request.Request(base+path,headers={'Authorization':'Bearer '+p['token']})) as response:
  return json.load(response)
identity=read('/authen/v1/user_info')
if identity['data']['open_id']!=p['actor_id']:
 print(json.dumps({'schema':p['schema'],'http_status':0,'request_sent':False,'error_code':'identity_unverified'}))
else:
 print(json.dumps({'schema':p['schema'],'http_status':200,'request_sent':True,'response':read(p['path'])}))
''', encoding='utf-8')
    binary.chmod(0o700)
    monkeypatch.setattr(feishu_cli, 'cli_binary', lambda: binary)
    native = feishu_cli.subprocess.Popen
    def popen(args, *pos, **kwargs):
        return native([sys.executable, str(binary)] if args == [str(binary)] else args, *pos, **kwargs)
    monkeypatch.setattr(feishu_cli.subprocess, 'Popen', popen)
    ctx.history_calls, ctx.history_replies = calls, replies
    try:
        yield ctx
    finally:
        server.shutdown()
        server.server_close()
        worker.join(5)


def test_real_oauth_user_chats_include_p2p_and_groups_and_wire_query(authorized):
    ctx = authorized
    ctx.history_replies.append({'items': [{'chat_id': 'oc_ab', 'name': 'B', 'chat_mode': 'p2p',
        'p2p_target_type': 'user', 'p2p_target_id': 'ou_b'}, {'chat_id': 'oc_f', 'name': 'F', 'chat_mode': 'group'}],
        'has_more': False, 'page_token': ''})
    result = execute_registry_test_call(ctx.agent.tools, 'channel_messages',
        {'action': 'list_chats', 'selector': 'user'}, allowed_tools=['channel_messages'],
        run_id=ctx.task_id, register_with=ctx.agent)
    assert result.ok, (result.error_code, result.output)
    data = json.loads(result.output)
    assert [row['chat_id'] for row in data['items']] == ['oc_ab', 'oc_f']
    assert ctx.history_calls[0][0] == '/im/v1/chats'
    assert ctx.history_calls[0][1]['types'] == ['group,p2p']
    assert ctx.history_calls[0][2] == 'Bearer Synthetic-Access-Token'
    assert 'Synthetic-Access-Token' not in result.output


def test_user_message_pages_cursor_scope_and_foreign_chat_are_preserved(authorized):
    ctx = authorized
    ctx.history_replies.extend([{'items': [message(chat='oc_ab')], 'has_more': True, 'page_token': 'page-two'},
                               {'items': [message(2, chat='oc_ab')], 'has_more': False, 'page_token': ''}])
    tool = ChannelMessagesTool(ctx.agent)
    query = {'action': 'list', 'selector': 'user', 'chat_id': 'oc_ab', 'start_time': 100, 'end_time': 200, 'limit': 1}
    first = tool.execute(query)
    assert first.ok, first.output
    cursor = json.loads(first.output)['next_cursor']
    assert cursor
    rejected = tool.execute({**query, 'chat_id': 'oc_ac', 'cursor': cursor})
    assert not rejected.ok and rejected.error_code == 'TOOL_INVALID_ARGUMENTS'
    assert len(ctx.history_calls) == 1
    second = tool.execute({**query, 'cursor': cursor})
    assert second.ok and json.loads(second.output)['items'][0]['message_id'] == 'om_2'
    assert ctx.history_calls[-1][1]['page_token'] == ['page-two']
    ctx.history_replies.append({'items': [message(chat='oc_foreign')], 'has_more': False, 'page_token': ''})
    foreign = tool.execute(query)
    assert foreign.ok and not json.loads(foreign.output)['items']
    assert 'foreign_chat' in json.loads(foreign.output)['coverage']['reasons']


@pytest.mark.parametrize('fault', ['actor', 'revoked', 'group_owner'])
def test_other_owner_actor_or_revocation_never_reaches_history_http(authorized, fault):
    ctx = authorized
    if fault == 'group_owner':
        ctx.agent.home_paths = type('GroupHome', (), {'owner_kind': 'group'})()
    else:
        row = _row(ctx)
        row['actor_id' if fault == 'actor' else 'status'] = 'ou_other' if fault == 'actor' else 'revoked'
        write_json_file_atomic(oauth.oauth_path(ctx.agent, 'ou_synthetic'), row)
    result = ChannelMessagesTool(ctx.agent).execute({'action': 'list_chats', 'selector': 'user'})
    assert not result.ok and result.error_code in {'FEISHU_USER_AUTH_REQUIRED', 'CHANNEL_MESSAGES_SCOPE_UNVERIFIED'}
    assert ctx.history_calls == []


def test_missing_authorization_and_offline_status_expose_structured_application_help(authorized):
    ctx = authorized
    row = _row(ctx)
    row['status'] = 'revoked'
    write_json_file_atomic(oauth.oauth_path(ctx.agent, 'ou_synthetic'), row)
    tool = ChannelMessagesTool(ctx.agent)
    status = tool.execute({'action': 'status', 'selector': 'user', '__network_access_mode': 'disabled'})
    assert status.ok
    facts = json.loads(status.output)
    assert facts['permission_status'] == 'not_verified' and facts['required_scopes']
    assert '/app/synthetic-app/auth?' in facts['permission_settings_url']
    assert facts['oauth_action'] == {'tool': 'feishu_auth', 'arguments': {'action': 'authorize'}}
    failed = tool.execute({'action': 'list_chats', 'selector': 'user'})
    assert failed.error_code == 'FEISHU_USER_AUTH_REQUIRED'
    assert failed.result_envelope['authorization']['required_scopes'] == ['im:chat:read']
    assert not ctx.history_calls


def test_existing_oauth_refresh_is_used_before_user_history(authorized, monkeypatch):
    import time
    ctx = authorized
    path = oauth.oauth_path(ctx.agent, 'ou_synthetic')
    row = _row(ctx)
    write_json_file_atomic(path, {**row, 'ready_expires_at': time.time() - 1})
    _api(ctx, monkeypatch, {'access_token': 'Refreshed-Token', 'expires_in': 3600})
    result = ChannelMessagesTool(ctx.agent).execute({'action': 'list_chats', 'selector': 'user'})
    assert result.ok, (result.error_code, result.output)
    assert ctx.history_calls[0][2] == 'Bearer Refreshed-Token'
    assert 'Refreshed-Token' not in result.output


def test_user_chat_cursor_binds_types_and_invalid_shape_never_reads_credentials(authorized):
    ctx = authorized
    ctx.history_replies.append({'items': [{'chat_id': 'oc_one'}], 'has_more': True, 'page_token': 'next'})
    first = ChannelMessagesTool(ctx.agent).execute({'action': 'list_chats', 'selector': 'user', 'limit': 1})
    assert first.ok
    cursor = json.loads(first.output)['next_cursor']
    count = len(ctx.history_calls)
    changed = ChannelMessagesTool(ctx.agent).execute({'action': 'list_chats', 'selector': 'user',
        'cursor': cursor, 'chat_types': ['group']})
    assert not changed.ok and len(ctx.history_calls) == count
    bad = ChannelMessagesTool(ctx.agent).execute({'action': 'list_chats', 'selector': 'user', 'chat_types': [{}]})
    assert not bad.ok and len(ctx.history_calls) == count


def test_revocation_between_pages_discards_buffered_messages(authorized):
    ctx = authorized
    ctx.history_replies.append({'items': [message(chat='oc_ab')], 'has_more': True, 'page_token': 'later'})
    def revoke():
        write_json_file_atomic(oauth.oauth_path(ctx.agent, 'ou_synthetic'), {**_row(ctx), 'status': 'revoked'})
    ctx.history_hook = revoke
    result = ChannelMessagesTool(ctx.agent).execute({'action': 'list', 'selector': 'user', 'chat_id': 'oc_ab',
        'start_time': 100, 'end_time': 200})
    assert not result.ok and result.error_code == 'FEISHU_USER_AUTH_REQUIRED'
    assert 'om_1' not in result.output and len(ctx.history_calls) == 1


def test_platform_scope_rejection_is_not_an_empty_success(authorized):
    ctx = authorized
    ctx.history_provider_code = 230027
    result = ChannelMessagesTool(ctx.agent).execute({'action': 'list_chats', 'selector': 'user'})
    assert not result.ok and result.error_code == 'FEISHU_PROVIDER_SCOPE_REQUIRED'
    assert result.result_envelope['authorization']['required_scopes'] == ['im:chat:read']
    assert len(ctx.history_calls) == 1


@pytest.mark.parametrize('change', ['application', 'scopes'])
def test_configuration_change_after_page_discards_buffer_without_second_request(authorized, change):
    ctx = authorized
    ctx.history_replies.append({'items': [message(chat='oc_ab')], 'has_more': True, 'page_token': 'later'})
    def change_configuration():
        if change == 'application':
            ctx.agent.config.feishu_app_id = 'different-synthetic-application'
        else:
            ctx.oauth_config.feishu_oauth_scopes = ['offline_access']
    ctx.history_hook = change_configuration
    result = ChannelMessagesTool(ctx.agent).execute({'action': 'list', 'selector': 'user', 'chat_id': 'oc_ab',
        'start_time': 100, 'end_time': 200})
    assert not result.ok and result.error_code == 'FEISHU_USER_AUTH_REQUIRED'
    assert len(ctx.history_calls) == 1
    assert 'om_1' not in result.output and 'Synthetic-Access-Token' not in result.output
    assert 'channel_messages' not in (result.result_envelope or {})


def test_remaining_history_budget_reaches_process_timeout_and_cleanup(authorized, monkeypatch):
    from types import SimpleNamespace
    ctx = authorized
    clock = [0.0]
    times, payloads, cleanup = [], [], []
    original_check = feishu_user_history.UserHistoryIdentity.check
    checks = [0]
    def check(identity):
        result = original_check(identity)
        checks[0] += 1
        if checks[0] == 2:
            clock[0] = 7.95  # 授权复核用去本次8秒业务预算的大部分。
        return result
    monkeypatch.setattr(feishu_user_history.UserHistoryIdentity, 'check', check)
    fake_clock = SimpleNamespace(monotonic=lambda: clock[0])
    monkeypatch.setattr(feishu_user_history, 'time', fake_clock)
    monkeypatch.setattr(feishu_cli, 'time', fake_clock)
    class Process:
        pid = 23456789
        returncode = None
        def communicate(self, *, input, timeout):
            times.append(timeout)
            payloads.append(json.loads(input))
            clock[0] = 8.01
            raise feishu_cli.subprocess.TimeoutExpired('synthetic-cli', timeout)
        def poll(self):
            return self.returncode
        def terminate(self):
            cleanup.append('terminate')
        def wait(self, *, timeout):
            cleanup.append(('wait', timeout))
            self.returncode = 0
            return 0
    process = Process()
    monkeypatch.setattr(feishu_cli.subprocess, 'Popen', lambda *args, **kwargs: process)
    monkeypatch.setattr(feishu_cli.os, 'killpg', lambda pid, sig: cleanup.append(('signal', pid)), raising=False)
    result = ChannelMessagesTool(ctx.agent).execute({'action': 'list_chats', 'selector': 'user'})
    assert times == [pytest.approx(0.05)]  # 不重新获得25秒，也不多发一次业务请求。
    assert payloads[0]['identity'] == 'user' and payloads[0]['token'] == 'Synthetic-Access-Token'
    assert not result.ok and result.error_code == 'FEISHU_PROVIDER_UNAVAILABLE'
    assert cleanup[-1] == ('wait', 2) and process.returncode == 0
    assert len(cleanup) == 2 and not ctx.history_calls
    assert 'Synthetic-Access-Token' not in result.output
