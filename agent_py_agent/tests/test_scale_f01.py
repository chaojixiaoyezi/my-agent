"""V13 历史增长、回执重放与提交点恢复的实际文件 I/O 回归。"""
from __future__ import annotations

import errno
import hashlib
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_py_agent.agent.business_spaces import v13_store as storage
from agent_py_agent.agent.business_spaces.v13_store import V13Store
from agent_py_agent.agent.conversation.business_access import BusinessError


# LLM: 只构造升级前的规范快照；热路径测量开始前先完成一次真实事务升级。
# 函数用途: 固定目标大小，仅增长本 owner 的运行、审计和回执历史。
def _legacy(home, count):
    root = home / 'data' / 'business_v13'
    root.mkdir(parents=True)
    for collection in ('script_runs', 'async_jobs', 'audit_log'):
        records = {f'old-{index:05}': {'id': f'old-{index:05}', 'state': 'completed'}
                   for index in range(count)}
        receipts = {hashlib.sha256(str(index).encode()).hexdigest():
                    {'fingerprint': 'a' * 64, 'result': {'index': index}}
                    for index in range(count)} if collection != 'audit_log' else {}
        state = {'schema_version': 'business-v13.v1', 'owner_id': home.name,
                 'collection': collection, 'revision': count, 'records': records, 'receipts': receipts}
        (root / (collection + '.json')).write_bytes(storage._encode(state))


# LLM: 使用真实 FD fstat，目录采样器自身先关闭；不存在的描述符不计入快照。
# 函数用途: 证明每次热事务后没有遗留打开的文件。
def _fds():
    directory = next(path for path in (Path('/proc/self/fd'), Path('/dev/fd')) if path.is_dir())
    with os.scandir(directory) as entries:
        numbers = [int(entry.name) for entry in entries if entry.name.isdecimal()]
    result = set()
    for number in numbers:
        try:
            os.fstat(number)
        except OSError as error:
            if error.errno != errno.EBADF:
                raise
        else:
            result.add(number)
    return result


# LLM: 故障夹具在准入外移动文件会使原quota基线失效，须显式跑原有界对账后再测迁移。
# 函数用途: 恢复准确磁盘计费观察，不改写或豁免quota账本。
def _finish_quota_reconcile(home):
    from agent_py_agent.agent.user_space.owner_quota import reconcile_owner_quota_slice

    for _attempt in range(32):
        if reconcile_owner_quota_slice(home, max_entries=128, max_ms=1000)['complete']:
            return
    pytest.fail('quota reconciliation did not converge within bounded fixture setup')


# LLM: 包装原 no-follow 函数只计量实际字节和枚举名称，不替换存储结果。
# 函数用途: 测量热事务的正文读写、目录扫描量及旧运行正文加载数。
def _meter(monkeypatch):
    from agent_py_agent.agent.conversation import business_receipt_rows

    values = {'read': 0, 'written': 0, 'old_runs': 0, 'enumerated': 0}
    modules = [storage, business_receipt_rows]
    try:
        from agent_py_agent.agent.business_spaces import v13_history_rows
        modules.append(v13_history_rows)
    except ImportError:
        pass
    for module in modules:
        original_read, original_write = module.read_bytes_beneath, module.write_bytes_atomic_beneath

        def read(root, parts, *args, _read=original_read, **kwargs):
            raw = _read(root, parts, *args, **kwargs)
            if raw is not None:
                values['read'] += len(raw)
                values['old_runs'] += raw.count(b'old-09999')
            return raw

        def write(root, parts, raw, *args, _write=original_write, **kwargs):
            result = _write(root, parts, raw, *args, **kwargs)
            values['written'] += len(raw)
            return result

        monkeypatch.setattr(module, 'read_bytes_beneath', read)
        monkeypatch.setattr(module, 'write_bytes_atomic_beneath', write)
        original_names = getattr(module, 'list_names_beneath', None)
        if original_names is not None:
            def names(root, parts, *args, _names=original_names, **kwargs):
                result = _names(root, parts, *args, **kwargs)
                values['enumerated'] += len(result)
                return result

            monkeypatch.setattr(module, 'list_names_beneath', names)
    return values


# LLM: 10倍增长后比较实际字节及目录枚举；首次迁移不算常规操作。
# 函数用途: 确认永久历史不进入单条更新、回执重放或点详情的正文与扫描路径。
def test_transaction_io_is_independent_of_history(tmp_path, monkeypatch):
    observed = []
    for count in (1000, 10000):
        home = tmp_path / f'owner-{count}'
        _legacy(home, count)
        store = V13Store(home, action='run')
        store.transaction('script_runs', lambda rows: rows.update(live={'state': 'running'}) or {'accepted': True},
                          idempotency_key='warm', params_digest='warm')
        with monkeypatch.context() as patch:
            measured = _meter(patch)
            before = _fds()

            def update(rows):
                rows.get('live')['state'] = 'completed'
                return {'done': True}

            result = store.transaction('script_runs', update, idempotency_key='finish', params_digest='finish')
            assert result == {'done': True}
            assert store.get('script_runs', 'live') == {'state': 'completed'}
            assert _fds() == before
            cost = dict(measured)
            measured.update(read=0, written=0, old_runs=0)

            def never(rows):
                raise AssertionError('receipt replay invoked mutate')

            assert store.transaction('script_runs', never, idempotency_key='finish', params_digest='finish') == result
            assert measured['old_runs'] == 0
            assert measured['written'] == 0
            assert _fds() == before
            observed.append(cost)
    print('V13 logical data I/O bytes:', observed)
    assert observed[1]['read'] <= observed[0]['read'] + 2048
    assert observed[1]['written'] <= observed[0]['written'] + 2048
    assert observed[1]['enumerated'] <= observed[0]['enumerated'] + 8
    assert observed[1]['enumerated'] < 64
    assert observed[1]['read'] < 32768 and observed[1]['written'] < 32768


# LLM: 单次失败在真实 journal 发布后、首个集合物化前；同键恢复不能再次执行 mutate。
# 函数用途: 验证多集合目标、回执与审计以唯一提交点一起可见并恰好恢复一次。
def test_row_commit_recovers_without_repeating_mutation(tmp_path, monkeypatch):
    store = V13Store(tmp_path / 'owner', action='multi')
    original = storage.write_bytes_atomic_beneath
    fired = False
    calls = 0

    def write(root, parts, raw):
        nonlocal fired
        if parts[-1] == 'projects.json' and not fired:
            fired = True
            raise OSError('simulated materialization I/O error')
        return original(root, parts, raw)

    def mutate(rows):
        nonlocal calls
        calls += 1
        rows['projects']['target'] = {'value': 1}
        rows['script_runs']['run'] = {'state': 'completed'}
        return {'committed': True}

    monkeypatch.setattr(storage, 'write_bytes_atomic_beneath', write)
    with pytest.raises(OSError):
        store.transaction_many(('projects', 'script_runs'), mutate, idempotency_key='once', params_digest='once')
    assert fired and calls == 1
    assert store.get('projects', 'target') == {'value': 1}
    assert store.get('script_runs', 'run') == {'state': 'completed'}
    cold = V13Store(store.owner_home, action='multi')
    assert cold.transaction_many(('projects', 'script_runs'), mutate,
                                 idempotency_key='once', params_digest='once') == {'committed': True}
    assert calls == 1 and cold.revision('projects') == 1 and cold.revision('script_runs') == 1
    assert len(cold.list('audit_log')) == 2
    assert cold.transaction_many(('projects', 'script_runs'), mutate,
                                 idempotency_key='once', params_digest='once') == {'committed': True}
    assert calls == 1 and len(cold.list('audit_log')) == 2


# LLM: 两个独立句柄实持同 owner 文件锁；回执参数与 CAS 保持原合同。
# 函数用途: 核对并发重放一致、不同参数冲突和原地编辑均耐久。
def test_row_replay_concurrent_and_cas(tmp_path):
    home = tmp_path / 'owner'

    def request(index):
        store = V13Store(home, action='run')
        return store.transaction('async_jobs', lambda rows: rows.update(job={'state': 'accepted'}) or {'first': index},
                                 idempotency_key='same', params_digest='same', expected_revision=0)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(request, range(2)))
    assert results[0] == results[1]
    store = V13Store(home, action='run')
    assert store.revision('async_jobs') == 1
    with pytest.raises(BusinessError, match='IDEMPOTENCY_CONFLICT'):
        store.transaction('async_jobs', lambda rows: {}, idempotency_key='same', params_digest='different')
    with pytest.raises(BusinessError, match='REVISION_CONFLICT'):
        store.transaction('async_jobs', lambda rows: {}, idempotency_key='other', params_digest='same', expected_revision=0)


# LLM: 子进程在唯一 journal 发布后直接退出，不经过 Python finally；恢复必须沿同一文件入口。
# 函数用途: 检查真实进程中断不会丢失回执、重复业务变更或产生半笔多集合视图。
def test_process_exit_after_journal_recovers_once(tmp_path):
    home = tmp_path / 'owner'
    program = '''
import os, sys
from agent_py_agent.agent.business_spaces import v13_store as storage
original = storage.write_bytes_atomic_beneath
def write(root, parts, raw):
    if parts[-1] == 'projects.json':
        os._exit(23)
    return original(root, parts, raw)
storage.write_bytes_atomic_beneath = write
store = storage.V13Store(sys.argv[1], action='multi')
def mutate(rows):
    rows['projects']['target'] = {'value': 1}
    rows['script_runs']['run'] = {'state': 'completed'}
    return {'committed': True}
store.transaction_many(('projects', 'script_runs'), mutate, idempotency_key='once', params_digest='once')
'''
    result = subprocess.run([sys.executable, '-c', program, str(home)], check=False, timeout=30)
    assert result.returncode == 23
    store = V13Store(home, action='multi')
    assert store.get('projects', 'target') == {'value': 1}
    assert store.get('script_runs', 'run') == {'state': 'completed'}

    def never(rows):
        raise AssertionError('recovery repeated the business mutation')

    for _index in range(2):
        assert store.transaction_many(('projects', 'script_runs'), never,
                                      idempotency_key='once', params_digest='once') == {'committed': True}
        assert store.revision('projects') == store.revision('script_runs') == 1
        assert len(store.list('audit_log')) == 2


# LLM: 在原 no-follow 行替换内部直接退出，保留真实临时文件而不执行 finally。
# 函数用途: 恢复后列表仍可读，且同键不再执行目标变更。
@pytest.mark.parametrize('finite_quota', [False, True])
def test_process_exit_inside_row_replace_recovers_once(tmp_path, finite_quota):
    home = tmp_path / 'owner'
    if finite_quota:
        home.mkdir()
        (home / 'quota.json').write_bytes(storage._encode({'max_disk_mb': 1}))
    program = '''
import hashlib, os, sys
from agent_py_agent.agent.business_spaces.v13_store import V13Store
original = os.rename
target = hashlib.sha256(b'run').hexdigest() + '.json'
def rename(src, dst, *args, **kwargs):
    if dst == target and src.startswith('.' + target + '.tmp-'):
        os._exit(24)
    return original(src, dst, *args, **kwargs)
if original in os.supports_dir_fd:
    os.supports_dir_fd.add(rename)
os.rename = rename
store = V13Store(sys.argv[1], action='run')
value = {'state': 'completed'}
if int(sys.argv[2]):
    value['body'] = 'x' * int(sys.argv[2])
store.transaction('script_runs', lambda rows: rows.update(run=value) or {'done': True},
                  idempotency_key='once', params_digest='once')
'''
    result = subprocess.run([sys.executable, '-c', program, str(home), '450000' if finite_quota else '0'],
                            check=False, timeout=30)
    assert result.returncode == 24
    store = V13Store(home, action='run')
    expected = {'state': 'completed', **({'body': 'x' * 450000} if finite_quota else {})}
    assert store.get('script_runs', 'run') == expected

    def never(rows):
        raise AssertionError('row recovery repeated mutate')

    assert store.transaction('script_runs', never,
                             idempotency_key='once', params_digest='once') == {'done': True}
    assert store.list('script_runs') == [expected]
    assert store.revision('script_runs') == 1 and len(store.list('audit_log')) == 1
    if finite_quota:
        assert not list((home / 'data/business_v13/history/script_runs/records').glob('.*.tmp-*'))


# LLM: 旧 main home 的审计由内部事务维护，公开集合 scope 检查不得因此放宽。
# 函数用途: main 升级首次写入迁移审计，仍拒绝外部访问 owner 集合。
def test_main_scope_migrates_internal_legacy_audit(tmp_path):
    home = tmp_path / 'main'
    folder = home / 'data/business_v13'
    folder.mkdir(parents=True)
    state = {'schema_version': 'business-v13.v1', 'owner_id': 'local/main',
             'collection': 'audit_log', 'revision': 1, 'receipts': {},
             'records': {'old': {'entry_id': 'old', 'outcome': 'committed'}}}
    (folder / 'audit_log.json').write_bytes(storage._encode(state))
    store = V13Store(home, owner_id='local/main', action='admin')
    store._scope = 'main'
    assert store.transaction('admin_ops', lambda rows: rows.update(op={'id': 'op'}) or {'created': 'op'},
                             idempotency_key='new', params_digest='new') == {'created': 'op'}
    assert store.get('admin_ops', 'op') == {'id': 'op'}
    owner = V13Store(home, owner_id='local/main')
    assert len(owner.list('audit_log')) == 2
    assert owner.get('audit_log', 'old') == {'entry_id': 'old', 'outcome': 'committed'}
    with pytest.raises(BusinessError, match='BUSINESS_ACCESS_DENIED'):
        store.list('audit_log')
    with pytest.raises(BusinessError, match='BUSINESS_ACCESS_DENIED'):
        store.list('async_jobs')


# LLM: transaction_many 原协议允许整体替换映射；前像不能来自 mutate 后的缓存。
# 函数用途: 原地编辑、插入和删除后再转 dict，三个变化都必须耐久。
def test_replaced_mapping_preserves_original_before_values(tmp_path):
    store = V13Store(tmp_path / 'owner', action='replace')
    store.transaction('script_runs', lambda rows: rows.update(
        run={'state': 'running'}, removed={'state': 'running'}) or {},
        idempotency_key='seed', params_digest='seed')

    def mutate(tables):
        rows = tables['script_runs']
        rows.get('run')['state'] = 'completed'
        rows['added'] = {'state': 'completed'}
        del rows['removed']
        tables['script_runs'] = dict(rows)
        return {'done': True}

    assert store.transaction_many(('script_runs',), mutate,
                                  idempotency_key='replace', params_digest='replace') == {'done': True}
    assert store.get('script_runs', 'run') == {'state': 'completed'}
    assert store.get('script_runs', 'added') == {'state': 'completed'}
    assert store.get('script_runs', 'removed') is None
    assert store.revision('script_runs') == 2


# LLM: 完整替换仍服从记录只能是非空字符串键到字典值的原校验，不把坏值伪装成删除。
# 函数用途: 非法替换必须回滚，不能留下成功回执或修订。
@pytest.mark.parametrize('replacement', [{'bad': None}, {'bad': 7}, {'': {}}])
def test_replaced_mapping_rejects_invalid_records(tmp_path, replacement):
    store = V13Store(tmp_path / 'owner')

    def mutate(tables):
        tables['script_runs'] = replacement
        return {'done': True}

    with pytest.raises(BusinessError, match='DOMAIN_RESULT_INVALID'):
        store.transaction_many(('script_runs',), mutate, idempotency_key='invalid', params_digest='invalid')
    assert store.revision('script_runs') == 0


# LLM: 列举第一行之后插入一笔完整双行提交；读者只能重试，不能把旧新值拼成一页。
# 函数用途: 页正文和游标必须同属一次已核验修订。
def test_page_rechecks_revision_after_lazy_rows(tmp_path, monkeypatch):
    from agent_py_agent.agent.business_spaces import v13_history_rows as history

    store = V13Store(tmp_path / 'owner')
    store.transaction('script_runs', lambda rows: rows.update(a={'value': 1}, b={'value': 1}) or {},
                      idempotency_key='seed', params_digest='seed')
    original = history.read_bytes_beneath
    fired = False
    leaf = hashlib.sha256(b'a').hexdigest() + '.json'

    def read(home, parts):
        nonlocal fired
        raw = original(home, parts)
        if not fired and tuple(parts[-3:]) == ('script_runs', 'records', leaf):
            fired = True
            store.transaction('script_runs', lambda rows: rows.update(a={'value': 2}, b={'value': 2}) or {},
                              idempotency_key='change', params_digest='change')
        return raw

    monkeypatch.setattr(history, 'read_bytes_beneath', read)
    first = store.page('script_runs', limit=1)
    assert fired and first['items'] == [{'value': 2}]
    second = store.page('script_runs', limit=1, cursor=first['next_cursor'])
    assert second['items'] == [{'value': 2}] and second['next_cursor'] is None


# LLM: 原有限quota门和真实批次检查点共同运行，第二批写后失败只能从上一检查点幂等续传。
# 函数用途: 保留全部旧证明并结算迁移字节，超额后不得提交新的目标或回执。
@pytest.mark.parametrize('missing_prefix', [False, True])
def test_migration_checkpoint_retry_with_finite_quota(tmp_path, monkeypatch, missing_prefix):
    import json

    from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaExceeded

    home = tmp_path / 'owner'
    _legacy(home, 300)
    (home / 'quota.json').write_text(json.dumps({'max_disk_mb': 1}))
    original = storage.write_bytes_atomic_beneath
    calls, fired = 0, False

    def write(root, parts, raw):
        nonlocal fired
        if parts[-1] == '.migrate-script_runs.json' and json.loads(raw)['offset'] > 256 and not fired:
            fired = True
            raise OSError('simulated migration checkpoint I/O error')
        return original(root, parts, raw)

    def mutate(rows):
        nonlocal calls
        calls += 1
        rows['live'] = {'state': 'completed'}
        return {'done': True}

    store = V13Store(home, action='run')
    before = _fds()
    monkeypatch.setattr(storage, 'write_bytes_atomic_beneath', write)
    with pytest.raises(OSError, match='checkpoint'):
        store.transaction('script_runs', mutate, idempotency_key='new', params_digest='new')
    assert fired and calls == 0
    checkpoint = home / 'data/business_v13/.migrate-script_runs.json'
    assert json.loads(checkpoint.read_bytes())['offset'] == 256
    assert store.get('script_runs', 'old-00299')['state'] == 'completed'
    assert not (home / 'data/business_v13/.transaction.json').exists()
    prefix_key = min(hashlib.sha256(str(index).encode()).hexdigest() for index in range(300))
    prefix = store._rows('script_runs').get('receipts', prefix_key)
    if missing_prefix:
        home.joinpath(*store._rows('script_runs').parts('receipts', prefix_key)).rename(tmp_path / 'saved-receipt.json')
        _finish_quota_reconcile(home)
    monkeypatch.setattr(storage, 'write_bytes_atomic_beneath', original)
    assert store.transaction('script_runs', mutate, idempotency_key='new', params_digest='new') == {'done': True}
    assert calls == 1 and len(store.list('script_runs')) == 301
    assert len(store.list('audit_log')) == 301
    assert store._rows('script_runs').get('receipts', prefix_key) == prefix
    for index in (0, 299):
        key = hashlib.sha256(str(index).encode()).hexdigest()
        assert store._rows('script_runs').get('receipts', key) == {
            'fingerprint': 'a' * 64, 'result': {'index': index}}
    revision = store.revision('script_runs')

    def too_large(rows):
        rows['over-limit'] = {'data': 'x' * (1024 * 1024)}
        return {'done': True}

    with pytest.raises(OwnerQuotaExceeded):
        store.transaction('script_runs', too_large, idempotency_key='denied', params_digest='denied')
    assert store.get('script_runs', 'over-limit') is None and store.revision('script_runs') == revision
    assert store.transaction('script_runs', mutate, idempotency_key='new', params_digest='new') == {'done': True}
    assert calls == 1 and _fds() == before


# LLM: 旧行回执不能代替目标与修订，丢失canonical元数据不属于首次初始化。
# 函数用途: 元数据缺失时读取和重放都必须拒绝。
def test_missing_snapshot_with_persistent_rows_fails_closed(tmp_path):
    home = tmp_path / 'owner'
    store = V13Store(home)
    store.transaction('async_jobs', lambda rows: rows.update(job={'state': 'completed'}) or {'done': True},
                      idempotency_key='once', params_digest='once')
    (home / 'data/business_v13/async_jobs.json').rename(tmp_path / 'saved-snapshot.json')
    with pytest.raises(BusinessError, match='STORE_UNAVAILABLE'):
        store.get('async_jobs', 'job')
    with pytest.raises(BusinessError, match='STORE_UNAVAILABLE'):
        store.transaction('async_jobs', lambda rows: pytest.fail('missing snapshot mutated'),
                          idempotency_key='once', params_digest='once')


# LLM: 子进程在journal发布前或旧源迁移行rename前退出，未发布候选绝不作为提交证据。
# 函数用途: 紧quota冷重试精确回收孤儿候选，仍只执行并提交一次新变更。
@pytest.mark.parametrize('phase', ['journal', 'migration'])
def test_unpublished_candidate_retry_with_tight_quota(tmp_path, phase):
    from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaExceeded

    home = tmp_path / 'owner'
    folder = home / 'data/business_v13'
    folder.mkdir(parents=True)
    (home / 'quota.json').write_bytes(storage._encode({'max_disk_mb': 1}))
    legacy = {'body': 'x' * 450000}
    if phase == 'migration':
        (folder / 'script_runs.json').write_bytes(storage._encode({
            'schema_version': 'business-v13.v1', 'owner_id': home.name,
            'collection': 'script_runs', 'revision': 7, 'records': {'legacy': legacy}, 'receipts': {}}))
    target = ('.transaction.json' if phase == 'journal'
              else hashlib.sha256(b'legacy').hexdigest() + '.json')
    program = '''
import os, sys
from agent_py_agent.agent.business_spaces.v13_store import V13Store
original = os.rename
target = sys.argv[2]
def rename(src, dst, *args, **kwargs):
    if dst == target and src.startswith('.' + target + '.tmp-'):
        os._exit(25)
    return original(src, dst, *args, **kwargs)
if original in os.supports_dir_fd:
    os.supports_dir_fd.add(rename)
os.rename = rename
value = {'state': 'completed'}
if target == '.transaction.json':
    value['body'] = 'x' * 450000
store = V13Store(sys.argv[1], action='run')
store.transaction('script_runs', lambda rows: rows.update(run=value) or {'done': True},
                  idempotency_key='once', params_digest='once')
'''
    exited = subprocess.run([sys.executable, '-c', program, str(home), target], check=False, timeout=30)
    assert exited.returncode == 25
    candidates = list(folder.rglob('.' + target + '.tmp-*'))
    assert len(candidates) == 1 and candidates[0].stat().st_size > 450000
    store = V13Store(home, action='run')
    assert not store._pending() and store.get('script_runs', 'run') is None
    assert store.revision('script_runs') == (7 if phase == 'migration' else 0)
    expected = {'state': 'completed', **({'body': 'x' * 450000} if phase == 'journal' else {})}
    calls = 0

    def mutate(rows):
        nonlocal calls
        calls += 1
        rows['run'] = expected
        return {'done': True}

    before = _fds()
    try:
        first = store.transaction('script_runs', mutate, idempotency_key='once', params_digest='once')
    except OwnerQuotaExceeded as error:
        pytest.fail(f'unpublished candidate blocked cold retry: {error}')
    assert first == {'done': True} and calls == 1
    assert store.transaction('script_runs', mutate, idempotency_key='once', params_digest='once') == first
    assert calls == 1 and store.get('script_runs', 'run') == expected
    assert store.revision('script_runs') == (8 if phase == 'migration' else 1)
    assert len(store.list('audit_log')) == 1 and not candidates[0].exists() and _fds() == before
    if phase == 'migration':
        assert store.get('script_runs', 'legacy') == legacy
