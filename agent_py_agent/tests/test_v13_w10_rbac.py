"""W10角色和账号治理的真实持久化与权限回归。"""
import hashlib
import json
from uuid import uuid4

from agent_py_agent.tests.test_v13_w10_identity import client, failure, seed_user, success


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
    failure(invoke('admin.users.list', {'limit': 2, 'cursor': first['next_cursor']}), 503, 'CURSOR_EXPIRED')
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
