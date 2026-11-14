"""Consolidated tests for shard c8 (test_consolidated_c8_g2.py).

Merged sources: test_v13_w10_tenancy.py, test_v13_w8_entity_risk.py, test_v13_w8_feedback.py, test_v13_w8_investigations.py, test_v13_w8_replay_windows.py, test_v13_w9_ai_assets.py, test_v13_w9_datasources.py.
"""
from __future__ import annotations

import csv
import hashlib
import http.client
import json
import secrets
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.assets.datasource_store import DatasourceStore
from agent_py_agent.agent.business_spaces import (
    v13_w8_alerts as alerts,
)
from agent_py_agent.agent.business_spaces import (
    v13_w8_alerts as shared,
)
from agent_py_agent.agent.business_spaces import (
    v13_w8_investigations as investigation,
)
from agent_py_agent.agent.business_spaces import (
    v13_w8_replay_windows as replay,
)
from agent_py_agent.agent.business_spaces import (
    v13_w9_ai_assets as assets,
)
from agent_py_agent.agent.business_spaces import (
    v13_w10_identity as identity,
)
from agent_py_agent.agent.business_spaces import (
    v13_w10_tenancy as tenancy,
)
from agent_py_agent.agent.common import global_write_guard
from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.agent.conversation.business_observability import (
    BusinessObservationService,
    ObservationPage,
    ObservationRecord,
)
from agent_py_agent.agent.conversation.business_spaces import BusinessSpaceService
from agent_py_agent.agent.gateway_parts import (
    business_api as api,
)
from agent_py_agent.agent.gateway_parts import (
    business_artifacts_http as media,
)
from agent_py_agent.agent.gateway_parts.business_api import (
    BusinessServices,
    _error_code,
    bind_business_context,
)
from agent_py_agent.agent.gateway_parts.business_api_v13 import v13_action_spec as registered_spec
from agent_py_agent.agent.plugin_host.business_action_specs import business_action_spec
from agent_py_agent.agent.plugin_host.contracts import DomainResult
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.plugin_host.schemas import thaw, validate_object
from agent_py_agent.agent.user_space.local_user_lifecycle import LocalUserLifecycle
from agent_py_agent.agent.user_space.local_user_store import LocalUserStore
from agent_py_agent.agent.user_space.owner_quota import read_local_user_quota
from agent_py_agent.tests import (
    test_rework_r8_business_wire_contract as wire_harness,
)
from agent_py_agent.tests import (
    test_v13_w0_harness,
)
from agent_py_agent.tests import (
    test_v13_w0_harness as harness,
)
from agent_py_agent.tests.test_v13_w0_harness import v13_seed
from agent_py_agent.tests.test_v13_w8_alerts import invoke, owner, seed_alert
from agent_py_agent.tests.test_v13_w8_cases import completed, rejected
from agent_py_agent.tests.test_v13_w9_hunting import (
    _call as _call_w9_datasources,
)
from agent_py_agent.tests.test_v13_w9_hunting import (
    _completed as _completed_w9_datasources,
)
from agent_py_agent.tests.test_v13_w9_hunting import (
    _error,
    _space,
    hunting_http,
)

pytest_plugins = ('agent_py_agent.tests.test_v13_w0_harness',)

native = harness.v13_native
native_http = test_v13_w0_harness.v13_native
gateway = wire_harness.gateway
thin_client = wire_harness.thin_client

# ============================================================================
# Source: agent_py_agent/tests/test_v13_w10_tenancy.py
# #88 原生生命周期、真实W0存储、只读MSSP投影及网关往返测试。
# ============================================================================

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
        for path in root.rglob('*') if path.name not in {'runtime.db-shm', 'runtime.db-wal'}}

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
def rejected_w10_tenancy(code, status, function, call):
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
    rejected_w10_tenancy('RESOURCE_NOT_FOUND', 404, tenancy.me, env.call('tenants.me', owner='local/unknown'))

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
    rejected_w10_tenancy('CURSOR_EXPIRED', 503, tenancy.my_users,
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
    rejected_w10_tenancy('STATE_CONFLICT', 409, tenancy.provision,
        env.call('tenants.admin.provision', {'user_id': 'carol'}))
    rejected_w10_tenancy('BUSINESS_ACCESS_DENIED', 403, tenancy.provision,
        env.call('tenants.admin.provision', {'user_id': 'dave'}, owner='local/alice'))

# LLM: 原生配额只能整数MiB，禁止静默舍入或截断显示名；失败不得创建账号。
# 函数用途: 验证原生输入限制提前拒绝。
@pytest.mark.parametrize('patch', [{'quota_bytes': 0}, {'quota_bytes': 1048577}, {'display_name': 'x' * 81}])
def test_provision_native_limits_rejected_without_writes(env, patch):
    before = snapshot(env.root)
    rejected_w10_tenancy('INVALID_SCHEMA', 400, tenancy.provision,
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
    rejected_w10_tenancy('BUSINESS_ACCESS_DENIED', 403, tenancy.waitlist_list,
        env.call('waitlist.entries.list', owner='local/alice'))
    assert tenancy.waitlist_list(env.call('waitlist.entries.list'))['items'] == [entry]
    rejected_w10_tenancy('STATE_CONFLICT', 409, tenancy.provision, env.call('tenants.admin.provision', {
        'user_id': 'newuser', 'waitlist_entry_id': entry_id}))
    assert env.registry.read_user('newuser') is None
    update = env.call('waitlist.entries.update', {'entry_id': entry_id, 'status': 'approved', 'note': '批准'}, revision=1)
    approved = tenancy.waitlist_update(update)['entry']
    assert tenancy.waitlist_update(update)['entry'] == approved
    assert approved['status'] == 'approved' and approved['decided_by'] == 'local/main'
    rejected_w10_tenancy('REVISION_CONFLICT', 409, tenancy.waitlist_update,
        env.call('waitlist.entries.update', {'entry_id': entry_id, 'status': 'rejected'}, revision=1))
    provision = env.call('tenants.admin.provision', {'user_id': 'newuser', 'waitlist_entry_id': entry_id})
    result = tenancy.provision(provision)
    assert result['waitlist_entry']['status'] == 'provisioned'
    assert result['waitlist_entry']['tenant_id'] == 'local/newuser'
    assert tenancy.provision(provision) == result
    rejected_w10_tenancy('STATE_CONFLICT', 409, tenancy.waitlist_update,
        env.call('waitlist.entries.update', {'entry_id': entry_id, 'status': 'rejected'}, revision=3))
    rejected_w10_tenancy('RESOURCE_NOT_FOUND', 404, tenancy.waitlist_update,
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
    rejected_w10_tenancy('STATE_CONFLICT', 409, tenancy.waitlist_update, env.call('waitlist.entries.update', {
        'entry_id': entry['entry_id'], 'status': 'approved'}, revision=3))

# LLM: 入口实时撤权与全局writer保护必须先于原生账号创建和候补提交。
# 函数用途: 验证撤权和受限执行器均无磁盘副作用。
def test_provision_live_check_and_global_writer(env, monkeypatch):
    before = snapshot(env.root)
    env.revoked[0] = True
    rejected_w10_tenancy('CAPABILITY_DENIED', 403, tenancy.provision,
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
        rejected_w10_tenancy('INVALID_SCHEMA', 400, tenancy.waitlist_create,
            env.call('waitlist.entries.create', {'email': email}))
    first = env.call('waitlist.entries.create', {'email': 'one@example.test'})
    tenancy.waitlist_create(first)
    rejected_w10_tenancy('IDEMPOTENCY_CONFLICT', 409, tenancy.waitlist_create,
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
        rejected_w10_tenancy('BUSINESS_ACCESS_DENIED', 403, handler, env.call(action, owner='local/alice'))
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
def completed_w10_tenancy(server, action, params, *, user='local', key=None, revision=None):
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
def test_envelopes_w10_tenancy(v13_native):
    server, agent = v13_native
    root = agent.home_paths.root
    LocalUserLifecycle(LocalUserStore(root)).create('alice', idempotency_key='tenancy-http-seed-alice')
    assert completed_w10_tenancy(server, 'tenants.me', {})['tenant']['owner_id'] == 'local/main'
    mine = completed_w10_tenancy(server, 'tenants.me', {}, user='alice')
    assert mine['tenant']['owner_id'] == 'providers/web/users/alice'
    assert [item['user_id'] for item in completed_w10_tenancy(server, 'tenants.users.list', {})['items']] == ['alice']
    assert len(completed_w10_tenancy(server, 'tenants.admin.list', {})['items']) == 1
    create_key = secrets.token_hex(16)
    first = completed_w10_tenancy(server, 'waitlist.entries.create', {'email': 'signup@example.test'}, user='alice', key=create_key)
    assert completed_w10_tenancy(server, 'waitlist.entries.create', {'email': 'signup@example.test'}, user='alice', key=create_key) == first
    entry = first['entry']
    assert completed_w10_tenancy(server, 'waitlist.entries.list', {})['items'] == [entry]
    approved = completed_w10_tenancy(server, 'waitlist.entries.update', {'entry_id': entry['entry_id'], 'status': 'approved'},
        key=secrets.token_hex(16), revision=entry['revision'])['entry']
    assert approved['status'] == 'approved'
    provision_key = secrets.token_hex(16)
    params = {'user_id': 'carol', 'waitlist_entry_id': entry['entry_id'], 'quota_bytes': 16 * 1024 * 1024}
    provisioned = completed_w10_tenancy(server, 'tenants.admin.provision', params, key=provision_key)
    assert provisioned['tenant']['status'] == 'enabled'
    assert provisioned['waitlist_entry']['status'] == 'provisioned'
    assert completed_w10_tenancy(server, 'tenants.admin.provision', params, key=provision_key) == provisioned
    assert len(completed_w10_tenancy(server, 'mssp.children.list', {})['items']) == 2
    assert completed_w10_tenancy(server, 'mssp.portfolio.get', {})['tenants'] == 2
    assert completed_w10_tenancy(server, 'mssp.portfolio.alerts', {})['items'] == []
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
        entry = completed_w10_tenancy(server, 'waitlist.entries.create', {'email': f'fault-{index}@example.test'},
            key=secrets.token_hex(16))['entry']
        approved = completed_w10_tenancy(server, 'waitlist.entries.update', {'entry_id': entry['entry_id'], 'status': 'approved'},
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
    assert completed_w10_tenancy(server, 'waitlist.entries.list', {})['items'] == expected_entries
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
    assert completed_w10_tenancy(server, 'waitlist.entries.list', {})['items'] == expected_entries
    restored = completed_w10_tenancy(server, 'tenants.admin.provision', params, key=key)
    assert restored['tenant']['owner_id'] == 'local/recoverhttp'
    assert restored['waitlist_entry']['entry_id'] == entries[0]['entry_id']
    assert restored['waitlist_entry']['status'] == 'provisioned'
    assert completed_w10_tenancy(server, 'tenants.admin.provision', params, key=key) == restored
    saved = {item['entry_id']: item for item in completed_w10_tenancy(server, 'waitlist.entries.list', {})['items']}
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
    assert completed_w10_tenancy(server, 'mssp.portfolio.get', {})['alerts_open'] == 2
    assert completed_w10_tenancy(server, 'mssp.portfolio.get', {})['cases_open'] == 2
    assert len(completed_w10_tenancy(server, 'mssp.portfolio.alerts', {})['items']) == 2
    assert len(completed_w10_tenancy(server, 'mssp.children.list', {})['items']) == 2
    assert [snapshot(home) for home in homes] == before
    # 损坏原始集合，不触碰提交日志；读取应报告未知而非吞异常成零。
    broken = homes[1] / 'data' / 'business_v13' / 'soc_alerts.json'
    broken.write_text('{invalid', encoding='utf-8')
    corrupt_before = [snapshot(home) for home in homes]
    result = completed_w10_tenancy(server, 'mssp.portfolio.get', {})
    assert result['coverage'] == 'partial' and result['alerts_open'] is None
    assert result['cases_open'] == 2
    assert completed_w10_tenancy(server, 'mssp.children.list', {})['coverage'] == 'partial'
    assert completed_w10_tenancy(server, 'mssp.portfolio.alerts', {})['coverage'] == 'partial'
    assert [snapshot(home) for home in homes] == corrupt_before

# ============================================================================
# Source: agent_py_agent/tests/test_v13_w8_entity_risk.py
# 实体评分按固定历史输入验证；Gateway 入口与纯算法边界分别覆盖。
# ============================================================================

# LLM: 历史输入与期望评分由明确权重组成，不依赖模型输出。
# 函数用途: 验证 issue 示例、得分排序及实体过滤。
def test_queue_example_limit_20_sorted_by_score(native):
    seed_alert(native, 'a', 'critical', entity={'type': 'user', 'value': 'alice'})
    seed_alert(native, 'b', 'high', entity={'type': 'user', 'value': 'alice'})
    seed_alert(native, 'c', 'medium', entity={'type': 'host', 'value': 'server'})
    seed_alert(native, 'd', 'low', entity={'type': 'ip', 'value': '192.0.2.1'})
    result = completed(invoke(native, 'entity_risk.queue', {'limit': 20}))
    assert [row['risk_score'] for row in result['items']] == [80, 15, 5]
    assert result['items'][0]['alert_count'] == 2
    assert result['items'][0]['baseline_deviation'] is None
    assert {row['entity_type'] for row in result['items']} == {'user', 'host', 'ip'}
    selected = completed(invoke(native, 'entity_risk.queue', {'entity_type': 'user', 'min_score': 70}))
    assert [row['entity_value'] for row in selected['items']] == ['alice']

# LLM: 阈值等号必须覆盖，统计与队列的输入完全一致。
# 函数用途: 验证高/中/低分桶及类型统计。
def test_stats_buckets_high_ge_70_medium_ge_40(native):
    for name, severity in [('h1', 'critical'), ('h2', 'medium'), ('h3', 'low')]:
        seed_alert(native, name, severity, entity={'type': 'user', 'value': 'high'})
    for name, severity in [('m1', 'high'), ('m2', 'low'), ('m3', 'low')]:
        seed_alert(native, name, severity, entity={'type': 'host', 'value': 'medium'})
    seed_alert(native, 'l1', 'info', entity={'type': 'ip', 'value': '192.0.2.2'})
    result = completed(invoke(native, 'entity_risk.stats', {}))
    assert {key: result[key] for key in ('entities_total', 'high', 'medium', 'low')} == {
        'entities_total': 3, 'high': 1, 'medium': 1, 'low': 1}
    assert result['by_type'] == {'user': 1, 'host': 1, 'ip': 1}
    assert result['generated_at'] and result['coverage'] == 'complete'

# LLM: 较早历史不会抬高三十日风险，未知实体不能空成功。
# 函数用途: 验证单实体每日历史、衰减与未知/其它owner错误。
def test_get_profile_history_and_unknown_entity_404(native):
    seed_alert(native, 'old', 'critical', age_days=60)
    seed_alert(native, 'middle', 'high', age_days=15)
    seed_alert(native, 'recent', 'medium', age_days=1)
    params = {'entity_type': 'host', 'entity_value': 'node-fixture'}
    result = completed(invoke(native, 'entity_risk.get', params))
    assert result['entity']['risk_score'] == 30
    assert result['entity']['alert_count'] == 2
    assert len(result['history']) == 3
    assert result['entity']['baseline_deviation'] == pytest.approx(2 / 3)
    assert [row['day'] for row in result['history']] == sorted(row['day'] for row in result['history'])
    rejected(invoke(native, 'entity_risk.get', dict(params, entity_value='not-seen')), 404, 'RESOURCE_NOT_FOUND')
    rejected(invoke(native, 'entity_risk.get', params, user='business-bob'), 404, 'RESOURCE_NOT_FOUND')

# LLM: cursor 与实体筛选共同绑定；篡改和跨类型复用必须显式失败。
# 函数用途: 验证实体队列分页及闭合请求。
def test_envelopes_w8_entity_risk(native):
    seed_alert(native, 'one', entity={'type': 'host', 'value': 'one'})
    seed_alert(native, 'two', entity={'type': 'host', 'value': 'two'})
    first = completed(invoke(native, 'entity_risk.queue', {'limit': 1}))
    assert first['next_cursor']
    second = completed(invoke(native, 'entity_risk.queue', {'limit': 1, 'cursor': first['next_cursor']}))
    assert second['items'][0]['entity_value'] != first['items'][0]['entity_value']
    rejected(invoke(native, 'entity_risk.queue', {'entity_type': 'user', 'cursor': first['next_cursor']}),
             410, 'CURSOR_EXPIRED')
    rejected(invoke(native, 'entity_risk.stats', {'extra': True}), 400, 'INVALID_SCHEMA')

# LLM: 算法额外覆盖日期边界、重复实体、上限与未观察数据，不替代上面的真实HTTP。
# 函数用途: 验证确定性风险的时间与计数边界。
def test_score_decay_window_and_duplicate_entity(monkeypatch):
    now = datetime(2026, 11, 2, tzinfo=timezone.utc).timestamp()
    call = SimpleNamespace(clock=lambda: now, params={})
    values = []
    for identifier, age, severity in [('fresh', 0, 'critical'), ('week', 8, 'high'),
                                      ('month', 31, 'critical'), ('future', -1, 'critical')]:
        stamp = datetime.fromtimestamp(now - age * 86400, timezone.utc).isoformat()
        values.append({'alert_id': identifier, 'created_at': stamp, 'severity': severity,
            'entities': [{'type': 'host', 'value': 'x'}, {'type': 'host', 'value': 'x'}]})
    monkeypatch.setattr(alerts, 'w8_rows', lambda call, collection: values)
    groups = alerts._entity_alerts(call)
    assert len(groups[('host', 'x')]) == 3
    result = alerts._risk_profile(call, ('host', 'x'), groups[('host', 'x')])
    assert result['risk_score'] == 65 and result['alert_count'] == 2
    assert result['contributing_alerts'] == ['fresh', 'week']
    assert alerts._risk_profile(call, ('host', 'x'), [values[0]] * 3)['risk_score'] == 100

# ============================================================================
# Source: agent_py_agent/tests/test_v13_w8_feedback.py
# 反馈在真实 HTTP 入口验证覆写、批量重处置、统计与受控共享形状。
# ============================================================================

# LLM: 使用问题中的中性 token 例参，重复请求必须重放同一反馈。
# 函数用途: 验证覆写示例、并发 stale 原判定及反馈原子性。
def test_submit_override_issue_example_and_stale_original_verdict(native):
    row = seed_alert(native, verdict='true_positive', confidence=0.9)
    params = {'alert_id': row['alert_id'], 'original_verdict': 'true_positive',
        'override_verdict': 'benign', 'reason': '内部合规扫描器触发'}
    key = uuid.uuid4().hex
    first = completed(invoke(native, 'feedback.submit_override', params, key))
    assert first['alert']['verdict'] == 'benign' and first['alert']['disposition'] == 'benign'
    assert first['override']['original_verdict'] == 'true_positive'
    assert completed(invoke(native, 'feedback.submit_override', params, key)) == first
    error = rejected(invoke(native, 'feedback.submit_override', params), 409, 'STATE_CONFLICT')
    assert {'key': 'current_verdict', 'value': 'benign'} in error['error']['details']
    rejected(invoke(native, 'feedback.submit_override', dict(params, reason='changed'), key),
             409, 'IDEMPOTENCY_CONFLICT')
    rejected(invoke(native, 'feedback.submit_override', dict(params, alert_id='not-seen')),
             404, 'RESOURCE_NOT_FOUND')
    rejected(invoke(native, 'feedback.submit_override', params, user='business-bob'),
             404, 'RESOURCE_NOT_FOUND')

# LLM: 规则与主实体共同决定匹配，dry_run 不改变领域集合。
# 函数用途: 验证重处置筛选、状态限制、反馈、幂等及缺失错误。
def test_redisposition_matches_similar_pending_alerts_and_dry_run(native):
    source = seed_alert(native, 'source', verdict='true_positive')
    seed_alert(native, 'similar', verdict='true_positive')
    seed_alert(native, 'other-rule', verdict='true_positive', rule_id='rule-other')
    seed_alert(native, 'other-entity', verdict='true_positive', entity={'type': 'host', 'value': 'other'})
    seed_alert(native, 'closed', verdict='true_positive', status='closed')
    saved = completed(invoke(native, 'feedback.submit_override', {'alert_id': source['alert_id'],
        'original_verdict': 'true_positive', 'override_verdict': 'benign', 'reason': '人工复核'}))['override']
    params = {'override_id': saved['override_id'], 'dry_run': True}
    plan = completed(invoke(native, 'feedback.apply_redisposition', params))
    assert plan['matched'] == ['similar'] and plan['applied'] == [] and plan['dry_run'] is True
    assert plan['skipped'] == [{'alert_id': 'closed', 'reason': 'not_pending'}]
    assert completed(invoke(native, 'soc.alerts.get', {'alert_id': 'similar'}))['alert']['verdict'] == 'true_positive'
    key = uuid.uuid4().hex
    result = completed(invoke(native, 'feedback.apply_redisposition', dict(params, dry_run=False), key))
    assert result['applied'] == ['similar']
    assert completed(invoke(native, 'feedback.apply_redisposition', dict(params, dry_run=False), key)) == result
    records = completed(invoke(native, 'feedback.overrides.list', {'alert_id': 'similar'}))['items']
    assert len(records) == 1 and records[0]['source'] == 'redisposition'
    rejected(invoke(native, 'feedback.apply_redisposition', {'override_id': 'missing'}), 404, 'RESOURCE_NOT_FOUND')

# LLM: analyst、space、alert、since 都按结构化字段匹配，游标不能跨范围重用。
# 函数用途: 验证反馈列表过滤及分页。
def test_overrides_list_filters(native):
    for name in ('first', 'second'):
        seed_alert(native, name, verdict='true_positive')
        completed(invoke(native, 'feedback.submit_override', {'alert_id': name,
            'original_verdict': 'true_positive', 'override_verdict': 'benign', 'reason': 'review'}))
    page = completed(invoke(native, 'feedback.overrides.list', {'limit': 1}))
    assert page['next_cursor']
    next_page = completed(invoke(native, 'feedback.overrides.list', {'cursor': page['next_cursor']}))
    assert next_page['items'][0]['override_id'] != page['items'][0]['override_id']
    assert len(completed(invoke(native, 'feedback.overrides.list', {'alert_id': 'first'}))['items']) == 1
    assert completed(invoke(native, 'feedback.overrides.list', {'analyst': 'not-reviewer'}))['items'] == []
    rejected(invoke(native, 'feedback.overrides.list', {'alert_id': 'first', 'cursor': page['next_cursor']}),
             410, 'CURSOR_EXPIRED')
    rejected(invoke(native, 'feedback.overrides.list', {'cursor': page['next_cursor'] + 'x'}),
             410, 'CURSOR_EXPIRED')

# LLM: 未知 AI 预测分母必须为空，不能用零冒充已观察的一致率。
# 函数用途: 验证反馈统计和 AI 预测一致率。
def test_summary_agreement_null_without_ai_verdicts(native):
    assert completed(invoke(native, 'feedback.summary', {}))['agreement_rate'] is None
    for name, confidence, override in [('unknown-model', None, 'benign'),
                                        ('confirmed', 0.9, 'true_positive'), ('changed', 0.8, 'benign')]:
        seed_alert(native, name, verdict='true_positive', confidence=confidence)
        completed(invoke(native, 'feedback.submit_override', {'alert_id': name,
            'original_verdict': 'true_positive', 'override_verdict': override, 'reason': '人工研判'}))
    result = completed(invoke(native, 'feedback.summary', {}))
    assert result['overrides_total'] == 3 and result['agreement_rate'] == 0.5
    assert result['top_rules'] == [{'rule_id': 'rule-fixture', 'overrides': 3}]
    assert sum(row['count'] for row in result['by_transition']) == result['overrides_total']

# LLM: 下游 W7/W11/W12 读取同一合同，反馈记录不携其它切片的内部状态。
# 函数用途: 验证持久反馈全字段、公开 DTO 与真正来源。
def test_feedback_record_shape_matches_contract(native):
    from agent_py_agent.agent.business_spaces.v13_store import V13Store

    seed_alert(native, verdict='true_positive')
    data = completed(invoke(native, 'feedback.submit_override', {'alert_id': 'alert-101',
        'original_verdict': 'true_positive', 'override_verdict': 'benign', 'reason': '人工覆写'}))
    row = V13Store(owner(native).home_dir).get('soc_feedback', data['override']['override_id'])
    assert set(row) == {'override_id', 'alert_id', 'space_id', 'rule_id', 'original_verdict',
        'override_verdict', 'reason', 'analyst', 'source', 'created_at', 'owner_id', 'revision', 'updated_at'}
    assert row['owner_id'] == owner(native).owner_id and row['source'] == 'override'
    assert 'owner_id' not in data['override'] and 'revision' not in data['override']

# LLM: schema 错误应在领域变更前拒绝。
# 函数用途: 验证完整信封及关闭的参数结构。
def test_envelopes_w8_feedback(native):
    rejected(invoke(native, 'feedback.submit_override', {'alert_id': 'alert-101'}), 400, 'INVALID_SCHEMA')
    rejected(invoke(native, 'feedback.summary', {'window_days': 0}), 400, 'INVALID_SCHEMA')
    rejected(invoke(native, 'feedback.overrides.list', {'owner_id': 'forged'}), 400, 'INVALID_SCHEMA')

# LLM: 没有规则的人工反馈仍计总量，但不能创建 schema 不允许的空规则排名。
# 函数用途: 回归无规则反馈汇总。
def test_summary_counts_unruled_feedback_without_null_rule_rank(native):
    row = seed_alert(native, rule_id=None)
    completed(invoke(native, 'soc.alerts.update', {'alert_id': row['alert_id'], 'verdict': 'benign'},
        revision=row['revision']))
    result = completed(invoke(native, 'feedback.summary', {}))
    assert result['overrides_total'] == 1 and result['top_rules'] == []
    assert result['agreement_rate'] is None

# LLM: 候选共享主实体即可匹配，候选数组的排列不应影响判定。
# 函数用途: 回归重处置对候选实体位置无依赖。
def test_redisposition_primary_entity_can_be_later_in_candidate(native):
    from agent_py_agent.agent.business_spaces.v13_store import V13Store

    source = seed_alert(native, 'original', verdict='true_positive')
    candidate = seed_alert(native, 'reordered', verdict='true_positive')
    candidate['entities'].insert(0, {'type': 'user', 'value': 'other'})
    harness.v13_seed(owner(native).home_dir, 'soc_alerts', {candidate['alert_id']: candidate})
    override = completed(invoke(native, 'feedback.submit_override', {'alert_id': source['alert_id'],
        'original_verdict': 'true_positive', 'override_verdict': 'benign', 'reason': 'review'}))['override']
    result = completed(invoke(native, 'feedback.apply_redisposition', {'override_id': override['override_id']}))
    assert result['applied'] == [candidate['alert_id']]
    assert V13Store(owner(native).home_dir).get('soc_alerts', candidate['alert_id'])['verdict'] == 'benign'

# ============================================================================
# Source: agent_py_agent/tests/test_v13_w8_investigations.py
# 调查领域定向测试；原媒体入库和下载为真实实现，缺 W0 时仅使用显式源替身。
# ============================================================================

# LLM: 测试 DTO 只描述输入，不冒充发行的业务授权上下文。
# 类用途: 在 W0 尚未同步时验证领域边界。
@dataclass(frozen=True)
class InvestigationCall:
    action: str
    params: dict
    context: object
    services: object
    owner_home: object
    owner_id: str
    subject_id: str
    request_id: str = 'investigation-request'
    idempotency_key: str = 'investigation-key-001'
    expected_revision: int | None = None
    clock: object = None

# LLM: 全部测试资料是合成数据，HOME 不指向真实个人资料。
# 函数用途: 构建真实 W1/媒体服务和可控制的调查事实源。
@pytest.fixture
def investigation_env(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path))
    host = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=tmp_path / 'alice',
        owner_id='local/user/alice', config_dir=tmp_path / 'alice' / 'config'),
        config=SimpleNamespace(business_spaces_enabled=True))
    services = api.BusinessServices(host)
    context = api.bind_business_context(host, authorize=lambda action, facts: True)
    # LLM: 真实观察服务负责 HMAC 游标，媒体读口从真实 registry 读回而非复制上传返回值。
    # 函数用途: 为有限测试宿主提供显式 artifacts.get 来源。
    def artifact_reader(params):
        records, size = media._registry(services)
        record = records.get(params['artifact_id'])
        if record is None or params.get('version_ref', record.metadata['version_ref']) != record.metadata['version_ref']:
            return ObservationPage()
        artifact = media._view(record)
        value = {'artifact': artifact, 'available': artifact['available'],
                 'version_ref': artifact['version_ref'], 'read_ref': artifact['artifact_id']}
        return ObservationPage(records=(ObservationRecord(value, context.owner_id,
            space_id=artifact['space_id'], task_id=artifact['task_id']),))
    services.projection = BusinessObservationService(host.home_paths.owner_home_dir,
        owner_id=context.owner_id, readers={'artifacts.get': artifact_reader})
    status, result = api.execute_business({'action': 'spaces.create', 'request_id': 'space-create',
        'params': {'name': '合成案件空间'}, 'idempotency_key': 'space-key-for-investigation'}, context, services)
    assert status == 200, result
    sid = result['data']['space_id']
    now = [datetime(2026, 11, 2, tzinfo=timezone.utc).timestamp()]
    call = InvestigationCall('soc.cases.investigate', {'space_id': sid, 'case_id': 'CASE-2026-001',
        'mode': 'autonomous_agent'}, context, services, host.home_paths.owner_home_dir,
        context.owner_id, context.subject_id, clock=lambda: now[0])
    stamp = shared.w8_now(call)
    case = {'case_id': 'CASE-2026-001', 'owner_id': call.owner_id, 'space_id': sid,
        'title': '<b>合成案件</b>', 'description': 'password=synthetic-report-secret',
        'status': 'open', 'severity': 'high', 'assignee': None, 'alert_ids': ['alert-one'],
        'merged_into': None, 'tags': [], 'custom_values': {}, 'closed_at': None,
        'revision': 0, 'created_at': stamp, 'updated_at': stamp}
    alert = {'alert_id': 'alert-one', 'owner_id': call.owner_id, 'space_id': sid,
        'title': '合成告警', 'description': 'password=synthetic-alert-secret', 'severity': 'high',
        'status': 'new', 'entities': [{'type': 'host', 'value': 'synthetic-host'}],
        'techniques': [], 'evidence_refs': []}
    tables = {'soc_cases': {case['case_id']: case}, 'soc_alerts': {alert['alert_id']: alert},
        'soc_investigations': {}, 'soc_investigation_shares': {}, 'async_jobs': {}}
    receipts, calls, sources = {}, [], {}
    job = {'job_id': 'job-one', 'task_id': 'task-one', 'agent_id': 'agent-one',
        'state': 'accepted', 'result': None, 'operation_id': 'operation-one', 'updated_at': stamp}
    task = {'task_id': 'task-one', 'thread_id': 'agent-one', 'status': 'queued',
        'result_refs': [], 'owner_id': call.owner_id, 'space_id': sid}
    host.business_reference_reader = lambda kind, rid, actor: task if rid == task['task_id'] else None

    # LLM: 仅模拟 W0 未提供的存储端口，领域函数仍使用真正公共 helper 的分页。
    # 函数用途: 在测试中执行明确的 owner/space 可见性检查。
    def access(current, row):
        if row.get('owner_id') != current.owner_id or (
                current.params.get('space_id') is not None and row.get('space_id') not in (
                    None, current.params['space_id'])):
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        return row

    # LLM: 返回独立记录，防止测试无锁共享引用掩盖覆盖问题。
    # 函数用途: 读取合成集合的一条可见记录。
    def get(current, col, rid):
        row = tables[col].get(rid)
        if row is None:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        return deepcopy(access(current, row))

    # LLM: 测试单独列出可见记录，不让其它 owner 混入聚合。
    # 函数用途: 返回完整合成列表。
    def rows(current, col):
        return [deepcopy(row) for row in tables[col].values()
                if row.get('owner_id') == current.owner_id and (
                    'space_id' not in current.params or row.get('space_id') == current.params['space_id'])]

    # LLM: 回执替身明确绑定参数，不能靠忽略幂等冲突让测试变绿。
    # 函数用途: 原子提交合成记录和回执。
    def transaction(current, collections, mutate):
        key = (current.owner_id, current.subject_id, current.action, current.idempotency_key)
        digest = json.dumps([current.params, current.expected_revision], sort_keys=True)
        if key in receipts:
            old_digest, data = receipts[key]
            if old_digest != digest:
                raise HostAPIError('IDEMPOTENCY_CONFLICT', 409)
            return deepcopy(data)
        local = {col: deepcopy(tables[col]) for col in collections}
        data = mutate(local)
        tables.update(local)
        receipts[key] = digest, deepcopy(data)
        return data

    # LLM: 冻结源替身必须首先通过真实闭合参数 schema。
    # 函数用途: 提供分页事实并把媒体读口交回真实 services。
    def frozen(current, action, params):
        calls.append((action, deepcopy(params)))
        validate_object(params, business_action_spec(action).params_schema)
        if action == 'tasks.get':
            if task.get('unavailable'):
                raise HostAPIError('HOST_NOT_READY', 503)
            return {'task': deepcopy(task)}
        if action == 'artifacts.get':
            return services.invoke(action, params, context)
        items = sources.get(action, [])
        start = int(params.get('cursor', '0'))
        end = start + params.get('limit', 50)
        cursor = str(end) if end < len(items) else None
        return {'events' if action == 'events.list' else 'items': deepcopy(items[start:end]),
                'next_cursor': cursor, 'coverage': 'partial' if cursor else 'complete'}

    # LLM: 记录模型提交次数和 context，不能把它当实际模型执行证明。
    # 函数用途: 提供 W0 accepted 回执形状。
    def start_job(current, kind, instruction, *, space_id=None):
        calls.append(('start_model_job', {'kind': kind, 'instruction': instruction, 'space_id': space_id}))
        tables['async_jobs'][job['job_id']] = dict(job, owner_id=current.owner_id, space_id=sid)
        return DomainResult(202, {'state': 'accepted', 'operation_id': job['operation_id'],
                                 'job_id': job['job_id'], 'task_id': job['task_id']})

    providers = {}
    monkeypatch.setattr(shared, 'w8_access', access)
    monkeypatch.setattr(shared, 'w8_get', get)
    monkeypatch.setattr(shared, 'w8_rows', rows)
    monkeypatch.setattr(shared, 'w8_transaction', transaction)
    monkeypatch.setattr(investigation, '_foundation', lambda: SimpleNamespace(
        v13_call_frozen=frozen, v13_start_model_job=start_job,
        v13_provider=lambda current, name: providers.get(name)))
    monkeypatch.setattr(investigation, '_job', lambda current, run: deepcopy(job))
    return SimpleNamespace(call=call, tables=tables, calls=calls, sources=sources, job=job,
        task=task, now=now, providers=providers, sid=sid, frozen=frozen)

# LLM: 原例参的空间标识由实际 W1 创建，不硬编码不存在的空间。
# 函数用途: 验证 accepted、任务关联、上下文及重复提交。
def test_investigate_example_params_returns_202_with_run_and_task(investigation_env):
    env = investigation_env
    first = investigation.investigate_case(env.call)
    assert first.http_status == 202
    assert first.data['state'] == 'accepted'
    assert first.data['task_id'] == 'task-one'
    row = env.tables['soc_investigations'][first.data['run_id']]
    assert row['job_id'] == 'job-one' and row['task_id'] == first.data['task_id']
    assert env.tables['soc_cases']['CASE-2026-001']['status'] == 'investigating'
    assert investigation.investigate_case(env.call).data == first.data
    submissions = [data for action, data in env.calls if action == 'start_model_job']
    assert len(submissions) == 1 and submissions[0]['kind'] == 'case_investigation'
    assert 'synthetic-host' in submissions[0]['instruction']
    with pytest.raises(HostAPIError, match='IDEMPOTENCY_CONFLICT'):
        investigation.investigate_case(replace(env.call, params=dict(env.call.params, instruction='different')))

# LLM: 单测覆盖任务读口刷新、job 校验终态和不可读状态。
# 函数用途: 验证状态筛选不使用旧缓存。
def test_runs_list_refreshes_state_from_tasks(investigation_env):
    env = investigation_env
    investigation.investigate_case(env.call)
    env.task['status'], env.job['state'] = 'running', 'running'
    read = replace(env.call, action='investigations.runs.list', params={'state': 'running'})
    assert investigation.list_runs(read)['items'][0]['state'] == 'running'
    env.task['status'], env.job['state'] = 'completed', 'completed'
    env.job['result'] = _conclusion()
    read = replace(read, params={'state': 'completed'})
    result = investigation.list_runs(read)['items'][0]
    assert result['state'] == 'completed' and result['verdict'] == 'benign'
    env.task['unavailable'], env.job['state'], env.job['result'] = True, 'unknown', None
    assert investigation.list_runs(replace(read, params={}))['items'][0]['state'] == 'unknown'

# LLM: 结论是合成结构，不来自真实模型。
# 函数用途: 创建符合 case_investigation schema 的结果。
def _conclusion():
    return {'verdict': 'benign', 'summary': '合成结论 password=synthetic-conclusion-secret',
        'confidence': None, 'recommended_actions': [], 'entities': [], 'techniques': []}

# LLM: 运行 ID 可读性检查必须发生在任何源调用之前。
# 函数用途: 验证其它 owner 无法访问运行。
def test_run_get_404_for_other_owner(investigation_env):
    env = investigation_env
    rid = investigation.investigate_case(env.call).data['run_id']
    other = replace(env.call, action='investigations.runs.get', owner_id='local/user/bob', params={'run_id': rid})
    with pytest.raises(HostAPIError) as error:
        investigation.get_run(other)
    assert error.value.status == 404

# LLM: 源本身跨页且混有同 agent 的其它 task，必须完整读取后再精确过滤。
# 函数用途: 验证步骤分页、完整性及游标绑定。
def test_run_events_pages_observation_steps(investigation_env):
    env = investigation_env
    rid = investigation.investigate_case(env.call).data['run_id']
    env.sources['messages.list'] = [{'message_id': 'm-' + str(index), 'task_id': 'task-one',
        'role': 'assistant', 'text': '合成假设 ' + str(index), 'occurred_at': '2026-11-02T00:00:00Z'}
        for index in range(241)] + [{'message_id': 'other-task', 'task_id': 'task-other',
                                    'role': 'assistant', 'text': '不可混入', 'occurred_at': None}]
    read = replace(env.call, action='investigations.runs.events', params={'run_id': rid, 'limit': 100})
    items, cursor = [], None
    while True:
        params = dict(read.params)
        if cursor:
            params['cursor'] = cursor
        page = investigation.run_events(replace(read, params=params))
        items.extend(page['items'])
        cursor = page['next_cursor']
        if not cursor:
            break
    assert len(items) == 241 and len({row['ref'] for row in items}) == 241
    assert page['coverage'] == 'complete'
    first = investigation.run_events(read)
    env.tables['soc_investigations']['run-other'] = dict(
        env.tables['soc_investigations'][rid], run_id='run-other')
    with pytest.raises(BusinessError) as error:
        investigation.run_events(replace(read, params={'run_id': 'run-other', 'cursor': first['next_cursor']}))
    assert error.value.code == 'CURSOR_EXPIRED'

# LLM: explain 必须是记录投影，禁止额外模型提交。
# 函数用途: 验证回放包含假设、工具、证据及结论。
def test_replay_example_run_id_full_timeline_and_explain_without_model(investigation_env):
    env = investigation_env
    rid = investigation.investigate_case(env.call).data['run_id']
    env.tables['soc_investigations']['run-sample-01'] = dict(
        env.tables['soc_investigations'][rid], run_id='run-sample-01')
    env.job.update(state='completed', result=_conclusion())
    env.task['status'] = 'completed'
    env.sources['messages.list'] = [{'message_id': 'hypothesis-one', 'task_id': 'task-one',
        'role': 'assistant', 'text': '已记录假设', 'occurred_at': '2026-11-02T00:00:00Z'}]
    env.sources['tool_calls.list'] = [{'operation_id': 'tool-one', 'task_id': 'task-one',
                                      'tool_name': 'synthetic_read', 'status': 'completed'}]
    env.sources['artifacts.list'] = [{'artifact_id': 'evidence-one', 'task_id': 'task-one',
        'name': '合成证据', 'space_id': env.sid, 'version_ref': 'version-one', 'mime_type': 'text/plain',
        'size_bytes': 1, 'content_sha256': 'a' * 64, 'available': True}]
    read = replace(env.call, action='investigations.runs.replay', params={'run_id': 'run-sample-01'})
    data = investigation.replay(read)
    assert {row['kind'] for row in data['steps']} == {'hypothesis', 'tool_call', 'evidence', 'conclusion'}
    assert data['coverage'] == 'complete' and data['conclusion']['verdict'] == 'benign'
    explanation = investigation.explain(replace(read, action='investigations.runs.explain'))
    assert explanation['decision'] == 'benign'
    assert explanation['evidence_refs'] == ['evidence-one'] and explanation['tool_call_ids'] == ['tool-one']
    assert 'synthetic-conclusion-secret' not in explanation['rationale']
    assert len([row for row in env.calls if row[0] == 'start_model_job']) == 1

# LLM: 快照冻结、幂等和有效期均按注入时钟验证。
# 函数用途: 验证分享不泄露合成秘密、不跨 owner、不会被后续记录修改。
def test_share_snapshot_is_redacted_and_expires(investigation_env):
    env = investigation_env
    rid = investigation.investigate_case(env.call).data['run_id']
    env.job.update(state='completed', result=_conclusion())
    create = replace(env.call, action='investigations.share.create', params={'run_id': rid, 'expires_in_hours': 1})
    first = investigation.create_share(create)
    assert first == investigation.create_share(create)
    read = replace(create, action='investigations.share.get', params={'slug': first['slug']})
    snapshot = investigation.get_share(read)
    assert snapshot['redacted'] and 'synthetic-conclusion-secret' not in json.dumps(snapshot)
    env.job['result']['summary'] = '后续修改'
    assert investigation.get_share(read) == snapshot
    with pytest.raises(HostAPIError) as other:
        investigation.get_share(replace(read, owner_id='local/user/bob'))
    assert other.value.status == 404
    env.now[0] += 3600
    with pytest.raises(HostAPIError) as expired:
        investigation.get_share(read)
    assert expired.value.status == 404

# LLM: JSON parser 不修复模型输出，也不接受字段覆盖或非有限数。
# 函数用途: 验证严格结构化 parse。
@pytest.mark.parametrize('text', ['[]', 'null', '{"a":1,"a":2}', '{"a":NaN}', '{"a":1e999}', '```json\n{}\n```'])
def test_finalize_rejects_invalid_structured_result(text):
    with pytest.raises(ValueError):
        investigation.finalize_investigation(text)

# LLM: 合法 JSON 保留原字段，最终字段约束由 registry schema 守住。
# 函数用途: 验证结构化结论解析无额外推理。
def test_finalize_preserves_structured_result():
    result = _conclusion()
    assert investigation.finalize_investigation(json.dumps(result)) == result

# LLM: 原生读口的 BusinessError 不能使只读账本误报成功，也不能吞掉撤权。
# 函数用途: 验证来源缺口与安全拒绝分开处理。
def test_source_unavailable_is_partial_but_revocation_propagates(investigation_env, monkeypatch):
    env = investigation_env
    rid = investigation.investigate_case(env.call).data['run_id']
    read = replace(env.call, action='investigations.runs.replay', params={'run_id': rid})

    # LLM: 明确模拟不可用的原生源。
    # 函数用途: 产生结构化来源读取失败。
    def missing_source(call, action, params):
        raise BusinessError('HOST_NOT_READY')

    monkeypatch.setattr(investigation, '_frozen', missing_source)
    assert investigation.replay(read)['coverage'] == 'partial'

    # LLM: 安全拒绝不得被转换成 unknown 后披露其它记录。
    # 函数用途: 模拟调用过程中实时撤权。
    def revoked_source(call, action, params):
        raise HostAPIError('CAPABILITY_DENIED', 403)

    monkeypatch.setattr(investigation, '_frozen', revoked_source)
    with pytest.raises(HostAPIError) as revoked:
        investigation.replay(read)
    assert revoked.value.status == 403

# LLM: 回放 schema 有上限，但 explain 不应把后续已有步骤当作不存在。
# 函数用途: 验证超长时间轴 partial 以及指定后续步骤解释。
def test_replay_bound_and_late_step_explain(investigation_env):
    env = investigation_env
    rid = investigation.investigate_case(env.call).data['run_id']
    env.sources['messages.list'] = [{'message_id': 'long-' + str(index), 'task_id': 'task-one',
        'role': 'assistant', 'text': '步骤' + str(index), 'occurred_at': '2026-11-02T00:00:00Z'}
        for index in range(1002)]
    read = replace(env.call, action='investigations.runs.replay', params={'run_id': rid})
    data = investigation.replay(read)
    assert len(data['steps']) == 1000 and data['coverage'] == 'partial'
    explanation = investigation.explain(replace(read, action='investigations.runs.explain',
                                               params={'run_id': rid, 'step_seq': 1001}))
    assert explanation['step_seq'] == 1001 and explanation['decision'].startswith('步骤')

# LLM: list 的分页范围和状态过滤必须共同绑定游标。
# 函数用途: 验证完整运行列表及过滤变更拒绝旧游标。
def test_runs_page_all_records_and_cursor_scope(investigation_env):
    env = investigation_env
    rid = investigation.investigate_case(env.call).data['run_id']
    row = env.tables['soc_investigations'].pop(rid)
    for index in range(137):
        record = dict(row, run_id='run-index-' + str(index))
        env.tables['soc_investigations'][record['run_id']] = record
    read = replace(env.call, action='investigations.runs.list', params={'limit': 50})
    first = investigation.list_runs(read)
    second = investigation.list_runs(replace(read, params={'limit': 50, 'cursor': first['next_cursor']}))
    third = investigation.list_runs(replace(read, params={'limit': 50, 'cursor': second['next_cursor']}))
    combined = first['items'] + second['items'] + third['items']
    assert len(combined) == 137 and len({row['run_id'] for row in combined}) == 137
    assert third['next_cursor'] is None
    with pytest.raises(BusinessError) as stale:
        investigation.list_runs(replace(read, params={'state': 'accepted', 'cursor': first['next_cursor']}))
    assert stale.value.code == 'CURSOR_EXPIRED'

# LLM: 此测试必须在 W0 和 W8 registry 同步后运行，不用源替身替代 HTTP 主链路。
# 函数用途: 验证真实 native 受理、任务、账本、报告入库与鉴权下载往返。
def test_native_investigation_report_and_share_roundtrip(native_http):
    import time

    from agent_py_agent.tests.test_v13_w0_harness import v13_call, v13_space
    from agent_py_agent.tests.test_v13_w8_alerts import drive_job, echo_profile

    echo_profile(native_http)
    sid = v13_space(native_http)
    status, created = v13_call(native_http, 'soc.cases.create', {'space_id': sid,
        'case_id': 'CASE-2026-001', 'title': '合成调查', 'description': 'password=synthetic-native-secret',
        'severity': 'high'}, user='business-alice', idempotency_key='native-investigation-case-create')
    assert status == 200 and created['error'] is None, created
    params = {'space_id': sid, 'case_id': 'CASE-2026-001', 'mode': 'autonomous_agent'}
    status, accepted = v13_call(native_http, 'soc.cases.investigate', params, user='business-alice',
                               idempotency_key='native-investigation-start-one')
    assert status == 202 and accepted['error'] is None, accepted
    run = accepted['data']
    status, task = v13_call(native_http, 'tasks.get', {'task_id': run['task_id']}, user='business-alice')
    assert status == 200 and task['data']['task']['task_id'] == run['task_id'], task
    status, replayed = v13_call(native_http, 'soc.cases.investigate', params, user='business-alice',
                              idempotency_key='native-investigation-start-one')
    assert status == 202 and replayed['data'] == accepted['data'], replayed
    drive_job(native_http, run['job_id'])
    for attempt in range(100):
        status, job = v13_call(native_http, 'async_jobs.get', {'job_id': run['job_id']}, user='business-alice')
        assert status == 200, job
        if job['data']['state'] in {'completed', 'failed'}:
            break
        time.sleep(0.05)
    assert job['data']['state'] == 'failed' and job['data']['error']['code'] == 'JOB_RESULT_INVALID', job
    # 原 echo 正文不符合 job schema；失败必须如实投影，不伪造完成结论。
    status, ledger = v13_call(native_http, 'investigations.runs.get', {'run_id': run['run_id']},
                             user='business-alice')
    assert status == 200 and ledger['data']['run']['state'] == job['data']['state'], ledger
    status, owner_b = v13_call(native_http, 'investigations.runs.get', {'run_id': run['run_id']},
                              user='business-bob')
    assert status == 404 and owner_b['error']['code'] == 'RESOURCE_NOT_FOUND', owner_b
    status, share = v13_call(native_http, 'investigations.share.create', {'run_id': run['run_id']},
        user='business-alice', idempotency_key='native-investigation-share-one')
    assert status == 200 and share['error'] is None, share
    status, snapshot = v13_call(native_http, 'investigations.share.get', {'slug': share['data']['slug']},
                               user='business-alice')
    assert status == 200 and snapshot['data']['redacted'], snapshot

# LLM: 模型替身只产生结构化结论；真实任务、终态校验、账本和分享不使用替身。
# 函数用途: 验证调查成功终态和已记录结论回放的原生链路。
def test_native_structured_investigation_finishes_and_replays(native_http, monkeypatch):
    import time

    from agent_py_agent.agent.backends.base import EchoBackend, ModelResponse
    from agent_py_agent.tests.test_v13_w0_harness import v13_call
    from agent_py_agent.tests.test_v13_w8_alerts import drive_job, echo_profile
    from agent_py_agent.tests.test_v13_w8_cases import completed, create, ingest, rejected

    echo_profile(native_http)
    case = create(native_http, ingest(native_http))
    seen = []

    # LLM: 没有真实模型/外网参与，确定性结论只服务于底座与切片接线验证。
    # 函数用途: 为实际后台任务返回可被唯一 job schema 验证的输出。
    def generate(self, prompt, on_chunk=None, tools=None, tool_choice=None, messages=None,
                 on_thinking_delta=None, request_options=None):
        seen.append(prompt)
        return ModelResponse(text=json.dumps(_conclusion()), backend='echo')

    monkeypatch.setattr(EchoBackend, 'generate', generate)
    status, accepted = v13_call(native_http, 'soc.cases.investigate', {'case_id': case['case_id']},
        user='business-alice', idempotency_key='structured-native-investigation')
    assert status == 202, accepted
    run = accepted['data']
    drive_job(native_http, run['job_id'])
    assert seen, '真实任务没有到达合成模型入口'
    for attempt in range(200):
        job = completed(v13_call(native_http, 'async_jobs.get', {'job_id': run['job_id']}, user='business-alice'))
        if job['state'] in {'completed', 'failed'}:
            break
        time.sleep(0.05)
    assert seen and job['state'] == 'completed', job
    current = completed(v13_call(native_http, 'investigations.runs.get', {'run_id': run['run_id']},
                                user='business-alice'))['run']
    assert current['state'] == 'completed' and current['verdict'] == 'benign' and current['finished_at']
    replayed = completed(v13_call(native_http, 'investigations.runs.replay', {'run_id': run['run_id']},
                                 user='business-alice'))
    assert replayed['conclusion']['verdict'] == 'benign'
    assert any(row['kind'] == 'conclusion' for row in replayed['steps'])
    explanation = completed(v13_call(native_http, 'investigations.runs.explain', {'run_id': run['run_id']},
                                    user='business-alice'))
    assert explanation['decision'] == 'benign' and 'synthetic-conclusion-secret' not in explanation['rationale']
    before = len(seen)
    completed(v13_call(native_http, 'investigations.runs.explain', {'run_id': run['run_id']}, user='business-alice'))
    assert len(seen) == before
    rejected(v13_call(native_http, 'investigations.runs.get', {'run_id': run['run_id']}, user='business-bob'),
             404, 'RESOURCE_NOT_FOUND')

# ============================================================================
# Source: agent_py_agent/tests/test_v13_w8_replay_windows.py
# 回放评测的固定样本、指标与隔离边界测试；数据全部为合成记录。
# ============================================================================

# LLM: 仅为领域单测显式构造 F10 输入，不将此替身当真实 Gateway 验收。
# 类用途: 保存可替换的测试调用事实。
@dataclass(frozen=True)
class ReplayCall:
    action: str
    params: dict
    owner_id: str = 'local/main'
    subject_id: str = 'analyst'
    idempotency_key: str = ''
    expected_revision: int | None = None
    services: object = None
    context: object = None
    owner_home: object = None
    clock: object = None

# LLM: 该内存替身只验证领域编排，不替代 W0 持久化/授权/HTTP 测试。
# 类用途: 提供受控原生边界，记录候选输入和外部派工。
class ReplayBoundary:
    # LLM: 每个用例有独立状态和事务回执，避免测试共享数据。
    # 函数用途: 初始化合成记录与计量。
    def __init__(self, home):
        self.home = home
        self.observation = BusinessObservationService(home, owner_id='local/main', cursor_key=b'synthetic-cursor-key'.ljust(32, b'x'))
        self.tables = {'soc_alerts': {}, 'soc_feedback': {}, 'soc_replay_evaluations': {}, 'async_jobs': {}}
        self.receipts = {}
        self.agents = {}
        self.jobs = {}
        self.instructions = []
        self.allowed = True
        self.timings = []
        self.actual = {'coverage': 'unknown', 'currency': None, 'cost': None}
        self.created = []

    # LLM: 替身保留回执摘要冲突语义；生产实现仍使用 W0 store。
    # 函数用途: 执行领域事务回调并保存回执。
    def transaction(self, collection, mutate, *, idempotency_key, params_digest):
        key = (collection, idempotency_key)
        if key in self.receipts:
            digest, result = self.receipts[key]
            if digest != params_digest:
                raise HostAPIError('IDEMPOTENCY_CONFLICT', 409)
            return deepcopy(result)
        rows = deepcopy(self.tables[collection])
        result = mutate(rows)
        self.tables[collection] = rows
        self.receipts[key] = (params_digest, deepcopy(result))
        return deepcopy(result)

    # LLM: 精确查找返回独立记录，避免锁外对象原地改变测试存储。
    # 函数用途: 读取测试集合。
    def list(self, collection):
        return deepcopy(list(self.tables[collection].values()))

    # LLM: 不把缺失 ID 变成空成功。
    # 函数用途: 读取合成记录。
    def get(self, collection, record_id):
        return deepcopy(self.tables[collection].get(record_id))

    # LLM: 单测显式触发 owner 与实时拒绝，不从文案推断权限。
    # 函数用途: 模拟下游实时访问检查。
    def access(self, call, row):
        if not self.allowed:
            raise HostAPIError('CAPABILITY_DENIED', 403)
        if row.get('owner_id', call.owner_id) != call.owner_id:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)

    # LLM: frozen 调用严格按动作分支，未声明动作不返回假成功。
    # 函数用途: 提供可记录的配置档、候选创建和计量读口。
    def frozen(self, call, action, params, *, idempotency_key=None, expected_revision=None):
        if action == 'model_profiles.list':
            return {'items': [], 'next_cursor': None}
        if action == 'background_agents.create':
            if idempotency_key not in self.agents:
                self.agents[idempotency_key] = {'agent_id': 'candidate-' + str(len(self.agents))}
                self.created.append(deepcopy(params))
            return {'agent': self.agents[idempotency_key]}
        if action == 'executions.list':
            return {'items': deepcopy(self.timings), 'next_cursor': None, 'coverage': 'complete'}
        if action == 'usage.get':
            return {'actual': deepcopy(self.actual)}
        raise AssertionError(action)

    # LLM: 模型边界记录输入，供标签泄露与候选归属断言核对。
    # 函数用途: 为合成候选创建一次可重放作业。
    def start_job(self, call, kind, instruction, *, space_id=None, agent_id=None):
        if call.idempotency_key not in self.jobs:
            self.instructions.append(instruction)
            self.jobs[call.idempotency_key] = {'job_id': 'job-' + str(len(self.jobs)),
                'task_id': 'task-' + str(len(self.jobs)), 'space_id': space_id,
                'agent_id': agent_id, 'kind': kind, 'state': 'accepted', 'result': None,
                'owner_id': call.owner_id, 'updated_at': '2026-11-02T12:00:00Z'}
        job = self.jobs[call.idempotency_key]
        self.tables['async_jobs'][job['job_id']] = job
        return DomainResult(202, {'state': 'accepted', 'operation_id': 'op-' + job['job_id'],
            'job_id': job['job_id'], 'task_id': job['task_id']})

    # LLM: W0 作业是唯一状态源，切片不得直接修改其集合。
    # 函数用途: 提供 services.invoke 的合成作业读取。
    def invoke(self, action, params, context):
        assert action == 'async_jobs.get'
        return deepcopy(next(job for job in self.jobs.values() if job['job_id'] == params['job_id']))

# LLM: 仅单测替换显式基础端口，保留真实模块的错误映射和冻结 schema；fixture 结束自动恢复。
# 函数用途: 建立完全隔离的领域测试边界。
@pytest.fixture
def boundary(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path))
    env = ReplayBoundary(tmp_path)
    from agent_py_agent.agent.business_spaces import v13_store
    from agent_py_agent.agent.gateway_parts import business_api_v13

    monkeypatch.setattr(business_api_v13, 'v13_require_access', env.access)
    monkeypatch.setattr(business_api_v13, 'v13_resolve_space',
                        lambda call: {'space_id': call.params.get('space_id', 'space-a')})
    monkeypatch.setattr(business_api_v13, 'v13_call_frozen', env.frozen)
    monkeypatch.setattr(business_api_v13, 'v13_start_model_job', env.start_job)
    monkeypatch.setattr(v13_store, 'v13_store_for', lambda call, scope: env)
    return env

# LLM: 时间、标签和原 verdict 均为显式合成值。
# 函数用途: 为单测装入三个已复核样本及窗口外记录。
def _seed(boundary):
    for index, label in enumerate(('true_positive', 'benign', 'true_positive')):
        alert_id = 'alert-' + str(index)
        boundary.tables['soc_alerts'][alert_id] = {'alert_id': alert_id, 'space_id': 'space-a',
            'owner_id': 'local/main', 'title': alert_id, 'description': '合成历史事实',
            'severity': 'medium', 'created_at': f'2026-11-0{index + 1}T10:00:00Z', 'verdict': label,
            'disposition': 'private_disposition', 'tags': ['private_tag'], 'evidence_refs': ['private-ref']}
        boundary.tables['soc_feedback'][alert_id] = {'override_id': alert_id, 'alert_id': alert_id,
            'space_id': 'space-a', 'owner_id': 'local/main', 'original_verdict': 'benign',
            'override_verdict': label, 'created_at': '2026-11-02T12:00:00Z', 'revision': 0}

# LLM: 所有测试调用使用显式测试身份和可信固定时钟。
# 函数用途: 生成 F10 领域调用。
def _call_w8_replay_windows(boundary, action, params, key=''):
    return ReplayCall('evaluations.replay.' + action, params, idempotency_key=key,
        services=SimpleNamespace(invoke=boundary.invoke, observation=boundary.observation), owner_home=boundary.home,
        clock=lambda: 1793620800.0)

# LLM: 参数遵循精确闭合 schema，不塞派工私有字段。
# 函数用途: 返回合成回放启动参数。
def _params():
    return {'space_id': 'space-a', 'candidate': {'model_profile_id': 'candidate-profile'},
        'replay_window': {'since': '2026-11-01T00:00:00Z', 'until': '2026-11-04T00:00:00Z'}}

# LLM: 测试使用权威输入 schema 验证产品 DTO，不复制另一份 schema。
# 函数用途: 验证动作结果契约。
def _validate(action, data, accepted=False):
    spec = registered_spec('evaluations.replay.' + action)
    schema = spec.accepted_result_schema if accepted else spec.result_schema
    validate_object(thaw(data), schema)

# LLM: 能力发现统计可见标签，且结果满足公开闭合 schema。
# 函数用途: 验证最小示例能力发现。
def test_capabilities_example_empty_params_counts_labelled_alerts(boundary):
    _seed(boundary)
    result = replay.capabilities(_call_w8_replay_windows(boundary, 'capabilities', {}))
    assert result['datasets'][0]['labelled_count'] == 3
    assert result['max_sample_size'] == 1000
    _validate('capabilities', result)

# LLM: 检查模型真实收到的结构化输入，不只检查调用存在。
# 函数用途: 验证窗口采样、最新标签、候选模型与标签隔离。
def test_start_samples_labelled_alerts_in_window_and_withholds_labels(boundary):
    _seed(boundary)
    boundary.tables['soc_feedback']['latest'] = {**boundary.tables['soc_feedback']['alert-2'],
        'override_id': 'latest', 'override_verdict': 'benign', 'created_at': '2026-11-03T12:00:00Z'}
    call = _call_w8_replay_windows(boundary, 'start', {**_params(), 'sample_size': 2}, 'start-synthetic-key')
    result = replay.start(call)
    _validate('start', result.data, accepted=True)
    row = boundary.tables['soc_replay_evaluations'][result.data['evaluation_id']]
    assert row['sample_alert_ids'] == ['alert-2', 'alert-1']
    assert row['decisions'][0]['expected_verdict'] == 'benign'
    facts = json.loads(boundary.instructions[0].split('\n', 1)[1])
    assert [alert['alert_id'] for alert in facts['alerts']] == row['sample_alert_ids']
    assert all('verdict' not in alert and 'disposition' not in alert and 'tags' not in alert
               and 'evidence_refs' not in alert for alert in facts['alerts'])
    assert boundary.created == [{'space_id': 'space-a', 'name': row['evaluation_id'],
        'purpose': '独立离线评测候选', 'after_task': 'retire', 'model_profile_id': 'candidate-profile'}]
    assert row['agent_id'] == next(iter(boundary.jobs.values()))['agent_id']
    assert replay.start(call).data == result.data
    assert len(boundary.jobs) == len(boundary.agents) == len(boundary.instructions) == 1
    with pytest.raises(HostAPIError, match='IDEMPOTENCY_CONFLICT'):
        replay.start(replace(call, params={**call.params, 'sample_size': 1}))

# LLM: 对空数据和逆序窗口要明确拒绝，不能制造空成功作业。
# 函数用途: 验证无标签和窗口错误。
def test_no_labels_and_invalid_window(boundary):
    with pytest.raises(HostAPIError) as error:
        replay.start(_call_w8_replay_windows(boundary, 'start', _params(), 'empty-synthetic-key'))
    assert (error.value.status, error.value.code) == (409, 'STATE_CONFLICT')
    assert not boundary.jobs and not boundary.agents
    params = _params()
    params['replay_window'] = {'since': '2026-11-04T00:00:00Z', 'until': '2026-11-01T00:00:00Z'}
    with pytest.raises(HostAPIError, match='INVALID_SCHEMA'):
        replay.start(_call_w8_replay_windows(boundary, 'start', params, 'inverse-window-key'))

# LLM: 完成结果与真实执行观测显式装入替身，不用模型正文推导状态。
# 函数用途: 完成一个合成评测作业。
def _complete(boundary, verdicts=('true_positive', 'true_positive', 'benign')):
    _seed(boundary)
    accepted = replay.start(_call_w8_replay_windows(boundary, 'start', _params(), 'metrics-synthetic-key'))
    row = boundary.tables['soc_replay_evaluations'][accepted.data['evaluation_id']]
    job = next(iter(boundary.jobs.values()))
    job['state'] = 'completed'
    job['result'] = {'decisions': [{'alert_id': alert_id, 'verdict': verdict, 'rationale': '合成依据'}
        for alert_id, verdict in zip(row['sample_alert_ids'], verdicts)]}
    return row['evaluation_id'], job

# LLM: 基线与候选用相同样本；时延只能来自实际时序而非模型自报。
# 函数用途: 验证准确率、精确率、召回率、延迟与真实用量成本。
def test_get_finalizes_metrics_from_job_result(boundary):
    evaluation_id, job = _complete(boundary)
    boundary.timings = [{'task_id': job['task_id'], 'started_at': '2026-11-02T12:00:00Z',
                         'ended_at': '2026-11-02T12:00:02Z'}]
    boundary.actual = {'coverage': 'complete', 'currency': 'USD', 'cost': 0.125}
    result = replay.get(_call_w8_replay_windows(boundary, 'get', {'evaluation_id': evaluation_id}))
    assert result['evaluation']['metrics'] == {'accuracy': 1 / 3, 'precision': 1 / 2,
        'recall': 1 / 2, 'baseline_accuracy': 1 / 3,
        'latency_ms_p50': 2000, 'latency_ms_p95': 2000, 'cost_usd': 0.125}
    assert result['evaluation']['state'] == 'completed'
    assert not {'owner_id', 'sample_alert_ids', 'decisions'} & result['evaluation'].keys()
    _validate('get', result)
    stored = deepcopy(boundary.tables['soc_replay_evaluations'][evaluation_id])
    job['state'] = 'running'
    assert replay.get(_call_w8_replay_windows(boundary, 'get', {'evaluation_id': evaluation_id})) == result
    assert boundary.tables['soc_replay_evaluations'][evaluation_id] == stored

# LLM: 不完整来源、估算成本和缺失时间不能变成零值或可比较指标。
# 函数用途: 验证未知用量和时延保持 null。
def test_unknown_metrics_remain_null(boundary):
    evaluation_id, job = _complete(boundary)
    boundary.actual = {'coverage': 'partial', 'currency': 'USD', 'cost': 9.0}
    boundary.timings = [{'task_id': job['task_id'], 'started_at': None, 'ended_at': None}]
    metrics = replay.get(_call_w8_replay_windows(boundary, 'get', {'evaluation_id': evaluation_id}))['evaluation']['metrics']
    assert metrics['latency_ms_p50'] is None
    assert metrics['latency_ms_p95'] is None
    assert metrics['cost_usd'] is None
    assert all(item['latency_ms'] is None for item in boundary.tables['soc_replay_evaluations'][evaluation_id]['decisions'])

# LLM: 少报、多报、重复或换 ID 均不能作为同一样本评测完成。
# 函数用途: 验证候选 ID 集合与结构完整性。
@pytest.mark.parametrize('change', ['missing', 'extra', 'duplicate', 'foreign', 'extra_field'])
def test_invalid_sample_result_fails_without_metrics(boundary, change):
    evaluation_id, job = _complete(boundary)
    rows = job['result']['decisions']
    if change == 'missing':
        rows.pop()
    elif change == 'extra':
        rows.append({'alert_id': 'unknown-id', 'verdict': 'benign', 'rationale': ''})
    elif change == 'duplicate':
        rows[1] = deepcopy(rows[0])
    elif change == 'foreign':
        rows[1]['alert_id'] = 'unknown-id'
    else:
        rows[1]['latency_ms'] = 1
    result = replay.get(_call_w8_replay_windows(boundary, 'get', {'evaluation_id': evaluation_id}))
    assert result['evaluation']['state'] == 'failed'
    assert result['evaluation']['metrics']['accuracy'] is None
    with pytest.raises(HostAPIError) as error:
        replay.export(_call_w8_replay_windows(boundary, 'export', {'evaluation_id': evaluation_id, 'format': 'json'}, 'export-invalid-key'))
    assert (error.value.status, error.value.code) == (409, 'STATE_CONFLICT')

# LLM: 使用原 HMAC cursor 服务验证作用域，而不是自制总返回成功的游标。
# 函数用途: 验证分页、mismatch、跨过滤/主体及篡改游标拒绝。
def test_decisions_page_and_mismatch_filter(boundary):
    evaluation_id, job = _complete(boundary)
    call = _call_w8_replay_windows(boundary, 'decisions', {'evaluation_id': evaluation_id, 'limit': 1})
    first = replay.decisions(call)
    _validate('decisions', first)
    assert first['items'][0]['alert_id'] == 'alert-2'
    assert first['next_cursor'] is not None
    second = replay.decisions(replace(call, params={**call.params, 'cursor': first['next_cursor']}))
    assert second['items'][0]['alert_id'] == 'alert-1'
    mismatch = replay.decisions(replace(call, params={'evaluation_id': evaluation_id, 'mismatches_only': True}))
    assert [item['alert_id'] for item in mismatch['items']] == ['alert-1', 'alert-0']
    for params, subject in (({**call.params, 'cursor': first['next_cursor'], 'mismatches_only': True}, 'analyst'),
                            ({**call.params, 'cursor': first['next_cursor']}, 'other-subject'),
                            ({**call.params, 'cursor': 'invalid-cursor'}, 'analyst')):
        with pytest.raises(BusinessError) as error:
            replay.decisions(replace(call, params=params, subject_id=subject))
        assert _error_code(error.value)[:2] == ('CURSOR_EXPIRED', 410)

# LLM: 单项不存在、其它 owner 与实时撤权须分别保留其边界错误。
# 函数用途: 验证读取和导出的 scope 检查先于披露。
def test_owner_and_live_scope_errors(boundary):
    evaluation_id, job = _complete(boundary)
    call = _call_w8_replay_windows(boundary, 'get', {'evaluation_id': evaluation_id})
    with pytest.raises(HostAPIError) as error:
        replay.get(replace(call, owner_id='local/other'))
    assert (error.value.status, error.value.code) == (404, 'RESOURCE_NOT_FOUND')
    with pytest.raises(HostAPIError) as error:
        replay.get(replace(call, params={'evaluation_id': 'missing-id'}))
    assert error.value.code == 'RESOURCE_NOT_FOUND'
    boundary.allowed = False
    with pytest.raises(HostAPIError) as error:
        replay.get(call)
    assert (error.value.status, error.value.code) == (403, 'CAPABILITY_DENIED')

# LLM: JSON 之外的正文、错误类型和未知字段不能进入结构化结果。
# 函数用途: 验证纯 JSON job finalizer 的拒绝行为。
@pytest.mark.parametrize('text', ['not-json', '[]', '{}', '{"decisions":null}',
    '{"decisions":[],"decisions":[]}',
    '{"decisions":[{"alert_id":"a","verdict":"benign","rationale":"","extra":1}]}'])
def test_finalize_replay_rejects_invalid_json(text):
    with pytest.raises(ValueError):
        replay.finalize_replay(text)

# LLM: 仅任务引用读口为合成依赖；空间、活跃上下文、配额、登记和 blob 读写均用生产实现。
# 函数用途: 验证 JSON/CSV 评测报告进入真实 artifact store 并能完整读回。
def test_export_json_csv_artifacts(boundary):
    from agent_py_agent.agent.gateway_parts.business_artifacts_http import artifact_content

    evaluation_id, job = _complete(boundary)
    replay.get(_call_w8_replay_windows(boundary, 'get', {'evaluation_id': evaluation_id}))
    host = SimpleNamespace(home_paths=SimpleNamespace(owner_id='local/main', owner_home_dir=boundary.home),
                           config=SimpleNamespace(business_spaces_enabled=True))
    services = BusinessServices(host)
    context = bind_business_context(host, subject_id='analyst', authorize=lambda action, facts: boundary.allowed)
    space = services.spaces.create({'name': '回放产物测试'}, actor=services.actor(context),
                                   idempotency_key='artifact-space-synthetic')
    boundary.tables['soc_replay_evaluations'][evaluation_id]['space_id'] = space['space_id']
    host.business_reference_reader = lambda kind, ref, actor: ({'owner_id': 'local/main', 'space_id': space['space_id']}
        if kind == 'task' and ref == job['task_id'] else None)
    exports = []
    for format_name in ('json', 'csv'):
        call = replace(_call_w8_replay_windows(boundary, 'export', {'evaluation_id': evaluation_id, 'format': format_name},
                             'artifact-export-' + format_name), services=services, context=context)
        result = replay.export(call)
        _validate('export', result)
        artifact = result['export']
        exports.append(artifact)
        status, headers, content = artifact_content(artifact['artifact_id'], artifact['version_ref'], context, services)
        assert status == 200
        assert len(content) == artifact['size_bytes']
        assert hashlib.sha256(content).hexdigest() == artifact['content_sha256']
        assert replay.export(call) == result
        if format_name == 'json':
            report = json.loads(content)
            assert report['evaluation']['evaluation_id'] == evaluation_id
            assert len(report['decisions']) == 3
            assert report['metrics']['accuracy'] == 1 / 3
            assert report['metrics']['cost_usd'] is None
            assert 'owner_id' not in report['evaluation']
        else:
            rows = list(csv.DictReader(content.decode().splitlines()))
            assert len(rows) == 3
            assert {row['alert_id'] for row in rows} == {'alert-0', 'alert-1', 'alert-2'}
            assert {row['evaluation_id'] for row in rows} == {evaluation_id}
            assert all(float(row['metric_accuracy']) == 1 / 3 for row in rows)
        with pytest.raises(HostAPIError) as error:
            replay.export(replace(call, params={**call.params, 'format': 'csv' if format_name == 'json' else 'json'}))
        assert (error.value.status, error.value.code) == (409, 'IDEMPOTENCY_CONFLICT')
    assert exports[0]['artifact_id'] != exports[1]['artifact_id']
    boundary.allowed = False
    with pytest.raises(HostAPIError) as error:
        artifact_content(exports[0]['artifact_id'], exports[0]['version_ref'], context, services)
    assert error.value.code == 'RESOURCE_NOT_FOUND'

# LLM: 未完成任务不能导出，不能把空结果包装为完成报告。
# 函数用途: 验证异步未完成导出的准确冲突错误。
def test_export_before_completion_is_409(boundary):
    _seed(boundary)
    accepted = replay.start(_call_w8_replay_windows(boundary, 'start', _params(), 'unfinished-synthetic'))
    with pytest.raises(HostAPIError) as error:
        replay.export(_call_w8_replay_windows(boundary, 'export', {'evaluation_id': accepted.data['evaluation_id'], 'format': 'json'},
                            'unfinished-export'))
    assert (error.value.status, error.value.code) == (409, 'STATE_CONFLICT')

# LLM: 列表和详情共用同一规范视图，列表游标绑定过滤和结果版本。
# 函数用途: 验证评测列表、详情、state 过滤和分页冲突。
def test_list_and_get(boundary):
    _seed(boundary)
    first = replay.start(_call_w8_replay_windows(boundary, 'start', _params(), 'first-evaluation-key')).data['evaluation_id']
    second = replay.start(_call_w8_replay_windows(boundary, 'start', _params(), 'second-evaluation-key')).data['evaluation_id']
    call = _call_w8_replay_windows(boundary, 'list', {'space_id': 'space-a', 'limit': 1})
    page = replay.list_evaluations(call)
    _validate('list', page)
    assert len(page['items']) == 1
    assert page['next_cursor'] is not None
    more = replay.list_evaluations(replace(call, params={**call.params, 'cursor': page['next_cursor']}))
    assert {row['evaluation_id'] for row in page['items'] + more['items']} == {first, second}
    details = replay.get(_call_w8_replay_windows(boundary, 'get', {'evaluation_id': first}))
    assert details['evaluation'] in page['items'] + more['items']
    assert replay.list_evaluations(replace(call, params={'state': 'completed'}))['items'] == []
    with pytest.raises(BusinessError) as error:
        replay.list_evaluations(replace(call, params={**call.params, 'state': 'running', 'cursor': page['next_cursor']}))
    assert _error_code(error.value)[:2] == ('CURSOR_EXPIRED', 410)

# LLM: 初次回执固定标签；后续反馈或样本删除不能改变同键派工输入。
# 函数用途: 验证幂等重放不重新采样或重新派工。
def test_start_receipt_keeps_sample_after_source_changes(boundary):
    _seed(boundary)
    call = _call_w8_replay_windows(boundary, 'start', _params(), 'stable-snapshot-key')
    accepted = replay.start(call)
    boundary.tables['soc_alerts'].clear()
    boundary.tables['soc_feedback'].clear()
    assert replay.start(call).data == accepted.data
    assert len(boundary.instructions) == 1
    assert len(boundary.jobs) == 1
    assert len(boundary.agents) == 1

# LLM: UTC 与不同偏移表达同一时刻，闭区间两端均应包含。
# 函数用途: 验证时间窗按实际时刻筛选且未知日期不参与采样。
def test_time_window_uses_offsets_and_closed_bounds(boundary):
    _seed(boundary)
    boundary.tables['soc_alerts']['alert-1']['created_at'] = '2026-11-02T18:00:00+08:00'
    boundary.tables['soc_alerts']['alert-2']['created_at'] = None
    params = {**_params(), 'replay_window': {'since': '2026-11-02T10:00:00Z', 'until': '2026-11-02T10:00:00Z'}}
    accepted = replay.start(_call_w8_replay_windows(boundary, 'start', params, 'offset-window-key'))
    row = boundary.tables['soc_replay_evaluations'][accepted.data['evaluation_id']]
    assert row['sample_alert_ids'] == ['alert-1']
    assert row['sample_size'] == 1

# LLM: 只有真实人工反馈可作真值，模型自己判定的告警仍属于未标注集合。
# 函数用途: 回归评测不会用 AI verdict 自我打分。
def test_unreviewed_model_verdict_is_not_ground_truth(boundary):
    _seed(boundary)
    boundary.tables['soc_feedback'].clear()
    for row in boundary.tables['soc_alerts'].values():
        row['ai_confidence'] = 0.95
    result = replay.capabilities(_call_w8_replay_windows(boundary, 'capabilities', {}))
    assert result['datasets'][0]['record_count'] == 3
    assert result['datasets'][0]['labelled_count'] == 0
    with pytest.raises(HostAPIError, match='STATE_CONFLICT'):
        replay.start(_call_w8_replay_windows(boundary, 'start', _params(), 'unreviewed-synthetic'))

# LLM: 校验钩子读取真实固定样本关系，不改原 job 状态；W0 负责记录 JOB_RESULT_INVALID。
# 函数用途: 验证同样本结果发布前钩子的拒绝边界。
def test_result_hook_checks_sample_ids_before_job_publication(boundary):
    evaluation_id, job = _complete(boundary)
    call = _call_w8_replay_windows(boundary, 'get', {'evaluation_id': evaluation_id})
    assert replay.validate_replay_result(call, job, job['result']) is True
    wrong = deepcopy(job['result'])
    wrong['decisions'][0]['alert_id'] = 'different-sample'
    with pytest.raises(ValueError):
        replay.validate_replay_result(call, job, wrong)
    assert job['state'] == 'completed'

# LLM: 仅真实 HTTP 返回才算原生边界证据；统一使用 harness 的准确用户和信封。
# 函数用途: 发起一次回放相关原生请求。
def _wire(server, action, params, key=None, revision=None, user='business-alice'):
    from agent_py_agent.tests.test_v13_w0_harness import v13_call

    return v13_call(server, action, params, user=user, idempotency_key=key, expected_revision=revision)

# LLM: 不把 HTTP 成功但业务失败的信封当完成。
# 函数用途: 断言标准完成信封并取出投影。
def _completed_w8_replay_windows(response):
    status, body = response
    assert status == 200, body
    assert body['state'] == 'completed', body
    assert body['error'] is None, body
    return body['data']

# LLM: 失败必须保留准确状态和结构化错误码，不能变成空成功。
# 函数用途: 断言原生错误信封。
def _rejected(response, status, code):
    actual, body = response
    assert actual == status, body
    assert body['state'] == 'failed', body
    assert body['data'] == {}, body
    assert body['error']['code'] == code, body

# LLM: W0 fixture 使用真正 Gateway 与隔离 HOME；空间只创建一次。
# 函数用途: 验证能力发现最小示例、无标签与参数闭合错误的 HTTP 往返。
def test_http_capabilities_empty_and_no_label_conflict(v13_native):
    from agent_py_agent.tests.test_v13_w0_harness import v13_space

    server, agent = v13_native
    space_id = v13_space(server)
    data = _completed_w8_replay_windows(_wire(server, 'evaluations.replay.capabilities', {}))
    _validate('capabilities', data)
    assert data['datasets'][0]['labelled_count'] == 0
    params = {**_params(), 'space_id': space_id}
    _rejected(_wire(server, 'evaluations.replay.start', params, uuid.uuid4().hex), 409, 'STATE_CONFLICT')
    _rejected(_wire(server, 'evaluations.replay.capabilities', {'unknown': True}), 400, 'INVALID_SCHEMA')
    _rejected(_wire(server, 'evaluations.replay.get', {'evaluation_id': 'unknown-evaluation'}),
              404, 'RESOURCE_NOT_FOUND')

# LLM: 模型可使用合成 echo，但候选创建、派工、存储、权限、游标和信封均经过真实 HTTP。
# 函数用途: 验证启动到详情/决策/导出状态冲突的原生关键链路。
def test_http_start_list_get_decisions_owner_idempotency(v13_native):
    from agent_py_agent.tests.test_v13_w0_harness import v13_space
    from agent_py_agent.tests.test_v13_w8_alerts import echo_profile

    server, agent = v13_native
    space_id = v13_space(server)
    configured = echo_profile(v13_native)
    profiles = _completed_w8_replay_windows(_wire(server, 'model_profiles.list', {'limit': 100}))['items']
    profile = next(row['profile_id'] for row in profiles if row['enabled'] and row['capability'] == 'agentic')
    assert profile == configured
    ingest = _completed_w8_replay_windows(_wire(server, 'soc.alerts.ingest', {'space_id': space_id, 'alerts': [
        {'external_ref': 'replay-http-synthetic', 'title': '合成回放事件', 'severity': 'medium',
         'occurred_at': '2026-11-02T10:00:00Z'}]}, uuid.uuid4().hex))
    alert_id = ingest['results'][0]['alert_id']
    alert = _completed_w8_replay_windows(_wire(server, 'soc.alerts.get', {'alert_id': alert_id}))['alert']
    _completed_w8_replay_windows(_wire(server, 'soc.alerts.update', {'alert_id': alert_id, 'verdict': 'true_positive'},
                     uuid.uuid4().hex, alert['revision']))
    params = {**_params(), 'space_id': space_id, 'candidate': {'model_profile_id': profile}}
    key = uuid.uuid4().hex
    status, accepted = _wire(server, 'evaluations.replay.start', params, key)
    assert status == 202, accepted
    assert accepted['state'] == 'accepted' and accepted['error'] is None, accepted
    _validate('start', accepted['data'], accepted=True)
    evaluation_id = accepted['data']['evaluation_id']
    replay_status, replay_body = _wire(server, 'evaluations.replay.start', params, key)
    assert replay_status == 202 and replay_body['data'] == accepted['data'], replay_body
    _rejected(_wire(server, 'evaluations.replay.start', {**params, 'sample_size': 1}, key),
              409, 'IDEMPOTENCY_CONFLICT')
    details = _completed_w8_replay_windows(_wire(server, 'evaluations.replay.get', {'evaluation_id': evaluation_id}))
    _validate('get', details)
    assert details['evaluation']['sample_size'] == 1
    assert details['evaluation']['agent_id'] is not None
    candidate = _completed_w8_replay_windows(_wire(server, 'background_agents.get',
                                 {'agent_id': details['evaluation']['agent_id']}))['agent']
    assert candidate['model_profile_id'] == profile
    _validate('list', _completed_w8_replay_windows(_wire(server, 'evaluations.replay.list', {'space_id': space_id})))
    _validate('decisions', _completed_w8_replay_windows(_wire(server, 'evaluations.replay.decisions', {'evaluation_id': evaluation_id})))
    _rejected(_wire(server, 'evaluations.replay.get', {'evaluation_id': evaluation_id}, user='business-bob'),
              404, 'RESOURCE_NOT_FOUND')
    if details['evaluation']['state'] != 'completed':
        _rejected(_wire(server, 'evaluations.replay.export', {'evaluation_id': evaluation_id, 'format': 'json'},
                        uuid.uuid4().hex), 409, 'STATE_CONFLICT')

# LLM: 唯一替身是本地模型输出；原任务执行、job 校验、指标、上传与HTTP下载必须全部真实往返。
# 函数用途: 验证同样本成功完成后 JSON/CSV 导出的完整原生链路。
@pytest.mark.parametrize('wrong_ids', [False, True])
def test_http_structured_candidate_to_metrics_and_download(v13_native, monkeypatch, wrong_ids):
    import http.client
    import time

    from agent_py_agent.agent.backends.base import EchoBackend, ModelResponse
    from agent_py_agent.tests.test_v13_w8_alerts import drive_job, echo_profile, invoke, seed_alert
    from agent_py_agent.tests.test_v13_w8_cases import completed, space

    server, agent = v13_native
    sid = space(v13_native)
    configured = echo_profile(v13_native)
    profiles = completed(_wire(server, 'model_profiles.list', {'limit': 100}))['items']
    profile = next(row['profile_id'] for row in profiles if row['enabled'] and row['capability'] == 'agentic')
    assert profile == configured
    row = seed_alert(v13_native, 'native-label-one', verdict='true_positive', confidence=0.9)
    completed(invoke(v13_native, 'feedback.submit_override', {'alert_id': row['alert_id'],
        'original_verdict': 'true_positive', 'override_verdict': 'benign', 'reason': '人工固定标签'}))
    captured = []

    # LLM: 合成模型不访问网络，不代替调度、存储、身份校验和HTTP入口。
    # 函数用途: 返回指定的规范决策或错误样本标识供完整链路复核。
    def generate(self, prompt, on_chunk=None, tools=None, tool_choice=None, messages=None,
                 on_thinking_delta=None, request_options=None):
        captured.append(prompt)
        return ModelResponse(backend='echo', text=json.dumps({'decisions': [{
            'alert_id': 'different-native-sample' if wrong_ids else row['alert_id'],
            'verdict': 'benign', 'rationale': '合成独立判断'}]}))

    monkeypatch.setattr(EchoBackend, 'generate', generate)
    params = {'space_id': sid, 'candidate': {'model_profile_id': profile},
              'replay_window': {'since': None, 'until': None}}
    status, accepted = _wire(server, 'evaluations.replay.start', params, uuid.uuid4().hex)
    assert status == 202, accepted
    evaluation_id = accepted['data']['evaluation_id']
    drive_job(v13_native, accepted['data']['job_id'])
    assert captured, '真实任务没有到达合成模型入口'
    for attempt in range(200):
        job = completed(_wire(server, 'async_jobs.get', {'job_id': accepted['data']['job_id']}))
        if job['state'] in {'completed', 'failed'}:
            break
        time.sleep(0.05)
    assert captured, '真实任务没有到达合成模型入口'
    assert all('expected_verdict' not in prompt and 'ai_confidence' not in prompt for prompt in captured)
    evaluation = completed(_wire(server, 'evaluations.replay.get', {'evaluation_id': evaluation_id}))['evaluation']
    if wrong_ids:
        assert job['state'] == 'failed' and job['error']['code'] == 'JOB_RESULT_INVALID', job
        assert evaluation['state'] == 'failed' and evaluation['metrics']['accuracy'] is None
        return
    assert job['state'] == evaluation['state'] == 'completed', job
    assert evaluation['metrics']['accuracy'] == 1 and evaluation['metrics']['baseline_accuracy'] == 0
    decisions = completed(_wire(server, 'evaluations.replay.decisions', {'evaluation_id': evaluation_id}))['items']
    assert len(decisions) == 1 and decisions[0]['match'] is True
    for fmt in ('json', 'csv'):
        result = completed(_wire(server, 'evaluations.replay.export', {'evaluation_id': evaluation_id,
            'format': fmt}, uuid.uuid4().hex))['export']
        metadata = completed(_wire(server, 'artifacts.get', {'artifact_id': result['artifact_id'],
            'version_ref': result['version_ref']}))
        assert metadata['available'] and metadata['artifact']['content_sha256'] == result['content_sha256']
        connection = http.client.HTTPConnection('127.0.0.1', server.server.server_address[1], timeout=20)
        try:
            connection.request('GET', '/client/business/artifacts/' + result['artifact_id']
                + '/content?version_ref=' + result['version_ref'], headers={
                    'X-Gateway-Token': server._local_client_credential, 'X-User-Id': 'business-alice',
                    'X-Channel': 'feishu'})
            response = connection.getresponse()
            content = response.read()
            assert response.status == 200 and len(content) == result['size_bytes']
        finally:
            connection.close()
        assert hashlib.sha256(content).hexdigest() == result['content_sha256']
        if fmt == 'json':
            assert json.loads(content)['decisions'][0]['alert_id'] == row['alert_id']
        else:
            assert list(csv.DictReader(content.decode().splitlines()))[0]['match'] == 'True'

# ============================================================================
# Source: agent_py_agent/tests/test_v13_w9_ai_assets.py
# #92 原生资产事实、无秘密投影和生产客户端真实治理往返。
# ============================================================================

# LLM: 仅提供调用 DTO；单元测试显式替换 I/O 叶，不把替身当成真实事务验收。
# 函数用途: 组装隔离 home 下的领域调用。
@pytest.fixture
def unit_call(tmp_path, monkeypatch):
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_resolver import (
        home_paths_with_owner,
        resolve_owner_home,
    )

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "home"))
    home = home_paths(tmp_path / "home")
    home = home_paths_with_owner(home, resolve_owner_home(home.root))
    host = SimpleNamespace(home_paths=home, config=AgentConfig(business_spaces_enabled=True), business_v13_providers={})
    return SimpleNamespace(action="ai_assets.list", params={}, owner_id=home.owner_id,
        subject_id="local", context=SimpleNamespace(host=host, check=lambda action, facts: None),
        owner_home=home.owner_home_dir, clock=lambda: 1793664000.0,
        idempotency_key="unit-ai-assets-0001", expected_revision=None)

# LLM: 生产客户端原样发送并解析真实信封；不替换HTTP运输，不手造返回 JSON。
# 函数用途: 通过真实 Gateway 调用一个扩展业务动作。
def wire_call(gateway, action, params=None, identity=None, key=None, revision=None):
    from agent_py_agent.agent.gateway_parts import business_api_v13
    from agent_py_agent.agent.plugin_host.business_action_specs import (
        business_schema_bundle,
        standalone_schema,
    )
    from agent_py_agent.agent.plugin_host.schemas import validate_object
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
    from agent_py_agent.cli.chat_client_context import post_gateway_json

    identity = identity or OwnerIdentity.provider_user("feishu", "business-alice")
    payload = {"action": action, "request_id": "w9-" + action, "params": params or {}}
    if key is not None:
        payload["idempotency_key"] = key
    if revision is not None:
        payload["expected_revision"] = revision
    status, body = post_gateway_json(thin_client(gateway, identity), "/client/business", payload, timeout=10.0)
    assert "state" in body, (status, body)
    schema = standalone_schema(business_schema_bundle()["response_envelope_schema"])
    result_schema = business_api_v13.v13_action_spec(action).result_schema
    schema["$defs"].update(result_schema.pop("$defs", {}))
    schema["properties"]["data"] = {"anyOf": [{"$ref": "#/$defs/EmptyData"}, result_schema]}
    envelope = validate_object(body, schema)
    if body["error"] is None:
        business_api_v13.v13_action_spec(action).validate_result(body["data"])
    assert (envelope["error"] is None and status == 200 and envelope["state"] == "completed") or (
        envelope["error"] is not None and status >= 400 and envelope["state"] == "failed"), (status, envelope)
    return {**envelope, "http_status": status, "ok": envelope["error"] is None}

# LLM: 只按可信 owner 解析目录，用 W0 官方种子函数写原集合。
# 函数用途: 将真实测试记录送入指定 owner 的原 v13 存储。
def seed_rows(gateway, collection, rows, identity=None):
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
    from agent_py_agent.tests import test_v13_w0_harness

    identity = identity or OwnerIdentity.provider_user("feishu", "business-alice")
    owner = resolve_owner_home(gateway.agent.home_paths.root, identity)
    test_v13_w0_harness.v13_seed(owner.home_dir, collection, [{**row, "owner_id": owner.owner_id} for row in rows])
    return owner

# LLM: 模型写入原目录保存入口，测试秘密仅为人工合成字符串。
# 函数用途: 物化一个真实模型服务商和档案。
def seed_model(home, endpoint="https://models.example.test/v1", profile_id="11111111-1111-4111-8111-111111111111"):
    from agent_py_agent.agent.plugin_host.model_settings_v2_handlers import ModelSettingsV2Handlers

    result = ModelSettingsV2Handlers(home).save_provider({"provider_id": "example", "display_name": "Example",
        "api_base": endpoint, "api_key": "TEST_ONLY_KEY_DO_NOT_USE", "custom_headers": {"x-test": "TEST_HEADER_SECRET"},
        "models": [{"profile_id": profile_id, "model_name": "fixture-model", "model_backend": "openai_compatible",
            "model_context_window_tokens": 32768}]})
    assert result["providers"][0]["models"][0]["profile_id"] == profile_id
    return "ai-" + profile_id

# LLM: 用原安装接口保存静态测试包，不启用、不启动插件。
# 函数用途: 在同一临时数据根建立真实插件安装事实。
def seed_plugin(tmp_path):
    from agent_py_agent.tests.test_plugin_management import manager
    from agent_py_agent.tests.test_plugin_package import _bundle

    service, _source = manager(tmp_path)
    service.install_package_bytes(_bundle(), operation_id="w9-test-install-package")
    return "ai-sample-peek"

# LLM: 用原 ConversationStore/ModelUsageStore 保存物理身份，不向产品塞造假的外部服务库存。
# 函数用途: 建立本 owner 原用量账的一个真实事件。
def seed_usage(call):
    from agent_py_agent.agent.conversation.store import ConversationStore

    store = ConversationStore(call.owner_home / "conversations")
    thread = store.threads.create(canonical_user_id="local", owner_id=call.owner_id,
        owner_home=str(call.owner_home), title="资产用量测试")
    store.model_usage.append_once({"thread_id": thread.thread_id, "event_id": "usage-asset-event",
        "request_id": "usage-request", "run_id": "run-assets", "task_id": "task-assets", "source": "test",
        "model_calls": {"schema": "model_call_summary.v1", "physical_model_attempt_count": 1, "backends": ["https://user:TEST_URL_SECRET@usage.example.test/v1?key=TEST_QUERY_SECRET"],
            "backend_attempt_counts": {"https://user:TEST_URL_SECRET@usage.example.test/v1?key=TEST_QUERY_SECRET": 1}},
        "now": call.clock()})
    call.context.host.conversation_store = store
    return store

# LLM: 读取与变更前后核原事实源；发现过程不应创建或改变任何目录内容。
# 函数用途: 验证四类真实来源、风险和敏感字段白名单。
def test_discovery_projects_models_mcp_plugins_usage_with_risk(unit_call, tmp_path):
    host = unit_call.context.host
    external = seed_model(host.home_paths)
    host.config.mcp_servers = {"asset-01": {"command": "fixture-command", "args": ["TEST_ARG_SECRET"],
        "env": {"KEY": "TEST_ENV_SECRET"}}, "remote": {"url": "https://user:TEST_MCP_SECRET@mcp.example.test/rpc?token=TEST_QUERY_SECRET"}}
    plugin = seed_plugin(tmp_path)
    seed_usage(unit_call)
    before = {str(p): p.read_bytes() for p in host.home_paths.root.rglob("*") if p.is_file()}
    rows, complete = assets._discover(unit_call)
    after = {str(p): p.read_bytes() for p in host.home_paths.root.rglob("*") if p.is_file()}
    assert before == after and complete
    by_id = {row["asset_id"]: assets._view(row) for row in rows}
    assert {row["kind"] for row in by_id.values()} == {"model_endpoint", "mcp_server", "agent_plugin", "external_service"}
    assert by_id[external]["risk"]["level"] == "high"
    assert by_id["ai-asset-01"]["risk"]["level"] == "medium"
    assert by_id[plugin]["risk"]["level"] == "unknown"
    assert by_id["ai-usage.example.test"]["endpoint_host"] == "usage.example.test"
    output = json.dumps(by_id)
    for secret in ("TEST_ONLY_KEY_DO_NOT_USE", "TEST_HEADER_SECRET", "TEST_ARG_SECRET", "TEST_ENV_SECRET",
        "TEST_MCP_SECRET", "TEST_QUERY_SECRET", "TEST_URL_SECRET"):
        assert secret not in output
    local_rows = assets._model_rows(unit_call)
    local_rows[0]["endpoint_host"] = "127.0.0.1"
    assert assets._view({**local_rows[0], "asset_id": external})["risk"]["level"] == "low"

# LLM: 冷 owner 的 host.config 可能来自管理员；不把主用户 MCP 当成本用户资产。
# 函数用途: 验证配置 owner 不匹配时不泄漏主宿主声明。
def test_mcp_config_cross_owner_is_not_projected(unit_call):
    unit_call.owner_id = "providers/feishu/users/business-alice"
    unit_call.context.host.config.mcp_servers = {"private-main": {"command": "private-command"}}
    assert assets._mcp_rows(unit_call) == ([], False)

# LLM: 同名不同类型加后缀；后缀再撞原 ID 时所有结果仍唯一且与输入顺序无关。
# 函数用途: 验证原生 ID 碰撞处理稳定，不靠覆盖丢行。
def test_stable_asset_id_collision_and_second_collision():
    rows = [{"native_id": "shared", "kind": "mcp_server"},
        {"native_id": "shared", "kind": "agent_plugin"},
        {"native_id": "shared-mcp_server", "kind": "model_endpoint"}]
    first = assets._asset_ids(rows)
    assert first == assets._asset_ids(list(reversed(rows)))
    assert len({row["asset_id"] for row in first}) == len(rows)
    assert any(row["asset_id"] == "ai-shared-agent_plugin" for row in first)

# LLM: 风险不从名字推导，也不把 sanction 后的外部服务凭空视为低风险。
# 函数用途: 验证风险规则仅由结构化端点和能力触发。
@pytest.mark.parametrize("kind,host,capability,sanctioned,level", [
    ("model_endpoint", "::1", False, False, "low"),
    ("model_endpoint", "192.168.1.4", False, False, "high"),
    ("model_endpoint", "models.example.test", False, True, "unknown"),
    ("mcp_server", None, True, True, "medium"),
    ("agent_plugin", None, False, False, "unknown"),
])
def test_risk_is_structured(kind, host, capability, sanctioned, level):
    row = {"kind": kind, "endpoint_host": host, "network_or_exec": capability, "name": "network exec trusted"}
    assert assets._risk(row, sanctioned)["level"] == level

# LLM: 产品直接调用也必须经过 W0 原 native admin，不用 RBAC roles 或布尔声明替代。
# 函数用途: 验证领域入口不会绕过管理员权威。
def test_sanction_calls_native_admin_before_discovery(unit_call, monkeypatch):
    import sys

    called = []

    # LLM: 测试只替换管理员检查叶，检查拒绝发生在读取目标之前。
    # 函数用途: 记录拒绝并返回原生错误。
    def deny(call):
        called.append(call.owner_id)
        raise HostAPIError("BUSINESS_ACCESS_DENIED", 403)

    module_name = "agent_py_agent.agent.gateway_parts.business_api_v13"
    if module_name in sys.modules:
        monkeypatch.setattr(sys.modules[module_name], "v13_require_admin", deny)
    else:
        monkeypatch.setitem(sys.modules, module_name, SimpleNamespace(v13_require_admin=deny))
    monkeypatch.setattr(assets, "_discover", lambda call: pytest.fail("拒绝后不应发现资产"))
    with pytest.raises(HostAPIError) as error:
        assets.sanction(unit_call)
    assert error.value.code == "BUSINESS_ACCESS_DENIED" and called == [unit_call.owner_id]

# LLM: main 走真实 native admin，普通用户即使有授权动作也不能通过该门。
# 函数用途: 验证示例 sanction、历史、幂等、跨 owner 与不存在目标。
def test_sanction_example_params_admin_only_wire(gateway):
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    main = OwnerIdentity.local_main()
    gateway.agent.config.mcp_servers = {"asset-01": {"command": "fixture-command"}}
    params = {"asset_id": "ai-asset-01", "sanctioned": True}
    first = wire_call(gateway, "ai_assets.sanction", params, main, "w9-sanction-example-0001")
    assert (first["http_status"], first["state"], first["error"]) == (200, "completed", None), first
    assert first["data"]["asset"]["sanction_state"] == "sanctioned"
    replay = wire_call(gateway, "ai_assets.sanction", params, main, "w9-sanction-example-0001")
    assert replay["data"] == first["data"]
    conflict = wire_call(gateway, "ai_assets.sanction", {**params, "sanctioned": False}, main, "w9-sanction-example-0001")
    assert (conflict["http_status"], conflict["error"]["code"]) == (409, "IDEMPOTENCY_CONFLICT")
    restricted = wire_call(gateway, "ai_assets.sanction", {**params, "sanctioned": False}, main, "w9-sanction-example-0002")
    assert restricted["data"]["asset"]["sanction_state"] == "restricted"
    denied = wire_call(gateway, "ai_assets.sanction", params, key="w9-sanction-nonadmin-0001")
    assert (denied["http_status"], denied["error"]["code"]) == (403, "BUSINESS_ACCESS_DENIED"), denied
    absent = wire_call(gateway, "ai_assets.sanction", {"asset_id": "ai-absent", "sanctioned": True}, main, "w9-sanction-absent-0001")
    assert (absent["http_status"], absent["error"]["code"]) == (404, "RESOURCE_NOT_FOUND")
    from agent_py_agent.agent.business_spaces.v13_store import V13Store

    store = V13Store.read_only(gateway.agent.home_paths.owner_home_dir)
    decision = store.get("ai_asset_decisions", "ai-asset-01")
    assert decision["revision"] == 2 and len(decision["history"]) == 2

# LLM: 分页使用真实来源快照，决定变化使旧游标失效；过滤不会悄悄返回另一 owner 决定。
# 函数用途: 验证列表过滤、HMAC 游标、owner 隔离和标准信封。
def test_envelopes_and_pages_wire(gateway):
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    main = OwnerIdentity.local_main()
    gateway.agent.config.mcp_servers = {"asset-01": {"command": "one"}, "asset-02": {"command": "two"}}
    first = wire_call(gateway, "ai_assets.list", {"limit": 1}, main)
    assert first["ok"] and first["http_status"] == 200 and first["error"] is None, first
    cursor = first["data"]["next_cursor"]
    assert cursor
    second = wire_call(gateway, "ai_assets.list", {"limit": 1, "cursor": cursor}, main)
    assert second["ok"] and second["data"]["items"] != first["data"]["items"]
    for params, identity in (({"cursor": cursor + "x"}, main), ({"cursor": cursor, "kind": "mcp_server"}, main),
        ({"cursor": cursor}, OwnerIdentity.provider_user("feishu", "business-bob"))):
        invalid = wire_call(gateway, "ai_assets.list", params, identity)
        assert (invalid["http_status"], invalid["error"]["code"]) == (410, "CURSOR_EXPIRED"), invalid
    wire_call(gateway, "ai_assets.sanction", {"asset_id": "ai-asset-01", "sanctioned": True}, main, "w9-page-sanction-key")
    stale = wire_call(gateway, "ai_assets.list", {"cursor": cursor}, main)
    assert stale["error"]["code"] == "CURSOR_EXPIRED"
    filtered = wire_call(gateway, "ai_assets.list", {"kind": "mcp_server", "sanction_state": "sanctioned"}, main)
    assert [row["asset_id"] for row in filtered["data"]["items"]] == ["ai-asset-01"]

# LLM: 已存决定不能单独制造资产存在性，也不能跨 owner 投影管理员审批。
# 函数用途: 验证另一 owner 的决定对本 owner 无效。
def test_cross_owner_decisions_wire(gateway):
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    gateway.agent.config.mcp_servers = {"asset-01": {"command": "fixture-command"}}
    seed_rows(gateway, "ai_asset_decisions", [{"asset_id": "ai-asset-01", "sanction_state": "sanctioned",
        "decided_by": "other-admin", "reason": "OTHER_OWNER_PRIVATE_REASON", "history": [], "revision": 1,
        "created_at": None, "updated_at": None}])
    result = wire_call(gateway, "ai_assets.list", identity=OwnerIdentity.local_main())
    assert result["ok"], result
    row = next(row for row in result["data"]["items"] if row["asset_id"] == "ai-asset-01")
    assert row["sanction_state"] == "unreviewed" and "OTHER_OWNER_PRIVATE_REASON" not in json.dumps(result)

# ============================================================================
# Source: agent_py_agent/tests/test_v13_w9_datasources.py
# #72 的真实 HTTP、原生 Vault/SQLite、CAS 与来源健康回归。
# ============================================================================

# LLM: 复用真实Gateway fixture，不替换认证、输入账本或业务执行路径。
# 函数用途: 为数据源测试显式绑定同一套原生HTTP环境。
@pytest.fixture(name="datasource_http")
def datasource_gateway(request):
    return request.getfixturevalue("hunting_http")

# LLM: 规格和数据库均来自当前测试原生数据根，不访问开发机配置。
# 函数用途: 创建可被真实业务查询的数据源和两张测试表。
def _source(native, identifier="source-one", *, credentials=None):
    params = {"datasource_id": identifier, "name": "测试数据源", "kind": "sqlite",
        "allowed_tables": ["events"], "poll_interval_sec": 60}
    if credentials is not None:
        params["credentials"] = credentials
    data = _completed_w9_datasources(_call_w9_datasources(native, "datasources.create", params, key=uuid4().hex))["datasource"]
    store = DatasourceStore(native[1].home_paths.owner_home_dir)
    spec = store.get_datasource_spec(identifier)
    with sqlite3.connect(spec.database_path) as conn:
        conn.executescript("CREATE TABLE events(id INTEGER PRIMARY KEY, value TEXT);"
            "INSERT INTO events VALUES(1,'one'),(2,'two'); CREATE TABLE hidden(secret TEXT);")
    return data, store, spec

# LLM: 列表必须反映原生规格，不额外生成影子数据源；过滤及签名游标走HTTP。
# 函数用途: 覆盖 #72-1 原生发现与分页。
def test_list_projects_native_store_without_secrets(datasource_http):
    first, store, spec = _source(datasource_http, credentials={"access_token": "synthetic-datasource-secret"})
    _source(datasource_http, "source-two")
    assert first["has_secret"] is True and first["credential_keys"] == ["access_token"]
    listed = _completed_w9_datasources(_call_w9_datasources(datasource_http, "datasources.list", {"limit": 1}))
    assert listed["next_cursor"] and len(listed["items"]) == 1
    second = _completed_w9_datasources(_call_w9_datasources(datasource_http, "datasources.list", {"limit": 1, "cursor": listed["next_cursor"]}))
    assert listed["items"][0]["datasource_id"] != second["items"][0]["datasource_id"]
    _error(_call_w9_datasources(datasource_http, "datasources.list", {"cursor": listed["next_cursor"], "kind": "postgres"}), 410, "CURSOR_EXPIRED")
    assert "synthetic-datasource-secret" not in json.dumps(listed)
    assert store.resolve_secret(spec.credential_ref)["access_token"] == "synthetic-datasource-secret"
    empty = _completed_w9_datasources(_call_w9_datasources(datasource_http, "datasources.list", {"kind": "postgres"}))
    assert empty["items"] == []
    _error(_call_w9_datasources(datasource_http, "datasources.test", {"datasource_id": "source-one"}, key=uuid4().hex, other_owner=True), 404, "RESOURCE_NOT_FOUND")

# LLM: 同键重放必须保持规格与凭据引用，不能再次改 native revision。
# 函数用途: 覆盖 #72-2 创建幂等、冲突及原 Vault 保密。
def test_create_uses_native_vault_and_idempotency(datasource_http):
    p = {"datasource_id": "created-once", "name": "一次创建", "kind": "https",
        "credentials": {"access_token": "synthetic-access-token"}}
    key = uuid4().hex
    first = _completed_w9_datasources(_call_w9_datasources(datasource_http, "datasources.create", p, key=key))
    assert _completed_w9_datasources(_call_w9_datasources(datasource_http, "datasources.create", p, key=key)) == first
    _error(_call_w9_datasources(datasource_http, "datasources.create", {**p, "name": "改名"}, key=key), 409, "IDEMPOTENCY_CONFLICT")
    _error(_call_w9_datasources(datasource_http, "datasources.create", p, key=uuid4().hex), 409, "STATE_CONFLICT")
    assert "synthetic-access-token" not in json.dumps(first)
    home = datasource_http[1].home_paths.owner_home_dir
    spec = DatasourceStore(home).get_datasource_spec("created-once")
    assert spec.revision == 1
    assert DatasourceStore(home).resolve_secret(spec.credential_ref)["access_token"] == "synthetic-access-token"
    for path in (home / "data" / "business_v13").glob("*.json"):
        assert "synthetic-access-token" not in path.read_text()

# LLM: 结果投影无法表示的凭据键必须在 native 副作用之前拒绝。
# 函数用途: 回归独审发现的凭据键数量和长度边界。
@pytest.mark.parametrize("credentials", [{"k" + str(i): "synthetic-value" for i in range(51)}, {"k" * 129: "synthetic-value"}])
def test_credential_keys_admitted_before_native_commit(datasource_http, credentials):
    p = {"datasource_id": "invalid-credential-keys", "name": "边界", "kind": "https", "credentials": credentials}
    _error(_call_w9_datasources(datasource_http, "datasources.create", p, key=uuid4().hex), 400, "INVALID_SCHEMA")
    with pytest.raises(KeyError):
        DatasourceStore(datasource_http[1].home_paths.owner_home_dir).get_datasource_spec(p["datasource_id"])

# LLM: 原生版本为 CAS 权威，更新不能因健康探测次数误判规格版本。
# 函数用途: 覆盖 #72-3 更新 CAS、幂等和真实字段合并。
def test_update_cas_and_idempotency(datasource_http):
    first, store, spec = _source(datasource_http)
    p, key = {"datasource_id": spec.datasource_id, "name": "更新名称", "poll_interval_sec": 120}, uuid4().hex
    updated = _completed_w9_datasources(_call_w9_datasources(datasource_http, "datasources.update", p, key=key, revision=first["revision"]))
    assert updated["datasource"]["revision"] == 2 and updated["datasource"]["poll_interval_sec"] == 120
    assert store.get_datasource_spec(spec.datasource_id).title == "更新名称"
    assert _completed_w9_datasources(_call_w9_datasources(datasource_http, "datasources.update", p, key=key, revision=1)) == updated
    _error(_call_w9_datasources(datasource_http, "datasources.update", p, key=uuid4().hex, revision=1), 409, "REVISION_CONFLICT")

# LLM: 原文件与 Vault 保留，默认列表及所有取数路径必须尊重墓碑。
# 函数用途: 覆盖 #72-4 软删除、CAS 和重放。
def test_delete_creates_tombstone_and_hides_from_list(datasource_http):
    _, store, spec = _source(datasource_http)
    p, key = {"datasource_id": spec.datasource_id}, uuid4().hex
    _error(_call_w9_datasources(datasource_http, "datasources.delete", p, key=uuid4().hex, revision=99), 409, "REVISION_CONFLICT")
    result = _completed_w9_datasources(_call_w9_datasources(datasource_http, "datasources.delete", p, key=key, revision=1))
    assert result == {**p, "deleted": True, "mode": "tombstone"}
    assert _completed_w9_datasources(_call_w9_datasources(datasource_http, "datasources.delete", p, key=key, revision=1)) == result
    assert _completed_w9_datasources(_call_w9_datasources(datasource_http, "datasources.list"))["items"] == []
    assert _completed_w9_datasources(_call_w9_datasources(datasource_http, "datasources.list", {"include_deleted": True}))["items"][0]["deleted"]
    assert store.get_datasource_spec(spec.datasource_id).datasource_id == spec.datasource_id
    _error(_call_w9_datasources(datasource_http, "datasources.test", p, key=uuid4().hex), 404, "RESOURCE_NOT_FOUND")

# LLM: 原认证有效绑定必须阻止删除，禁用后可删除，不删除原 token 账。
# 函数用途: 验证数据源引用约束。
def test_delete_rejects_bound_token(datasource_http):
    space = _space(datasource_http)
    _, _, spec = _source(datasource_http)
    token = _completed_w9_datasources(_call_w9_datasources(datasource_http, "inbox.tokens.mint", {"space_id": space,
        "datasource_id": spec.datasource_id, "label": "引用"}, key=uuid4().hex))["token"]
    p = {"datasource_id": spec.datasource_id}
    _error(_call_w9_datasources(datasource_http, "datasources.delete", p, key=uuid4().hex, revision=1), 409, "STATE_CONFLICT")
    _completed_w9_datasources(_call_w9_datasources(datasource_http, "inbox.tokens.revoke", {"fingerprint": token["fingerprint"]}, key=uuid4().hex))
    assert _completed_w9_datasources(_call_w9_datasources(datasource_http, "datasources.delete", p, key=uuid4().hex, revision=1))["deleted"]

# LLM: 连接失败是观察结果，不伪装成接口失败或连通成功。
# 函数用途: 覆盖 #72-5 原生探测和可观察失败。
def test_test_failure_is_200_ok_false_and_persisted(datasource_http):
    _, store, spec = _source(datasource_http)
    spec_path = Path(spec.database_path)
    spec_path.rename(spec_path.with_suffix(".saved"))
    data = _completed_w9_datasources(_call_w9_datasources(datasource_http, "datasources.test", {"datasource_id": spec.datasource_id}, key=uuid4().hex))
    assert data["ok"] is False and data["error_code"] == "DATASOURCE_CONNECT_FAILED"
    health = _completed_w9_datasources(_call_w9_datasources(datasource_http, "datasources.fleet_health", {}))
    assert health["items"][0]["status"] == "down" and health["items"][0]["last_test"]["ok"] is False
    assert store.get_datasource_spec(spec.datasource_id).revision == 1

# LLM: SQL 必须由真实只读连接执行，文件和隐藏表都不能受影响。
# 函数用途: 覆盖 #72-7 SELECT/CTE、行数、写入及越表拒绝。
def test_query_read_only_rejects_writes_and_disallowed_tables(datasource_http):
    _, _, spec = _source(datasource_http)
    p = {"statement": "SELECT * FROM events ORDER BY id", "datasource_ids": [spec.datasource_id], "limit": 1}
    data = _completed_w9_datasources(_call_w9_datasources(datasource_http, "datasources.query", p))
    assert data["results"][0]["rows"] == [[1, "one"]] and data["results"][0]["truncated"]
    for sql in ("DELETE FROM events", "SELECT * FROM hidden", "SELECT 1; DROP TABLE events", "ATTACH ':memory:' AS second", "SELECT load_extension('x')"):
        _error(_call_w9_datasources(datasource_http, "datasources.query", {**p, "statement": sql}), 400, "QUERY_REJECTED")
    cte = _completed_w9_datasources(_call_w9_datasources(datasource_http, "datasources.query", {**p,
        "statement": "WITH e AS (SELECT * FROM events) SELECT count(*) FROM e"}))
    assert cte["results"][0]["rows"] == [[2]]
    with sqlite3.connect(spec.database_path) as conn:
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 2

# LLM: SQLite DSN 映射必须实际改变路径，并继续走原 nofollow/作用域限制。
# 函数用途: 验证 DSN 与真实 SQLite 查询目标一致。
def test_sqlite_dsn_maps_to_native_path(datasource_http):
    home = datasource_http[1].home_paths.owner_home_dir
    path = home / "data" / "selected.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE selected(value INTEGER)")
        conn.execute("INSERT INTO selected VALUES(7)")
    p = {"datasource_id": "dsn-selected", "name": "指定库", "kind": "sqlite",
        "dsn_template": "sqlite:///" + str(path), "allowed_tables": ["selected"]}
    _completed_w9_datasources(_call_w9_datasources(datasource_http, "datasources.create", p, key=uuid4().hex))
    spec = DatasourceStore(home).get_datasource_spec(p["datasource_id"])
    assert Path(spec.database_path) == path
    result = _completed_w9_datasources(_call_w9_datasources(datasource_http, "datasources.query", {
        "datasource_ids": [p["datasource_id"]], "statement": "SELECT value FROM selected"}))
    assert result["results"][0]["rows"] == [[7]]

# LLM: 标准信封的字段、错误和发现状态都来自真实网关。
# 函数用途: 覆盖 #72-8..9 HTTP 信封及未来驱动的明确结果。
def test_envelopes_w9_datasources(datasource_http):
    params = {"datasource_id": "future-driver", "name": "未来驱动", "kind": "future"}
    assert _completed_w9_datasources(_call_w9_datasources(datasource_http, "datasources.create", params, key=uuid4().hex))["datasource"]["queryable"] is False
    data = _completed_w9_datasources(_call_w9_datasources(datasource_http, "datasources.query", {"statement": "SELECT 1", "datasource_ids": [params["datasource_id"]]}))
    assert data["coverage"] == "partial" and data["results"][0]["error_code"] == "DATASOURCE_DRIVER_UNSUPPORTED"
    _error(_call_w9_datasources(datasource_http, "datasources.list", {"owner_id": "other"}), 400, "INVALID_SCHEMA")

# LLM: 健康时间只能来自原生受理回执，业务时钟只用于计算经过时间，不伪造last_event。
# 函数用途: 覆盖 #72-6 真实push从unknown到healthy再到lagging。
def test_fleet_health_uses_real_bound_push_and_clock(datasource_http):
    from agent_py_agent.tests.test_v13_w9_canary_markers import _push
    clock = [__import__("time").time()]
    datasource_http[1].business_v13_clock = lambda: clock[0]
    space = _space(datasource_http)
    _, _, spec = _source(datasource_http)
    params = {"datasource_id": spec.datasource_id}
    initial = _completed_w9_datasources(_call_w9_datasources(datasource_http, "datasources.fleet_health", params))["items"][0]
    assert initial["status"] == "unknown" and initial["last_event_at"] is None
    token = _completed_w9_datasources(_call_w9_datasources(datasource_http, "inbox.tokens.mint", {"space_id": space,
        "datasource_id": spec.datasource_id, "label": "健康来源"}, key=uuid4().hex))
    receipt = _push(datasource_http, token, "原生数据源事件")
    clock[0] = receipt["queued_at"] + 1
    healthy = _completed_w9_datasources(_call_w9_datasources(datasource_http, "datasources.fleet_health", params))["items"][0]
    assert healthy["status"] == "healthy" and healthy["lag_sec"] == 1
    clock[0] += 121
    lagging = _completed_w9_datasources(_call_w9_datasources(datasource_http, "datasources.fleet_health", params))["items"][0]
    assert lagging["status"] == "lagging" and lagging["lag_sec"] == 122
