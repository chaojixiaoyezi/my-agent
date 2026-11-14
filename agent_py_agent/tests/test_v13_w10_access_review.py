"""#90 原生扫描、真实W0持久化与Gateway HTTP往返测试。"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.business_spaces import v13_w10_access_review as review
from agent_py_agent.agent.business_spaces import v13_w10_identity as identity
from agent_py_agent.agent.gateway_parts.business_api import local_business_context
from agent_py_agent.agent.gateway_parts.business_api_v13 import V13Call
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.session.manager import SessionManager
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.local_user_lifecycle import LocalUserLifecycle
from agent_py_agent.agent.user_space.local_user_store import LocalUserStore
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    resolve_owner_home,
)
from agent_py_agent.tests.test_v13_w0_harness import v13_call

pytest_plugins = ('agent_py_agent.tests.test_v13_w0_harness',)


# LLM: HOME与MY_AGENT_HOME都被隔离，认证上下文、业务存储和来源全部走真实实现。
# 函数用途: 构造带真实本地账号的测试环境。
@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path))
    root = tmp_path / 'native'
    native = LocalUserStore(root)
    lifecycle = LocalUserLifecycle(native)
    lifecycle.create('alice', idempotency_key='review-create-alice')
    clock = [1800000000.0]

    # LLM: 上下文由真实local_business_context签发，不伪造角色或业务响应。
    # 函数用途: 构造调用。
    def call(action, params=None, *, owner='local/main', key=None, revision=None):
        native_owner = OwnerIdentity.local_main() if owner == 'local/main' else OwnerIdentity.local_user(owner.removeprefix('local/'))
        paths = home_paths_with_owner(home_paths(root), resolve_owner_home(root, native_owner))
        host = SimpleNamespace(home_paths=paths, config=SimpleNamespace(business_spaces_enabled=True))
        context, services = local_business_context(host)
        return V13Call(action=action, params=params or {}, owner_id=context.owner_id, subject_id=context.subject_id,
            request_id='review-request', idempotency_key=key or secrets.token_hex(16), expected_revision=revision,
            owner_home=paths.owner_home_dir, clock=lambda: clock[0], services=services, context=context)

    return SimpleNamespace(root=root, native=native, lifecycle=lifecycle, clock=clock, call=call,
        home=native.owner_home('alice').home_dir)


# LLM: 构造标准UTC源字段而非依赖机器当前时间。
# 函数用途: 生成测试时间。
def iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat().replace('+00:00', 'Z')


# LLM: 生产扫描只读，源记录由真实RuntimeRepository与SessionManager准备。
# 函数用途: 建立已追平且可核实的活动源。
def activity(env, stamp):
    env.home.mkdir(parents=True, exist_ok=True)
    repository = RuntimeRepository(env.home / 'runtime.db')
    repository.record_run_creation(owner_id='local/alice', run_id=secrets.token_hex(16))
    with repository.transaction() as connection:
        connection.execute('UPDATE task_runs SET created_at=?', (stamp,))
        connection.execute('UPDATE agent_runs SET created_at=?', (stamp,))
    manager = SessionManager(SimpleNamespace(session_workspace=env.home / 'sessions', user_id='alice'))
    session = manager.create_session()
    session.created_at = stamp
    session.updated_at = stamp
    manager.save_session(session)
    assert manager.list_page()['coverage'] == 'complete'


# LLM: 通过原native tokens.json生产元数据，不给扫描器预填发现。
# 函数用途: 写入测试推送令牌源。
def inbox(env, records):
    path = env.root / 'remote_push' / 'tokens.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'tokens': records}))


# LLM: 测试令牌只包含虚拟验证器，产品必须剥除秘密字段。
# 函数用途: 构造原生令牌元数据。
def token(env, token_id='stale', *, used=..., expires=None, enabled=True):
    return {'token_id': token_id, 'owner_id': 'local/main', 'enabled': enabled,
        'created_at': env.clock[0] - 100 * 86400,
        'last_used_at': env.clock[0] - 100 * 86400 if used is ... else used, 'expires_at': expires,
        'token_sha256': 'test-private-verifier'}


# LLM: 分类不触发扫描或写入。
# 函数用途: 验证#90-1目录。
def test_kinds_static_catalog(env):
    result = review.kinds(env.call('access_review.kinds'))
    assert {row['kind'] for row in result['kinds']} == {'dormant_account', 'stale_token', 'privilege_drift'}
    assert identity.rows(env.call('access_review.kinds'), 'access_findings', 'main') == []


# LLM: 原文provider=all例子从未运行时必须unknown，不是complete空态。
# 函数用途: 验证#90-2汇总示例。
def test_summary_example_provider_all(env):
    result = review.summary(env.call('access_review.summary', {'provider': 'all'}))
    assert result == {'provider': 'all', 'last_run_at': None, 'open': 0,
        'by_kind': {}, 'by_severity': {}, 'coverage': 'unknown'}


# LLM: 创建账号、运行、会话和推送令牌，发现必须由扫描真实产生。
# 函数用途: 验证#90-3/4扫描、幂等upsert及自动解决。
def test_run_creates_findings_from_real_sources(env):
    activity(env, env.clock[0] - 100 * 86400)
    request = env.call('access_review.run', {'provider': 'local'})
    first = review.run(request)
    assert first['run']['findings_created'] == 1 and first['run']['coverage'] == 'complete', (
        first, review._activity(env.home, 'local/alice', env.clock[0]), review._session_activity(env.home))
    assert review.run(request) == first
    listed = review.findings(env.call('access_review.findings.list', {'provider': 'local'}))['items']
    assert len(listed) == 1 and listed[0]['kind'] == 'dormant_account'
    assert review.run(env.call('access_review.run', {'provider': 'local'}))['run']['findings_created'] == 0
    activity(env, env.clock[0] - 10)
    assert review.run(env.call('access_review.run', {'provider': 'local'}))['run']['findings_resolved'] == 1
    assert review.findings(env.call('access_review.findings.list'))['items'][0]['status'] == 'resolved'
    assert identity.rows(env.call('access_review.summary'), 'audit_log')


# LLM: 账号创建时间很旧也不能代替未知活动；部分覆盖不得解决旧发现。
# 函数用途: 验证缺源partial且无休眠误报。
def test_unknown_activity_is_not_created_at_and_missing_source_preserves_finding(env):
    first = review.run(env.call('access_review.run', {'provider': 'local'}))
    assert first['run']['coverage'] == 'partial' and first['run']['findings_created'] == 0
    activity(env, env.clock[0] - 100 * 86400)
    assert review.run(env.call('access_review.run', {'provider': 'local'}))['run']['findings_created'] == 1
    (env.home / 'runtime.db').rename(env.home / 'runtime-missing.db')
    assert review.run(env.call('access_review.run', {'provider': 'local'}))['run']['findings_resolved'] == 0
    assert review.findings(env.call('access_review.findings.list'))['items'][0]['status'] == 'open'


# LLM: expired enabled与90天边界、停用排除、秘密剥离都用原生推送源验证。
# 函数用途: 验证stale_token来源和边界。
def test_native_inbox_expiry_and_stale_boundary(env):
    rows = [token(env), token(env, 'expired', used=env.clock[0] - 1, expires=env.clock[0] - 1),
        token(env, 'boundary', used=env.clock[0] - 90 * 86400),
        token(env, 'recent', used=env.clock[0] - 90 * 86400 + 1), token(env, 'disabled', enabled=False)]
    inbox(env, rows)
    result = review.run(env.call('access_review.run', {'provider': 'inbox_tokens'}))
    assert result['run']['findings_created'] == 3 and result['run']['coverage'] == 'complete'
    listed = review.findings(env.call('access_review.findings.list'))['items']
    assert {row['subject'] for row in listed} == {'local/main:stale', 'local/main:expired', 'local/main:boundary'}
    assert 'test-private-verifier' not in repr(listed)


# LLM: 确认与标记不实际回收；CAS、重放和源关闭后的resolved连成完整序列。
# 函数用途: 验证#90-5两种确认操作。
def test_acknowledge_and_mark_revocation_cas(env):
    rows = [token(env)]
    inbox(env, rows)
    review.run(env.call('access_review.run', {'provider': 'inbox_tokens'}))
    finding = review.findings(env.call('access_review.findings.list'))['items'][0]
    request = env.call('access_review.findings.acknowledge', {'finding_id': finding['finding_id'], 'note': '待核对'}, revision=1)
    result = review.acknowledge(request)
    assert result['finding']['status'] == 'acknowledged' and review.acknowledge(request) == result
    with pytest.raises(HostAPIError, match='REVISION_CONFLICT'):
        review.acknowledge(env.call('access_review.findings.acknowledge', request.params, revision=1))
    marked = review.acknowledge(env.call('access_review.findings.acknowledge',
        {'finding_id': finding['finding_id'], 'decision': 'mark_revocation'}, revision=2))
    assert marked['finding']['status'] == 'revocation_marked'
    assert json.loads((env.root / 'remote_push' / 'tokens.json').read_text())['tokens'][0]['enabled'] is True
    rows[0]['enabled'] = False
    inbox(env, rows)
    assert review.run(env.call('access_review.run', {'provider': 'inbox_tokens'}))['run']['findings_resolved'] == 1
    assert review.summary(env.call('access_review.summary', {'provider': 'inbox_tokens'}))['open'] == 0


# LLM: 只经真实事务种入源记录，不手写W0集合文件或替换扫描输出。
# 函数用途: 为各原生集合准备可追溯测试数据。
def seed(env, collection, records, *, owner='local/main', scope='main'):
    # LLM: 种入与业务提交沿同一W0端口，测试只控制来源数据。
    # 函数用途: 合并测试记录。
    def change(rows):
        rows.update(records)
        return {'count': len(rows)}

    action = 'api_keys.create' if collection == 'api_keys' else 'access_review.run'
    identity.write(env.call(action, owner=owner), collection, change, scope=scope)


# LLM: API keys跨owner只读，SCIM使用main scope；两者的无用/过期规则不能靠mock通过。
# 函数用途: 验证真实W0令牌集合扫描。
def test_api_keys_and_scim_real_collections(env):
    now = env.clock[0]
    for owner in ('local/main', 'local/alice'):
        row = {'key_id': 'key-old', 'owner_id': owner, 'enabled': True,
            'created_at': iso(now - 100 * 86400), 'last_used_at': None, 'expires_at': None,
            'secret_sha256': 'private-not-for-findings'}
        seed(env, 'api_keys', {'key-old': row}, owner=owner, scope='owner')
    seed(env, 'scim_tokens', {'scim-old': {'token_id': 'scim-old', 'owner_id': 'local/main', 'enabled': True,
        'created_at': iso(now - 10), 'last_used_at': iso(now - 1), 'expires_at': iso(now - 1)}})
    assert review.run(env.call('access_review.run', {'provider': 'api_keys'}))['run']['findings_created'] == 2
    assert review.run(env.call('access_review.run', {'provider': 'scim'}))['run']['findings_created'] == 1
    assert review.summary(env.call('access_review.summary', {'provider': 'api_keys'}))['coverage'] == 'complete'
    assert review.summary(env.call('access_review.summary', {'provider': 'scim'}))['coverage'] == 'complete'
    assert 'private-not-for-findings' not in repr(review.findings(env.call('access_review.findings.list')))


# LLM: 首次仅建基线；后续权限或审计组变化以事实差异生成一次drift。
# 函数用途: 验证角色权限、分配及原生审计组基线。
def test_privilege_drift_role_permissions_and_audit_baseline(env):
    activity(env, env.clock[0] - 10)
    role = {'role_id': 'analyst', 'permissions': ['access_review.summary']}
    seed(env, 'rbac_roles', {'analyst': role})
    seed(env, 'rbac_assignments', {'alice': {'user_id': 'alice', 'roles': ['analyst']}})
    assert review.run(env.call('access_review.run', {'provider': 'local'}))['run']['findings_created'] == 0
    role['permissions'] += ['access_review.kinds']
    seed(env, 'rbac_roles', {'analyst': role})
    run = review.run(env.call('access_review.run', {'provider': 'local'}))['run']
    assert run['findings_created'] == 1
    finding = review.findings(env.call('access_review.findings.list', {'kind': 'privilege_drift'}))['items'][0]
    assert finding['evidence']['before']['permissions'] == ['access_review.summary']
    assert finding['evidence']['after']['permissions'] == ['access_review.kinds', 'access_review.summary']
    assert review.run(env.call('access_review.run', {'provider': 'local'}))['run']['findings_resolved'] == 1
    env.lifecycle.set_audit_group_membership('alice', in_audit_group=True)
    review.run(env.call('access_review.run', {'provider': 'local'}))
    finding = review.findings(env.call('access_review.findings.list', {'status': 'open'}))['items'][0]
    assert finding['kind'] == 'privilege_drift' and finding['evidence']['after']['in_audit_group'] is True


# LLM: 重复编号、坏时间和数量级异常不进入正常排名或自动消失判定。
# 函数用途: 验证脏源单列partial、provider隔离与未知来源。
def test_dirty_sources_and_provider_scoping(env):
    inbox(env, [token(env), token(env), token(env, 'bad-time', used='not-a-timestamp'),
        token(env, 'future', used=10 ** 100)])
    run = review.run(env.call('access_review.run', {'provider': 'inbox_tokens'}))['run']
    assert run['coverage'] == 'partial' and run['findings_created'] == 0
    run = review.run(env.call('access_review.run', {'provider': 'feishu'}))['run']
    assert run['coverage'] == 'partial'
    assert review.summary(env.call('access_review.summary', {'provider': 'local'}))['coverage'] == 'unknown'
    assert review.summary(env.call('access_review.summary', {'provider': 'all'}))['last_run_at'] is not None
    assert review.summary(env.call('access_review.summary', {'provider': 'all'}))['coverage'] == 'partial'


# LLM: 写回调任意位置失败时，两集合都不能暴露半笔结果。
# 函数用途: 验证真实transaction_many回滚和幂等冲突。
def test_atomic_run_failure_rolls_back_both_collections(env, monkeypatch):
    inbox(env, [token(env)])
    original = review._upsert

    # LLM: 仅注入提交前故障，不改断言或W0行为。
    # 函数用途: 在发现变更之后抛错。
    def failed(call, rows, candidates, covered, blocked, stamp):
        original(call, rows, candidates, covered, blocked, stamp)
        raise RuntimeError('test-before-commit')

    monkeypatch.setattr(review, '_upsert', failed)
    request = env.call('access_review.run', {'provider': 'inbox_tokens'})
    with pytest.raises(RuntimeError, match='test-before-commit'):
        review.run(request)
    for collection in ('access_findings', 'access_review_runs'):
        assert identity.rows(request, collection, 'main') == []
    monkeypatch.setattr(review, '_upsert', original)
    assert review.run(request)['run']['findings_created'] == 1
    with pytest.raises(Exception) as error:
        review.run(env.call('access_review.run', {'provider': 'local'}, key=request.idempotency_key))
    assert error.value.code == 'IDEMPOTENCY_CONFLICT'


# LLM: 本地原生权限也不可由业务角色提升，错误必须带准确状态码。
# 函数用途: 验证所有动作重复执行管理员门。
@pytest.mark.parametrize('action,handler,params', [
    ('access_review.kinds', review.kinds, {}), ('access_review.summary', review.summary, {}),
    ('access_review.findings.list', review.findings, {}), ('access_review.run', review.run, {}),
    ('access_review.findings.acknowledge', review.acknowledge, {'finding_id': 'not-here'})])
def test_admin_required_for_every_action(env, action, handler, params):
    with pytest.raises(HostAPIError) as error:
        handler(env.call(action, params, owner='local/alice', revision=0))
    assert (error.value.code, error.value.status) == ('BUSINESS_ACCESS_DENIED', 403)


# LLM: 真实Gateway使用共享harness启动；默认admin是原生AuthManager配置，不使用角色mock。
# 函数用途: 验证#90-6及创建、确认、标记、解决的HTTP全链路。
def test_envelopes(v13_native):
    server, agent = v13_native
    source = SimpleNamespace(root=agent.home_paths.root)
    current = time.time()
    rows = [{'token_id': 'wire-one', 'owner_id': 'local/main', 'enabled': True,
        'created_at': current - 100 * 86400, 'last_used_at': current - 100 * 86400, 'expires_at': None},
        {'token_id': 'wire-two', 'owner_id': 'local/main', 'enabled': True,
        'created_at': current - 100 * 86400, 'last_used_at': current - 100 * 86400, 'expires_at': None}]
    inbox(source, rows)

    # LLM: 每次都走真实HTTP与标准信封，schema由dispatcher验证。
    # 函数用途: 调用并核验成功响应。
    def wire(action, params, *, key=None, revision=None):
        status, body = v13_call(server, action, params, user='admin', idempotency_key=key,
            expected_revision=revision)
        assert status == 200, body
        assert body['state'] == 'completed' and body['error'] is None, body
        assert set(body) == {'request_id', 'operation_id', 'state', 'data', 'error',
            'allowed_actions', 'warnings', 'effect_outcome'}
        return body['data']

    assert len(wire('access_review.kinds', {})['kinds']) == 3
    assert wire('access_review.summary', {'provider': 'all'})['coverage'] == 'unknown'
    key = secrets.token_hex(16)
    first = wire('access_review.run', {'provider': 'inbox_tokens'}, key=key)
    assert first['run']['findings_created'] == 2 and first['run']['coverage'] == 'complete'
    assert wire('access_review.run', {'provider': 'inbox_tokens'}, key=key) == first
    page = wire('access_review.findings.list', {'limit': 1})
    assert len(page['items']) == 1 and page['next_cursor']
    second = wire('access_review.findings.list', {'limit': 1, 'cursor': page['next_cursor']})
    assert len(second['items']) == 1 and second['items'][0]['finding_id'] != page['items'][0]['finding_id']
    finding = page['items'][0]
    ack = wire('access_review.findings.acknowledge', {'finding_id': finding['finding_id']},
        key=secrets.token_hex(16), revision=finding['revision'])['finding']
    assert ack['status'] == 'acknowledged'
    mark = wire('access_review.findings.acknowledge', {'finding_id': finding['finding_id'], 'decision': 'mark_revocation'},
        key=secrets.token_hex(16), revision=ack['revision'])['finding']
    assert mark['status'] == 'revocation_marked'
    assert all(row['enabled'] for row in json.loads((source.root / 'remote_push' / 'tokens.json').read_text())['tokens'])
    for row in rows:
        row['enabled'] = False
    inbox(source, rows)
    result = wire('access_review.run', {'provider': 'inbox_tokens'}, key=secrets.token_hex(16))
    assert result['run']['findings_resolved'] == 2
    assert wire('access_review.summary', {'provider': 'inbox_tokens'})['open'] == 0
    assert len(wire('access_review.findings.list', {'status': 'resolved'})['items']) == 2
    for action, params, revision, expected, code in [
        ('access_review.findings.acknowledge', {'finding_id': finding['finding_id']}, 0, 409, 'REVISION_CONFLICT'),
        ('access_review.findings.acknowledge', {'finding_id': 'not-here'}, 0, 404, 'RESOURCE_NOT_FOUND'),
        ('access_review.run', {'provider': 'local'}, None, 409, 'IDEMPOTENCY_CONFLICT'),
        ('access_review.findings.list', {'cursor': 'invalid'}, None, 503, 'CURSOR_EXPIRED'),
        ('access_review.kinds', {'extra': True}, None, 400, 'INVALID_SCHEMA')]:
        status, body = v13_call(server, action, params, user='admin',
            idempotency_key=key if action == 'access_review.run' else secrets.token_hex(16), expected_revision=revision)
        assert status == expected and body['error']['code'] == code, body
    status, body = v13_call(server, 'access_review.kinds', {}, user='ordinary-reviewer')
    assert status == 403 and body['error']['code'] == 'BUSINESS_ACCESS_DENIED', body


# LLM: 读运行、会话和账号canonical不改源字节，写入仅在主租户业务存储。
# 函数用途: 验证原生来源零内容变更与缺源不物化。
def test_source_reads_do_not_modify_native_files(env):
    activity(env, env.clock[0] - 100 * 86400)
    sources = [env.home / 'runtime.db', env.home / '.sessions-session-list.sqlite3',
        *list((env.home / 'sessions').glob('*/session.json')), env.root / 'local_users' / 'alice.json']
    before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    review.run(env.call('access_review.run', {'provider': 'local'}))
    after = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    assert before == after
    home = env.native.owner_home('unknown-no-source').home_dir
    assert review._activity(home, 'local/unknown-no-source', env.clock[0]) == (None, False)
    assert not home.exists()


# LLM: 已知库中的未来、无穷或畸形时间不能降级为90天未活动。
# 函数用途: 验证时间异常保留未知。
@pytest.mark.parametrize('value', [None, True, float('nan'), float('inf'), -(10 ** 100), 10 ** 10000,
    '2026-10-01', 'bad-timestamp'], ids=['null', 'bool', 'nan', 'inf', 'negative', 'overflow', 'no-zone', 'invalid'])
def test_invalid_timestamp_is_unknown(value):
    assert review._epoch(value) is None


# LLM: 真实SQL的非null非法MAX不能与空结果等价，旧会话不能掩盖坏运行时间。
# 函数用途: 验证坏活动源抑制休眠误报且保留旧发现。
@pytest.mark.parametrize('bad', ['bad-timestamp', '2026-10-01', -100])
def test_invalid_runtime_max_blocks_dormant_finding(env, bad):
    activity(env, env.clock[0] - 100 * 86400)
    repository = RuntimeRepository(env.home / 'runtime.db')
    with repository.transaction() as connection:
        connection.execute('UPDATE task_runs SET created_at=?', (bad,))
        connection.execute('UPDATE agent_runs SET created_at=?', (bad,))
    assert review._runtime_activity(env.home, 'local/alice') == (None, False)
    result = review.run(env.call('access_review.run', {'provider': 'local'}))['run']
    assert result['coverage'] == 'partial' and result['findings_created'] == 0
    activity(env, env.clock[0] - 100 * 86400)
    assert review.run(env.call('access_review.run', {'provider': 'local'}))['run']['findings_created'] == 1
    with repository.transaction() as connection:
        connection.execute('UPDATE task_runs SET created_at=?', (bad,))
        connection.execute('UPDATE agent_runs SET created_at=?', (bad,))
    result = review.run(env.call('access_review.run', {'provider': 'local'}))['run']
    assert result['coverage'] == 'partial' and result['findings_resolved'] == 0
    assert review.findings(env.call('access_review.findings.list'))['items'][0]['status'] == 'open'


# LLM: 空SQL源是已知无记录，不同owner的真实活动不能变成本owner时间。
# 函数用途: 验证空活动查询与非法值有不同覆盖结论。
def test_runtime_no_matching_rows_is_complete(env):
    activity(env, env.clock[0] - 100 * 86400)
    assert review._runtime_activity(env.home, 'local/other') == (None, True)


# LLM: 无使用追踪时创建时间只说明年龄；停用和已到期规则仍然独立有效。
# 函数用途: 验证inbox未知时间、到期与旧发现保留。
@pytest.mark.parametrize('missing', [True, False], ids=['absent', 'null'])
def test_inbox_without_usage_is_partial_not_idle(env, missing):
    inbox(env, [token(env)])
    review.run(env.call('access_review.run', {'provider': 'inbox_tokens'}))
    unknown = token(env, used=None)
    if missing:
        unknown.pop('last_used_at')
    inbox(env, [unknown, token(env, 'expired', used=None, expires=env.clock[0] - 1),
        token(env, 'disabled', used=None, enabled=False)])
    result = review.run(env.call('access_review.run', {'provider': 'inbox_tokens'}))['run']
    assert result['coverage'] == 'partial'
    assert result['findings_created'] == 1 and result['findings_resolved'] == 0
    listed = review.findings(env.call('access_review.findings.list'))['items']
    assert {row['subject'] for row in listed} == {'local/main:stale', 'local/main:expired'}
    assert all(row['status'] == 'open' for row in listed)


# LLM: 原生推送接收成功仍不回写last_used_at，扫描不能把活跃老令牌判为闲置。
# 函数用途: 经真实HTTP推送再审查，复现原生产链事实缺口。
def test_native_push_then_review_keeps_usage_unknown(v13_native):
    from agent_py_agent.tests.test_wave9_remote_agent_push import post, signed
    server, agent = v13_native
    source = SimpleNamespace(root=agent.home_paths.root, clock=[time.time()])
    secret = 'review-test-push-' + secrets.token_hex(32)
    record = token(source, 'actively-used', used=None)
    record.pop('last_used_at')
    record.update(token_sha256=hashlib.sha256(secret.encode()).hexdigest(),
        allowed_target_owners=['local/main'], allowed_actions=['push_event'], allowed_source_ips=[])
    inbox(source, [record])
    raw, headers = signed({'target_owner': 'local/main', 'instruction': '记录测试事件',
        'idempotency_key': secrets.token_hex(16)}, secret)
    status, receipt = post(server, raw, headers)
    assert status == 202 and receipt['status'] == 'accepted', receipt
    assert list(server.paths.inbox.glob('*.json'))
    persisted = json.loads((source.root / 'remote_push' / 'tokens.json').read_text())['tokens'][0]
    assert 'last_used_at' not in persisted
    status, body = v13_call(server, 'access_review.run', {'provider': 'inbox_tokens'}, user='admin',
        idempotency_key=secrets.token_hex(16))
    assert status == 200 and body['error'] is None, body
    assert body['data']['run']['coverage'] == 'partial'
    assert body['data']['run']['findings_created'] == 0


# LLM: 通过审计核对每次真实W0提交边界，不以总数正确掩盖超限或重复提交。
# 函数用途: 取得发现集合的逐事务变更编号。
def finding_commits(request):
    return [row['record_ids'] for row in identity.rows(request, 'audit_log')
        if row['collection'] == 'access_findings']


# LLM: 阈值两侧均覆盖创建、更新、解决和幂等重放；来源超过分页大小仍须全部审查。
# 函数用途: 验证100条边界及多批真实存储提交。
@pytest.mark.parametrize('count', [100, 101, 205])
def test_large_review_batches_create_refresh_resolve(env, count):
    rows = [token(env, f'token-{index}') for index in range(count)]
    inbox(env, rows)
    request = env.call('access_review.run', {'provider': 'inbox_tokens'})
    first = review.run(request)
    assert first['run']['findings_created'] == count and first['run']['coverage'] == 'complete'
    commits = finding_commits(request)
    assert len(commits) == (count + 99) // 100 and all(len(ids) <= 100 for ids in commits)
    assert len({identifier for ids in commits for identifier in ids}) == count
    assert review.run(request) == first and finding_commits(request) == commits
    env.clock[0] += 1
    result = review.run(env.call('access_review.run', {'provider': 'inbox_tokens'}))['run']
    assert result['findings_created'] == result['findings_resolved'] == 0
    findings = identity.rows(request, 'access_findings', 'main')
    assert len(findings) == count and all(row['revision'] == 2 for row in findings)
    for row in rows:
        row['enabled'] = False
    inbox(env, rows)
    result = review.run(env.call('access_review.run', {'provider': 'inbox_tokens'}))['run']
    assert result['findings_resolved'] == count
    assert all(row['status'] == 'resolved' for row in identity.rows(request, 'access_findings', 'main'))
    assert all(len(ids) <= 100 for ids in finding_commits(request))


# LLM: 第一个批次耐久可见，未处理项不提前改变；同键只恢复冻结计划、不重扫新来源。
# 函数用途: 验证创建、更新和解决阶段的中断恢复与意图冲突。
@pytest.mark.parametrize('phase', ['create', 'refresh', 'resolve'])
def test_large_review_interruption_resumes_frozen_plan(env, monkeypatch, phase):
    rows = [token(env, f'token-{index}') for index in range(205)]
    inbox(env, rows)
    if phase != 'create':
        review.run(env.call('access_review.run', {'provider': 'inbox_tokens'}))
    if phase == 'resolve':
        for row in rows:
            row['enabled'] = False
        inbox(env, rows)
    env.clock[0] += 1
    request = env.call('access_review.run', {'provider': 'inbox_tokens'})
    original = review._advance_run

    # LLM: 只在第二批应用前故障，真实第一批已沿W0完成提交。
    # 函数用途: 注入可确定复现的批间中断。
    def interrupted(call, collections, row):
        if row['baseline']['progress']['cursor'] == 100:
            raise RuntimeError('second-batch-interrupted')
        return original(call, collections, row)

    monkeypatch.setattr(review, '_advance_run', interrupted)
    with pytest.raises(RuntimeError, match='second-batch-interrupted'):
        review.run(request)
    runs = identity.rows(request, 'access_review_runs', 'main')
    pending = [row for row in runs if row['state'] == 'running']
    assert len(pending) == 1 and pending[0]['baseline']['progress']['cursor'] == 100
    current = identity.rows(request, 'access_findings', 'main')
    if phase == 'create':
        assert len(current) == 100
        assert review.summary(env.call('access_review.summary', {'provider': 'inbox_tokens'}))['coverage'] == 'unknown'
    elif phase == 'refresh':
        assert sum(row['revision'] == 2 for row in current) == 100
        assert sum(row['revision'] == 1 for row in current) == 105
    else:
        assert sum(row['status'] == 'resolved' for row in current) == 100
        assert sum(row['status'] == 'open' for row in current) == 105
    with pytest.raises(HostAPIError, match='IDEMPOTENCY_CONFLICT'):
        review.run(env.call('access_review.run', {'provider': 'local'}, key=request.idempotency_key))
    with pytest.raises(HostAPIError, match='STATE_CONFLICT'):
        review.run(env.call('access_review.run', {'provider': 'inbox_tokens'}))
    if phase == 'refresh':
        untouched = next(row for row in current if row['revision'] == 1)
        review.acknowledge(env.call('access_review.findings.acknowledge',
            {'finding_id': untouched['finding_id'], 'note': '批间确认'}, revision=1))
    (env.root / 'remote_push' / 'tokens.json').unlink()
    monkeypatch.setattr(review, '_advance_run', original)
    recovered = review.run(env.call('access_review.run', request.params, key=request.idempotency_key))
    assert recovered['run']['state'] == 'completed' and recovered['run']['coverage'] == 'complete'
    assert recovered['run']['findings_created'] == (205 if phase == 'create' else 0)
    assert recovered['run']['findings_resolved'] == (205 if phase == 'resolve' else 0)
    final = identity.rows(request, 'access_findings', 'main')
    assert len(final) == 205
    if phase == 'refresh':
        preserved = next(row for row in final if row['finding_id'] == untouched['finding_id'])
        assert preserved['status'] == 'acknowledged' and preserved['note'] == '批间确认'
    assert review.run(request) == recovered
    with pytest.raises(HostAPIError, match='IDEMPOTENCY_CONFLICT'):
        review.run(env.call('access_review.run', request.params, key=request.idempotency_key, revision=7))
    assert all(len(ids) <= 100 for ids in finding_commits(request))


# LLM: 日志替换后物化失败是已提交批次，重新进入必须滚前而非再创建同一批发现。
# 函数用途: 验证W0提交点之后中断的精确恢复和幂等计数。
def test_large_review_recovers_committed_journal(env, monkeypatch):
    from agent_py_agent.agent.business_spaces import v13_store
    inbox(env, [token(env, f'token-{index}') for index in range(205)])
    request = env.call('access_review.run', {'provider': 'inbox_tokens'})
    original = v13_store.write_bytes_atomic_beneath
    writes = [0]

    # LLM: 第二批日志已存在时阻断发现物化，不伪造transaction_many的返回值。
    # 函数用途: 注入提交后I/O故障。
    def interrupted(root, parts, payload):
        if parts[-1] == 'access_findings.json':
            writes[0] += 1
            if writes[0] == 2:
                raise OSError('second-batch-materialization')
        return original(root, parts, payload)

    monkeypatch.setattr(v13_store, 'write_bytes_atomic_beneath', interrupted)
    with pytest.raises(OSError, match='second-batch-materialization'):
        review.run(request)
    pending = identity.rows(request, 'access_review_runs', 'main')[0]
    assert pending['state'] == 'running' and pending['baseline']['progress']['cursor'] == 200
    assert len(identity.rows(request, 'access_findings', 'main')) == 200
    with pytest.raises(HostAPIError, match='IDEMPOTENCY_CONFLICT'):
        review.run(env.call('access_review.run', {'provider': 'local'}, key=request.idempotency_key))
    monkeypatch.setattr(v13_store, 'write_bytes_atomic_beneath', original)
    result = review.run(request)
    assert result['run']['state'] == 'completed' and result['run']['findings_created'] == 205
    commits = finding_commits(request)
    assert sorted(len(ids) for ids in commits) == [5, 100, 100]
    assert review.run(request) == result and finding_commits(request) == commits


# LLM: 入口通过后撤销原生管理员资格，存储末门须阻止两集合与确认状态的提交。
# 函数用途: 确定性交错验证父级共享authorize的管理员末核。
@pytest.mark.parametrize('action', ['run', 'acknowledge'])
def test_admin_revocation_before_commit_is_rejected(env, monkeypatch, action):
    inbox(env, [token(env)])
    if action == 'acknowledge':
        review.run(env.call('access_review.run', {'provider': 'inbox_tokens'}))
        finding = review.findings(env.call('access_review.findings.list'))['items'][0]
        request = env.call('access_review.findings.acknowledge', {'finding_id': finding['finding_id']}, revision=1)
        original = identity.cas

        # LLM: 在真实CAS完成之后改变宿主原生裁决，提交末门才能观察到撤权。
        # 函数用途: 注入确认阶段的管理员撤权。
        def revoke(call, row):
            original(call, row)
            call.context.host.business_native_admin = lambda: False

        monkeypatch.setattr(identity, 'cas', revoke)
        handler = review.acknowledge
    else:
        request = env.call('access_review.run', {'provider': 'inbox_tokens'})
        original = review._scan

        # LLM: 只撤销宿主裁决，不改扫描数据或预期拒绝结果。
        # 函数用途: 注入扫描完成、提交之前的管理员撤权。
        def revoke(call, previous):
            result = original(call, previous)
            call.context.host.business_native_admin = lambda: False
            return result

        monkeypatch.setattr(review, '_scan', revoke)
        handler = review.run
    before = {collection: identity.rows(request, collection, 'main') for collection in (
        'access_findings', 'access_review_runs')}
    with pytest.raises(HostAPIError) as error:
        handler(request)
    assert (error.value.code, error.value.status) == ('BUSINESS_ACCESS_DENIED', 403)
    assert {collection: identity.rows(request, collection, 'main') for collection in before} == before


# LLM: 原失败HTTP入口应完整处理超过W0单集合上限的数据，且每个批次确有提交审计。
# 函数用途: 验证真实Gateway大扫描、分页、重放、冲突与解决。
def test_http_large_review_roundtrip(v13_native):
    from agent_py_agent.agent.business_spaces.v13_store import V13Store
    server, agent = v13_native
    source = SimpleNamespace(root=agent.home_paths.root, clock=[time.time()])
    rows = [token(source, f'wire-{index}', used=None, expires=source.clock[0] - 1) for index in range(205)]
    inbox(source, rows)
    key = secrets.token_hex(16)
    status, body = v13_call(server, 'access_review.run', {'provider': 'inbox_tokens'}, user='admin', idempotency_key=key)
    assert status == 200 and body['error'] is None and body['state'] == 'completed', body
    result = body['data']
    assert result['run']['findings_created'] == 205 and result['run']['coverage'] == 'complete'
    status, replay = v13_call(server, 'access_review.run', {'provider': 'inbox_tokens'}, user='admin', idempotency_key=key)
    assert status == 200 and replay['data'] == result, replay
    status, conflict = v13_call(server, 'access_review.run', {'provider': 'local'}, user='admin', idempotency_key=key)
    assert status == 409 and conflict['error']['code'] == 'IDEMPOTENCY_CONFLICT', conflict
    items, cursor = [], None
    while True:
        params = {'limit': 100}
        if cursor:
            params['cursor'] = cursor
        status, page = v13_call(server, 'access_review.findings.list', params, user='admin')
        assert status == 200 and page['error'] is None, page
        items.extend(page['data']['items'])
        cursor = page['data']['next_cursor']
        if not cursor:
            break
    assert len(items) == len({row['finding_id'] for row in items}) == 205
    for row in rows:
        row['enabled'] = False
    inbox(source, rows)
    status, resolved = v13_call(server, 'access_review.run', {'provider': 'inbox_tokens'}, user='admin',
        idempotency_key=secrets.token_hex(16))
    assert status == 200 and resolved['data']['run']['findings_resolved'] == 205, resolved
    status, summary = v13_call(server, 'access_review.summary', {'provider': 'inbox_tokens'}, user='admin')
    assert status == 200 and summary['data']['open'] == 0, summary
    audit = V13Store.read_only(agent.home_paths.owner_home_dir).list('audit_log')
    commits = [row['record_ids'] for row in audit if row['collection'] == 'access_findings']
    assert sorted(len(ids) for ids in commits) == [5, 5, 100, 100, 100, 100]


# LLM: 原已完成扫描的回执仍由W0维护，新分批键不得使历史重放重新执行扫描。
# 函数用途: 验证未带progress的既有完成记录可重放且参数变化冲突。
def test_existing_completed_receipt_still_replays(env):
    request = env.call('access_review.run', {'provider': 'inbox_tokens'})
    run_id = 'review-' + hashlib.sha256((request.owner_id + '\0' + request.subject_id + '\0'
        + request.idempotency_key).encode()).hexdigest()[:40]
    stamp = identity.now(request)
    result = {'run': {'run_id': run_id, 'state': 'completed', 'provider': 'inbox_tokens',
        'started_at': stamp, 'finished_at': stamp, 'findings_created': 0, 'findings_resolved': 0, 'coverage': 'complete'}}

    # LLM: 只准备旧记录及原始回执的真实事务，不替换扫描器或业务响应。
    # 函数用途: 建立变更之前的已完成持久形状。
    def completed(collections):
        collections['access_review_runs'][run_id] = {'run_id': run_id, 'owner_id': 'local/main',
            'provider': 'inbox_tokens', 'state': 'completed', 'baseline': {
                'snapshots': {}, 'coverage': {'inbox_tokens': 'complete'}, 'unavailable': []},
            'findings_created': 0, 'findings_resolved': 0, 'revision': 1, 'created_at': stamp, 'updated_at': stamp}
        return result

    identity.write_many(request, ('access_findings', 'access_review_runs'), completed, scope='main')
    assert review.run(request) == result
    assert len(finding_commits(request)) == 1
    with pytest.raises(Exception) as error:
        review.run(env.call('access_review.run', {'provider': 'local'}, key=request.idempotency_key))
    assert error.value.code == 'IDEMPOTENCY_CONFLICT'
