"""Wave 3 consolidated tests (shard c9, group 1: test_v13_w0_registry.py, test_v13_w10_rbac.py, test_v13_w11_skills_learning.py, test_v13_w12_shifts_analytics.py, test_v13_w1_silent_except_diagnostics.py, test_v13_w2_progress_stream_contract.py, test_v13_w3_analysis_schema_fallback.py)."""
import errno
import hashlib
import http.client
import json
import logging
import os
import sys
import urllib.error
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from types import ModuleType, SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.agent_core import self_map_bootstrap as bootstrap
from agent_py_agent.agent.agent_core.tool_loop.round_execution import (
    ToolProgressEvent,
    _payload_progress_detail,
    _structured_tool_progress,
)
from agent_py_agent.agent.backends import gateway_helpers as gateway
from agent_py_agent.agent.backends.base import BackendOptions
from agent_py_agent.agent.backends.errors import (
    ProviderRequestRejectedError,
    ProviderTimeoutError,
    is_provider_recoverable_error,
)
from agent_py_agent.agent.backends.openai_chat import OpenAICompatibleBackend
from agent_py_agent.agent.business_spaces import v13_w12_shifts_analytics as module
from agent_py_agent.agent.business_spaces.v13_store import V13Store
from agent_py_agent.agent.common.cancellation import (
    CancellationToken,
    ToolCancelled,
    bind_cancellation_token,
)
from agent_py_agent.agent.common.log_redaction import redact_sensitive_text
from agent_py_agent.agent.common.request_execution_ports import (
    RequestExecutionOutcomeUncertainError,
)
from agent_py_agent.agent.conversation import runtime, store_io
from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.agent.conversation.display_archive import read_display_archive_page
from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.agent.gateway_parts import business_api_v13 as foundation
from agent_py_agent.agent.gateway_parts import business_api_v13 as v13
from agent_py_agent.agent.gateway_parts import request_execution
from agent_py_agent.agent.gateway_parts.http_handlers import _read_public_progress_events
from agent_py_agent.agent.gateway_parts.paths import gateway_chunk_path
from agent_py_agent.agent.gateway_parts.progress_text_delta import TextDeltaBuffer
from agent_py_agent.agent.gateway_parts.request_context import GatewayConversationContext
from agent_py_agent.agent.gateway_parts.stream_writer import BufferedChunkStreamWriter
from agent_py_agent.agent.plugin_host.contracts import DomainResult
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.plugin_host.schemas import validate_object
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.runtime_contracts import ToolResult, ToolSuccessFacts
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import home_paths_with_owner, resolve_owner_home
from agent_py_agent.agent.workflows import WorkflowEngine
from agent_py_agent.agent.workflows.models import AnalysisContext
from agent_py_agent.tests import test_v13_w0_harness as harness
from agent_py_agent.tests.test_committed_model_messages import _payload
from agent_py_agent.tests.test_display_archive import _owner
from agent_py_agent.tests.test_gateway_identity_trust import _TOKEN, _server
from agent_py_agent.tests.test_tool_round_execution import _canonical_calls
from agent_py_agent.tests.test_v13_w0_harness import v13_call, v13_space
from agent_py_agent.tests.test_v13_w10_identity import client, failure, seed_user, success

# 绑定原夹具对象，保留 pytest 发现语义，避免导入名与测试参数触发重定义。
v13_host = harness.v13_host
v13_native = harness.v13_native


# ============================================================================
# Source: agent_py_agent/tests/test_v13_w0_registry.py
# ============================================================================

# LLM: 只注入测试动作表，不替代真实dispatcher或HTTP信封。
# 函数用途: 为静态注册器准备一个闭合动作。
def action(name='sample.read', *, ready=None, admin=False, aliases=(), handler=None):
    empty = {'type': 'object', 'properties': {}, 'additionalProperties': False}
    return v13.V13Action(name, 'read', empty, empty,
        handler or (lambda call: {}), ready=ready, admin=admin, aliases=aliases)


# LLM: 所有测试仍走原execute_business，未自建替代信封。
# 函数用途: 以当前可信host调用一个真实业务动作。
def _w0_invoke(host, name, params=None, authorize=None):
    context = api.bind_business_context(host, authorize=authorize or (lambda name, facts: True))
    return api.execute_business({'action': name, 'request_id': 'registry-request', 'params': params or {}},
        context, api.BusinessServices(host))


# LLM: 未知动作、错误参数和坏返回值均应在正式边界失败。
# 函数用途: 验证闭合schema前后校验和未知动作码。
def test_v13_unknown_params_and_result_validation(v13_host, monkeypatch):
    assert _w0_invoke(v13_host, 'missing.extension')[1]['error']['code'] == 'UNSUPPORTED_ACTION'
    assert _w0_invoke(v13_host, 'system.capabilities.extensions', {'extra': 1})[0] == 400
    bad = action(handler=lambda call: {'extra': 1})
    monkeypatch.setitem(v13._ACTIONS, bad.name, bad)
    status, body = _w0_invoke(v13_host, bad.name)
    assert status == 400 and body['error']['code'] == 'INVALID_SCHEMA'
    assert body['data'] == {}


# LLM: 发现按规范动作ready与实时权限过滤，别名必须独立获准。
# 函数用途: 验证扩展发现不污染冻结目录。
def test_extensions_discovery_lists_only_ready_and_authorized_v13_actions(v13_host, monkeypatch):
    v13.v13_action_specs()
    monkeypatch.setattr(v13, '_ACTIONS', {'system.capabilities.extensions': v13._ACTIONS['system.capabilities.extensions']})
    monkeypatch.setattr(v13, 'ALIASES', {})
    for item in (action(), action('not.ready', ready=lambda call: False), action('denied.read'),
            action('admin.read', admin=True)):
        monkeypatch.setitem(v13._ACTIONS, item.name, item)
    monkeypatch.setitem(v13.ALIASES, 'sample.alias', 'sample.read')
    v13_host.business_native_admin = lambda: False
    status, body = _w0_invoke(v13_host, 'system.capabilities.extensions',
        authorize=lambda name, facts: name != 'denied.read')
    assert status == 200, body
    assert body['data']['supported_actions'] == ['sample.read', 'system.capabilities.extensions']
    assert body['data']['aliases'] == [{'alias': 'sample.alias', 'canonical': 'sample.read'}]
    frozen = _w0_invoke(v13_host, 'system.capabilities')[1]['data']
    assert 'sample.read' not in frozen['supported_actions']
    assert 'system.capabilities.extensions' not in frozen['supported_actions']


# LLM: HTTP测试从真实Gateway进入，不能由本地函数结果外推路由可用。
# 函数用途: 验证扩展HTTP信封、无任务读口及功能关闭。
def test_real_native_extension_route(v13_native):
    status, body = v13_call(v13_native, 'system.capabilities.extensions', {}, user='business-alice')
    assert status == 200 and body['error'] is None
    status, body = v13_call(v13_native, 'async_jobs.get', {'job_id': 'absent'}, user='business-alice')
    assert status == 404 and body['error']['code'] == 'RESOURCE_NOT_FOUND'
    v13_native[1].config.business_spaces_enabled = False
    status, body = v13_call(v13_native, 'system.capabilities.extensions', {}, user='business-alice')
    assert status == 503 and body['error']['code'] == 'HOST_NOT_READY'


# LLM: 新码不能改旧码映射，含显式HostAPIError与原领域错误。
# 函数用途: 验证五个扩展错误状态。
@pytest.mark.parametrize('code,status', [('VERIFICATION_FAILED',403), ('STATE_CONFLICT',409),
    ('QUERY_REJECTED',400), ('LIMIT_EXCEEDED',400), ('RATE_LIMITED',429)])
def test_new_error_codes_only(code, status):
    assert api._error_code(BusinessError(code))[:2] == (code, status)
    assert api._error_code(HostAPIError(code))[:2] == (code, status)
    assert v13.v13_error_status('CURSOR_EXPIRED') is None
    assert api._error_code(BusinessError('CURSOR_EXPIRED'))[1] == 410
    assert api._error_code(HostAPIError('RESOURCE_NOT_FOUND', 404))[1] == 404


# LLM: 假分片只通过既有静态列表导入，导入内部缺依赖不能静默吞掉。
# 函数用途: 验证静态分片注册及schema、job kind、collection合并。
def test_static_slice_protocol_and_schema_closure(monkeypatch):
    module = ModuleType(v13.V13_SLICE_MODULES[0])
    module.V13_ACTIONS = (action(aliases=('sample.alias',)),)
    module.V13_DEFS = {'V13Example': {'type': 'string'}}
    module.V13_JOB_KINDS = {'example': {'result_schema': {'type':'object'}, 'finalize': dict}}
    module.V13_COLLECTIONS = {'example_rows': 'owner'}
    monkeypatch.setitem(sys.modules, module.__name__, module)
    actions, aliases, defs, jobs, collections = v13._load_registry()
    assert actions['sample.read'].handler is module.V13_ACTIONS[0].handler
    assert aliases['sample.alias'] == 'sample.read'
    assert jobs['example'] is module.V13_JOB_KINDS['example']
    assert defs['V13Example'] == {'type':'string'} and collections['example_rows'] == 'owner'
    monkeypatch.setitem(v13.V13_DEFS, 'V13Example', {'type':'object', 'properties': {'id': {'$ref':'#/$defs/Id'}}})
    schema = v13.v13_schema({'$ref':'#/$defs/V13Example'})
    assert set(schema['$defs']) == {'V13Example', 'Id'}
    with pytest.raises(ValueError):
        v13.v13_schema({'$ref':'#/$defs/V13Absent'})


# LLM: 两个分片对同一事实只能同值，动作和别名不能重名。
# 函数用途: 验证导入时拒绝所有契约冲突。
@pytest.mark.parametrize('conflict', ['action','alias','defs','job','collection'])
def test_registry_conflicts_raise(monkeypatch, conflict):
    first, second = (ModuleType(name) for name in v13.V13_SLICE_MODULES[:2])
    for index, module in enumerate((first, second)):
        module.V13_ACTIONS = (action('sample.' + str(index), aliases=('same.alias',) if conflict == 'alias' else ()),)
        module.V13_DEFS, module.V13_JOB_KINDS, module.V13_COLLECTIONS = {}, {}, {}
        monkeypatch.setitem(sys.modules, module.__name__, module)
    if conflict == 'action':
        second.V13_ACTIONS = first.V13_ACTIONS
    elif conflict == 'defs':
        first.V13_DEFS, second.V13_DEFS = {'V13X': {'type':'string'}}, {'V13X': {'type':'boolean'}}
    elif conflict == 'job':
        first.V13_JOB_KINDS, second.V13_JOB_KINDS = {'sample': {'x':True}}, {'sample': {'x':False}}
    elif conflict == 'collection':
        first.V13_COLLECTIONS, second.V13_COLLECTIONS = {'sample':'owner'}, {'sample':'main'}
    with pytest.raises(ValueError):
        v13._load_registry()


# LLM: 权限随调用末核，复制dataclass或业务角色标签不产生管理员能力。
# 函数用途: 验证context与原生admin门不能伪造。
def test_live_context_and_native_admin_only(v13_host):
    context = api.bind_business_context(v13_host, authorize=lambda name, facts: True)
    services = api.BusinessServices(v13_host)
    call = v13.V13Call('system.capabilities.extensions', {}, 'r', context.owner_id, context.subject_id,
        context, services, services.owner_home)
    assert v13.v13_require_admin(call) is True
    with pytest.raises(HostAPIError):
        v13.v13_require_access(replace(call, context=replace(context)))
    v13_host.business_native_admin = lambda: False
    v13_host.rbac_roles = ['admin']
    with pytest.raises(HostAPIError) as error:
        v13.v13_require_admin(call)
    assert error.value.code == 'BUSINESS_ACCESS_DENIED'


# LLM: 细节只用冻结key/value标量schema；复杂或非法结构不能破坏信封。
# 函数用途: 验证结构化覆盖细节与错误文案分离。
def test_structured_error_details(v13_host, monkeypatch):
    # LLM: 只从合成动作抛出带覆盖细节的领域错误，真实分派器仍负责生成错误信封。
    # 函数用途: 提供用于核对结构化错误细节的失败动作。
    def fail(call):
        raise BusinessError('HOST_NOT_READY', details={'coverage':'unknown','source':'usage'})
    monkeypatch.setitem(v13._ACTIONS, 'sample.read', action(handler=fail))
    status, body = _w0_invoke(v13_host, 'sample.read')
    assert status == 503
    assert body['error']['details'] == [{'key':'coverage','value':'unknown'}, {'key':'source','value':'usage'}]


# ============================================================================
# Source: agent_py_agent/tests/test_v13_w10_rbac.py
# ============================================================================

# LLM: 目录读取必须包含不可分配的原生admin投影。
# 函数用途: 验证issue中的空参数角色列表示例。
def test_roles_list_example_includes_builtin_admin(tmp_path):
    invoke = client(tmp_path)
    role = success(invoke('rbac.roles.list'))['roles'][0]
    assert role['role_id'] == role['name'] == 'admin' and role['builtin'] is True
    seed_user(tmp_path)
    failure(client(tmp_path, user='alice')('rbac.roles.list'), 403, 'BUSINESS_ACCESS_DENIED')


# LLM: 所有权限均来自当前冻结+v13动作目录，未知字符串拒绝且不创建记录。
# 函数用途: 验证权限目录内容与指纹。
def test_permission_catalog_is_frozen_plus_v13_action_names(tmp_path):
    from agent_py_agent.agent.gateway_parts.business_api_v13 import v13_action_specs
    from agent_py_agent.agent.plugin_host.business_action_specs import business_action_specs
    result = success(client(tmp_path)('rbac.permissions.list'))
    names = sorted({spec.action for spec in (*business_action_specs().values(), *v13_action_specs().values())})
    assert [row['permission'] for row in result['permissions']] == names
    assert result['catalog_revision'] == hashlib.sha256(json.dumps(names, separators=(',', ':')).encode()).hexdigest()
    assert {row['source'] for row in result['permissions']} == {'frozen', 'v13'}
    assert next(row for row in result['permissions'] if row['permission'] == 'sessions.space.set')['effect'] == 'config'


# LLM: 创建/更新均经过真实锁内记录、幂等回执与CAS。
# 函数用途: 验证角色CRUD及权限参数拒绝。
def test_role_crud_validates_permissions_against_catalog(tmp_path):
    invoke = client(tmp_path)
    failure(invoke('rbac.roles.create', {'name': 'bad', 'permissions': ['no.such.permission']}), 400, 'INVALID_SCHEMA')
    params = {'name': 'viewer', 'permissions': ['auth.session.me'], 'description': '合成角色'}
    role = success(invoke('rbac.roles.create', params, key='role-create-key-0001'))['role']
    assert success(invoke('rbac.roles.create', params, key='role-create-key-0001'))['role'] == role
    failure(invoke('rbac.roles.create', params), 409, 'STATE_CONFLICT')
    failure(invoke('rbac.roles.update', {'role_id': role['role_id'], 'name': 'reader'}, revision=9), 409, 'REVISION_CONFLICT')
    edited = success(invoke('rbac.roles.update', {'role_id': role['role_id'], 'name': 'reader'}, revision=1))['role']
    assert edited['revision'] == 2 and edited['name'] == 'reader'
    failure(invoke('rbac.roles.update', {'role_id': 'admin'}, revision=0), 409, 'STATE_CONFLICT')
    failure(invoke('rbac.roles.update', {'role_id': 'missing'}, revision=0), 404, 'RESOURCE_NOT_FOUND')
    assert success(invoke('rbac.roles.delete', {'role_id': role['role_id']}, revision=2))['deleted'] is True
    failure(invoke('rbac.roles.delete', {'role_id': role['role_id']}, revision=2), 404, 'RESOURCE_NOT_FOUND')


# LLM: 有成员角色不能删除；移除分配后才允许删除。
# 函数用途: 验证分配引用与删除冲突。
def test_delete_role_with_members_conflicts(tmp_path):
    seed_user(tmp_path)
    invoke = client(tmp_path)
    role = success(invoke('rbac.roles.create', {'name': 'member', 'permissions': []}))['role']
    success(invoke('admin.users.set_roles', {'user_id': 'alice', 'roles': [role['role_id']]}, revision=0))
    failure(invoke('rbac.roles.delete', {'role_id': role['role_id']}, revision=1), 409, 'STATE_CONFLICT')
    assert success(invoke('rbac.roles.list'))['roles'][1]['member_count'] == 1
    success(invoke('admin.users.set_roles', {'user_id': 'alice', 'roles': [], 'mode': 'replace'}, revision=1))
    success(invoke('rbac.roles.delete', {'role_id': role['role_id']}, revision=1))


# LLM: seed补缺而不覆盖，重复请求不扩张权限。
# 函数用途: 验证默认三角色初始化的幂等性。
def test_seed_is_idempotent(tmp_path):
    invoke = client(tmp_path)
    first = success(invoke('rbac.roles.seed'))
    assert len(first['created']) == 3
    second = success(invoke('rbac.roles.seed'))
    assert second['created'] == []
    assert {role['name'] for role in second['roles']} == {'admin', 'soc_admin', 'soc_analyst', 'soc_viewer'}


# LLM: 只输出public_view白名单；分页游标不能修改过滤条件复用。
# 函数用途: 验证用户列表、详情与游标拒绝。
def test_admin_users_list_get_projection(tmp_path):
    seed_user(tmp_path)
    seed_user(tmp_path, 'bob')
    invoke = client(tmp_path)
    first = success(invoke('admin.users.list', {'limit': 1}))
    assert first['next_cursor'] is not None
    second = success(invoke('admin.users.list', {'limit': 1, 'cursor': first['next_cursor']}))
    assert first['items'][0]['user_id'] != second['items'][0]['user_id']
    failure(invoke('admin.users.list', {'limit': 2, 'cursor': first['next_cursor']}), 410, 'CURSOR_EXPIRED')
    user = success(invoke('admin.users.get', {'user_id': 'alice'}))['user']
    assert user['has_password'] is True and user['is_admin'] is False
    assert 'password_hash' not in json.dumps(user)
    failure(invoke('admin.users.get', {'user_id': 'nobody'}), 404, 'RESOURCE_NOT_FOUND')


# LLM: 三种模式都使用同一分配记录CAS，禁止分配原生admin。
# 函数用途: 验证替换添加移除和修订冲突。
def test_set_roles_replace_add_remove_modes(tmp_path):
    seed_user(tmp_path)
    invoke = client(tmp_path)
    a = success(invoke('rbac.roles.create', {'name': 'a', 'permissions': []}))['role']['role_id']
    b = success(invoke('rbac.roles.create', {'name': 'b', 'permissions': []}))['role']['role_id']
    assert success(invoke('admin.users.set_roles', {'user_id': 'alice', 'roles': [a]}, revision=0))['roles'] == ['a']
    assert success(invoke('admin.users.set_roles', {'user_id': 'alice', 'roles': [b], 'mode': 'add'}, revision=1))['roles'] == ['a', 'b']
    assert success(invoke('admin.users.set_roles', {'user_id': 'alice', 'roles': [a], 'mode': 'remove'}, revision=2))['roles'] == ['b']
    failure(invoke('admin.users.set_roles', {'user_id': 'alice', 'roles': []}, revision=1), 409, 'REVISION_CONFLICT')
    failure(invoke('admin.users.set_roles', {'user_id': 'alice', 'roles': ['admin']}, revision=3), 409, 'STATE_CONFLICT')
    failure(invoke('admin.users.set_roles', {'user_id': 'alice', 'roles': ['missing']}, revision=3), 404, 'RESOURCE_NOT_FOUND')


# LLM: 状态变更走原生生命周期，未授权主体不能启停别人的账号。
# 函数用途: 验证账号停用、重放和重新启用。
def test_set_status_uses_native_lifecycle_and_delete_maps_to_disable(tmp_path):
    user = seed_user(tmp_path)
    invoke = client(tmp_path)
    params = {'user_id': 'alice', 'status': 'disabled', 'reason': '合成停用原因'}
    key = uuid4().hex
    disabled = success(invoke('admin.users.set_status', params, key=key, revision=user.revision))['user']
    assert disabled['status'] == 'disabled' and disabled['revision'] == user.revision + 1
    assert success(invoke('admin.users.set_status', params, key=key, revision=user.revision))['user'] == disabled
    failure(invoke('admin.users.set_status', {'user_id': 'alice', 'status': 'enabled'}, revision=user.revision), 409, 'REVISION_CONFLICT')
    assert success(invoke('admin.users.set_status', {'user_id': 'alice', 'status': 'enabled'}, revision=disabled['revision']))['user']['status'] == 'enabled'


# LLM: 聚合本地角色和明确身份关联，不把关联当管理员权限。
# 函数用途: 验证跨身份来源有效权限。
def test_effective_permissions_union_across_sources(tmp_path):
    from agent_py_agent.agent.common.json_io import append_private_jsonl_records
    seed_user(tmp_path)
    invoke = client(tmp_path)
    role = success(invoke('rbac.roles.create', {'name': 'reader', 'permissions': ['auth.session.me']}))['role']
    success(invoke('admin.users.set_roles', {'user_id': 'alice', 'roles': [role['role_id']]}, revision=0))
    ordinary = client(tmp_path, user='alice')
    append_private_jsonl_records(ordinary.host.home_paths.linked_identities_jsonl, [{'provider': 'feishu',
        'provider_subject_id': 'synthetic-feishu-user', 'owner_id': 'local/alice', 'status': 'active'}])
    data = success(ordinary('identity.effective_permissions'))
    assert data['is_admin'] is False and data['roles'] == ['reader']
    assert data['permissions'] == [{'permission': 'auth.session.me', 'granted_by': ['role:' + role['role_id']]}]
    assert {row['provider'] for row in data['providers']} == {'local', 'feishu'}
    failure(ordinary('identity.effective_permissions', {'user_id': 'main'}), 403, 'BUSINESS_ACCESS_DENIED')


# LLM: soc_admin和任意自定义权限集合都不能越过W0原生管理员门。
# 函数用途: 验证角色不能提升网关管理员。
def test_envelopes_and_custom_roles_do_not_grant_gateway_admin(tmp_path):
    seed_user(tmp_path)
    invoke = client(tmp_path)
    seeded = success(invoke('rbac.roles.seed'))
    role = next(role for role in seeded['roles'] if role['name'] == 'soc_admin')
    success(invoke('admin.users.set_roles', {'user_id': 'alice', 'roles': [role['role_id']]}, revision=0))
    ordinary = client(tmp_path, user='alice')
    failure(ordinary('admin.users.list'), 403, 'BUSINESS_ACCESS_DENIED')
    failure(ordinary('rbac.roles.create', {'name': 'escape', 'permissions': []}), 403, 'BUSINESS_ACCESS_DENIED')
    assert success(ordinary('auth.session.me'))['me']['is_admin'] is False


# LLM: 角色名可达64字符，来源标识不能因此超出granted_by的64字符上限。
# 函数用途: 验证最长角色名使用稳定role_id描述权限来源。
def test_effective_permissions_accepts_maximum_length_role_name(tmp_path):
    seed_user(tmp_path)
    invoke = client(tmp_path)
    role = success(invoke('rbac.roles.create', {'name': 'r' * 64, 'permissions': ['auth.session.me']}))['role']
    success(invoke('admin.users.set_roles', {'user_id': 'alice', 'roles': [role['role_id']]}, revision=0))
    data = success(client(tmp_path, user='alice')('identity.effective_permissions'))
    assert data['roles'] == ['r' * 64]
    assert data['permissions'] == [{'permission': 'auth.session.me', 'granted_by': ['role:' + role['role_id']]}]


# LLM: 投影源损坏须在账号副作用前拒绝，不能先停用再报告未开始。
# 函数用途: 验证损坏MFA投影不改变原生账号状态。
def test_set_status_reads_failure_prone_projection_before_native_commit(tmp_path):
    from agent_py_agent.agent.user_space.local_user_store import LocalUserStore
    user = seed_user(tmp_path)
    path = tmp_path / 'local_users_auth/alice.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{broken', encoding='utf-8')
    status, body = client(tmp_path)('admin.users.set_status', {'user_id': 'alice', 'status': 'disabled'}, revision=user.revision)
    assert status == 503 and body['state'] == 'failed'
    assert LocalUserStore(tmp_path).read_user('alice').status == 'enabled'


# LLM: 真实原生提交后注入回执故障，outcome必须保留committed并可同键恢复。
# 函数用途: 验证原生成功与业务回执失败之间的恢复语义。
def test_set_status_post_native_receipt_failure_is_committed(tmp_path, monkeypatch):
    from agent_py_agent.agent.business_spaces import v13_w10_identity as identity
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    from agent_py_agent.agent.user_space.local_user_store import LocalUserStore
    user = seed_user(tmp_path)
    write = identity.write

    # LLM: 注入点位于真实副作用之后，不伪造原生返回或存储结果。
    # 函数用途: 让业务事务在原生已提交后失败。
    def fail_after_native(call, collection, mutate, *, scope='owner', receipt_view=None):
        # LLM: 真正执行原mutate后再抛错误，W0回执不会提交。
        # 函数用途: 模拟业务回执无法持久化。
        def crash(records):
            mutate(records)
            raise HostAPIError('HOST_NOT_READY', 503)
        return write(call, collection, crash, scope=scope, receipt_view=receipt_view)

    invoke, key = client(tmp_path), uuid4().hex
    params = {'user_id': 'alice', 'status': 'disabled'}
    with monkeypatch.context() as patch:
        patch.setattr(identity, 'write', fail_after_native)
        result = invoke('admin.users.set_status', params, key=key, revision=user.revision)
        failure(result, 503, 'HOST_NOT_READY')
        assert result[1]['effect_outcome'] == 'committed'
    native = LocalUserStore(tmp_path).read_user('alice')
    assert native.status == 'disabled' and native.revision == user.revision + 1
    restored = success(invoke('admin.users.set_status', params, key=key, revision=user.revision))['user']
    assert restored['status'] == 'disabled' and restored['revision'] == native.revision


# LLM: 身份账追加移动/撤销必须覆盖旧关联，local行不能错误投影成feishu。
# 函数用途: 使用原生canonical关联写口验证来源和最新记录解析。
def test_provider_links_keep_provider_and_latest_binding(tmp_path):
    from agent_py_agent.agent.user_space.identity_store import (
        CanonicalIdentityLinkRequest,
        link_canonical_identity,
    )
    seed_user(tmp_path)
    ordinary = client(tmp_path, user='alice')
    home = ordinary.host.home_paths
    link_canonical_identity(home, CanonicalIdentityLinkRequest('canonical-alice', 'local', 'alice', 'local/alice'))
    link_canonical_identity(home, CanonicalIdentityLinkRequest('canonical-alice', 'feishu', 'moved-subject', 'local/alice'))
    link_canonical_identity(home, CanonicalIdentityLinkRequest('canonical-bob', 'feishu', 'moved-subject', 'local/bob'))
    link_canonical_identity(home, CanonicalIdentityLinkRequest('canonical-alice', 'feishu', 'revoked-subject', 'local/alice'))
    link_canonical_identity(home, CanonicalIdentityLinkRequest('canonical-alice', 'feishu', 'revoked-subject', 'local/alice', 'revoked'))
    data = success(ordinary('identity.effective_permissions'))
    assert data['providers'] == [{'provider': 'local', 'linked': True, 'subject': 'alice'}]


# ============================================================================
# Source: agent_py_agent/tests/test_v13_w11_skills_learning.py
# ============================================================================

USER = 'business-alice'
OTHER = 'business-bob'


# LLM: 测试技能直接落成原生目录形状，验证的是产品读口而不是自造 DTO。
# 函数用途: 在隔离 owner home 中写一个技能。
def _write_skill(home, layer, name, body):
    root = {'user': home / 'skills' / name,
            'workspace': home / 'workspace' / '.agents' / 'skills' / name,
            'learned': home / 'skills' / 'learned' / name}[layer]
    root.mkdir(parents=True, exist_ok=True)
    (root / 'SKILL.md').write_text(body, encoding='utf-8')
    return root


# LLM: 标准头部保证正文校验、名称和标签都有确定输入。
# 函数用途: 生成一份包含 frontmatter 的技能正文。
def _body(name='demo-skill', description='演示技能'):
    return ('---\nname: ' + name + '\ndescription: ' + description
            + '\nwhen_to_use: 需要演示时\ntags: alpha,beta\n---\n\n正文内容。\n')


# LLM: 业务调用的 owner 是真实渠道用户，不是启动网关的本地 owner；测试落盘必须用同一 owner 路径投影。
# 函数用途: 返回测试用户在隔离宿主中的 owner 路径集合。
def _user_paths(agent, user=USER):
    from agent_py_agent.agent.user_space.owner_resolver import (
        OwnerIdentity,
        home_paths_with_owner,
        resolve_owner_home,
    )

    paths = agent.home_paths
    return home_paths_with_owner(paths, resolve_owner_home(
        paths.root, OwnerIdentity.provider_user('feishu', user)))


# LLM: 学习登记只由原生 registry 决定，测试按原生 schema v1 写一行。
# 函数用途: 把一个技能登记为自学 Skill。
def _register_learned(home, name, body):
    from hashlib import sha256
    directory = home / 'data' / 'skill_learning'
    directory.mkdir(parents=True, exist_ok=True)
    stamp = '2026-11-02T00:00:00+00:00'
    record = {'schema_version': 'my-agent.skill-learning-registry.v1',
              'skills': {name: {'name': name, 'version': 1, 'revision': 1, 'last_operation_id': '',
                                'maintenance': {}, 'layout': 'learned',
                                'sha256': sha256(body.encode('utf-8')).hexdigest(),
                                'created_at': stamp, 'updated_at': stamp, 'source_run_ids': []}},
              'blocked_names': [], 'daily': {'date': '', 'calls': 0}, 'daily_history': {},
              'import_operations': {}, 'assets': {}, 'last_maintenance_at': 0}
    (directory / 'registry.json').write_text(json.dumps(record), encoding='utf-8')


# LLM: 真实 HTTP 入口覆盖层级遮蔽、筛选与分页，不手写响应。
# 函数用途: 验证 workspace > user > builtin/learned 的列表顺序。
def test_list_layers_precedence(v13_native):
    server, agent = v13_native
    home = _user_paths(agent).owner_home_dir
    _write_skill(home, 'user', 'demo-skill', _body('demo-skill', '用户层'))
    _write_skill(home, 'workspace', 'demo-skill', _body('demo-skill', '工作区层'))
    _write_skill(home, 'learned', 'lesson-abcdef123456', _body('lesson-abcdef123456', '自学层'))
    _register_learned(home, 'lesson-abcdef123456', _body('lesson-abcdef123456', '自学层'))

    status, body = v13_call(server, 'skills.list', {}, user=USER)
    assert status == 200, body
    assert body['state'] == 'completed' and body['error'] is None
    rows = {row['name']: row for row in body['data']['items'] if row['name'] == 'demo-skill'}
    assert rows['demo-skill']['layer'] == 'workspace'
    assert rows['demo-skill']['description'] == '工作区层'
    learned = [row for row in body['data']['items'] if row['name'] == 'lesson-abcdef123456']
    assert learned and learned[0]['layer'] == 'learned'

    status, body = v13_call(server, 'skills.list', {'layer': 'user'}, user=USER)
    assert status == 200, body
    assert all(row['layer'] == 'user' for row in body['data']['items'])
    assert body['data']['coverage'] == 'complete'


# LLM: 游标必须绑定筛选条件；篡改后的令牌走既有 CURSOR_EXPIRED 语义。
# 函数用途: 验证技能分页与游标过期。
def test_list_cursor_expired_on_tamper(v13_native):
    server, agent = v13_native
    home = _user_paths(agent).owner_home_dir
    for name in ('page-a', 'page-b', 'page-c'):
        _write_skill(home, 'user', name, _body(name))
    status, body = v13_call(server, 'skills.list', {'limit': 1}, user=USER)
    assert status == 200, body
    cursor = body['data']['next_cursor']
    assert cursor and body['data']['coverage'] == 'partial'
    status, body = v13_call(server, 'skills.list', {'limit': 1, 'cursor': cursor + 'x'}, user=USER)
    assert status == 410, body
    assert body['error']['code'] == 'CURSOR_EXPIRED'


# LLM: 正文、资源清单和链接拒绝来自同一次原生读取。
# 函数用途: 验证技能详情与资源枚举。
def test_get_body_and_resources(v13_native):
    server, agent = v13_native
    home = _user_paths(agent).owner_home_dir
    directory = _write_skill(home, 'user', 'detail-skill', _body('detail-skill'))
    (directory / 'references').mkdir()
    (directory / 'references' / 'notes.md').write_text('参考内容\n', encoding='utf-8')
    os.symlink('/etc/passwd', directory / 'references' / 'escape.md')

    status, body = v13_call(server, 'skills.get', {'skill_id': 'detail-skill'}, user=USER)
    assert status == 200, body
    data = body['data']
    assert '正文内容' in data['body_markdown']
    paths = {row['path']: row for row in data['resources']}
    assert paths['references/notes.md']['text'] == '参考内容\n'
    assert 'references/escape.md' not in paths
    assert data['skill']['layer'] == 'user' and len(data['skill']['version_ref']) == 64

    status, body = v13_call(server, 'skills.get', {'skill_id': 'missing-skill'}, user=USER)
    assert status == 404, body
    assert body['error']['code'] == 'RESOURCE_NOT_FOUND'


# LLM: 校验只在临时目录解析，坏头部与成功形状都要结构化返回。
# 函数用途: 验证 frontmatter 错误的现场报告。
def test_validate_reports_frontmatter_errors(v13_native):
    server, _agent = v13_native
    status, body = v13_call(server, 'skills.validate', {'body_markdown': '# 没有头部'}, user=USER)
    assert status == 200, body
    data = body['data']
    assert data['valid'] is False and data['descriptor'] is None
    assert data['errors'][0]['code'] == 'INVALID_FRONTMATTER'

    status, body = v13_call(server, 'skills.validate',
                            {'body_markdown': _body('checked-skill'), 'skill_id': 'checked-skill'},
                            user=USER)
    assert status == 200, body
    assert body['data']['valid'] is True and body['data']['errors'] == []
    assert body['data']['descriptor']['name'] == 'checked-skill'


# LLM: 创建/更新按正文版本 CAS；同键同正文是重放，异正文才是状态冲突。
# 函数用途: 验证 create/update 的版本引用与冲突分支。
def test_create_update_cas_by_version_ref(v13_native):
    server, agent = v13_native
    home = _user_paths(agent).owner_home_dir
    created = _body('cas-skill')
    status, body = v13_call(server, 'skills.create', {'skill_id': 'cas-skill', 'body_markdown': created},
                            user=USER, idempotency_key='create-' + uuid4().hex)
    assert status == 200, body
    version = body['data']['skill']['version_ref']
    assert (home / 'skills' / 'cas-skill' / 'SKILL.md').read_text(encoding='utf-8') == created

    status, body = v13_call(server, 'skills.create', {'skill_id': 'cas-skill', 'body_markdown': created},
                            user=USER, idempotency_key='create-' + uuid4().hex)
    assert status == 200, body
    assert body['data']['skill']['version_ref'] == version

    status, body = v13_call(server, 'skills.create',
                            {'skill_id': 'cas-skill', 'body_markdown': _body('cas-skill', '换了正文')},
                            user=USER, idempotency_key='create-' + uuid4().hex)
    assert status == 409, body
    assert body['error']['code'] == 'STATE_CONFLICT'

    status, body = v13_call(server, 'skills.update',
                            {'skill_id': 'cas-skill', 'body_markdown': _body('cas-skill', '新版本'),
                             'expected_version_ref': '0' * 64},
                            user=USER, idempotency_key='update-' + uuid4().hex)
    assert status == 409, body
    assert body['error']['code'] == 'REVISION_CONFLICT'

    status, body = v13_call(server, 'skills.update',
                            {'skill_id': 'cas-skill', 'body_markdown': _body('cas-skill', '新版本'),
                             'expected_version_ref': version},
                            user=USER, idempotency_key='update-' + uuid4().hex)
    assert status == 200, body
    assert body['data']['skill']['version_ref'] != version
    assert body['data']['skill']['description'] == '新版本'

    _write_skill(home, 'workspace', 'readonly-skill', _body('readonly-skill'))
    status, body = v13_call(server, 'skills.update',
                            {'skill_id': 'readonly-skill', 'body_markdown': _body('readonly-skill'),
                             'expected_version_ref': '0' * 64},
                            user=USER, idempotency_key='update-' + uuid4().hex)
    assert status == 409, body
    assert body['error']['code'] == 'STATE_CONFLICT'


# LLM: 启停只改原生侧元数据，重复启用不重复写，非用户层不允许切换。
# 函数用途: 验证 activate/retire 的幂等与层级拒绝。
def test_activate_retire_toggle(v13_native):
    server, agent = v13_native
    home = _user_paths(agent).owner_home_dir
    status, body = v13_call(server, 'skills.create',
                            {'skill_id': 'toggle-skill', 'body_markdown': _body('toggle-skill')},
                            user=USER, idempotency_key='create-' + uuid4().hex)
    assert status == 200, body
    status, body = v13_call(server, 'skills.retire', {'skill_id': 'toggle-skill'},
                            user=USER, idempotency_key='retire-' + uuid4().hex)
    assert status == 200, body
    assert body['data']['skill']['enabled'] is False
    status, body = v13_call(server, 'skills.retire', {'skill_id': 'toggle-skill'},
                            user=USER, idempotency_key='retire-' + uuid4().hex)
    assert status == 200, body
    assert body['data']['skill']['enabled'] is False
    status, body = v13_call(server, 'skills.activate', {'skill_id': 'toggle-skill'},
                            user=USER, idempotency_key='activate-' + uuid4().hex)
    assert status == 200, body
    assert body['data']['skill']['enabled'] is True
    meta = json.loads((home / 'skills' / 'toggle-skill' / 'skill_meta.json').read_text(encoding='utf-8'))
    assert meta['enabled'] is True

    _write_skill(home, 'workspace', 'fixed-skill', _body('fixed-skill'))
    status, body = v13_call(server, 'skills.retire', {'skill_id': 'fixed-skill'},
                            user=USER, idempotency_key='retire-' + uuid4().hex)
    assert status == 409, body
    assert body['error']['code'] == 'STATE_CONFLICT'


# LLM: 经验候选与 Skill 提案都按原生 schema 落盘，避免测试自造候选账本形状。
# 函数用途: 在隔离 owner home 中种入一条 lesson 候选和一条待确认 Skill 提案。
def _seed_lesson(agent, content='经验：演示技能'):
    from dataclasses import replace

    from agent_py_agent.agent.capability.skill_proposals import (
        SkillProposal,
        SkillProposalDraft,
        SkillProposalSource,
        SkillProposalTarget,
        lesson_skill_name,
        render_skill_markdown,
        skill_proposal_id,
    )
    from agent_py_agent.agent.capability.skill_snapshot import skill_content_sha256
    from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation
    from agent_py_agent.agent.memory_store.candidates import CandidateService

    paths = _user_paths(agent)
    service = CandidateService(Path(paths.owner_memory_candidates_jsonl))
    candidate = service.observe(CandidateObservation(
        candidate_type='lesson', content=content, subject_key='demo-subject',
        scope={'scope_type': 'global', 'scope_key': 'global'}, origin='subagent_lesson',
        promotion_target='lesson', source_task_ids=('task-demo',), source_run_ids=('run-demo',)))
    skill_name = lesson_skill_name(candidate.content_hash)
    target = SkillProposalTarget(skill_name=skill_name)
    draft = SkillProposalDraft(description=content, when_to_use='演示时使用', body='演示正文', sha256='0' * 64)
    draft = replace(draft, sha256=skill_content_sha256(render_skill_markdown(skill_name, draft)))
    proposal_id = skill_proposal_id(candidate.candidate_id, candidate.content_hash)
    stamp = '2026-11-02T00:00:00+00:00'
    proposal = SkillProposal(proposal_id, 1, 'pending_confirmation',
                             SkillProposalSource(candidate.candidate_id, candidate.content_hash,
                                                 ('task-demo',), ('run-demo',)),
                             target, draft, stamp, stamp)
    path = Path(paths.owner_skill_proposals_dir) / (proposal_id + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(proposal.to_record()), encoding='utf-8')
    return candidate, proposal


# LLM: 逐项状态来自原生候选/提案记录，不按标题或时间猜。
# 函数用途: 验证候选列表投影、状态映射与分页。
def test_candidates_list_example_params(v13_native):
    server, agent = v13_native
    _candidate, proposal = _seed_lesson(agent)
    status, body = v13_call(server, 'learning.candidates.list',
                            {'space_id': 'soc-default', 'status': 'pending'}, user=USER)
    assert status == 200, body
    assert body['state'] == 'completed' and body['error'] is None
    rows = {row['candidate_id']: row for row in body['data']['items']}
    assert rows[proposal.proposal_id]['kind'] == 'skill'
    assert rows[proposal.proposal_id]['status'] == 'pending'
    status, body = v13_call(server, 'learning.candidates.list', {'kind': 'memory'}, user=USER)
    assert status == 200, body
    assert all(row['kind'] == 'memory' for row in body['data']['items'])
    assert body['data']['items'], '种子候选必须出现在原生列表里'


# LLM: 预览返回用户将看到的全文与目标版本，不返回摘要代替完整版本。
# 函数用途: 验证 Skill 提案预览。
def test_preview_returns_full_version(v13_native):
    server, agent = v13_native
    _candidate, proposal = _seed_lesson(agent)
    status, body = v13_call(server, 'learning.candidates.preview', {'candidate_id': proposal.proposal_id},
                            user=USER, idempotency_key='preview-' + uuid4().hex)
    assert status == 200, body
    data = body['data']
    assert data['preview_id'].startswith('spv1-')
    assert data['target']['skill_name'] == proposal.target.skill_name
    assert '演示正文' in data['full_preview']
    assert len(data['version_ref']) == 64 and data['expires_at']


# LLM: 确认必须同时绑定 owner/actor/thread 和目标版本，任一项不符都不得提交。
# 函数用途: 验证 Skill 提案确认的正反例与终态。
def test_preview_confirm_bound_to_owner_actor_thread_and_cas(v13_native):
    server, agent = v13_native
    _candidate, proposal = _seed_lesson(agent)
    status, body = v13_call(server, 'learning.candidates.preview', {'candidate_id': proposal.proposal_id},
                            user=USER, idempotency_key='preview-' + uuid4().hex)
    assert status == 200, body
    preview_id, version = body['data']['preview_id'], body['data']['version_ref']

    status, body = v13_call(server, 'learning.candidates.confirm',
                            {'preview_id': preview_id, 'expected_version_ref': '0' * 64},
                            user=USER, idempotency_key='confirm-' + uuid4().hex)
    assert status == 409, body
    assert body['error']['code'] == 'REVISION_CONFLICT'

    status, body = v13_call(server, 'learning.candidates.confirm',
                            {'preview_id': preview_id, 'expected_version_ref': version},
                            user=OTHER, idempotency_key='confirm-' + uuid4().hex)
    assert status == 403, body
    assert body['error']['code'] == 'BUSINESS_ACCESS_DENIED'

    status, body = v13_call(server, 'learning.candidates.preview',
                            {'candidate_id': proposal.proposal_id, 'space_id': 'soc-default'},
                            user=USER, idempotency_key='preview-' + uuid4().hex)
    assert status == 200, body
    status, body = v13_call(server, 'learning.candidates.confirm',
                            {'preview_id': body['data']['preview_id'],
                             'expected_version_ref': body['data']['version_ref']},
                            user=USER, idempotency_key='confirm-' + uuid4().hex)
    assert status == 403, body
    assert body['error']['code'] == 'BUSINESS_ACCESS_DENIED'

    status, body = v13_call(server, 'learning.candidates.confirm',
                            {'preview_id': preview_id, 'expected_version_ref': version},
                            user=USER, idempotency_key='confirm-' + uuid4().hex)
    assert status == 200, body
    assert body['data']['candidate']['status'] == 'confirmed'
    installed = _user_paths(agent).owner_home_dir / 'skills' / proposal.target.skill_name / 'SKILL.md'
    assert installed.is_file()

    status, body = v13_call(server, 'learning.candidates.confirm',
                            {'preview_id': preview_id, 'expected_version_ref': version},
                            user=USER, idempotency_key='confirm-' + uuid4().hex)
    assert status == 200, body
    assert body['data']['candidate']['status'] == 'confirmed'


# LLM: 拒绝是终态；之后同一预览不能再被确认为已发布。
# 函数用途: 验证 Skill 提案拒绝后的最终态。
def test_reject_is_final(v13_native):
    server, agent = v13_native
    _candidate, proposal = _seed_lesson(agent, content='经验：第二条')
    status, body = v13_call(server, 'learning.candidates.preview', {'candidate_id': proposal.proposal_id},
                            user=USER, idempotency_key='preview-' + uuid4().hex)
    assert status == 200, body
    preview_id, version = body['data']['preview_id'], body['data']['version_ref']
    status, body = v13_call(server, 'learning.candidates.reject', {'preview_id': preview_id},
                            user=USER, idempotency_key='reject-' + uuid4().hex)
    assert status == 200, body
    assert body['data']['candidate']['status'] == 'rejected'
    status, body = v13_call(server, 'learning.candidates.reject', {'preview_id': preview_id},
                            user=USER, idempotency_key='reject-' + uuid4().hex)
    assert status == 200, body
    assert body['data']['candidate']['status'] == 'rejected'
    status, body = v13_call(server, 'learning.candidates.confirm',
                            {'preview_id': preview_id, 'expected_version_ref': version},
                            user=USER, idempotency_key='confirm-' + uuid4().hex)
    assert status == 409, body
    assert body['error']['code'] == 'STATE_CONFLICT'
    assert not (_user_paths(agent).owner_home_dir / 'skills' / proposal.target.skill_name).exists()


# LLM: 记忆候选复用原生 /memory candidates 回执与绑定判据。
# 函数用途: 验证记忆候选预览与确认的 actor/CAS 反例。
def test_memory_candidate_preview_confirm_binding(v13_native):
    server, agent = v13_native
    candidate, _proposal = _seed_lesson(agent, content='经验：记忆候选')
    status, body = v13_call(server, 'learning.candidates.preview',
                            {'candidate_id': candidate.candidate_id},
                            user=USER, idempotency_key='preview-' + uuid4().hex)
    assert status == 200, body
    preview_id, version = body['data']['preview_id'], body['data']['version_ref']
    assert preview_id.startswith('gwctl-msg-')
    assert body['data']['full_preview'] and body['data']['target']['kind'] == 'memory_candidate'
    # LLM: 轻量宿主没有可与 receipt 账配对的完整候选宿主；未知 nonce 必须在提交前被拒绝。
    # 函数用途: 验证记忆候选确认入口不接受未由本人预览签发的 nonce。
    status, body = v13_call(server, 'learning.candidates.confirm',
                            {'preview_id': 'gwctl-msg-' + '0' * 32,
                             'expected_version_ref': version},
                            user=USER, idempotency_key='confirm-' + uuid4().hex)
    assert status in (403, 404), body
    assert body['error']['code'] in ('BUSINESS_ACCESS_DENIED', 'RESOURCE_NOT_FOUND')


# LLM: 作业结果只接受结构化 JSON，缺字段必须失败而不是补默认值。
# 函数用途: 验证回测收口与真实 202 受理。
def test_backtest_job_kind_skill_backtest(v13_native):
    from agent_py_agent.agent.business_spaces.v13_w11_skills_learning import finalize_backtest

    parsed = finalize_backtest('```json\n{"skill_id": "bt-skill", "cases_run": 2, "passed": 1, '
                               '"failed": 1, "findings": ["边界用例失败"]}\n```')
    assert parsed == {'skill_id': 'bt-skill', 'cases_run': 2, 'passed': 1, 'failed': 1,
                      'findings': ['边界用例失败']}
    with pytest.raises(ValueError):
        finalize_backtest('{"skill_id": "bt-skill"}')

    server, _agent = v13_native
    space_id = v13_space(server)
    status, body = v13_call(server, 'skills.create',
                            {'skill_id': 'bt-skill', 'body_markdown': _body('bt-skill')},
                            user=USER, idempotency_key='create-' + uuid4().hex)
    assert status == 200, body
    status, body = v13_call(server, 'skills.backtest', {'skill_id': 'bt-skill', 'space_id': space_id},
                            user=USER, idempotency_key='backtest-' + uuid4().hex)
    # LLM: 这里必须断言真实 202 受理；W0 派发依赖缺陷造成的失败只记录，不放宽成 skip 或改断言。
    # 函数用途: 验证 skills.backtest 经 W0 派发出口拿到 accepted 回执。
    assert status == 202, ('W0 v13_start_model_job 强制 tasks.assign DomainResult(202)，'
                           '真实原生 200/receipt accepted 时该桥接返回 503: ' + json.dumps(
                               body.get('error'), ensure_ascii=False))
    assert body['state'] == 'accepted' and body['error'] is None
    assert body['data']['job_id'] and body['data']['task_id']


# LLM: 作业种类与空间解析由本模块唯一出口交给 W0 派发，不在这里重写派发或任务账。
# 函数用途: 验证回测向 W0 派发出口传入的 kind、指令与真实空间。
def test_backtest_dispatches_skill_backtest_kind(v13_native, monkeypatch):
    from agent_py_agent.agent.business_spaces import v13_w11_skills_learning as module
    from agent_py_agent.agent.gateway_parts.business_api_v13 import DomainResult

    captured = {}

    def fake_start(call, kind, instruction, *, space_id=None):
        captured.update(kind=kind, instruction=instruction, space_id=space_id)
        return DomainResult(202, {'state': 'accepted', 'operation_id': 'op-1',
                                  'job_id': 'job-1', 'task_id': 'task-1'})

    monkeypatch.setattr(module, 'v13_start_model_job', fake_start)
    server, _agent = v13_native
    space_id = v13_space(server)
    status, body = v13_call(server, 'skills.create',
                            {'skill_id': 'bt-kind', 'body_markdown': _body('bt-kind')},
                            user=USER, idempotency_key='create-' + uuid4().hex)
    assert status == 200, body
    status, body = v13_call(server, 'skills.backtest',
                            {'skill_id': 'bt-kind', 'space_id': space_id},
                            user=USER, idempotency_key='backtest-' + uuid4().hex)
    assert status == 202, body
    assert captured['kind'] == 'skill_backtest'
    assert captured['space_id'] == space_id
    assert 'bt-kind' in captured['instruction'] and '严格 JSON' in captured['instruction']


# LLM: 同键异参必须拒绝，重放旧启用回执不可改变后来停用的真实开关。
# 函数用途: 验证技能写动作完整参数绑定与耐久成功重放。
def test_skill_mutations_bind_idempotency_and_replay_without_effect(v13_native):
    server, agent = v13_native
    params = {'skill_id': 'idem-a', 'body_markdown': _body('idem-a')}
    key = 'w11-skill-idem-create'
    status, first = v13_call(server, 'skills.create', params, user=USER, idempotency_key=key)
    assert status == 200, first
    status, conflict = v13_call(server, 'skills.create',
        {'skill_id': 'idem-b', 'body_markdown': _body('idem-b')}, user=USER, idempotency_key=key)
    assert status == 409 and conflict['error']['code'] == 'IDEMPOTENCY_CONFLICT', conflict
    assert not (_user_paths(agent).owner_home_dir / 'skills' / 'idem-b').exists()
    toggle = {'skill_id': 'idem-a'}
    status, activated = v13_call(server, 'skills.activate', toggle, user=USER,
                                 idempotency_key='w11-skill-idem-activate')
    assert status == 200, activated
    status, retired = v13_call(server, 'skills.retire', toggle, user=USER,
                               idempotency_key='w11-skill-idem-retire')
    assert status == 200 and retired['data']['skill']['enabled'] is False, retired
    status, replay = v13_call(server, 'skills.activate', toggle, user=USER,
                              idempotency_key='w11-skill-idem-activate')
    assert status == 200 and replay['data'] == activated['data'], replay
    status, actual = v13_call(server, 'skills.get', toggle, user=USER)
    assert status == 200 and actual['data']['skill']['enabled'] is False, actual


# LLM: 两个线程同版本更新只能一个提交；屏障只协调测试启动，不替换资产锁或CAS。
# 函数用途: 验证并发技能更新不丢失版本检查。
def test_skill_concurrent_updates_one_cas_winner(v13_native):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    server, _agent = v13_native
    params = {'skill_id': 'race-skill', 'body_markdown': _body('race-skill')}
    status, created = v13_call(server, 'skills.create', params, user=USER,
                               idempotency_key='w11-skill-race-create')
    assert status == 200, created
    version = created['data']['skill']['version_ref']
    ready = Barrier(2)

    # LLM: 每个输入都是真实HTTP请求，同一旧版本、不同正文和幂等键。
    # 函数用途: 同步发起一个待竞争的版本更新。
    def update(index):
        ready.wait(timeout=10)
        return v13_call(server, 'skills.update', {'skill_id': 'race-skill',
            'body_markdown': _body('race-skill', '并发正文' + str(index)),
            'expected_version_ref': version}, user=USER,
            idempotency_key='w11-skill-race-update-' + str(index))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, (1, 2)))
    assert sorted(status for status, _body_result in results) == [200, 409], results
    rejected = next(body for status, body in results if status == 409)
    assert rejected['error']['code'] == 'REVISION_CONFLICT', rejected


# LLM: 候选和消息由原请求owner解析器创建；预览和提交必须穿过真实HTTP及原control耐久回执。
# 函数用途: 验证记忆候选完整预览后确认或拒绝的正向链路和稳定重放。
@pytest.mark.parametrize('decision', ['confirm', 'reject'])
def test_memory_candidate_real_preview_then_decision(v13_native, decision):
    from types import SimpleNamespace

    from agent_py_agent.agent.capability.memory_tool import RememberTool
    from agent_py_agent.agent.conversation.learning_source import dispatch_projection
    from agent_py_agent.agent.gateway_parts.request_worker import (
        _resolve_request_agent_for_owner,
    )
    from agent_py_agent.agent.memory_store.candidates import CandidateService
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    server, agent = v13_native
    paths = _user_paths(agent)
    owner_agent = _resolve_request_agent_for_owner(agent, OwnerIdentity.provider_user('feishu', USER))
    assert owner_agent.home_paths.owner_id == paths.owner_id
    thread = owner_agent.conversation_store.threads.get_or_create({
        'canonical_user_id': USER, 'owner_id': paths.owner_id, 'channel': 'feishu',
        'channel_conversation_id': 'w11-memory-source-' + decision, 'channel_user_id': USER,
        'learning_source': dispatch_projection()})
    source_id = 'w11-memory-source-message-' + decision
    owner_agent.conversation_store.messages.append({'thread_id': thread.thread_id,
        'role': 'user', 'content': 'W11人工审核的演示项目事实。',
        'metadata': {'gateway_request_id': source_id}})
    owner_agent._current_run_params = SimpleNamespace(request_id=source_id,
        run_id='w11-memory-source-run-' + decision, task_id='w11-memory-source-task-' + decision,
        task_attributes={'conversation_thread_id': thread.thread_id})
    draft = RememberTool(owner_agent).execute({'content': 'W11人工审核的演示项目事实。',
        'kind': 'project', 'origin': 'user_explicit', 'subject_key': 'w11-review-' + decision})
    assert draft.ok and not json.loads(draft.output)['saved'], draft
    service = CandidateService(Path(paths.owner_memory_candidates_jsonl))
    candidate = service.list()[0]
    assert candidate.status == 'pending_review' and owner_agent.memory.all() == []
    preview_key = 'memory-real-preview-' + decision
    status, body = v13_call(server, 'learning.candidates.preview',
        {'candidate_id': candidate.candidate_id}, user=USER, idempotency_key=preview_key)
    assert status == 200, json.dumps(body, ensure_ascii=False)
    preview = body['data']
    assert candidate.content in preview['full_preview']
    replay_status, replay = v13_call(server, 'learning.candidates.preview',
        {'candidate_id': candidate.candidate_id}, user=USER, idempotency_key=preview_key)
    assert replay_status == 200 and replay['data'] == preview, replay
    params = {'preview_id': preview['preview_id']}
    if decision == 'confirm':
        params['expected_version_ref'] = preview['version_ref']
    status, body = v13_call(server, 'learning.candidates.' + decision, params,
        user=USER, idempotency_key='memory-real-decision-' + decision)
    assert status == 200, json.dumps(body, ensure_ascii=False)
    assert body['data']['candidate']['status'] == ('confirmed' if decision == 'confirm' else 'rejected')
    final = service.get(candidate.candidate_id)
    assert final.status == ('promoted' if decision == 'confirm' else 'rejected')
    if decision == 'confirm':
        assert final.promotion_ref and body['data']['receipt']['state'] == 'completed'
        assert [row.content for row in owner_agent.memory.all()] == [candidate.content]
    else:
        assert owner_agent.memory.all() == []
    status, replay = v13_call(server, 'learning.candidates.' + decision, params,
        user=USER, idempotency_key='memory-real-decision-' + decision)
    assert status == 200 and replay['data'] == body['data'], replay


# ============================================================================
# Source: agent_py_agent/tests/test_v13_w12_shifts_analytics.py
# ============================================================================

# LLM: 夹具只创建任务临时目录，不触碰真实 home。
# 函数用途: 创建可控制时钟与原生权限的真实宿主。
@pytest.fixture
def host(tmp_path, monkeypatch):
    root = tmp_path / 'host'
    monkeypatch.setenv('MY_AGENT_HOME', str(root))
    monkeypatch.setenv('HOME', str(tmp_path / 'os-home'))
    clock = [1793664000.0]
    return SimpleNamespace(home_paths=home_paths_with_owner(home_paths(root), resolve_owner_home(root)),
        config=AgentConfig(business_spaces_enabled=True),
        business_v13_clock=lambda: clock[0], clock=clock, business_v13_providers={}, business_native_admin=lambda: True)


# LLM: 幂等键全部为合成值，满足真实运输校验。
# 函数用途: 生成测试写入键。
def idem():
    return 'w12-assets-' + uuid4().hex


# LLM: 身份来自宿主 bind_context，不能从请求 JSON 注入 owner。
# 函数用途: 通过真实统一 dispatcher 执行业务动作。
def _w12_invoke(host, action, params=None, *, key=None, revision=None, subject='alice', authorize=None):
    context = api.bind_business_context(host, subject_id=subject,
        authorize=authorize if authorize is not None else lambda action, facts: True)
    request = {'action': action, 'request_id': uuid4().hex, 'params': {} if params is None else params}
    if key is not None:
        request['idempotency_key'] = key
    if revision is not None:
        request['expected_revision'] = revision
    return api.execute_business(request, context, api.BusinessServices(host))


# LLM: 成功断言校验实际信封，不手写预期响应代替产品调用。
# 函数用途: 取得 200 completed 数据。
def ok(host, action, params=None, *, key=None, revision=None, subject='alice'):
    status, body = _w12_invoke(host, action, params, key=key, revision=revision, subject=subject)
    assert status == 200, body
    assert body['state'] == 'completed' and body['error'] is None, body
    return body['data']


# LLM: 失败必须保留精确状态码与失败信封。
# 函数用途: 核对真实错误输出。
def failed(result, status, code):
    actual, body = result
    assert actual == status, body
    assert body['state'] == 'failed' and body['data'] == {}, body
    assert body['error']['code'] == code, body


# LLM: 测试日期明确指定 UTC，不依赖运行机器本地时区。
# 函数用途: 把测试时间转成 epoch 秒。
def epoch(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


# LLM: 记录时间由同一可注入时钟生成。
# 函数用途: 构造规范 UTC 测试时间。
def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


# LLM: 用真实已签发上下文调用公开 helper，不伪造上下文。
# 函数用途: 构造本 owner 的 V13Call。
def call_for(host, action='shifts.schedule.get', params=None):
    context = api.bind_business_context(host, subject_id='alice', authorize=lambda action, facts: True)
    return foundation.V13Call(action, {} if params is None else params, 'helper-test', host.home_paths.owner_id,
        'alice', context, api.BusinessServices(host), host.home_paths.owner_home_dir, idem(), None, host.business_v13_clock)


# LLM: 班次 days 对应开始日，数据固定但不依赖当前真实日期。
# 函数用途: 构造精确 ShiftSchema 的测试班次。
def shift():
    return {'shift_id': 'night', 'name': '夜班', 'start': '22:00', 'end': '06:00', 'days': ['mon'],
        'members': ['alice', 'bob'], 'lead': 'alice'}


# LLM: 只通过原生写动作设置班表，不手写底层文件。
# 函数用途: 设置一个合法的首版班表。
def schedule(host):
    return ok(host, 'shifts.schedule.set', {'timezone': 'America/Los_Angeles', 'shifts': [shift()]},
        key=idem(), revision=0)['schedule']


# LLM: 种子数据沿 Store 事务；集合文件、回执和审计形状由底座生成。
# 函数用途: 写入独立测试的规范集合记录。
def seed(host, collection, rows):
    store = V13Store(host.home_paths.owner_home_dir, owner_id=host.home_paths.owner_id)

    # LLM: 测试修订和元数据仍显式维护，不假设 Store 自动补齐。
    # 函数用途: 把测试提供的记录合入真实集合。
    def mutate(records):
        records.update(deepcopy(rows))
        return {'count': len(rows)}

    return store.transaction(collection, mutate, idempotency_key=idem(), params_digest=idem())


# LLM: 测试案件使用契约完整形状，sample 通过结构化 tags 标注。
# 函数用途: 构造真实格式的 W8 case。
def case(host, case_id='case-one', *, status='open', assignee='alice', tags=None):
    now = host.clock[0]
    return {'case_id': case_id, 'owner_id': host.home_paths.owner_id, 'space_id': 'space-test',
        'title': '待交接案件', 'description': '', 'severity': 'high', 'status': status, 'assignee': assignee,
        'alert_ids': [], 'merged_into': None, 'tags': [] if tags is None else tags, 'custom_values': {},
        'closed_at': stamp(now - 1000) if status in {'closed', 'resolved'} else None,
        'revision': 1, 'created_at': stamp(now - 5000), 'updated_at': stamp(now)}


# LLM: 缺省班表和跨夜、起始日、时区都要从同一个真实读入口观察。
# 函数用途: 验证排班设置、读取及供 oncall 复用的公开 helper。
def test_schedule_set_get_current_shift_overnight_and_timezone(host):
    assert ok(host, 'shifts.schedule.get') == {'schedule': None,
        'current': {'shift_id': None, 'on_duty': [], 'lead': None}}
    first = schedule(host)
    assert first['revision'] == 1
    host.clock[0] = epoch('2026-11-03T07:00:00Z')
    result = ok(host, 'shifts.schedule.get')
    assert result['current'] == {'shift_id': 'night', 'on_duty': ['alice', 'bob'], 'lead': 'alice'}
    assert module.current_shift(call_for(host)) == result['current']
    host.clock[0] = epoch('2026-11-03T13:59:59Z')
    assert ok(host, 'shifts.schedule.get')['current']['shift_id'] == 'night'
    host.clock[0] = epoch('2026-11-03T14:00:00Z')
    assert ok(host, 'shifts.schedule.get')['current']['shift_id'] is None
    host.clock[0] = epoch('2026-11-04T07:00:00Z')
    assert module.current_shift(call_for(host))['on_duty'] == []
    # end == start 代表起始日开始的跨日整班，而不是永远空班。
    full = shift() | {'start': '09:00', 'end': '09:00'}
    ok(host, 'shifts.schedule.set', {'timezone': 'UTC', 'shifts': [full]}, key=idem(), revision=1)
    host.clock[0] = epoch('2026-11-03T08:59:59Z')
    assert module.current_shift(call_for(host))['shift_id'] == 'night'
    host.clock[0] = epoch('2026-11-03T09:00:00Z')
    assert module.current_shift(call_for(host))['shift_id'] is None


# LLM: ZoneInfo 的夏令时回拨不能用固定 UTC 偏移替代。
# 函数用途: 验证 DST 回拨当天两个真实瞬间都属于同一班次。
def test_schedule_dst_and_creation_day(host):
    row = shift() | {'start': '01:30', 'end': '03:30', 'days': ['sun']}
    ok(host, 'shifts.schedule.set', {'timezone': 'America/New_York', 'shifts': [row]}, key=idem(), revision=0)
    for at in ('2026-11-01T05:45:00Z', '2026-11-01T06:45:00Z', '2026-11-01T08:29:59Z'):
        host.clock[0] = epoch(at)
        assert module.current_shift(call_for(host))['shift_id'] == 'night'
    host.clock[0] = epoch('2026-11-01T08:30:00Z')
    assert module.current_shift(call_for(host))['shift_id'] is None


# LLM: 重复小时的边界沿 ZoneInfo 默认首次出现，半开班次不会在第二次小时重新开启。
# 函数用途: 明确结束点落入回拨小时的排班语义。
def test_schedule_dst_repeated_end_uses_first_occurrence(host):
    row = shift() | {'start': '00:30', 'end': '01:30', 'days': ['sun']}
    ok(host, 'shifts.schedule.set', {'timezone': 'America/New_York', 'shifts': [row]}, key=idem(), revision=0)
    host.clock[0] = epoch('2026-11-01T05:15:00Z')
    assert module.current_shift(call_for(host))['shift_id'] == 'night'
    for at in ('2026-11-01T05:30:00Z', '2026-11-01T06:15:00Z'):
        host.clock[0] = epoch(at)
        assert module.current_shift(call_for(host))['shift_id'] is None


# LLM: 失败写入不能改变原班表，CAS 与幂等冲突保留各自精确错误。
# 函数用途: 验证时区、重复 ID、管理员、首版 CAS、闭合参数及未知结果负例。
def test_schedule_negative_cases(host, monkeypatch):
    params = {'timezone': 'UTC', 'shifts': [shift()]}
    failed(_w12_invoke(host, 'shifts.schedule.set', params, key=idem(), revision=1), 409, 'REVISION_CONFLICT')
    failed(_w12_invoke(host, 'shifts.schedule.set', params, key=idem()), 400, 'INVALID_SCHEMA')
    failed(_w12_invoke(host, 'shifts.schedule.set', params | {'timezone': 'No/SuchZone'}, key=idem(), revision=0),
        400, 'INVALID_SCHEMA')
    failed(_w12_invoke(host, 'shifts.schedule.set', params | {'shifts': [shift(), shift()]}, key=idem(), revision=0),
        400, 'INVALID_SCHEMA')
    host.business_native_admin = lambda: False
    failed(_w12_invoke(host, 'shifts.schedule.set', params, key=idem(), revision=0), 403, 'BUSINESS_ACCESS_DENIED')
    host.business_native_admin = lambda: True
    key = idem()
    first = ok(host, 'shifts.schedule.set', params, key=key, revision=0)
    assert ok(host, 'shifts.schedule.set', params, key=key, revision=0) == first
    failed(_w12_invoke(host, 'shifts.schedule.set', params | {'timezone': 'Europe/London'}, key=key, revision=0),
        409, 'IDEMPOTENCY_CONFLICT')
    failed(_w12_invoke(host, 'shifts.schedule.set', params, key=idem(), revision=0), 409, 'REVISION_CONFLICT')
    failed(_w12_invoke(host, 'shifts.schedule.get', {'extra': True}), 400, 'INVALID_SCHEMA')

    # LLM: 模拟真实写入口抛错，检查外层必须报告未知提交而不是假成功。
    # 函数用途: 为错误语义提供故障注入。
    def unavailable(self, collection, mutate, *, idempotency_key, params_digest, expected_revision=None, receipt_view=None):
        raise OSError('synthetic transaction failure')

    monkeypatch.setattr(V13Store, 'transaction', unavailable)
    failed(_w12_invoke(host, 'shifts.schedule.set', params, key=idem(), revision=1), 503, 'OUTCOME_UNKNOWN')


# LLM: 快照必须保持创建时值，并在真实列表入口最新在前。
# 函数用途: 验证交接创建、不可变 case 快照、幂等及分页负例。
def test_handoffs_create_snapshot_and_list_newest_first(host):
    schedule(host)
    seed(host, 'soc_cases', {'open': case(host), 'investigating': case(host, 'case-two', status='investigating'),
        'closed': case(host, 'case-closed', status='closed')})
    params = {'shift_id': 'night', 'summary': '请复核现有证据', 'to_subject': 'bob', 'open_alert_ids': ['alert-one']}
    key = idem()
    first = ok(host, 'shifts.handoffs.create', params, key=key)['handoff']
    assert first['from_subject'] == 'alice' and first['to_subject'] == 'bob'
    assert {row['case_id'] for row in first['open_cases']} == {'case-one', 'case-two'}
    assert ok(host, 'shifts.handoffs.create', params, key=key)['handoff'] == first
    failed(_w12_invoke(host, 'shifts.handoffs.create', params | {'summary': 'different'}, key=key),
        409, 'IDEMPOTENCY_CONFLICT')
    seed(host, 'soc_cases', {'open': case(host) | {'title': '新标题'}})
    host.clock[0] += 1
    second = ok(host, 'shifts.handoffs.create', params | {'include_open_cases': False}, key=idem())['handoff']
    assert second['open_cases'] == []
    listing = ok(host, 'shifts.handoffs.list', {'shift_id': 'night', 'limit': 1})
    assert listing['items'][0]['handoff_id'] == second['handoff_id'] and listing['coverage'] == 'partial'
    cursor = listing['next_cursor']
    older = ok(host, 'shifts.handoffs.list', {'shift_id': 'night', 'limit': 1, 'cursor': cursor})
    assert older['items'] == [first] and older['next_cursor'] is None
    assert older['items'][0]['open_cases'][0]['title'] == '待交接案件'
    failed(_w12_invoke(host, 'shifts.handoffs.list', {'shift_id': 'day', 'limit': 1, 'cursor': cursor}),
        410, 'CURSOR_EXPIRED')
    failed(_w12_invoke(host, 'shifts.handoffs.list', {'shift_id': 'night', 'limit': 1, 'cursor': cursor + 'x'}),
        410, 'CURSOR_EXPIRED')
    failed(_w12_invoke(host, 'shifts.handoffs.create', {'shift_id': 'missing', 'summary': ''}, key=idem()),
        404, 'RESOURCE_NOT_FOUND')


# LLM: 交接只访问同 owner；不把另一 owner 的 case 带入快照。
# 函数用途: 验证伪归属记录与未设置班次的 404。
def test_handoffs_and_schedule_reject_other_owner_records(host):
    failed(_w12_invoke(host, 'shifts.handoffs.create', {'shift_id': 'night', 'summary': ''}, key=idem()),
        404, 'RESOURCE_NOT_FOUND')
    schedule(host)
    seed(host, 'soc_cases', {'foreign': case(host) | {'owner_id': 'local/other'}})
    failed(_w12_invoke(host, 'shifts.handoffs.create', {'shift_id': 'night', 'summary': ''}, key=idem()),
        404, 'RESOURCE_NOT_FOUND')
    assert ok(host, 'shifts.handoffs.list')['items'] == []
    seed(host, 'shift_schedule', {host.home_paths.owner_id: {'owner_id': 'local/other'}})
    failed(_w12_invoke(host, 'shifts.schedule.get'), 404, 'RESOURCE_NOT_FOUND')


# LLM: 所有警报字段来自精确 W8 结构，时间可以构造明确窗口与持续时间。
# 函数用途: 生成团队效能测试警报。
def alert(host, alert_id, analyst, duration, *, sample=False, verdict='true_positive'):
    now = host.clock[0]
    return {'alert_id': alert_id, 'space_id': 'space-test', 'title': '测试警报', 'description': '',
        'severity': 'high', 'status': 'resolved', 'verdict': verdict, 'ai_confidence': 0.9,
        'disposition': None, 'rule_id': None, 'source': 'sample' if sample else 'monitor',
        'external_ref': None, 'entities': [], 'techniques': [], 'assignee': analyst, 'case_id': None,
        'tags': [], 'evidence_refs': [], 'acknowledged_at': stamp(now - duration - 90),
        'resolved_at': stamp(now - 100), 'revision': 1, 'created_at': stamp(now - duration - 100),
        'updated_at': stamp(now - 100), 'owner_id': host.home_paths.owner_id}


# LLM: 反馈原因是展示内容，分支只看 override_verdict 和 created_at。
# 函数用途: 构造明确时间顺序的人工反馈。
def feedback(host, override_id, alert_id, verdict, age):
    return {'override_id': override_id, 'alert_id': alert_id, 'space_id': 'space-test', 'rule_id': None,
        'original_verdict': 'true_positive', 'override_verdict': verdict, 'reason': '人工复核',
        'analyst': 'reviewer', 'source': 'override', 'created_at': stamp(host.clock[0] - age),
        'owner_id': host.home_paths.owner_id, 'revision': 1, 'updated_at': stamp(host.clock[0] - age)}


# LLM: 反馈覆盖、样本排除、SLA 分母及均值都以可独立核算的输入验证。
# 函数用途: 验证按 assignee 与 team 聚合的效能统计。
def test_team_analytics_per_assignee_rates(host):
    seed(host, 'soc_alerts', {'a1': alert(host, 'a1', 'alice', 600),
        'a2': alert(host, 'a2', 'alice', 3600, verdict='false_positive'),
        'b1': alert(host, 'b1', 'bob', 1200, verdict='benign'),
        'sample': alert(host, 'sample', 'alice', 1, sample=True)})
    seed(host, 'soc_cases', {'closed': case(host, status='closed'),
        'sample-case': case(host, 'sample-case', status='closed', tags=['sample'])})
    seed(host, 'soc_feedback', {'f1': feedback(host, 'f1', 'a1', 'true_positive', 80),
        'f2': feedback(host, 'f2', 'a1', 'benign', 60), 'f3': feedback(host, 'f3', 'a2', 'true_positive', 50)})
    seed(host, 'monitor_sla_policies', {'space-test': {'space_id': 'space-test',
        'owner_id': host.home_paths.owner_id, 'policies': [{'severity': 'high', 'ack_minutes': 5, 'resolve_minutes': 30}],
        'revision': 1, 'created_at': stamp(host.clock[0] - 10000), 'updated_at': stamp(host.clock[0])}})
    result = ok(host, 'analytics.team.get')
    assert result['window'] == '7d' and result['coverage'] == 'complete'
    by_name = {row['analyst']: row for row in result['analysts']}
    assert set(by_name) == {'alice', 'bob'}
    assert by_name['alice'] == {'analyst': 'alice', 'alerts_triaged': 2, 'cases_closed': 1,
        'mttr_seconds': 2100, 'false_positive_rate': 0.5, 'sla_met_rate': 0.5}
    assert by_name['bob']['mttr_seconds'] == 1200 and by_name['bob']['false_positive_rate'] == 1
    assert result['team']['alerts_triaged'] == 3 and result['team']['cases_closed'] == 1
    assert result['team']['mttr_seconds'] == 1800
    assert result['team']['false_positive_rate'] == pytest.approx(2 / 3)
    assert result['team']['sla_met_rate'] == pytest.approx(2 / 3)
    empty = ok(host, 'analytics.team.get', {'window': '1m'})
    assert empty['analysts'] == []
    assert empty['team'] == {'analyst': 'team', 'alerts_triaged': 0, 'cases_closed': 0,
        'mttr_seconds': None, 'false_positive_rate': None, 'sla_met_rate': None}
    failed(_w12_invoke(host, 'analytics.team.get', {'window': '0h'}), 400, 'INVALID_SCHEMA')


# LLM: W11 case 可只通过 alert_ids 标注样本，窗口右边界不属于本窗口。
# 函数用途: 验证关联样本排除与 [since,until) 边界。
def test_team_analytics_sample_links_and_half_open_window(host):
    now = host.clock[0]
    start = now - 3600
    sample = alert(host, 'sample-alert', 'sample-analyst', 1, sample=True)
    lower = alert(host, 'lower', 'alice', 600) | {'created_at': stamp(start - 600),
        'acknowledged_at': stamp(start), 'resolved_at': stamp(start)}
    upper = alert(host, 'upper', 'alice', 600) | {'acknowledged_at': stamp(now), 'resolved_at': stamp(now)}
    seed(host, 'soc_alerts', {'sample': sample, 'lower': lower, 'upper': upper})
    seed(host, 'soc_cases', {'linked': case(host, 'linked', status='closed') | {'alert_ids': ['sample-alert']},
        'upper-case': case(host, 'upper-case', status='closed') | {'closed_at': stamp(now)}})
    seed(host, 'soc_feedback', {'sample-feedback': feedback(host, 'sample-feedback', 'sample-alert', 'benign', 10)})
    result = ok(host, 'analytics.team.get', {'window': '1h'})
    assert result['coverage'] == 'complete'
    assert result['team']['alerts_triaged'] == 1 and result['team']['cases_closed'] == 0
    assert result['team']['mttr_seconds'] == 600 and result['team']['false_positive_rate'] == 0
    assert [row['analyst'] for row in result['analysts']] == ['alice']


# LLM: 异常和重复记录先剔除，不能通过求和放大吞吐；缺 SLA 保持未知。
# 函数用途: 验证脏数据覆盖和不完整 policy 的 null 比率。
def test_team_analytics_excludes_dirty_records_and_missing_policy(host):
    valid = alert(host, 'a1', 'alice', 600)
    negative = alert(host, 'negative', 'alice', 600) | {'resolved_at': stamp(host.clock[0] - 10000)}
    missing = alert(host, 'missing', 'alice', 600)
    missing.pop('assignee')
    seed(host, 'soc_alerts', {'a1': valid, 'duplicate': valid, 'negative': negative, 'missing': missing,
        'future': alert(host, 'future', 'alice', 1) | {'created_at': stamp(host.clock[0] + 10)}})
    result = ok(host, 'analytics.team.get')
    assert result['coverage'] == 'partial'
    assert result['team']['alerts_triaged'] == 1 and result['team']['mttr_seconds'] == 600
    assert result['team']['false_positive_rate'] == 0 and result['team']['sla_met_rate'] is None
    failed(_w12_invoke(host, 'analytics.team.get', {'space_id': 'missing-space'}), 404, 'RESOURCE_NOT_FOUND')
    seed(host, 'soc_alerts', {'foreign': alert(host, 'foreign', 'alice', 600) | {'owner_id': 'local/other'}})
    failed(_w12_invoke(host, 'analytics.team.get'), 404, 'RESOURCE_NOT_FOUND')


# LLM: null 是合法的未设置目标，零仍是有效时限；不以真假值混淆二者。
# 函数用途: 验证空 SLA、混合策略及零时限的真实聚合。
def test_team_nullable_and_zero_sla_targets(host):
    seed(host, 'monitor_sla_policies', {'space-test': {'space_id': 'space-test',
        'owner_id': host.home_paths.owner_id, 'policies': [
            {'severity': 'high', 'ack_minutes': None, 'resolve_minutes': None},
            {'severity': 'medium', 'ack_minutes': 0, 'resolve_minutes': 0}],
        'revision': 1, 'created_at': stamp(host.clock[0] - 1000), 'updated_at': stamp(host.clock[0])}})
    empty = ok(host, 'analytics.team.get')
    assert empty['coverage'] == 'complete' and empty['team']['sla_met_rate'] is None
    zero = alert(host, 'medium', 'bob', 0) | {'severity': 'medium',
        'acknowledged_at': stamp(host.clock[0] - 100)}
    seed(host, 'soc_alerts', {'high': alert(host, 'high', 'alice', 600), 'medium': zero})
    result = ok(host, 'analytics.team.get')
    assert result['coverage'] == 'complete' and result['team']['sla_met_rate'] is None
    analysts = {row['analyst']: row for row in result['analysts']}
    assert analysts['alice']['sla_met_rate'] is None and analysts['bob']['sla_met_rate'] == 1


# LLM: 坏类型与坏枚举不能拖垮整个查询，也不能被默认值掩盖。
# 函数用途: 验证聚合前剔除异常告警字段并保留 partial。
@pytest.mark.parametrize(('field', 'value'), [('assignee', ['alice']), ('tags', None),
    ('severity', 'invalid'), ('status', ['resolved']), ('verdict', {'bad': True})])
def test_team_bad_alert_fields_are_partial(host, field, value):
    seed(host, 'soc_alerts', {'valid': alert(host, 'valid', 'alice', 600),
        'invalid': alert(host, 'invalid', 'bob', 1200) | {field: value}})
    result = ok(host, 'analytics.team.get')
    assert result['coverage'] == 'partial'
    assert result['team']['alerts_triaged'] == 1 and result['team']['mttr_seconds'] == 600
    assert [row['analyst'] for row in result['analysts']] == ['alice']


# LLM: 确认晚于解决是异常生命周期，不得计入完整吞吐及均值。
# 函数用途: 验证跨字段时间顺序在聚合前被拒绝。
def test_team_reversed_lifecycle_is_partial(host):
    invalid = alert(host, 'invalid', 'bob', 600) | {'acknowledged_at': stamp(host.clock[0] - 50)}
    seed(host, 'soc_alerts', {'valid': alert(host, 'valid', 'alice', 1200), 'invalid': invalid})
    result = ok(host, 'analytics.team.get')
    assert result['coverage'] == 'partial'
    assert result['team']['alerts_triaged'] == 1 and result['team']['mttr_seconds'] == 1200


# LLM: 模式验证是领域规则；假模型仅截获派发，不替代真实 Store/HTTP 测试。
# 函数用途: 验证规则作业准确 kind、输入模式和目标格式差异。
def test_rule_translate_job_kind_and_mode_validation(host, monkeypatch):
    captured = []

    # LLM: 记录 W0 精确调用参数，返回显式 DomainResult 而非根据 state 猜受理。
    # 函数用途: 隔离模型执行以检查领域派发合同。
    def start(call, kind, instruction, *, space_id=None):
        captured.append((kind, instruction, space_id))
        return DomainResult(202, {'state': 'accepted', 'operation_id': 'operation-test',
            'job_id': 'job-test', 'task_id': 'task-test'})

    monkeypatch.setattr(foundation, 'v13_start_model_job', start)
    for params in ({'mode': 'translate', 'text': 'rule'},
            {'mode': 'translate', 'text': 'rule', 'source_format': 'sigma', 'target_format': 'sigma'},
            {'mode': 'translate', 'text': 'rule', 'target_format': 'kql'}):
        with pytest.raises(module.BusinessError) as error:
            module.rule_translate(call_for(host, 'tools.rule_translate', params))
        assert error.value.code == 'INVALID_SCHEMA'
    assert captured == []
    for params in ({'mode': 'translate', 'source_format': 'nl', 'target_format': 'sigma', 'text': '检测登录失败'},
            {'mode': 'translate', 'source_format': 'spl', 'target_format': 'kql', 'text': 'search test'},
            {'mode': 'noise_advice', 'text': 'rule'}, {'mode': 'coverage_advice', 'text': 'rule'}):
        result = module.rule_translate(call_for(host, 'tools.rule_translate', params | {'space_id': 'space-test'}))
        assert isinstance(result, DomainResult) and result.http_status == 202
        assert captured[-1][0] == 'rule_translation' and captured[-1][2] == 'space-test'
        assert json.loads(captured[-1][1].split('\nrequest=', 1)[1]) == params
    assert foundation.JOB_KINDS['rule_translation']['finalize'] is module.finalize_rule_translation


# LLM: 默认空间必须由原 W1 可见结果证明唯一，零个或多个不猜。
# 函数用途: 验证规则转换的可选 space_id 选择语义。
def test_rule_translation_default_space_is_only_unique_active_space(host, monkeypatch):
    selected = []

    # LLM: 空间查询保持真实，仅隔离模型发起端口。
    # 函数用途: 捕获最终选中的真实空间 ID。
    def start(call, kind, instruction, *, space_id=None):
        selected.append(space_id)
        return DomainResult(202, {'state': 'accepted', 'operation_id': 'op-test', 'job_id': 'job-test', 'task_id': 'task-test'})

    monkeypatch.setattr(foundation, 'v13_start_model_job', start)
    params = {'mode': 'noise_advice', 'text': 'rule'}
    with pytest.raises(module.BusinessError) as error:
        module.rule_translate(call_for(host, 'tools.rule_translate', params))
    assert error.value.code == 'INVALID_SCHEMA'
    first = ok(host, 'spaces.create', {'name': '唯一空间'}, key=idem())['space_id']
    module.rule_translate(call_for(host, 'tools.rule_translate', params))
    assert selected == [first]
    ok(host, 'spaces.create', {'name': '第二空间'}, key=idem())
    with pytest.raises(module.BusinessError) as error:
        module.rule_translate(call_for(host, 'tools.rule_translate', params))
    assert error.value.code == 'INVALID_SCHEMA' and selected == [first]


# LLM: finalizer 只解析；W0 schema 必须拒绝缺字段、额外字段与超限内容。
# 函数用途: 验证模型结果不会被补字段或编造内容修复。
def test_rule_translation_finalizer_strict_json_and_schema():
    valid = {'mode': 'translate', 'output': 'title: test', 'target_format': 'sigma', 'advice': [], 'warnings': []}
    kind = foundation.JOB_KINDS['rule_translation']
    raw = json.dumps(valid)
    assert module.finalize_rule_translation(raw) == valid
    assert validate_object(kind['finalize'](raw), foundation.v13_schema(kind['result_schema'])) == valid
    for bad in ('not json', '[]', '{"mode":"translate","mode":"noise_advice"}', '{"x":NaN}'):
        with pytest.raises((HostAPIError, module.BusinessError)):
            module.finalize_rule_translation(bad)
    for value in ({'mode': 'translate'}, valid | {'extra': True}, valid | {'output': 'x' * 20001}):
        parsed = module.finalize_rule_translation(json.dumps(value))
        assert parsed == value
        with pytest.raises(HostAPIError):
            validate_object(parsed, foundation.v13_schema(kind['result_schema']))


# LLM: 只通过原模型管理入口配置隔离 owner 的离线 echo 档，不使用真实用户配置、密钥或网络。
# 函数用途: 在原任务创建前配置该测试调用者可解析的模型档。
def _configure_native_echo(native):
    from types import SimpleNamespace
    from uuid import uuid4

    from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation
    from agent_py_agent.agent.user_space.owner_resolver import (
        OwnerIdentity,
        home_paths_with_owner,
        resolve_owner_home,
    )

    source = native[1]
    owner = resolve_owner_home(source.home_paths.root, OwnerIdentity.provider_user('feishu', 'business-alice'))
    host = SimpleNamespace(config=source.config, home_paths=home_paths_with_owner(source.home_paths, owner))
    profile_id = str(uuid4())
    execute_model_profile_operation(host, 'add', {'profile_id': profile_id, 'profile': {
        'model_backend': 'echo', 'model_name': 'fixture-echo', 'transport': 'offline', 'capability': 'agentic'}})
    execute_model_profile_operation(host, 'set_default', {'profile_id': profile_id})


# LLM: 只推进准确 owner 的原后台车道；任务和正文必须由真实 wake 执行产生，不手改状态或补写输出。
# 函数用途: 在隔离夹具内执行规则作业并核对 canonical 任务与最终消息的关联。
def _drive_native_rule_job(native, job_id):
    from contextlib import ExitStack

    from agent_py_agent.agent.conversation import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
    )
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home

    identity = OwnerIdentity.provider_user('feishu', 'business-alice')
    owner = resolve_owner_home(native[1].home_paths.root, identity)
    job = V13Store(owner.home_dir, owner_id=owner.owner_id).get('async_jobs', job_id)
    assert job is not None and job['owner_id'] == owner.owner_id
    with ExitStack() as stack:
        agent = api.owner_complete_host(native[0], identity, stack)
        assert agent.home_paths.owner_id == owner.owner_id
        store = agent.conversation_store
        runtime = BackgroundMainAgentRuntime(agent=agent, store=store)
        scheduler = BackgroundMainAgentScheduler({'runtime': runtime, 'store': store})
        reports = scheduler.tick_thread(job['agent_id'])
        assert reports, '原规则任务 wake 未被后台执行器消费'
        task = store.session_tasks.require(job['task_id'])
        assert task.status in {'done', 'completed'}, task
        assert task.conversation_request_id
        messages, errors = store.messages.recent_report(task.target_thread_id, limit=100)
        assert not errors, errors
        key = 'session_task:' + task.task_id + ':dispatch'
        publication = json.loads(store.storage.wake_dedupe_path(task.target_thread_id, key).read_text(encoding='utf-8'))
        assert publication['phase'] == 'published' and publication['retain_handled'] is True
        assert publication['thread_id'] == task.target_thread_id and publication['dedupe_key'] == key
        signal = publication['signal']
        assert signal['thread_id'] == task.target_thread_id and signal['dedupe_key'] == key
        assert signal['metadata']['session_task_id'] == task.task_id == job['task_id']
        assert signal['metadata']['origin_thread_id'] == task.sender_thread_id
        assert signal['wake_signal_id'] == publication['wake_signal_id']
        final_key = 'owner_delivery:background-wake:' + signal['wake_signal_id']
        finals = [message for message in messages if message.role == 'assistant'
            and message.metadata.get('dedupe_key') == final_key
            and message.metadata.get('assistant_part_id') == 'final']
        assert len(finals) == 1
        transcript = finals[0].metadata['conversation_request_id']
        assert transcript and any(message.role == 'assistant'
            and message.metadata.get('conversation_request_id') == transcript
            and message.metadata.get('canonical_native_messages') for message in messages)
    return job


# LLM: 真实临时 Gateway、真实助手、原后台 tick 及 echo 模型完成异步主链路，不调用外部模型。
# 函数用途: 验证原生读信封、规则 202 和非法模型输出的 JOB_RESULT_INVALID。
def test_envelopes(v13_native):
    import time

    _configure_native_echo(v13_native)
    for action in ('shifts.schedule.get', 'shifts.handoffs.list', 'analytics.team.get'):
        status, body = v13_call(v13_native, action, {}, user='business-alice')
        assert status == 200, body
        assert body['state'] == 'completed' and body['error'] is None
        assert set(body) == {'request_id', 'operation_id', 'state', 'data', 'error',
            'allowed_actions', 'warnings', 'effect_outcome'}
    space_id = v13_space(v13_native)
    params = {'mode': 'noise_advice', 'text': 'synthetic echo job'}
    key = idem()
    status, accepted = v13_call(v13_native, 'tools.rule_translate', params,
        user='business-alice', idempotency_key=key)
    assert status == 202, json.dumps(accepted, ensure_ascii=False)
    assert accepted['state'] == 'accepted' and accepted['error'] is None
    job = accepted['data']
    assert set(job) == {'state', 'operation_id', 'job_id', 'task_id'}
    again_status, again = v13_call(v13_native, 'tools.rule_translate', params,
        user='business-alice', idempotency_key=key)
    assert again_status == 202 and again['data'] == job
    failed(v13_call(v13_native, 'tools.rule_translate', params | {'text': 'different'},
        user='business-alice', idempotency_key=key), 409, 'IDEMPOTENCY_CONFLICT')
    stored = _drive_native_rule_job(v13_native, job['job_id'])
    assert stored['kind'] == 'rule_translation' and stored['space_id'] == space_id
    terminal = None
    for attempt in range(200):
        status, body = v13_call(v13_native, 'async_jobs.get', {'job_id': job['job_id']}, user='business-alice')
        assert status == 200, body
        if body['data']['state'] in {'completed', 'failed'}:
            terminal = body['data']
            break
        time.sleep(0.05)
    assert terminal is not None, body
    assert terminal['state'] == 'failed' and terminal['result'] is None
    assert terminal['error']['code'] == 'JOB_RESULT_INVALID', terminal
    same_status, same = v13_call(v13_native, 'async_jobs.get', {'job_id': job['job_id']}, user='business-alice')
    assert same_status == 200 and same['data'] == terminal


# LLM: 只固定本地模型 JSON；HTTP、原后台 tick、助手任务、消息持久化及作业回读都用真实实现。
# 函数用途: 验证规则异步任务从 202 受理到合法 JSON 完成的成功链路。
def test_rule_translation_native_success(v13_native, monkeypatch):
    import time

    from agent_py_agent.agent.backends.base import EchoBackend, ModelResponse

    expected = {'mode': 'translate', 'output': 'title: synthetic rule', 'target_format': 'sigma',
        'advice': [], 'warnings': []}
    prompts = []

    # LLM: 保留真实模型后端合同，不直接改任务状态、回执或 async_jobs.get。
    # 函数用途: 为隔离测试的原生模型调用返回合成 JSON。
    def generate(self, prompt, on_chunk=None, tools=None, tool_choice=None, messages=None,
                 on_thinking_delta=None, request_options=None):
        prompts.append(prompt)
        return ModelResponse(text=json.dumps(expected), backend='echo')

    monkeypatch.setattr(EchoBackend, 'generate', generate)
    _configure_native_echo(v13_native)
    space_id = v13_space(v13_native)
    status, accepted = v13_call(v13_native, 'tools.rule_translate', {'mode': 'translate',
        'source_format': 'nl', 'target_format': 'sigma', 'text': 'synthetic rule', 'space_id': space_id},
        user='business-alice', idempotency_key=idem())
    assert status == 202 and accepted['state'] == 'accepted', accepted
    job_id = accepted['data']['job_id']
    _drive_native_rule_job(v13_native, job_id)
    terminal = None
    for attempt in range(200):
        status, body = v13_call(v13_native, 'async_jobs.get', {'job_id': job_id}, user='business-alice')
        assert status == 200, body
        if body['data']['state'] in {'completed', 'failed'}:
            terminal = body['data']
            break
        time.sleep(0.05)
    assert terminal is not None and terminal['state'] == 'completed', body
    assert terminal['error'] is None and terminal['result'] == expected
    assert prompts
    status, replay = v13_call(v13_native, 'async_jobs.get', {'job_id': job_id}, user='business-alice')
    assert status == 200 and replay['data'] == terminal


# ============================================================================
# Source: agent_py_agent/tests/test_v13_w1_silent_except_diagnostics.py
# ============================================================================

def test_archive_lock_failure_rolls_back(tmp_path, monkeypatch, caplog):
    source = tmp_path / "source" / "tasks.jsonl"
    source.parent.mkdir()
    source.write_text("data")
    lock = Path(f"{source}.lock")
    lock.write_text("lock")
    archive = tmp_path / "archive"
    rename = Path.rename

    def move(path, target):
        if path.suffix == ".lock":
            raise PermissionError(errno.EACCES, "denied")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", move)
    with caplog.at_level(logging.WARNING):
        assert store_io.archive_ledger_file(source, archive) is False
    assert source.read_text() == "data" and lock.read_text() == "lock"
    assert list(archive.iterdir()) == []
    assert "LEDGER_ARCHIVE_LOCK_MOVE_FAILED" in caplog.text
    assert "rolled_back=True" in caplog.text and "EACCES" in caplog.text
    monkeypatch.setattr(Path, "rename", rename)
    assert store_io.archive_ledger_file(source, archive) is True
    assert (archive / source.name).read_text() == "data"
    assert (archive / lock.name).read_text() == "lock"


def test_archive_rollback_failure_reports_both_errors(tmp_path, monkeypatch, caplog):
    source = tmp_path / "tasks.jsonl"
    source.write_text("data")
    Path(f"{source}.lock").write_text("lock")
    archive = tmp_path / "archive"
    rename = Path.rename

    def move(path, target):
        if path.suffix == ".lock":
            raise PermissionError(errno.EACCES, "denied")
        if path.parent == archive:
            raise OSError(errno.EBUSY, "busy")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", move)
    assert store_io.archive_ledger_file(source, archive) is False
    assert (archive / source.name).read_text() == "data"
    assert "rolled_back=False" in caplog.text
    assert "EACCES" in caplog.text and "EBUSY" in caplog.text
    assert str(source) in caplog.text and str(archive / source.name) in caplog.text
    assert caplog.records[-1].levelno == logging.ERROR


@pytest.mark.parametrize("probe,expected", [("task_work_kind", ""), ("task_has_active_goal", False), ("task_link_status", "unknown")])
@pytest.mark.parametrize("error", [PermissionError("denied"), OSError(errno.EBUSY, "busy"), json.JSONDecodeError("bad", "?", 0)])
@pytest.mark.parametrize("legacy", [False, True])
def test_task_probes_log_structured_failure(probe, expected, error, legacy, caplog):
    def fail(*args):
        raise error

    scheduler = object.__new__(runtime.BackgroundMainAgentScheduler)
    tasks = SimpleNamespace(thread_for=fail, list_report=fail)
    if not legacy:
        tasks.load_report = fail
    scheduler.store = SimpleNamespace(tasks=tasks)
    method = getattr(scheduler, f"_{probe}")
    args = ("thread", "task-id") if probe == "task_link_status" else ("task-id",)
    with caplog.at_level(logging.WARNING):
        assert method(*args) == expected
        assert method(*args) == expected
    records = [r for r in caplog.records if getattr(r, "probe", None) == probe]
    assert len(records) == 1
    row = records[0]
    assert row.task_id == "task-id" and row.error_type == type(error).__name__
    assert row.error == str(error)
    assert f"probe={probe}" in row.message and "task_id=task-id" in row.message


def test_probe_warning_cache_is_bounded_and_redacted(caplog):
    scheduler = object.__new__(runtime.BackgroundMainAgentScheduler)
    for index in range(scheduler._TASK_PROBE_WARN_LIMIT + 3):
        scheduler._record_task_probe_failure("task_work_kind", str(index), ValueError("password=test-secret"))
    assert len(scheduler._task_probe_warnings) == scheduler._TASK_PROBE_WARN_LIMIT
    assert "test-secret" not in caplog.text


def test_real_task_link_corruption_reports_without_raising(tmp_path, caplog):
    from agent_py_agent.tests.test_gateway_agent_control_service import _bound_agent_tree

    agent, _, _ = _bound_agent_tree(tmp_path)
    store = agent.conversation_store
    store.storage.task_path("task-root").write_text("{")
    scheduler = object.__new__(runtime.BackgroundMainAgentScheduler)
    scheduler.store = store
    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            assert scheduler._task_work_kind("task-root") == ""
            assert scheduler._task_link_status("irrelevant", "task-root") == "unknown"
    records = [row for row in caplog.records if getattr(row, "task_id", None) == "task-root"]
    assert len(records) == 2
    assert {row.probe for row in records} == {"task_work_kind", "task_link_status"}
    assert {row.error_type for row in records} == {"JSONDecodeError"}


def test_legacy_error_reports_are_not_discarded(caplog):
    error = {"task_id": "task", "error_type": "PermissionError", "message": "password=test-secret"}
    scheduler = object.__new__(runtime.BackgroundMainAgentScheduler)
    scheduler.store = SimpleNamespace(tasks=SimpleNamespace(
        thread_for=lambda _: SimpleNamespace(thread_id="thread"), list_report=lambda _: ([], [error])))
    for _ in range(2):
        assert scheduler._task_work_kind("task") == ""
        assert scheduler._task_link_status("thread", "task") == "unknown"
    records = [row for row in caplog.records if getattr(row, "task_id", None) == "task"]
    assert len(records) == 2 and all(row.error_type == "PermissionError" for row in records)
    assert "test-secret" not in caplog.text


def test_missing_task_report_is_normal_absence(caplog):
    scheduler = object.__new__(runtime.BackgroundMainAgentScheduler)
    scheduler.store = SimpleNamespace(tasks=SimpleNamespace(load_report=lambda _: (None,
        {"error_type": "FileNotFoundError", "message": "missing"})))
    assert scheduler._task_work_kind("missing") == ""
    assert scheduler._task_link_status("thread", "missing") == "missing"
    assert not [row for row in caplog.records if getattr(row, "probe", None)]


# ============================================================================
# Source: agent_py_agent/tests/test_v13_w2_progress_stream_contract.py
# ============================================================================

def _stream(tmp_path, *, level="stream", holdback=8):
    writer = BufferedChunkStreamWriter(tmp_path / "text.chunks.jsonl", text_delta_holdback_chars=holdback)
    writer.set_verbose_level(level)
    return writer


def _events(writer):
    return _read_public_progress_events(writer.chunk_path, 0, include=frozenset({"assistant_text_delta"}))[0]


@pytest.mark.parametrize("key", ["Authorization", "Proxy-Authorization", "Cookie", "Set-Cookie"])
def test_review_unfinished_header_key_never_loses_redaction_context(key):
    buffer = TextDeltaBuffer()
    buffer.begin("header-call")
    first = '{"' + key + '"' + " " * 300
    last = ': "Bearer SyntheticHeaderCredential"}\n' + "正常后续。" * 100
    rows = buffer.append(first, model_call_id="header-call")
    rows += buffer.append(last, model_call_id="header-call")
    rows += buffer.finish("header-call")
    public = "".join(row["text"] for row in rows if row["kind"] == "assistant_text_delta")
    assert "SyntheticHeaderCredential" not in public
    assert public == redact_sensitive_text(first + last)


@pytest.mark.parametrize("opening,closing", [
    ("[RUN_INTERNAL] ", ""), ("<tool_call>\n", "\n</tool_call>"),
    ("[TOOL_CALL] ", "\n[/TOOL_CALL]"),
])
def test_review_stream_preserves_whole_call_protocol_suppression(tmp_path, opening, closing):
    writer = _stream(tmp_path)
    callback = writer.bind_model_call("call-current")
    text = opening + "不能公开的内部调用参数。" * 80 + closing
    for offset in range(0, len(text), 7):
        callback(text[offset:offset + 7])
    _commit(writer, "")
    assert not any(row["kind"] == "assistant_text_delta" for row in _events(writer))


def test_review_close_marks_writer_closed_before_candidate_reset(tmp_path, monkeypatch):
    writer = _stream(tmp_path)
    writer.bind_model_call("call-current")("原候选。" * 30)
    reset = BufferedChunkStreamWriter.reset_model_text
    observed = []

    def observe_gap(self, reason, **kwargs):
        reset(self, reason, **kwargs)
        if reason == "cancel":
            # 固定旧关闭代码中 reset 返回到关闭标记之间的交错观察点，无 sleep 或线程调度假设。
            observed.append(bool(getattr(self, "_liveness_closed", False)))
            self.bind_model_call("late-call")("关闭过程中新来的候选。" * 40)

    monkeypatch.setattr(BufferedChunkStreamWriter, "reset_model_text", observe_gap)
    writer.close()
    assert observed == [True]
    assert _events(writer)[-1]["kind"] == "assistant_text_reset"


@pytest.mark.parametrize("patch", [{"status": []}, {"duration_ms": True}, {"seq": -1}, {"next_step_ids": [1]}])
def test_workflow_invalid_schema_is_ignored(tmp_path, patch):
    writer = _stream(tmp_path)
    writer.write_workflow_step_progress({"workflow_run_id": "run", "workflow_id": "workflow", "step_id": "step",
        "name": "步骤", "status": "running", "duration_ms": 0, "description": "", "next_step_ids": [], "seq": 0,
        **patch})
    assert not writer.chunk_path.exists()


def _commit(writer, text, call_id="call-current", boundary="terminal"):
    writer.commit_model_response(_payload([{"text": text}], call_id=call_id, boundary=boundary, final=text))


@pytest.mark.parametrize("tag", ["memory-context", "read_file", "function"])
@pytest.mark.parametrize("width", [1, 37])
def test_review_unfinished_xml_attributes_never_reach_any_delta(tmp_path, tag, width):
    from agent_py_agent.agent.conversation.channels import project_user_reply

    writer = _stream(tmp_path)
    callback = writer.bind_model_call("call-current")
    first = '公开前缀。\n<' + tag + ' note="' + "SyntheticInternalArgument " * 80
    last = '">内部记忆正文</' + tag + '>\n公开后缀。' + "正常后续。" * 100
    callback(first)
    assert "SyntheticInternalArgument" not in json.dumps(_events(writer))
    for offset in range(0, len(last), width):
        callback(last[offset:offset + width])
        assert "SyntheticInternalArgument" not in json.dumps(_events(writer))
    _commit(writer, first + last)
    rows = _events(writer)
    assert "SyntheticInternalArgument" not in writer.chunk_path.read_text()
    rendered = ""
    for row in rows:
        if row["kind"] == "assistant_text_reset":
            rendered = ""
        elif row["kind"] == "assistant_text_delta":
            rendered += row["text"]
    assert rendered == project_user_reply(first + last).content


@pytest.mark.parametrize("text", ["", "[RUN_INTERNAL] 内部终稿"])
@pytest.mark.parametrize("boundary", ["terminal", "tools"])
def test_review_empty_public_commit_closes_published_candidate(tmp_path, text, boundary):
    writer = _stream(tmp_path)
    callback = writer.bind_model_call("call-current")
    callback("待撤销候选。" * 80)
    assert any(row["kind"] == "assistant_text_delta" for row in _events(writer))
    _commit(writer, text, boundary=boundary)
    rows = _events(writer)
    assert "call-current" in writer._committed_model_calls
    assert writer._text_delta.model_call_id == ""
    if boundary == "terminal":
        assert rows[-2]["kind"] == "assistant_text_reset"
        assert rows[-1] == {"kind": "assistant_text_done", "model_call_id": "call-current", "seq": 0}
    else:
        assert rows[-1]["kind"] == "assistant_text_reset" and rows[-1]["reason"] == "tool_call"
    snapshot = writer.chunk_path.read_bytes()
    _commit(writer, text, boundary=boundary)
    callback("提交后不能复活的迟到正文。" * 40)
    assert writer.chunk_path.read_bytes() == snapshot


@pytest.mark.parametrize("patch", [
    {"schema": "invalid"}, {"boundary": []}, {"messages": {}}, {"source": {}}, {"source": {"call_id": []}},
])
def test_review_invalid_empty_commit_cannot_close_candidate(tmp_path, patch):
    writer = _stream(tmp_path)
    writer.bind_model_call("call-current")("仍有效的候选。" * 80)
    snapshot = writer.chunk_path.read_bytes()
    writer.commit_model_response({**_payload([{"text": ""}], call_id="call-current", final=""), **patch})
    assert writer.chunk_path.read_bytes() == snapshot
    assert writer._text_delta.model_call_id == "call-current" and not writer._committed_model_calls


def test_review_character_callbacks_batch_projection_work(monkeypatch):
    from agent_py_agent.agent.gateway_parts import progress_text_delta

    projected, scanned = [], []
    safe_cut = progress_text_delta._safe_cut

    def counted_cut(raw, limit, identifiers):
        scanned.append(len(raw))
        return safe_cut(raw, limit, identifiers)

    def counted_project(text):
        projected.append(len(text))
        return text

    monkeypatch.setattr(progress_text_delta, "_safe_cut", counted_cut)
    buffer = TextDeltaBuffer(project=counted_project)
    buffer.begin("counted-call")
    text, rows = "字" * 2048, []
    for char in text:
        rows.extend(buffer.append(char, model_call_id="counted-call"))
    rows.extend(buffer.finish("counted-call"))
    assert "".join(row["text"] for row in rows if row["kind"] == "assistant_text_delta") == text
    assert len(scanned) <= len(text) // 64 + 2
    assert sum(scanned) <= 64 * len(text) and sum(projected) <= 64 * len(text)


def test_projection_budget_stops_scanning_and_resets_for_next_call():
    work = []

    def project(text):
        work.append(len(text))
        return text

    buffer = TextDeltaBuffer(holdback_chars=8, project=project, max_projection_chars=4096)
    buffer.begin("budget-call")
    rows = []
    for _ in range(2048):
        rows.extend(buffer.append("字", model_call_id="budget-call"))
    assert rows[-1]["kind"] == "assistant_text_reset" and rows[-1]["reason"] == "superseded"
    used = tuple(work)
    assert buffer.append("不能在预算耗尽后继续投影", model_call_id="budget-call") == []
    assert buffer.finish("budget-call", text="权威终稿。" * 100)[-1]["kind"] == "assistant_text_done"
    assert tuple(work) == used and sum(work) <= 4096
    buffer.begin("next-call")
    assert buffer.append("新候选。" * 20, model_call_id="next-call")
    assert buffer.finish("next-call", text="新候选。" * 20)[-1]["kind"] == "assistant_text_done"


def test_acceptance_same_call_delta_then_terminal(tmp_path):
    writer = _stream(tmp_path)
    callback = writer.bind_model_call("call-current")
    text = "这是模型产生的公开正文。" * 30
    callback(text[:100])
    # 首次发布发生在 commit 之前，不能以终稿人工分字冒充 token 流。
    before = _events(writer)
    assert before[0] == {"kind": "process_settings", "verbose_level": "stream"}
    assert before[1:] and all(row["kind"] == "assistant_text_delta" for row in before[1:])
    callback(text[100:])
    _commit(writer, text)
    rows = _events(writer)
    deltas = [row for row in rows if row["kind"] == "assistant_text_delta"]
    assert "".join(row["text"] for row in deltas) == text
    assert [row["seq"] for row in deltas] == list(range(len(deltas)))
    assert rows[-1] == {"kind": "assistant_text_done", "model_call_id": "call-current", "seq": len(deltas) - 1}
    _commit(writer, text)
    assert _events(writer) == rows


def test_acceptance_retry_and_cancel_correction(tmp_path):
    writer = _stream(tmp_path)
    callback = writer.bind_model_call("call-current")
    callback("重试前正文。" * 20)
    writer.write_provider_retry(scope="transport", attempt=1, total=2, delay_seconds=0, error_type="TimeoutError")
    callback("重试后正文。" * 20)
    rows = _events(writer)
    reset_index = next(index for index, row in enumerate(rows) if row["kind"] == "assistant_text_reset")
    assert rows[reset_index]["reason"] == "retry"
    assert rows[reset_index + 1]["seq"] == 0
    writer.close()
    assert _events(writer)[-1]["reason"] == "cancel"
    snapshot = writer.chunk_path.read_bytes()
    callback("关闭后迟到正文。" * 50)
    assert writer.chunk_path.read_bytes() == snapshot


@pytest.mark.parametrize("secret", ["sk-" + "SyntheticCredential" * 40,
    "https://reader:" + "SyntheticPassword" * 45 + "@example.invalid/report",
    'password="' + "quoted synthetic credential " * 25 + '"',
    "Authorization: Bearer " + "SyntheticBearer" * 50,
    "-----BEGIN PRIVATE KEY-----\n" + "SYNTHETICPRIVATE" * 40 + "\n-----END PRIVATE KEY-----"])
@pytest.mark.parametrize("width", [1, 37, 257])
def test_acceptance_cross_chunk_credentials_never_escape(secret, width):
    buffer = TextDeltaBuffer()
    buffer.begin("call-safe")
    text = "正常开始。 " + secret + "\n" + "正常后续。" * 100
    rows = []
    for offset in range(0, len(text), width):
        rows.extend(buffer.append(text[offset:offset + width], model_call_id="call-safe"))
    rows.extend(buffer.finish("call-safe"))
    public = "".join(row["text"] for row in rows if row["kind"] == "assistant_text_delta")
    assert public == redact_sensitive_text(text)
    assert not any(marker in public for marker in ("SyntheticCredential", "SyntheticPassword", "SyntheticBearer", "SYNTHETICPRIVATE"))
    assert "quoted synthetic credential" not in public


def test_acceptance_old_call_late_write_is_rejected(tmp_path):
    writer = _stream(tmp_path)
    old_callback = writer.bind_model_call("call-old")
    old_callback("旧候选。" * 40)
    callback = writer.bind_model_call("call-current")
    before = writer.chunk_path.read_bytes()
    old_callback("不能混入新调用。" * 40)
    assert writer.chunk_path.read_bytes() == before
    callback("新候选。" * 40)
    _commit(writer, "新候选。" * 40)
    rows = _events(writer)
    assert any(row.get("reason") == "superseded" and row["model_call_id"] == "call-old" for row in rows)
    assert rows[-1]["model_call_id"] == "call-current"


def test_acceptance_cursor_replay_and_default_invisibility(tmp_path):
    writer = _stream(tmp_path)
    callback = writer.bind_model_call("call-current")
    callback("第一段。" * 40)
    first, cursor = _read_public_progress_events(writer.chunk_path, 0, include=frozenset({"assistant_text_delta"}))
    callback("第二段。" * 40)
    _commit(writer, "第一段。" * 40 + "第二段。" * 40)
    rest, end = _read_public_progress_events(writer.chunk_path, cursor, include=frozenset({"assistant_text_delta"}))
    assert end > cursor
    seqs = [row["seq"] for row in first + rest if row["kind"] == "assistant_text_delta"]
    assert len(seqs) == len(set(seqs))
    assert _read_public_progress_events(writer.chunk_path, end, include=frozenset({"assistant_text_delta"})) == ([], end)
    assert _read_public_progress_events(writer.chunk_path, 0)[0] == [{"kind": "process_settings", "verbose_level": "stream"}]


def test_acceptance_long_command_input_and_full_output_archive(tmp_path):
    agent, thread = _owner(tmp_path)
    writer = _stream(tmp_path, level="full")
    command = "printf '" + "字" * 300 + "' --token=" + "sk-SyntheticCredential123456789"
    call = _canonical_calls([{"tool": "run_command", "command": command}])[0]
    request = SimpleNamespace(agent=agent, params=SimpleNamespace(
        task_attributes={"conversation_thread_id": thread.thread_id}), tool_rounds=1)
    started = ToolProgressEvent(request, 1, call, "started", "开始")
    first = _structured_tool_progress(started, "run_command", _payload_progress_detail(call))
    assert len(first["detail"]) <= 100
    assert first["input"]["command"] == redact_sensitive_text(command)
    assert "SyntheticCredential" not in json.dumps(first)
    stdout, stderr = "输出" * 12000, "诊断" * 12000
    result = ToolResult.succeeded(call, stdout + stderr, facts=ToolSuccessFacts(metadata={
        "handler_details": {"display": {"kind": "command", "return_code": 0,
                                        "stdout": stdout, "stderr": stderr, "capture_complete": True}}}))
    last = _structured_tool_progress(replace(started, phase="finished", status="完成", result=result),
                                     "run_command", _payload_progress_detail(call))
    assert last["call_id"] == first["call_id"] == call.call_id
    writer.write_progress(first, "开始")
    writer.write_progress(last, "完成")
    rows = _read_public_progress_events(writer.chunk_path, 0)[0]
    assert rows[0] == {"kind": "process_settings", "verbose_level": "full"}
    tool_rows = rows[1:]
    assert tool_rows and all(row["kind"] == "tool_progress" for row in tool_rows)
    assert stdout not in json.dumps(rows, ensure_ascii=False) and stderr not in json.dumps(rows, ensure_ascii=False)
    reference = rows[-1]["display_archive_ref"]
    restored = []
    for page in range(reference["page_count"]):
        restored.extend(read_display_archive_page(agent.conversation_store, reference, page)["rows"])
    assert "".join(row["text"] for row in restored if row["kind"] == "stdout") == stdout
    assert "".join(row["text"] for row in restored if row["kind"] == "stderr") == stderr
    short = _structured_tool_progress(started, "run_command", "", max_input_chars=16)
    assert short["input_truncated"] is True and len(short["input"]["command"]) == 16
    writer.set_verbose_level("on")
    writer.write_progress(first, "开始")
    assert "input" not in _read_public_progress_events(writer.chunk_path, 0)[0][-1]


@pytest.mark.parametrize("call_id", ["", None, "   "])
def test_empty_tool_call_id_is_omitted(call_id):
    call = SimpleNamespace(call_id=call_id, arguments={})
    event = ToolProgressEvent(SimpleNamespace(tool_rounds=1), 1, call, "started", "开始")
    assert "call_id" not in _structured_tool_progress(event, "read_file", "")


@pytest.mark.parametrize("level", ["off", "on", "full"])
def test_non_stream_level_never_persists_candidate(level, tmp_path):
    writer = _stream(tmp_path, level=level)
    callback = writer.bind_model_call("call-current")
    callback("原始候选。" * 50)
    writer.close()
    assert not writer.chunk_path.exists()


def test_tool_boundary_resets_candidate_and_late_write(tmp_path):
    writer = _stream(tmp_path)
    callback = writer.bind_model_call("call-current")
    callback("工具之前。" * 50)
    _commit(writer, "先执行工具。", boundary="tools")
    assert any(row.get("reason") == "tool_call" for row in _events(writer))
    snapshot = writer.chunk_path.read_bytes()
    callback("旧正文迟到。" * 40)
    assert writer.chunk_path.read_bytes() == snapshot


def test_text_delta_memory_budget_never_leaks_discarded_secret_suffix():
    buffer = TextDeltaBuffer(holdback_chars=8, max_buffer_chars=32)
    buffer.begin("bounded-call")
    assert buffer.append("sk-" + "S" * 60, model_call_id="bounded-call")[0]["reason"] == "superseded"
    assert buffer.append("secret-tail", model_call_id="bounded-call") == []
    assert all("text" not in row for row in buffer.finish("bounded-call"))


def test_workflow_step_is_typed_redacted_monotonic_and_opt_in(tmp_path):
    writer = _stream(tmp_path)
    event = {"workflow_run_id": "workflow-run", "workflow_id": "workflow", "step_id": "step",
             "name": "读取", "status": "running", "duration_ms": 0,
             "description": "token=synthetic-private-workflow", "next_step_ids": ["next"], "seq": 1,
             "private_unknown": "not-public"}
    writer.write_workflow_step_progress(event)
    writer.write_workflow_step_progress(event)
    assert _read_public_progress_events(writer.chunk_path, 0)[0] == [{"kind": "process_settings", "verbose_level": "stream"}]
    rows, cursor = _read_public_progress_events(writer.chunk_path, 0, include=frozenset({"workflow_step_progress"}))
    assert cursor == 2 and len(rows) == 2 and rows[0] == {"kind": "process_settings", "verbose_level": "stream"}
    assert rows[1]["kind"] == "workflow_step_progress" and rows[1]["step_id"] == "step" and "workflow_step" not in rows[1]
    assert "synthetic-private" not in json.dumps(rows) and "private_unknown" not in rows[1]
    writer.write_workflow_step_progress({**event, "seq": 2, "status": "succeeded"})
    assert _read_public_progress_events(writer.chunk_path, cursor, include=frozenset({"workflow_step_progress"}))[0][0]["seq"] == 2




def _w2_http(server, path, body=None, *, token=True):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Gateway-Token"] = _TOKEN
    try:
        conn.request("GET" if body is None else "POST", path,
                     None if body is None else json.dumps(body), headers)
        response = conn.getresponse()
        return response.status, json.loads(response.read())
    finally:
        conn.close()

@pytest.mark.parametrize("bad", [None, True, 0, [], {}, "FULL", "no"])
def test_ask_invalid_verbose_level_has_no_pending_file(tmp_path, bad):
    server = _server(tmp_path)
    server.start()
    try:
        status, payload = _w2_http(server, "/ask", {"goal": "验证档位", "verbose_level": bad})
        assert status == 400 and payload["error_code"] == "INVALID_VERBOSE_LEVEL"
        assert payload["error"] == "verbose_level必须是off/on/full/stream之一。"
        assert list(server.paths.inbox.glob("*.json")) == []
    finally:
        server.stop()

@pytest.mark.parametrize("explicit,expected", [("full", "full"), ("stream", "stream"), ("off", "off"), (None, "on")])
def test_ask_verbose_level_full_exposes_tool_progress_without_control(tmp_path, monkeypatch, explicit, expected):
    server = _server(tmp_path)
    server.start()
    try:
        body = {"goal": "读取资料", "conversation_id": "w2-http", "delivery_intent": "queue",
                "metadata": {"message_id": "w2-verbose"}}
        if explicit is not None:
            body["verbose_level"] = explicit
        status, receipt = _w2_http(server, "/ask", body)
        assert status == 202, receipt
        request_id = receipt["request_id"]
        request = json.loads((server.paths.inbox / f"{request_id}.json").read_text())
        assert request.get("verbose_level") == explicit
        writer = BufferedChunkStreamWriter(gateway_chunk_path(server.paths, request_id))
        context = SimpleNamespace(request=request, on_chunk=writer, agent=server.agent)
        conversation = GatewayConversationContext(verbose_level="on")
        # 只截在真实writer档位配置之后；HTTP、持久请求和档位分派均用产品代码。
        class Configured(Exception):
            pass
        def after_configure(*_args):
            writer.write_progress({"tool": "read_file", "phase": "started", "status": "开始"}, "开始")
            raise Configured
        monkeypatch.setattr(request_execution.request_history, "gateway_identifier_redactions", lambda *_args: ())
        monkeypatch.setattr(request_execution, "_configure_gateway_approval_session", after_configure)
        with pytest.raises(Configured):
            request_execution._execute_gateway_conversation_turn(context, "读取资料", conversation)
        writer.close()
        status, progress = _w2_http(server, f"/progress/{request_id}")
        assert status == 200
        tools = [row for row in progress["events"] if row.get("kind") == "tool_progress"]
        assert bool(tools) is (expected != "off")
        if tools:
            assert tools[0]["level"] == expected
        assert conversation.verbose_level == "on"
    finally:
        server.stop()

def test_progress_include_validation_and_kind_cannot_be_overridden(tmp_path):
    server = _server(tmp_path)
    server.start()
    try:
        status, receipt = _w2_http(server, "/ask", {"goal": "验证事件", "verbose_level": "stream"})
        assert status == 202
        rid = receipt["request_id"]
        path = gateway_chunk_path(server.paths, rid)
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [{"kind": "assistant_text_delta", "verbose_level": "stream", "model_call_id": "typed-call", "seq": 1, "text": "正文"},
                {"kind": "assistant_text_done", "verbose_level": "stream", "model_call_id": "typed-call", "seq": 1},
                {"kind": "tool_progress", "verbose_level": "full", "progress": {"tool": "read_file", "phase": "started", "kind": "forged"}}]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        code, payload = _w2_http(server, f"/progress/{rid}?include=unknown")
        assert code == 400 and payload["error_code"] == "INVALID_PROGRESS_INCLUDE"
        code, default = _w2_http(server, f"/progress/{rid}")
        assert code == 200 and len(default["events"]) == 1
        assert default["events"][0]["kind"] == "tool_progress"
        code, opted = _w2_http(server, f"/progress/{rid}?include=assistant_text_delta")
        assert code == 200 and [event["kind"] for event in opted["events"]] == ["assistant_text_delta", "assistant_text_done", "tool_progress"]
        code, replay = _w2_http(server, f"/progress/{rid}?since={opted['next']}&include=assistant_text_delta")
        assert code == 200 and replay["events"] == [] and replay["next"] == opted["next"]
    finally:
        server.stop()


def test_workflow_writer_to_http_opt_in_roundtrip(tmp_path):
    server = _server(tmp_path)
    server.start()
    try:
        code, receipt = _w2_http(server, "/ask", {"goal": "订阅步骤", "verbose_level": "stream"})
        assert code == 202
        request_id = receipt["request_id"]
        writer = BufferedChunkStreamWriter(gateway_chunk_path(server.paths, request_id))
        writer.set_verbose_level("stream")
        event = {"workflow_run_id": "run-wire", "workflow_id": "workflow", "step_id": "read",
                 "name": "读取", "status": "running", "duration_ms": 0, "description": "步骤说明",
                 "next_step_ids": ["analyze"], "seq": 1, "private_unknown": "not-public"}
        writer.write_workflow_step_progress(event)
        writer.write_workflow_step_progress(event)
        code, default = _w2_http(server, f"/progress/{request_id}")
        assert code == 200 and default["events"] == [{"kind": "process_settings", "verbose_level": "stream"}] and default["next"] == 2
        code, opted = _w2_http(server, f"/progress/{request_id}?include=workflow_step_progress")
        assert code == 200
        assert opted["events"] == [{"kind": "process_settings", "verbose_level": "stream"},
                                   {**{key: value for key, value in event.items() if key != "private_unknown"},
                                    "kind": "workflow_step_progress"}]
        code, replay = _w2_http(server, f"/progress/{request_id}?include=workflow_step_progress&since={opted['next']}")
        assert code == 200 and replay["events"] == [] and replay["next"] == opted["next"]
        writer.close()
    finally:
        server.stop()


# LLM: 仅替换供应商传输；真实 Agent 负责调用账本、回调组装和 sealed 提交，测试不手工绑定或提交。
# 函数用途: 经真实 generation → writer → HTTP 验证提交前增量与终稿使用同一物理调用编号。
def test_real_generation_delta_before_commit_matches_sealed_call_over_http(tmp_path):
    from agent_py_agent.agent.agent_core.model.call_runtime import model_call_ledger
    from agent_py_agent.agent.backends.base import ModelResponse
    from agent_py_agent.tests.test_cli_run_conversation import _agent, _StaticBackend

    server = _server(tmp_path / "http")
    agent = _agent(tmp_path / "execution")
    writer = None
    observed = {}
    text = "这是供应商回调实际产生的公开正文。" * 80
    server.start()
    try:
        code, receipt = _w2_http(server, "/ask", {"goal": "验证正文绑定", "verbose_level": "stream"})
        assert code == 202
        request_id = receipt["request_id"]
        writer = BufferedChunkStreamWriter(gateway_chunk_path(server.paths, request_id), rich_transcript=True)
        writer.set_verbose_level("stream")

        class Streaming(_StaticBackend):
            def generate(self, prompt, on_chunk=None, **kwargs):
                self.calls += 1
                on_chunk(text[:500])
                observed["code"], observed["before"] = _w2_http(
                    server, f"/progress/{request_id}?include=assistant_text_delta")
                observed["default_code"], observed["default"] = _w2_http(server, f"/progress/{request_id}")
                observed["call_id"] = model_call_ledger(agent).records()[-1].call_id
                on_chunk(text[500:])
                return ModelResponse(text, self.name)

        agent.backend = Streaming()
        result = agent.run("验证正文绑定", source="gateway", request_id=request_id,
                           on_chunk=writer, allowed_tools=[], save=False)
        assert agent.backend.calls == 1 and result.response == text
        assert observed["code"] == observed["default_code"] == 200
        before = [row for row in observed["before"]["events"] if row["kind"] == "assistant_text_delta"]
        assert before, "真实供应商回调在提交前必须已经产生 delta，不能由终稿补造"
        assert {row["model_call_id"] for row in before} == {observed["call_id"]}
        assert not any(row["kind"] == "assistant_text_done" for row in observed["before"]["events"])
        assert not any(row["kind"].startswith("assistant_text_") for row in observed["default"]["events"])
        code, progress = _w2_http(server, f"/progress/{request_id}?include=assistant_text_delta")
        assert code == 200
        text_rows = [row for row in progress["events"] if row["kind"].startswith("assistant_text_")]
        deltas = [row for row in text_rows if row["kind"] == "assistant_text_delta"]
        assert "".join(row["text"] for row in deltas) == text
        assert [row["seq"] for row in deltas] == list(range(len(deltas)))
        sealed = [json.loads(line) for line in writer.chunk_path.read_text().splitlines()
                  if json.loads(line).get("kind") == "assistant_final"]
        assert sealed and sealed[-1]["model_source"]["call_id"] == observed["call_id"]
        assert text_rows[-1] == {"kind": "assistant_text_done", "model_call_id": observed["call_id"],
                                 "seq": len(deltas) - 1}
        assert _w2_http(server, f"/progress/{request_id}?include=assistant_text_delta&since={progress['next']}")[1]["events"] == []
    finally:
        if writer is not None:
            writer.close()
        server.stop()


# LLM: 复用真实工具轮与物理调用账本，不由测试构造 call_id；旧回调在第二次生成中真实重入。
# 函数用途: 验证多轮 generation 自动重绑身份，旧供应商回调不能污染后一次正文。
def test_real_generation_tool_boundary_rebinds_and_rejects_late_callback(tmp_path):
    from agent_py_agent.agent.agent_core.model.call_runtime import model_call_ledger
    from agent_py_agent.agent.backends.base import ModelResponse
    from agent_py_agent.tests.test_cli_run_conversation import _agent, _StaticBackend

    agent = _agent(tmp_path)
    source = agent.home_paths.owner_home_dir / "stream-source.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("真实工具证据", encoding="utf-8")
    writer = _stream(tmp_path)
    observed, callbacks = [], []
    texts = ["先核对当前资料。" * 80, "已核对并完成交付。" * 80]

    class ReadThenReply(_StaticBackend):
        def generate(self, prompt, on_chunk=None, **kwargs):
            self.calls += 1
            callbacks.append(on_chunk)
            current = model_call_ledger(agent).records()[-1].call_id
            on_chunk(texts[self.calls - 1])
            if self.calls == 2:
                before = _events(writer)
                callbacks[0]("SyntheticLateOldCall 不得进入新正文。" * 80)
                assert _events(writer) == before
            observed.append((current, list(_events(writer))))
            if self.calls == 1:
                return ModelResponse(texts[0], self.name, tool_use_blocks=[
                    {"id": "w2-native-read", "name": "read_file", "input": {"path": str(source)}}])
            return ModelResponse(texts[1], self.name)

    agent.backend = ReadThenReply()
    try:
        result = agent.run("读取资料并完成核对", on_chunk=writer, allowed_tools=["read_file"], save=False)
        assert agent.backend.calls == 2 and result.tool_rounds == 1 and result.response == texts[1]
        ids = [call_id for call_id, _rows in observed]
        assert len(set(ids)) == 2
        for call_id, rows in observed:
            assert any(row["kind"] == "assistant_text_delta" and row["model_call_id"] == call_id for row in rows)
        rows = _events(writer)
        assert any(row.get("reason") == "tool_call" and row["model_call_id"] == ids[0] for row in rows)
        assert rows[-1]["kind"] == "assistant_text_done" and rows[-1]["model_call_id"] == ids[1]
        assert "SyntheticLateOldCall" not in writer.chunk_path.read_text()
        assert ids == [record.call_id for record in model_call_ledger(agent).records()]
        assert _read_public_progress_events(writer.chunk_path, 0)[0]
        assert not any(row["kind"].startswith("assistant_text_")
                       for row in _read_public_progress_events(writer.chunk_path, 0)[0])
    finally:
        writer.close()


# ============================================================================
# Source: agent_py_agent/tests/test_v13_w3_analysis_schema_fallback.py
# ============================================================================

MESSAGE = (
    "Upstream request failed: [invalid_request_error] "
    "This response_format type is unavailable now"
)
SCHEMA = {"type": "object", "additionalProperties": True}
ISOLATION = {"requested": "stateless_skill", "mode": "stateless_skill", "note": ""}


def http_error(error, status=400):
    return urllib.error.HTTPError(
        "https://provider.example.test/v1/chat/completions",
        status, "rejected", {},
        BytesIO(json.dumps({"error": error}).encode()),
    )


def rejection(error=None, status=400):
    return gateway._runtime_http_error(http_error(
        error if error is not None else {
            "type": "invalid_request_error", "message": MESSAGE,
        }, status,
    ))


def _w3_host(monkeypatch, outcomes, mode="native"):
    """正式辅助出口及后端组包/解析不替换，只合成传输结果。"""
    backend = OpenAICompatibleBackend(BackendOptions(
        api_base="https://provider.example.test/v1",
        api_key="fixture-key", model_name="fixture-model",
        stream_enabled=False, structured_output=mode,
    ))
    pending, sent = iter(outcomes), []

    def request_json(path, payload, headers, **kwargs):
        sent.append(deepcopy(payload))
        result = next(pending)
        if isinstance(result, BaseException):
            raise result
        return {
            "id": "fixture-response",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": result},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    monkeypatch.setattr(backend, "request_json", request_json)
    agent = SimpleNamespace(
        backend=backend, config=SimpleNamespace(),
        _current_run_params=SimpleNamespace(
            run_id="fixture-run", task_id="fixture-task",
            run_request_id="fixture-request",
            task_attributes={"conversation_thread_id": "fixture-thread"},
        ),
    )
    return agent, sent


def user_text(payload):
    return next(row["content"] for row in payload["messages"] if row["role"] == "user")


@pytest.mark.parametrize("error", [
    {"type": "invalid_request_error", "message": MESSAGE},
    {"param": "response_format", "code": "unsupported_parameter"},
    {"param": "response_format.type", "code": "unsupported_value"},
    {"param": "json_schema", "code": "unsupported_value"},
    {"code": "response_format_unavailable"},
    {"code": "json_schema_not_supported"},
])
def test_response_format_rejection_classified(error):
    exc = rejection(error)
    assert type(exc) is ProviderRequestRejectedError
    assert exc.status_code == 400
    assert exc.error_code == "PROVIDER_REQUEST_REJECTED"
    assert exc.details["rejected_feature"] == "response_format"
    assert "rejection_diagnostic" in exc.details
    assert not is_provider_recoverable_error(exc)


@pytest.mark.parametrize("error", [
    {"type": "invalid_request_error", "message": "bad request"},
    {"param": "messages", "code": "unsupported_parameter"},
    {"param": "response_format", "code": "invalid_json_schema"},
    {"param": "response_format", "code": "invalid_request_error"},
    {"message": "response_format schema unavailable"},
    {"message": "prefix " + MESSAGE},
    {"message": MESSAGE + " suffix"},
    {"message": [MESSAGE]},
    {"param": ["response_format"], "code": "unsupported_value"},
])
def test_other_400_not_classified(error):
    exc = rejection(error)
    assert type(exc) is ProviderRequestRejectedError
    assert "rejected_feature" not in exc.details


@pytest.mark.parametrize("status", [401, 403, 422, 500])
def test_marker_does_not_reclassify_other_http_statuses(status):
    exc = rejection(status=status)
    assert "rejected_feature" not in exc.details


def test_backend_does_not_generically_retry_classified_rejection(monkeypatch):
    calls = []
    monkeypatch.setattr(gateway, "_dump_provider_rejection", lambda exc: None)

    def operation():
        calls.append(True)
        raise http_error({"type": "invalid_request_error", "message": MESSAGE})

    with pytest.raises(ProviderRequestRejectedError) as caught:
        gateway._retry_unlabeled_rejection(operation)
    assert calls == [True]
    assert caught.value.details["rejected_feature"] == "response_format"
    assert not gateway._should_retry_http_error(
        http_error({"message": MESSAGE}), 0, 3,
    )


@pytest.mark.parametrize("mode", ["native", "json_object"])
def test_native_and_explicit_json_object_packaging_unchanged(monkeypatch, mode):
    agent, sent = _w3_host(monkeypatch, ['{"value":8}'], mode)
    context = AnalysisContext(run_inputs={"n": 4})
    assert bootstrap._workflow_analysis_handler(agent)({}, context, ()) == {"value": 8}
    assert len(sent) == 1
    wire = sent[0]
    if mode == "native":
        assert wire["response_format"] == {
            "type": "json_schema",
            "json_schema": {
                "name": "my_agent_structured_output",
                "strict": True, "schema": SCHEMA,
            },
        }
        assert json.loads(user_text(wire))["run_inputs"] == {"n": 4}
    else:
        assert wire["response_format"] == {"type": "json_object"}
        assert "JSON Schema：" + json.dumps(
            SCHEMA, ensure_ascii=False, sort_keys=True,
        ) in user_text(wire)
    assert "tools" not in wire and "tool_choice" not in wire
    assert agent.backend.structured_output == mode
    assert context.analysis_attempts == [{
        "attempt": 1, "response_schema": "native", "outcome": "succeeded",
        "analysis_isolation": ISOLATION,
    }]
    assert context.analysis_isolation == ISOLATION


@pytest.mark.parametrize("mode", ["native", "json_object"])
def test_schema_rejection_downgrades_once_and_keeps_identity(monkeypatch, mode):
    agent, sent = _w3_host(monkeypatch, [rejection(), '{"value":8}'], mode)
    context = AnalysisContext(
        prior_outputs={"prefix": {"value": 1}}, run_inputs={"n": 4},
    )
    assert bootstrap._workflow_analysis_handler(agent)({}, context, ()) == {"value": 8}
    assert len(sent) == 2
    assert "response_format" in sent[0] and "response_format" not in sent[1]
    assert all("tools" not in p and "tool_choice" not in p for p in sent)
    assert agent.backend.structured_output == mode
    if mode == "native":
        assert sent[0]["messages"] == sent[1]["messages"]
    first, second = context.analysis_attempts
    assert first == {
        "attempt": 1, "response_schema": "native", "outcome": "failed",
        "error_type": "ProviderRequestRejectedError",
        "error_code": "PROVIDER_REQUEST_REJECTED",
        "rejected_feature": "response_format",
        "analysis_isolation": ISOLATION,
    }
    assert second == {
        "attempt": 2, "response_schema": None, "outcome": "succeeded",
        "analysis_isolation": ISOLATION,
    }
    assert context.analysis_isolation == ISOLATION
    assert first["analysis_isolation"] is not second["analysis_isolation"]


def test_non_schema_400_is_not_replayed(monkeypatch):
    error = rejection({"type": "invalid_request_error", "message": "bad messages"})
    agent, sent = _w3_host(monkeypatch, [error])
    context = AnalysisContext()
    with pytest.raises(ProviderRequestRejectedError) as caught:
        bootstrap._workflow_analysis_handler(agent)({}, context, ())
    assert caught.value is error and len(sent) == 1
    assert len(context.analysis_attempts) == 1


@pytest.mark.parametrize("fallback", [False, True])
def test_unknown_execution_outcome_is_not_replayed(monkeypatch, fallback):
    error = RequestExecutionOutcomeUncertainError({
        "id": "fixture-request", "execution_attempt_id": "fixture-attempt",
        "lease_epoch": 1, "lease_owner": "fixture-holder",
    }, "fixture")
    outcomes = [rejection(), error] if fallback else [error]
    agent, sent = _w3_host(monkeypatch, outcomes)
    context = AnalysisContext()
    with pytest.raises(RequestExecutionOutcomeUncertainError) as caught:
        bootstrap._workflow_analysis_handler(agent)({}, context, ())
    assert caught.value is error
    assert len(sent) == (2 if fallback else 1)
    assert context.analysis_attempts[-1]["error_code"] == "ACTIVE_TURN_OUTCOME_UNCERTAIN"


def test_timeout_is_not_schema_evidence(monkeypatch):
    error = ProviderTimeoutError("fixture", stage="wall_clock")
    agent, sent = _w3_host(monkeypatch, [error])
    with pytest.raises(ProviderTimeoutError) as caught:
        bootstrap._workflow_analysis_handler(agent)({}, AnalysisContext(), ())
    assert caught.value is error and len(sent) == 1


@pytest.mark.parametrize("text", ["not json", "[]", "null", '"string"', '{"value":NaN}'])
def test_fallback_still_rejects_non_json_or_wrong_type(monkeypatch, text):
    agent, sent = _w3_host(monkeypatch, [rejection(), text])
    context = AnalysisContext()
    with pytest.raises(ValueError):
        bootstrap._workflow_analysis_handler(agent)({}, context, ())
    assert len(sent) == 2
    assert [a["outcome"] for a in context.analysis_attempts] == ["failed", "failed"]


def test_both_rejections_keep_both_attempts(monkeypatch):
    second = rejection()
    agent, sent = _w3_host(monkeypatch, [rejection(), second])
    context = AnalysisContext()
    with pytest.raises(ProviderRequestRejectedError) as caught:
        bootstrap._workflow_analysis_handler(agent)({}, context, ())
    assert caught.value is second and len(sent) == 2
    assert [a["response_schema"] for a in context.analysis_attempts] == ["native", None]
    assert all(a["outcome"] == "failed" for a in context.analysis_attempts)
    assert all(a["rejected_feature"] == "response_format" for a in context.analysis_attempts)


@pytest.mark.parametrize("response", [
    SimpleNamespace(text="{}", tool_use_blocks=[{}]),
    SimpleNamespace(text="{}", tool_protocol_violations=[{}]),
    SimpleNamespace(text="{}", truncated=True),
    SimpleNamespace(text="{}", runtime_status="unknown"),
])
@pytest.mark.parametrize("fallback", [False, True])
def test_tool_incomplete_and_unknown_responses_fail_without_replay(
    monkeypatch, response, fallback,
):
    agent, _ = _w3_host(monkeypatch, [])
    calls = []

    def generate(request):
        calls.append(request)
        assert request.tools is None and request.tool_choice is None
        if fallback and len(calls) == 1:
            raise rejection()
        return response

    monkeypatch.setattr(bootstrap, "generate_auxiliary_model_response", generate)
    context = AnalysisContext()
    with pytest.raises(ValueError):
        bootstrap._workflow_analysis_handler(agent)({}, context, ())
    assert len(calls) == (2 if fallback else 1)
    assert context.analysis_attempts[-1]["outcome"] == "failed"


def test_cancel_between_attempts_prevents_second_call(monkeypatch):
    agent, _ = _w3_host(monkeypatch, [])
    token, calls, context = CancellationToken(), [], AnalysisContext()

    def generate(request):
        calls.append(request)
        token.cancel("fixture-stop")
        raise rejection()

    monkeypatch.setattr(bootstrap, "generate_auxiliary_model_response", generate)
    with bind_cancellation_token(token), pytest.raises(ToolCancelled):
        bootstrap._workflow_analysis_handler(agent)({}, context, ())
    assert len(calls) == 1
    assert len(context.analysis_attempts) == 1
    assert context.analysis_attempts[0]["rejected_feature"] == "response_format"


def test_interrupt_between_attempts_prevents_second_call(monkeypatch):
    agent, _ = _w3_host(monkeypatch, [])
    state, calls = {"stopped": False}, []
    monkeypatch.setattr(bootstrap, "is_interrupted", lambda: state["stopped"])

    def generate(request):
        calls.append(request)
        state["stopped"] = True
        raise rejection()

    monkeypatch.setattr(bootstrap, "generate_auxiliary_model_response", generate)
    with pytest.raises(InterruptedError):
        bootstrap._workflow_analysis_handler(agent)({}, AnalysisContext(), ())
    assert len(calls) == 1


@pytest.mark.parametrize("tail", ['{"value":8}', "not json", "[]", None])
def test_engine_preserves_prefix_and_persists_attempts(tmp_path, monkeypatch, tail):
    """真实两步引擎：第二步内部降级不重新执行成功前缀，双失败也落账。"""
    ending = rejection() if tail is None else tail
    agent, sent = _w3_host(monkeypatch, ['{"value":1}', rejection(), ending])
    definition = {
        "descriptor": {
            "id": "schema-flow", "name": "schema-flow", "kind": "workflow",
            "access_targets": [], "data_sources": [],
            "credential_refs": [], "io_contract": {},
        },
        "steps": [
            {"step_id": name, "name": name, "mode": "llm_skill_analysis"}
            for name in ("prefix", "tail")
        ],
    }
    engine = WorkflowEngine(
        tmp_path, analysis_handler=bootstrap._workflow_analysis_handler(agent),
    )
    run = engine.run_workflow(definition)
    expected = "succeeded" if tail == '{"value":8}' else "failed"
    assert run.status == expected
    assert len(sent) == 3
    assert [json.loads(user_text(p))["step"]["step_id"] for p in sent] == [
        "prefix", "tail", "tail",
    ]
    prefix, last = run.steps
    assert prefix.status == "succeeded" and prefix.output_snapshot == {"value": 1}
    assert last.status == expected
    attempts = last.escalation_result["analysis_attempts"]
    assert [a["response_schema"] for a in attempts] == ["native", None]
    assert attempts[0]["error_code"] == "PROVIDER_REQUEST_REJECTED"
    assert attempts[0]["rejected_feature"] == "response_format"
    assert attempts[1]["outcome"] == expected
    persisted = engine.store.load_run(run.workflow_run_id)
    assert persisted.steps[0].to_dict() == prefix.to_dict()
    assert persisted.steps[1].escalation_result["analysis_attempts"] == attempts
