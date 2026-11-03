"""#88 原生生命周期、真实W0存储、只读MSSP投影及网关往返测试。"""
from __future__ import annotations

import http.client
import json
import secrets
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import RLock
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.business_spaces import v13_w10_identity as identity
from agent_py_agent.agent.business_spaces import v13_w10_tenancy as tenancy
from agent_py_agent.agent.common import global_write_guard
from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.agent.conversation.business_spaces import BusinessSpaceService
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.user_space.local_user_lifecycle import LocalUserLifecycle
from agent_py_agent.agent.user_space.local_user_store import LocalUserStore
from agent_py_agent.agent.user_space.owner_quota import read_local_user_quota
from agent_py_agent.tests.test_v13_w0_harness import v13_seed

pytest_plugins = ['agent_py_agent.tests.test_v13_w0_harness']


# LLM: 本替身只用于领域测试的端口隔离，不实现或伪装生产W0基础层。
# 类用途: 在内存提供锁、回执和回滚，用于调用真实identity.write。
class MemoryPort:
    # LLM: 状态严格按测试实例隔离，异常不提交新记录。
    # 函数用途: 初始化短事务内存端口。
    def __init__(self):
        self.records, self.receipts, self.lock = {}, {}, RLock()

    # LLM: 返回副本，避免读取意外变成写操作。
    # 函数用途: 查询测试集合记录。
    def list(self, collection):
        with self.lock:
            return deepcopy(list(self.records.get(collection, {}).values()))

    # LLM: 单条查询仍返回副本，保持原生用户视图对角色查询的读口合同。
    # 函数用途: 取得测试记录。
    def get(self, collection, record_id):
        return deepcopy(self.records.get(collection, {}).get(record_id))

    # LLM: 接受F2调用形状，不代替原生LocalUserLifecycle的持久化与恢复。
    # 函数用途: 执行领域测试的幂等短事务。
    def transaction(self, collection, mutate, *, idempotency_key, params_digest, receipt_view=None):
        with self.lock:
            key = (collection, idempotency_key)
            if key in self.receipts:
                digest, result = self.receipts[key]
                if digest != params_digest:
                    raise HostAPIError('IDEMPOTENCY_CONFLICT', 409)
                return deepcopy(result)
            state = deepcopy(self.records.get(collection, {}))
            result = mutate(state)
            self.records[collection] = state
            self.receipts[key] = (params_digest, deepcopy(receipt_view(result) if receipt_view else result))
            return result


# LLM: 快照只在隔离测试根执行；包括目录、文件内容和mtime，确保只读不写锁/回执。
# 函数用途: 捕获测试目录的可比较磁盘状态。
def snapshot(root):
    return {str(path.relative_to(root)): (path.stat().st_mtime_ns, path.read_bytes() if path.is_file() else None)
        for path in root.rglob('*')}


# LLM: 每次测试使用临时HOME和真实原生账号；仅替换W0存储构造及管理员决策端口。
# 函数用途: 组合隔离领域测试环境。
@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path))
    root = tmp_path / 'host'
    registry = LocalUserStore(root)
    for name in ('alice', 'bob'):
        LocalUserLifecycle(registry).create(name, idempotency_key='tenancy-native-seed-' + name)
    port = MemoryPort()
    spaces = {}
    checks = []

    # LLM: 所有集合访问都明确标为main；任何错误scope都让测试失败。
    # 函数用途: 返回测试存储端口。
    def storage(call, scope='owner'):
        assert scope == 'main'
        return port

    # LLM: 原生实时校验的调用顺序留给测试观察，可用revoked事实确定性撤权。
    # 函数用途: 检查领域调用身份仍有效。
    def check(action, facts):
        checks.append((action, facts))
        if revoked[0]:
            raise HostAPIError('CAPABILITY_DENIED', 403)

    revoked = [False]
    monkeypatch.setattr(identity, 'store', storage)
    monkeypatch.setattr(identity, 'admin', lambda call: call.owner_id == 'local/main')

    # LLM: DTO不表示宿主签发授权，仅供handler单测，真实HTTP使用下面独立的W0 harness。
    # 函数用途: 构造冻结合同同形的测试调用。
    def call(action, params=None, *, owner='local/main', key=None, revision=None):
        home = root / 'owners' / 'local' / owner.removeprefix('local/')
        paths = SimpleNamespace(root=root, owner_home_dir=home, owner_id=owner, owner_kind='user')
        context = SimpleNamespace(host=SimpleNamespace(home_paths=paths), check=check,
            actor=SimpleNamespace(channel='web'))
        return SimpleNamespace(action=action, params=params or {}, owner_id=owner, subject_id=owner,
            owner_home=home, context=context, services=SimpleNamespace(spaces=spaces.setdefault(
                owner, BusinessSpaceService(home, owner_id=owner))),
            clock=lambda: 1800000000.0, expected_revision=revision, idempotency_key=key or secrets.token_hex(16))

    return SimpleNamespace(root=root, registry=registry, port=port, checks=checks, call=call, revoked=revoked)


# LLM: 拒绝只按结构化业务错误码与HTTP状态校验，不依赖异常自然语言。
# 函数用途: 检查准确的拒绝结果。
def rejected(code, status, function, call):
    with pytest.raises((HostAPIError, BusinessError)) as error:
        function(call)
    observed = error.value.status if isinstance(error.value, HostAPIError) else {
        'CURSOR_EXPIRED': 503, 'INVALID_SCHEMA': 400}[error.value.code]
    assert (error.value.code, observed) == (code, status)


# LLM: 原生owner映射和配额读取必须不创建任何文件或更改mtime。
# 函数用途: 验证当前租户身份与磁盘配额。
def test_tenants_me_owner_mapping(env):
    before = snapshot(env.root)
    result = tenancy.me(env.call('tenants.me', owner='local/alice'))
    assert result['tenant']['tenant_id'] == result['tenant']['owner_id'] == 'local/alice'
    assert result['tenant']['kind'] == 'local_user' and result['tenant']['is_admin'] is False
    assert result['tenant']['quota']['limit_bytes'] == 1024 * 1024 * 1024
    assert result['tenant']['quota']['used_bytes'] > 0
    assert result['identity'] == {'subject_id': 'local/alice', 'channel': 'web'}
    assert tenancy.me(env.call('tenants.me'))['tenant']['kind'] == 'main'
    assert snapshot(env.root) == before
    rejected('RESOURCE_NOT_FOUND', 404, tenancy.me, env.call('tenants.me', owner='local/unknown'))


# LLM: 普通租户不能通过subject或自定义角色扩大用户列表；分页沿真实空间服务。
# 函数用途: 验证管理员列全体、普通用户仅自身及游标篡改拒绝。
def test_tenant_users_admin_sees_all_user_sees_self(env):
    before = snapshot(env.root)
    result = tenancy.my_users(env.call('tenants.users.list'))
    assert {item['user_id'] for item in result['items']} == {'alice', 'bob'}
    assert [item['user_id'] for item in tenancy.my_users(
        env.call('tenants.users.list', owner='local/alice'))['items']] == ['alice']
    first = tenancy.my_users(env.call('tenants.users.list', {'limit': 1}))
    assert first['next_cursor']
    second = tenancy.my_users(env.call('tenants.users.list', {'limit': 1, 'cursor': first['next_cursor']}))
    assert first['items'][0]['user_id'] != second['items'][0]['user_id']
    rejected('CURSOR_EXPIRED', 503, tenancy.my_users,
        env.call('tenants.users.list', {'limit': 1, 'cursor': first['next_cursor'] + 'bad'}))
    assert snapshot(env.root) == before


# LLM: 创建走真实生命周期且物化策略和quota；相同幂等键回放，不重复发表。
# 函数用途: 验证管理员列表、一键开通及原生拒绝映射。
def test_admin_list_and_provision(env):
    call = env.call('tenants.admin.provision', {'user_id': 'carol', 'display_name': '租户C',
        'quota_bytes': 16 * 1024 * 1024})
    first = tenancy.provision(call)
    assert first['tenant']['owner_id'] == 'local/carol' and first['waitlist_entry'] is None
    assert first['tenant']['quota']['limit_bytes'] == 16 * 1024 * 1024
    home = env.registry.owner_home('carol').home_dir
    assert (home / 'permissions.json').exists() and (home / 'quota.json').exists()
    limits, revision = read_local_user_quota(env.root, home)
    assert limits.max_disk_mb == 16 and revision == 1
    before = snapshot(env.root)
    assert tenancy.provision(call) == first
    assert snapshot(env.root) == before
    assert {item['owner_id'] for item in tenancy.admin_list(env.call('tenants.admin.list'))['items']} == {
        'local/alice', 'local/bob', 'local/carol'}
    rejected('STATE_CONFLICT', 409, tenancy.provision,
        env.call('tenants.admin.provision', {'user_id': 'carol'}))
    rejected('BUSINESS_ACCESS_DENIED', 403, tenancy.provision,
        env.call('tenants.admin.provision', {'user_id': 'dave'}, owner='local/alice'))


# LLM: 原生配额只能整数MiB，禁止静默舍入或截断显示名；失败不得创建账号。
# 函数用途: 验证原生输入限制提前拒绝。
@pytest.mark.parametrize('patch', [{'quota_bytes': 0}, {'quota_bytes': 1048577}, {'display_name': 'x' * 81}])
def test_provision_native_limits_rejected_without_writes(env, patch):
    before = snapshot(env.root)
    rejected('INVALID_SCHEMA', 400, tenancy.provision,
        env.call('tenants.admin.provision', {'user_id': 'carol', **patch}))
    assert snapshot(env.root) == before


# LLM: 普通主体可申请但不能审批；候补字段、CAS、不可逆终态和原生开通形成连续流程。
# 函数用途: 验证创建、列表、审批和开通主链路。
def test_waitlist_create_list_update_flow(env):
    create = env.call('waitlist.entries.create', {'email': 'new@example.test', 'organization': '测试组织'}, owner='local/alice')
    entry = tenancy.waitlist_create(create)['entry']
    assert tenancy.waitlist_create(create)['entry'] == entry
    assert entry['status'] == 'pending' and entry['revision'] == 1
    entry_id = entry['entry_id']
    rejected('BUSINESS_ACCESS_DENIED', 403, tenancy.waitlist_list,
        env.call('waitlist.entries.list', owner='local/alice'))
    assert tenancy.waitlist_list(env.call('waitlist.entries.list'))['items'] == [entry]
    rejected('STATE_CONFLICT', 409, tenancy.provision, env.call('tenants.admin.provision', {
        'user_id': 'newuser', 'waitlist_entry_id': entry_id}))
    assert env.registry.read_user('newuser') is None
    update = env.call('waitlist.entries.update', {'entry_id': entry_id, 'status': 'approved', 'note': '批准'}, revision=1)
    approved = tenancy.waitlist_update(update)['entry']
    assert tenancy.waitlist_update(update)['entry'] == approved
    assert approved['status'] == 'approved' and approved['decided_by'] == 'local/main'
    rejected('REVISION_CONFLICT', 409, tenancy.waitlist_update,
        env.call('waitlist.entries.update', {'entry_id': entry_id, 'status': 'rejected'}, revision=1))
    provision = env.call('tenants.admin.provision', {'user_id': 'newuser', 'waitlist_entry_id': entry_id})
    result = tenancy.provision(provision)
    assert result['waitlist_entry']['status'] == 'provisioned'
    assert result['waitlist_entry']['tenant_id'] == 'local/newuser'
    assert tenancy.provision(provision) == result
    rejected('STATE_CONFLICT', 409, tenancy.waitlist_update,
        env.call('waitlist.entries.update', {'entry_id': entry_id, 'status': 'rejected'}, revision=3))
    rejected('RESOURCE_NOT_FOUND', 404, tenancy.waitlist_update,
        env.call('waitlist.entries.update', {'entry_id': 'absent', 'status': 'approved'}, revision=0))


# LLM: 同一个记录修订只允许一个审批成功，不依赖随机sleep安排竞争。
# 函数用途: 验证候补CAS并发线性化和拒绝终态不可逆。
def test_waitlist_cas_concurrency_and_terminal_rejection(env):
    entry = tenancy.waitlist_create(env.call('waitlist.entries.create', {'email': 'case@example.test'}))['entry']

    # LLM: 各请求拥有独立幂等键，共同竞争同一已知修订。
    # 函数用途: 执行一个审批竞争者。
    def approve(index):
        try:
            return tenancy.waitlist_update(env.call('waitlist.entries.update', {
                'entry_id': entry['entry_id'], 'status': 'approved', 'note': str(index)}, revision=1))
        except HostAPIError as error:
            assert error.code == 'REVISION_CONFLICT'
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(approve, range(4)))
    assert sum(result is not None for result in results) == 1
    rejected_entry = tenancy.waitlist_update(env.call('waitlist.entries.update', {
        'entry_id': entry['entry_id'], 'status': 'rejected'}, revision=2))['entry']
    assert rejected_entry['revision'] == 3
    rejected('STATE_CONFLICT', 409, tenancy.waitlist_update, env.call('waitlist.entries.update', {
        'entry_id': entry['entry_id'], 'status': 'approved'}, revision=3))


# LLM: 入口实时撤权与全局writer保护必须先于原生账号创建和候补提交。
# 函数用途: 验证撤权和受限执行器均无磁盘副作用。
def test_provision_live_check_and_global_writer(env, monkeypatch):
    before = snapshot(env.root)
    env.revoked[0] = True
    rejected('CAPABILITY_DENIED', 403, tenancy.provision,
        env.call('tenants.admin.provision', {'user_id': 'carol'}))
    env.revoked[0] = False
    monkeypatch.setattr(global_write_guard, '_restricted_writer', lambda: 'unit-executor')
    with pytest.raises(global_write_guard.GlobalWriteInWorkerError):
        tenancy.provision(env.call('tenants.admin.provision', {'user_id': 'carol'}))
    with pytest.raises(global_write_guard.GlobalWriteInWorkerError):
        tenancy.waitlist_create(env.call('waitlist.entries.create', {'email': 'x@example.test'}))
    assert snapshot(env.root) == before and env.port.records == {}


# LLM: 外层回执写失败不能导致相同原生操作被重复执行，重试必须消费原生命周期回执。
# 函数用途: 验证账号已发表但外层提交失败时可恢复。
def test_native_provision_recovers_outer_receipt_failure(env, monkeypatch):
    original = env.port.transaction
    failed = [False]

    # LLM: 只在内存端口提交之前注入一次故障，真实账号写入仍由原生服务完成。
    # 函数用途: 模拟外层回执失败但保留原生结果。
    def transaction(collection, mutate, *, idempotency_key, params_digest, receipt_view=None):
        if not failed[0]:
            failed[0] = True
            mutate({})
            raise OSError('test receipt unavailable')
        return original(collection, mutate, idempotency_key=idempotency_key,
            params_digest=params_digest, receipt_view=receipt_view)

    monkeypatch.setattr(env.port, 'transaction', transaction)
    call = env.call('tenants.admin.provision', {'user_id': 'recover'})
    with pytest.raises(OSError):
        tenancy.provision(call)
    assert env.registry.read_user('recover').status == 'enabled'
    before = snapshot(env.root)
    result = tenancy.provision(call)
    assert result['tenant']['owner_id'] == 'local/recover'
    assert snapshot(env.root) == before


# LLM: email格式只决定输入有效性，幂等摘要改变不能重用同一键创建第二条记录。
# 函数用途: 验证候补输入拒绝和幂等冲突。
def test_waitlist_email_and_idempotency_conflict(env):
    for email in ('bad', '@example.test', 'x@@example.test', 'x @example.test'):
        rejected('INVALID_SCHEMA', 400, tenancy.waitlist_create,
            env.call('waitlist.entries.create', {'email': email}))
    first = env.call('waitlist.entries.create', {'email': 'one@example.test'})
    tenancy.waitlist_create(first)
    rejected('IDEMPOTENCY_CONFLICT', 409, tenancy.waitlist_create,
        env.call('waitlist.entries.create', {'email': 'two@example.test'}, key=first.idempotency_key))
    assert len(env.port.records['waitlist_entries']) == 1


# LLM: 领域测试只替换安全集合读口；真正F2 read_only及无落盘行为由独立HTTP测试验证。
# 函数用途: 提供确定性的跨租户源数据。
@pytest.fixture
def security_rows(env, monkeypatch):
    data = {}
    for user_id in ('alice', 'bob'):
        owner = 'local/' + user_id
        data[(owner, 'soc_alerts')] = [{'owner_id': owner, 'alert_id': user_id + '-alert',
            'title': '测试告警', 'status': 'new', 'severity': 'critical' if user_id == 'alice' else 'low',
            'created_at': '2027-01-15T00:00:00Z'}]
        data[(owner, 'soc_cases')] = [{'owner_id': owner, 'case_id': user_id + '-case', 'status': 'open'}]
    bad_sources = set()

    # LLM: 返回副本并记录调用，无文件写入，不伪称F2已经接线。
    # 函数用途: 隔离跨owner聚合领域计算。
    def read(home, owner, collection):
        assert home == env.registry.owner_home(owner.removeprefix('local/')).home_dir
        return deepcopy(data.get((owner, collection), [])), (owner, collection) not in bad_sources

    monkeypatch.setattr(tenancy, '_security_rows', read)
    return data, bad_sources


# LLM: 总量、子租户排序与筛选只来自结构化原始行，所有读调用保持磁盘零变化。
# 函数用途: 验证MSSP领域聚合、筛选、分页及管理员范围。
def test_mssp_children_portfolio_alerts_cross_owner_read_only(env, security_rows):
    before = snapshot(env.root)
    result = tenancy.portfolio(env.call('mssp.portfolio.get'))
    assert result['tenants'] == 2 and result['alerts_open'] == 2 and result['cases_open'] == 2
    assert result['alerts_by_severity']['critical'] == 1 and result['coverage'] == 'complete'
    assert {item['tenant_id'] for item in tenancy.children(env.call('mssp.children.list'))['items']} == {
        'local/alice', 'local/bob'}
    selected = tenancy.portfolio_alerts(env.call('mssp.portfolio.alerts', {'severity': 'critical'}))
    assert [alert['tenant_id'] for alert in selected['items']] == ['local/alice']
    first = tenancy.portfolio_alerts(env.call('mssp.portfolio.alerts', {'limit': 1}))
    second = tenancy.portfolio_alerts(env.call('mssp.portfolio.alerts', {'limit': 1, 'cursor': first['next_cursor']}))
    assert len(first['items']) == len(second['items']) == 1 and first['items'] != second['items']
    for action, handler in (('mssp.children.list', tenancy.children), ('mssp.portfolio.get', tenancy.portfolio),
            ('mssp.portfolio.alerts', tenancy.portfolio_alerts)):
        rejected('BUSINESS_ACCESS_DENIED', 403, handler, env.call(action, owner='local/alice'))
    assert snapshot(env.root) == before


# LLM: 任一源损坏必须传播partial及未知null，不能拿其余子集求和伪装完整总量。
# 函数用途: 验证MSSP损坏覆盖与筛选隔离。
def test_mssp_unreadable_child_counts_are_unknown(env, security_rows):
    _data, bad = security_rows
    bad.add(('local/bob', 'soc_alerts'))
    result = tenancy.portfolio(env.call('mssp.portfolio.get'))
    assert result['coverage'] == 'partial' and result['alerts_open'] is None
    assert all(value is None for value in result['alerts_by_severity'].values())
    assert result['cases_open'] == 2
    bob = next(item for item in result['top_tenants'] if item['tenant_id'] == 'local/bob')
    assert bob['alerts_open'] is None and bob['alerts_critical_open'] is None
    assert tenancy.portfolio_alerts(env.call('mssp.portfolio.alerts'))['coverage'] == 'partial'
    assert tenancy.portfolio_alerts(env.call('mssp.portfolio.alerts', {'tenant_id': 'local/alice'}))['coverage'] == 'complete'


# LLM: 明显脏数据先识别再聚合，字段缺失、跨owner和非法枚举不能成为计数依据。
# 函数用途: 验证安全行的最低结构和身份校验。
@pytest.mark.parametrize('patch', [{'owner_id': 'local/bob'}, {'severity': 'other'}, {'status': 'other'},
    {'alert_id': ''}, {'title': ''}, {'created_at': 'not-a-time'}, {'created_at': '2027-01-15'}])
def test_mssp_bad_source_rows_not_counted(patch):
    row = {'owner_id': 'local/alice', 'alert_id': 'alert', 'title': '标题', 'status': 'new',
        'severity': 'critical', 'created_at': '2027-01-15T00:00:00Z'}
    assert tenancy._valid_security_record(row, 'soc_alerts', 'local/alice')
    assert not tenancy._valid_security_record({**row, **patch}, 'soc_alerts', 'local/alice')


# LLM: 真实F2状态按原事务创建，损坏源只在隔离目录注入；不用任何V13Store替身。
# 函数用途: 验证只读store确实不写回执，以及重复/坏数据触发partial。
def test_real_store_read_only_missing_corrupt_and_duplicate_rows(env):
    home = env.registry.owner_home('alice').home_dir
    before = snapshot(home)
    assert tenancy._security_rows(home, 'local/alice', 'soc_alerts') == ([], True)
    assert snapshot(home) == before
    row = {'owner_id': 'local/alice', 'alert_id': 'same', 'title': '告警', 'status': 'new',
        'severity': 'critical', 'created_at': '2027-01-15T00:00:00Z'}
    v13_seed(home, 'soc_alerts', {'first': row, 'second': row,
        'invalid': {'owner_id': 'local/alice', 'alert_id': 'invalid'}})
    before = snapshot(home)
    records, complete = tenancy._security_rows(home, 'local/alice', 'soc_alerts')
    assert records == [row] and complete is False
    assert snapshot(home) == before
    (home / 'data/business_v13/soc_alerts.json').write_text('{invalid', encoding='utf-8')
    before = snapshot(home)
    assert tenancy._security_rows(home, 'local/alice', 'soc_alerts') == ([], False)
    assert snapshot(home) == before


# LLM: 真实W0事务而非内存端口负责候补和回执，原生生命周期仍使用同一隔离根。
# 函数用途: 验证候补创建审批开通的真实耐久集成。
def test_real_store_waitlist_provision_persistence(env, monkeypatch):
    from agent_py_agent.agent.business_spaces.v13_store import V13Store
    from agent_py_agent.agent.user_space.owner_resolver import ensure_owner_home
    home = ensure_owner_home(env.root).home_dir

    # LLM: 只绑定真实存储范围；测试身份端口沿领域fixture，HTTP身份另测。
    # 函数用途: 给真实store传入规范main范围。
    def storage(call, scope='owner'):
        assert scope == 'main'
        port = V13Store(home, owner_id='local/main', subject_id=call.subject_id,
            action=call.action, clock=call.clock)
        port._scope = 'main'
        return port

    monkeypatch.setattr(identity, 'store', storage)
    create = env.call('waitlist.entries.create', {'email': 'persist@example.test'}, owner='local/alice')
    entry = tenancy.waitlist_create(create)['entry']
    assert tenancy.waitlist_create(create)['entry'] == entry
    approve = env.call('waitlist.entries.update', {'entry_id': entry['entry_id'], 'status': 'approved'}, revision=1)
    tenancy.waitlist_update(approve)
    provision = env.call('tenants.admin.provision', {'user_id': 'persisted', 'waitlist_entry_id': entry['entry_id']})
    first = tenancy.provision(provision)
    assert first['waitlist_entry']['status'] == 'provisioned' and first['waitlist_entry']['revision'] == 3
    before = snapshot(env.root)
    assert tenancy.provision(provision) == first
    assert snapshot(env.root) == before
    saved = storage(create, 'main').get('waitlist_entries', entry['entry_id'])
    assert saved['tenant_id'] == 'local/persisted' and saved['status'] == 'provisioned'


# LLM: 本地账号使用宿主真实签发context，不通过web provider头假装local用户，也不启动用户服务。
# 函数用途: 验证原生普通账号的live context身份、self范围、主集合写入与管理员拒绝。
def test_local_user_live_context(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.business_api import (
        BusinessServices,
        bind_business_context,
        execute_business,
    )
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_resolver import (
        ensure_owner_home,
        home_paths_with_owner,
    )
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path))
    root = tmp_path / 'live-native'
    registry = LocalUserStore(root)
    LocalUserLifecycle(registry).create('alice', idempotency_key='tenancy-live-context-alice')
    LocalUserLifecycle(registry).create('bob', idempotency_key='tenancy-live-context-bob')
    ensure_owner_home(root)
    host = SimpleNamespace(home_paths=home_paths_with_owner(home_paths(root), registry.owner_home('alice')),
        config=SimpleNamespace(business_spaces_enabled=True))
    services = BusinessServices(host)

    # LLM: 原生账号状态每次复查；授权由真实bind_business_context签发，不用可复制DTO冒充。
    # 函数用途: 给本地账号接入当前原生有效性检查。
    def authorize(action, facts):
        registry.require_enabled('alice')
        return facts.get('owner_id', 'local/alice') == 'local/alice'

    context = bind_business_context(host, authorize=authorize, subject_id='local/alice')
    status, body = execute_business({'action': 'tenants.me', 'request_id': 'live-me', 'params': {}}, context, services)
    assert status == 200 and body['error'] is None, body
    assert body['data']['tenant']['owner_id'] == 'local/alice'
    status, body = execute_business({'action': 'tenants.users.list', 'request_id': 'live-users', 'params': {}}, context, services)
    assert status == 200 and [item['user_id'] for item in body['data']['items']] == ['alice'], body
    status, body = execute_business({'action': 'waitlist.entries.create', 'request_id': 'live-signup',
        'params': {'email': 'local-alice@example.test'}, 'idempotency_key': secrets.token_hex(16)}, context, services)
    assert status == 200 and body['data']['entry']['status'] == 'pending', body
    status, body = execute_business({'action': 'tenants.admin.list', 'request_id': 'live-admin', 'params': {}}, context, services)
    assert status == 403 and body['error']['code'] == 'BUSINESS_ACCESS_DENIED', body


# LLM: 凭据取自隔离server；main沿原生无身份头宿主终端，显式用户沿web provider；不启动用户Agent。
# 函数用途: 执行本模块的真实HTTP往返。
def web_call(server, action, params, *, user='local', key=None, revision=None):
    payload = {'action': action, 'request_id': 'tenancy-' + secrets.token_hex(12), 'params': params}
    if key is not None:
        payload['idempotency_key'] = key
    if revision is not None:
        payload['expected_revision'] = revision
    headers = {'Content-Type': 'application/json', 'X-Gateway-Token': server._local_client_credential}
    if user != 'local':
        headers.update({'X-User-Id': user, 'X-Channel': 'web'})
    connection = http.client.HTTPConnection('127.0.0.1', server.server.server_address[1], timeout=20)
    try:
        connection.request('POST', '/client/business', json.dumps(payload), headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


# LLM: 每个成功结果由真实注册器校验，测试再核闭合信封及请求对应动作，绝不手写替代响应。
# 函数用途: 断言HTTP成功和实际结果schema。
def completed(server, action, params, *, user='local', key=None, revision=None):
    from agent_py_agent.agent.gateway_parts.business_api_v13 import v13_validate_result
    status, body = web_call(server, action, params, user=user, key=key, revision=revision)
    assert status == 200, body
    assert body['state'] == 'completed' and body['error'] is None, body
    assert set(body) == {'request_id', 'operation_id', 'state', 'data', 'error',
        'allowed_actions', 'warnings', 'effect_outcome'}
    v13_validate_result(action, body['data'])
    return body['data']


# LLM: 全部#88动作经真实HTTP→dispatcher→持久化store→schema；local账号另用真实live context覆盖。
# 函数用途: 验证完整租户候补主链路与成功信封。
def test_envelopes(v13_native):
    server, agent = v13_native
    root = agent.home_paths.root
    LocalUserLifecycle(LocalUserStore(root)).create('alice', idempotency_key='tenancy-http-seed-alice')
    assert completed(server, 'tenants.me', {})['tenant']['owner_id'] == 'local/main'
    mine = completed(server, 'tenants.me', {}, user='alice')
    assert mine['tenant']['owner_id'] == 'providers/web/users/alice'
    assert [item['user_id'] for item in completed(server, 'tenants.users.list', {})['items']] == ['alice']
    assert len(completed(server, 'tenants.admin.list', {})['items']) == 1
    create_key = secrets.token_hex(16)
    first = completed(server, 'waitlist.entries.create', {'email': 'signup@example.test'}, user='alice', key=create_key)
    assert completed(server, 'waitlist.entries.create', {'email': 'signup@example.test'}, user='alice', key=create_key) == first
    entry = first['entry']
    assert completed(server, 'waitlist.entries.list', {})['items'] == [entry]
    approved = completed(server, 'waitlist.entries.update', {'entry_id': entry['entry_id'], 'status': 'approved'},
        key=secrets.token_hex(16), revision=entry['revision'])['entry']
    assert approved['status'] == 'approved'
    provision_key = secrets.token_hex(16)
    params = {'user_id': 'carol', 'waitlist_entry_id': entry['entry_id'], 'quota_bytes': 16 * 1024 * 1024}
    provisioned = completed(server, 'tenants.admin.provision', params, key=provision_key)
    assert provisioned['tenant']['status'] == 'enabled'
    assert provisioned['waitlist_entry']['status'] == 'provisioned'
    assert completed(server, 'tenants.admin.provision', params, key=provision_key) == provisioned
    assert len(completed(server, 'mssp.children.list', {})['items']) == 2
    assert completed(server, 'mssp.portfolio.get', {})['tenants'] == 2
    assert completed(server, 'mssp.portfolio.alerts', {})['items'] == []
    status, body = web_call(server, 'tenants.admin.list', {}, user='alice')
    assert status == 403 and body['error']['code'] == 'BUSINESS_ACCESS_DENIED'
    status, body = web_call(server, 'waitlist.entries.update', {
        'entry_id': entry['entry_id'], 'status': 'rejected'}, key=secrets.token_hex(16), revision=1)
    assert status == 409 and body['error']['code'] == 'REVISION_CONFLICT'


# LLM: 故障注入真实W0事务日志首写，原生账号已提交但外层尚未落盘；换参必须先拒绝，再同参恢复。
# 函数用途: 验证完整v13意图在原生副作用之前耐久绑定，尤其不能将另一候补错误推进。
@pytest.mark.parametrize('changed', ['waitlist_entry_id', 'display_name', 'quota_bytes', 'user_id', 'expected_revision'])
def test_http_provision_outer_failure_binds_complete_intent(v13_native, monkeypatch, changed):
    from agent_py_agent.agent.business_spaces import v13_store
    from agent_py_agent.agent.user_space.owner_resolver import ensure_owner_home
    server, agent = v13_native
    root = agent.home_paths.root
    entries = []
    for index in range(2):
        entry = completed(server, 'waitlist.entries.create', {'email': f'fault-{index}@example.test'},
            key=secrets.token_hex(16))['entry']
        approved = completed(server, 'waitlist.entries.update', {'entry_id': entry['entry_id'], 'status': 'approved'},
            key=secrets.token_hex(16), revision=entry['revision'])['entry']
        entries.append(approved)
    original = v13_store.write_bytes_atomic_beneath
    failed = [False]

    # LLM: 真正执行原store事务和原生开通，只在外层日志写入边界报一次I/O故障。
    # 函数用途: 精确复现原生成功、业务回执未提交的窗口。
    def fail_outer_journal(home, parts, raw, **kwargs):
        if tuple(parts) == ('data', 'business_v13', '.transaction.json') and not failed[0]:
            failed[0] = True
            raise OSError('injected outer receipt journal failure')
        return original(home, parts, raw, **kwargs)

    monkeypatch.setattr(v13_store, 'write_bytes_atomic_beneath', fail_outer_journal)
    params = {'user_id': 'recoverhttp', 'display_name': 'Recover HTTP', 'quota_bytes': 16 * 1024 * 1024,
        'waitlist_entry_id': entries[0]['entry_id']}
    key = secrets.token_hex(16)
    status, body = web_call(server, 'tenants.admin.provision', params, key=key)
    assert status == 503 and body['error']['code'] == 'OUTCOME_UNKNOWN', body
    assert body['effect_outcome'] == 'unknown' and body['error']['retryable'] is True, body
    assert failed[0] is True and LocalUserStore(root).read_user('recoverhttp').status == 'enabled'
    expected_entries = sorted(entries, key=lambda item: (item['created_at'], item['entry_id']), reverse=True)
    assert completed(server, 'waitlist.entries.list', {})['items'] == expected_entries
    home = ensure_owner_home(root).home_dir
    before = snapshot(home)
    wrong = {**params, **({'waitlist_entry_id': entries[1]['entry_id']} if changed == 'waitlist_entry_id'
        else {'display_name': 'Changed'} if changed == 'display_name'
        else {'quota_bytes': 32 * 1024 * 1024} if changed == 'quota_bytes'
        else {'user_id': 'anotherhttp'} if changed == 'user_id' else {})}
    status, body = web_call(server, 'tenants.admin.provision', wrong, key=key,
        revision=1 if changed == 'expected_revision' else None)
    assert status == 409 and body['error']['code'] == 'IDEMPOTENCY_CONFLICT', body
    assert snapshot(home) == before
    assert completed(server, 'waitlist.entries.list', {})['items'] == expected_entries
    restored = completed(server, 'tenants.admin.provision', params, key=key)
    assert restored['tenant']['owner_id'] == 'local/recoverhttp'
    assert restored['waitlist_entry']['entry_id'] == entries[0]['entry_id']
    assert restored['waitlist_entry']['status'] == 'provisioned'
    assert completed(server, 'tenants.admin.provision', params, key=key) == restored
    saved = {item['entry_id']: item for item in completed(server, 'waitlist.entries.list', {})['items']}
    assert saved[entries[1]['entry_id']] == entries[1]
    assert saved[entries[0]['entry_id']]['status'] == 'provisioned'


# LLM: 真正跨owner读取只经F2.read_only；抓取前后完整子租户快照证明无锁/回执/审计新写入。
# 函数用途: 验证MSSP真实store、脏源partial与只读不变量。
def test_http_mssp_real_read_only_store(v13_native):
    server, agent = v13_native
    registry = LocalUserStore(agent.home_paths.root)
    for name in ('alice', 'bob'):
        LocalUserLifecycle(registry).create(name, idempotency_key='tenancy-http-mssp-' + name)
        owner = 'local/' + name
        home = registry.owner_home(name).home_dir
        v13_seed(home, 'soc_alerts', {name + '-alert': {'alert_id': name + '-alert', 'owner_id': owner,
            'title': '真实集合告警', 'severity': 'critical', 'status': 'new', 'created_at': '2027-01-15T00:00:00Z'}})
        v13_seed(home, 'soc_cases', {name + '-case': {'case_id': name + '-case', 'owner_id': owner, 'status': 'open'}})
    homes = [registry.owner_home(name).home_dir for name in ('alice', 'bob')]
    before = [snapshot(home) for home in homes]
    assert completed(server, 'mssp.portfolio.get', {})['alerts_open'] == 2
    assert completed(server, 'mssp.portfolio.get', {})['cases_open'] == 2
    assert len(completed(server, 'mssp.portfolio.alerts', {})['items']) == 2
    assert len(completed(server, 'mssp.children.list', {})['items']) == 2
    assert [snapshot(home) for home in homes] == before
    # 损坏原始集合，不触碰提交日志；读取应报告未知而非吞异常成零。
    broken = homes[1] / 'data' / 'business_v13' / 'soc_alerts.json'
    broken.write_text('{invalid', encoding='utf-8')
    corrupt_before = [snapshot(home) for home in homes]
    result = completed(server, 'mssp.portfolio.get', {})
    assert result['coverage'] == 'partial' and result['alerts_open'] is None
    assert result['cases_open'] == 2
    assert completed(server, 'mssp.children.list', {})['coverage'] == 'partial'
    assert completed(server, 'mssp.portfolio.alerts', {})['coverage'] == 'partial'
    assert [snapshot(home) for home in homes] == corrupt_before
