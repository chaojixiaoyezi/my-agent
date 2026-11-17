# LLM: 只用临时Gateway目录与真实原终态/投影合同，不触碰生产队列；缺实现先以断言失败。
# 模块用途: 验证终态分段可回读、欠账保护、预算和提交前原件保留。
from __future__ import annotations

import json

import pytest

from agent_py_agent.agent.gateway_parts import owner_retention as archive
from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.recovery import (
    _terminal_gateway_request_payload,
    mark_gateway_terminal_projection_complete,
)


# LLM: 缺失API是明确功能红测，不通过ImportError掩盖无效夹具。
# 函数用途: 取得唯一分段维护入口。
def _sealer():
    operation = getattr(archive, 'seal_gateway_receipts_slice', None)
    assert callable(operation), 'bounded terminal receipt sealing is not implemented'
    return operation


# LLM: 终态、投影和结算marker均由原产品builder构造；附带真实旧chunk文件。
# 函数用途: 在临时目录准备一个已完成并可复核的请求。
def _settled(paths, key='request-a', *, completed_at=1, pending=False):
    response = {'id': key, 'ok': True, 'status': 'done', 'ended_at': completed_at, 'output': 'answer'}
    payload = _terminal_gateway_request_payload(key, {'id': key, 'user_id': 'alice', 'prompt': 'hello'}, response)
    write_json_file_atomic(paths.terminal / f'{key}.json', payload)
    write_json_file_atomic(paths.done / f'{key}.json', payload)
    write_json_file_atomic(paths.responses / f'{key}.json', response)
    mark_gateway_terminal_projection_complete(paths, key, response, settlement_pending=pending)
    chunk = paths.done / f'{key}.chunks.jsonl'
    chunk.write_bytes(b'{"event":"delta","text":"answer"}\n')
    return payload, response, chunk


# LLM: 原件删除前须可从同一规范读口准确回读；不允许为每个ID留下locator文件。
# 函数用途: 验证多文件合段、物理移除和原JSON读取fallback。
def test_gateway_receipt_archive_seals_and_reads_original_paths(tmp_path):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    payload, response, chunk = _settled(paths)
    report = _sealer()(paths, before=100, max_entries=4, max_ms=1000)
    assert report['sealed_requests'] == 1 and report['errors'] == []
    assert not (paths.terminal / 'request-a.json').exists()
    assert not (paths.responses / 'request-a.json').exists() and not chunk.exists()
    assert read_json_file(paths.terminal / 'request-a.json') == payload
    assert read_json_file(paths.responses / 'request-a.json') == response
    segments = list((paths.root / 'archive').glob('receipts-seg-*.jsonl'))
    assert len(segments) == 1
    rows = [json.loads(line) for line in segments[0].read_text().splitlines()]
    assert len(rows) >= 4
    assert not list((paths.root / 'archive').glob('request-a*'))


# LLM: 年轻、未结算、活跃processing和明确未知结果不能被终态目录名洗成安全回收对象。
# 函数用途: 验证不同保护事实均保留原件且不生成假封存回执。
@pytest.mark.parametrize('protection', ['young', 'settlement', 'processing', 'unknown'])
def test_gateway_receipt_archive_protects_unsettled_or_active(tmp_path, protection):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    _settled(paths, completed_at=1000 if protection == 'young' else 1, pending=protection == 'settlement')
    if protection == 'processing':
        write_json_file_atomic(paths.processing / 'request-a.json', {'id': 'request-a', 'status': 'processing'})
    if protection == 'unknown':
        row = read_json_file(paths.terminal / 'request-a.json')
        row['execution_unknown'] = True
        write_json_file_atomic(paths.terminal / 'request-a.json', row)
    report = _sealer()(paths, before=100, max_entries=4, max_ms=1000)
    assert report['sealed_requests'] == 0
    assert (paths.terminal / 'request-a.json').exists()


# LLM: 即使目录中有积压，每拍只观察指定数量的原候选，不用list/glob全量装入内存。
# 函数用途: 验证小预算多拍能够继续并减少多个松散请求。
def test_gateway_receipt_archive_small_budget_progresses(tmp_path):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    for key in ('one', 'two', 'three'):
        _settled(paths, key)
    totals = []
    for _ in range(6):
        report = _sealer()(paths, before=100, max_entries=1, max_ms=1000)
        assert report['scanned_entries'] <= 1
        totals.append(report['sealed_requests'])
    assert sum(totals) == 3
    assert all(read_json_file(paths.responses / f'{key}.json')['id'] == key for key in ('one', 'two', 'three'))


# LLM: 发布段失败时原件与投影必须保留，不能先unlink再尝试写归档。
# 函数用途: 验证备份/索引未成功提交前无破坏性删除。
def test_gateway_receipt_archive_failed_segment_write_keeps_loose(tmp_path, monkeypatch):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    _settled(paths)
    sealer = _sealer()

    # LLM: 注入真实段发布口的失败，不替换原读口或清理结果。
    # 函数用途: 模拟磁盘故障发生于第一份不可变段发布。
    def fail(*args, **kwargs):
        raise OSError('segment disk full')

    monkeypatch.setattr(archive, 'write_bytes_atomic_beneath', fail)
    report = sealer(paths, before=100, max_entries=4, max_ms=1000)
    assert report['errors'] and report['sealed_requests'] == 0
    assert (paths.terminal / 'request-a.json').exists() and (paths.responses / 'request-a.json').exists()


# LLM: 发布段成功、manifest回执失败会留下可验证孤儿段；重试不能永久卡死或删除未索引原件。
# 函数用途: 验证中途崩溃后同一批前像可安全重试。
def test_gateway_receipt_archive_recovers_segment_publish_interruption(tmp_path, monkeypatch):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    _settled(paths)
    original = archive.write_bytes_atomic_beneath

    # LLM: 只中断manifest写一次，保留真实已发布段与全部原件。
    # 函数用途: 模拟段发布之后进程未完成manifest。
    def fail_manifest(root, parts, data, **kwargs):
        if parts[-1] == 'receipts-manifest.json':
            raise OSError('manifest failed')
        return original(root, parts, data, **kwargs)

    monkeypatch.setattr(archive, 'write_bytes_atomic_beneath', fail_manifest)
    assert _sealer()(paths, before=100, max_ms=1000)['errors']
    assert (paths.terminal / 'request-a.json').exists()
    monkeypatch.setattr(archive, 'write_bytes_atomic_beneath', original)
    result = _sealer()(paths, before=100, max_ms=1000)
    assert result['sealed_requests'] == 1 and result['errors'] == []
    assert read_json_file(paths.responses / 'request-a.json')['output'] == 'answer'


# LLM: 发布时捕获的旧前像不能掩盖随后出现的processing或结算欠账；拿回原T后必须重核。
# 函数用途: 验证发布与删除之间重新活跃的请求仍保留全部热文件。
def test_gateway_receipt_archive_rechecks_active_after_publication(tmp_path, monkeypatch):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    _settled(paths)
    publish = archive._publish_receipt_segment

    # LLM: 发布后模拟同请求恢复变活，不更改归档或删除逻辑。
    # 函数用途: 在两个T窗口之间注入真实在途文件。
    def reactivate(*args):
        publish(*args)
        write_json_file_atomic(paths.processing / 'request-a.json', {'id': 'request-a'})

    monkeypatch.setattr(archive, '_publish_receipt_segment', reactivate)
    result = _sealer()(paths, before=100, max_ms=1000)
    assert result['sealed_requests'] == 0
    assert (paths.terminal / 'request-a.json').exists()
    assert (paths.responses / 'request-a.json').exists()


# LLM: 从真实默认owner维护入口观察账本与归档；读口切换已闭合：非legal_hold封存后删除原件，legal_hold不封存也不删除。
# 函数用途: 验证生产维护调用真实适配器并持久化同一份游标。
@pytest.mark.parametrize('hold', [False, True])
def test_default_owner_maintenance_runs_protected_storage_adapters(tmp_path, hold):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_maintenance import run_owner_retention_if_due
    from agent_py_agent.agent.user_space.runtime_paths import runtime_paths_for_agent
    home = ensure_my_agent_home(tmp_path / 'home')
    root = runtime_paths_for_agent(None, home.owner_home_dir, home)['gateway_workspace']
    paths = gateway_paths_from_root(root)
    _settled(paths)
    policy = json.loads(home.owner_retention_json.read_text())
    policy['legal_hold'] = hold
    home.owner_retention_json.write_text(json.dumps(policy))
    result = run_owner_retention_if_due(home, now=2_000_000, include_global_indexes=False)
    assert result.ran
    marker = json.loads((home.owner_data_dir / 'maintenance.json').read_text())
    storage = marker.get('storage_maintenance', {})
    if hold:
        # 法律保留只推进无删除投影与配额观察，不能为满足旧游标形状调用封存写者。
        assert storage['retention_deletion_safe'] is False
        assert storage['deferred_reason'] == 'retention_not_safe'
        assert 'gateway_terminal' not in storage.get('cursors', {})
        assert 'quota_reconciliation' in storage
    else:
        assert {'gateway_terminal', 'cache_tmp_trash'} <= set(storage.get('cursors', {}))
        assert storage['unbound_kinds'] == []
        assert storage['coordinator_deferred'] == ['global_indexes']
    assert (home.owner_home_dir / '.owner-quota-ledger.json').is_file()
    assert bool(list((paths.root / 'archive').glob('receipts-seg-*.jsonl'))) is not hold
    assert (paths.terminal / 'request-a.json').exists() is hold
    assert (paths.responses / 'request-a.json').exists() is hold
    assert archive.gateway_terminal_exists(paths, 'request-a.json')


# LLM: 配额首次扫描用显式有界预算推进，完成后仍核归档幂等和原周期不忙循环。
# 函数用途: 验证真实维护可收敛且下一周期不会重复发布同一终态副本。
def test_default_maintenance_archive_is_idempotent_without_busy_partial(tmp_path):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_maintenance import (
        owner_maintenance_due,
        run_owner_retention_if_due,
    )
    from agent_py_agent.agent.user_space.runtime_paths import runtime_paths_for_agent
    from agent_py_agent.agent.user_space.storage_lanes import LANE_BINDINGS

    home = ensure_my_agent_home(tmp_path / 'home')
    paths = gateway_paths_from_root(runtime_paths_for_agent(None, home.owner_home_dir, home)['gateway_workspace'])
    _settled(paths)

    def cycle(start):
        # 新投影各有扫描/修复阶段；按原注册车道数给有限片数，不把首片或旧八片当完整周期。
        for tick in range(len(LANE_BINDINGS)):
            now = start + tick
            assert run_owner_retention_if_due(home, now=now, include_global_indexes=False,
                quota_max_ms=1000, quota_max_entries=4096).ran
            state = json.loads((home.owner_data_dir / 'maintenance.json').read_text())
            if not state['maintenance_partial']:
                return state, now
        pytest.fail('registered maintenance lanes did not settle within one bounded fixture cycle')

    state, _ = cycle(2_000_000)
    assert state['maintenance_partial'] is False
    assert state['storage_maintenance']['continuation_pending'] is False
    state, completed_at = cycle(2_086_401)
    manifest = json.loads((paths.root / 'archive' / 'receipts-manifest.json').read_text())
    assert manifest['segment_count'] == 1
    assert state['storage_maintenance']['partial'] is True
    assert state['maintenance_partial'] is False
    assert state['storage_maintenance']['cursors']['gateway_terminal']['archived_requests'] == 0
    assert state['storage_maintenance']['continuation_pending'] is False
    assert not owner_maintenance_due(home.owner_home_dir, now=completed_at + 1)


# LLM: C/W原transition不能被新adapter绕过；策略损坏也不能被当默认许可写账或归档。
# 函数用途: 验证延期路径不调用新领域写者，且不伪报全部完成。
@pytest.mark.parametrize('reason', ['coordinator', 'policy'])
def test_storage_maintenance_defers_without_authorized_adapter_protocol(tmp_path, monkeypatch, reason):
    from types import SimpleNamespace

    from agent_py_agent.agent.user_space import owner_quota
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_maintenance import (
        run_owner_storage_maintenance_slice,
    )

    home = ensure_my_agent_home(tmp_path / 'home')
    monkeypatch.setattr(owner_quota, 'reconcile_owner_quota_slice', lambda *a, **kw: pytest.fail('unauthorized quota write'))
    monkeypatch.setattr(archive, 'seal_gateway_receipts_slice', lambda *a, **kw: pytest.fail('unauthorized receipt write'))
    policy = SimpleNamespace(load_errors=['bad'] if reason == 'policy' else [], applied=True, legal_hold=False)
    result = run_owner_storage_maintenance_slice(home, {}, policy, now=2_000_000, coordinator=reason == 'coordinator')
    assert result['partial'] and result['unbound_kinds']
    assert result['continuation_pending'] is False and result['deletion_enabled'] is False
    assert not (home.owner_home_dir / '.owner-quota-ledger.json').exists()


# LLM: 单片不足必须保留待续责任，不能为幂等测试变绿把真实partial改成完成。
# 函数用途: 用一项预算确定性触发默认维护配额未完成。
def test_default_maintenance_small_quota_budget_reports_partial(tmp_path):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_maintenance import run_owner_retention_if_due

    home = ensure_my_agent_home(tmp_path / 'home')
    assert run_owner_retention_if_due(home, now=2_000_000, include_global_indexes=False,
        quota_max_entries=1, quota_max_ms=1000).ran
    state = json.loads((home.owner_data_dir / 'maintenance.json').read_text())
    assert state['maintenance_partial'] is True
    assert state['storage_maintenance']['continuation_pending'] is True
    assert state['storage_maintenance']['cursors']['cache_tmp_trash']['complete'] is False


def _write_oversized_chunk(chunk_path, target_bytes: int) -> int:
    line = b'{"kind":"progress","text":"' + b'x' * 2000 + b'"}\n'
    repeats = target_bytes // len(line) + 1
    with chunk_path.open('wb') as handle:
        handle.write(line * repeats)
    return chunk_path.stat().st_size


def test_gateway_receipt_archive_defers_oversized_chunk_and_seals_followers(tmp_path, monkeypatch):
    import os

    from agent_py_agent.agent.gateway_parts.io import read_json_file

    paths = gateway_paths_from_root(tmp_path / 'gateway')
    large_payload, large_response, large_chunk = _settled(paths, 'oversize-first')
    chunk_bytes = _write_oversized_chunk(large_chunk, 100 * 1024 * 1024)
    assert chunk_bytes > 100 * 1024 * 1024
    followers = {f'tail-{idx:04d}': _settled(paths, f'tail-{idx:04d}') for idx in range(20)}

    def ordered_entries(folder):
        with os.scandir(folder) as stream:
            rows = sorted(stream, key=lambda entry: (entry.name != 'oversize-first.json', entry.name))
        yield from rows

    monkeypatch.setattr(archive, '_receipt_scan_entries', ordered_entries)
    try:
        report = _sealer()(paths, before=100, max_entries=128, max_ms=5000)
    finally:
        for it in archive._RECEIPT_SCANS.values():
            it.close()
        archive._RECEIPT_SCANS.clear()

    assert report['complete'] is True
    assert report['errors'] == []
    assert report['resume_after'] == ''
    assert report['sealed_requests'] == 20
    assert report['deferred_requests'] == [{
        'request_id': 'oversize-first',
        'entry': 'oversize-first.json',
        'path': 'requests/done/oversize-first.chunks.jsonl',
        'reason': 'source_exceeds_read_limit',
    }]
    assert (paths.terminal / 'oversize-first.json').exists()
    assert (paths.done / 'oversize-first.json').exists()
    assert (paths.responses / 'oversize-first.json').exists()
    assert large_chunk.stat().st_size == chunk_bytes
    assert read_json_file(paths.terminal / 'oversize-first.json') == large_payload
    assert read_json_file(paths.responses / 'oversize-first.json') == large_response
    for key, (payload, response, chunk) in followers.items():
        assert not (paths.terminal / f'{key}.json').exists()
        assert not chunk.exists()
        assert archive.gateway_terminal_exists(paths, f'{key}.json')
        assert read_json_file(paths.terminal / f'{key}.json') == payload
        assert read_json_file(paths.responses / f'{key}.json') == response

    # 对照：仅将超大流缩回 1 MiB 以内后，下一拍可干净封存该请求。
    large_chunk.write_bytes(b'{"event":"delta","text":"answer"}\n')
    followup = _sealer()(paths, before=100, max_entries=128, max_ms=1000)
    assert followup['complete'] is True
    assert followup['errors'] == []
    assert followup['deferred_requests'] == []
    assert followup['sealed_requests'] == 1
    assert not large_chunk.exists()
    assert read_json_file(paths.responses / 'oversize-first.json') == large_response


def test_gateway_receipt_archive_defers_oversized_bundle_but_pauses_on_small_byte_budget(tmp_path, monkeypatch):
    import os

    paths = gateway_paths_from_root(tmp_path / 'gateway')
    _settled(paths, 'bundle-big')
    payload_chunk = b'{"x":"' + b'y' * (archive._RECEIPT_MAX_FILE_BYTES - 16) + b'"}\n'
    assert len(payload_chunk) <= archive._RECEIPT_MAX_FILE_BYTES
    paths.failed.mkdir(parents=True, exist_ok=True)
    for folder in (paths.done, paths.failed, paths.responses):
        (folder / 'bundle-big.chunks.jsonl').write_bytes(payload_chunk)
    (paths.failed / 'bundle-big.json').write_bytes(payload_chunk)
    _settled(paths, 'tail-0')

    def ordered_entries(folder):
        with os.scandir(folder) as stream:
            rows = sorted(stream, key=lambda entry: (entry.name != 'bundle-big.json', entry.name))
        yield from rows

    monkeypatch.setattr(archive, '_receipt_scan_entries', ordered_entries)
    try:
        report = _sealer()(paths, before=100, max_entries=16, max_ms=1000)
    finally:
        for it in archive._RECEIPT_SCANS.values():
            it.close()
        archive._RECEIPT_SCANS.clear()

    assert report['complete'] is True and report['errors'] == []
    assert report['sealed_requests'] == 1
    assert [item['request_id'] for item in report['deferred_requests']] == ['bundle-big']
    assert report['deferred_requests'][0]['reason'] == 'bundle_exceeds_segment_budget'
    assert (paths.terminal / 'bundle-big.json').exists()
    assert not (paths.terminal / 'tail-0.json').exists()

    # 小于单条正常记录的 slice max_bytes 属于预算耗尽，不应错当成 source deferral 跳过。
    small_paths = gateway_paths_from_root(tmp_path / 'gateway-small')
    _settled(small_paths, 'ordinary-small')
    paused = _sealer()(small_paths, before=100, max_entries=16, max_bytes=100, max_ms=1000)
    assert paused['complete'] is False
    assert paused['sealed_requests'] == 0
    assert paused['deferred_requests'] == []
    assert (small_paths.terminal / 'ordinary-small.json').exists()


@pytest.mark.parametrize('protection', ['young', 'settlement', 'processing', 'unknown'])
def test_gateway_receipt_archive_protects_oversized_unsettled_or_active(tmp_path, protection):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    _, _, chunk = _settled(paths, completed_at=1000 if protection == 'young' else 1, pending=protection == 'settlement')
    _write_oversized_chunk(chunk, archive._RECEIPT_MAX_FILE_BYTES + 4096)
    if protection == 'processing':
        write_json_file_atomic(paths.processing / 'request-a.json', {'id': 'request-a', 'status': 'processing'})
    if protection == 'unknown':
        row = read_json_file(paths.terminal / 'request-a.json')
        row['execution_unknown'] = True
        write_json_file_atomic(paths.terminal / 'request-a.json', row)
    report = _sealer()(paths, before=100, max_entries=4, max_ms=1000)
    assert report['complete'] is True and report['errors'] == []
    assert report['sealed_requests'] == 0 and report['deferred_requests'] == []
    assert (paths.terminal / 'request-a.json').exists() and chunk.exists()


@pytest.mark.parametrize('fail_target', ['segment', 'manifest'])
def test_gateway_receipt_archive_publish_error_preserves_all_with_deferred_present(tmp_path, monkeypatch, fail_target):
    import os

    paths = gateway_paths_from_root(tmp_path / 'gateway')
    _, _, large_chunk = _settled(paths, 'oversize-first')
    _write_oversized_chunk(large_chunk, archive._RECEIPT_MAX_FILE_BYTES + 4096)
    for idx in range(5):
        _settled(paths, f'tail-{idx:02d}')

    def ordered_entries(folder):
        with os.scandir(folder) as stream:
            rows = sorted(stream, key=lambda entry: (entry.name != 'oversize-first.json', entry.name))
        yield from rows

    original_write = archive.write_bytes_atomic_beneath

    def fail_write(root, parts, data, **kwargs):
        if fail_target == 'segment' and parts[-1].startswith('receipts-seg-'):
            raise OSError('simulated segment publish error')
        if fail_target == 'manifest' and parts[-1] == 'receipts-manifest.json':
            raise OSError('simulated manifest publish error')
        return original_write(root, parts, data, **kwargs)

    monkeypatch.setattr(archive, '_receipt_scan_entries', ordered_entries)
    monkeypatch.setattr(archive, 'write_bytes_atomic_beneath', fail_write)
    try:
        report = _sealer()(paths, before=100, max_entries=32, max_ms=1000)
    finally:
        for it in archive._RECEIPT_SCANS.values():
            it.close()
        archive._RECEIPT_SCANS.clear()

    assert report['complete'] is False
    assert report['sealed_requests'] == 0
    assert report['resume_after'] == ''
    assert len(report['errors']) == 1 and report['errors'][0]['error_type'] == 'OSError'
    assert report['errors'][0]['side_effects_unknown'] is True
    assert (paths.terminal / 'oversize-first.json').exists() and large_chunk.exists()
    for idx in range(5):
        assert (paths.terminal / f'tail-{idx:02d}.json').exists()
        assert (paths.responses / f'tail-{idx:02d}.json').exists()


def test_owner_maintenance_advances_runtime_phases_past_deferred_oversized_receipt(tmp_path, monkeypatch):
    import os

    from agent_py_agent.agent.gateway_parts import input_delivery_service as inputs
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_maintenance import (
        owner_maintenance_due,
        run_owner_retention_if_due,
    )
    from agent_py_agent.agent.user_space.runtime_paths import runtime_paths_for_agent

    home = ensure_my_agent_home(tmp_path / 'home')
    paths = gateway_paths_from_root(runtime_paths_for_agent(None, home.owner_home_dir, home)['gateway_workspace'])
    _, _, large_chunk = _settled(paths, 'oversize-first')
    chunk_size = _write_oversized_chunk(large_chunk, archive._RECEIPT_MAX_FILE_BYTES + 4096)
    for idx in range(20):
        _settled(paths, f'tail-{idx:04d}')

    prepared = {'id': 'input-a', 'kind': 'ask', 'goal': 'bounded input',
                'metadata': {'client_input_digest': 'digest-input-a'}}
    input_row = inputs.GatewayInputReceipt('input-a', 'digest-input-a', 'input-a', 'consumed', prepared,
        inputs._prepared_payload_digest(prepared), 'input-a', updated_at=1)
    input_path = inputs.gateway_input_receipt_path(paths, 'input-a')
    write_json_file_atomic(input_path, input_row.to_dict())
    outbox_path = paths.root / 'client_outbox' / 'empty.json'
    write_json_file_atomic(outbox_path, {'schema_version': 'tui_active_input_outbox.v3', 'entries': {}, 'updated_at': 1})

    def ordered_entries(folder):
        with os.scandir(folder) as stream:
            rows = sorted(stream, key=lambda entry: (entry.name != 'oversize-first.json', entry.name))
        yield from rows

    monkeypatch.setattr(archive, '_receipt_scan_entries', ordered_entries)
    state = {}
    for tick in range(10):
        result = run_owner_retention_if_due(home, now=2_000_000 + tick, include_global_indexes=False,
                                            quota_max_entries=4096, quota_max_ms=1000)
        assert result.ran
        state = json.loads((home.owner_data_dir / 'maintenance.json').read_text())
        if not state['maintenance_partial']:
            break

    assert state['maintenance_partial'] is False
    assert state['storage_maintenance']['errors'] == []
    assert state['storage_maintenance']['continuation_pending'] is False
    gt_cursor = state['storage_maintenance']['cursors']['gateway_terminal']
    assert gt_cursor['complete'] is True
    assert [item['request_id'] for item in gt_cursor['deferred_requests']] == ['oversize-first']
    assert not owner_maintenance_due(home.owner_home_dir, now=2_000_100)
    assert (paths.terminal / 'oversize-first.json').exists() and large_chunk.stat().st_size == chunk_size
    assert all(not (paths.terminal / f'tail-{idx:04d}.json').exists() for idx in range(20))
    assert all(archive.gateway_terminal_exists(paths, f'tail-{idx:04d}.json') for idx in range(20))
    assert not input_path.exists() and (paths.root / 'archive/input_receipts/197001/input-a.json').is_file()
    assert not outbox_path.exists() and (paths.root / 'archive/client_outbox/197001/empty.json').is_file()


def test_owner_maintenance_fatal_publish_error_still_blocks_runtime_phases(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import input_delivery_service as inputs
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_maintenance import run_owner_retention_if_due
    from agent_py_agent.agent.user_space.runtime_paths import runtime_paths_for_agent

    home = ensure_my_agent_home(tmp_path / 'home')
    paths = gateway_paths_from_root(runtime_paths_for_agent(None, home.owner_home_dir, home)['gateway_workspace'])
    _settled(paths, 'ordinary-a')
    prepared = {'id': 'input-a', 'kind': 'ask', 'goal': 'bounded input',
                'metadata': {'client_input_digest': 'digest-input-a'}}
    input_row = inputs.GatewayInputReceipt('input-a', 'digest-input-a', 'input-a', 'consumed', prepared,
        inputs._prepared_payload_digest(prepared), 'input-a', updated_at=1)
    input_path = inputs.gateway_input_receipt_path(paths, 'input-a')
    write_json_file_atomic(input_path, input_row.to_dict())
    outbox_path = paths.root / 'client_outbox' / 'empty.json'
    write_json_file_atomic(outbox_path, {'schema_version': 'tui_active_input_outbox.v3', 'entries': {}, 'updated_at': 1})

    monkeypatch.setattr(archive, '_publish_receipt_segment', lambda *a, **kw: (_ for _ in ()).throw(OSError('publish fail')))
    assert run_owner_retention_if_due(home, now=2_000_000, include_global_indexes=False,
                                      quota_max_entries=4096, quota_max_ms=1000).ran
    state = json.loads((home.owner_data_dir / 'maintenance.json').read_text())
    gt_cursor = state['storage_maintenance']['cursors']['gateway_terminal']
    assert gt_cursor['complete'] is False
    assert gt_cursor['errors'] and gt_cursor['errors'][0]['error_type'] == 'OSError'
    assert 'runtime_directory' not in gt_cursor
    assert state['maintenance_partial'] is True
    assert (paths.terminal / 'ordinary-a.json').exists()
    assert input_path.exists() and outbox_path.exists()
