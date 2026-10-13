"""合成飞书 HTTP 接口与原 ToolExecutor 的 owner/授权/读回对照，不发送真实飞书数据。"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import unquote

import pytest

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.admin_controls_tool import AdminControlsTool
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from agent_py_agent.agent.user_space.resource_grants import (
    read_resource_grants,
    set_resource_grant,
)
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call

SECRET = 'Synthetic-Feishu-App-Secret-Only'
TOKEN = 'Synthetic-Tenant-Token-Only'


# LLM: 只在本地测试端口提供官方接口形状；记录授权和动作，不访问供应商或真实数据。
# 类用途: 检验真实 HTTP 请求、追加后读回及供应商拒绝/未知状态。
class _CloudHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_PUT(self):
        self._handle()

    def _handle(self):
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or '{}')
        self.server.calls.append((self.command, self.path, dict(self.headers), body))
        if '/auth/' in self.path:
            assert body == {'app_id': 'cli_synthetic', 'app_secret': SECRET}
            result = {'code': 0, 'tenant_access_token': TOKEN, 'expire': 7200}
        else:
            assert self.headers['Authorization'] == 'Bearer ' + TOKEN
            if self.command == 'POST' and self.path.endswith('/values_append'):
                self.server.rows.extend(body['valueRange']['values'])
            elif self.command == 'PUT':
                self.server.rows[:] = body['valueRange']['values']
            if '/wiki/' in self.path:
                result = {'code': 0, 'data': {'node': {'obj_token': 'other_sheet', 'obj_type': 'sheet'}}}
            elif '/docx/' in self.path:
                result = {'code': 0, 'data': {'items': body.get('children', []), 'has_more': False}}
            else:
                result = {'code': 0, 'data': {'valueRange': {'values': self.server.rows}, 'echo': TOKEN + SECRET}}
            if self.server.redirect:
                self.send_response(302)
                self.send_header('Location', self.server.redirect)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if self.server.api_error:
                result = {'code': 99991672, 'msg': SECRET}
            if self.server.conflicting_identifiers:
                result = {'code': 0, 'data': {'obj_token': 'one', 'spreadsheet_token': 'two'}}
        raw = json.dumps(result).encode()
        self.send_response(500 if self.server.fail_http else 200)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def cloud_server(monkeypatch):
    from agent_py_agent.agent.adapter import feishu, feishu_cloud
    server = ThreadingHTTPServer(('127.0.0.1', 0), _CloudHandler)
    server.calls, server.rows = [], []
    server.api_error, server.fail_http, server.redirect = False, False, ''
    server.conflicting_identifiers = False
    base = f'http://127.0.0.1:{server.server_port}/open-apis'
    monkeypatch.setattr(feishu, '_FEIHSU_API_BASE', base)
    monkeypatch.setattr(feishu_cloud, 'API_BASE', base)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    thread.join(timeout=2)
    server.server_close()


def _agent(tmp_path, identity):
    config = AgentConfig(model_backend='echo', my_agent_home=str(tmp_path/'home'), enable_plugins=False,
        my_agent_owner_provider=identity.provider, my_agent_owner_kind=identity.owner_kind,
        my_agent_owner_id=identity.owner_id, feishu_app_id='cli_synthetic', feishu_app_secret=SECRET)
    return SimpleAgent(config, resolve_owner_home(tmp_path/'home', identity).home_dir)


def _grant(admin, agent, kind='sheet', token='sheet_one', operations=None):
    return set_resource_grant(admin.home_paths, agent.home_paths, {'provider': 'feishu', 'resource_type': kind,
        'resource_token': token, 'operations': operations or ['sheet_read', 'sheet_append', 'sheet_update']})


def _call(agent, action, *, token='sheet_one', boundary=None, **kwargs):
    return execute_registry_test_call(agent.tools, 'feishu_cloud', {'action': action, 'resource_token': token, **kwargs},
        write_boundary=boundary, register_with=agent)


@pytest.mark.parametrize('kind', ['ordinary', 'group', 'admin'])
def test_actual_executor_and_http_append_read_update_without_agent_secret(tmp_path, cloud_server, kind):
    admin = _agent(tmp_path, OwnerIdentity.local_main())
    identity = {'ordinary': OwnerIdentity.provider_user('feishu', 'ou_one'),
        'group': OwnerIdentity.provider_group('feishu', 'oc_group'), 'admin': OwnerIdentity.local_main()}[kind]
    agent = admin if kind == 'admin' else _agent(tmp_path, identity)
    _grant(admin, agent)
    append = _call(agent, 'sheet_append', range='sid!A1:B9', values=[['合成行', 123]])
    assert append.ok, (append.error_code, append.output)
    read = _call(agent, 'sheet_read', range='sid!A1:B9')
    assert read.ok and json.loads(read.output)['data']['valueRange']['values'] == [['合成行', 123]]
    assert _call(agent, 'sheet_update', range='sid!A1:B1', values=[['更新', 456]]).ok
    assert json.loads(_call(agent, 'sheet_read', range='sid!A1:B9').output)['data']['valueRange']['values'] == [['更新', 456]]
    assert all(SECRET not in text and TOKEN not in text for text in [append.output, read.output, json.dumps(read_resource_grants(agent.home_paths))])
    assert agent.home_paths.owner_tool_policy_json.stat().st_mode & 0o777 == 0o600
    assert not list(agent.home_paths.owner_home_dir.rglob('*app_secret*'))
    assert unquote(cloud_server.calls[-1][1]).endswith('/values/sid!A1:B9')


def test_cross_owner_missing_operation_revoke_and_full_access_cannot_override(tmp_path, cloud_server):
    admin = _agent(tmp_path, OwnerIdentity.local_main())
    one = _agent(tmp_path, OwnerIdentity.provider_user('feishu', 'ou_one'))
    two = _agent(tmp_path, OwnerIdentity.provider_user('feishu', 'ou_two'))
    _grant(admin, one, operations=['sheet_read'])
    assert not _call(one, 'sheet_append', range='s!A1', values=[[1]]).ok
    denied = _call(two, 'sheet_read', range='s!A1')
    assert denied.reported_error_code == 'CLOUD_RESOURCE_NOT_AUTHORIZED'
    admin.config.access_mode = 'full-access'
    assert not _call(admin, 'sheet_read', range='s!A1').ok
    assert not cloud_server.calls
    grant = read_resource_grants(one.home_paths)[0]
    with pytest.raises(PermissionError):
        set_resource_grant(one.home_paths, two.home_paths, grant)
    set_resource_grant(admin.home_paths, one.home_paths, grant, revoke=True)
    assert not _call(one, 'sheet_read', range='s!A1').ok
    assert not cloud_server.calls


def test_admin_entry_updates_original_policy_preserving_other_fields(tmp_path):
    admin = _agent(tmp_path, OwnerIdentity.local_main())
    target = _agent(tmp_path, OwnerIdentity.provider_group('feishu', 'oc_one'))
    tool = AdminControlsTool(admin)
    original = json.loads(target.home_paths.owner_tool_policy_json.read_text())
    resource = {'provider': 'feishu', 'resource_type': 'sheet', 'resource_token': 'sheet_one', 'operations': ['sheet_read']}
    result = tool.execute({'action': 'resource_grant', 'owner_id': target.home_paths.owner_id, 'resource': resource})
    assert result.ok, result.output
    saved = json.loads(target.home_paths.owner_tool_policy_json.read_text())
    assert all(saved[key] == value for key, value in original.items())
    assert tool.execute({'action': 'resources', 'owner_id': target.home_paths.owner_id}).ok
    assert tool.execute({'action': 'resource_revoke', 'owner_id': target.home_paths.owner_id, 'resource': resource}).ok
    assert read_resource_grants(target.home_paths) == []
    assert not AdminControlsTool(target).execute({'action': 'resource_grant', 'owner_id': target.home_paths.owner_id, 'resource': resource}).ok


def test_wiki_mapping_never_grants_resulting_object_and_document_blocks_use_fixed_parent(tmp_path, cloud_server):
    admin = _agent(tmp_path, OwnerIdentity.local_main())
    agent = _agent(tmp_path, OwnerIdentity.provider_group('feishu', 'oc_one'))
    _grant(admin, agent, kind='wiki', token='wiki_one', operations=['wiki_resolve'])
    resolved = _call(agent, 'wiki_resolve', token='wiki_one')
    assert resolved.ok and 'other_sheet' in resolved.output
    count = len(cloud_server.calls)
    assert not _call(agent, 'sheet_append', token='other_sheet', range='s!A1', values=[[1]]).ok
    assert len(cloud_server.calls) == count
    _grant(admin, agent, kind='docx', token='doc_one', operations=['document_read', 'document_append'])
    children = [{'block_type': 2, 'text': {'elements': [{'text_run': {'content': '合成文档'}}]}}]
    result = _call(agent, 'document_append', token='doc_one', children=children)
    assert result.ok, result.output
    assert cloud_server.calls[-1][1] == '/open-apis/docx/v1/documents/doc_one/blocks/doc_one/children'
    assert cloud_server.calls[-1][3] == {'children': children, 'index': -1}
    assert _call(agent, 'document_read', token='doc_one', page_token='cursor').ok


@pytest.mark.parametrize('mode', ['owner-network', 'parent-network', 'feature', 'malformed-policy'])
def test_disabled_or_invalid_policy_stops_before_credentials_or_http(tmp_path, cloud_server, mode):
    from agent_py_agent.agent.capability.config import CapabilityConfig
    admin = _agent(tmp_path, OwnerIdentity.local_main())
    agent = _agent(tmp_path, OwnerIdentity.provider_user('feishu', 'ou_one'))
    _grant(admin, agent)
    boundary = {}
    if mode == 'owner-network':
        path = agent.home_paths.owner_permissions_json
        data = json.loads(path.read_text())
        data['network']['enabled'] = False
        path.write_text(json.dumps(data))
    elif mode == 'parent-network':
        boundary = {'network_access_mode': 'disabled'}
    elif mode == 'feature':
        agent._capability_config_runtime_snapshot = SimpleNamespace(config=CapabilityConfig(feishu_cloud_tools_enabled=False))
    else:
        agent.home_paths.owner_tool_policy_json.write_text('{broken')
    result = _call(agent, 'sheet_read', range='s!A1', boundary=boundary)
    assert not result.ok
    assert not cloud_server.calls


@pytest.mark.parametrize('mode', ['api', 'unknown-write', 'redirect', 'projection-failed'])
def test_supplier_error_redaction_and_unknown_write_is_not_retried(tmp_path, cloud_server, mode):
    admin = _agent(tmp_path, OwnerIdentity.local_main())
    _grant(admin, admin)
    # 先取得原 token，之后供应商失败不影响认证测试。
    assert _call(admin, 'sheet_read', range='s!A1').ok
    cloud_server.api_error = mode == 'api'
    cloud_server.fail_http = mode == 'unknown-write'
    cloud_server.conflicting_identifiers = mode == 'projection-failed'
    cloud_server.redirect = f'http://127.0.0.1:{cloud_server.server_port}/evil' if mode == 'redirect' else ''
    before = len(cloud_server.calls)
    result = _call(admin, 'sheet_append', range='s!A1', values=[[1]])
    assert not result.ok and SECRET not in result.output and TOKEN not in result.output
    assert len(cloud_server.calls) == before + 1
    assert all('/evil' not in row[1] for row in cloud_server.calls)
    if mode in {'unknown-write', 'projection-failed'}:
        assert result.effect_outcome == 'unknown'


@pytest.mark.parametrize('params', [
    {'action': 'sheet_append', 'resource_token': 'sheet_one', 'range': 's!A1'},
    {'action': 'sheet_read', 'resource_token': '../other', 'range': 's!A1'},
    {'action': 'document_append', 'resource_token': 'doc_one', 'children': []},
    {'action': 'sheet_read', 'resource_token': 'sheet_one', 'range': 's!A1', '__network_access_mode': 'inherit'},
])
def test_invalid_or_forged_parameters_never_reach_http(tmp_path, cloud_server, params):
    admin = _agent(tmp_path, OwnerIdentity.local_main())
    _grant(admin, admin)
    result = execute_registry_test_call(admin.tools, 'feishu_cloud', params, register_with=admin)
    assert not result.ok and not cloud_server.calls
