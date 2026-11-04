"""V13 Store 的耐久主链路、权限边界与故障恢复。"""
from __future__ import annotations

import base64
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.business_spaces import v13_store as module
from agent_py_agent.agent.business_spaces.v13_store import V13Store, v13_store_for
from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.agent.gateway_parts.business_api import bind_business_context
from agent_py_agent.agent.gateway_parts.business_api_v13 import V13Call
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    resolve_owner_home,
)


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
    expected = hashlib.sha256(module._encode(['host', 'job.create', 'request-00000001'])).hexdigest()
    receipts = first._rows('async_jobs')
    receipt = receipts.get('receipts', expected)
    assert receipt['result']['secret'] == '<redacted>'
    assert receipt['fingerprint'] == hashlib.sha256(
        module._encode(['async_jobs', 'job.create', 'params', 0])).hexdigest()
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
    monkeypatch.setattr(module, 'owner_quota_enforcer_from_policy', lambda root: pytest.fail('quota read'))
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
    write = module.write_bytes_atomic_beneath
    # LLM: 仅阻断审计文件替换，其余写入仍调用原原子文件接口。
    # 函数用途: 模拟提交日志成功但审计文件物化失败。
    def fail_audit(owner, parts, raw):
        if parts[-1] == 'audit_log.json':
            raise OSError('synthetic materialization failure')
        return write(owner, parts, raw)
    monkeypatch.setattr(module, 'write_bytes_atomic_beneath', fail_audit)
    store = V13Store(root)
    with pytest.raises(OSError, match='synthetic'):
        _write(store, receipt_view=lambda row: {**row, 'secret': '<redacted>'})
    assert store.get('async_jobs', 'one')['value'] == 1
    assert len(store.list('audit_log')) == 1
    monkeypatch.setattr(module, 'write_bytes_atomic_beneath', write)
    assert _write(V13Store(root))['secret'] == '<redacted>'
    assert json.loads((root / 'data/business_v13/.transaction.json').read_bytes()) == {}
    assert len(store.list('audit_log')) == 1


# LLM: 保留真实文件锁，记录准入顺序并在配额末核撤权，禁止产生业务文件或提交日志。
# 函数用途: 配额检查之后的授权撤销不能写提交日志。
def test_quota_lock_order_and_last_guard(tmp_path, monkeypatch):
    events, allowed = [], [True]
    lock = module.locked_private_directory
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
    monkeypatch.setattr(module, 'locked_private_directory', tracked_lock)
    monkeypatch.setattr(module, 'owner_quota_enforcer_from_policy', lambda root: Gate())
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
    write = module.write_bytes_atomic_beneath
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
    monkeypatch.setattr(module, 'write_bytes_atomic_beneath', fail_second)
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
    monkeypatch.setattr(module, 'write_bytes_atomic_beneath', write)
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
    (folder / '.transaction.json').write_bytes(module._encode(
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
    write = module.write_bytes_atomic_beneath

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

    monkeypatch.setattr(module, 'write_bytes_atomic_beneath', interrupt)
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
    monkeypatch.setattr(module, 'write_bytes_atomic_beneath', write)
    assert store.transaction_many(names, lambda rows: pytest.fail('bulk replay mutated'),
        idempotency_key='delete-bulk', params_digest='delete-bulk') == {'deleted': count}
    assert len(store.list('audit_log')) == 8
