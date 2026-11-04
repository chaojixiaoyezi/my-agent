"""有限业务目录与永久回执行分离的规模和提交边界回归。"""
import hashlib
import json
import subprocess
import sys

import pytest

from agent_py_agent.agent.conversation import business_spaces as catalog
from agent_py_agent.agent.conversation.business_access import BusinessActor, BusinessError
from agent_py_agent.tests.test_scale_f01 import _fds, _finish_quota_reconcile


# LLM: 使用正式创建动作生成有效实体，再只增加旧版回执历史。
# 函数用途: 为升级路径提供真实目录及固定的小实体集。
def _service(home, count=0):
    service = catalog.BusinessSpaceService(home, owner_id=home.name, enabled=True)
    actor = BusinessActor(home.name, 'actor', service, lambda action, facts: True)
    created = service.create({'name': '固定目录'}, actor=actor, idempotency_key='create')
    if count:
        state = json.loads(service.state_path.read_bytes())
        state['receipts'].update({hashlib.sha256(str(index).encode()).hexdigest():
            {'fingerprint': 'b' * 64, 'result': {'index': index}} for index in range(count)})
        service.state_path.write_text(json.dumps(state))
    return service, actor, created


# LLM: 计真实字节、JSON解码及枚举名称，不替换授权、事务或返回值。
# 函数用途: 证明10倍回执增长不会进入常规目录请求的正文或目录扫描。
def test_catalog_io_and_json_decodes_stay_bounded(tmp_path, monkeypatch):
    observed = []
    for count in (1000, 10000):
        service, actor, created = _service(tmp_path / f'owner-{count}', count)
        service.update(created['space_id'], {'purpose': 'warm'}, actor=actor,
                       expected_revision=0, idempotency_key='warm')
        values = {'read': 0, 'written': 0, 'decoded': 0, 'enumerated': 0}
        modules = [catalog]
        try:
            from agent_py_agent.agent.conversation import business_receipt_rows
            modules.append(business_receipt_rows)
        except ImportError:
            pass
        with monkeypatch.context() as patch:
            loads = json.loads

            def decode(*args, **kwargs):
                values['decoded'] += 1
                return loads(*args, **kwargs)

            patch.setattr(json, 'loads', decode)
            for module in modules:
                original_read, original_write = module.read_bytes_beneath, module.write_bytes_atomic_beneath

                def read(root, parts, *args, _read=original_read, **kwargs):
                    raw = _read(root, parts, *args, **kwargs)
                    values['read'] += len(raw) if raw is not None else 0
                    return raw

                def write(root, parts, raw, *args, _write=original_write, **kwargs):
                    result = _write(root, parts, raw, *args, **kwargs)
                    values['written'] += len(raw)
                    return result

                patch.setattr(module, 'read_bytes_beneath', read)
                patch.setattr(module, 'write_bytes_atomic_beneath', write)
                original_names = getattr(module, 'list_names_beneath', None)
                if original_names is not None:
                    def names(root, parts, *args, _names=original_names, **kwargs):
                        result = _names(root, parts, *args, **kwargs)
                        values['enumerated'] += len(result)
                        return result

                    patch.setattr(module, 'list_names_beneath', names)
            before = _fds()
            changed = service.update(created['space_id'], {'purpose': 'changed'}, actor=actor,
                                     expected_revision=1, idempotency_key='changed')
            assert changed['space']['purpose'] == 'changed'
            assert service.update(created['space_id'], {'purpose': 'changed'}, actor=actor,
                                  expected_revision=1, idempotency_key='changed') == changed
            assert service.get(created['space_id'], actor=actor)['space']['purpose'] == 'changed'
            assert _fds() == before
        observed.append(dict(values))
    print('catalog logical data I/O/decodes:', observed)
    assert observed[1]['read'] <= observed[0]['read'] + 2048
    assert observed[1]['written'] <= observed[0]['written'] + 2048
    assert observed[1]['decoded'] <= observed[0]['decoded'] + 2
    assert observed[1]['enumerated'] <= observed[0]['enumerated'] + 8
    assert observed[1]['enumerated'] < 64
    assert observed[1]['read'] < 32768 and observed[1]['written'] < 16384


# LLM: 即使命中成功回执，当前 actor 对记录的权限仍须重新核验。
# 函数用途: 保留回执原结果、指纹和最终授权拒绝。
def test_catalog_replay_keeps_fingerprint_and_live_authorization(tmp_path):
    service, actor, created = _service(tmp_path / 'owner')
    result = service.update(created['space_id'], {'purpose': 'original'}, actor=actor,
                            expected_revision=0, idempotency_key='once')
    assert service.update(created['space_id'], {'purpose': 'original'}, actor=actor,
                          expected_revision=0, idempotency_key='once') == result
    with pytest.raises(BusinessError, match='IDEMPOTENCY_CONFLICT'):
        service.update(created['space_id'], {'purpose': 'different'}, actor=actor,
                       expected_revision=0, idempotency_key='once')
    denied = BusinessActor('owner', 'actor', service,
                           lambda action, facts: facts.get('space_id') != created['space_id'])
    with pytest.raises(BusinessError, match='BUSINESS_ACCESS_DENIED'):
        service.update(created['space_id'], {'purpose': 'original'}, actor=denied,
                       expected_revision=0, idempotency_key='once')


# LLM: 第二批已写部分行但检查点失败，旧目录保持权威；续传不允许改写任何旧证明。
# 函数用途: 有限quota内验证目录分批迁移、失败续传及最终原回执重放。
@pytest.mark.parametrize('missing_prefix', [False, True])
def test_catalog_checkpoint_retry_keeps_all_receipts(tmp_path, monkeypatch, missing_prefix):
    from agent_py_agent.agent.conversation import business_receipt_rows as receipts

    home = tmp_path / 'owner'
    service, actor, created = _service(home, 300)
    (home / 'quota.json').write_text(json.dumps({'max_disk_mb': 1}))
    original = receipts.write_bytes_atomic_beneath
    fired = False

    def write(root, parts, raw):
        nonlocal fired
        if parts[-1] == '.receipt-migration.json' and json.loads(raw)['offset'] > 256 and not fired:
            fired = True
            raise OSError('simulated catalog checkpoint I/O error')
        return original(root, parts, raw)

    monkeypatch.setattr(receipts, 'write_bytes_atomic_beneath', write)
    before = _fds()
    with pytest.raises(OSError, match='checkpoint'):
        service.update(created['space_id'], {'purpose': 'migrated'}, actor=actor,
                       expected_revision=0, idempotency_key='migrate')
    assert fired and len(json.loads(service.state_path.read_bytes())['receipts']) == 300
    assert service.get(created['space_id'], actor=actor)['space']['purpose'] == ''
    if missing_prefix:
        key = min(hashlib.sha256(str(index).encode()).hexdigest() for index in range(300))
        home.joinpath(*receipts.ReceiptRows(service).parts(key)).rename(tmp_path / 'saved-receipt.json')
        _finish_quota_reconcile(home)
    monkeypatch.setattr(receipts, 'write_bytes_atomic_beneath', original)
    result = service.update(created['space_id'], {'purpose': 'migrated'}, actor=actor,
                            expected_revision=0, idempotency_key='migrate')
    assert service.update(created['space_id'], {'purpose': 'migrated'}, actor=actor,
                          expected_revision=0, idempotency_key='migrate') == result
    backend = receipts.ReceiptRows(service)
    for index in range(300):
        key = hashlib.sha256(str(index).encode()).hexdigest()
        assert backend.get(key) == {'fingerprint': 'b' * 64, 'result': {'index': index}}
    assert len(list((home / 'data/business/receipts').glob('*.json'))) == 302
    assert json.loads(service.state_path.read_bytes())['receipts'] == {} and _fds() == before


# LLM: journal发布后目录替换失败，逻辑读与冷启动重放仍绑定同一提交，不再增加修订。
# 函数用途: 验证目录、回执、修订在物化中断后一起恢复。
def test_catalog_committed_journal_recovers_original_receipt(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import business_receipt_rows as receipts

    home = tmp_path / 'owner'
    service, actor, created = _service(home)
    original = receipts.write_bytes_atomic_beneath

    def write(root, parts, raw):
        if parts[-1] == 'catalog.json':
            raise OSError('simulated catalog materialization I/O error')
        return original(root, parts, raw)

    monkeypatch.setattr(receipts, 'write_bytes_atomic_beneath', write)
    with pytest.raises(OSError, match='materialization'):
        service.update(created['space_id'], {'purpose': 'committed'}, actor=actor,
                       expected_revision=0, idempotency_key='once')
    assert service.get(created['space_id'], actor=actor)['space']['purpose'] == 'committed'
    monkeypatch.setattr(receipts, 'write_bytes_atomic_beneath', original)
    cold = catalog.BusinessSpaceService(home, owner_id=home.name, enabled=True)
    cold_actor = BusinessActor(home.name, 'actor', cold, lambda action, facts: True)
    first = cold.update(created['space_id'], {'purpose': 'committed'}, actor=cold_actor,
                        expected_revision=0, idempotency_key='once')
    assert first['space']['purpose'] == 'committed' and first['space']['revision'] == 1
    assert cold.update(created['space_id'], {'purpose': 'committed'}, actor=cold_actor,
                       expected_revision=0, idempotency_key='once') == first
    assert json.loads(cold.state_path.read_bytes())['revision'] == 2
    assert json.loads(home.joinpath(*receipts.ReceiptRows(cold).journal).read_bytes()) == {}


# LLM: 使用正式quota.json及实际磁盘计量，拒绝时不能留下新目标、回执或提交日志。
# 函数用途: 证明回执行新增仍受原有限配额约束。
def test_catalog_quota_rejection_has_no_committed_change(tmp_path):
    from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaExceeded

    home = tmp_path / 'owner'
    service, actor, created = _service(home)
    (home / 'quota.json').write_text(json.dumps({'max_disk_mb': 1}))
    (home / 'padding.bin').write_bytes(b'x' * (1024 * 1024))
    before = service.state_path.read_bytes()
    count = len(list((home / 'data/business/receipts').glob('*.json')))
    with pytest.raises(OwnerQuotaExceeded):
        service.update(created['space_id'], {'purpose': 'denied'}, actor=actor,
                       expected_revision=0, idempotency_key='denied')
    assert service.state_path.read_bytes() == before
    assert len(list((home / 'data/business/receipts').glob('*.json'))) == count
    assert service.get(created['space_id'], actor=actor)['space']['purpose'] == ''


# LLM: catalog元数据丢失不能伪装成空库，永久回执也不能代替目标存在。
# 函数用途: 已有回执时缺失目录快照的读取和创建重放均拒绝。
def test_missing_catalog_with_persistent_receipts_fails_closed(tmp_path):
    service, actor, _created = _service(tmp_path / 'owner')
    service.state_path.rename(tmp_path / 'saved-catalog.json')
    with pytest.raises(BusinessError, match='STORE_UNAVAILABLE'):
        service.list(actor=actor)
    with pytest.raises(BusinessError, match='STORE_UNAVAILABLE'):
        service.create({'name': '固定目录'}, actor=actor, idempotency_key='create')


# LLM: 原目录事务在发布前或旧回执迁移rename前退出，旧快照保持权威；不注入已提交日志。
# 函数用途: 有限quota下冷重试清掉准确孤儿候选，保留旧证明和新回执的恰好一次语义。
@pytest.mark.parametrize('phase', ['journal', 'migration'])
def test_catalog_unpublished_candidate_retry_with_tight_quota(tmp_path, phase):
    from agent_py_agent.agent.conversation.business_receipt_rows import ReceiptRows
    from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaExceeded

    home = tmp_path / 'owner'
    service, actor, created = _service(home, 1 if phase == 'migration' else 0)
    key = hashlib.sha256(b'0').hexdigest()
    receipt = {'fingerprint': 'b' * 64, 'result': {'body': 'x' * 450000}}
    if phase == 'migration':
        state = json.loads(service.state_path.read_bytes())
        state['receipts'][key] = receipt
        service.state_path.write_text(json.dumps(state))
    (home / 'quota.json').write_text(json.dumps({'max_disk_mb': 1}))
    _finish_quota_reconcile(home)
    before_state = service.state_path.read_bytes()
    target = '.catalog-transaction.json' if phase == 'journal' else key + '.json'
    program = '''
import os, sys
from pathlib import Path
from agent_py_agent.agent.conversation.business_spaces import BusinessSpaceService
from agent_py_agent.agent.conversation.business_access import BusinessActor
home = Path(sys.argv[1])
service = BusinessSpaceService(home, owner_id=home.name, enabled=True)
actor = BusinessActor(home.name, 'actor', service, lambda action, facts: True)
original = os.rename
target = sys.argv[3]
def rename(src, dst, *args, **kwargs):
    if dst == target and src.startswith('.' + target + '.tmp-'):
        os._exit(26)
    return original(src, dst, *args, **kwargs)
if original in os.supports_dir_fd:
    os.supports_dir_fd.add(rename)
os.rename = rename
service.update(sys.argv[2], {'purpose': 'retry'}, actor=actor,
               expected_revision=0, idempotency_key='retry')
'''
    exited = subprocess.run([sys.executable, '-c', program, str(home), created['space_id'], target],
                            check=False, timeout=30)
    assert exited.returncode == 26 and service.state_path.read_bytes() == before_state
    candidates = list((home / 'data/business').rglob('.' + target + '.tmp-*'))
    assert len(candidates) == 1 and ReceiptRows(service).pending() is None
    assert service.get(created['space_id'], actor=actor)['space']['purpose'] == ''
    cold = catalog.BusinessSpaceService(home, owner_id=home.name, enabled=True)
    cold_actor = BusinessActor(home.name, 'actor', cold, lambda action, facts: True)
    before = _fds()
    try:
        first = cold.update(created['space_id'], {'purpose': 'retry'}, actor=cold_actor,
                            expected_revision=0, idempotency_key='retry')
    except OwnerQuotaExceeded as error:
        pytest.fail(f'unpublished catalog candidate blocked cold retry: {error}')
    assert first['space']['purpose'] == 'retry' and first['space']['revision'] == 1
    assert cold.update(created['space_id'], {'purpose': 'retry'}, actor=cold_actor,
                       expected_revision=0, idempotency_key='retry') == first
    assert json.loads(cold.state_path.read_bytes())['revision'] == 2
    assert not candidates[0].exists() and _fds() == before
    if phase == 'migration':
        assert ReceiptRows(cold).get(key) == receipt
