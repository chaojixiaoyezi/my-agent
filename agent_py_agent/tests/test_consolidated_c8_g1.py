"""Consolidated tests for shard c8 (test_consolidated_c8_g1.py).

Merged sources: test_v13_w0_audit.py, test_v13_w0_store.py, test_v13_w10_access_review.py, test_v13_w11_autonomy.py, test_v13_w11_conversations.py, test_v13_w11_onboarding.py, test_v13_w11_packages.py.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.auth.manager import AuthManager
from agent_py_agent.agent.auth.middleware import AuthMiddleware
from agent_py_agent.agent.business_spaces import (
    v13_audit as module_w0_audit,
)
from agent_py_agent.agent.business_spaces import (
    v13_store as module_w0_store,
)
from agent_py_agent.agent.business_spaces import (
    v13_w10_access_review as review,
)
from agent_py_agent.agent.business_spaces import (
    v13_w10_identity as identity,
)
from agent_py_agent.agent.business_spaces.v13_audit import audit_records
from agent_py_agent.agent.business_spaces.v13_store import V13Store, v13_store_for
from agent_py_agent.agent.capability.package_build import build_capability_pack
from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.agent.gateway_parts.business_api import (
    bind_business_context,
    local_business_context,
)
from agent_py_agent.agent.gateway_parts.business_api_v13 import (
    V13Call,
    v13_action_spec,
    v13_action_specs,
)
from agent_py_agent.agent.gateway_parts.owner_conversation_store import (
    owner_conversation_store_from_config,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.request_binding import MODEL_OBSERVATION_KEY
from agent_py_agent.agent.plugin_host.business_action_specs import business_action_specs
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.plugin_management import PluginManagement, plugin_management_context
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.session.manager import SessionManager
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home, home_paths
from agent_py_agent.agent.user_space.local_user_lifecycle import LocalUserLifecycle
from agent_py_agent.agent.user_space.local_user_store import LocalUserStore
from agent_py_agent.agent.user_space.owner_access import is_complete_local_admin_owner
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    resolve_owner_home,
)
from agent_py_agent.cli.chat_client_context import owner_identity_from_config
from agent_py_agent.tests.test_mega_business_api_actions import native_request
from agent_py_agent.tests.test_v13_w0_harness import v13_call, v13_space

pytest_plugins = (
    'agent_py_agent.tests.test_business_v12_f9_e2e',
    'agent_py_agent.tests.test_v13_w0_harness',
)

# ============================================================================
# Source: agent_py_agent/tests/test_v13_w0_audit.py
# 真实请求文件、会话归属和 V13 写入的共享审计测试。
# ============================================================================

# LLM: 仅在 tmp_path 建立真实会话、请求文件和事务审计；合成凭据不连接真实宿主。
# 函数用途: 构造只使用任务临时 home 的真实原生审计来源。
@pytest.fixture
def audit_home(tmp_path):
    paths_home = ensure_my_agent_home(tmp_path / 'home')
    base = home_paths_with_owner(paths_home, resolve_owner_home(paths_home.root))
    config = SimpleNamespace(gateway_workspace=str(tmp_path / 'queue'),
                             conversation_workspace=str(tmp_path / 'conversations'))
    conversations = owner_conversation_store_from_config(config, base, initialize=True)
    thread = conversations.threads.create(owner_id=base.owner_id)
    host = SimpleNamespace(home_paths=base, config=config, conversation_store=conversations)
    paths = gateway_paths_from_root(tmp_path / 'queue')
    paths.done.mkdir(parents=True)
    payload = {'id': 'request-one', 'conversation_claim': {'thread_id': thread.thread_id},
               'owner_id': base.owner_id, 'status': 'completed',
               MODEL_OBSERVATION_KEY: {'status': 'selected', 'reason': 'synthetic'},
               'prompt': 'private synthetic content', 'metadata': {'channel': 'chat'}}
    request = paths.done / 'request-one.json'
    request.write_text(json.dumps(payload))
    os.utime(request, (1700000020, 1700000020))
    # LLM: 写入指定临时 owner 的真实事务，以固定主体和时钟生成可追溯审计。
    # 函数用途: 使用正式事务写审计，而非手工拼造 v13 审计文件。
    def seed(home, subject, at, key):
        return V13Store(home, subject_id=subject, action='async.create', clock=lambda: at).transaction(
            'async_jobs', lambda rows: (rows.update({key: {'job_id': key}}) or {'job_id': key}),
            idempotency_key=key, params_digest=key)
    seed(base.owner_home_dir, 'main-actor', 1700000030, 'main-job')
    alice = home_paths_with_owner(base, resolve_owner_home(base.root, OwnerIdentity.local_user('alice')))
    alice.owner_home_dir.mkdir(parents=True)
    seed(alice.owner_home_dir, 'alice-actor', 1700000040, 'alice-job')
    foreign = paths.done / 'request-alice.json'
    foreign.write_text(json.dumps({'id': 'request-alice', 'owner_id': alice.owner_id, 'status': 'failed'}))
    os.utime(foreign, (1700000010, 1700000010))
    middleware = AuthMiddleware(AuthManager(), auth_token='synthetic-gateway-credential')
    handler = SimpleNamespace(_auth_middleware=middleware,
        headers={'X-Gateway-Token': 'synthetic-gateway-credential'}, client_address=('127.0.0.1', 1))
    return SimpleNamespace(base=base, alice=alice, host=host, handler=handler, paths=paths, request=request)

# LLM: 读取夹具中的真实审计源，跨 owner 记录和私有请求内容不得进入公开 DTO。
# 函数用途: 合并的三个来源按新到旧排序、owner 隔离、仅返回冻结 DTO。
def test_audit_merges_native_sources_and_v13_newest_first(audit_home):
    data = audit_home
    page = audit_records(data.base.owner_home_dir, data.base.owner_id, host=data.host)
    assert page['coverage'] == 'complete', page
    assert page['unavailable'] == []
    assert [row['source'] for row in page['records']] == ['v13', 'request', 'decision']
    assert [row['at'] for row in page['records']] == sorted((row['at'] for row in page['records']), reverse=True)
    assert all(set(row) == {'record_id', 'at', 'actor', 'action', 'outcome', 'source'} for row in page['records'])
    assert 'alice-actor' not in json.dumps(page)
    assert 'private synthetic content' not in json.dumps(page)
    selected = audit_records(data.base.owner_home_dir, data.base.owner_id, host=data.host,
                             actor='main-actor', since='2023-11-14T22:13:45Z', until='2023-11-14T22:14:00Z')
    assert len(selected['records']) == 1 and selected['records'][0]['source'] == 'v13'

# LLM: host 查询只能使用仍有效的原生管理员身份，改坏合成凭据后须立即拒绝跨 owner 读取。
# 函数用途: 管理员重验来自原生中间件，不接受 actor、对象属性或撤销后的旧身份。
def test_host_scope_requires_native_admin_and_spans_owners(audit_home):
    data = audit_home
    with pytest.raises(BusinessError, match='CAPABILITY_DENIED'):
        audit_records(data.base.owner_home_dir, data.base.owner_id, scope='host', actor='admin', host=data.host)
    with pytest.raises(BusinessError, match='CAPABILITY_DENIED'):
        audit_records(data.base.owner_home_dir, data.base.owner_id, scope='host',
                      admin_context=SimpleNamespace(is_admin=True), host=data.host)
    page = audit_records(data.base.owner_home_dir, data.base.owner_id, scope='host',
                         admin_context=data.handler, host=data.host)
    assert page['coverage'] == 'complete', page
    assert page['records'][0]['actor'] == 'alice-actor'
    assert len(page['records']) == 5
    data.handler.headers = {'X-Gateway-Token': 'wrong'}
    with pytest.raises(BusinessError, match='CAPABILITY_DENIED'):
        audit_records(data.base.owner_home_dir, data.base.owner_id, scope='host',
                      admin_context=data.handler, host=data.host)

# LLM: 原游标仅对同一 owner、筛选和来源修订有效；临时源新增事务后必须拒绝旧游标。
# 函数用途: 同一页可由下一次函数调用延续，筛选和源修订变更或篡改都使游标过期。
def test_audit_cursor_hmac_binding_filters_owner_and_revision(audit_home):
    data = audit_home
    first = audit_records(data.base.owner_home_dir, data.base.owner_id, limit=1, host=data.host)
    assert first['coverage'] == 'partial' and first['next_cursor']
    second = audit_records(data.base.owner_home_dir, data.base.owner_id, cursor=first['next_cursor'],
                           limit=1, host=data.host)
    assert second['records'][0]['source'] == 'request'
    third = audit_records(data.base.owner_home_dir, data.base.owner_id, cursor=second['next_cursor'],
                          limit=1, host=data.host)
    assert third['records'][0]['source'] == 'decision'
    assert third['coverage'] == 'complete' and third['next_cursor'] is None
    for kwargs in ({'actor': 'main-actor'}, {'since': '2023-11-14T22:13:45Z'}, {'cursor': 'tampered'}):
        args = {'cursor': first['next_cursor'], 'host': data.host, **kwargs}
        with pytest.raises(BusinessError, match='CURSOR_EXPIRED'):
            audit_records(data.base.owner_home_dir, data.base.owner_id, **args)
    with pytest.raises(BusinessError, match='CURSOR_EXPIRED'):
        audit_records(data.alice.owner_home_dir, data.alice.owner_id, cursor=first['next_cursor'], host=data.host)
    V13Store(data.base.owner_home_dir).transaction('async_jobs', lambda rows: {},
        idempotency_key='new', params_digest='new')
    with pytest.raises(BusinessError, match='CURSOR_EXPIRED'):
        audit_records(data.base.owner_home_dir, data.base.owner_id, cursor=first['next_cursor'], host=data.host)

# LLM: 仅破坏夹具内请求文件并注入单源异常；其它来源仍应返回，缺口不能被隐藏。
# 函数用途: 真实坏请求文件与来源异常均必须披露 partial，而不是空 complete。
def test_failed_and_unreadable_sources_are_explicit(audit_home, monkeypatch):
    data = audit_home
    bad = data.paths.done / 'bad.json'
    bad.write_text('{bad')
    os.utime(bad, (1700000020, 1700000020))
    page = audit_records(data.base.owner_home_dir, data.base.owner_id, host=data.host)
    assert page['coverage'] == 'partial'
    assert {item['source'] for item in page['unavailable']} == {'decision', 'request'}
    # LLM: 只向被替换的来源读口抛出合成异常，不改其它审计来源或持久数据。
    # 函数用途: 模拟一个原生来源无法打开。
    def failed(paths, query):
        raise OSError('synthetic source failure')
    monkeypatch.setattr(module_w0_audit, 'request_outcome_records', failed)
    page = audit_records(data.base.owner_home_dir, data.base.owner_id, host=data.host)
    assert {'source': 'request', 'reason_code': 'HOST_NOT_READY'} in page['unavailable']
    assert any(row['source'] == 'v13' for row in page['records'])

# LLM: 对临时审计源提交无效筛选或页长，必须以结构化错误拒绝而不能退回默认全量。
# 函数用途: 查询形状错误不能被解释为宽查询。
@pytest.mark.parametrize('kwargs', [{'since': 'bad'}, {'until': '2024-01-01'}, {'actor': ''}, {'limit': True},
                                  {'limit': 101}, {'since': '2025-01-01T00:00:00Z', 'until': '2024-01-01T00:00:00Z'}])
def test_invalid_query_rejected(audit_home, kwargs):
    data = audit_home
    with pytest.raises(BusinessError, match='INVALID_SCHEMA'):
        audit_records(data.base.owner_home_dir, data.base.owner_id, host=data.host, **kwargs)

# ============================================================================
# Source: agent_py_agent/tests/test_v13_w0_store.py
# V13 Store 的耐久主链路、权限边界与故障恢复。
# ============================================================================

# LLM: 只向传入的测试存储提交合成记录，回执脱敏和修订检查均交给公开事务接口。
# 函数用途: 用公开事务接口写入合成记录。
def _write(store, key='request-00000001', *, digest='params', revision=None, value=1, receipt_view=None):
    # LLM: 仅改事务提供的锁内记录，返回的合成秘密用于核对首次响应与回执脱敏。
    # 函数用途: 在锁内改记录并返回一次性合成秘密。
    def mutate(records):
        records['one'] = {'id': 'one', 'value': value}
        return {'id': 'one', 'value': value, 'secret': 'synthetic-once-value'}
    return store.transaction('async_jobs', mutate, idempotency_key=key, params_digest=digest,
                             expected_revision=revision, receipt_view=receipt_view)

# LLM: 在临时 owner 中执行真实写入和重放，秘密不得进入持久回执，旧修订不能覆盖新记录。
# 函数用途: 核验跨实例回执、秘密只返一次、SHA256 持久键与 CAS。
def test_receipts_redact_once_and_cas_across_instances(tmp_path):
    root = tmp_path / 'owner'
    first = V13Store(root, action='job.create')
    result = _write(first, revision=0, receipt_view=lambda row: {**row, 'secret': '<redacted>'})
    assert result['secret'] == 'synthetic-once-value'
    second = V13Store(root, action='job.create')
    assert _write(second, revision=0)['secret'] == '<redacted>'
    assert second.revision('async_jobs') == 1
    assert len(second.list('audit_log')) == 1
    with pytest.raises(BusinessError, match='IDEMPOTENCY_CONFLICT'):
        _write(second, digest='different', revision=0)
    with pytest.raises(BusinessError, match='REVISION_CONFLICT'):
        _write(second, key='request-00000002', revision=0)
    _write(second, key='request-00000002', revision=1, value=2,
           receipt_view=lambda row: {**row, 'secret': '<redacted>'})
    assert first.get('async_jobs', 'one')['value'] == 2
    state = json.loads((root / 'data/business_v13/async_jobs.json').read_bytes())
    expected = hashlib.sha256(module_w0_store._encode(['host', 'job.create', 'request-00000001'])).hexdigest()
    receipts = first._rows('async_jobs')
    receipt = receipts.get('receipts', expected)
    assert receipt['result']['secret'] == '<redacted>'
    assert receipt['fingerprint'] == hashlib.sha256(
        module_w0_store._encode(['async_jobs', 'job.create', 'params', 0])).hexdigest()
    persisted = [json.loads(path.read_bytes()) for path in
                 (root / 'data/business_v13/history/async_jobs/receipts').glob('*.json')]
    assert expected in {row['key'] for row in persisted}
    assert all(len(row['key']) == 64 for row in persisted)
    assert state['history_rows'] is True and state['receipts'] == {}
    assert b'synthetic-once-value' not in root.joinpath(*receipts.parts('receipts', expected)).read_bytes()
    assert b'synthetic-once-value' not in (root / 'data/business_v13/async_jobs.json').read_bytes()
    assert [row['outcome'] for row in first.list('audit_log')] == ['committed', 'committed']

# LLM: 用临时存储的真实游标续页；任何绑定条件变化或签名篡改都必须失败。
# 函数用途: 核验分页跨实例延续，并拒绝其它 owner、collection、筛选、主体、修订与篡改。
def test_cursor_scope_filters_revision_and_hmac(tmp_path):
    store = V13Store(tmp_path / 'owner')
    store.transaction('async_jobs', lambda rows: (rows.update({str(i): {'id': str(i), 'kind': 'x'}
        for i in range(3)}) or {}), idempotency_key='seed', params_digest='seed')
    first = store.page('async_jobs', limit=1, filters={'kind': 'x'})
    token = first['next_cursor']
    assert first['coverage'] == 'partial'
    assert V13Store(tmp_path / 'owner').page('async_jobs', cursor=token, limit=1,
        filters={'kind': 'x'})['items'] == [{'id': '1', 'kind': 'x'}]
    for other, collection, filters in ((V13Store(tmp_path / 'other'), 'async_jobs', {'kind': 'x'}),
            (store, 'assistant_agents', {'kind': 'x'}), (store, 'async_jobs', {'kind': 'y'}),
            (V13Store(tmp_path / 'owner', subject_id='other'), 'async_jobs', {'kind': 'x'})):
        with pytest.raises(BusinessError, match='CURSOR_EXPIRED'):
            other.page(collection, cursor=token, filters=filters)
    tampered = json.loads(base64.urlsafe_b64decode(token))
    tampered['position'] = 2
    with pytest.raises(BusinessError, match='CURSOR_EXPIRED'):
        store.page('async_jobs', cursor=base64.urlsafe_b64encode(json.dumps(tampered).encode()).decode(),
                   filters={'kind': 'x'})
    _write(store, key='change')
    with pytest.raises(BusinessError, match='CURSOR_EXPIRED'):
        store.page('async_jobs', cursor=token, filters={'kind': 'x'})

# LLM: 只读查询不得创建临时 owner 或触发配额准入，所有写入口和审计集合写入均须拒绝。
# 函数用途: 只读入口不创建目录且在 quota 前拒写，审计集合不可由分片改写。
def test_read_only_has_no_side_effects_and_refuses_mutation(tmp_path, monkeypatch):
    root = tmp_path / 'absent'
    readonly = V13Store.read_only(root)
    monkeypatch.setattr(module_w0_store, 'owner_quota_enforcer_from_policy', lambda root: pytest.fail('quota read'))
    assert readonly.list('async_jobs') == []
    assert readonly.get('async_jobs', 'missing') is None
    assert readonly.page('async_jobs') == {'items': [], 'coverage': 'complete', 'next_cursor': None}
    with pytest.raises(BusinessError, match='CAPABILITY_DENIED'):
        _write(readonly)
    with pytest.raises(BusinessError, match='CAPABILITY_DENIED'):
        readonly.transaction_many(('soc_cases', 'soc_alerts'), lambda rows: {},
                                  idempotency_key='readonly', params_digest='readonly')
    assert not root.exists()
    with pytest.raises(BusinessError, match='CAPABILITY_DENIED'):
        V13Store(root).transaction('audit_log', lambda rows: {}, idempotency_key='x', params_digest='x')

# LLM: 仅使用临时 home 构造可信调用；main 集合写到主 owner，撤权后不得再提交。
# 函数用途: 工厂 main 只落主用户路径，每次访问集合核 scope，实时撤销不提交。
def test_main_scope_and_live_authorization(tmp_path):
    base = home_paths(tmp_path)
    user = home_paths_with_owner(base, resolve_owner_home(tmp_path, OwnerIdentity.local_user('alice')))
    allowed = [True]
    host = SimpleNamespace(home_paths=user, config=SimpleNamespace(business_spaces_enabled=True))
    context = bind_business_context(host, authorize=lambda action, facts: allowed[0])
    call = V13Call('system.capabilities.extensions', {}, 'request', user.owner_id, 'host', context,
                   SimpleNamespace(owner_id=user.owner_id), user.owner_home_dir)
    main = v13_store_for(call, 'main')
    main.transaction('admin_ops', lambda rows: (rows.update({'op': {'op_id': 'op'}}) or {'op_id': 'op'}),
                     idempotency_key='main', params_digest='main')
    assert (base.owner_home_dir / 'data/business_v13/admin_ops.json').is_file()
    assert not (user.owner_home_dir / 'data/business_v13/admin_ops.json').exists()
    with pytest.raises(BusinessError, match='BUSINESS_ACCESS_DENIED'):
        v13_store_for(call).list('admin_ops')
    with pytest.raises(BusinessError, match='BUSINESS_ACCESS_DENIED'):
        main.list('async_jobs')
    allowed[0] = False
    with pytest.raises(Exception, match='CAPABILITY_DENIED'):
        main.transaction('admin_ops', lambda rows: {}, idempotency_key='revoked', params_digest='revoked')

# LLM: 多线程共享临时 owner 的真实事务锁，以持久计数、修订和审计核对并发提交。
# 函数用途: 正常事务使用实际锁和文件原语，多个实例并发不丢更新。
def test_multiple_instances_serialize_updates(tmp_path):
    root = tmp_path / 'owner'
    # LLM: 每次新建存储实例参与同一文件锁，不在锁外预读或缓存计数。
    # 函数用途: 每次锁内读取最新计数，禁止锁外旧快照覆盖。
    def increment(index):
        # LLM: 只更新事务传入的当前内存快照，持久化由外层事务完成。
        # 函数用途: 增加当前记录计数。
        def mutate(rows):
            count = rows.get('counter', {}).get('value', 0) + 1
            rows['counter'] = {'value': count}
            return {'value': count}
        return V13Store(root).transaction('async_jobs', mutate, idempotency_key=str(index), params_digest=str(index))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(increment, range(12)))
    store = V13Store(root)
    assert store.get('async_jobs', 'counter') == {'value': 12}
    assert store.revision('async_jobs') == len(results) == len(store.list('audit_log'))

# LLM: 只在临时存储的物化阶段注入故障；已提交记录与审计必须可读，恢复重放不得泄密。
# 函数用途: 替换中断后逻辑审计仍在，同键恢复不再执行 mutate 或泄露秘密。
def test_interrupted_materialization_recovers_same_receipt(tmp_path, monkeypatch):
    root = tmp_path / 'owner'
    write = module_w0_store.write_bytes_atomic_beneath
    # LLM: 仅阻断审计文件替换，其余写入仍调用原原子文件接口。
    # 函数用途: 模拟提交日志成功但审计文件物化失败。
    def fail_audit(owner, parts, raw):
        if parts[-1] == 'audit_log.json':
            raise OSError('synthetic materialization failure')
        return write(owner, parts, raw)
    monkeypatch.setattr(module_w0_store, 'write_bytes_atomic_beneath', fail_audit)
    store = V13Store(root)
    with pytest.raises(OSError, match='synthetic'):
        _write(store, receipt_view=lambda row: {**row, 'secret': '<redacted>'})
    assert store.get('async_jobs', 'one')['value'] == 1
    assert len(store.list('audit_log')) == 1
    monkeypatch.setattr(module_w0_store, 'write_bytes_atomic_beneath', write)
    assert _write(V13Store(root))['secret'] == '<redacted>'
    assert json.loads((root / 'data/business_v13/.transaction.json').read_bytes()) == {}
    assert len(store.list('audit_log')) == 1

# LLM: 保留真实文件锁，记录准入顺序并在配额末核撤权，禁止产生业务文件或提交日志。
# 函数用途: 配额检查之后的授权撤销不能写提交日志。
def test_quota_lock_order_and_last_guard(tmp_path, monkeypatch):
    events, allowed = [], [True]
    lock = module_w0_store.locked_private_directory
    # LLM: 仅追加锁事件，锁的获取、持有和释放仍委托原上下文管理器。
    # 函数用途: 保留实际文件锁并记录其位置。
    @contextmanager
    def tracked_lock(root, *, relative_parts, lock_name):
        events.append('lock')
        with lock(root, relative_parts=relative_parts, lock_name=lock_name):
            yield
    class Admission:
        # LLM: 核对计划写入集合后撤销测试授权，不直接改写任何业务文件。
        # 函数用途: 在配额末核后撤销权限以测试提交末核。
        def check(self, changes):
            events.append('check')
            base = root / 'data/business_v13'
            paths = {change.path for change in changes}
            metadata = {base / name for name in ('async_jobs.json', 'audit_log.json', '.transaction.json')}
            assert metadata <= paths
            rows = paths - metadata
            assert len(paths) == len(changes) == 6
            assert {path.parent for path in rows} == {
                base / 'history/async_jobs/receipts', base / 'history/async_jobs/records',
                base / 'history/audit_log/records'}
            assert all(len(path.stem) == 64 and path.suffix == '.json' for path in rows)
            assert all(change.final_size > 0 for change in changes)
            allowed[0] = False
    class Gate:
        # LLM: 仅记录配额准入事件并提供测试末核对象，不替换真实文件锁。
        # 函数用途: 记录准入先于文件锁。
        @contextmanager
        def admission(self):
            events.append('admission')
            yield Admission()
    monkeypatch.setattr(module_w0_store, 'locked_private_directory', tracked_lock)
    monkeypatch.setattr(module_w0_store, 'owner_quota_enforcer_from_policy', lambda root: Gate())
    root = tmp_path / 'owner'
    with pytest.raises(BusinessError, match='CAPABILITY_DENIED'):
        _write(V13Store(root, authorize=lambda: allowed[0]))
    assert events == ['admission', 'lock', 'check']
    assert not (root / 'data/business_v13/async_jobs.json').exists()
    assert not (root / 'data/business_v13/.transaction.json').exists()

# LLM: 仅在 tmp_path 内创建符号链接和损坏状态，外侧目录不得写入，错误不得降格为空结果。
# 函数用途: 符号链接父链、叶子与损坏状态绝不能跟随或变成空成功。
def test_nofollow_and_corrupt_state_fail_closed(tmp_path):
    root, outside = tmp_path / 'owner', tmp_path / 'outside'
    outside.mkdir()
    root.mkdir()
    (root / 'data').symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        _write(V13Store(root))
    assert list(outside.iterdir()) == []
    clean = tmp_path / 'clean'
    _write(V13Store(clean))
    path = clean / 'data/business_v13/async_jobs.json'
    path.write_text('{broken')
    with pytest.raises(BusinessError, match='STORE_UNAVAILABLE'):
        V13Store(clean).list('async_jobs')
    with pytest.raises(BusinessError, match='INVALID_SCHEMA'):
        V13Store(clean).list('../outside')

# LLM: 在临时双集合提交后阻断物化，读者和恢复回执必须保持原子性与秘密只返一次。
# 函数用途: 多集合物化中断时读者仍看到同笔全量提交，并且重放不再次返回秘密。
def test_many_interrupted_materialization_is_atomic_and_secret_once(tmp_path, monkeypatch):
    root = tmp_path / 'owner'
    store = V13Store(root, action='case.link')
    write = module_w0_store.write_bytes_atomic_beneath
    # LLM: 仅对第二集合的物化抛错，其余文件仍调用原原子写入接口。
    # 函数用途: 第一份集合成功物化后阻断第二份集合。
    def fail_second(owner, parts, raw):
        if parts[-1] == 'soc_cases.json':
            raise OSError('synthetic second collection failure')
        return write(owner, parts, raw)
    # LLM: 在同一事务快照内更新双向关联，合成秘密仅供首次响应和脱敏回执核对。
    # 函数用途: 单笔维护双向关联。
    def link(records):
        records['soc_cases']['case'] = {'case_id': 'case', 'alert_ids': ['alert']}
        records['soc_alerts']['alert'] = {'alert_id': 'alert', 'case_id': 'case'}
        return {'linked': True, 'secret': 'synthetic-multi-once'}
    monkeypatch.setattr(module_w0_store, 'write_bytes_atomic_beneath', fail_second)
    with pytest.raises(OSError, match='synthetic second'):
        store.transaction_many(('soc_cases', 'soc_alerts'), link, idempotency_key='link',
            params_digest='link', expected_revision=0,
            receipt_view=lambda row: {**row, 'secret': '<redacted>'})
    assert (root / 'data/business_v13/soc_alerts.json').is_file()
    assert not (root / 'data/business_v13/soc_cases.json').exists()
    readonly = V13Store.read_only(root)
    assert readonly.get('soc_cases', 'case')['alert_ids'] == ['alert']
    assert readonly.get('soc_alerts', 'alert')['case_id'] == 'case'
    assert {row['collection'] for row in readonly.list('audit_log')} == {'soc_cases', 'soc_alerts'}
    monkeypatch.setattr(module_w0_store, 'write_bytes_atomic_beneath', write)
    replay = V13Store(root, action='case.link').transaction_many(('soc_alerts', 'soc_cases'),
        lambda rows: pytest.fail('replay ran mutation'), idempotency_key='link', params_digest='link', expected_revision=0)
    assert replay == {'linked': True, 'secret': '<redacted>'}
    assert json.loads((root / 'data/business_v13/.transaction.json').read_bytes()) == {}
    assert store.revision('soc_cases') == store.revision('soc_alerts') == 1
    assert len(store.list('audit_log')) == 2

# LLM: 在临时双集合上验证失败不留半笔数据，修订和幂等冲突均由真实事务边界拒绝。
# 函数用途: CAS、幂等和 mutator 异常都必须同时约束全部集合。
def test_many_cas_replay_and_mutation_rollback(tmp_path):
    store = V13Store(tmp_path / 'owner')
    names = ('soc_alerts', 'soc_feedback')
    # LLM: 只改事务内存快照后抛出异常，不自行落盘，以核对外层事务回滚。
    # 函数用途: 在内存变更一部分后失败，证明没有半笔提交。
    def fail(records):
        records['soc_alerts']['one'] = {'value': 1}
        raise ValueError('synthetic mutation failure')
    with pytest.raises(ValueError, match='synthetic mutation'):
        store.transaction_many(names, fail, idempotency_key='rollback', params_digest='rollback')
    assert store.list('soc_alerts') == store.list('soc_feedback') == []
    assert store.list('audit_log') == []
    result = store.transaction_many(names, lambda rows: {'ok': True}, idempotency_key='cas',
        params_digest='cas', expected_revision={'soc_alerts': 0, 'soc_feedback': 0})
    assert result == {'ok': True}
    with pytest.raises(BusinessError, match='REVISION_CONFLICT'):
        store.transaction_many(names, lambda rows: {}, idempotency_key='stale', params_digest='stale',
            expected_revision={'soc_alerts': 1, 'soc_feedback': 0})
    assert store.revision('soc_alerts') == store.revision('soc_feedback') == 1
    with pytest.raises(BusinessError, match='IDEMPOTENCY_CONFLICT'):
        store.transaction_many(names, lambda rows: {}, idempotency_key='cas', params_digest='changed',
            expected_revision={'soc_alerts': 0, 'soc_feedback': 0})
    with pytest.raises(BusinessError, match='INVALID_SCHEMA'):
        store.transaction_many(names, lambda rows: {}, idempotency_key='shape', params_digest='shape',
            expected_revision={'soc_alerts': 1})

# LLM: 多线程使用真实多集合事务和同一临时 owner，关联计数、修订与审计必须一致。
# 函数用途: 多实例并发关联更新共享同一把锁，不丢更新或分叉两份计数。
def test_many_concurrent_instances_preserve_pair_invariant(tmp_path):
    root = tmp_path / 'owner'
    # LLM: 每次用新存储实例提交双集合事务，幂等键仅来自当前测试索引。
    # 函数用途: 一笔更新关联的两个集合。
    def increment(index):
        # LLM: 仅在事务快照内核对并递增两边，不允许先更新一边再另开事务。
        # 函数用途: 锁内核对旧不变量并更新两边。
        def mutate(records):
            alerts = records['soc_alerts'].get('counter', {}).get('value', 0)
            feedback = records['soc_feedback'].get('counter', {}).get('value', 0)
            assert alerts == feedback
            records['soc_alerts']['counter'] = {'value': alerts + 1}
            records['soc_feedback']['counter'] = {'value': feedback + 1}
            return {'value': alerts + 1}
        return V13Store(root).transaction_many(('soc_alerts', 'soc_feedback'), mutate,
            idempotency_key=str(index), params_digest=str(index))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(increment, range(12)))
    store = V13Store(root)
    assert sorted(row['value'] for row in results) == list(range(1, 13))
    assert store.get('soc_alerts', 'counter') == store.get('soc_feedback', 'counter') == {'value': 12}
    assert store.revision('soc_alerts') == store.revision('soc_feedback') == 12
    assert len(store.list('audit_log')) == 24

# LLM: 仅在临时目录重建旧日志格式，真实读取和重放必须保留已提交回执并清理恢复日志。
# 函数用途: 升级后的读取和滚前仍识别原单集合提交日志，避免遗失已提交回执。
def test_legacy_single_journal_is_readable_and_recovered(tmp_path):
    root = tmp_path / 'owner'
    store = V13Store(root)
    _write(store, receipt_view=lambda row: {**row, 'secret': '<redacted>'})
    folder = root / 'data/business_v13'
    state = json.loads((folder / 'async_jobs.json').read_bytes())
    audit = json.loads((folder / 'audit_log.json').read_bytes())
    (folder / '.transaction.json').write_bytes(module_w0_store._encode(
        {'collection': 'async_jobs', 'state': state, 'audit': audit}))
    assert store.get('async_jobs', 'one') == {'id': 'one', 'value': 1}
    assert _write(V13Store(root))['secret'] == '<redacted>'
    assert json.loads((folder / '.transaction.json').read_bytes()) == {}
    assert len(store.list('audit_log')) == 1

# LLM: 审计单行100个ID不限制产品原子事务；物化中断后也不能暴露半批删除。
# 函数用途: 验证101与200条双集合写删及完整分段审计仍是一笔提交。
@pytest.mark.parametrize('count', [101, 200])
def test_many_bulk_changes_keep_atomicity_and_all_audit_ids(tmp_path, monkeypatch, count):
    store = V13Store(tmp_path / 'bulk-owner', action='bulk.change')
    names = ('soc_alerts', 'soc_feedback')
    keys = {f'row-{index:03d}' for index in range(count)}

    # LLM: 所有相关行在同一mutate内建立，审计不能迫使调用方拆批。
    # 函数用途: 创建指定数量的关联记录。
    def seed(records):
        for name in names:
            records[name].update({key: {'id': key, 'linked': True} for key in keys})
        return {'created': len(keys)}

    assert store.transaction_many(names, seed, idempotency_key='seed-bulk', params_digest='seed-bulk') == {'created': count}
    write = module_w0_store.write_bytes_atomic_beneath

    # LLM: journal提交后阻断第二集合物化，读者仍须看到整笔删除。
    # 函数用途: 注入真实原子替换链上的中断。
    def interrupt(owner, parts, raw):
        if parts[-1] == 'soc_feedback.json':
            raise OSError('synthetic bulk interruption')
        return write(owner, parts, raw)

    # LLM: 两边都在一个事务清除，不能拆开调用transaction。
    # 函数用途: 原子删除全部关联行。
    def remove(records):
        for name in names:
            records[name].clear()
        return {'deleted': len(keys)}

    monkeypatch.setattr(module_w0_store, 'write_bytes_atomic_beneath', interrupt)
    with pytest.raises(OSError, match='synthetic bulk'):
        store.transaction_many(names, remove, idempotency_key='delete-bulk', params_digest='delete-bulk')
    assert all(store.list(name) == [] and store.revision(name) == 2 for name in names)
    audit = store.list('audit_log')
    assert len(audit) == 8 and all(len(row['record_ids']) <= 100 for row in audit)
    for name in names:
        for revision in (1, 2):
            observed = [key for row in audit if row['collection'] == name and row['revision'] == revision
                for key in row['record_ids']]
            assert len(observed) == len(keys) and set(observed) == keys
    monkeypatch.setattr(module_w0_store, 'write_bytes_atomic_beneath', write)
    assert store.transaction_many(names, lambda rows: pytest.fail('bulk replay mutated'),
        idempotency_key='delete-bulk', params_digest='delete-bulk') == {'deleted': count}
    assert len(store.list('audit_log')) == 8

# ============================================================================
# Source: agent_py_agent/tests/test_v13_w10_access_review.py
# #90 原生扫描、真实W0持久化与Gateway HTTP往返测试。
# ============================================================================

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
def seed_w10_access_review(env, collection, records, *, owner='local/main', scope='main'):
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
        seed_w10_access_review(env, 'api_keys', {'key-old': row}, owner=owner, scope='owner')
    seed_w10_access_review(env, 'scim_tokens', {'scim-old': {'token_id': 'scim-old', 'owner_id': 'local/main', 'enabled': True,
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
    seed_w10_access_review(env, 'rbac_roles', {'analyst': role})
    seed_w10_access_review(env, 'rbac_assignments', {'alice': {'user_id': 'alice', 'roles': ['analyst']}})
    assert review.run(env.call('access_review.run', {'provider': 'local'}))['run']['findings_created'] == 0
    role['permissions'] += ['access_review.kinds']
    seed_w10_access_review(env, 'rbac_roles', {'analyst': role})
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
def test_envelopes_w10_access_review(v13_native):
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

# ============================================================================
# Source: agent_py_agent/tests/test_v13_w11_autonomy.py
# W11自治建议与业务上下文：原生存储、实际HTTP及负向边界回归。
# ============================================================================

# LLM: 测试根均来自tmp_path，不接触运行中的用户空间。
# 函数用途: 建立原生签发context及真实业务服务。
def _host(tmp_path, *, admin=True):
    paths = home_paths_with_owner(home_paths(tmp_path / 'root'), resolve_owner_home(tmp_path / 'root'))
    host = SimpleNamespace(home_paths=paths,
                           config=SimpleNamespace(business_spaces_enabled=True),
                           business_native_admin=lambda: admin,
                           business_v13_clock=lambda: datetime(2026, 11, 2, tzinfo=timezone.utc).timestamp())
    return host

# LLM: 所有断言经过真实统一dispatcher和schema，不直接调用产品handler。
# 函数用途: 发送一笔本机业务调用并返回标准信封。
def _call_w11_autonomy(host, action, params=None, *, key=None, revision=None, authorize=None):
    context = api.bind_business_context(host, authorize=authorize or (lambda action, facts: True), subject_id='reviewer')
    request = {'action': action, 'request_id': 'test-' + action, 'params': params or {}}
    if key is not None:
        request['idempotency_key'] = key
    if revision is not None:
        request['expected_revision'] = revision
    return api.execute_business(request, context, api.BusinessServices(host))

# LLM: 断言同一标准信封及动作结果schema，不能把任意HTTP200当成功。
# 函数用途: 取出已校验的完成结果。
def _completed_w11_autonomy(response, action):
    status, body = response
    assert status == 200, body
    assert body['state'] == 'completed' and body['error'] is None
    return v13_action_spec(action).validate_result(body['data'])

# LLM: 测试种子沿原Store事务，无手写第二持久格式。
# 函数用途: 保存符合记录边界的合成事实。
def _seed(host, collection, records):
    store = V13Store(host.home_paths.owner_home_dir, owner_id=host.home_paths.owner_id)

    # LLM: 合成数据只在隔离测试根创建，不碰真实运行账。
    # 函数用途: 在原Store中提交给定记录。
    def mutate(current):
        current.update(records)
        return {'seeded': len(records)}

    store.transaction(collection, mutate, idempotency_key='seed-' + collection,
                      params_digest=hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest())
    return store

# LLM: 完整W8记录字段不随当前handler读取范围缩水。
# 函数用途: 创建规范样本告警。
def _alert(host, alert_id, *, verdict='true_positive', entity='core-01', created_at=None):
    at = created_at or '2026-11-01T12:00:00+00:00'
    return {'alert_id': alert_id, 'space_id': 'space-one', 'title': '合成告警', 'description': '',
            'severity': 'high', 'status': 'new', 'verdict': verdict, 'ai_confidence': 0.9,
            'disposition': 'triage', 'rule_id': None, 'source': 'ingest', 'external_ref': None,
            'entities': [{'type': 'host', 'value': entity}], 'techniques': [], 'assignee': None,
            'case_id': None, 'tags': ['core'], 'evidence_refs': [], 'acknowledged_at': None,
            'resolved_at': None, 'revision': 0, 'created_at': at, 'updated_at': at,
            'owner_id': host.home_paths.owner_id}

# LLM: 一条反馈必须保留原建议及分析师最终结构化裁决。
# 函数用途: 创建规范人机反馈记录。
def _feedback(host, identifier, alert_id, verdict, at):
    return {'override_id': identifier, 'alert_id': alert_id, 'space_id': 'space-one', 'rule_id': None,
            'original_verdict': 'true_positive', 'override_verdict': verdict, 'reason': '合成审核',
            'analyst': 'reviewer', 'source': 'override', 'created_at': at,
            'owner_id': host.home_paths.owner_id, 'revision': 0, 'updated_at': at}

# LLM: 同键重放和不同参数复用键分别验证，旧CAS不能覆盖并发新版本。
# 函数用途: 验证自治建议列表、更新、重置和版本单调性。
def test_policy_list_update_reset_cas(tmp_path):
    host = _host(tmp_path)
    initial = _completed_w11_autonomy(_call_w11_autonomy(host, 'autonomy_policy.list'), 'autonomy_policy.list')
    assert initial == {'policies': [], 'default_level': 'manual_approval', 'enforcement': 'advisory', 'revision': 0}
    params = {'action': 'isolate_host', 'level': 'shadow'}
    first = _completed_w11_autonomy(_call_w11_autonomy(host, 'autonomy_policy.update', params, key='policy-update-one', revision=0),
                       'autonomy_policy.update')
    assert first['policy']['level'] == 'shadow' and first['revision'] == 1
    assert _call_w11_autonomy(host, 'autonomy_policy.update', params, key='policy-update-one', revision=0)[1]['data'] == first
    conflict = _call_w11_autonomy(host, 'autonomy_policy.update', {**params, 'level': 'autonomous'}, key='policy-update-one', revision=0)
    assert conflict[0] == 409 and conflict[1]['error']['code'] == 'IDEMPOTENCY_CONFLICT'
    stale = _call_w11_autonomy(host, 'autonomy_policy.reset', {'action': 'isolate_host'}, key='policy-reset-old', revision=0)
    assert stale[0] == 409 and stale[1]['error']['code'] == 'REVISION_CONFLICT'
    reset = _completed_w11_autonomy(_call_w11_autonomy(host, 'autonomy_policy.reset', {'action': 'isolate_host'},
                             key='policy-reset-new', revision=1), 'autonomy_policy.reset')
    assert reset == {'action': 'isolate_host', 'level': 'manual_approval', 'revision': 2}
    assert _call_w11_autonomy(host, 'autonomy_policy.list')[1]['data']['revision'] == 2

# LLM: 影子模式只改建议，CAS与其它策略共用规范最高版本。
# 函数用途: 验证影子分类读取、设置和旧版本拒绝。
def test_shadow_mode_get_set(tmp_path):
    host = _host(tmp_path)
    assert _call_w11_autonomy(host, 'autonomy_policy.shadow_mode.get')[1]['data'] == {'classes': [], 'revision': 0}
    value = _completed_w11_autonomy(_call_w11_autonomy(host, 'autonomy_policy.shadow_mode.set', {'alert_class': 'service', 'shadow': True},
                             key='shadow-set-class', revision=0), 'autonomy_policy.shadow_mode.set')
    assert value['class']['shadow'] is True and value['revision'] == 1
    assert _call_w11_autonomy(host, 'autonomy_policy.shadow_mode.get')[1]['data']['classes'] == [value['class']]
    assert _call_w11_autonomy(host, 'autonomy_policy.shadow_mode.set', {'alert_class': 'service', 'shadow': False},
                 key='shadow-stale-key', revision=0)[0] == 409

# LLM: 授权只保存在当前owner，过期与撤销不混同，撤销不恢复。
# 函数用途: 验证建议授权创建、幂等、过期过滤和最终撤销。
def test_grants_create_list_revoke(tmp_path):
    host = _host(tmp_path)
    params = {'action': 'isolate_host', 'level': 'shadow', 'expires_in_hours': 1}
    first = _completed_w11_autonomy(_call_w11_autonomy(host, 'autonomy_policy.grants.create', params, key='grant-create-one'),
                       'autonomy_policy.grants.create')['grant']
    assert _call_w11_autonomy(host, 'autonomy_policy.grants.create', params, key='grant-create-one')[1]['data']['grant'] == first
    assert _call_w11_autonomy(host, 'autonomy_policy.grants.list')[1]['data']['items'] == [first]
    now = host.business_v13_clock()
    host.business_v13_clock = lambda: now + 3601
    assert _call_w11_autonomy(host, 'autonomy_policy.grants.list')[1]['data']['items'] == []
    assert _call_w11_autonomy(host, 'autonomy_policy.grants.list', {'include_expired': True})[1]['data']['items'] == [first]
    revoked = _completed_w11_autonomy(_call_w11_autonomy(host, 'autonomy_policy.grants.revoke', {'grant_id': first['grant_id']},
                               key='grant-revoke-one'), 'autonomy_policy.grants.revoke')
    assert revoked == {'grant_id': first['grant_id'], 'revoked': True}
    assert _call_w11_autonomy(host, 'autonomy_policy.grants.list', {'include_expired': True})[1]['data']['items'] == []
    absent = _call_w11_autonomy(host, 'autonomy_policy.grants.revoke', {'grant_id': 'missing'}, key='grant-missing-key')
    assert absent[0] == 404 and absent[1]['error']['code'] == 'RESOURCE_NOT_FOUND'

# LLM: 同一个告警多次修订只计最后反馈，窗口外、未来及其它owner不得混入。
# 函数用途: 从真实存储反馈计算7d人机一致率。
def test_agreement_example_window_7d_from_feedback(tmp_path):
    host = _host(tmp_path)
    _seed(host, 'soc_alerts', {'a': _alert(host, 'a'), 'b': _alert(host, 'b'),
          'c': _alert(host, 'c', verdict=None), 'old': _alert(host, 'old', created_at='2025-01-01T00:00:00+00:00')})
    _seed(host, 'soc_feedback', {
        'f1': _feedback(host, 'f1', 'a', 'false_positive', '2026-11-01T13:00:00+00:00'),
        'f2': _feedback(host, 'f2', 'a', 'true_positive', '2026-11-01T14:00:00+00:00'),
        'f3': _feedback(host, 'f3', 'b', 'false_positive', '2026-11-01T14:00:00+00:00')})
    result = _completed_w11_autonomy(_call_w11_autonomy(host, 'autonomy_policy.agreement', {'window': '7d'}), 'autonomy_policy.agreement')
    assert (result['ai_suggestions'], result['human_reviewed'], result['agreed'], result['agreement_rate']) == (2, 2, 1, 0.5)
    assert result['by_action'] == [{'action': 'triage', 'suggested': 2, 'reviewed': 2, 'agreed': 1, 'agreement_rate': 0.5}]
    empty = _call_w11_autonomy(_host(tmp_path / 'empty'), 'autonomy_policy.agreement', {'window': '7d'})[1]['data']
    assert empty['human_reviewed'] == 0 and empty['agreement_rate'] is None

# LLM: 预览只读，版本、回执和审计不应增加；匹配结果直接由合成事实计算。
# 函数用途: 验证规则创建、过滤、实体glob及影响预览。
def test_business_context_rules_create_list_preview(tmp_path):
    host = _host(tmp_path)
    store = _seed(host, 'soc_alerts', {'a': _alert(host, 'a'), 'b': _alert(host, 'b', entity='other')})
    rule = {'name': '核心服务', 'match': {'entity_type': 'host', 'pattern': 'core-*', 'tags': ['core']},
            'effect': {'severity_boost': 2, 'priority_label': '核心'}, 'enabled': True}
    created = _completed_w11_autonomy(_call_w11_autonomy(host, 'business_context.rules.create', rule, key='context-rule-key'),
                         'business_context.rules.create')['rule']
    assert _call_w11_autonomy(host, 'business_context.rules.list', {'enabled': True})[1]['data']['items'] == [created]
    before = store.revision('soc_alerts'), store.revision('business_context_rules'), store.revision('audit_log')
    result = _completed_w11_autonomy(_call_w11_autonomy(host, 'business_context.rules.preview', {'rule_id': created['rule_id']}),
                        'business_context.rules.preview')
    assert result['matched_alerts'] == 1 and result['affected_entities'] == 1
    assert result['severity_changes'] == {'high->critical': 1}
    assert result['sample'] == [{'alert_id': 'a', 'entity': 'core-01', 'before_severity': 'high', 'after_severity': 'critical'}]
    assert before == (store.revision('soc_alerts'), store.revision('business_context_rules'), store.revision('audit_log'))
    assert _call_w11_autonomy(host, 'business_context.rules.preview')[0] == 400
    assert _call_w11_autonomy(host, 'business_context.rules.preview', {'rule_id': created['rule_id'],
                 'rule': {'match': rule['match'], 'effect': rule['effect']}})[0] == 400
    assert _call_w11_autonomy(host, 'business_context.rules.preview', {'rule_id': 'missing'})[0] == 404
    other = _host(tmp_path / 'other')
    assert _call_w11_autonomy(other, 'business_context.rules.preview', {'rule_id': created['rule_id']})[0] == 404

# LLM: 原生管理员门、功能开关与live check三者均不可被数据字段绕过。
# 函数用途: 验证非管理员和已撤权上下文不产生策略记录。
def test_admin_owner_and_live_access_boundaries(tmp_path):
    ordinary = _host(tmp_path, admin=False)
    for action, params, revision in (
        ('autonomy_policy.update', {'action': 'service', 'level': 'shadow'}, 0),
        ('autonomy_policy.reset', {'action': 'service'}, 0),
        ('autonomy_policy.shadow_mode.set', {'alert_class': 'service', 'shadow': True}, 0),
        ('autonomy_policy.grants.create', {'action': 'service', 'level': 'shadow'}, None),
        ('autonomy_policy.grants.revoke', {'grant_id': 'missing'}, None),
    ):
        result = _call_w11_autonomy(ordinary, action, params, key='ordinary-admin-denied', revision=revision)
        assert result[0] == 403, result
    assert not (ordinary.home_paths.owner_home_dir / 'data' / 'business_v13').exists()
    result = _call_w11_autonomy(ordinary, 'autonomy_policy.list', authorize=lambda action, facts: False)
    assert result[0] == 403 and result[1]['error']['code'] == 'CAPABILITY_DENIED'
    ordinary.config.business_spaces_enabled = False
    assert _call_w11_autonomy(ordinary, 'autonomy_policy.list')[0] == 503

# LLM: 同一清单应包含所有46动作，闭合schema必须在任何领域调用前拒绝未知字段。
# 函数用途: 验证W11静态目录、权限位与Schema引用闭包。
def test_registry_contracts_closed_and_complete(tmp_path):
    from agent_py_agent.agent.gateway_parts.business_api_v13_w11 import (
        _ACTION_CONTRACTS,
        V13_ACTIONS,
        V13_COLLECTIONS,
        V13_DEFS,
        V13_JOB_KINDS,
    )
    # 经冻结合同逐字段核验后的静态投影指纹；不依赖部署外任务文件。
    for value, expected in ((_ACTION_CONTRACTS, '609892a9c5d0fcb76b20062d76da36463439fef8c6b0b7255e5d9215db943398'),
                            (V13_DEFS, '3fd49da67b0f0955d66f0533f6eff0b09ab20c2f6fb5eed57610e7404020e9fc')):
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
        assert hashlib.sha256(encoded).hexdigest() == expected
    assert len(V13_ACTIONS) == 46
    assert len({item.name for item in V13_ACTIONS}) == 46
    assert V13_COLLECTIONS['conversations'] == V13_COLLECTIONS['soc_alerts'] == 'owner'
    assert set(V13_JOB_KINDS) == {'skill_backtest'}
    host = _host(tmp_path)
    for item in V13_ACTIONS:
        request = {'action': item.name, 'request_id': 'closed-check', 'params': {'unexpected_field': True}}
        if item.idempotency_required:
            request['idempotency_key'] = 'closed-schema-key'
        if item.expected_revision_required:
            request['expected_revision'] = 0
        context = api.bind_business_context(host, authorize=lambda action, facts: True)
        status, body = api.execute_business(request, context, api.BusinessServices(host))
        assert status == 400 and body['error']['code'] == 'INVALID_SCHEMA', (item.name, body)
        assert v13_action_spec(item.name).result_schema

# LLM: 实际GatewayHTTPServer消费标准JSON，而非前后端各造一个假信封。
# 函数用途: 验证真实网络入口的建议读取、规则写入与标准响应。
def test_envelopes_w11_autonomy(native_http):
    for action, params in (('autonomy_policy.list', {}), ('autonomy_policy.shadow_mode.get', {}),
                           ('autonomy_policy.grants.list', {}), ('autonomy_policy.agreement', {'window': '7d'}),
                           ('business_context.rules.list', {})):
        status, _, raw = native_request(native_http, 'POST', '/client/business',
            {'action': action, 'request_id': 'wire-' + action, 'params': params})
        data = _completed_w11_autonomy((status, json.loads(raw)), action)
        if action == 'autonomy_policy.list':
            assert data['enforcement'] == 'advisory'
    rule = {'name': 'wire规则', 'match': {'entity_type': 'host', 'pattern': 'core-*'},
            'effect': {'severity_boost': 1, 'priority_label': None}}
    status, _, raw = native_request(native_http, 'POST', '/client/business',
        {'action': 'business_context.rules.create', 'request_id': 'wire-rule-create', 'params': rule,
         'idempotency_key': 'wire-context-create'})
    created = _completed_w11_autonomy((status, json.loads(raw)), 'business_context.rules.create')['rule']
    status, _, raw = native_request(native_http, 'POST', '/client/business',
        {'action': 'business_context.rules.preview', 'request_id': 'wire-preview',
         'params': {'rule_id': created['rule_id']}})
    assert _completed_w11_autonomy((status, json.loads(raw)), 'business_context.rules.preview')['matched_alerts'] == 0
    status, _, raw = native_request(native_http, 'POST', '/client/business',
        {'action': 'business_context.rules.preview', 'request_id': 'wire-other-owner',
         'params': {'rule_id': created['rule_id']}}, user='business-bob')
    assert status == 404 and json.loads(raw)['error']['code'] == 'RESOURCE_NOT_FOUND'

# LLM: 时钟在mutate中撤回管理员身份，确定性覆盖提交前而不是入口前撤权。
# 函数用途: 验证原生管理员资格撤回后不留下策略或审计记录。
def test_admin_revocation_at_commit(tmp_path):
    host = _host(tmp_path)
    current = {'admin': True}
    host.business_native_admin = lambda: current['admin']
    now = host.business_v13_clock()

    # LLM: 仅撤销管理员资格，普通上下文依旧有效，避免用普通撤权掩盖缺口。
    # 函数用途: 在事务构建时间触发身份变化。
    def clock():
        current['admin'] = False
        return now

    host.business_v13_clock = clock
    result = _call_w11_autonomy(host, 'autonomy_policy.update', {'action': 'triage', 'level': 'shadow'},
                   key='revoke-admin-last', revision=0)
    assert result[0] == 403, result
    store = V13Store(host.home_paths.owner_home_dir, owner_id=host.home_paths.owner_id)
    assert store.list('autonomy_policies') == []
    assert store.list('audit_log') == []

# LLM: 审核窗口不能被额外的告警创建时间过滤，时间偏移须按实际时刻排序。
# 函数用途: 验证老告警的新审核及多时区反馈的正确一致率。
def test_agreement_review_window_and_timezone_order(tmp_path):
    host = _host(tmp_path)
    _seed(host, 'soc_alerts', {'old': _alert(host, 'old', created_at='2026-10-20T00:00:00+00:00')})
    earlier = _feedback(host, 'f1', 'old', 'false_positive', '2026-11-01T10:00:00+02:00')
    later = _feedback(host, 'f2', 'old', 'false_positive', '2026-11-01T09:00:00+00:00')
    later['original_verdict'] = 'false_positive'
    _seed(host, 'soc_feedback', {'f1': earlier, 'f2': later})
    result = _completed_w11_autonomy(_call_w11_autonomy(host, 'autonomy_policy.agreement', {'window': '7d'}), 'autonomy_policy.agreement')
    assert (result['human_reviewed'], result['agreed'], result['agreement_rate']) == (1, 0, 0.0)

# LLM: 没有后台写口的宿主不应在能力发现中承诺可受理异步回测。
# 函数用途: 验证回测readiness依赖原生异步作业能力。
def test_backtest_readiness_without_jobs(tmp_path):
    host = _host(tmp_path)
    data = _completed_w11_autonomy(_call_w11_autonomy(host, 'system.capabilities.extensions'), 'system.capabilities.extensions')
    assert 'skills.backtest' not in data['supported_actions']

# LLM: 注入实际入口内的来源异常，读与写的未知结果必须采用不同精确错误码。
# 函数用途: 验证所有自治写口的OUTCOME_UNKNOWN及读取来源失败信封。
def test_source_failures_and_unknown_mutations(tmp_path, monkeypatch):
    from agent_py_agent.agent.business_spaces import v13_w11_autonomy as domain
    host = _host(tmp_path)

    # LLM: 模拟领域来源进入后失败，不伪造成功或吞异常。
    # 函数用途: 为同一dispatcher注入真实OSError分支。
    def unavailable(call):
        raise OSError('synthetic source unavailable')

    monkeypatch.setattr(domain, 'v13_store_for', unavailable)
    for action, params, revision in (
        ('autonomy_policy.update', {'action': 'triage', 'level': 'shadow'}, 0),
        ('autonomy_policy.reset', {'action': 'triage'}, 0),
        ('autonomy_policy.shadow_mode.set', {'alert_class': 'service', 'shadow': True}, 0),
        ('autonomy_policy.grants.create', {'action': 'triage', 'level': 'shadow'}, None),
        ('autonomy_policy.grants.revoke', {'grant_id': 'missing'}, None),
        ('business_context.rules.create', {'name': 'rule', 'match': {'entity_type': 'any', 'pattern': '*'},
                                          'effect': {'severity_boost': 1, 'priority_label': None}}, None),
    ):
        status, body = _call_w11_autonomy(host, action, params, key='unknown-mutation-key', revision=revision)
        assert status == 503 and body['error']['code'] == 'OUTCOME_UNKNOWN', body
        assert body['state'] == 'failed' and body['data'] == {}
    status, body = _call_w11_autonomy(host, 'autonomy_policy.list')
    assert status == 503 and body['error']['code'] == 'HOST_NOT_READY'

# ============================================================================
# Source: agent_py_agent/tests/test_v13_w11_conversations.py
# W11 会话与 Copilot：真实 Gateway 往返、线程别名、上下文事实与流式过滤。
# ============================================================================

# LLM: 唯一被测响应来自真实Gateway，不手写替代信封。
# 函数用途: 发起一次业务调用并返回HTTP状态与解码正文。
def call(native, action, params, *, idempotency_key=None, expected_revision=None, user='business-alice'):
    return v13_call(native, action, params, user=user, idempotency_key=idempotency_key,
        expected_revision=expected_revision)

# LLM: 幂等键必须满足冻结模式，测试用合成值不带真实凭据。
# 函数用途: 生成一次调用用的稳定幂等键。
def key(prefix):
    return prefix + '-' + uuid4().hex

# LLM: 空间名在owner内唯一，测试用唯一名创建独立空间。
# 函数用途: 经真实W1入口创建当前用户空间并返回ID。
def new_space(native, name):
    status, body = call(native, 'spaces.create', {'name': name}, idempotency_key=key('space'))
    assert status == 200, body
    return body['data']['space_id']

# LLM: 会话ID与线程ID来自真实创建回执，不从标题或路径推断。
# 函数用途: 经HTTP新建会话并返回会话视图。
def conversation(native, space_id, title):
    status, body = call(native, 'conversations.create', {'space_id': space_id, 'title': title},
        idempotency_key=key('conversation'))
    assert status == 200, body
    assert body['state'] == 'completed' and body['error'] is None
    return body['data']['conversation']

# LLM: 种入走原Store事务，不手写生产文件形状。
# 函数用途: 返回当前用户真实owner存储句柄。
def owner_store(native, user='business-alice'):
    from agent_py_agent.agent.business_spaces.v13_store import V13Store

    server = native[0] if isinstance(native, tuple) else native
    [services] = [item for item in server._business_services.values() if item.owner_id.endswith('/' + user)]
    return V13Store(services.owner_home, owner_id=services.owner_id), services

# LLM: 记录ID由记录内的规范ID字段决定，不按顺序猜。
# 函数用途: 向指定集合种入一条规范记录并返回其ID。
def seed_w11_conversations(native, collection, record):
    store, _ = owner_store(native)
    identifier = next(item for item in record if item.endswith('_id') and item != 'owner_id')

    # LLM: 同名记录不覆盖，重复种入直接报错。
    # 函数用途: 写入一条种子记录。
    def mutate(rows):
        assert record[identifier] not in rows
        rows[record[identifier]] = record
        return {'count': len(rows)}

    store.transaction(collection, mutate, idempotency_key='seed-' + uuid4().hex,
        params_digest='seed-' + uuid4().hex)
    return record[identifier]

# LLM: 上下文事实只来自本owner真实记录，形状与W8集合声明一致。
# 函数用途: 种入一条告警记录并返回ID。
def seed_alert(native, space_id, *, title='可疑登录', description='合成描述'):
    _, services = owner_store(native)
    now = '2026-11-02T00:00:00+00:00'
    return seed_w11_conversations(native, 'soc_alerts', {'alert_id': 'alert-' + uuid4().hex[:12], 'space_id': space_id,
        'title': title, 'description': description, 'severity': 'high', 'status': 'new', 'verdict': None,
        'ai_confidence': 0.82, 'disposition': None, 'rule_id': None, 'source': 'monitor',
        'external_ref': None, 'entities': [{'type': 'account', 'value': 'svc-synthetic'}],
        'techniques': ['T1078'], 'assignee': None, 'case_id': None, 'tags': ['synthetic'],
        'evidence_refs': [], 'acknowledged_at': None, 'resolved_at': None, 'revision': 0,
        'created_at': now, 'updated_at': now, 'owner_id': services.owner_id})

# LLM: 目录请求同样走真实HTTP，不直接调用handler绕过运输层。
# 函数用途: 读取指定上下文类型的快捷动作目录。
def catalog(native, context_kind):
    status, body = call(native, 'copilot.contextual_actions.list', {'context_kind': context_kind})
    assert status == 200, body
    assert body['error'] is None
    return body['data']['items']

# LLM: 成功路径必须真的受理，依赖来源失败不能充当成功断言的替代分支。
# 函数用途: 核验冻结线程别名或Copilot动作的精确202载荷。
def assert_accepted(status, body, *, task_alias=False):
    assert status == 202, body
    assert body['state'] == 'accepted' and body['error'] is None
    expected = ({'task_id', 'request_id', 'operation_id', 'state'} if task_alias else
                {'state', 'operation_id', 'conversation_id', 'task_id', 'thread_id'})
    assert set(body['data']) == expected
    assert body['data']['state'] == 'accepted' and body['data']['task_id']

# LLM: 流入口沿用既有 NDJSON 路由，只按线程过滤，不新增执行器。
# 函数用途: 打开一次真实业务事件流并返回连接与响应。
def open_stream(native, request, *, user='business-alice', timeout=20):
    server = native[0] if isinstance(native, tuple) else native
    connection = http.client.HTTPConnection('127.0.0.1', server.server.server_address[1], timeout=timeout)
    connection.request('POST', '/client/business/stream', json.dumps(request).encode(),
        {'X-Gateway-Token': server._local_client_credential, 'X-User-Id': user,
         'X-Channel': 'feishu', 'Content-Type': 'application/json'})
    return connection, connection.getresponse()

# LLM: 归档态默认排除；显式 archived 与空间筛选都必须读真实记录。
# 函数用途: 验证示例参数下的会话列表筛选。
def test_list_example_params_filters_archived(v13_native):  # noqa: F811
    space = new_space(v13_native, '空间-' + uuid4().hex[:8])
    other_space = new_space(v13_native, '空间-' + uuid4().hex[:8])
    archived = conversation(v13_native, space, '待归档会话')
    kept = conversation(v13_native, space, '保留会话')
    foreign = conversation(v13_native, other_space, '另一空间会话')
    status, body = call(v13_native, 'conversations.archive', {'conversation_id': archived['conversation_id']},
        idempotency_key=key('archive'), expected_revision=archived['revision'])
    assert status == 200, body

    status, listed = call(v13_native, 'conversations.list', {'space_id': space, 'limit': 20})
    assert status == 200, listed
    assert listed['state'] == 'completed' and listed['error'] is None
    assert [row['conversation_id'] for row in listed['data']['items']] == [kept['conversation_id']]
    assert listed['data']['coverage'] in {'complete', 'partial', 'unknown'}
    assert listed['data']['next_cursor'] is None

    status, hidden = call(v13_native, 'conversations.list', {'space_id': space, 'archived': True})
    assert status == 200, hidden
    assert [row['conversation_id'] for row in hidden['data']['items']] == [archived['conversation_id']]
    assert hidden['data']['items'][0]['archived'] is True

    status, others = call(v13_native, 'conversations.list', {'space_id': other_space})
    assert status == 200, others
    assert [row['conversation_id'] for row in others['data']['items']] == [foreign['conversation_id']]

    status, filtered = call(v13_native, 'conversations.list', {'space_id': space, 'query': '保留'})
    assert status == 200, filtered
    assert [row['conversation_id'] for row in filtered['data']['items']] == [kept['conversation_id']]

    status, stranger = call(v13_native, 'conversations.list', {'space_id': space}, user='business-bob')
    assert status == 200 and stranger['data']['items'] == [], stranger

# LLM: 新建会话必须复用冻结后台身份，会话ID就是线程ID，可被三个线程别名接受。
# 函数用途: 验证新建会话与 tasks.submit/messages.list/events.list 的 thread_id 别名。
def test_create_then_thread_id_aliases_work(v13_native):  # noqa: F811
    space = new_space(v13_native, '空间-' + uuid4().hex[:8])
    reuse_key = key('create')
    alert_id = seed_alert(v13_native, space)
    params = {'space_id': space, 'title': '解释会话',
        'context_ref': {'kind': 'alert', 'id': alert_id}}
    status, created = call(v13_native, 'conversations.create', params, idempotency_key=reuse_key)
    assert status == 200, created
    row = created['data']['conversation']
    assert row['conversation_id'] == row['thread_id'] == row['agent_id']
    assert row['space_id'] == space and row['archived'] is False and row['revision'] == 0
    assert row['context_ref'] == {'kind': 'alert', 'id': alert_id}

    status, replay = call(v13_native, 'conversations.create', params, idempotency_key=reuse_key)
    assert status == 200 and replay['data']['conversation'] == row
    status, listed = call(v13_native, 'conversations.list', {'space_id': space})
    assert status == 200 and len(listed['data']['items']) == 1, listed

    status, native_agent = call(v13_native, 'background_agents.get', {'agent_id': row['agent_id']})
    assert status == 200, native_agent
    assert native_agent['data']['agent']['thread_id'] == row['thread_id']

    status, messages = call(v13_native, 'messages.list', {'thread_id': row['thread_id']})
    assert status == 200, messages
    assert set(messages['data']) == {'items', 'next_cursor', 'coverage', 'history_boundary'}

    status, events = call(v13_native, 'events.list', {'thread_id': row['thread_id']})
    assert status == 200, events
    assert set(events['data']) == {'events', 'next_cursor', 'coverage', 'reset_required'}

    status, submitted = call(v13_native, 'tasks.submit',
        {'thread_id': row['thread_id'], 'space_id': space, 'instruction': '单步解释该告警'},
        idempotency_key=key('submit'))
    assert_accepted(status, submitted, task_alias=True)

# LLM: 详情恢复只读原生消息页，缺失与跨owner一致返回404。
# 函数用途: 验证会话详情与消息分页来自同一线程。
def test_get_restores_messages_page(v13_native):  # noqa: F811
    space = new_space(v13_native, '空间-' + uuid4().hex[:8])
    row = conversation(v13_native, space, '详情会话')
    status, detail = call(v13_native, 'conversations.get',
        {'conversation_id': row['conversation_id'], 'limit': 10})
    assert status == 200, detail
    assert detail['state'] == 'completed' and detail['error'] is None
    assert detail['data']['conversation'] == row
    status, native_page = call(v13_native, 'messages.list', {'thread_id': row['thread_id'], 'limit': 10})
    assert status == 200, native_page
    assert detail['data']['messages'] == native_page['data']['items']
    assert detail['data']['coverage'] == native_page['data']['coverage']
    for message in detail['data']['messages']:
        assert message['thread_id'] == row['thread_id']

    status, missing = call(v13_native, 'conversations.get', {'conversation_id': 'conversation-absent'})
    assert status == 404 and missing['error']['code'] == 'RESOURCE_NOT_FOUND'
    status, foreign = call(v13_native, 'conversations.get',
        {'conversation_id': row['conversation_id']}, user='business-bob')
    assert status == 404 and foreign['error']['code'] == 'RESOURCE_NOT_FOUND'

# LLM: 归档走记录级修订，过期修订与同键异参都必须拒绝。
# 函数用途: 验证归档、恢复、修订CAS和幂等冲突。
def test_archive_hides_from_default_list_and_unarchive(v13_native):  # noqa: F811
    space = new_space(v13_native, '空间-' + uuid4().hex[:8])
    row = conversation(v13_native, space, '归档目标')
    stale = call(v13_native, 'conversations.archive', {'conversation_id': row['conversation_id']},
        idempotency_key=key('stale'), expected_revision=row['revision'] + 5)
    assert stale[0] == 409 and stale[1]['error']['code'] == 'REVISION_CONFLICT'

    archive_key = key('archive')
    status, archived = call(v13_native, 'conversations.archive',
        {'conversation_id': row['conversation_id']}, idempotency_key=archive_key,
        expected_revision=row['revision'])
    assert status == 200, archived
    view = archived['data']['conversation']
    assert view['archived'] is True and view['revision'] == row['revision'] + 1

    status, listed = call(v13_native, 'conversations.list', {'space_id': space})
    assert status == 200 and listed['data']['items'] == []

    status, conflict = call(v13_native, 'conversations.archive',
        {'conversation_id': row['conversation_id'], 'archived': False},
        idempotency_key=archive_key, expected_revision=view['revision'])
    assert status == 409 and conflict['error']['code'] == 'IDEMPOTENCY_CONFLICT'

    status, restored = call(v13_native, 'conversations.archive',
        {'conversation_id': row['conversation_id'], 'archived': False},
        idempotency_key=key('restore'), expected_revision=view['revision'])
    assert status == 200 and restored['data']['conversation']['archived'] is False, restored
    status, visible = call(v13_native, 'conversations.list', {'space_id': space})
    assert [item['conversation_id'] for item in visible['data']['items']] == [row['conversation_id']]

    status, foreign = call(v13_native, 'conversations.archive', {'conversation_id': row['conversation_id']},
        idempotency_key=key('foreign'), expected_revision=restored['data']['conversation']['revision'],
        user='business-bob')
    assert status == 404 and foreign['error']['code'] == 'RESOURCE_NOT_FOUND'

# LLM: 解释只派发一次并只写会话元数据；进度复用既有 NDJSON 线程过滤流。
# 函数用途: 验证告警解释的受理、幂等防重派与事件流过滤。
def test_explain_streams_via_business_stream_thread_filter(v13_native):  # noqa: F811
    space = new_space(v13_native, '空间-' + uuid4().hex[:8])
    alert_id = seed_alert(v13_native, space)
    explain_key = key('explain')
    params = {'context_ref': {'kind': 'alert', 'id': alert_id}, 'space_id': space,
        'question': '为什么判定为高危?'}
    status, body = call(v13_native, 'copilot.explain', params, idempotency_key=explain_key)
    assert_accepted(status, body)

    status, replay = call(v13_native, 'copilot.explain', params, idempotency_key=explain_key)
    assert status == 202 and replay['data'] == body['data'], replay
    status, task = call(v13_native, 'tasks.get', {'task_id': body['data']['task_id']})
    assert status == 200, task

    status, listed = call(v13_native, 'conversations.list', {'space_id': space})
    assert status == 200 and len(listed['data']['items']) == 1, listed
    created = listed['data']['items'][0]
    assert created['context_ref'] == {'kind': 'alert', 'id': alert_id}
    assert created['title'] == '告警会话 ' + alert_id

    connection, response = open_stream(v13_native, {'request_id': 'explain-stream',
        'filter': {'agent_id': created['thread_id']}})
    try:
        assert response.status == 200, response.read()
        assert response.getheader('Content-Type').startswith('application/x-ndjson')
        snapshot = json.loads(response.readline())
        assert snapshot['stream_item'] == 'snapshot'
    finally:
        response.close()
        connection.close()

# LLM: 未知或脚本动作都不能经 copilot.contextual_action 派发；AI动作只受理一次。
# 函数用途: 验证快捷动作拒绝规则与真实流式受理。
def test_contextual_action_unknown_action_rejected_and_stream(v13_native):  # noqa: F811
    space = new_space(v13_native, '空间-' + uuid4().hex[:8])
    alert_id = seed_alert(v13_native, space)
    ref = {'kind': 'alert', 'id': alert_id}
    status, unknown = call(v13_native, 'copilot.contextual_action',
        {'action': 'delete_everything', 'context_ref': ref, 'space_id': space},
        idempotency_key=key('unknown'))
    assert status == 400 and unknown['error']['code'] == 'INVALID_SCHEMA'

    status, missing = call(v13_native, 'copilot.contextual_action',
        {'action': 'summarize', 'context_ref': {'kind': 'alert', 'id': 'alert-absent'}, 'space_id': space},
        idempotency_key=key('missing'))
    assert status == 404 and missing['error']['code'] == 'RESOURCE_NOT_FOUND'

    action_key = key('contextual')
    params = {'action': 'summarize', 'context_ref': ref, 'space_id': space, 'instruction': '只列三条'}
    status, body = call(v13_native, 'copilot.contextual_action', params, idempotency_key=action_key)
    assert_accepted(status, body)
    status, replay = call(v13_native, 'copilot.contextual_action', params, idempotency_key=action_key)
    assert status == 202 and replay['data'] == body['data']

    status, listed = call(v13_native, 'conversations.list', {'space_id': space})
    assert status == 200 and len(listed['data']['items']) == 1, listed
    created = listed['data']['items'][0]
    assert created['title'] == '事实摘要'

    connection, response = open_stream(v13_native, {'request_id': 'contextual-stream',
        'filter': {'agent_id': created['thread_id']}})
    try:
        assert response.status == 200, response.read()
        snapshot = json.loads(response.readline())
        assert snapshot['stream_item'] == 'snapshot'
    finally:
        response.close()
        connection.close()

# LLM: 静态AI目录始终可读；脚本项只来自声明匹配的元数据，不执行代码。
# 函数用途: 验证快捷动作目录的AI与脚本组成。
def test_contextual_actions_catalog_ai_and_scripts(v13_native):  # noqa: F811
    items = catalog(v13_native, 'alert')
    ai_actions = [item for item in items if item['kind'] == 'ai']
    assert [item['action'] for item in ai_actions] == sorted(
        ['explain', 'summarize', 'next_steps', 'related_alerts', 'draft_report'])
    assert all(item['script_id'] is None for item in ai_actions)
    assert items == sorted(items, key=lambda item: item['action'])

    from agent_py_agent.agent.assets.descriptor import AssetDescriptor
    from agent_py_agent.agent.assets.script_store import ScriptStore

    _, services = owner_store(v13_native)
    ScriptStore(services.owner_home).save_script(AssetDescriptor(id='alert_helper', kind='script',
        name='告警分流助手', summary='按告警上下文给出处置清单',
        metadata={'contexts': ['alert']}), 'print("synthetic")', entrypoint='main.py')

    with_script = catalog(v13_native, 'alert')
    scripts = [item for item in with_script if item['kind'] == 'script']
    assert [item['script_id'] for item in scripts] == ['alert_helper']
    assert scripts[0]['action'].startswith('script_')
    assert catalog(v13_native, 'rule') == sorted(
        [item for item in with_script if item['kind'] == 'ai'], key=lambda item: item['action'])

    status, invalid = call(v13_native, 'copilot.contextual_actions.list', {'context_kind': 'host'})
    assert status == 400 and invalid['error']['code'] == 'INVALID_SCHEMA'

# LLM: 读动作 200、异步动作 202、未知动作 400，错误data为空且error非空。
# 函数用途: 验证本片动作的统一信封。
def test_envelopes_w11_conversations(v13_native):  # noqa: F811
    space = new_space(v13_native, '空间-' + uuid4().hex[:8])
    standard = {'request_id', 'operation_id', 'state', 'data', 'error', 'allowed_actions', 'warnings',
        'effect_outcome'}

    for action, params in (('conversations.list', {}),
            ('copilot.contextual_actions.list', {'context_kind': 'case'})):
        status, body = call(v13_native, action, params)
        assert status == 200, body
        assert set(body) == standard
        assert body['state'] == 'completed' and body['error'] is None

    status, created = call(v13_native, 'conversations.create', {'space_id': space},
        idempotency_key=key('envelope'))
    assert status == 200 and set(created) == standard and created['error'] is None
    assert created['data']['conversation']['title'] == 'Copilot 会话'

    alert_id = seed_alert(v13_native, space)
    status, accepted = call(v13_native, 'copilot.explain', {'context_ref': {'kind': 'alert', 'id': alert_id},
        'space_id': space, 'conversation_id': created['data']['conversation']['conversation_id']},
        idempotency_key=key('envelope-explain'))
    assert set(accepted) == standard
    assert_accepted(status, accepted)

    status, unsupported = call(v13_native, 'conversations.unknown', {})
    assert status == 400 and unsupported['error']['code'] == 'UNSUPPORTED_ACTION'
    assert unsupported['state'] == 'failed' and unsupported['data'] == {}

    status, refused = call(v13_native, 'copilot.explain', {'context_ref': {'kind': 'alert', 'id': alert_id},
        'space_id': space}, idempotency_key='short')
    assert status == 400 and refused['error']['code'] == 'INVALID_SCHEMA'
    assert refused['data'] == {}

# LLM: 受理结构只由 DomainResult(202) 表达；同键重放必须复用已提交回执，不再派发。
# 函数用途: 用真实Store与已签发上下文核验派发回执与防二次派发。
def test_dispatch_returns_domain_result_202_and_never_dispatches_twice(tmp_path, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    from agent_py_agent.agent.business_spaces import v13_w11_conversations as module
    from agent_py_agent.agent.business_spaces.v13_store import V13Store
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.gateway_parts import business_api as api
    from agent_py_agent.agent.gateway_parts.business_api_v13 import V13Call
    from agent_py_agent.agent.plugin_host.contracts import DomainResult
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_resolver import (
        home_paths_with_owner,
        resolve_owner_home,
    )

    root = tmp_path / 'dispatch-home'
    root.mkdir()
    paths = home_paths_with_owner(home_paths(root), resolve_owner_home(root))
    host = SimpleNamespace(home_paths=paths, config=SimpleNamespace(business_spaces_enabled=True),
        conversation_store=ConversationStore(paths.owner_home_dir / 'conversations'))
    store = V13Store(paths.owner_home_dir, owner_id=paths.owner_id)
    now = '2026-11-02T00:00:00+00:00'
    row = {'conversation_id': 'thread-one', 'thread_id': 'thread-one', 'agent_id': 'thread-one',
        'space_id': 'space-one', 'title': '派发会话', 'archived': False, 'context_ref': None,
        'last_message_at': now, 'revision': 0, 'created_at': now, 'updated_at': now,
        'owner_id': paths.owner_id}

    # LLM: 会话记录经原集合事务种入，不从文件手写。
    # 函数用途: 写入待派发的会话记录。
    def seed(rows):
        rows['thread-one'] = row
        return {'count': 1}

    store.transaction('conversations', seed, idempotency_key='seed-dispatch', params_digest='seed-dispatch')
    # 独立派发测试也准备真实上下文，避免以缺失资源绕过记录级末核。
    store.transaction('soc_alerts', lambda rows: rows.update({'alert-one': {
        'alert_id': 'alert-one', 'space_id': 'space-one', 'owner_id': paths.owner_id,
        'title': '合成告警', 'severity': 'high', 'status': 'new'}}) or {'seeded': 1},
        idempotency_key='seed-context-dispatch', params_digest='seed-context-dispatch')
    context = api.bind_business_context(host, authorize=lambda action, facts: True)
    services = api.BusinessServices(host)
    request = V13Call('copilot.explain', {'context_ref': {'kind': 'alert', 'id': 'alert-one'}},
        'dispatch-request', paths.owner_id, 'host', context, services, Path(paths.owner_home_dir),
        idempotency_key='explain-dispatch-key-01')
    dispatched = []

    # LLM: 只替换跨分派发端口，会话集合、回执与CAS仍走真实Store。
    # 函数用途: 记录派发参数并返回冻结 tasks.assign 形状。
    def frozen(call, action, params, **options):
        assert action == 'tasks.assign' and params['agent_id'] == 'thread-one'
        dispatched.append(params)
        return {'assignment_ref': 'task-nine', 'task_id': 'task-nine', 'receipt_state': 'accepted'}

    monkeypatch.setattr(module, 'v13_call_frozen', frozen)
    expected = {'state': 'accepted', 'operation_id': 'task-nine', 'conversation_id': 'thread-one',
        'task_id': 'task-nine', 'thread_id': 'thread-one'}
    first = module._dispatch(request, space_id='space-one', conversation=row, instruction='解释')
    assert isinstance(first, DomainResult) and first.http_status == 202
    assert dict(first.data) == expected
    replay = module._dispatch(request, space_id='space-one', conversation=row, instruction='解释')
    assert dict(replay.data) == expected
    assert len(dispatched) == 1
    assert store.get('conversations', 'thread-one')['revision'] == 1

# LLM: 只有context_ref也属于合法请求，真实记录的空间必须成为新线程的唯一空间。
# 函数用途: 回归只给上下文的新会话解释与跨空间引用拒绝。
def test_context_only_infers_space_and_cross_space_is_rejected(v13_native):  # noqa: F811
    space = new_space(v13_native, '上下文空间-' + uuid4().hex[:8])
    other = new_space(v13_native, '目标空间-' + uuid4().hex[:8])
    alert = seed_alert(v13_native, space)
    ref = {'kind': 'alert', 'id': alert}
    status, body = call(v13_native, 'copilot.explain', {'context_ref': ref},
        idempotency_key=key('infer-space'))
    assert_accepted(status, body)
    status, rows = call(v13_native, 'conversations.list', {'space_id': space})
    assert status == 200 and len(rows['data']['items']) == 1
    for action, params in (
            ('copilot.explain', {'context_ref': ref, 'space_id': other}),
            ('copilot.contextual_action', {'context_ref': ref, 'space_id': other, 'action': 'summarize'})):
        status, refused = call(v13_native, action, params, idempotency_key=key('cross-space'))
        assert status == 404 and refused['error']['code'] == 'RESOURCE_NOT_FOUND', refused
    status, empty = call(v13_native, 'conversations.list', {'space_id': other})
    assert status == 200 and empty['data']['items'] == []

# LLM: 同键改变上下文/空间要在创建线程之前拒绝，不允许留下孤儿会话。
# 函数用途: 回归分阶段建会话的完整参数幂等准入。
def test_changed_context_key_does_not_create_orphan_conversation(v13_native):  # noqa: F811
    first = new_space(v13_native, '首次-' + uuid4().hex[:8])
    second = new_space(v13_native, '冲突-' + uuid4().hex[:8])
    a, b = seed_alert(v13_native, first), seed_alert(v13_native, second)
    operation = key('whole-request')
    status, accepted = call(v13_native, 'copilot.explain',
        {'space_id': first, 'context_ref': {'kind': 'alert', 'id': a}}, idempotency_key=operation)
    assert_accepted(status, accepted)
    status, refused = call(v13_native, 'copilot.explain',
        {'space_id': second, 'context_ref': {'kind': 'alert', 'id': b}}, idempotency_key=operation)
    assert status == 409 and refused['error']['code'] == 'IDEMPOTENCY_CONFLICT', refused
    status, rows = call(v13_native, 'conversations.list', {'space_id': second})
    assert status == 200 and rows['data']['items'] == [], rows
    status, agents = call(v13_native, 'background_agents.list', {'space_id': second})
    assert status == 200 and agents['data']['items'] == [], agents

# LLM: query必须先过滤再分页，旧cursor不能用于另一个查询条件。
# 函数用途: 回归小页搜索不会漏掉后面的命中会话。
def test_query_filters_before_paging_and_binds_cursor(v13_native):  # noqa: F811
    space = new_space(v13_native, '查询-' + uuid4().hex[:8])
    for title in ('暂定一', '暂定二', '暂定三'):
        conversation(v13_native, space, title)
    store, _ = owner_store(v13_native)
    identifiers = [row['conversation_id'] for row in store.list('conversations')]

    # LLM: 测试记录键排序固定后才设置标题，消除随机线程ID对回归触发条件的影响。
    # 函数用途: 使第一页原始记录不匹配、后两条匹配。
    def retitle(rows):
        for identifier, title in zip(identifiers, ('不相关', '命中甲', '命中乙'), strict=True):
            rows[identifier]['title'] = title
        return {'changed': len(rows)}

    store.transaction('conversations', retitle, idempotency_key=key('retitle'), params_digest='titles')
    params = {'space_id': space, 'query': '命中', 'limit': 1}
    status, first = call(v13_native, 'conversations.list', params)
    assert status == 200 and [row['title'] for row in first['data']['items']] == ['命中甲'], first
    cursor = first['data']['next_cursor']
    assert cursor
    status, second = call(v13_native, 'conversations.list', {**params, 'cursor': cursor})
    assert status == 200 and [row['title'] for row in second['data']['items']] == ['命中乙'], second
    status, stale = call(v13_native, 'conversations.list', {**params, 'query': '不相关', 'cursor': cursor})
    # plan-w11错误表明确沿冻结_error_code保留503 retryable，而不是另造400。
    assert status == 410 and stale['error']['code'] == 'CURSOR_EXPIRED', stale

# ============================================================================
# Source: agent_py_agent/tests/test_v13_w11_onboarding.py
# W11初始化引导与品牌：真实Gateway HTTP、原生Store与设置来源回归。
# ============================================================================

# 1x1透明PNG头足以让原上传路由识别image/png；不引入外部图片依赖。
_PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 16

# LLM: 只用原Gateway的本地凭据和真实环回连接，不往JSON塞身份字段。
# 函数用途: 发送一笔真实HTTP请求并返回状态、头和正文。
def _request(server, method, path, body=None, *, user='business-alice', headers=None):
    server = server[0] if isinstance(server, tuple) else server
    request_headers = {'X-Gateway-Token': server._local_client_credential, 'X-User-Id': user,
                       'X-Channel': 'feishu', **(headers or {})}
    if body is not None and 'Content-Type' not in request_headers:
        request_headers['Content-Type'] = 'application/json'
    connection = http.client.HTTPConnection('127.0.0.1', server.server.server_address[1], timeout=20)
    try:
        connection.request(method, path, body, request_headers)
        response = connection.getresponse()
        content = response.read()
        return response.status, dict(response.getheaders()), content
    finally:
        connection.close()

# LLM: 断言仍走统一dispatcher的标准信封和动作result schema，不只看HTTP200。
# 函数用途: 发送一笔真实HTTP业务调用并解码信封。
def _call_w11_onboarding(server, action, params=None, *, user='business-alice', key=None, revision=None, headers=None):
    request = {'action': action, 'request_id': 'w11-' + action, 'params': params or {}}
    if key is not None:
        request['idempotency_key'] = key
    if revision is not None:
        request['expected_revision'] = revision
    status, _, raw = _request(server, 'POST', '/client/business', json.dumps(request).encode(),
                              user=user, headers=headers)
    return status, json.loads(raw)

# LLM: 完成结果必须在发送前由当前动作schema验证，错误行单独断言状态与错误码。
# 函数用途: 取出已校验的completed数据。
def _completed_w11_onboarding(server, action, params=None, **options):
    status, body = _call_w11_onboarding(server, action, params, **options)
    assert status == 200, body
    assert body['state'] == 'completed' and body['error'] is None
    return v13_action_spec(action).validate_result(body['data'])

# LLM: 失败必须同时核HTTP状态和稳定错误码，不能把任意4xx当通过。
# 函数用途: 断言指定的失败信封。
def _failed_w11_onboarding(server, action, params, code, status, **options):
    actual_status, body = _call_w11_onboarding(server, action, params, **options)
    assert actual_status == status, body
    assert body['state'] == 'failed' and body['error']['code'] == code and body['data'] == {}
    return body

# LLM: 样例写入后从真实owner目录定位Store，路径来自本次HTTP实际落盘而非推断。
# 函数用途: 返回当前测试owner的home与owner_id。
def _owner_home(agent):
    marker = next(Path(agent.home_paths.root).rglob('data/business_v13/onboarding.json'))
    state = json.loads(marker.read_text(encoding='utf-8'))
    owner_id = next(iter(state['records'].values()))['owner_id']
    return marker.parents[2], owner_id

# LLM: 统一用生产身份解析重建同一owner的home_paths，模型目录摘要才会与实时调用一致。
# 函数用途: 从真实owner目录反推可复用的home_paths。
def _owned_paths(agent, owner_home):
    relative = Path(owner_home).relative_to(Path(agent.home_paths.root)).parts
    if (len(relative) == 5 and relative[0] == 'owners' and relative[1] == 'providers'
            and relative[3] == 'users'):
        identity = OwnerIdentity.provider_user(relative[2], relative[4])
    else:
        identity = OwnerIdentity.local_main()
    return home_paths_with_owner(agent.home_paths, resolve_owner_home(agent.home_paths.root, identity))

# LLM: 生产宿主带会话库时W14产物观察端口才就绪；测试用原工厂补齐同一接线，不伪造响应。
# 函数用途: 为真实Gateway启用原会话库与投影。
def _enable_projection(server):
    from agent_py_agent.agent.gateway_parts.owner_conversation_store import owner_conversation_store
    agent = server[1]
    if getattr(agent, 'conversation_store', None) is None:
        agent.conversation_store = owner_conversation_store(agent, agent.home_paths, initialize=True)

# LLM: 记录读取使用原Store公开list，不手写第二份持久格式。
# 函数用途: 读取owner集合中的规范记录。
def _rows(owner_home, owner_id, collection):
    store = V13Store(owner_home, owner_id=owner_id, subject_id='reviewer')
    return store.list(collection)

# LLM: 品牌logo必须先经原产物路由上传，这里构造真实multipart请求体。
# 函数用途: 上传一个测试文件并返回ArtifactView。
def _upload(server, space_id, data, *, name='logo.png', key='w11-logo-upload-key'):
    boundary = 'w11boundary'
    parts = [
        ('--' + boundary + '\r\nContent-Disposition: form-data; name="space_id"\r\n\r\n'
         + space_id + '\r\n').encode(),
        ('--' + boundary + '\r\nContent-Disposition: form-data; name="idempotency_key"\r\n\r\n'
         + key + '\r\n').encode(),
        ('--' + boundary + '\r\nContent-Disposition: form-data; name="file"; filename="' + name
         + '"\r\nContent-Type: application/octet-stream\r\n\r\n').encode() + data + b'\r\n',
        ('--' + boundary + '--\r\n').encode(),
    ]
    status, _, raw = _request(server, 'POST', '/client/business/artifacts', b''.join(parts),
                              headers={'Content-Type': 'multipart/form-data; boundary=' + boundary})
    assert status == 201, raw
    return json.loads(raw)

# LLM: 空owner的真实事实全部来自来源读取；创建空间后同一入口必须反映变化。
# 函数用途: 验证示例空参数的状态清单与空间步骤。
def test_status_example_empty_params_real_facts(v13_native):  # noqa: F811
    fresh = _completed_w11_onboarding(v13_native, 'onboarding.status', {})
    assert [item['step'] for item in fresh['steps']] == [
        'llm_configured', 'connector_added', 'space_created',
        'sample_data_loaded', 'first_rule_enabled', 'branding_set']
    assert fresh['completed_steps'] == []
    assert fresh['has_connectors'] is False and fresh['has_llm'] is False
    assert fresh['sample_data_loaded'] is False
    assert fresh['coverage'] == 'complete'
    space_id = v13_space(v13_native)
    after = _completed_w11_onboarding(v13_native, 'onboarding.status', {})
    assert after['completed_steps'] == ['space_created']
    assert after['steps'][2] == {'step': 'space_created', 'done': True}
    assert space_id

# LLM: 模型服务商和连接器必须由原设置目录与数据源Store决定，而不是写死的布尔值。
# 函数用途: 验证状态读取真实模型provider与DatasourceStore，且按owner隔离。
def test_status_reads_real_model_and_connector_sources(v13_native, tmp_path):  # noqa: F811
    from agent_py_agent.agent.assets.datasource_store import DatasourceStore
    from agent_py_agent.agent.plugin_host.model_settings_v2_handlers import ModelSettingsV2Handlers

    v13_space(v13_native)
    base = v13_native[1].home_paths
    seeded_paths = home_paths_with_owner(
        base,
        resolve_owner_home(base.root, OwnerIdentity.provider_user('feishu', 'business-alice')),
    )
    V13Store(seeded_paths.owner_home_dir, owner_id=seeded_paths.owner_id, subject_id='reviewer').transaction(
        'onboarding',
        lambda records: records.update({
            seeded_paths.owner_id: {
                'owner_id': seeded_paths.owner_id,
                'sample_batch_id': 'sample-batch-seeded',
                'revision': 1,
            }
        }) or {'seeded': 1},
        idempotency_key='w11-status-seed-key',
        params_digest='w11-status-seed',
    )
    owner_home, owner_id = _owner_home(v13_native[1])
    paths = _owned_paths(v13_native[1], owner_home)
    assert paths.owner_id == owner_id and paths.owner_home_dir == owner_home
    ModelSettingsV2Handlers(paths).save_provider({
        'provider_id': 'example', 'display_name': 'Example', 'api_base': 'https://example.invalid/v1',
        'api_key': 'TEST_KEY_NOT_A_CREDENTIAL', 'models': [{
            'model_name': 'agent-model', 'model_backend': 'openai_compatible',
            'model_context_window_tokens': 32768}]})
    log = tmp_path / 'sample.log'
    log.write_text('sample line\n', encoding='utf-8')
    DatasourceStore(owner_home).save_datasource({
        'datasource_id': 'sample-logs', 'kind': 'file', 'title': 'sample-logs',
        'options': {'path': str(log), 'device_name': 'fw-01',
                    'log_source_name': 'sample.log', 'system': 'logs'}})
    data = _completed_w11_onboarding(v13_native, 'onboarding.status', {})
    assert data['has_llm'] is True and data['has_connectors'] is True
    assert {'llm_configured', 'connector_added'} <= set(data['completed_steps'])
    other = _completed_w11_onboarding(v13_native, 'onboarding.status', {}, user='business-bob')
    assert other['has_llm'] is False and other['has_connectors'] is False
    assert other['completed_steps'] == []
    assert base.owner_id

# LLM: 品牌初始为空、CAS按记录版本、logo只用冻结artifacts.get核过owner和图片类型。
# 函数用途: 验证品牌读取、更新、CAS、幂等与原生logo内容地址。
def test_branding_get_update_logo_artifact(v13_native):  # noqa: F811
    _enable_projection(v13_native)
    empty = _completed_w11_onboarding(v13_native, 'branding.get', {})
    assert empty == {'branding': {'configured': False, 'product_name': None, 'primary_color': None,
                                  'logo': None, 'logo_url': None, 'revision': 0, 'updated_at': None}}
    space_id = v13_space(v13_native)
    image = _upload(v13_native, space_id, _PNG)
    probe = _call_w11_onboarding(v13_native, 'artifacts.get',
                  {'artifact_id': image['artifact_id'], 'version_ref': image['version_ref']})
    assert probe[0] == 200 and probe[1]['data']['available'] is True, probe
    text = _upload(v13_native, space_id, b'not an image', name='note.txt',
                   key='w11-text-upload-key')
    _failed_w11_onboarding(v13_native, 'branding.update', {'logo_artifact_id': text['artifact_id'],
                                            'logo_version_ref': text['version_ref']},
            'INVALID_SCHEMA', 400, key='w11-branding-text-key', revision=0)
    # 此夹具没有完整旧run登记覆盖证明；未知ID保持可核验的503，不能冒充已证不存在。
    missing = _failed_w11_onboarding(v13_native, 'branding.update', {'logo_artifact_id': 'artifact-missing',
                                                     'logo_version_ref': 'version-missing'},
                      'HOST_NOT_READY', 503, key='w11-branding-missing-key', revision=0)
    details = {item['key']: item['value'] for item in missing['error']['details']}
    assert details['reason'] == 'ARTIFACT_REGISTRY_DISCOVERY_INCOMPLETE'
    assert details['next_action'] and missing['effect_outcome'] == 'not_started'
    assert _completed_w11_onboarding(v13_native, 'branding.get', {}) == empty
    params = {'product_name': 'AiSOC 租户', 'primary_color': '#1a2b3c',
              'logo_artifact_id': image['artifact_id'], 'logo_version_ref': image['version_ref']}
    brand = _completed_w11_onboarding(v13_native, 'branding.update', params,
                       key='w11-branding-first-key', revision=0)['branding']
    assert brand['configured'] is True and brand['revision'] == 1
    assert brand['product_name'] == 'AiSOC 租户' and brand['primary_color'] == '#1a2b3c'
    assert brand['logo'] == {key: image[key] for key in
                             ('artifact_id', 'version_ref', 'name', 'mime_type', 'size_bytes', 'content_sha256')}
    assert brand['logo_url'] == ('/client/business/artifacts/' + image['artifact_id']
                                 + '/content?version_ref=' + image['version_ref'])
    assert _completed_w11_onboarding(v13_native, 'branding.get', {})['branding'] == brand
    replay = _completed_w11_onboarding(v13_native, 'branding.update', params,
                        key='w11-branding-first-key', revision=0)['branding']
    assert replay == brand
    _failed_w11_onboarding(v13_native, 'branding.update', {'product_name': '过期写入'},
            'REVISION_CONFLICT', 409, key='w11-branding-stale-key', revision=0)
    second = _completed_w11_onboarding(v13_native, 'branding.update', {'product_name': 'AiSOC 新名称'},
                        key='w11-branding-second-key', revision=1)['branding']
    assert second['revision'] == 2 and second['product_name'] == 'AiSOC 新名称'
    assert second['logo'] == brand['logo'] and second['primary_color'] == '#1a2b3c'

    status, headers, content = _request(v13_native, 'GET', brand['logo_url'])
    assert status == 200 and headers['Content-Type'] == 'image/png' and content == _PNG
    foreign_status, _, foreign_content = _request(v13_native, 'GET', brand['logo_url'], user='business-bob')
    assert foreign_status == 503 and _PNG not in foreign_content
    foreign_error = json.loads(foreign_content)['error']
    assert foreign_error['code'] == 'HOST_NOT_READY'
    assert foreign_error['message'] == 'ARTIFACT_REGISTRY_DISCOVERY_INCOMPLETE'
    assert foreign_error['outcome'] == 'not_started'
    other = _completed_w11_onboarding(v13_native, 'branding.get', {}, user='business-bob')
    assert other['branding']['configured'] is False

# LLM: 四个动作都经同一原生入口返回闭合信封；非法信封和未知动作按冻结错误码拒绝。
# 函数用途: 验证成功信封字段、必需幂等键/版本与INVALID_SCHEMA/UNSUPPORTED_ACTION。
def test_envelopes_w11_onboarding(v13_native):  # noqa: F811
    space_id = v13_space(v13_native)
    status, body = _call_w11_onboarding(v13_native, 'onboarding.status', {})
    assert status == 200
    assert set(body) == {'request_id', 'operation_id', 'state', 'data', 'error',
                         'allowed_actions', 'warnings', 'effect_outcome'}
    assert body['effect_outcome'] == 'none'
    for action, params, options in (
            ('branding.get', {}, {}),
            ('branding.update', {'product_name': '信封品牌'}, {'key': 'w11-envelope-brand-key', 'revision': 0})):
        status, body = _call_w11_onboarding(v13_native, action, params, **options)
        assert status == 200 and body['state'] == 'completed' and body['error'] is None
        assert set(body) == {'request_id', 'operation_id', 'state', 'data', 'error',
                             'allowed_actions', 'warnings', 'effect_outcome'}
        v13_action_spec(action).validate_result(body['data'])
    _failed_w11_onboarding(v13_native, 'onboarding.status', {'unexpected': 1}, 'INVALID_SCHEMA', 400)
    _failed_w11_onboarding(v13_native, 'branding.update', {'product_name': '缺键'}, 'INVALID_SCHEMA', 400)
    _failed_w11_onboarding(v13_native, 'branding.update', {'product_name': '缺版本'},
            'INVALID_SCHEMA', 400, key='w11-envelope-no-revision')
    _failed_w11_onboarding(v13_native, 'onboarding.unknown', {}, 'UNSUPPORTED_ACTION', 400)
    assert space_id

# ============================================================================
# Source: agent_py_agent/tests/test_v13_w11_packages.py
# W11 能力包与合规治理：真实 Gateway HTTP、原安装表与 v13 证据集合的端到端回归。
# ============================================================================

_ENVELOPE_KEYS = {'request_id', 'operation_id', 'state', 'data', 'error', 'allowed_actions',
                  'warnings', 'effect_outcome'}
_PACKAGE_ID = 'soc-core-detections-pack'

# LLM: 合成包只走宿主打包器生成合法字节，不手写 ZIP；语言/内容均为中性测试数据。
# 函数用途: 造一个带规则、流程、脚本、技能与合规声明的能力包。
def _package(package_id, version, *, controls=None):
    contents = {'README.md': b'# synthetic pack\n',
                'rules/ssh-brute.json': b'{"rule": "ssh-brute"}\n',
                'workflows/contain-host.json': b'{"workflow": "contain"}\n',
                'scripts/collect-evidence.sh': b'#!/bin/sh\nexit 0\n',
                'skills/triage/SKILL.md': b'# triage\n'}
    for control_id, assets in (controls or {'CC6.1': [('rule', 'rule-1')]}).items():
        contents['compliance/soc2/' + control_id + '.json'] = json.dumps(
            {'title': 'control ' + control_id, 'family': 'CC6',
             'assets': [{'asset_kind': kind, 'asset_ref': ref} for kind, ref in assets]}).encode()
    declaration = {'plugin_id': package_id, 'version': version, 'summary': 'synthetic package',
        'capability': {'description': 'synthetic detection pack', 'keywords': ['soc'],
                       'entry_document': 'README.md'},
        'files': [{'path': path} for path in contents], 'settings_schema': {}}
    return build_capability_pack(declaration, contents).payload

# LLM: 本地安装直接走 PluginManagement.install_package_bytes，沿用真实 owner 安装表。
# 函数用途: 通过宿主本地包安装端口安装合成能力包。
def _install_local(agent, raw, *, operation_id, expected_revision=0):
    owner = resolve_owner_home(agent.home_paths.root, owner_identity_from_config(agent.config))
    context = plugin_management_context(
        owner,
        agent.home_paths,
        agent.config,
        agent.conversation_store.threads,
        actor_id='w11-test',
        channel='http',
        conversation_id=operation_id,
        is_admin=is_complete_local_admin_owner(agent.home_paths),
    )
    return PluginManagement(context).install_package_bytes(
        raw, operation_id=operation_id, expected_revision=expected_revision
    )

# LLM: 使用隔离真实 Gateway，不绑定远程包源，仅测试本地已安装包查验、卸载与合规。
# 函数用途: 提供启用本地业务网关的测试夹具。
@pytest.fixture
def packages(native_http):
    server, agent = native_http
    return server, agent

# LLM: 调用只经原 HTTP 与宿主凭据，身份不从 JSON 携带；base 通道用于本机管理员宿主。
# 函数用途: 发一笔 /client/business 请求并返回状态与标准信封。
def _call_w11_packages(server, action, params=None, *, key=None, revision=None):
    request = {'action': action, 'request_id': 'w11-' + action, 'params': params or {}}
    if key is not None:
        request['idempotency_key'] = key
    if revision is not None:
        request['expected_revision'] = revision
    status, _headers, raw = native_request((server, None), 'POST', '/client/business', request,
        headers={'X-Channel': 'http'})
    return status, json.loads(raw)

# LLM: 结果凡声明 schema 就按同一规范校验；冻结动作没有 v13 登记时只读标准信封。
# 函数用途: 断言完成信封并返回结果。
def _completed_w11_packages(server, action, params=None, *, key=None, revision=None):
    status, body = _call_w11_packages(server, action, params, key=key, revision=revision)
    assert status == 200, json.dumps(body, ensure_ascii=False)
    assert body['state'] == 'completed' and body['error'] is None, json.dumps(body, ensure_ascii=False)
    specs = v13_action_specs()
    if action in specs:
        return specs[action].validate_result(body['data'])
    return body['data']

# LLM: 失败响应只在同一标准信封里报告，data 为空对象；不据 HTTP 文本猜原因。
# 函数用途: 断言失败信封的准确状态与错误码。
def _failed_w11_packages(server, action, params, code, status_code, *, key=None, revision=None):
    status, body = _call_w11_packages(server, action, params, key=key, revision=revision)
    assert status == status_code, json.dumps(body, ensure_ascii=False)
    assert body['state'] == 'failed' and body['data'] == {}, json.dumps(body, ensure_ascii=False)
    assert body['error']['code'] == code, json.dumps(body, ensure_ascii=False)
    return body

# LLM: 空间由真实 W1 入口创建，不把虚构 ID 塞给安装或导出动作。
# 函数用途: 创建一个本机业务空间。
def _space(server, key):
    data = _completed_w11_packages(server, 'spaces.create', {'name': 'w11 空间'}, key=key)
    return data['space_id'], data['revision']

# LLM: 安装表是产品文件，测试只读原文并检查包是否仍在账上，不手写安装状态。
# 函数用途: 读取当前 owner 的安装表原文。
def _installations_text(agent):
    path = Path(agent.home_paths.owner_home_dir) / 'data' / 'plugins' / 'installations.json'
    assert path.is_file(), path
    return path.read_text(encoding='utf-8')

# LLM: 证据集合写入走宿主 Store，不手写状态文件；记录形状使用冻结集合定义。
# 函数用途: 在 v13 证据集合中种入一条合规证据。
def _seed_evidence(agent, rows):
    store = V13Store(agent.home_paths.owner_home_dir, owner_id=agent.home_paths.owner_id)

    # LLM: 一次事务写入全部合成证据。
    # 函数用途: 合并给定证据记录并返回数量。
    def mutate(current):
        current.update(rows)
        return {'seeded': len(rows)}

    return store.transaction('compliance_evidence', mutate,
        idempotency_key='seed-evidence-' + str(len(rows)), params_digest='seed')

# LLM: 规则事实来自 W7 集合，测试只种入冻结字段形状。
# 函数用途: 种入一条监控规则记录。
def _seed_rule(agent, rule_id, *, enabled):
    store = V13Store(agent.home_paths.owner_home_dir, owner_id=agent.home_paths.owner_id)

    # LLM: 规则只保留测试关心的结构化字段。
    # 函数用途: 写入合成规则。
    def mutate(current):
        current[rule_id] = {'rule_id': rule_id, 'space_id': None, 'name': rule_id,
                            'description': '', 'enabled': enabled, 'severity': 'medium',
                            'query': {'language': 'text', 'text': 'x'}, 'schedule': None,
                            'script_id': None, 'skill_ids': [], 'datasource_ids': [],
                            'mitre_techniques': [], 'tags': [], 'exclusions': [],
                            'origin': 'manual', 'scheduler_job_id': None, 'last_run': None,
                            'revision': 0, 'created_at': '2026-11-02T00:00:00+00:00',
                            'updated_at': '2026-11-02T00:00:00+00:00',
                            'owner_id': agent.home_paths.owner_id}
        return {'seeded': 1}

    return store.transaction('monitor_rules', mutate,
        idempotency_key='seed-rule-' + rule_id, params_digest='seed')

# LLM: 已安装包的内容清单来自原安装表字节的静态检查，不执行任何随包文件；未安装包返回 404。
# 函数用途: 验证 packages.inspect 返回规则/流程/脚本/技能与合规映射。
def test_inspect_lists_rules_workflows_scripts_skills(packages):
    server, agent = packages
    _install_local(agent, _package(_PACKAGE_ID, '1.0.0'), operation_id='w11-inspect-install')
    data = _completed_w11_packages(server, 'packages.inspect', {'package_id': _PACKAGE_ID,
                                                   'version': '1.0.0'})
    assert data['installed'] is True and data['version'] == '1.0.0'
    assert data['manifest']['plugin_id'] == _PACKAGE_ID
    contents = data['contents']
    assert [row['ref'] for row in contents['detection_rules']] == ['rules/ssh-brute.json']
    assert [row['ref'] for row in contents['workflows']] == ['workflows/contain-host.json']
    assert [row['ref'] for row in contents['scripts']] == ['scripts/collect-evidence.sh']
    assert [row['name'] for row in contents['skills']] == ['triage']
    assert contents['compliance_mappings'] == [{'framework': 'soc2', 'control_id': 'CC6.1',
        'asset_kind': 'rule', 'asset_ref': 'rule-1'}]
    _failed_w11_packages(server, 'packages.inspect', {'package_id': 'unknown-pack', 'version': '1.0.0'},
            'RESOURCE_NOT_FOUND', 404)
    _failed_w11_packages(server, 'packages.inspect', {'package_id': _PACKAGE_ID, 'version': '9.9.9'},
            'RESOURCE_NOT_FOUND', 404)

# LLM: 卸载顺序必须是先摘挂载再走原卸载命令，卸载后同包再请求要 404。
# 函数用途: 验证 packages.uninstall 的摘除与删除事实。
def test_uninstall_detaches_then_removes(packages):
    server, agent = packages
    _install_local(agent, _package(_PACKAGE_ID, '1.0.0'), operation_id='w11-uninstall-install')
    data = _completed_w11_packages(server, 'packages.uninstall', {'package_id': _PACKAGE_ID},
                      key='w11-uninstall-remove')
    assert data['uninstalled'] is True and data['detached_spaces'] == []
    assert _PACKAGE_ID not in _installations_text(agent)
    _failed_w11_packages(server, 'packages.uninstall', {'package_id': _PACKAGE_ID},
            'RESOURCE_NOT_FOUND', 404, key='w11-uninstall-again')

# LLM: 控制项目录只来自已安装包声明，状态与证据计数来自证据集合，不返回猜测状态。
# 函数用途: 验证 compliance.status 的控制项、证据计数与未知框架 404。
def test_compliance_status_from_installed_mappings_and_evidence(packages):
    server, agent = packages
    _install_local(agent, _package(_PACKAGE_ID, '1.0.0'), operation_id='w11-status-install')
    _seed_rule(agent, 'rule-1', enabled=True)
    _completed_w11_packages(server, 'compliance.collect', {'framework': 'soc2'}, key='w11-status-collect')
    _seed_evidence(agent, {'evidence-seeded': {'evidence_id': 'evidence-seeded',
        'owner_id': agent.home_paths.owner_id, 'space_id': None, 'framework': 'soc2',
        'control_id': 'CC6.1', 'status': 'unmet', 'facts': {'assets': []},
        'collected_at': '2026-11-01T00:00:00+00:00', 'revision': 0,
        'created_at': '2026-11-01T00:00:00+00:00', 'updated_at': '2026-11-01T00:00:00+00:00'}})
    data = _completed_w11_packages(server, 'compliance.status', {'framework': 'soc2'})
    controls = {row['control_id']: row for row in data['controls']}
    assert controls['CC6.1']['status'] == 'met' and controls['CC6.1']['evidence_count'] == 2
    assert controls['CC6.1']['last_collected_at'] > '2026-11-01T00:00:00+00:00'
    assert controls['CC6.1']['mapped_assets'] == [{'framework': 'soc2', 'control_id': 'CC6.1',
        'asset_kind': 'rule', 'asset_ref': 'rule-1'}]
    assert data['summary'] == {'met': 1, 'partial': 0, 'unmet': 0, 'unknown': 0}
    _failed_w11_packages(server, 'compliance.status', {'framework': 'iso27001'}, 'RESOURCE_NOT_FOUND', 404)

# LLM: 采集是同一框架全部控制项的一次有界快照；同状态再采集不产生新版本，重放返回原回执。
# 函数用途: 验证 compliance.collect 的状态判定、证据落盘与确定性。
def test_compliance_collect_records_evidence_deterministically(packages):
    server, agent = packages
    source_package = _package(_PACKAGE_ID, '1.0.0',
        controls={'CC6.1': [('rule', 'rule-1')], 'CC6.2': [('rule', 'rule-2')],
                  'CC6.3': [('script', 'collect-evidence.sh')]})
    _install_local(agent, source_package, operation_id='w11-collect-install')
    _seed_rule(agent, 'rule-1', enabled=True)
    _seed_rule(agent, 'rule-2', enabled=False)
    first = _completed_w11_packages(server, 'compliance.collect', {'framework': 'soc2'},
                       key='w11-collect-first')
    assert first['collected'] == 3 and first['controls_updated'] == 3
    assert first['coverage'] == 'complete'
    replay = _completed_w11_packages(server, 'compliance.collect', {'framework': 'soc2'},
                        key='w11-collect-first')
    assert replay == first
    again = _completed_w11_packages(server, 'compliance.collect', {'framework': 'soc2'},
                       key='w11-collect-second')
    assert again['controls_updated'] == 0
    store = V13Store(agent.home_paths.owner_home_dir, owner_id=agent.home_paths.owner_id)
    rows = {row['control_id']: row for row in store.list('compliance_evidence')}
    assert rows['CC6.1']['status'] == 'met'
    assert rows['CC6.2']['status'] == 'unmet'
    assert rows['CC6.3']['status'] == 'unknown'

# LLM: 导出必须真的登记到原产物账本并通过摘要核对；三种格式互不冒充。
# 函数用途: 验证 compliance.export 的产物登记与内容摘要。
def test_compliance_export_artifact(packages):
    server, agent = packages
    space_id, _revision = _space(server, 'w11-export-space-key')
    _install_local(agent, _package(_PACKAGE_ID, '1.0.0'), operation_id='w11-export-install')
    _seed_rule(agent, 'rule-1', enabled=True)
    _completed_w11_packages(server, 'compliance.collect', {'framework': 'soc2'}, key='w11-export-collect')
    data = _completed_w11_packages(server, 'compliance.export', {'framework': 'soc2', 'format': 'json',
        'space_id': space_id}, key='w11-export-json-01')
    export = data['export']
    assert export['format'] == 'json' and export['name'].endswith('.json')
    root = Path(agent.home_paths.owner_home_dir) / 'artifacts' / 'business'
    blob = next(path for path in root.iterdir() if path.name.endswith('.blob'))
    payload = blob.read_bytes()
    assert len(payload) == export['size_bytes']
    assert hashlib.sha256(payload).hexdigest() == export['content_sha256']
    assert json.loads(payload)['framework'] == 'soc2'
    csv_export = _completed_w11_packages(server, 'compliance.export', {'framework': 'soc2', 'format': 'csv',
        'space_id': space_id}, key='w11-export-csv-01')['export']
    assert csv_export['format'] == 'csv' and csv_export['content_sha256'] != export['content_sha256']

# LLM: #74-9 只冻结映射，不改原动作名；v13 不能覆盖已有 spaces.packages.*。
# 函数用途: 验证冻结包动作名仍存在且未被扩展覆盖。
def test_frozen_package_names_exist():
    specs = business_action_specs()
    for name in ('spaces.packages.catalog', 'spaces.packages.list', 'spaces.packages.attach',
                 'spaces.packages.detach', 'spaces.packages.set', 'spaces.packages.reorder'):
        assert name in specs, name
    v13_specs = v13_action_specs()
    assert not [name for name in v13_specs if name.startswith('spaces.packages.')]
    for retired in ('packages.index', 'packages.install', 'packages.upgrade'):
        assert retired not in v13_specs
    for kept in ('packages.inspect', 'packages.uninstall'):
        assert kept in v13_specs

# LLM: 200/202 只用于完成或受理，error 必须为空；信封字段集合固定，不许夹带私有路径。
# 函数用途: 验证能力包与合规动作的标准信封形状。
def test_envelopes_w11_packages(packages):
    server, agent = packages
    _install_local(agent, _package(_PACKAGE_ID, '1.0.0'), operation_id='w11-envelope-install')
    for action, params in (('packages.inspect', {'package_id': _PACKAGE_ID}),
                           ('compliance.status', {'framework': 'soc2'})):
        status, body = _call_w11_packages(server, action, params)
        assert status == 200, json.dumps(body, ensure_ascii=False)
        assert set(body) == _ENVELOPE_KEYS, json.dumps(body, ensure_ascii=False)
        assert body['state'] == 'completed' and body['error'] is None
        assert body['effect_outcome'] == 'none'
        assert isinstance(body['allowed_actions'], list)

# LLM: 重放必须先消费耐久回执，不可在卸载后 404 处分叉。
# 函数用途: 验证卸载的原键原参数成功重放。
def test_package_uninstall_replay_original_receipts(packages):
    server, agent = packages
    _install_local(agent, _package(_PACKAGE_ID, '1.0.0'), operation_id='w11-replay-install')
    removed = _completed_w11_packages(server, 'packages.uninstall', {'package_id': _PACKAGE_ID},
                         key='w11-replay-uninstall')
    assert _completed_w11_packages(server, 'packages.uninstall', {'package_id': _PACKAGE_ID},
                      key='w11-replay-uninstall') == removed
