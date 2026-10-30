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
