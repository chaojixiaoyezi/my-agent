# LLM: 使用真实准入、账本和对账；大文件为稀疏文件，只替换明确的观察失效和禁止调用探针。
# 模块用途: 覆盖压力只读语义、永久资产余量和挂起对账累计增长上限。
import json

import pytest

from agent_py_agent.agent.user_space import owner_quota as quota


# LLM: 固定有限轮数，真实扫描完成才返回，不用替代统计或放宽完成断言。
# 函数用途: 在测试目录完成一轮真实配额对账。
def _reconcile(root):
    for _ in range(32):
        result = quota.reconcile_owner_quota_slice(root, max_entries=1024, max_ms=1000)
        if result['complete']:
            assert quota._read_quota_ledger(root)['usage_authoritative'] is True
            return result
    pytest.fail('quota reconciliation did not complete')


# LLM: 预先建好目录并扫描真实160文件，置脏后保持原元数据表与计数；复用原 quota_pending 夹具口径。
# 函数用途: 创建等待对账且前台128项快照读不完的 owner。
def _quota_pending(root):
    (root / 'memory').mkdir()
    for index in range(160):
        (root / str(index)).write_bytes(b'x')
    _reconcile(root)
    ledger = quota._read_quota_ledger(root)
    ledger['dirty_since'] = 1
    quota._write_quota_ledger(root, ledger)
    return ledger['used_bytes']


# LLM: 写者沿原 reserve 门并真实落盘，稀疏文件的 st_size 就是生产逻辑计费字节。
# 函数用途: 经配额准入写入指定逻辑长度的测试文件。
def _write(enforcer, path, size):
    with enforcer.reserve([quota.OwnerQuotaChange(path, size)]):
        with path.open('wb') as handle:
            handle.truncate(size)


# LLM: 只调用真实 admission.check，退出后照常结算，不给准入器虚构额外公开接口。
# 函数用途: 在原配额锁生命周期内检查一批变更。
def _check(enforcer, changes):
    with enforcer.admission() as admission:
        return admission.check(changes)


# LLM: 不造假账本计数；quota.json 也计入用量，用稀疏填充文件刚好占满100MiB。
# 函数用途: 创建准确满额且带真实策略的 owner。
def _full_owner(root):
    maximum = 100 * 1024 * 1024
    (root / 'memory').mkdir()
    policy = root / 'quota.json'
    policy.write_text(json.dumps({'max_disk_mb': 100}))
    with (root / 'filler').open('wb') as handle:
        handle.truncate(maximum - policy.stat().st_size)
    _reconcile(root)
    assert quota._read_quota_ledger(root)['used_bytes'] == maximum
    return quota.OwnerQuotaEnforcer(root, max_bytes=maximum), maximum


# LLM: 调用探针前记录目录，禁止任何配额锁、写账或扫描入口，缺策略及缺账均不得创建元数据。
# 函数用途: 验证压力未知时纯读返回None。
def test_pressure_missing_policy_or_ledger_is_read_only(tmp_path, monkeypatch):
    # LLM: 探针触及任何禁止入口都立即失败，不能以目录没增加掩盖一次扫描或锁调用。
    # 函数用途: 拒绝压力探针的写入、锁定和扫描副作用。
    def forbidden(*args, **kwargs):
        pytest.fail('pressure probe attempted lock/write/scan')

    for name in ('_locked_owner_quota', '_write_quota_ledger', '_bounded_quota_snapshot', 'owner_logical_usage_bytes'):
        monkeypatch.setattr(quota, name, forbidden)
    assert quota.owner_quota_pressure(tmp_path) is None
    assert list(tmp_path.iterdir()) == []
    policy = tmp_path / 'quota.json'
    policy.write_text('{"max_disk_mb":1}')
    before = sorted(tmp_path.iterdir())
    assert quota.owner_quota_pressure(tmp_path) is None
    assert sorted(tmp_path.iterdir()) == before


# LLM: 真实完整账本只调整被测观察值及有效预留，精确断言全部字段；探针不能改动原字节或目录。
# 函数用途: 验证压力阈值、预留计费和权威标志。
@pytest.mark.parametrize('mb,used,reserved,dirty,authoritative,level', [
    (1, int(0.9 * 1024 * 1024), 0, None, True, 'high'),
    (1, int(0.96 * 1024 * 1024), 0, None, True, 'critical'),
    (100, 85 * 1024 * 1024 - 1, 0, None, True, 'normal'),
    (100, 85 * 1024 * 1024, 0, None, True, 'high'),
    (100, 95 * 1024 * 1024 - 1, 1, 1, True, 'critical'),
    (100, 0, 0, None, 1, 'normal'),
])
def test_pressure_thresholds_and_reservations(tmp_path, mb, used, reserved, dirty, authoritative, level):
    (tmp_path / 'quota.json').write_text(json.dumps({'max_disk_mb': mb}))
    _reconcile(tmp_path)
    ledger = quota._read_quota_ledger(tmp_path)
    ledger.update(used_bytes=used, reserved_bytes=reserved, dirty_since=dirty, usage_authoritative=authoritative,
                  reservations={'held': {'bytes': reserved, 'expires_at': 1}} if reserved else {})
    quota._write_quota_ledger(tmp_path, ledger)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert quota.owner_quota_pressure(tmp_path) == {
        'level': level, 'used_bytes': used + reserved, 'max_bytes': mb * 1024 * 1024,
        'ratio': round((used + reserved) / (mb * 1024 * 1024), 6),
        'authoritative': authoritative is True and dirty is None, 'reason': 'ledger',
    }
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


# LLM: 坏账和关闭策略均未知，不能重建账本或误读为零用量。
# 函数用途: 验证不可读压力来源安全降级且不写回。
@pytest.mark.parametrize('payload', ['{broken', '{}', '{"schema_generation":999}'])
def test_pressure_bad_ledger_is_unknown(tmp_path, payload):
    (tmp_path / 'quota.json').write_text('{"max_disk_mb":1}')
    ledger = tmp_path / quota._QUOTA_LEDGER
    ledger.write_text(payload)
    assert quota.owner_quota_pressure(tmp_path) is None
    assert ledger.read_text() == payload
    (tmp_path / 'quota.json').write_text('{"max_disk_mb":0}')
    assert quota.owner_quota_pressure(tmp_path) is None
    assert not (tmp_path / '.owner-quota.lock').exists()


# LLM: 真实100MiB满额准入与结算；余量边界准确到一字节，projection 不修改原 max_bytes。
# 函数用途: 验证永久数据允许5%余量，普通和混合变更仍拒绝。
def test_permanent_headroom_exact_boundary_and_mixed_sets(tmp_path):
    enforcer, maximum = _full_owner(tmp_path)
    for changes in ([quota.OwnerQuotaChange(tmp_path / 'ordinary', 1)],
                    [quota.OwnerQuotaChange(tmp_path / 'memory' / 'x', 1), quota.OwnerQuotaChange(tmp_path / 'ordinary', 0)]):
        with pytest.raises(quota.OwnerQuotaExceeded) as error:
            _check(enforcer, changes)
        assert error.value.projection.max_bytes == maximum
    headroom = maximum * 5 // 100
    target = tmp_path / 'memory' / 'x.md'
    with enforcer.admission() as admission:
        projection = admission.check([quota.OwnerQuotaChange(target, headroom)])
        assert (projection.used_bytes, projection.projected_bytes, projection.max_bytes) == (maximum, maximum + headroom, maximum)
        with target.open('wb') as handle:
            handle.truncate(headroom)
    assert quota._read_quota_ledger(tmp_path)['used_bytes'] == maximum + headroom
    with pytest.raises(quota.OwnerQuotaExceeded):
        _check(enforcer, [quota.OwnerQuotaChange(tmp_path / 'memory' / 'beyond.md', 1)])


# LLM: 100MiB验证5MiB比例上限，1000MiB验证32MiB绝对上限；每次写入真实结算并跨准入器复用同一账。
# 函数用途: 验证待对账额度累计耗尽、普通写拒绝及干净对账清零。
@pytest.mark.parametrize('mb', [100, 1000])
def test_pending_permanent_growth_is_cumulative_and_reconciliation_resets(tmp_path, mb):
    base = _quota_pending(tmp_path)
    maximum = mb * 1024 * 1024
    budget = min(32 * 1024 * 1024, maximum * 5 // 100)
    enforcer = quota.OwnerQuotaEnforcer(tmp_path, max_bytes=maximum)
    for changes in ([quota.OwnerQuotaChange(tmp_path / 'ordinary', 1)],
                    [quota.OwnerQuotaChange(tmp_path / 'memory' / 'mixed.md', 1),
                     quota.OwnerQuotaChange(tmp_path / 'ordinary', 0)]):
        with pytest.raises(quota.OwnerQuotaUnavailable) as error:
            _check(enforcer, changes)
        assert error.value.reason_code == 'reconciliation_pending'
        assert quota._read_quota_ledger(tmp_path)['permanent_pending_growth_bytes'] == 0
    _write(enforcer, tmp_path / 'memory' / 'x.md', 1024)
    assert quota._read_quota_ledger(tmp_path)['permanent_pending_growth_bytes'] == 1024
    _write(quota.OwnerQuotaEnforcer(tmp_path, max_bytes=maximum), tmp_path / 'memory' / 'rest.md', budget - 1024)
    ledger = quota._read_quota_ledger(tmp_path)
    assert ledger['permanent_pending_growth_bytes'] == budget
    assert ledger['used_bytes'] == base + budget and ledger['dirty_since'] is not None
    assert not ledger['reservations'] and ledger['reserved_bytes'] == 0
    for path in (tmp_path / 'memory' / 'beyond.md', tmp_path / 'ordinary'):
        with pytest.raises(quota.OwnerQuotaUnavailable) as error:
            _check(enforcer, [quota.OwnerQuotaChange(path, 1)])
        assert error.value.reason_code == 'reconciliation_pending'
        assert not path.exists()
    _reconcile(tmp_path)
    ledger = quota._read_quota_ledger(tmp_path)
    assert ledger['permanent_pending_growth_bytes'] == 0
    assert ledger['used_bytes'] == base + budget


# LLM: bool、负数及非整数计数不能恢复额度；不得利用坏计数无限增长。
# 函数用途: 验证待对账计数格式异常时拒绝永久数据增长。
@pytest.mark.parametrize('spent', [True, -1, None, '0'])
def test_invalid_pending_counter_refuses_growth(tmp_path, spent):
    _quota_pending(tmp_path)
    ledger = quota._read_quota_ledger(tmp_path)
    ledger['permanent_pending_growth_bytes'] = spent
    quota._write_quota_ledger(tmp_path, ledger)
    with pytest.raises(quota.OwnerQuotaUnavailable) as error:
        _check(quota.OwnerQuotaEnforcer(tmp_path, max_bytes=100 * 1024 * 1024), [
            quota.OwnerQuotaChange(tmp_path / 'memory' / 'x.md', 1)])
    assert error.value.reason_code == 'reconciliation_pending'


# LLM: 前台扫描及准入真实执行，只在观察状态输入标记失效，不能让冷账额度绕过失效拒绝。
# 函数用途: 验证同次快照失效时不开放永久数据待对账额度。
def test_invalidated_snapshot_refuses_permanent_pending_growth(tmp_path, monkeypatch):
    (tmp_path / 'memory').mkdir()
    _reconcile(tmp_path)
    original = quota._bounded_quota_snapshot

    # LLM: 返回真实扫描结果，但模拟扫描发现外部目录观察已失效的状态标记。
    # 函数用途: 提供明确的快照失效观察点。
    def invalidated(*args, **kwargs):
        result = original(*args, **kwargs)
        quota._QUOTA_SNAPSHOT_STATE.get()['metadata_invalidated'] = True
        return result

    monkeypatch.setattr(quota, '_bounded_quota_snapshot', invalidated)
    with pytest.raises(quota.OwnerQuotaUnavailable) as error:
        _check(quota.OwnerQuotaEnforcer(tmp_path, max_bytes=100 * 1024 * 1024), [
            quota.OwnerQuotaChange(tmp_path / 'memory' / 'x.md', 1)])
    assert error.value.reason_code == 'reconciliation_pending'
    assert quota._read_quota_ledger(tmp_path).get('permanent_pending_growth_bytes', 0) == 0


# LLM: 小目录真实前台完整快照必须清掉旧计数；微小测试配额没有额外最低余量。
# 函数用途: 验证前台清零及1字节配额边界。
def test_foreground_complete_snapshot_resets_counter_and_tiny_quota_is_hard(tmp_path):
    (tmp_path / 'memory').mkdir()
    _reconcile(tmp_path)
    ledger = quota._read_quota_ledger(tmp_path)
    ledger['permanent_pending_growth_bytes'] = 1234
    quota._write_quota_ledger(tmp_path, ledger)
    enforcer = quota.OwnerQuotaEnforcer(tmp_path, max_bytes=1)
    with pytest.raises(quota.OwnerQuotaExceeded):
        _check(enforcer, [quota.OwnerQuotaChange(tmp_path / 'memory' / 'x.md', 2)])
    _check(enforcer, [quota.OwnerQuotaChange(tmp_path / 'memory' / 'x.md', 1)])
    assert quota._read_quota_ledger(tmp_path)['permanent_pending_growth_bytes'] == 0


# LLM: 真实读取器只校验非负整数；无法表示为浮点比率的观察仍应未知，不改写或截断未来账本。
# 函数用途: 验证异常超大计数不会让只读压力探针崩溃。
def test_pressure_unrepresentable_ratio_is_unknown_and_preserves_ledger(tmp_path):
    (tmp_path / 'quota.json').write_text('{"max_disk_mb":1}')
    _reconcile(tmp_path)
    ledger = quota._read_quota_ledger(tmp_path)
    ledger['used_bytes'] = 10 ** 400
    quota._write_quota_ledger(tmp_path, ledger)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert quota.owner_quota_pressure(tmp_path) is None
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


@pytest.mark.parametrize('untrusted_ledger', [False, True])
def test_pending_permanent_growth_requires_existing_authoritative_baseline(tmp_path, untrusted_ledger):
    (tmp_path / 'memory').mkdir()
    for index in range(160):
        (tmp_path / f'item-{index}').write_bytes(b'x')
    with (tmp_path / 'external.bin').open('wb') as handle:
        handle.truncate(2 * 1024 * 1024)
    if untrusted_ledger:
        ledger = quota._read_quota_ledger(tmp_path)
        ledger['usage_authoritative'] = False
        quota._write_quota_ledger(tmp_path, ledger)
    target = tmp_path / 'memory' / 'new.md'
    enforcer = quota.OwnerQuotaEnforcer(tmp_path, max_bytes=1024 * 1024)
    with pytest.raises(quota.OwnerQuotaUnavailable, match='reconciliation pending'):
        _write(enforcer, target, 1)
    assert not target.exists()
    assert quota._read_quota_ledger(tmp_path).get('permanent_pending_growth_bytes', 0) == 0
    _reconcile(tmp_path)
    with pytest.raises(quota.OwnerQuotaExceeded):
        _write(enforcer, target, 1)
    assert not target.exists()
