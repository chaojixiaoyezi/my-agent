"""F08: real 16 MiB ceiling, bounded point updates and restartable migration.

Source: gpt-rework-r3-f08.md (strict three-file slice). Reproduce with pytest on
this file plus test_v15_w10_alert_topic_anchors.py and test_v15_w10_isolation.py.
No provider traffic; reservations, files, locks and subprocess restarts are real.
"""
from __future__ import annotations

import ast
import errno
import importlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_py_agent.agent.scheduler import alert_topic_anchors as anchors

MODULE = 'agent_py_agent.agent.scheduler.alert_topic_anchor_migration'


# LLM: Match the actual production ceiling without adding a module-level numeric declaration.
# 类用途: 保留测试使用的真实生产上限，不缩小阈值制造回归。
class _RegressionLimits:
    LIMIT = 16777216
    LARGE_ROWS = 513
    ROW_METADATA_BYTES = 32768


# LLM: Check declaration placement, not just names, so aliases/expressions cannot bypass the gate.
# 函数用途: 防止两个生产模块重新声明未注册的模块级数字常量。
@pytest.mark.parametrize('module_name', [anchors.__name__, MODULE])
def test_numeric_limits_are_not_declared_at_module_scope(module_name):
    module = importlib.import_module(module_name)
    tree = ast.parse(Path(module.__file__).read_text(encoding='utf-8'))
    assignments = (node for node in tree.body if isinstance(node, (ast.Assign, ast.AnnAssign)))
    offenders = [ast.unparse(node) for node in assignments if any(
        isinstance(value, ast.Constant) and type(value.value) in (int, float, complex)
        for value in ast.walk(node.value))]
    assert offenders == []


# LLM: Deterministic identities belong only to synthetic temporary owners.
# 函数用途: 构造合法且容易核对的事件键。
def _key(index):
    return f'{index:064x}'


# LLM: Every persisted field and original dispatch is checked after migration.
# 函数用途: 生成完整的旧版锚点和原始派发键。
def _row(index, *, metadata_bytes=0):
    row = {'root_message_id': 'om_' + 'r' * 48 + str(index),
        'thread_id': 'omt_' + 't' * 48 + str(index),
        'state': 'unknown' if index % 2 else 'sent',
        'updated_at': '2026-11-04T18:00:00.000000+00:00'}
    if metadata_bytes:
        row['legacy_metadata'] = 'x' * metadata_bytes
    return row


# LLM: Build genuine v1 persisted history; do not simulate active tasks or lower a quota.
# 函数用途: 写入可以被旧实现正常写出的历史累计账本。
def _legacy(home, count, *, keys_first=False, metadata_bytes=0):
    path = home / 'data/alert_topic_anchors.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {'schema_version': 'alert_topic_anchors.v1',
        'anchors': {_key(i): _row(i, metadata_bytes=metadata_bytes) for i in range(count)},
        'root_delivery_keys': {_key(i): f'original-dispatch-{i}' for i in range(count)}}
    if keys_first:
        data = {'root_delivery_keys': data['root_delivery_keys'],
            'schema_version': data['schema_version'], 'anchors': data['anchors']}
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    return path


# LLM: An absent new API is an assertion failure, never a collection/import error.
# 函数用途: 取得真实迁移模块。
def _migration():
    assert importlib.util.find_spec(MODULE) is not None, 'bounded migration API is missing'
    return importlib.import_module(MODULE)


# LLM: The optional baseline path still exercises the old reserve and its real ceiling.
# 函数用途: 在新实现存在时跑完有界迁移，旧基线仍直接验证原始故障。
def _finish(home, *, max_records=256, restart=False):
    if importlib.util.find_spec(MODULE) is None:
        return []
    module = _migration()
    reports = []
    for _ in range(2000):
        report = module.migrate_legacy(home, max_records=max_records)
        assert 0 <= report['records'] <= max_records
        assert 0 <= report['bytes_read'] <= module._MigrationLimits.MAX_MIGRATION_BYTES
        reports.append(report)
        if report['complete']:
            return reports
        if restart:
            module = importlib.reload(module)
    pytest.fail('bounded migration did not converge')


# LLM: 保留真实16MiB门限及多批迁移，用合法旧元数据增大字节量，避免六万次逐文件提交。
# 函数用途: 准备超限字节夹具；独立多小记录用例另守记录条数维度。
@pytest.fixture(scope='module')
def large_history(tmp_path_factory):
    home = tmp_path_factory.mktemp('f08-over-limit')
    source = _legacy(home, _RegressionLimits.LARGE_ROWS, metadata_bytes=_RegressionLimits.ROW_METADATA_BYTES)
    size = source.stat().st_size
    assert _RegressionLimits.LIMIT < size < 100_000_000
    reports = _finish(home)
    return home, size, reports


# LLM: This was red at the original production ceiling; no mock replaces the reader/writer.
# 函数用途: 核验超限历史迁移保留所有抽查的已送达、未知和派发证明，且接受新事件。
def test_over_limit_history_preserves_proof_and_accepts_new_event(large_history):
    home, size, reports = large_history
    assert size > _RegressionLimits.LIMIT
    for index in (0, 1, 255, 256, 300, _RegressionLimits.LARGE_ROWS - 2, _RegressionLimits.LARGE_ROWS - 1):
        row, first, dispatch = anchors._reserve_for_delivery(home, _key(index), now='reopen', dispatch_key='retry')
        assert row == _row(index, metadata_bytes=_RegressionLimits.ROW_METADATA_BYTES)
        assert first is False and dispatch == f'original-dispatch-{index}'
    row, first, dispatch = anchors._reserve_for_delivery(home, _key(70001), now='new', dispatch_key='new-dispatch')
    assert row['state'] == 'pending' and first is True and dispatch == 'new-dispatch'
    assert reports and len(reports) > 1


# LLM: Observe actual bounded-reader/writer bytes; ready and exact-key files are the only reads.
# 函数用途: 对比小历史与真实超限历史的单事件实际读写开销。
def test_hot_path_io_is_independent_of_history(tmp_path, large_history, monkeypatch):
    small = tmp_path / 'small'
    anchors.reserve(small, _key(999), now='init')
    observed = []
    read, write = anchors.read_bytes_beneath, anchors.write_text_atomic_beneath
    for home in (small, large_history[0]):
        reads, writes = [], []

        # LLM: Instrument the actual primitive without replacing storage results.
        # 函数用途: 记录真实读取路径、上限和字节数。
        def read_probe(root, parts, **kwargs):
            raw = read(root, parts, **kwargs)
            reads.append((parts, kwargs.get('max_bytes'), len(raw or b'')))
            return raw

        # LLM: All written content still reaches the original durable writer.
        # 函数用途: 记录真实原子写入字节。
        def write_probe(root, parts, text, **kwargs):
            writes.append((parts, len(text.encode('utf-8'))))
            return write(root, parts, text, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(anchors, 'read_bytes_beneath', read_probe)
            patch.setattr(anchors, 'write_text_atomic_beneath', write_probe)
            key = _key(80000)
            assert anchors._reserve_for_delivery(home, key, now='reserve', dispatch_key='original')[1] is True
            anchors.mark_unknown(home, key, now='unknown')
            anchors.mark_sent(home, key, root_message_id='om_confirmed', thread_id='omt_confirmed', now='sent')
            row, first, dispatch = anchors._reserve_for_delivery(home, key, now='retry', dispatch_key='different')
            assert row['root_message_id'] == 'om_confirmed' and first is False and dispatch == 'original'
        assert all(limit is not None and limit <= _RegressionLimits.LIMIT for _, limit, _ in reads)
        assert len(reads) <= 12 and len(writes) == 3
        assert sum(size for _, _, size in reads) < 32768
        assert sum(size for _, size in writes) < 16384
        assert all(parts != ('data', 'alert_topic_anchors.json') for parts, _, _ in reads)
        observed.append((len(reads), len(writes)))
    assert observed[0] == observed[1]


def test_many_small_records_migrate_across_multiple_batches(tmp_path):
    count = 1201
    _legacy(tmp_path, count)
    reports = _finish(tmp_path, max_records=256)
    assert len(reports) > 4
    for index in range(count):
        row, first, dispatch = anchors._reserve_for_delivery(tmp_path, _key(index), now='reopen')
        assert row == _row(index)
        assert first is False and dispatch == f'original-dispatch-{index}'


# LLM: Hold one real write open; a different event must finish before the first is released.
# 函数用途: 证明锁粒度不再覆盖整个 Owner。
def test_different_events_do_not_serialize_on_owner_ledger(tmp_path, monkeypatch):
    for index in range(3):
        anchors.reserve(tmp_path, _key(index), now='init')
    entered, release = threading.Event(), threading.Event()
    write = anchors.write_text_atomic_beneath

    # LLM: Block exactly the selected event's actual durable-write boundary.
    # 函数用途: 在确定位置悬停一个事件写入。
    def blocked(root, parts, text, **kwargs):
        row = json.loads(text).get('anchors', {}).get(_key(1), {})
        if row.get('updated_at') == 'paused':
            entered.set()
            assert release.wait(10)
        return write(root, parts, text, **kwargs)

    monkeypatch.setattr(anchors, 'write_text_atomic_beneath', blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(anchors.mark_unknown, tmp_path, _key(1), now='paused')
        assert entered.wait(5)
        try:
            second = pool.submit(anchors.mark_unknown, tmp_path, _key(2), now='independent')
            assert second.result(timeout=5)['updated_at'] == 'independent'
        finally:
            release.set()
        assert first.result(timeout=5)['state'] == 'unknown'


# LLM: Real contention must grant one initial dispatch and preserve that winner's correlation.
# 函数用途: 核验同一事件的线程并发预留。
def test_same_event_has_one_winner_and_one_atomic_correlation(tmp_path):
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(anchors._reserve_for_delivery, tmp_path, _key(33), now=str(i), dispatch_key=str(i))
                   for i in range(24)]
        results = [future.result(timeout=10) for future in futures]
    winners = [result for result in results if result[1]]
    assert len(winners) == 1
    assert all(result[0] == winners[0][0] and result[2] == winners[0][2] for result in results)
    sent = anchors.mark_sent(tmp_path, _key(33), root_message_id='om_root', thread_id='', now='sent')
    enriched = anchors.mark_sent(tmp_path, _key(33), root_message_id='om_root', thread_id='omt_thread', now='thread')
    assert sent['thread_id'] == '' and enriched['thread_id'] == 'omt_thread'
    assert anchors.mark_unknown(tmp_path, _key(33), now='late') == enriched
    with pytest.raises(ValueError, match='cannot change'):
        anchors.mark_sent(tmp_path, _key(33), root_message_id='om_other', thread_id='', now='bad')


# LLM: Keep the real limit and reject an unreadable candidate before changing durable proof.
# 函数用途: 核验成功写入永不超过其读取上限，失败更新不损坏旧记录。
def test_oversized_point_update_is_rejected_before_replace(tmp_path):
    expected = anchors.reserve(tmp_path, _key(1), now='original')
    with pytest.raises(ValueError, match='limit|large|bound'):
        anchors.mark_sent(tmp_path, _key(1), root_message_id='x' * _RegressionLimits.LIMIT, thread_id='', now='oversize')
    assert anchors.reserve(tmp_path, _key(1), now='reopen') == expected


# LLM: Check both ordering variants emitted by valid JSON serializers, including keys before rows.
# 函数用途: 核验迁移不依赖顶层字段顺序和是否带派发键。
@pytest.mark.parametrize('keys_first', [False, True])
def test_incremental_migration_reopens_and_closes_all_descriptors(tmp_path, keys_first):
    _legacy(tmp_path, 17, keys_first=keys_first)
    fd_dir = '/dev/fd' if Path('/dev/fd').is_dir() else '/proc/self/fd'
    before = len(os.listdir(fd_dir))
    reports = _finish(tmp_path, max_records=1, restart=True)
    assert len(reports) > 17
    assert len(os.listdir(fd_dir)) == before
    for index in range(17):
        row, first, dispatch = anchors._reserve_for_delivery(tmp_path, _key(index), now='restart')
        assert row == _row(index) and first is False and dispatch == f'original-dispatch-{index}'


# LLM: A partially imported point is not yet evidence that missing old keys never existed.
# 函数用途: 核验迁移中所有写入口都不会给未知旧事件重新授予根发送许可。
def test_incomplete_migration_is_not_absence_for_any_public_writer(tmp_path):
    module = _migration()
    _legacy(tmp_path, 1024)
    assert module.migrate_legacy(tmp_path, max_records=1)['complete'] is False
    operations = (
        lambda: anchors.reserve(tmp_path, _key(99999), now='new'),
        lambda: anchors._reserve_for_delivery(tmp_path, _key(1023), now='retry'),
        lambda: anchors.mark_unknown(tmp_path, _key(99999), now='new'),
        lambda: anchors.mark_sent(tmp_path, _key(99999), root_message_id='om_new', thread_id='', now='new'),
    )
    for operation in operations:
        with pytest.raises(module.MigrationPending):
            operation()
    _finish(tmp_path)
    assert anchors._reserve_for_delivery(tmp_path, _key(1023), now='reopen')[0] == _row(1023)


# LLM: Crash after a durable point but before its cursor checkpoint; replay must be idempotent.
# 函数用途: 核验记录提交后检查点前中断可以恢复，原派发关联不丢失。
def test_crash_after_point_commit_resumes_without_new_root(tmp_path, monkeypatch):
    module = _migration()
    _legacy(tmp_path, 9)
    stage = module._stage_entry
    calls = []

    # LLM: The real point write occurs before the injected process-cut equivalent.
    # 函数用途: 在第三个真实记录提交后模拟进程中断。
    def cut(*args, **kwargs):
        result = stage(*args, **kwargs)
        calls.append(True)
        if len(calls) == 3:
            raise RuntimeError('injected crash after point commit')
        return result

    with monkeypatch.context() as patch:
        patch.setattr(module, '_stage_entry', cut)
        with pytest.raises(RuntimeError, match='injected crash'):
            module.migrate_legacy(tmp_path, max_records=8)
    _finish(tmp_path, max_records=2, restart=True)
    for index in range(9):
        row, first, dispatch = anchors._reserve_for_delivery(tmp_path, _key(index), now='replay')
        assert row == _row(index) and first is False and dispatch == f'original-dispatch-{index}'


# LLM: The source fence is replaced only after import is durable; publication can safely resume.
# 函数用途: 核验格式切换后、就绪标记前中断仍不会清空历史。
def test_cutover_before_ready_publication_is_restartable(tmp_path, monkeypatch):
    module = _migration()
    path = _legacy(tmp_path, 4)

    # LLM: Raise at the real final publication boundary, not instead of point imports.
    # 函数用途: 模拟最终就绪标记写入失败。
    def cut(*args, **kwargs):
        raise OSError('injected ready publication failure')

    with monkeypatch.context() as patch:
        patch.setattr(module, '_publish_ready', cut)
        with pytest.raises(OSError, match='publication failure'):
            module.migrate_legacy(tmp_path)
    assert json.loads(path.read_text())['schema_version'] == 'alert_topic_anchors.v2'
    _finish(tmp_path, restart=True)
    assert anchors._reserve_for_delivery(tmp_path, _key(3), now='restart')[2] == 'original-dispatch-3'


# LLM: Changed source identity/content invalidates the cursor; never silently start from empty.
# 函数用途: 核验分片间旧文件被改动会失败关闭。
def test_changed_legacy_source_does_not_reset_migration(tmp_path):
    module = _migration()
    path = _legacy(tmp_path, 50)
    assert module.migrate_legacy(tmp_path, max_records=1)['complete'] is False
    with path.open('ab') as handle:
        handle.write(b' ')
    for _ in range(2):
        with pytest.raises(ValueError, match='changed'):
            module.migrate_legacy(tmp_path, max_records=2)
    with pytest.raises(ValueError, match='changed'):
        anchors._reserve_for_delivery(tmp_path, _key(99999), now='must-not-send')


# LLM: Invalid legacy data must remain unresolved, including corruption after valid imported rows.
# 函数用途: 核验坏账本绝不被当作没有旧锚点。
@pytest.mark.parametrize('damage', ['truncated', 'schema', 'state', 'tail', 'nan', 'trailing-comma'])
def test_invalid_legacy_proof_fails_closed(tmp_path, damage):
    module = _migration()
    path = _legacy(tmp_path, 3)
    raw = path.read_text()
    if damage == 'truncated':
        raw = raw[:-7]
    elif damage == 'schema':
        raw = raw.replace('alert_topic_anchors.v1', 'unknown-schema')
    elif damage == 'state':
        raw = raw.replace('"state": "sent"', '"state": "lost"')
    elif damage == 'nan':
        raw = raw.replace('"updated_at": "2026-11-04T18:00:00.000000+00:00"', '"updated_at": NaN')
    elif damage == 'trailing-comma':
        raw = raw[:-1] + ',}'
    else:
        raw += ' unreadable-tail'
    path.write_text(raw)
    with pytest.raises(ValueError):
        module.migrate_legacy(tmp_path)
    with pytest.raises(ValueError):
        anchors._reserve_for_delivery(tmp_path, _key(99999), now='not-absent')


# LLM: Cross-process reopen reads committed state, not an in-memory cache or a Python singleton.
# 函数用途: 真实新进程重启后核验根、线程和原始派发证明。
def test_new_process_reopens_exact_committed_proof(tmp_path):
    anchors._reserve_for_delivery(tmp_path, _key(5), now='first', dispatch_key='dispatch-original')
    expected = anchors.mark_sent(tmp_path, _key(5), root_message_id='om_confirmed', thread_id='omt_confirmed', now='sent')
    checkout = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join((str(checkout), str(checkout / 'agent_py_agent'),
        env.get('PYTHONPATH', '')))
    script = ('import json,sys; from pathlib import Path; '
              'from agent_py_agent.agent.scheduler import alert_topic_anchors as a; '
              'assert Path(a.__file__).resolve() == Path(sys.argv[3]).resolve(); '
              'print(json.dumps(a._reserve_for_delivery(sys.argv[1],sys.argv[2],now="restart",dispatch_key="retry")))')
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path), _key(5), anchors.__file__],
        check=True, capture_output=True, text=True, timeout=15, cwd=checkout, env=env)
    assert json.loads(result.stdout) == [expected, False, 'dispatch-original']


# LLM: Real symlinks stay real; if this host denies hardlink creation with EPERM, inject nlink=2
# at the real reader's stat boundary instead, still asserting refusal and unchanged external bytes.
# 函数用途: 核验分区父目录和记录叶符号链接均拒绝，外部原件不变。
@pytest.mark.parametrize('kind', ['directory', 'record', 'hardlink'])
def test_partition_paths_do_not_follow_links(tmp_path, kind, monkeypatch):
    owner, outside = tmp_path / 'owner', tmp_path / 'outside'
    outside.mkdir()
    anchors.reserve(owner, _key(1), now='init')
    key = _key(0x123456)
    parts = anchors._record_parts(key)
    target = owner.joinpath(*parts)
    original = outside / 'proof.json'
    original.write_text('{"outside":"untouched"}')
    injected = []
    if kind == 'directory':
        target.parent.parent.mkdir(parents=True, exist_ok=True)
        target.parent.symlink_to(outside, target_is_directory=True)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        if kind == 'record':
            target.symlink_to(original)
        else:
            try:
                os.link(original, target)
            except PermissionError as exc:
                if exc.errno != errno.EPERM:
                    raise
                target.write_bytes(original.read_bytes())
                fstat = os.fstat
                identity = (target.stat().st_dev, target.stat().st_ino)

                # LLM: Keep real path/type/IO behavior; only model the unavailable hardlink metadata.
                # 函数用途: 将准确叶文件的链接数设为 2，实测正式读取器会拒绝它。
                def linked_stat(descriptor):
                    info = fstat(descriptor)
                    if (info.st_dev, info.st_ino) == identity:
                        values = list(info)
                        values[3] = 2
                        injected.append(True)
                        return os.stat_result(values)
                    return info

                monkeypatch.setattr(os, 'fstat', linked_stat)
    with pytest.raises(OSError):
        anchors.reserve(owner, key, now='reject')
    assert original.read_text() == '{"outside":"untouched"}'
    if kind == 'hardlink' and target.stat().st_nlink == 1:
        assert injected, 'hardlink guard was not actually exercised'


# LLM: Exercise byte-exact cursor recovery with UTF-8 split inside multibyte characters.
# 函数用途: 核验小块流式读取、字段乱序和没有原派发键的历史兼容。
def test_utf8_chunk_boundaries_and_legacy_without_dispatch_map(tmp_path, monkeypatch):
    module = _migration()
    row = {'root_message_id': 'om_中文𓆏', 'thread_id': 'omt_话题', 'state': 'sent', 'updated_at': '旧时间'}
    path = tmp_path / 'data/alert_topic_anchors.json'
    path.parent.mkdir()
    path.write_text(json.dumps({'anchors': {_key(8): row},
        'schema_version': 'alert_topic_anchors.v1'}, ensure_ascii=False))
    monkeypatch.setattr(module._MigrationLimits, '_CHUNK_BYTES', 7)
    reports = []
    for _ in range(30):
        report = module.migrate_legacy(tmp_path, max_records=1, max_bytes=512)
        assert report['bytes_read'] <= 512
        reports.append(report)
        if report['complete']:
            break
    assert reports[-1]['complete']
    assert anchors._reserve_for_delivery(tmp_path, _key(8), now='reopen', dispatch_key='retry') == (row, False, '')


# LLM: Orphan dispatch proof cannot become a new root, regardless of JSON map ordering.
# 函数用途: 核验只有派发关联却缺少锚点的旧数据不会被发布成可重发记录。
@pytest.mark.parametrize('keys_first', [False, True])
def test_orphan_legacy_dispatch_fails_closed(tmp_path, keys_first):
    _migration()
    path = _legacy(tmp_path, 2, keys_first=keys_first)
    data = json.loads(path.read_text())
    data['root_delivery_keys'][_key(999)] = 'orphan-original'
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='no anchor'):
        _finish(tmp_path)
    with pytest.raises(ValueError, match='no anchor'):
        anchors._reserve_for_delivery(tmp_path, _key(999), now='must-not-resend')


# LLM: Real alert_delivery is unchanged; migrated original-dispatch replay cannot hit transport.
# 函数用途: 从原告警投递入口核验迁移后重放不建根、后续更新仍回复旧根。
def test_existing_delivery_caller_reuses_migrated_root_and_original_dispatch(tmp_path):
    from dataclasses import replace
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.background_delivery import BackgroundDeliveryDependencies
    from agent_py_agent.agent.conversation.channels import DeliveryContext, ReplyEnvelope
    from agent_py_agent.agent.scheduler import alert_delivery

    key = anchors.make_alert_key('fixture', 'owner-a', 'oc_fixture', 'evt')
    path = tmp_path / 'data/alert_topic_anchors.json'
    path.parent.mkdir()
    path.write_text(json.dumps({'schema_version': 'alert_topic_anchors.v1', 'anchors': {key: _row(0)},
        'root_delivery_keys': {key: 'original-task'}}))
    _finish(tmp_path)
    calls = []

    # LLM: Simulate only provider transport; routing, anchor persistence and dispatch decisions are real.
    # 函数用途: 记录原投递器实际选择的根或回复入口。
    def deliver(context, envelope):
        calls.append(context)
        return SimpleNamespace(delivery_status='sent')

    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=tmp_path, owner_id='owner-a', owner_kind='group'),
        config=SimpleNamespace(im_topic_aggregation_enabled=True))
    deps = BackgroundDeliveryDependencies(agent, None, SimpleNamespace(deliver=deliver), lambda request: 'done')
    request = SimpleNamespace(wake_signal={'source': 'fixture', 'event_id': 'evt'}, task_id='original-task')
    wrapped = alert_delivery._topic_delivery(deps, request)
    context = DeliveryContext('feishu', 'oc_fixture', idempotency_key='original-task')
    assert wrapped.channels.deliver(context, ReplyEnvelope('replay')).delivery_status == 'sent'
    assert calls == []
    assert wrapped.channels.deliver(replace(context, idempotency_key='next-update'), ReplyEnvelope('update')).delivery_status == 'sent'
    assert len(calls) == 1 and calls[0].mode == 'reply' and calls[0].reply_to == _row(0)['root_message_id']


# LLM: Capability simulation checks the retained portable policy; it is not Windows E2E evidence.
# 函数用途: 核验无 dir_fd 时新 Owner 初始化和既有账本流式迁移仍可用。
@pytest.mark.parametrize('legacy', [False, True])
def test_portable_initialization_and_migration_keep_existing_policy(tmp_path, monkeypatch, legacy):
    from agent_py_agent.agent.common import nofollow_fs

    monkeypatch.setattr(nofollow_fs, '_supports_dir_fd', lambda: False)
    monkeypatch.setattr(anchors, '_supports_dir_fd', lambda: False)
    if legacy:
        _legacy(tmp_path, 3, keys_first=True)
        _finish(tmp_path, max_records=1)
        row, first, dispatch = anchors._reserve_for_delivery(tmp_path, _key(1), now='retry')
        assert row == _row(1) and first is False and dispatch == 'original-dispatch-1'
    else:
        row, first, dispatch = anchors._reserve_for_delivery(tmp_path, _key(1), now='first', dispatch_key='original')
        assert row['state'] == 'pending' and first is True and dispatch == 'original'
    sent = anchors.mark_sent(tmp_path, _key(10), root_message_id='om_portable', thread_id='omt_portable', now='sent')
    assert anchors.mark_unknown(tmp_path, _key(10), now='late') == sent


def test_portable_source_uses_fd_clock_and_binary_bytes(tmp_path, monkeypatch):
    from types import SimpleNamespace
    module = _migration()
    path = _legacy(tmp_path, 3)
    raw = path.read_bytes().replace(b', ', b',\r\n') + b'\x1atail'
    path.write_bytes(raw)
    real_path = module._portable_path
    class CreationClockPath:
        def __fspath__(self):
            return os.fspath(path)
        def lstat(self):
            info = path.lstat()
            values = {key: getattr(info, key) for key in
                      ('st_dev', 'st_ino', 'st_mode', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns')}
            values['st_ctime_ns'] = 1
            return SimpleNamespace(**values)
    monkeypatch.setattr(anchors, '_supports_dir_fd', lambda: False)
    monkeypatch.setattr(module, '_portable_path', lambda root, parts, create=False:
                        CreationClockPath() if parts == anchors._ROOT_PARTS else real_path(root, parts, create=create))
    fd = module._open_source(tmp_path)
    try:
        assert os.read(fd, len(raw) + 1) == raw
        assert module._seal(fd) == [getattr(os.fstat(fd), key) for key in
                                   ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')]
    finally:
        os.close(fd)


def test_old_five_field_checkpoint_resumes_byte_exact_portable_source(tmp_path, monkeypatch):
    module = _migration()
    monkeypatch.setattr(anchors, '_supports_dir_fd', lambda: False)
    path = _legacy(tmp_path, 5, keys_first=True)
    path.write_bytes(path.read_bytes().replace(b', ', b',\r\n'))
    first = module.migrate_legacy(tmp_path, max_records=1)
    assert not first['complete'] and first['cursor'] > 0
    state = module._load_progress(tmp_path)
    original_source = list(state['source'])
    # 原五字段断点没有native补充字段，来源数值始终由实际旧FD取得而非伪造。
    state.pop('source_change_ns')
    module._save_progress(tmp_path, state)
    _finish(tmp_path, max_records=1)
    assert module._load_progress(tmp_path)['source'] == original_source
    for index in range(5):
        row, first, dispatch = anchors._reserve_for_delivery(tmp_path, _key(index), now='retry')
        assert row == _row(index) and first is False and dispatch == f'original-dispatch-{index}'


@pytest.mark.parametrize('missing', [False, True])
@pytest.mark.parametrize('old_checkpoint', [False, True])
def test_native_generation_unknown_or_changed_never_advances_checkpoint(tmp_path, monkeypatch, missing, old_checkpoint):
    module = _migration()
    _legacy(tmp_path, 5)
    assert not module.migrate_legacy(tmp_path, max_records=1)['complete']
    before = module._load_progress(tmp_path)
    expected = before['source_change_ns']
    if old_checkpoint:
        before.pop('source_change_ns')
        module._save_progress(tmp_path, before)
        expected = before['source'][4]
        class WindowsObservationOS:
            name = 'nt'
            def __getattr__(self, name):
                return getattr(os, name)
        monkeypatch.setattr(module, 'os', WindowsObservationOS())
    monkeypatch.setattr(module, 'file_descriptor_change_ns', lambda fd, info: None if missing else expected + 1)
    with pytest.raises((ValueError, module.NoFollowPathError)):
        module.migrate_legacy(tmp_path, max_records=1)
    assert module._load_progress(tmp_path) == before
    assert anchors._read_ready(tmp_path) is None


def test_cutover_closes_source_readers_before_replace_and_recovers_published_fence(tmp_path, monkeypatch):
    module = _migration()
    _legacy(tmp_path, 3)
    monkeypatch.setattr(anchors, '_supports_dir_fd', lambda: False)
    opened = set()
    real_open, real_close = module._open_source, os.close
    real_write, real_ready = anchors._write, module._publish_ready
    fences = []
    def observe_open(root):
        fd = real_open(root)
        opened.add(fd)
        return fd
    def observe_close(fd):
        real_close(fd)
        opened.discard(fd)
    def require_closed(root, data):
        if not data['alert_key']:
            assert not opened, 'Windows cannot replace a source with live migration readers'
            fences.append(data['migration_id'])
        return real_write(root, data)
    def crash_before_ready(root, state):
        raise OSError('after durable fence')
    monkeypatch.setattr(module, '_open_source', observe_open)
    monkeypatch.setattr(module.os, 'close', observe_close)
    monkeypatch.setattr(anchors, '_write', require_closed)
    monkeypatch.setattr(module, '_publish_ready', crash_before_ready)
    with pytest.raises(OSError, match='after durable fence'):
        _finish(tmp_path, max_records=1)
    assert len(fences) == 1 and not opened and anchors._read_ready(tmp_path) is None
    state = module._load_progress(tmp_path)
    assert state['phase'] == 'publish'
    assert anchors._read(tmp_path) == anchors._record('', state)
    monkeypatch.setattr(module, '_publish_ready', real_ready)
    assert module.migrate_legacy(tmp_path, max_records=1)['complete']
    assert len(fences) == 1 and not opened
    for index in range(3):
        row, first, dispatch = anchors._reserve_for_delivery(tmp_path, _key(index), now='retry')
        assert row == _row(index) and not first and dispatch == f'original-dispatch-{index}'


def test_cutover_rechecks_changed_source_after_closing_initial_reader(tmp_path, monkeypatch):
    module = _migration()
    path = _legacy(tmp_path, 3)
    real_finish = module._finish_publish
    monkeypatch.setattr(module, '_finish_publish', lambda root, state: None)
    for _ in range(20):
        module.migrate_legacy(tmp_path, max_records=1)
        if module._load_progress(tmp_path)['phase'] == 'publish':
            break
    else:
        pytest.fail('source never reached cutover checkpoint')
    before = module._load_progress(tmp_path)
    monkeypatch.setattr(module, '_finish_publish', real_finish)
    real_open, real_close = module._open_source, os.close
    tracked, changed = set(), []
    def observe_open(root):
        fd = real_open(root)
        tracked.add(fd)
        return fd
    def change_after_close(fd):
        owned = fd in tracked
        real_close(fd)
        tracked.discard(fd)
        if owned and not changed:
            changed.append(True)
            # 合法同大小新来源不能被旧断点覆盖成fence；无需重放任一导入副作用。
            path.write_bytes(path.read_bytes().replace(b'om_rr', b'om_ss'))
    monkeypatch.setattr(module, '_open_source', observe_open)
    monkeypatch.setattr(module.os, 'close', change_after_close)
    with pytest.raises(ValueError, match='source changed'):
        module.migrate_legacy(tmp_path, max_records=1)
    assert changed and not tracked
    assert module._load_progress(tmp_path) == before
    assert anchors._read_ready(tmp_path) is None
    assert json.loads(path.read_bytes())['schema_version'] == 'alert_topic_anchors.v1'
