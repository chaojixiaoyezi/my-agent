"""云资源真实 HTTP/ToolExecutor 合成验收：不访问飞书租户或生产数据。"""
from __future__ import annotations

import json
import threading
import uuid
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.user_space.admin_channel_identity import bind_admin_channel_identity
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call
from agent_py_agent.tests.test_feishu_cloud_tool import SECRET, TOKEN, _agent, _CloudHandler, _grant


# LLM: 只提供合成官方权限/元数据响应；HTTP 记录用于断言禁止业务请求，不触达真实租户或用户。
# 类用途: 为准确用户、群、wiki 与权限撤销提供可变测试服务器。
class _PermissionCloudHandler(_CloudHandler):
    def _handle(self):
        route = urlparse(self.path)
        proof = any(part in route.path for part in ('/permissions/', '/metas/', '/wiki/', '/metainfo', '/protected_range_batch_get'))
        if not proof:
            return super()._handle()
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or '{}')
        self.server.calls.append((self.command, self.path, dict(self.headers), body))
        assert self.headers['Authorization'] == 'Bearer ' + TOKEN
        if '/permissions/' in route.path:
            token = route.path.split('/')[-2]
            scope = parse_qs(route.query).get('perm_type', [''])[0]
            members = self.server.wiki_members.get(scope, self.server.members.get(token, []))
            result = {'code': 0, 'data': {'items': members}}
        elif '/metas/' in route.path:
            assert parse_qs(route.query)['user_id_type'] == ['open_id']
            result = {'code': 0, 'data': {'metas': self.server.metas}}
        elif '/wiki/' in route.path:
            result = {'code': 0, 'data': {'node': {'obj_token': 'sheet_one', 'obj_type': 'sheet'}}}
        elif route.path.endswith('/metainfo'):
            assert parse_qs(route.query) == {'extFields': ['protectedRange'], 'user_id_type': ['open_id']}
            result = {'code': 0, 'data': self.server.sheet_metadata}
        else:
            query = parse_qs(route.query)
            assert query['memberType'] == ['openId']
            ids = query['protectIds'][0].split(',')
            assert len(ids) <= 5
            result = {'code': 0, 'data': {'protectedRanges': [row for row in self.server.protections if row.get('protectId') in ids]}}
        if self.server.permission_code:
            result = {'code': self.server.permission_code, 'msg': SECRET + TOKEN}
        raw = json.dumps(result).encode()
        self.send_response(self.server.permission_status)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def server(monkeypatch):
    from agent_py_agent.agent.adapter import feishu, feishu_cloud

    service = ThreadingHTTPServer(('127.0.0.1', 0), _PermissionCloudHandler)
    service.calls, service.rows, service.members, service.metas = [], [], {}, []
    service.wiki_members = {}
    service.sheet_metadata, service.protections = {'sheets': [{'sheetId': 'sid', 'protectedRange': []}]}, []
    service.api_error, service.fail_http, service.redirect = False, False, ''
    service.conflicting_identifiers, service.permission_code, service.permission_status = False, 0, 200
    base = f'http://127.0.0.1:{service.server_port}/open-apis'
    monkeypatch.setattr(feishu_cloud, 'API_BASE', base)
    monkeypatch.setattr(feishu, '_FEIHSU_API_BASE', base)
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    yield service
    service.shutdown()
    thread.join(timeout=2)
    service.server_close()


def _self_agent(tmp_path, identity):
    agent = _agent(tmp_path, identity)
    agent._capability_config_runtime_snapshot = SimpleNamespace(config=CapabilityConfig())
    return agent


def _member(kind, member_id, permission):
    return {'member_type': kind, 'member_id': member_id, 'perm': permission}


def _call(agent, action='sheet_append', *, token='sheet_one', boundary=None, **params):
    arguments = {'action': action, 'resource_token': token, **params}
    if action.startswith('sheet_'):
        arguments.setdefault('range', 'sid!A1:B2')
    if action in {'sheet_append', 'sheet_update'}:
        arguments.setdefault('values', [['合成行', 123]])
    return execute_registry_test_call(agent.tools, 'feishu_cloud', arguments, write_boundary=boundary,
                                      allowed_tools=['feishu_cloud'], call_id=uuid.uuid4().hex, register_with=agent)


def _business_writes(server):
    return [row for row in server.calls if row[0] in {'POST', 'PUT'} and '/values' in row[1]]


@pytest.mark.parametrize(('identity', 'kind', 'member_id'), [
    (OwnerIdentity.provider_user('feishu', 'ou_one'), 'openid', 'ou_one'),
    (OwnerIdentity.provider_group('feishu', 'oc_one'), 'openchat', 'oc_one'),
])
def test_user_and_group_append_read_back_without_local_grants(tmp_path, server, identity, kind, member_id):
    agent = _self_agent(tmp_path, identity)
    server.members['sheet_one'] = [_member(kind, member_id, 'edit')]
    result = _call(agent)
    assert result.ok, result.output
    assert json.loads(result.output)['resource_authorization']['source'] == 'feishu_collaborator'
    assert 'principal_id' not in json.loads(result.output)['resource_authorization']
    read = _call(agent, 'sheet_read')
    assert json.loads(read.output)['data']['valueRange']['values'] == [['合成行', 123]]
    assert len([row for row in server.calls if '/permissions/' in row[1]]) == 2
    assert SECRET not in result.output and TOKEN not in result.output
    from agent_py_agent.agent.user_space.resource_grants import read_resource_grants

    assert read_resource_grants(agent.home_paths) == []


@pytest.mark.parametrize('members', [
    [_member('openid', 'ou_one', 'edit')],  # 群内某人有权不等于本群有权
    [_member('openchat', 'oc_other', 'edit')],
    [_member('openid', 'cli_synthetic', 'full_access')],
    [_member('openchat', 'oc_one', 'view')],
    [_member('userid', 'oc_one', 'edit')],
])
def test_group_cannot_borrow_person_app_other_group_or_read_only_permission(tmp_path, server, members):
    agent = _self_agent(tmp_path, OwnerIdentity.provider_group('feishu', 'oc_one'))
    server.members['sheet_one'] = members
    result = _call(agent)
    assert not result.ok and result.reported_error_code == 'CLOUD_RESOURCE_NOT_AUTHORIZED'
    assert not _business_writes(server)
    assert not any('/metas/' in row[1] for row in server.calls)


def test_live_permission_revocation_and_read_only_share(tmp_path, server):
    agent = _self_agent(tmp_path, OwnerIdentity.provider_user('feishu', 'ou_one'))
    server.members['sheet_one'] = [_member('openid', 'ou_one', 'edit')]
    assert _call(agent).ok
    server.members['sheet_one'] = [_member('openid', 'ou_one', 'view')]
    assert _call(agent, 'sheet_read').ok
    assert not _call(agent).ok
    server.members.clear()
    assert not _call(agent, 'sheet_read').ok
    assert len(_business_writes(server)) == 1


def test_owner_missing_from_members_is_checked_with_exact_open_id_metadata(tmp_path, server):
    agent = _self_agent(tmp_path, OwnerIdentity.provider_user('feishu', 'ou_one'))
    server.metas = [{'doc_token': 'sheet_one', 'doc_type': 'sheet', 'owner_id': 'ou_one'}]
    result = _call(agent)
    assert result.ok and json.loads(result.output)['resource_authorization']['source'] == 'feishu_owner'
    server.metas = [{'doc_token': 'other_sheet', 'doc_type': 'sheet', 'owner_id': 'ou_one'}]
    assert not _call(agent).ok
    assert len(_business_writes(server)) == 1


def test_wiki_maps_to_one_business_resource_without_double_grant_and_cannot_leak(tmp_path, server):
    agent = _self_agent(tmp_path, OwnerIdentity.provider_group('feishu', 'oc_one'))
    server.members['wiki_one'] = [_member('openchat', 'oc_one', 'edit')]
    assert _call(agent, token='wiki_one', resource_type='wiki').ok
    assert '/sheet_one/values_append' in _business_writes(server)[0][1]
    resolved = _call(agent, 'wiki_resolve', token='wiki_one')
    assert resolved.ok and 'sheet_one' in resolved.output
    server.members.clear()
    denied = _call(agent, 'wiki_resolve', token='wiki_one')
    assert not denied.ok and 'sheet_one' not in denied.output
    assert len(_business_writes(server)) == 1


def test_wiki_page_scope_and_underlying_personal_owner_cannot_replace_page_permission(tmp_path, server):
    agent = _self_agent(tmp_path, OwnerIdentity.provider_user('feishu', 'ou_one'))
    server.wiki_members = {'container': [], 'single_page': [_member('openid', 'ou_one', 'edit')]}
    assert _call(agent, token='wiki_one', resource_type='wiki').ok
    queries = [parse_qs(urlparse(row[1]).query) for row in server.calls if '/permissions/' in row[1]]
    assert queries == [{'type': ['wiki'], 'perm_type': ['container']}, {'type': ['wiki'], 'perm_type': ['single_page']}]
    server.wiki_members = {'container': [], 'single_page': []}
    server.members['sheet_one'] = [_member('openid', 'ou_one', 'full_access')]
    server.metas = [{'doc_token': 'sheet_one', 'doc_type': 'sheet', 'owner_id': 'ou_one'}]
    assert not _call(agent, token='wiki_one', resource_type='wiki').ok
    assert not any('/metas/' in row[1] for row in server.calls)
    assert len(_business_writes(server)) == 1


def test_bad_policy_disabled_feature_and_parent_network_never_request_proof(tmp_path, server):
    agent = _self_agent(tmp_path, OwnerIdentity.provider_group('feishu', 'oc_one'))
    server.members['sheet_one'] = [_member('openchat', 'oc_one', 'edit')]
    assert not _call(agent, boundary={'network_access_mode': 'disabled'}).ok
    agent._capability_config_runtime_snapshot = SimpleNamespace(config=CapabilityConfig(feishu_cloud_collaborator_authorization_enabled=False))
    assert not _call(agent).ok
    assert not any('/permissions/' in row[1] for row in server.calls)
    agent._capability_config_runtime_snapshot = SimpleNamespace(config=CapabilityConfig())
    agent.home_paths.owner_tool_policy_json.write_text('{broken')
    assert not _call(agent).ok
    assert not server.calls


def test_admin_binding_is_exact_and_ambiguous_identity_does_not_fallback(tmp_path, server):
    agent = _self_agent(tmp_path, OwnerIdentity.local_main())
    agent.config.admin_channel_identity_enabled = True
    bind_admin_channel_identity(agent.home_paths.root, 'feishu', 'ou_admin')
    server.members['sheet_one'] = [_member('openid', 'ou_admin', 'edit')]
    assert _call(agent).ok
    bind_admin_channel_identity(agent.home_paths.root, 'feishu', 'ou_second')
    denied = _call(agent)
    assert not denied.ok and denied.reported_error_code == 'CLOUD_RESOURCE_PRINCIPAL_UNAVAILABLE'
    assert len(_business_writes(server)) == 1


@pytest.mark.parametrize(('status', 'code', 'expected'), [
    (403, 99991672, 'FEISHU_CLOUD_SCOPE_REQUIRED'),
    (200, 91403, 'FEISHU_CLOUD_RESOURCE_FORBIDDEN'),
    (401, 0, 'FEISHU_CLOUD_AUTH_INVALID'),
    (404, 0, 'FEISHU_CLOUD_RESOURCE_NOT_FOUND'),
    (429, 0, 'FEISHU_CLOUD_RATE_LIMITED'),
    (503, 0, 'FEISHU_CLOUD_SERVICE_UNAVAILABLE'),
])
def test_proof_failure_returns_typed_public_reason_without_business_effect(tmp_path, server, status, code, expected):
    agent = _self_agent(tmp_path, OwnerIdentity.provider_group('feishu', 'oc_one'))
    server.permission_status, server.permission_code = status, code
    result = _call(agent)
    assert not result.ok and result.reported_error_code == expected
    facts = json.loads(result.output)
    assert facts['http_status'] == (0 if status == 200 else status)
    assert SECRET not in result.output and TOKEN not in result.output
    assert result.effect_outcome != 'unknown' and not _business_writes(server)


def test_write_service_error_stays_unknown_and_is_not_replayed(tmp_path, server):
    admin = _agent(tmp_path, OwnerIdentity.local_main())
    _grant(admin, admin)
    assert _call(admin, 'sheet_read').ok
    server.fail_http = True
    result = _call(admin)
    assert result.reported_error_code == 'FEISHU_CLOUD_SERVICE_UNAVAILABLE'
    assert result.effect_outcome == 'unknown' and len(_business_writes(server)) == 1


@pytest.mark.parametrize('mode', ['unknown-provider-code', 'invalid-data-shape'])
def test_unrecognized_write_failure_remains_unknown_and_sensitive_vendor_text_is_not_returned(tmp_path, server, mode):
    admin = _agent(tmp_path, OwnerIdentity.local_main())
    _grant(admin, admin)
    assert _call(admin, 'sheet_read').ok
    if mode == 'unknown-provider-code':
        server.business_error_code = 99991500
    else:
        server.response_data_override = TOKEN + SECRET
    result = _call(admin)
    assert not result.ok and result.effect_outcome == 'unknown'
    assert SECRET not in result.output and TOKEN not in result.output
    assert len(_business_writes(server)) == 1


def test_admin_resources_read_only_entry_skips_confirmation_but_grant_requires_it(tmp_path):
    admin = _agent(tmp_path, OwnerIdentity.local_main())
    target = _agent(tmp_path, OwnerIdentity.provider_group('feishu', 'oc_one'))
    read = execute_registry_test_call(admin.tools, 'admin_controls', {'action': 'resources', 'owner_id': target.home_paths.owner_id},
                                     allowed_tools=['admin_controls'], register_with=admin)
    assert read.ok, read.output
    write = execute_registry_test_call(admin.tools, 'admin_controls', {'action': 'resource_grant',
        'owner_id': target.home_paths.owner_id, 'resource': {'provider': 'feishu', 'resource_type': 'sheet',
        'resource_token': 'sheet_one', 'operations': ['sheet_read']}}, allowed_tools=['admin_controls'], register_with=admin)
    assert not write.ok and write.error_code == 'APPROVAL_REQUIRED'
