# LLM: 只在临时owner执行真实配额锁、扫描与文件变更；禁用旧整树入口来证明冷启动不阻塞。
# 模块用途: 验证增量配额的预算、预留、异常结算和崩溃恢复。
import json
import os
import shutil
import time
from types import SimpleNamespace

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


# LLM: 多owner缓存必须有界且按LRU关闭淘汰迭代器；两个owner不能相互淘汰。
# 函数用途: 验证第十七个owner到来后最早扫描栈被关闭。
def test_quota_scan_cache_evicts_and_closes_previous_stack(tmp_path):
    for value in quota._QUOTA_SCANS.values():
        value['iterator'].close()
    quota._QUOTA_SCANS.clear()
    first, second = tmp_path / 'one', tmp_path / 'two'
    for root in (first, second):
        (root / 'nested' / 'deep').mkdir(parents=True)
        (root / 'nested' / 'deep' / 'data').write_bytes(b'abc')
    _reconcile(first, max_entries=1, max_ms=1000)
    old = next(value['iterator'] for key, value in quota._QUOTA_SCANS.items() if key[1] == str(first))
    _reconcile(second, max_entries=1, max_ms=1000)
    assert old.gi_frame is not None
    for index in range(15):
        root = tmp_path / f'extra-{index}'
        root.mkdir()
        (root / 'data').write_bytes(b'x')
        _reconcile(root, max_entries=1, max_ms=1000)
    assert len(quota._QUOTA_SCANS) == 16
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


# LLM: 两个真实owner交替不能重置epoch；清空缓存模拟新进程仍须恢复原持久路径与部分合计。
# 函数用途: 固定复现A/B各300字节在第二轮完成的收敛合同。
@pytest.mark.parametrize('clear_cache', [False, True])
def test_alternating_owners_resume_and_converge_second_round(tmp_path, clear_cache):
    roots = [tmp_path / name for name in ('A', 'B')]
    for root in roots:
        root.mkdir()
        for index in range(300):
            (root / f'{index:03d}').write_bytes(b'x')
    first = [_reconcile(root, max_entries=256, max_ms=1000) for root in roots]
    assert all(not row['complete'] for row in first)
    if clear_cache:
        for scan in quota._QUOTA_SCANS.values():
            scan['iterator'].close()
        quota._QUOTA_SCANS.clear()
    second = [_reconcile(root, max_entries=256, max_ms=1000) for root in roots]
    for before, result in zip(first, second, strict=True):
        assert result['complete'] is True
        assert result['used_bytes'] == result['inode_count'] == 300
        assert result['cursor']['epoch'] == before['cursor']['epoch']
        assert result['cursor']['scanned'] > before['cursor']['scanned']
        assert result['cursor']['last_relpath'] > before['cursor']['last_relpath']


# LLM: 相对路径顺序包含斜杠与目录同前缀文件，重启后不能重复累计或跳过同前缀子树。
# 函数用途: 验证嵌套目录的字典序恢复与持久部分合计。
def test_restart_cursor_tracks_nested_lexical_paths(tmp_path):
    for name in ('a', 'a-'):
        (tmp_path / name).mkdir()
        (tmp_path / name / 'inside').write_bytes(b'ab')
    (tmp_path / 'a.').write_bytes(b'xyz')
    result = _reconcile(tmp_path, max_entries=2, max_ms=1000)
    epoch = result['cursor']['epoch']
    for _ in range(20):
        if result['complete']:
            break
        for scan in quota._QUOTA_SCANS.values():
            scan['iterator'].close()
        quota._QUOTA_SCANS.clear()
        result = _reconcile(tmp_path, max_entries=2, max_ms=1000)
        assert result['cursor']['epoch'] == epoch
    assert result['complete']
    assert (result['used_bytes'], result['inode_count']) == (7, 3)


# LLM: 排序准备也必须可暂停，不能先枚举完整大目录再假称只扫描一个项；每次原始枚举都有预算。
# 函数用途: 统计真实scandir取项，复现小预算背后的无界排序工作。
def test_sort_preparation_is_budgeted_and_resumable(tmp_path, monkeypatch):
    for index in range(1400):
        (tmp_path / f'{index:04d}').write_bytes(b'x')
    original = quota._quota_scandir
    visited = []

    # LLM: 包装真实scandir且保留context退出关闭；不篡改顺序或扫描结果。
    # 类用途: 精确记录排序准备实际读取多少目录项。
    class CountingScan:
        # LLM: 只保存真实迭代器，关闭仍交原context负责。
        # 函数用途: 初始化取项计数器。
        def __init__(self, fd):
            self.scan = original(fd)

        # LLM: 返回惰性包装器，不预先枚举整个目录。
        # 函数用途: 进入真实扫描context。
        def __enter__(self):
            self.scan.__enter__()
            return self

        # LLM: 关闭真实scandir，异常也不能泄漏FD。
        # 函数用途: 退出扫描context。
        def __exit__(self, *args):
            return self.scan.__exit__(*args)

        # LLM: 迭代不创建任何额外目录内容。
        # 函数用途: 返回自身迭代协议。
        def __iter__(self):
            return self

        # LLM: 每次仅转发一个真实目录项并记账，不替换业务元数据。
        # 函数用途: 观察实际枚举工作量。
        def __next__(self):
            entry = next(self.scan)
            visited.append(entry.name)
            return entry

    monkeypatch.setattr(quota, '_quota_scandir', CountingScan)
    first = _reconcile(tmp_path, max_entries=1, max_ms=1000)
    assert len(visited) <= 1024
    assert first['complete'] is False and first['scanned_entries'] <= 1
    assert first['enumerated_entries'] == len(visited)
    second = _reconcile(tmp_path, max_entries=256, max_ms=1000)
    assert second['cursor']['epoch'] == first['cursor']['epoch']
    assert second['cursor']['scanned'] > first['cursor']['scanned']


# LLM: 大目录排序准备阶段若发生跨进程重启或LRU缓存淘汰，必须从持久prep状态有界继续推进并最终收敛。
# 函数用途: 覆盖1400文件每片清缓存（含透明scandir枚举预算守恒）及17个owner超过LRU容量的完整收敛合同。
@pytest.mark.parametrize("cookie_block", [None, 1363], ids=["native", "linux-buffered-cookie"])
def test_large_directory_sort_preparation_converges_across_cache_eviction(tmp_path, monkeypatch, cookie_block):
    if cookie_block is not None:
        _buffer_directory_cookies(monkeypatch, cookie_block)
    original_scandir = quota._quota_scandir
    raw_scandir_count = [0]

    class TransparentCountingScan:
        def __init__(self, fd):
            self.it = original_scandir(fd)

        def __enter__(self):
            self.it.__enter__()
            return self

        def __exit__(self, *args):
            return self.it.__exit__(*args)

        def __iter__(self):
            return self

        def __next__(self):
            entry = next(self.it)
            raw_scandir_count[0] += 1
            return entry

    monkeypatch.setattr(quota, '_quota_scandir', TransparentCountingScan)
    roots = [tmp_path / f'owner-{idx:02d}' for idx in range(2)]
    for root in roots:
        root.mkdir()
        assert _reconcile(root, max_entries=256, max_ms=1000)['complete']
        for index in range(1400):
            (root / f'{index:04d}').write_bytes(b'x')
    epochs, progress = {}, {}
    for _ in range(30):
        all_done = True
        for root in roots:
            for scan in quota._QUOTA_SCANS.values():
                scan['iterator'].close()
            quota._QUOTA_SCANS.clear()
            raw_scandir_count[0] = 0
            before = json.loads((root / '.owner-quota-ledger.json').read_text())['reconcile_cursor']
            skip = before.get('prep', {}).get('block_skip', 0)
            allowance = skip if skip >= 1024 else 0
            result = _reconcile(root, max_entries=256, max_ms=1000)
            assert raw_scandir_count[0] == result['enumerated_entries']
            assert raw_scandir_count[0] <= 1024 + allowance
            assert result['replay_allowance'] == allowance
            epochs.setdefault(root, result['cursor']['epoch'])
            assert result['cursor']['epoch'] == epochs[root]
            if not result['complete']:
                all_done = False
                cursor = result['cursor']
                point = (cursor['last_relpath'], cursor.get('prep', {}).get('enumerated', 0))
                assert root not in progress or point > progress[root]
                progress[root] = point
        if all_done:
            break
    for root in roots:
        ledger = json.loads((root / '.owner-quota-ledger.json').read_text())
        assert ledger['reconcile_cursor']['complete'] is True
        assert ledger['used_bytes'] == ledger['inode_count'] == 1400

    lru_roots = [tmp_path / f'lru-{idx:02d}' for idx in range(17)]
    for root in lru_roots:
        root.mkdir()
        assert _reconcile(root, max_entries=256, max_ms=1000)['complete']
        for index in range(1200):
            (root / f'{index:04d}').write_bytes(b'x')
    lru_epochs = {}
    for _ in range(25):
        all_done = True
        for root in lru_roots:
            raw_scandir_count[0] = 0
            before = json.loads((root / '.owner-quota-ledger.json').read_text())['reconcile_cursor']
            skip = before.get('prep', {}).get('block_skip', 0)
            allowance = skip if skip >= 1024 else 0
            result = _reconcile(root, max_entries=256, max_ms=1000)
            assert raw_scandir_count[0] == result['enumerated_entries']
            assert raw_scandir_count[0] <= 1024 + allowance
            lru_epochs.setdefault(root, result['cursor']['epoch'])
            assert result['cursor']['epoch'] == lru_epochs[root]
            if not result['complete']:
                all_done = False
        if all_done:
            break
    for root in lru_roots:
        ledger = json.loads((root / '.owner-quota-ledger.json').read_text())
        assert ledger['reconcile_cursor']['complete'] is True
        assert ledger['used_bytes'] == ledger['inode_count'] == 1200


@pytest.mark.parametrize("evict", [False, True], ids=["retained-control", "evicted-contract"])
@pytest.mark.parametrize("tick_seconds", [0.00004, 0.025], ids=["default-slice", "already-expired"])
def test_buffered_cookie_progress_with_default_time_slice(tmp_path, monkeypatch, evict, tick_seconds):
    from types import SimpleNamespace

    _buffer_directory_cookies(monkeypatch, 1363)
    root = tmp_path / "quota"
    root.mkdir()
    assert _reconcile(root, max_entries=256, max_ms=1000)["complete"]
    for index in range(1400):
        (root / f"{index:04d}").write_bytes(b"x")
    result = _reconcile(root, max_entries=256, max_ms=1000)
    assert result["cursor"]["prep"]["block_skip"] == 1024
    epoch = result["cursor"]["epoch"]
    real_time = quota.time
    ticks = [0.0]

    def monotonic():
        ticks[0] += tick_seconds
        return ticks[0]

    monkeypatch.setattr(quota, "time", SimpleNamespace(time=real_time.time, monotonic=monotonic))
    try:
        for _ in range(5):
            before = result["cursor"]
            point = (before["last_relpath"], before.get("prep", {}).get("enumerated", 0))
            skip = before.get("prep", {}).get("block_skip", 0) if evict else 0
            allowance = skip if skip >= 1024 else 0
            if evict:
                for current in quota._QUOTA_SCANS.values():
                    current["iterator"].close()
                quota._QUOTA_SCANS.clear()
            result = _reconcile(root)  # 不提高默认的 128 项 / 20ms 预算。
            cursor = result["cursor"]
            assert cursor["epoch"] == epoch
            assert (cursor["last_relpath"], cursor.get("prep", {}).get("enumerated", 0)) > point
            assert result["replay_allowance"] == allowance
            assert result["enumerated_entries"] <= 1024 + allowance
            assert result["scanned_entries"] <= 128
            persisted = json.loads((root / ".owner-quota-ledger.json").read_text())
            assert persisted["reconcile_cursor"] == cursor
            if result["complete"]:
                assert result["used_bytes"] == result["inode_count"] == 1400
                break
    finally:
        for current in quota._QUOTA_SCANS.values():
            current["iterator"].close()
        quota._QUOTA_SCANS.clear()


@pytest.mark.parametrize("block,files,replay_target", [(1363, 1400, 1024), (5000, 5200, 3072)])
def test_buffered_cookie_replay_gets_full_fresh_time_budget(tmp_path, monkeypatch, block, files, replay_target):
    from types import SimpleNamespace

    _buffer_directory_cookies(monkeypatch, block)
    root = tmp_path / "fresh-slice"
    root.mkdir()
    assert _reconcile(root, max_entries=256, max_ms=1000)["complete"]
    for index in range(files):
        (root / f"{index:05d}").write_bytes(b"x")
    result = _reconcile(root, max_entries=256, max_ms=1000)
    while result["cursor"]["prep"]["block_skip"] < replay_target:
        result = _reconcile(root, max_entries=256, max_ms=1000)
    before = result["cursor"]
    skip = before["prep"]["block_skip"]
    for scan in quota._QUOTA_SCANS.values():
        scan["iterator"].close()
    quota._QUOTA_SCANS.clear()
    original_scan, original_time = quota._quota_scandir, quota.time
    clock = [0.0]

    class TimedScan:
        def __init__(self, fd):
            self.scan = original_scan(fd)

        def __enter__(self):
            self.scan.__enter__()
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return self.scan.__exit__(exc_type, exc_val, exc_tb)

        def __iter__(self):
            return self

        def __next__(self):
            entry = next(self.scan)
            clock[0] += 0.001  # 枚举本身消耗时间，不能只模拟 deadline 检查开销。
            return entry

    monkeypatch.setattr(quota, "_quota_scandir", TimedScan)
    monkeypatch.setattr(quota, "time", SimpleNamespace(time=original_time.time, monotonic=lambda: clock[0]))
    try:
        result = _reconcile(root, max_entries=256, max_ms=10)
        fresh = result["enumerated_entries"] - skip
        assert 5 <= fresh <= 11, (skip, fresh)
        assert result["enumerated_entries"] <= 1024 + result["replay_allowance"]
        assert result["scanned_entries"] <= 256
        cursor = result["cursor"]
        assert cursor["epoch"] == before["epoch"]
        assert cursor["prep"]["enumerated"] > before["prep"]["enumerated"]
        assert json.loads((root / ".owner-quota-ledger.json").read_text())["reconcile_cursor"] == cursor
    finally:
        for scan in quota._QUOTA_SCANS.values():
            scan["iterator"].close()
        quota._QUOTA_SCANS.clear()


def _buffer_directory_cookies(monkeypatch, block_size):
    """真实 DirEntry/统计不变，仅模拟 glibc 整块预读期间保持不变的目录 cookie。"""
    import stat

    real_scan, real_open, real_seek = quota._quota_scandir, quota.open_directory_beneath, quota.os.lseek
    states = {}
    cookie_base = 9030876314358498251

    def open_dir(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        states[fd] = {"cookie": 0}
        return fd

    def seek(fd, offset, whence):
        if fd not in states or not stat.S_ISDIR(quota.os.fstat(fd).st_mode):
            return real_seek(fd, offset, whence)
        if whence == quota.os.SEEK_SET:
            assert offset == 0 or offset >= cookie_base
            states[fd]["cookie"] = offset
        else:
            assert whence == quota.os.SEEK_CUR and offset == 0
        return states[fd]["cookie"]

    class BufferedScan:
        def __init__(self, fd):
            with real_scan(fd) as rows:
                self.rows = sorted(rows, key=lambda row: row.name)
            self.state = states[fd]
            cookie = self.state["cookie"]
            self.index = 0 if cookie == 0 else cookie - cookie_base

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def __iter__(self):
            return self

        def __next__(self):
            if self.index >= len(self.rows):
                raise StopIteration
            row = self.rows[self.index]
            self.index += 1
            end = min(((self.index - 1) // block_size + 1) * block_size, len(self.rows))
            self.state["cookie"] = cookie_base + end
            return row

    def scan(fd):
        return BufferedScan(fd) if fd in states else real_scan(fd)

    monkeypatch.setattr(quota, 'open_directory_beneath', open_dir)
    monkeypatch.setattr(quota.os, 'lseek', seek)
    monkeypatch.setattr(quota, '_quota_scandir', scan)


@pytest.mark.parametrize('files', [256, 512, 1024, 2048, 4096])
@pytest.mark.parametrize('clear_cache', [False, True])
def test_reconciliation_enumerates_flat_directory_once(tmp_path, files, clear_cache, monkeypatch):
    for index in range(files):
        (tmp_path / f'{index:05d}').write_bytes(b'x')
    original = quota._quota_scandir
    enumerated = [0]

    class CountingScan:
        def __init__(self, fd):
            self.scan = original(fd)

        def __enter__(self):
            self.scan.__enter__()
            return self

        def __exit__(self, *args):
            return self.scan.__exit__(*args)

        def __iter__(self):
            return self

        def __next__(self):
            entry = next(self.scan)
            enumerated[0] += 1
            return entry

        def __getattr__(self, name):
            return getattr(self.scan, name)

    monkeypatch.setattr(quota, '_quota_scandir', CountingScan)
    reports = []
    for _ in range(150):
        if clear_cache:
            for current in quota._QUOTA_SCANS.values():
                current['iterator'].close()
            quota._QUOTA_SCANS.clear()
        reports.append(_reconcile(tmp_path, max_entries=128, max_ms=20))
        assert reports[-1]['scanned_entries'] <= 128
        if reports[-1]['complete']:
            break
    assert reports[-1]['complete'], reports[-1]
    assert reports[-1]['used_bytes'] == reports[-1]['inode_count'] == files
    assert enumerated[0] == sum(row['enumerated_entries'] for row in reports)
    assert enumerated[0] <= files + 5, (files, enumerated[0])


@pytest.mark.parametrize('clear_cache', [False, True])
def test_reconcile_many_subdirectories_prunes_empty_batches_and_converges(tmp_path, clear_cache):
    for index in range(100):
        directory = tmp_path / f'd{index:03d}'
        directory.mkdir()
        for name in ('a', 'b'):
            (directory / name).write_bytes(b'x')
    epoch = None
    for turn in range(10):
        if clear_cache:
            for scan in quota._QUOTA_SCANS.values():
                scan['iterator'].close()
            quota._QUOTA_SCANS.clear()
        result = _reconcile(tmp_path, max_entries=128, max_ms=1000)
        cursor = result['cursor']
        epoch = epoch or cursor['epoch']
        assert cursor['epoch'] == epoch
        batches = cursor.get('dir_batch', {})
        assert len(batches) <= 65
        assert all(batch['names'] and (not rel or cursor['last_relpath'].startswith(rel + '/'))
                   for rel, batch in batches.items())
        if result['complete']:
            break
    assert result['complete'] and turn + 1 <= 4
    assert result['used_bytes'] == result['inode_count'] == 200
    assert result['cursor']['metadata_complete'] is True


def test_reconcile_long_filenames_cold_eviction_stays_within_ledger_cap(tmp_path, monkeypatch):
    for index in range(5000):
        (tmp_path / (f'{index:05d}-' + 'a' * 179)).write_bytes(b'')
    original_checkpoint = quota._QuotaDirectoryStream.checkpoint
    checkpoints = []

    def checkpoint(stream):
        checkpoints.append(True)
        return original_checkpoint(stream)

    monkeypatch.setattr(quota._QuotaDirectoryStream, 'checkpoint', checkpoint)
    point, epoch = (-1, '', -1), None
    for _ in range(6):
        for scan in quota._QUOTA_SCANS.values():
            scan['iterator'].close()
        quota._QUOTA_SCANS.clear()
        checkpoints.clear()
        result = _reconcile(tmp_path, max_entries=256, max_ms=1000)
        cursor = result['cursor']
        epoch = epoch or cursor['epoch']
        assert cursor['epoch'] == epoch
        assert (tmp_path / '.owner-quota-ledger.json').stat().st_size < 512 * 1024
        # 排序准备也受预算约束：前几片推进枚举续点，准备完成后才推进计费路径。
        current = (cursor['scanned'], cursor['last_relpath'], cursor.get('prep', {}).get('enumerated', 0))
        assert current > point
        point = current
        assert len(checkpoints) <= 1
        names = cursor.get('prep', {}).get('names', [])
        names += [name for batch in cursor.get('dir_batch', {}).values() for name in batch['names']]
        assert len(names) <= 4096
        assert sum(len(name.encode('utf-8')) for name in names) <= 196608
    assert cursor['scanned'] > 0 and cursor['last_relpath']


@pytest.mark.parametrize('nested', [False, True])
def test_bounded_name_windows_finish_after_overflow_and_cold_truncation(tmp_path, nested):
    roots = [tmp_path]
    if nested:
        child = tmp_path / '!child'
        child.mkdir()
        roots.append(child)
    for root in roots:
        for index in range(1200):
            (root / (f'{index:05d}-' + '汉' * 70)).write_bytes(b'x')
    for _ in range(60):
        for scan in quota._QUOTA_SCANS.values():
            scan['iterator'].close()
        quota._QUOTA_SCANS.clear()
        result = _reconcile(tmp_path, max_entries=256, max_ms=1000)
        assert (tmp_path / '.owner-quota-ledger.json').stat().st_size < 1_048_576
        if result['complete']:
            break
    assert result['complete']
    assert result['used_bytes'] == result['inode_count'] == 1200 * len(roots)
    assert quota._verify_quota_metadata(tmp_path, result['cursor'])


def test_reconcile_thirty_thousand_files_uses_compact_metadata(tmp_path):
    for index in range(30000):
        (tmp_path / f'{index:05d}').write_bytes(b'x')
    for _ in range(80):
        for scan in quota._QUOTA_SCANS.values():
            scan['iterator'].close()
        quota._QUOTA_SCANS.clear()
        result = _reconcile(tmp_path, max_entries=4096, max_ms=1000)
        assert (tmp_path / '.owner-quota-ledger.json').stat().st_size < 512 * 1024
        if result['complete']:
            break
    assert result['complete'] and result['used_bytes'] == result['inode_count'] == 30000
    assert result['cursor']['metadata_complete'] is True
    assert set(result['cursor']['dir_stamps']) == {''}
    assert 'file_stamps' not in result['cursor'] and 'dir_names' not in result['cursor']
    assert quota._verify_quota_metadata(tmp_path, result['cursor'])


def test_legacy_partial_metadata_is_rebuilt_without_trusting_four_field_stamps(tmp_path):
    for index in range(160):
        (tmp_path / f'{index:04d}').write_bytes(b'x')
    result = _reconcile(tmp_path, max_entries=32, max_ms=1000)
    assert not result['complete']
    path = tmp_path / '.owner-quota-ledger.json'
    ledger = json.loads(path.read_text())
    cursor = ledger['reconcile_cursor']
    cursor['dir_stamps'] = {rel: stamp[:4] for rel, stamp in cursor['dir_stamps'].items()}
    cursor.update(file_stamps={}, dir_names={'': {}})
    path.write_text(json.dumps(ledger))
    for scan in quota._QUOTA_SCANS.values():
        scan['iterator'].close()
    quota._QUOTA_SCANS.clear()
    for _ in range(10):
        result = _reconcile(tmp_path, max_entries=32, max_ms=1000)
        if result['complete']:
            break
    assert result['complete'] and result['used_bytes'] == result['inode_count'] == 160
    assert result['cursor']['metadata_complete'] is True
    assert quota._verify_quota_metadata(tmp_path, result['cursor'])


# LLM: 每个用例从空的进程内扫描缓存开始，别的用例留下的热扫描器不能影响续扫路径。
# 函数用途: 关闭并清空进程内配额扫描器。
def _r7_clear_scans():
    for scan in quota._QUOTA_SCANS.values():
        scan['iterator'].close()
    quota._QUOTA_SCANS.clear()


# 函数用途: 用给定额度把对账推进到完成并返回最后一片回执。
def _r7_finish(root, *, max_entries=4096, max_ms=5000):
    for _ in range(500):
        report = quota.reconcile_owner_quota_slice(root, max_entries=max_entries, max_ms=max_ms)
        if report['complete']:
            return report
    pytest.fail('quota reconciliation did not finish')


# LLM: 走真实reserve与原文件追加；超额与对账待定分别返回，便于断言被拦下的原因。
# 函数用途: 受控追加一个字节并返回ok/exceeded/refused。
def _r7_grow(root, name='managed', *, max_bytes=1 << 30):
    enforcer = quota.OwnerQuotaEnforcer(root, max_bytes=max_bytes)
    try:
        with enforcer.reserve([quota.OwnerQuotaChange(root / name, 1, append=True)]):
            with (root / name).open('ab') as stream:
                stream.write(b'y')
    except quota.OwnerQuotaExceeded:
        return 'exceeded'
    except quota.OwnerQuotaUnavailable:
        return 'refused'
    return 'ok'


# 函数用途: 读取持久配额账。
def _r7_ledger(root):
    return json.loads((root / '.owner-quota-ledger.json').read_text())


# LLM: 只包装os.scandir返回的目录项，统计逐项lstat次数；目录项与结果都来自真实系统调用。
# 函数用途: 安装逐项stat计数器并返回计数字典。
def _r7_count_entry_stats(monkeypatch):
    counts = {'stat': 0}
    raw = os.scandir

    # 类用途: 转发目录项并统计stat调用。
    class Entry:
        def __init__(self, entry):
            self.entry = entry

        def __getattr__(self, key):
            return getattr(self.entry, key)

        def stat(self, **kwargs):
            counts['stat'] += 1
            return self.entry.stat(**kwargs)

    # 类用途: 转发目录流，逐项包装。
    class Scan:
        def __init__(self, scan):
            self.scan = scan

        def __enter__(self):
            self.scan.__enter__()
            return self

        def __exit__(self, *args):
            return self.scan.__exit__(*args)

        def __iter__(self):
            return self

        def __next__(self):
            return Entry(next(self.scan))

        def close(self):
            self.scan.close()

    monkeypatch.setattr(os, 'scandir', lambda *args, **kwargs: Scan(raw(*args, **kwargs)))
    return counts


# LLM: R6-QUOTA-01：5000个空锁不计费但都是物理目录项；1字节追加的前台lstat不得随空锁数增长。
# 函数用途: 验证基线已记录大量空锁名时，准入与结算的逐项stat不超过一份核验额度。
@pytest.mark.parametrize('where', ['root', 'subdir'])
def test_empty_lock_names_keep_foreground_stat_work_bounded(tmp_path, monkeypatch, where):
    _r7_clear_scans()
    base = tmp_path if where == 'root' else tmp_path / 'locks'
    base.mkdir(exist_ok=True)
    for index in range(200):
        (base / f'p-{index:04d}').write_bytes(b'x')
    for index in range(5000):
        (base / f'{index:05d}.lock').touch()
    _r7_finish(tmp_path)
    counts = _r7_count_entry_stats(monkeypatch)
    assert _r7_grow(tmp_path, str((base / 'p-0000').relative_to(tmp_path))) == 'ok'
    assert counts['stat'] <= quota._QUOTA_VERIFY_BUDGET_ENTRIES
    ledger = _r7_ledger(tmp_path)
    assert ledger['dirty_since'] is None
    assert (ledger['used_bytes'], ledger['inode_count']) == (201, 200)


# LLM: 基线记录时目录还小，之后准入外堆进5000个空锁：前台逐项核验数到额度即停，转名字计数核计费项，不扫完全部目录项。
# 函数用途: 验证前台在扫描中发现物理项超额时立即停下且不误判脏账。
def test_lock_names_added_after_baseline_stop_foreground_scan_at_budget(tmp_path, monkeypatch):
    _r7_clear_scans()
    for index in range(200):
        (tmp_path / f'p-{index:04d}').write_bytes(b'x')
    _r7_finish(tmp_path)
    for index in range(5000):
        (tmp_path / f'{index:05d}.lock').touch()
    counts = _r7_count_entry_stats(monkeypatch)
    assert _r7_grow(tmp_path, 'p-0000') == 'ok'
    assert counts['stat'] <= quota._QUOTA_VERIFY_BUDGET_ENTRIES
    assert _r7_ledger(tmp_path)['dirty_since'] is None
    monkeypatch.undo()
    _r7_clear_scans()
    report = _r7_finish(tmp_path)
    assert (report['used_bytes'], report['inode_count']) == (201, 200)


# LLM: R6-QUOTA-02：结算的inode增减必须与扫描同一排除口径（0字节*.lock不计、有内容即计）。
# 函数用途: 验证三种锁文件转变后结算账与随后完整对账一致。
@pytest.mark.parametrize('transition', ['empty_to_nonempty', 'nonempty_to_empty', 'create_empty'])
@pytest.mark.parametrize('nested', [False, True])
def test_lock_file_settlement_matches_reconcile(tmp_path, transition, nested):
    _r7_clear_scans()
    for index in range(200):
        (tmp_path / f'p-{index:04d}').write_bytes(b'x')
    target = tmp_path / 'sub' / 'target.lock' if nested else tmp_path / 'target.lock'
    target.parent.mkdir(exist_ok=True)
    if transition == 'empty_to_nonempty':
        target.write_bytes(b'')
    elif transition == 'nonempty_to_empty':
        target.write_bytes(b'abc')
    _r7_finish(tmp_path)
    payload = b'hello' if transition == 'empty_to_nonempty' else b''
    with quota.OwnerQuotaEnforcer(tmp_path, max_bytes=1 << 30).reserve([quota.OwnerQuotaChange(target, len(payload))]):
        target.write_bytes(payload)
    settled = _r7_ledger(tmp_path)
    assert (settled['used_bytes'], settled['inode_count']) == (200 + len(payload), 200 + bool(payload))
    assert settled['dirty_since'] is None
    _r7_clear_scans()
    report = _r7_finish(tmp_path)
    assert (report['used_bytes'], report['inode_count']) == (settled['used_bytes'], settled['inode_count'])


# LLM: R6-T1：新owner的全新有界快照保持128项/10ms；依赖它的测试固定配额模块单调钟，结果只由条目额度决定。
# 函数用途: 验证前台全新快照使用128项/10ms额度。
def test_fresh_owner_snapshot_budget_is_128_entries_10_ms(tmp_path, monkeypatch):
    calls = []
    original = quota._bounded_quota_snapshot

    # 函数用途: 记录额度并调用原快照。
    def recorded(root, *, max_entries, max_ms):
        calls.append((max_entries, max_ms))
        return original(root, max_entries=max_entries, max_ms=max_ms)

    monkeypatch.setattr(quota, '_bounded_quota_snapshot', recorded)
    assert _r7_grow(tmp_path) == 'ok'
    assert calls == [(128, 10)]


# LLM: R6-Q2：超过256项的大目录里准入外新建文件，目录戳变化加名字计数在下一次增长时就能发现。
# 函数用途: 验证大目录（root或子目录）准入外新建文件被下一次增长拦下并置脏，完整对账后计入。
@pytest.mark.parametrize('where', ['root', 'subdir'])
def test_out_of_band_file_in_large_directory_is_caught_on_next_growth(tmp_path, where):
    _r7_clear_scans()
    big = tmp_path if where == 'root' else tmp_path / 'big'
    big.mkdir(exist_ok=True)
    for index in range(301):
        (big / f'{index:05d}').write_bytes(b'x')
    _r7_finish(tmp_path)
    assert _r7_grow(tmp_path) == 'ok'
    (big / 'oob.bin').write_bytes(b'z' * 4096)
    assert _r7_grow(tmp_path) == 'refused'
    assert _r7_ledger(tmp_path)['dirty_since'] is not None
    report = _r7_finish(tmp_path)
    assert report['used_bytes'] == quota.owner_logical_usage_bytes(tmp_path, exact=True) == 301 + 1 + 4096


# LLM: 大目录里锁侧车的增删只改目录戳不改计费项数；名字计数核对相符即采纳新目录戳，不能误判脏账。
# 函数用途: 验证大目录空锁侧车变动与受控新建文件不触发误报。
def test_lock_sidecar_churn_in_large_directory_keeps_baseline_clean(tmp_path):
    _r7_clear_scans()
    big = tmp_path / 'big'
    big.mkdir()
    for index in range(301):
        (big / f'{index:05d}').write_bytes(b'x')
    _r7_finish(tmp_path)
    (big / 'store.json.lock').touch()
    (big / 'other.lock').touch()
    (big / 'other.lock').unlink()
    assert _r7_grow(tmp_path, 'big/managed') == 'ok'
    assert _r7_grow(tmp_path, 'big/managed') == 'ok'
    ledger = _r7_ledger(tmp_path)
    assert ledger['dirty_since'] is None and ledger['used_bytes'] == 303


# LLM: R6-Q2：对账进行中（游标未完成、账本干净）仍按旧行与本轮新行核验；被写目录的准入外新文件当次拦下，
#   已走过目录的准入外新文件在本轮发布前的漂移清点里补正，未走到的目录由本轮扫描计入，三者完成后都与实际一致。
# 函数用途: 验证对账进行中准入外写入的发现与补正。
@pytest.mark.parametrize('target', ['touched', 'scanned', 'unscanned'])
def test_out_of_band_write_during_active_pass(tmp_path, target):
    _r7_clear_scans()
    for number in range(40):
        directory = tmp_path / f'd{number:03d}'
        directory.mkdir()
        for index in range(10):
            (directory / f'{index:02d}').write_bytes(b'x')
    _r7_finish(tmp_path)
    started = quota.reconcile_owner_quota_slice(tmp_path, max_entries=64, max_ms=5000)
    assert not started['complete'] and 'd005/' < started['cursor']['last_relpath'] < 'd030'
    oob = {'touched': 'd039', 'scanned': 'd000', 'unscanned': 'd030'}[target]
    (tmp_path / oob / 'oob.bin').write_bytes(b'z' * 4096)
    grown = _r7_grow(tmp_path, 'd039/managed')
    if target == 'touched':
        assert grown == 'refused' and _r7_ledger(tmp_path)['dirty_since'] is not None
    finished = _r7_finish(tmp_path, max_entries=64)
    assert finished['cursor']['epoch'] == started['cursor']['epoch']
    assert finished['used_bytes'] == quota.owner_logical_usage_bytes(tmp_path, exact=True) == 400 + 4096 + (grown == 'ok')
    assert _r7_ledger(tmp_path)['dirty_since'] is None
    assert quota._verify_quota_metadata(tmp_path, finished['cursor'])


# LLM: 已走过区域里整棵子树被删除或新建，漂移清点要减去旧子树行并有界补扫新子树，发布的总量与目录表都与实际一致。
# 函数用途: 验证对账走过之后的子树增删在本轮完成时被补正。
def test_subtree_removed_or_created_after_scan_is_settled(tmp_path):
    _r7_clear_scans()
    for number in range(40):
        directory = tmp_path / f'd{number:03d}'
        directory.mkdir()
        for index in range(10):
            (directory / f'{index:02d}').write_bytes(b'x')
    _r7_finish(tmp_path)
    started = quota.reconcile_owner_quota_slice(tmp_path, max_entries=64, max_ms=5000)
    assert started['cursor']['last_relpath'] > 'd003/'
    shutil.rmtree(tmp_path / 'd001')
    (tmp_path / 'd002' / 'new' / 'deeper').mkdir(parents=True)
    (tmp_path / 'd002' / 'new' / 'deeper' / 'f').write_bytes(b'n' * 7)
    (tmp_path / 'c000').mkdir()
    (tmp_path / 'c000' / 'f').write_bytes(b'c' * 11)
    finished = _r7_finish(tmp_path, max_entries=64)
    actual = quota.owner_logical_usage_bytes(tmp_path, exact=True)
    assert finished['used_bytes'] == actual == 390 + 7 + 11
    assert finished['inode_count'] == 392
    assert {'c000', 'd002/new', 'd002/new/deeper'} <= set(finished['cursor']['dir_stamps'])
    assert 'd001' not in finished['cursor']['dir_stamps']
    assert quota._verify_quota_metadata(tmp_path, finished['cursor'])


# LLM: R6-Q1：存活期按最后一次推进计，持续推进的一轮总耗时远超期限也不重来；真正闲置超期的游标才作废。
# 函数用途: 验证游标闲置期限而非整轮年龄决定是否重扫。
def test_progressing_pass_outlives_cursor_ttl_but_idle_cursor_restarts(tmp_path, monkeypatch):
    _r7_clear_scans()
    for number in range(30):
        directory = tmp_path / f'd{number:03d}'
        directory.mkdir()
        for index in range(10):
            (directory / f'{index:02d}').write_bytes(b'x')
    fake = [time.time()]
    monkeypatch.setattr(quota, 'time', SimpleNamespace(time=lambda: fake[0], monotonic=time.monotonic))
    monkeypatch.setattr(quota, '_QUOTA_SCAN_CURSOR_TTL_SECONDS', 120)
    epochs, ticks = set(), 0
    for ticks in range(1, 200):
        report = quota.reconcile_owner_quota_slice(tmp_path, max_entries=16, max_ms=5000)
        epochs.add(report['cursor']['epoch'])
        fake[0] += 60
        if report['complete']:
            break
    assert report['complete'] and len(epochs) == 1 and ticks * 60 > 120
    assert report['used_bytes'] == 300
    first = quota.reconcile_owner_quota_slice(tmp_path, max_entries=16, max_ms=5000)
    fake[0] += 121
    second = quota.reconcile_owner_quota_slice(tmp_path, max_entries=16, max_ms=5000)
    assert second['cursor']['epoch'] != first['cursor']['epoch']


# LLM: R6-Q1：每片吞吐由逐目录打开次数决定；子目录打开一次即供排序准备与首个窗口共用，整轮打开数不随文件数增长。
# 函数用途: 验证嵌套树一整轮的目录打开次数低于目录项数且不超过目录数的两倍。
def test_nested_tree_pass_opens_each_directory_about_once(tmp_path, monkeypatch):
    _r7_clear_scans()
    for top in range(3):
        for middle in range(40):
            leaf = tmp_path / 'w' / f'a{top:03d}' / f'b{middle:03d}' / 'c'
            leaf.mkdir(parents=True)
            for index in range(3):
                (leaf / f'f{index}').write_bytes(b'x')
    directories = sum(1 for path in tmp_path.rglob('*') if path.is_dir()) + 1
    entries = sum(1 for _ in tmp_path.rglob('*'))
    opens = [0]
    original = quota.open_directory_beneath

    # 函数用途: 计数后调用原nofollow逐层打开。
    def counted(*args, **kwargs):
        opens[0] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(quota, 'open_directory_beneath', counted)
    report = _r7_finish(tmp_path, max_entries=256, max_ms=5000)
    assert report['used_bytes'] == 360
    assert opens[0] <= 2 * directories < entries


# LLM: 账本缺失、脏或一轮未走完时维护默认额度升到追赶档；账干净且上一轮已走完回到稳态档；调用方显式额度原样使用。
# 函数用途: 验证维护入口的配额对账额度选择。
def test_maintenance_quota_budget_catches_up_until_pass_completes(tmp_path):
    from agent_py_agent.agent.user_space import owner_maintenance as maintenance
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    home = ensure_my_agent_home(tmp_path / 'home')
    catch_up = (maintenance._QUOTA_CATCH_UP_MAX_ENTRIES, maintenance._QUOTA_CATCH_UP_MAX_MS)
    steady = (maintenance._QUOTA_STEADY_MAX_ENTRIES, maintenance._QUOTA_STEADY_MAX_MS)
    assert catch_up[0] >= 1024 and catch_up[1] >= 40 and steady == (256, 10)
    assert maintenance._quota_maintenance_budget(home, None, None) == steady
    quota.OwnerQuotaEnforcer(home.owner_home_dir, max_bytes=1 << 30)
    assert maintenance._quota_maintenance_budget(home, None, None) == catch_up
    quota.reconcile_owner_quota_slice(home.owner_home_dir, max_entries=1, max_ms=5000)
    assert maintenance._quota_maintenance_budget(home, None, None) == catch_up
    _r7_finish(home.owner_home_dir)
    assert maintenance._quota_maintenance_budget(home, None, None) == steady
    assert maintenance._quota_maintenance_budget(home, 7, 1000) == (7, 1000)
    path = home.owner_home_dir / '.owner-quota-ledger.json'
    ledger = json.loads(path.read_text())
    ledger['dirty_since'] = 1.0
    path.write_text(json.dumps(ledger))
    assert maintenance._quota_maintenance_budget(home, None, None) == catch_up
    with pytest.raises(ValueError, match='invalid_quota_maintenance_budget'):
        maintenance._quota_maintenance_budget(home, 0, None)


# LLM: R6-Q1：配额对账只写配额账、不删内容，保留策略加载失败或被拒时仍推进一片并回报；一轮未完成保留待续。
# 函数用途: 验证保留不安全的延期路径仍执行配额对账。
@pytest.mark.parametrize('reason', ['load_errors', 'refused'])
def test_quota_reconcile_runs_when_retention_is_not_safe(tmp_path, reason):
    from agent_py_agent.agent.user_space import owner_maintenance as maintenance
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    home = ensure_my_agent_home(tmp_path / 'home')
    policy = SimpleNamespace(load_errors=['bad'] if reason == 'load_errors' else [],
                             applied=reason != 'refused', legal_hold=False)
    partial = maintenance.run_owner_storage_maintenance_slice(home, {}, policy, now=2_000_000,
                                                              quota_max_entries=1, quota_max_ms=5000, state_locked=True)
    assert partial['quota_reconciliation']['complete'] is False and partial['continuation_pending'] is True
    result = maintenance.run_owner_storage_maintenance_slice(home, {}, policy, now=2_000_000,
                                                             quota_max_ms=5000, state_locked=True)
    assert result['deferred_reason'] == 'retention_not_safe' and result['deletion_enabled'] is False
    assert result['quota_reconciliation']['complete'] is True and result['continuation_pending'] is False
    assert 'cursor' not in result['quota_reconciliation']
    assert json.loads((home.owner_home_dir / '.owner-quota-ledger.json').read_text())['dirty_since'] is None


# LLM: C/W下配额对账必须经原transition提交才写账；没有transition时只报告延期，不写配额账。
# 函数用途: 验证协调模式的配额对账只走调用方给出的transition。
def test_coordinator_quota_reconcile_runs_only_through_transition_commit(tmp_path):
    from agent_py_agent.agent.user_space import owner_maintenance as maintenance
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    policy = SimpleNamespace(load_errors=[], applied=True, legal_hold=False)
    home = ensure_my_agent_home(tmp_path / 'home')
    committed = []

    # 函数用途: 记录经transition提交的操作并原样执行。
    def commit(operation):
        committed.append(operation)
        return operation()

    result = maintenance.run_owner_storage_maintenance_slice(home, {}, policy, now=2_000_000, coordinator=True,
                                                             commit_transition=commit)
    assert len(committed) == 1 and result['quota_reconciliation']['complete'] is True
    assert result['deferred_reason'] == 'coordinator_adapter_protocol_required'
    other = ensure_my_agent_home(tmp_path / 'other')
    deferred = maintenance.run_owner_storage_maintenance_slice(other, {}, policy, now=2_000_000, coordinator=True)
    assert deferred['quota_reconciliation'] == {'complete': False,
                                                'deferred_reason': 'coordinator_adapter_protocol_required'}
    assert deferred['continuation_pending'] is False
    assert not (other.owner_home_dir / '.owner-quota-ledger.json').exists()


# LLM: 上一版目录行（含七整数旧行）不能当本版表核验，也不能接着累加；受控写按代次维持，下一轮重建本版表。
# 函数用途: 验证旧格式目录表的完整游标与进行中游标都安全过渡。
def test_previous_row_format_is_rebuilt_not_trusted(tmp_path):
    _r7_clear_scans()
    for index in range(160):
        (tmp_path / f'{index:04d}').write_bytes(b'x')
    _r7_finish(tmp_path)
    path = tmp_path / '.owner-quota-ledger.json'
    ledger = json.loads(path.read_text())
    cursor = ledger['reconcile_cursor']
    cursor['dir_stamps'] = {rel: row[:7] for rel, row in cursor['dir_stamps'].items()}
    cursor.pop('row_format')
    path.write_text(json.dumps(ledger))
    assert _r7_grow(tmp_path) == 'ok'
    assert _r7_ledger(tmp_path)['dirty_since'] is None
    started = quota.reconcile_owner_quota_slice(tmp_path, max_entries=32, max_ms=5000)
    ledger = json.loads(path.read_text())
    ledger['reconcile_cursor']['scan_rows'] = {rel: row[:7] for rel, row in started['cursor']['scan_rows'].items()}
    path.write_text(json.dumps(ledger))
    _r7_clear_scans()
    restarted = quota.reconcile_owner_quota_slice(tmp_path, max_entries=32, max_ms=5000)
    assert restarted['cursor']['epoch'] != started['cursor']['epoch']
    report = _r7_finish(tmp_path)
    assert report['used_bytes'] == report['inode_count'] == 161
    assert report['cursor']['row_format'] == quota._QUOTA_ROW_SCHEMA_VERSION
    assert quota._verify_quota_metadata(tmp_path, report['cursor'])


# LLM: 上一轮目录表恰在字节上限时，新一轮的扫描行与同目录旧行短暂并存不能把表挤爆；走完后仍发布完整的本版目录表。
# 函数用途: 验证滚动基线下目录表字节按目录去重计数。
def test_table_at_byte_cap_survives_next_pass(tmp_path, monkeypatch):
    _r7_clear_scans()
    for number in range(40):
        directory = tmp_path / f'd{number:03d}'
        directory.mkdir()
        for index in range(3):
            (directory / f'{index:02d}').write_bytes(b'x')
    first = _r7_finish(tmp_path)
    monkeypatch.setattr(quota, '_QUOTA_MAX_METADATA_BYTES',
                        sum(quota._quota_row_bytes(rel) for rel in first['cursor']['dir_stamps']))
    second = _r7_finish(tmp_path, max_entries=16)
    assert second['cursor']['epoch'] != first['cursor']['epoch']
    assert second['cursor']['metadata_complete'] is True
    assert set(second['cursor']['dir_stamps']) == set(first['cursor']['dir_stamps'])
    assert quota._verify_quota_metadata(tmp_path, second['cursor'])


# LLM: R7-Q1：真实owner布局下的宿主状态（运行库WAL、审计日志、维护状态原子替换、对话压缩子树增删）在准入外写入，
#   不让目录摘要失效：下一次受控增长照常放行且账本干净，全量核验通过；字节仍计费，下一轮对账与实际一致。
# 函数用途: 验证宿主状态写入不破坏可信目录基线。
@pytest.mark.parametrize('action', ['runtime_db', 'audit_log', 'maintenance_json', 'compact'])
def test_r8_host_state_writes_keep_directory_baseline_clean(tmp_path, action):
    import sqlite3

    from agent_py_agent.agent.common.json_io import write_json_file_atomic_unlocked

    _r7_clear_scans()
    pad = tmp_path / 'workspace' / 'pad'
    pad.mkdir(parents=True)
    for index in range(300):
        (pad / f'{index:05d}').write_bytes(b'x')
    database = sqlite3.connect(tmp_path / 'runtime.db')
    database.execute('PRAGMA journal_mode=WAL')
    database.execute('CREATE TABLE runs (id INTEGER PRIMARY KEY, body TEXT)')
    database.commit()
    (tmp_path / 'audit_log.jsonl').write_text('{"seed": 1}\n')
    (tmp_path / 'data').mkdir()
    write_json_file_atomic_unlocked(tmp_path / 'data' / 'maintenance.json', {'epoch': 0})
    (tmp_path / 'compact' / 'conversations').mkdir(parents=True)
    (tmp_path / 'compact' / 'conversations' / 't.jsonl').write_text('{}\n')
    _r7_finish(tmp_path)
    assert _r7_grow(tmp_path, 'workspace/managed') == 'ok'
    if action == 'runtime_db':
        database.execute("INSERT INTO runs (body) VALUES ('host run record')")
        database.commit()
        database.close()
    elif action == 'audit_log':
        with (tmp_path / 'audit_log.jsonl').open('a') as stream:
            stream.write('{"event": "host audit"}\n')
    elif action == 'maintenance_json':
        write_json_file_atomic_unlocked(tmp_path / 'data' / 'maintenance.json', {'epoch': 1, 'pad': 'm' * 64})
    else:
        shutil.rmtree(tmp_path / 'compact' / 'conversations')
        (tmp_path / 'compact' / 'by_task').mkdir()
        (tmp_path / 'compact' / 'by_task' / 'x.jsonl').write_text('{"turn": 1}\n')
    for _ in range(3):
        assert _r7_grow(tmp_path, 'workspace/managed') == 'ok'
    ledger = _r7_ledger(tmp_path)
    assert ledger['dirty_since'] is None
    assert quota._verify_quota_metadata(tmp_path, ledger['reconcile_cursor'])
    if action != 'runtime_db':
        database.close()
    _r7_clear_scans()
    report = _r7_finish(tmp_path)
    assert report['used_bytes'] == quota.owner_logical_usage_bytes(tmp_path, exact=True)
    assert not any(rel.split('/')[0] == 'compact' for rel in report['cursor']['dir_stamps'])


# LLM: R7-Q1反向与R8-Q1：模型/用户可写目录（含owner根目录下的.*.tmp文件与目录）仍关闭式核验；
#   准入外新文件、带内容的新子目录、宿主状态同名但不在宿主位置的文件、以及根目录下的.audit_log.jsonl.*.tmp与.runtime.db*.*.tmp
#   都在下一次增长时拒绝并置脏；仅宿主只读目录data/下的.maintenance.json.*.tmp豁免；准入外新建的空目录按0字节采纳，
#   之后往里写准入外内容同样被拦下。
# 函数用途: 验证非宿主路径与根目录临时名的准入外增删仍被前台关闭式拦截。
@pytest.mark.parametrize('change', [
    'file', 'nonempty_dir', 'host_name_elsewhere', 'empty_dir_then_file',
    'root_plain_file', 'root_audit_tmp_file', 'root_runtime_tmp_file',
    'root_wal_tmp_file', 'root_audit_tmp_dir', 'data_maint_tmp_file',
])
def test_r8_agent_writable_out_of_band_changes_stay_fail_closed(tmp_path, change):
    _r7_clear_scans()
    notes = tmp_path / 'workspace' / 'notes'
    notes.mkdir(parents=True)
    (tmp_path / 'data').mkdir(exist_ok=True)
    for index in range(3):
        (notes / f'{index}.md').write_bytes(b'n')
    for index in range(200):
        (tmp_path / 'workspace' / f'pad-{index:03d}').write_bytes(b'x')
    _r7_finish(tmp_path)
    assert _r7_grow(tmp_path, 'workspace/notes/managed') == 'ok'
    if change == 'file':
        (notes / 'oob.bin').write_bytes(b'z' * 4096)
    elif change == 'nonempty_dir':
        (notes / 'sub').mkdir()
        (notes / 'sub' / 'oob.bin').write_bytes(b'z' * 4096)
    elif change == 'host_name_elsewhere':
        (notes / 'maintenance.json').write_bytes(b'z' * 4096)
    elif change == 'root_plain_file':
        (tmp_path / 'plain.bin').write_bytes(b'z' * 4096)
    elif change == 'root_audit_tmp_file':
        (tmp_path / '.audit_log.jsonl.deadbeef.tmp').write_bytes(b'z' * 4096)
    elif change == 'root_runtime_tmp_file':
        (tmp_path / '.runtime.db.deadbeef.tmp').write_bytes(b'z' * 4096)
    elif change == 'root_wal_tmp_file':
        (tmp_path / '.runtime.db-wal.cafe01.tmp').write_bytes(b'z' * 4096)
    elif change == 'root_audit_tmp_dir':
        (tmp_path / '.audit_log.jsonl.d0.tmp').mkdir()
        (tmp_path / '.audit_log.jsonl.d0.tmp' / 'big').write_bytes(b'z' * 4096)
    elif change == 'data_maint_tmp_file':
        (tmp_path / 'data' / '.maintenance.json.deadbeef.tmp').write_bytes(b'z' * 4096)
        assert _r7_grow(tmp_path, 'workspace/notes/managed') == 'ok'
        assert _r7_ledger(tmp_path)['dirty_since'] is None
        return
    else:
        (notes / 'sub').mkdir()
        assert _r7_grow(tmp_path, 'workspace/notes/managed') == 'ok'
        assert _r7_ledger(tmp_path)['dirty_since'] is None
        assert 'workspace/notes/sub' in _r7_ledger(tmp_path)['reconcile_cursor']['dir_stamps']
        (notes / 'sub' / 'oob.bin').write_bytes(b'z' * 4096)
        assert _r7_grow(tmp_path, 'workspace/notes/sub/managed') == 'refused'
        assert _r7_ledger(tmp_path)['dirty_since'] is not None
        return
    assert _r7_grow(tmp_path, 'workspace/notes/managed') == 'refused'
    assert _r7_ledger(tmp_path)['dirty_since'] is not None
    report = _r7_finish(tmp_path)
    assert report['used_bytes'] == quota.owner_logical_usage_bytes(tmp_path, exact=True)


# LLM: R7-Q1：维护写完状态后在不阻塞的配额锁下刷新所在目录行：干净完整的表核过后写回当前目录戳；无账不建锁文件、
#   脏账不改账。账脏时维护下一拍就到期续作对账，不等整天周期。
# 函数用途: 验证宿主行刷新与待对账触发维护。
def test_r8_maintenance_refreshes_host_rows_and_runs_while_quota_dirty(tmp_path):
    from agent_py_agent.agent.common.json_io import write_json_file_atomic_unlocked
    from agent_py_agent.agent.user_space import owner_maintenance as maintenance
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    _r7_clear_scans()
    fresh = tmp_path / 'fresh'
    fresh.mkdir()
    assert quota.refresh_owner_quota_host_rows(fresh, ('data', '')) is False
    assert not (fresh / '.owner-quota.lock').exists()
    home = ensure_my_agent_home(tmp_path / 'home')
    owner = home.owner_home_dir
    quota.OwnerQuotaEnforcer(owner, max_bytes=1 << 30)
    assert maintenance.run_owner_retention_if_due(home, now=3_000_000_000).ran
    _r7_finish(owner)
    assert not maintenance.owner_maintenance_due(owner, now=3_000_000_060)
    write_json_file_atomic_unlocked(owner / 'data' / 'maintenance.json',
                                    {**json.loads((owner / 'data' / 'maintenance.json').read_text()), 'pad': 'p' * 64})
    info = (owner / 'data').stat()
    assert _r7_ledger(owner)['reconcile_cursor']['dir_stamps']['data'][2:4] != [info.st_mtime_ns, info.st_ctime_ns]
    assert quota.refresh_owner_quota_host_rows(owner, ('data', '')) is True
    rows = _r7_ledger(owner)['reconcile_cursor']['dir_stamps']
    assert rows['data'][2:4] == [info.st_mtime_ns, info.st_ctime_ns]
    assert quota._verify_quota_metadata(owner, _r7_ledger(owner)['reconcile_cursor'])
    ledger = _r7_ledger(owner)
    ledger['dirty_since'] = 1.0
    (owner / '.owner-quota-ledger.json').write_text(json.dumps(ledger))
    assert quota.refresh_owner_quota_host_rows(owner, ('data', '')) is False
    assert maintenance.owner_maintenance_due(owner, now=3_000_000_060)
    (owner / '.owner-quota-ledger.json').write_text('{"corrupt": true}')
    assert not maintenance.owner_maintenance_due(owner, now=3_000_000_060)
    (owner / '.owner-quota-ledger.json').unlink()
    assert not maintenance.owner_maintenance_due(owner, now=3_000_000_060)


# LLM: R7-QUOTA-01：大目录需lstat定性的锁名超过额度且行里记有计费锁时，名字计数无法在额度内证明计费项数未变，
#   目录戳一变就关闭式拒绝；准入外新的普通文件按名字即可发现。
# 函数用途: 验证超过256个计费锁名的大目录不再被区间判断放行。
@pytest.mark.parametrize('placement', ['root', 'subdir'])
def test_r8_large_directory_with_unclassified_billed_locks_fails_closed(tmp_path, placement):
    _r7_clear_scans()
    directory = tmp_path if placement == 'root' else tmp_path / 'locks'
    directory.mkdir(exist_ok=True)
    for index in range(300):
        (directory / f'{index:04d}.lock').write_bytes(b'1')
    (tmp_path / 'other').mkdir()
    _r7_finish(tmp_path)
    assert _r7_grow(tmp_path, 'other/managed') == 'ok'
    assert _r7_ledger(tmp_path)['dirty_since'] is None
    (directory / 'oob-new.bin').write_bytes(b'z' * (2 << 20))
    name = 'other/managed' if placement == 'root' else 'locks/managed'
    assert _r7_grow(tmp_path, name, max_bytes=1 << 20) == 'refused'
    assert _r7_ledger(tmp_path)['dirty_since'] is not None


# LLM: R7-QUOTA-01：空锁名再多也只lstat额度个；行里没有计费锁时抽查全不计费且名字可定性项数相符才放行，
#   准入外新的普通文件因名字计数不符被拦下，新增空锁侧车仍放行。
# 函数用途: 验证空锁密集大目录的放行与拦截都只靠有界lstat。
def test_r8_empty_lock_heavy_directory_uses_bounded_lock_stats(tmp_path, monkeypatch):
    _r7_clear_scans()
    for index in range(1000):
        (tmp_path / f'{index:05d}.lock').touch()
    for index in range(200):
        (tmp_path / f'payload-{index:04d}.bin').write_bytes(b'x')
    _r7_finish(tmp_path)
    calls = {'lock_stat': 0}
    original = os.stat

    # 函数用途: 统计按名字对锁文件的lstat。
    def counted(path, *args, **kwargs):
        if isinstance(path, str) and path.endswith('.lock') and kwargs.get('dir_fd') is not None:
            calls['lock_stat'] += 1
        return original(path, *args, **kwargs)

    monkeypatch.setattr(quota, 'os', SimpleNamespace(**{**vars(os), 'stat': counted}))
    assert _r7_grow(tmp_path) == 'ok'
    assert calls['lock_stat'] <= quota._QUOTA_VERIFY_BUDGET_ENTRIES
    (tmp_path / 'new-sidecar.lock').touch()
    calls['lock_stat'] = 0
    assert _r7_grow(tmp_path) == 'ok'
    assert calls['lock_stat'] <= quota._QUOTA_VERIFY_BUDGET_ENTRIES
    assert _r7_ledger(tmp_path)['dirty_since'] is None
    (tmp_path / 'oob.bin').write_bytes(b'z')
    assert _r7_grow(tmp_path) == 'refused'


# LLM: R7-Q2：行在上限内的大目录按名字核对至多读上限+1项，超过即关闭式拒绝；行记录已超过上限的目录前台不枚举、
#   留给后台对账（披露残留），下一轮扫描补正总量。两种情形持锁时间都不随单目录规模增长。
# 函数用途: 验证名字枚举在上限处停止，以及超上限目录交后台补正。
@pytest.mark.parametrize('row_over_limit', [False, True])
def test_r8_large_directory_name_enumeration_is_bounded(tmp_path, monkeypatch, row_over_limit):
    _r7_clear_scans()
    for index in range(400):
        (tmp_path / f'{index:05d}').write_bytes(b'x')
    _r7_finish(tmp_path)
    limit = 300 if row_over_limit else 450
    monkeypatch.setattr(quota, '_QUOTA_LARGE_DIR_MAX_ENTRIES', limit)
    raw = os.scandir
    seen = [0]

    # 类用途: 转发真实目录流并统计读出的目录项。
    class Counting:
        def __init__(self, *args):
            self.inner = raw(*args)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.inner.__exit__(*args)

        def __iter__(self):
            for entry in self.inner:
                seen[0] += 1
                yield entry

    for index in range(100):
        (tmp_path / f'oob-{index:03d}.bin').write_bytes(b'z' * 1024)
    monkeypatch.setattr(quota, 'os', SimpleNamespace(**{**vars(os), 'scandir': Counting}))
    if not row_over_limit:
        assert _r7_grow(tmp_path) == 'refused'
        assert seen[0] <= limit + 1
        return
    assert _r7_grow(tmp_path) == 'ok'
    assert seen[0] == 0
    monkeypatch.setattr(quota, 'os', os)
    report = _r7_finish(tmp_path)
    assert report['used_bytes'] == 400 + 100 * 1024 + 1
    assert _r7_ledger(tmp_path)['dirty_since'] is None


# LLM: R7-QUOTA-02：发布前的漂移清点每轮只建一次，逐项交还对账片，单片清点项数受本片额度约束；热续扫跨片走完后
#   总量含走过之后新增的内容、账本干净、目录表全量核验通过。
# 函数用途: 验证漂移清点的真实目录项额度与跨片续作。
def test_r8_drift_settlement_is_budgeted_across_slices(tmp_path, monkeypatch):
    _r7_clear_scans()
    for number in range(40):
        directory = tmp_path / f'd{number:03d}'
        directory.mkdir()
        for index in range(10):
            (directory / f'{index:04d}.bin').write_bytes(b'x')
    _r7_finish(tmp_path)
    assert not quota.reconcile_owner_quota_slice(tmp_path, max_entries=64, max_ms=5000)['complete']
    for index in range(3000):
        (tmp_path / 'd000' / f'new-{index:05d}.bin').write_bytes(b'x')
    original = quota._settle_quota_pass_drift
    sweeps = []

    # 函数用途: 记录每轮建立清点的次数。
    def counted(root, scan):
        sweeps.append(root)
        return original(root, scan)

    monkeypatch.setattr(quota, '_settle_quota_pass_drift', counted)
    reports = []
    for _ in range(200):
        reports.append(quota.reconcile_owner_quota_slice(tmp_path, max_entries=64, max_ms=5000))
        if reports[-1]['complete']:
            break
    assert reports[-1]['complete'] and len(sweeps) == 1
    budget = max(1024, 64 * 4)
    assert all(row['enumerated_entries'] + row['settled_entries'] <= budget for row in reports)
    assert sum(row['settled_entries'] for row in reports) >= 3010
    assert reports[-1]['used_bytes'] == quota.owner_logical_usage_bytes(tmp_path, exact=True) == 3400
    assert _r7_ledger(tmp_path)['dirty_since'] is None
    assert quota._verify_quota_metadata(tmp_path, reports[-1]['cursor'])


# LLM: R7-QUOTA-02：重扫到一半遇冷恢复（缓存淘汰、重启或其它写入）的目录不从头重扫，本轮照常发布但置脏，
#   反复冷恢复也能走完；下一轮正常扫描补正后账本恢复干净。
# 函数用途: 验证漂移清点在每片冷恢复时不活锁。
def test_r8_drift_settlement_converges_under_cold_resumes(tmp_path):
    _r7_clear_scans()
    for number in range(40):
        directory = tmp_path / f'd{number:03d}'
        directory.mkdir()
        for index in range(10):
            (directory / f'{index:04d}.bin').write_bytes(b'x')
    _r7_finish(tmp_path)
    assert not quota.reconcile_owner_quota_slice(tmp_path, max_entries=64, max_ms=5000)['complete']
    for index in range(3000):
        (tmp_path / 'd000' / f'new-{index:05d}.bin').write_bytes(b'x')
    for _ in range(200):
        _r7_clear_scans()
        report = quota.reconcile_owner_quota_slice(tmp_path, max_entries=64, max_ms=5000)
        if report['complete']:
            break
    assert report['complete'] and _r7_ledger(tmp_path)['dirty_since'] is not None
    assert _r7_grow(tmp_path, 'd001/managed') == 'refused'
    report = _r7_finish(tmp_path, max_entries=64)
    assert report['used_bytes'] == quota.owner_logical_usage_bytes(tmp_path, exact=True) == 3400
    assert _r7_ledger(tmp_path)['dirty_since'] is None


# LLM: R7-QUOTA-02：root只在本轮连续推进中证明只被本进程写账本改动时才免于重扫；对账进行中root里的准入外新文件
#   仍在发布前补正进总量与root行。
# 函数用途: 验证root连续性判断不漏掉对账期间root的准入外增删。
def test_r8_root_out_of_band_change_during_pass_is_settled(tmp_path):
    _r7_clear_scans()
    for number in range(40):
        directory = tmp_path / f'd{number:03d}'
        directory.mkdir()
        for index in range(10):
            (directory / f'{index:04d}.bin').write_bytes(b'x')
    _r7_finish(tmp_path)
    assert not quota.reconcile_owner_quota_slice(tmp_path, max_entries=64, max_ms=5000)['complete']
    (tmp_path / 'oob.bin').write_bytes(b'z' * 4096)
    report = _r7_finish(tmp_path, max_entries=64)
    assert report['used_bytes'] == quota.owner_logical_usage_bytes(tmp_path, exact=True) == 400 + 4096
    assert quota._verify_quota_metadata(tmp_path, report['cursor'])


# LLM: R7-MAINT-01：公开维护入口显式给出的配额对账额度在C/W transition路径同样原样传到对账片。
# 函数用途: 验证transition维护转发显式配额额度。
@pytest.mark.parametrize('transition', [False, True])
def test_r8_public_maintenance_forwards_explicit_quota_budget(tmp_path, monkeypatch, transition):
    from agent_py_agent.agent.user_space import owner_maintenance as maintenance
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    home = ensure_my_agent_home(tmp_path / 'home')
    original = quota.reconcile_owner_quota_slice
    calls = []

    # 函数用途: 记录对账片额度并调用原实现。
    def recorded(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(quota, 'reconcile_owner_quota_slice', recorded)
    result = maintenance.run_owner_retention_if_due(
        home, now=2_000_000_000, include_global_indexes=False,
        commit_transition=(lambda operation: operation()) if transition else None,
        quota_max_entries=7, quota_max_ms=5)
    assert result.ran and len(calls) == 1
    assert calls[0]['max_entries'] == 7 and calls[0]['max_ms'] <= 5


# LLM: R7-Q4：配额关闭（max_disk_mb=0）时无论retention是否带load_errors，维护都保持每天一次且不置待续；
#   配额开启时追赶期间每拍只推进轻量配额片，apply_owner_retention每天只跑一次。
# 函数用途: 验证配额关闭与配额开启在正常路径与延期路径下的维护节拍与保留调用次数。
@pytest.mark.parametrize('quota_enabled', [False, True])
@pytest.mark.parametrize('load_error', [False, True])
def test_r8_quota_disabled_and_enabled_continuation_cadence(tmp_path, monkeypatch, quota_enabled, load_error):
    import dataclasses

    from agent_py_agent.agent.memory_store.retention_models import MemoryRetentionError
    from agent_py_agent.agent.user_space import owner_maintenance as maintenance
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    _r7_clear_scans()
    home = ensure_my_agent_home(tmp_path / 'home')
    owner = home.owner_home_dir
    for d in range(300):
        sub = owner / 'workspace' / 'tree' / f'a{d // 50:02d}' / f'b{d:04d}'
        sub.mkdir(parents=True, exist_ok=True)
        for f in range(5):
            (sub / f'f{f}').write_bytes(b'x')
    if quota_enabled:
        quota_payload = json.loads((owner / 'quota.json').read_text())
        quota_payload['max_disk_mb'] = 1024
        (owner / 'quota.json').write_text(json.dumps(quota_payload))
    real_apply = maintenance.apply_owner_retention
    retention_calls = [0]

    # 函数用途: 统计apply_owner_retention调用次数并按参数注入加载错误。
    def counted_apply(*args, **kwargs):
        retention_calls[0] += 1
        plan = real_apply(*args, **kwargs)
        if not load_error:
            return plan
        error = MemoryRetentionError('MEMORY_RETENTION_PIN_SOURCE_UNREADABLE', 'memory/x.jsonl', 'OSError')
        return dataclasses.replace(plan, errors=(*plan.errors, error))

    monkeypatch.setattr(maintenance, 'apply_owner_retention', counted_apply)
    start = 3_000_000_000
    day1_runs = [tick for tick in range(30)
                 if maintenance.run_owner_retention_if_due(home, now=start + tick * 60).ran]
    state = json.loads((owner / 'data' / 'maintenance.json').read_text())
    if not quota_enabled:
        assert day1_runs == [0]
        assert retention_calls[0] == 1
        assert state['maintenance_partial'] is False
    else:
        assert len(day1_runs) > 1
        assert retention_calls[0] == 1
        assert state['maintenance_partial'] is False
        ledger = json.loads((owner / '.owner-quota-ledger.json').read_text())
        assert ledger['dirty_since'] is None and ledger['reconcile_cursor']['complete'] is True
    day2_runs = [tick for tick in range(30)
                 if maintenance.run_owner_retention_if_due(home, now=start + 86400 + tick * 60).ran]
    assert retention_calls[0] == 2
    if not quota_enabled:
        assert day2_runs == [0]
    else:
        from agent_py_agent.agent.scheduler.repository import (
            SchedulerJobCreateRequest,
            SchedulerRepository,
        )

        maint_cursor = state['storage_maintenance'].get('cursors', {}).get('cache_tmp_trash') or {}
        assert not any(
            k in maint_cursor
            for k in ('dir_stamps', 'base_rows', 'scan_rows', 'dir_batch', 'prep', 'orphans')
        )
        enforcer = quota.owner_quota_enforcer_from_policy(owner)
        repository = SchedulerRepository(owner / 'data' / 'scheduler', owner_provider='feishu', owner_kind='user',
                                         owner_id='u-1', default_timezone='Asia/Shanghai', quota_enforcer=enforcer)
        created, reused = repository.create_job(SchedulerJobCreateRequest(
            name='job-1', prompt='execute 1', thread_id='thread-1', source_task_id='task-1',
            schedule={'kind': 'every', 'every_seconds': 600, 'anchor_at': 1_000}, misfire_grace_seconds=None,
            skill_refs=[], source_request_id='request:1', now=901))
        assert reused is False and created['name'] == 'job-1'
        assert _r7_ledger(owner)['dirty_since'] is None
        assert (
            quota._parse_quota_disk_mb('1') == 1
            and quota._parse_quota_disk_mb(True) == 0
            and quota._parse_quota_disk_mb(float('inf')) == 0
        )


# LLM: R8-QUOTA-01/02：漂移补扫期间发生真实文件删除竞态或异常时必须保留dirty并拒绝增长；已有大目录漂移补扫的真实scandir迭代必须逐项受单片预算上限约束。
# 函数用途: 验证漂移补扫异常不发布干净旧账本且已有大目录真实枚举严格遵守切片预算。
def test_r8_settlement_oserror_keeps_dirty_and_large_dir_rescan_obeys_raw_scandir_budget(tmp_path, monkeypatch):
    _r7_clear_scans()
    race_root = tmp_path / 'race_owner'
    race_root.mkdir()
    for name, count in (('a', 10), ('z', 200)):
        sub = race_root / name
        sub.mkdir()
        for idx in range(count):
            (sub / f'{idx:05d}.bin').write_bytes(b'x')
    assert _r7_finish(race_root)['used_bytes'] == 210
    partial = quota.reconcile_owner_quota_slice(race_root, max_entries=64, max_ms=5000)
    assert not partial['complete'] and partial['cursor']['dir_stamps']['a'][7] == 10
    (race_root / 'a' / 'new-big.bin').write_bytes(b'y' * (2 * 1024 * 1024))
    victim = race_root / 'a' / '00000.bin'
    deleted = [False]
    real_scandir = os.scandir

    # 类用途: 在目标目录项stat前触发一次真实文件删除以复现漂移补扫竞态。
    class _DeleteBeforeStat:
        def __init__(self, entry):
            self.entry = entry

        def __getattr__(self, attr):
            return getattr(self.entry, attr)

        def stat(self, **kwargs):
            if self.entry.name == victim.name and not deleted[0]:
                victim.unlink()
                deleted[0] = True
            return self.entry.stat(**kwargs)

    # 类用途: 包装os.scandir迭代器以注入真实删除竞态。
    class _DeleteRaceScandir:
        def __init__(self, inner):
            self.inner = inner

        def __enter__(self):
            self.inner.__enter__()
            return self

        def __exit__(self, *args):
            return self.inner.__exit__(*args)

        def __iter__(self):
            return self

        def __next__(self):
            return _DeleteBeforeStat(next(self.inner))

        def close(self):
            self.inner.close()

    monkeypatch.setattr(quota, 'os', SimpleNamespace(**{
        **vars(os), 'scandir': lambda *args, **kwargs: _DeleteRaceScandir(real_scandir(*args, **kwargs))
    }))
    _r7_finish(race_root)
    monkeypatch.setattr(quota, 'os', os)
    assert deleted[0] is True
    assert _r7_ledger(race_root)['dirty_since'] is not None
    with pytest.raises((quota.OwnerQuotaUnavailable, quota.OwnerQuotaExceeded)):
        with quota.OwnerQuotaEnforcer(race_root, max_bytes=1024 * 1024).reserve(
            [quota.OwnerQuotaChange(race_root / 'z' / 'managed', 1)]
        ):
            (race_root / 'z' / 'managed').write_bytes(b'z')
    recovered = _r7_finish(race_root)
    assert recovered['complete'] and recovered['used_bytes'] == 2 * 1024 * 1024 + 209
    assert _r7_ledger(race_root)['dirty_since'] is None

    _r7_clear_scans()
    budget_root = tmp_path / 'budget_owner'
    budget_root.mkdir()
    for name, count in (('a', 4096), ('z', 600)):
        sub = budget_root / name
        sub.mkdir()
        for idx in range(count):
            (sub / f'{idx:05d}.bin').write_bytes(b'x')
    assert _r7_finish(budget_root, max_entries=10000)['used_bytes'] == 4696
    partial_large = quota.reconcile_owner_quota_slice(budget_root, max_entries=4200, max_ms=5000)
    assert not partial_large['complete'] and partial_large['cursor']['dir_stamps']['a'][7] == 4096
    (budget_root / 'a' / 'new.bin').write_bytes(b'y')
    scandir_count = [0]

    # 类用途: 统计真实os.scandir迭代返回的目录项数。
    class _ScandirCounter:
        def __init__(self, inner):
            self.inner = inner

        def __enter__(self):
            self.inner.__enter__()
            return self

        def __exit__(self, *args):
            return self.inner.__exit__(*args)

        def __iter__(self):
            return self

        def __next__(self):
            item = next(self.inner)
            scandir_count[0] += 1
            return item

        def close(self):
            self.inner.close()

    monkeypatch.setattr(quota, 'os', SimpleNamespace(**{
        **vars(os), 'scandir': lambda *args, **kwargs: _ScandirCounter(real_scandir(*args, **kwargs))
    }))
    for _ in range(40):
        before = scandir_count[0]
        report = quota.reconcile_owner_quota_slice(budget_root, max_entries=64, max_ms=5000)
        assert scandir_count[0] - before <= 1024
        if report['complete']:
            break
    monkeypatch.setattr(quota, 'os', os)
    assert report['complete'] and report['used_bytes'] == 4697
