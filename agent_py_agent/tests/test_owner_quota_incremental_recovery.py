# LLM: 只在临时owner执行真实配额锁、扫描与文件变更；禁用旧整树入口来证明冷启动不阻塞。
# 模块用途: 验证增量配额的预算、预留、异常结算和崩溃恢复。
import json

import pytest

from agent_py_agent.agent.user_space import owner_quota as quota


# LLM: API缺失用功能断言失败，不以ImportError冒充业务红测。
# 函数用途: 取得真实分片对账入口。
def _reconcile(root, **options):
    operation = getattr(quota, 'reconcile_owner_quota_slice', None)
    assert callable(operation), 'bounded quota reconciliation is missing'
    return operation(root, **options)


# LLM: 最多两项预算必须多拍完成，硬链接按路径计费且不跟随外部链接。
# 函数用途: 验证有界扫描与持久游标及最终统计。
def test_quota_reconcile_is_bounded_and_persistent(tmp_path):
    (tmp_path / 'nested').mkdir()
    for n in range(7):
        (tmp_path / 'nested' / str(n)).write_bytes(b'abc')
    (tmp_path / 'outside').symlink_to('/dev/null')
    reports = [_reconcile(tmp_path, max_entries=2, max_ms=1000)]
    for _ in range(30):
        if reports[-1]['complete']:
            break
        reports.append(_reconcile(tmp_path, max_entries=2, max_ms=1000))
    assert reports[0]['complete'] is False
    assert all(row['scanned_entries'] <= 2 for row in reports)
    assert reports[-1]['complete'] and reports[-1]['used_bytes'] == 21
    ledger = json.loads((tmp_path / '.owner-quota-ledger.json').read_text())
    assert ledger['inode_count'] == 7 and ledger['schema_generation'] == 1
    assert ledger['reconcile_cursor']['complete'] is True


# LLM: 旧整树扫描被禁止；小owner仍能真实写入并结算准确字节。
# 函数用途: 证明准入不再调用os.walk/native find热入口。
def test_quota_cold_write_never_calls_full_tree_usage(tmp_path, monkeypatch):
    monkeypatch.setattr(quota, '_owner_usage_bytes_for_admission', lambda *_: pytest.fail('unbounded foreground scan'))
    target = tmp_path / 'new'
    with quota.OwnerQuotaEnforcer(tmp_path, max_bytes=10).reserve([quota.OwnerQuotaChange(target, 8)]):
        ledger = json.loads((tmp_path / '.owner-quota-ledger.json').read_text())
        assert ledger['reserved_bytes'] == 8 and ledger['reservations']
        target.write_bytes(b'12345678')
    ledger = json.loads((tmp_path / '.owner-quota-ledger.json').read_text())
    assert ledger['used_bytes'] == 8 and ledger['reserved_bytes'] == 0 and not ledger['reservations']


# LLM: 失败可能已完成部分落盘，不能只按异常把预留当作零副作用撤销。
# 函数用途: 验证异常后按实际字节commit_delta。
def test_quota_partial_write_exception_settles_actual_delta(tmp_path):
    target = tmp_path / 'partial'
    with pytest.raises(OSError):
        with quota.OwnerQuotaEnforcer(tmp_path, max_bytes=20).reserve([quota.OwnerQuotaChange(target, 10)]):
            target.write_bytes(b'123')
            raise OSError('after partial write')
    ledger = json.loads((tmp_path / '.owner-quota-ledger.json').read_text())
    assert ledger['used_bytes'] == 3 and ledger['reserved_bytes'] == 0


# LLM: 超预算目录的未知用量不得授予增长；非增长修复仍有出口。
# 函数用途: 验证大冷目录快速拒绝与过额缩减。
def test_quota_large_cold_owner_refuses_growth_without_full_scan(tmp_path, monkeypatch):
    for n in range(160):
        (tmp_path / str(n)).write_bytes(b'x')
    monkeypatch.setattr(quota, '_owner_usage_bytes_for_admission', lambda *_: pytest.fail('full scan'))
    enforcer = quota.OwnerQuotaEnforcer(tmp_path, max_bytes=10000)
    with pytest.raises(quota.OwnerQuotaUnavailable):
        with enforcer.reserve([quota.OwnerQuotaChange(tmp_path / 'new', 1)]):
            pytest.fail('unknown usage permitted growth')
    with enforcer.reserve([quota.OwnerQuotaChange(tmp_path / '0', 0)]):
        (tmp_path / '0').write_bytes(b'')


# LLM: 更高代际账本不得覆盖；损坏账本也不能重置为零。
# 函数用途: 验证持久账本的R1与损坏关闭式拒绝。
@pytest.mark.parametrize('raw', ['{"schema_generation":2}', '{broken'])
def test_quota_invalid_ledger_is_not_reset(tmp_path, raw):
    path = tmp_path / '.owner-quota-ledger.json'
    path.write_text(raw)
    with pytest.raises((quota.OwnerQuotaUnavailable, RuntimeError, ValueError)):
        _reconcile(tmp_path, max_entries=2, max_ms=10)
    assert path.read_text() == raw


# LLM: 显式结算后退出context不可再计一次差额。
# 函数用途: 验证commit_delta可重入调用的幂等性。
def test_quota_explicit_settlement_is_idempotent(tmp_path):
    target = tmp_path / 'a'
    with quota.OwnerQuotaEnforcer(tmp_path, max_bytes=20).admission() as admission:
        admission.check([quota.OwnerQuotaChange(target, 5)])
        target.write_bytes(b'12345')
        admission.commit_delta()
    assert json.loads((tmp_path / '.owner-quota-ledger.json').read_text())['used_bytes'] == 5


# LLM: 多次检查必须以最后鲜活快照为账本基线，不能重复加前次已写字节。
# 函数用途: 验证同一准入里分阶段写入的最终准确计数。
def test_quota_repeated_check_does_not_double_count(tmp_path):
    first, second = tmp_path / 'a', tmp_path / 'b'
    with quota.OwnerQuotaEnforcer(tmp_path, max_bytes=20).admission() as admission:
        admission.check([quota.OwnerQuotaChange(first, 3)])
        first.write_bytes(b'123')
        admission.check([quota.OwnerQuotaChange(second, 4)])
        second.write_bytes(b'1234')
    assert json.loads((tmp_path / '.owner-quota-ledger.json').read_text())['used_bytes'] == 7


# LLM: 配额控制账不是用户可用的免费payload写口。
# 函数用途: 验证结构化用户写入不能覆盖账本或原配额锁。
@pytest.mark.parametrize('name', ['.owner-quota-ledger.json', '.owner-quota.lock'])
def test_quota_control_metadata_is_not_a_payload_target(tmp_path, name):
    with pytest.raises((ValueError, quota.OwnerQuotaUnavailable)):
        with quota.OwnerQuotaEnforcer(tmp_path, max_bytes=20).reserve([quota.OwnerQuotaChange(tmp_path / name, 5)]):
            pytest.fail('quota metadata permitted as free payload')


# LLM: 清理过期预留前必须完成新扫描；进程重启无法复用旧生成器。
# 函数用途: 验证遗留预留与游标丢失后的重新扫描、字节回收。
def test_quota_orphan_reservation_recovered_only_after_complete_scan(tmp_path):
    (tmp_path / 'data').write_bytes(b'hello')
    _reconcile(tmp_path, max_entries=1, max_ms=1000)
    path = tmp_path / '.owner-quota-ledger.json'
    ledger = json.loads(path.read_text())
    ledger.update(reservations={'dead': {'bytes': 100, 'expires_at': 1}}, reserved_bytes=100)
    path.write_text(json.dumps(ledger))
    for value in quota._QUOTA_SCANS.values():
        value['iterator'].close()
    quota._QUOTA_SCANS.clear()
    result = _reconcile(tmp_path, max_entries=1, max_ms=1000)
    assert result['complete'] is False
    assert json.loads(path.read_text())['reserved_bytes'] == 100
    for _ in range(20):
        result = _reconcile(tmp_path, max_entries=1, max_ms=1000)
        if result['complete']:
            break
    assert result['complete'] and result['used_bytes'] == 5
    assert json.loads(path.read_text())['reserved_bytes'] == 0


# LLM: inode_count必须在commit时更新，不能把上次扫描计数冒充当前账。
# 函数用途: 验证新增和删除文件的即时inode结算。
def test_quota_inode_count_tracks_committed_creation_and_removal(tmp_path):
    target = tmp_path / 'a'
    enforcer = quota.OwnerQuotaEnforcer(tmp_path, max_bytes=20)
    with enforcer.reserve([quota.OwnerQuotaChange(target, 3)]):
        target.write_bytes(b'123')
    ledger = tmp_path / '.owner-quota-ledger.json'
    assert json.loads(ledger.read_text())['inode_count'] == 1
    with enforcer.reserve([quota.OwnerQuotaChange(target, None)]):
        target.unlink()
    assert json.loads(ledger.read_text())['inode_count'] == 0


# LLM: 硬链接逐路径计费但原地扩写会影响多个别名，不允许只预留一个路径的增量。
# 函数用途: 验证多链接目标增长被明确拒绝。
def test_quota_hardlinked_growth_does_not_underreserve(tmp_path, monkeypatch):
    import os
    target = tmp_path / 'a'
    target.write_bytes(b'12')
    original = type(target).stat

    # LLM: 本机link系统调用明确EPERM；只替换目标的nlink元数据，不替换配额判定或结算。
    # 函数用途: 为拒绝分支提供双链接文件元数据，真实硬链接行为仍需外部车道复核。
    def linked_stat(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == target:
            fields = list(info)
            fields[3] = 2
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(type(target), 'stat', linked_stat)
    with pytest.raises(quota.OwnerQuotaUnavailable):
        with quota.OwnerQuotaEnforcer(tmp_path, max_bytes=9).reserve([quota.OwnerQuotaChange(target, 5)]):
            pytest.fail('hardlink aliases were not charged')


# LLM: 每个暂停的递归生成器可能持有多层目录FD，不能只按32个owner限制而积累几千FD。
# 函数用途: 验证跨owner切换先关闭旧扫描，进程只保留一个后台扫描栈。
def test_quota_scan_cache_evicts_and_closes_previous_stack(tmp_path):
    first, second = tmp_path / 'one', tmp_path / 'two'
    for root in (first, second):
        (root / 'nested' / 'deep').mkdir(parents=True)
        (root / 'nested' / 'deep' / 'data').write_bytes(b'abc')
    _reconcile(first, max_entries=1, max_ms=1000)
    old = next(value['iterator'] for key, value in quota._QUOTA_SCANS.items() if key[1] == str(first))
    _reconcile(second, max_entries=1, max_ms=1000)
    assert len(quota._QUOTA_SCANS) <= 1
    assert old.gi_frame is None


# LLM: 深度截断和真实目录打开故障都不能发布complete或清除旧预留；异常须释放扫描栈。
# 函数用途: 验证不完整对账严格关闭式失败，保留最后持久回执。
@pytest.mark.parametrize('failure', ['depth', 'open'])
def test_quota_scan_error_keeps_reservation_and_closes_stack(tmp_path, monkeypatch, failure):
    _reconcile(tmp_path, max_entries=1, max_ms=1000)
    path = tmp_path / '.owner-quota-ledger.json'
    ledger = json.loads(path.read_text())
    ledger.update(reservations={'dead': {'bytes': 5, 'expires_at': 1}}, reserved_bytes=5,
                  revision=ledger['revision'] + 1)
    path.write_text(json.dumps(ledger))
    if failure == 'depth':
        directory = tmp_path
        for _ in range(66):
            directory = directory / 'd'
            directory.mkdir()
        (directory / 'data').write_bytes(b'abc')
    else:
        (tmp_path / 'bad').mkdir()
        original = quota.open_directory_beneath

        # LLM: 只在真实遍历将进入子目录时注入EACCES，不替换扫描结果或账本。
        # 函数用途: 模拟中途权限故障。
        def denied(root, parts, **kwargs):
            if parts == ('bad',):
                raise PermissionError('directory denied')
            return original(root, parts, **kwargs)

        monkeypatch.setattr(quota, 'open_directory_beneath', denied)
    with pytest.raises((quota.OwnerQuotaUnavailable, PermissionError)):
        _reconcile(tmp_path, max_entries=1000, max_ms=1000)
    assert json.loads(path.read_text()) == ledger
    assert all(key[1] != str(tmp_path) for key in quota._QUOTA_SCANS)
