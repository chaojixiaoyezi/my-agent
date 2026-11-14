# LLM: 使用真实草稿、演进请求/候选、自动与用户提案writer，临时Owner内推进时间并注入并发。
# 模块用途: 验证资产辅助TTL通道不碰正式版本、blob、待发表证据和用户提案。
import gc
import json
import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from agent_py_agent.agent.assets import revision_history as history
from agent_py_agent.agent.assets.mutation_ledger import freeze_asset_mutations
from agent_py_agent.agent.assets.turn_extractor import (
    commit_extracted_assets,
    propose_assets_from_turns,
    save_evolution_draft,
)
from agent_py_agent.agent.capability.asset_evolution import AssetEvolutionService
from agent_py_agent.agent.capability.asset_evolution_routes import (
    save_evolution_proposal,
    write_evolution_json,
)
from agent_py_agent.agent.common.directory_lock import locked_private_directory
from agent_py_agent.agent.common.json_io import locked_json_path
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home, home_paths
from agent_py_agent.agent.user_space.owner_quota import owner_logical_usage_bytes
from agent_py_agent.agent.user_space.storage_lanes import (
    StorageLaneBudget,
    StorageLaneRequest,
    validate_lane_receipt,
)
from agent_py_agent.tests.test_issues_w14_lane_wiring_e2e import _fd_count


# LLM: 真实初始化只落临时目录，策略和pin来源与生产格式一致。
# 函数用途: 返回测试Owner与本轮观察时刻。
@pytest.fixture
def setup(tmp_path):
    return ensure_my_agent_home(tmp_path / 'home'), datetime.now(timezone.utc)


# LLM: 参数为冻结调度DTO；每次调用重新建立预算，不保存模型侧隐形状态。
# 函数用途: 创建辅助通道请求。
def request(setup, *, cursor=None, items=128, now=None, commits=None, hold=False):
    home, instant = setup
    commit = None
    if commits is not None:
        # LLM: 只记录并原样执行领域变更，不伪造成功回执。
        # 函数用途: 观察run_write是否实际被调用。
        def commit(operation):
            commits.append(True)
            return operation()
    return StorageLaneRequest('asset_auxiliary', home, now or instant.timestamp(), cursor or {},
        StorageLaneBudget(items, None, None), hold, commits is not None, None, commit)


# LLM: 游标每片JSON往返，阶段和候选目录必须真实推进至完成。
# 函数用途: 模拟重启直至一轮辅助维护结束。
def drain(setup, *, cursor=None, items=128, now=None):
    rows = []
    for _ in range(100):
        result = history.maintain_asset_auxiliary_lane(request(setup, cursor=cursor, items=items, now=now))
        validate_lane_receipt(result)
        rows.append(result)
        if result['complete']:
            return rows
        cursor = json.loads(json.dumps(result['cursor']))
    pytest.fail('auxiliary lane did not complete: ' + repr(rows[-1]))


# LLM: 请求通过真实freeze落盘；已处理标记由真实service生成，然后仅回填测试时间。
# 函数用途: 生成有确定业务身份的过期或未处理请求。
def evolution_request(setup, name, *, processed=True, age=15):
    home, now = setup
    value = freeze_asset_mutations(home.owner_home_dir, {'asset_mutations': {'evidence_tags': ['query_failed']}}, run_id=name)
    if processed:
        assert AssetEvolutionService(home.owner_home_dir).process(name)['status'] == 'query_failed'
        value['result'] = {'status': 'query_failed', 'notices': [], 'routes': []}
    value['created_at'] = (now - timedelta(days=age)).isoformat()
    parts = ('data', 'asset_evolution', 'requests', name + '.json')
    write_evolution_json(home.owner_home_dir, parts, value)
    return home.owner_home_dir.joinpath(*parts)


# LLM: 草稿保存与blob发布均用原writer，测试不手建不存在的schema。
# 函数用途: 生成可到期的history草稿。
def draft(setup, name, *, age=15):
    home, now = setup
    value = history.save_draft(home.owner_home_dir, 'script', name, payload={'kind': 'script', 'id': name}, code=b'def main(inputs): return {}\n')
    value.update(created_at=(now - timedelta(days=age)).isoformat(), updated_at=(now - timedelta(days=age)).isoformat())
    parts = ('data', 'asset_evolution', 'drafts', 'script', name + '.json')
    write_evolution_json(home.owner_home_dir, parts, value)
    return home.owner_home_dir.joinpath(*parts)


# LLM: 提案正文通过既有校验器，不启动脚本或模型。
# 函数用途: 生成自动演进草稿。
def automatic(setup, *, family='a' * 64, age=15):
    home, now = setup
    script = {'script_id': 'automatic-' + family[:4], 'title': '自动草稿', 'language': 'python',
        'entry_source': 'def main(inputs):\n return {}\n', 'input_schema': {'properties': {}}, 'output_schema': {'properties': {}}}
    value = save_evolution_draft(home.owner_home_dir, family, script, run_id='auto-run', thread_id='auto-thread',
        created_at=(now - timedelta(days=age)).isoformat())
    return home.owner_home_dir / 'data' / 'asset_extractions' / (value['proposal_id'] + '.json')


# LLM: 候选实际writer保存code_sha256 blob；TTL只退休JSON。
# 函数用途: 创建到期候选并回填测试时间。
def candidate(setup, name='candidate', *, age=31):
    home, now = setup
    value = save_evolution_proposal(home.owner_home_dir, name, 'def main(inputs):\n return {}\n')
    value['created_at'] = (now - timedelta(days=age)).isoformat()
    parts = ('data', 'asset_evolution', 'candidates', 'script', name, value['candidate_id'] + '.json')
    write_evolution_json(home.owner_home_dir, parts, value)
    return home.owner_home_dir.joinpath(*parts)


# LLM: 用户草稿来自真实会话，不携带自动族/到期字段；提交使用原确认入口。
# 函数用途: 保存一个用户草稿或已正式提交的提案。
def user_proposal(setup, name, *, committed=False):
    home, _ = setup
    store = ConversationStore(home.owner_home_dir / 'conversations')
    thread = store.threads.get_or_create({'channel': 'tui', 'channel_conversation_id': name})
    message = store.messages.append({'thread_id': thread.thread_id, 'role': 'user', 'content': name})
    value = propose_assets_from_turns(store, owner_home=home.owner_home_dir,
        turn_refs=[{'thread_id': thread.thread_id, 'message_id': message.message_id}], target_types=('script',))
    if committed:
        assert commit_extracted_assets(home.owner_home_dir, proposal_id=value['proposal_id'])['ok']
    return home.owner_home_dir / 'data' / 'asset_extractions' / (value['proposal_id'] + '.json')


# LLM: 线程持有与生产相同的请求锁或记录集合锁，事件保证测试观察点准确。
# 函数用途: 构造可重复的原领域锁竞争。
@contextmanager
def held_lock(lock):
    ready, release = threading.Event(), threading.Event()
    # LLM: 上下文退出才释放锁，不用sleep猜测锁已获得。
    # 函数用途: 持有真实领域锁。
    def worker():
        with lock:
            ready.set()
            assert release.wait(10)
    thread = threading.Thread(target=worker)
    thread.start()
    assert ready.wait(5)
    try:
        yield
    finally:
        release.set()
        thread.join(5)
        assert not thread.is_alive()


# LLM: 混合真实writer样本验证所有phase；计量比较精确逻辑用量，blob和正式版本逐字保留。
# 函数用途: 覆盖TTL矩阵、保护对象、释放计量和阶段推进。
def test_real_writers_ttl_matrix_and_exact_usage(setup):
    home, _ = setup
    root = home.owner_home_dir
    expired = [evolution_request(setup, 'processed'), draft(setup, 'old-draft', age=15), candidate(setup), automatic(setup)]
    retained = [evolution_request(setup, 'pending', processed=False), draft(setup, 'new-draft', age=0),
        draft(setup, 'near-ttl-draft', age=13), automatic(setup, family='b' * 64, age=0),
        user_proposal(setup, 'user-draft'), user_proposal(setup, 'committed', committed=True)]
    for number in range(1, 102):
        history.record_version(
            root,
            'script',
            'published',
            payload={'kind': 'script', 'id': 'published'},
            code=f'pass  # {number}\n'.encode(),
        )
    evolution = root / 'data' / 'asset_evolution'
    protected = {path: path.read_bytes() for folder in ('versions', 'blobs') for path in (evolution / folder).rglob('*') if path.is_file()}
    before = owner_logical_usage_bytes(root, exact=True)
    expected_freed = sum(path.stat().st_size for path in expired)
    rows = drain(setup)
    after = owner_logical_usage_bytes(root, exact=True)
    assert sum(row['deleted_items'] for row in rows) == 4
    assert sum(row['freed_bytes'] for row in rows) == expected_freed == before - after
    assert all(not path.exists() for path in expired)
    assert all(path.exists() for path in retained)
    assert all(path.read_bytes() == value for path, value in protected.items())
    assert AssetEvolutionService(root).process('processed')['status'] == 'absent'
    published_rows = history.list_versions(root, 'script', 'published', limit=200)
    assert len(published_rows) == 101
    assert history.get_version(root, 'script', 'published', 1)['legacy'] is False
    for published_row in published_rows:
        record = history.get_version(root, 'script', 'published', published_row['version'])
        assert history.blob_exists(root, record['code_sha256'])
        assert history.blob_exists(root, record['payload_blob'])


# LLM: 待发表槽不可因旧草稿到期被清掉，读取失败同样只能保留。
# 函数用途: 验证publication_pending的草稿保护。
def test_publication_pending_protects_draft(setup):
    path = draft(setup, 'pending-publish')
    history.retain_publication(setup[0].owner_home_dir, 'script', 'pending-publish', {'payload': {'id': 'pending-publish'}})
    rows = drain(setup)
    assert path.exists() and sum(row['skipped'].get('publication_pending', 0) for row in rows) == 1
    assert sum(row['deleted_items'] for row in rows) == 0


# LLM: 请求锁忙时不越过游标，处理中的原请求仍由原service独占。
# 函数用途: 验证请求竞争及释放后的准确重试。
def test_request_lock_busy_and_new_generation(setup):
    path = evolution_request(setup, 'busy')
    with held_lock(locked_json_path(path)):
        result = history.maintain_asset_auxiliary_lane(request(setup))
        assert result['skipped']['lock_busy'] == 1 and not result['complete']
        assert result['cursor']['after'] == '' and path.exists()
    value = json.loads(path.read_text())
    value['created_at'] = setup[1].isoformat()
    write_evolution_json(setup[0].owner_home_dir, ('data', 'asset_evolution', 'requests', 'busy.json'), value)
    rows = drain(setup, cursor=result['cursor'])
    assert path.exists() and sum(row['deleted_items'] for row in rows) == 0


# LLM: 自动草稿与用户确认共用原.records.lock，竞争后保留精确提案续点。
# 函数用途: 验证集合锁有界等待与重试。
def test_extraction_records_lock_busy(setup):
    home, _ = setup
    path = automatic(setup)
    cursor = {'phase': 'extractions', 'kind': 'script', 'after': ''}
    lock = locked_private_directory(home.owner_home_dir, relative_parts=('data', 'asset_extractions'), lock_name='.records.lock')
    with held_lock(lock):
        result = history.maintain_asset_auxiliary_lane(request(setup, cursor=cursor))
        assert result['skipped']['lock_busy'] == 1 and not result['complete'] and path.exists()
    assert sum(row['deleted_items'] for row in drain(setup, cursor=result['cursor'])) == 1
    assert path.parent.joinpath('.records.lock').exists()


# LLM: 小预算覆盖同一候选子目录内多叶，不能因第一叶被保留而永远重读或跨过锁忙叶。
# 函数用途: 验证分片重启、候选目录内续点及不重复释放。
def test_small_budget_restart_progress(setup):
    paths = [evolution_request(setup, 'request-' + str(index)) for index in range(3)]
    paths += [draft(setup, 'draft-' + str(index)) for index in range(2)]
    old = candidate(setup, 'multi')
    value = json.loads(old.read_text())
    second = {**value, 'candidate_id': 'second'}
    write_evolution_json(setup[0].owner_home_dir, ('data', 'asset_evolution', 'candidates', 'script', 'multi', 'second.json'), second)
    paths += [old, old.with_name('second.json'), automatic(setup)]
    rows = drain(setup, items=1)
    assert all(row['items'] <= 1 for row in rows)
    assert len(rows) > len(paths) and sum(row['deleted_items'] for row in rows) == len(paths)
    assert all(not path.exists() for path in paths)
    next_pass = history.maintain_asset_auxiliary_lane(request(setup, cursor=rows[-1]['cursor']))
    assert next_pass['items'] == 0 and next_pass['complete']


# LLM: 不先初始化冷Owner；无data目录不能通过写门或锁创建存储。
# 函数用途: 验证冷路径、legal_hold和pass gate均零写。
def test_cold_hold_and_pass_gate_zero_write(tmp_path, setup):
    home = home_paths(tmp_path / 'cold')
    calls = []
    result = history.maintain_asset_auxiliary_lane(request((home, setup[1]), commits=calls))
    assert result['complete'] and calls == [] and not home.owner_home_dir.exists()
    path = evolution_request(setup, 'held')
    result = history.maintain_asset_auxiliary_lane(request(setup, hold=True, commits=calls))
    assert result['deferred_reason'] == 'legal_hold' and path.exists() and calls == []
    result = history.maintain_asset_auxiliary_lane(request(setup, cursor={'next_pass_at': setup[1].timestamp() + 1}, commits=calls))
    assert result['items'] == 0 and calls == []


# LLM: 一个异常候选目录项只能被保守跳过，不应卡住其它候选和提炼阶段。
# 函数用途: 复现并约束异常首项导致cursor不推进的实现。
def test_candidate_non_directory_does_not_starve_following_phases(setup):
    path = candidate(setup, 'zzz-valid')
    (path.parent.parent / 'aaa').write_text('not a directory')
    extraction = automatic(setup)
    rows = drain(setup, cursor={'phase': 'candidates', 'kind': 'script', 'after': ''})
    assert sum(row['deleted_items'] for row in rows) == 2
    assert not path.exists() and not extraction.exists()
    assert sum(row['skipped'].get('unknown_schema', 0) for row in rows) >= 1


# LLM: 小于单个JSON的预算不能先整读再报超限；坏JSON的实际读取也不能被报告为零。
# 函数用途: 覆盖辅助记录的实际字节预算和解析失败计量。
@pytest.mark.parametrize('invalid', [False, True])
def test_auxiliary_read_budget_and_invalid_json_accounting(setup, invalid):
    path = evolution_request(setup, 'bounded')
    if invalid:
        path.write_bytes(b'{' * 31)
    dto = replace(request(setup), budget=StorageLaneBudget(128, 64, None))
    result = history.maintain_asset_auxiliary_lane(dto)
    assert path.exists() and result['bytes'] <= 64
    if invalid:
        assert result['bytes'] == 31 and result['skipped']['unknown_schema'] == 1
    assert result['deleted_items'] == 0


# LLM: 预算假钟只在真实pins处推进，不替换引用视图；每个有到期候选的片必须前进，空集合仍走原阶段切换。
# 函数用途: 验证慢扫描下辅助候选最终清完、完整轮结束、字节计量及FD观察都准确。
def test_slow_pin_snapshot_still_retires_due_auxiliary_files(setup, monkeypatch, record_property):
    from types import SimpleNamespace

    from agent_py_agent.agent.memory_store import retention_scan
    from agent_py_agent.agent.user_space import storage_lanes
    from agent_py_agent.agent.user_space.storage_lanes import STORAGE_LANE_SLICE_MS

    paths = [candidate(setup, f'slow-{index}', age=31) for index in range(3)]
    expected_bytes = sum(path.stat().st_size for path in paths)
    blobs = setup[0].owner_home_dir / 'data/asset_evolution/blobs'
    preserved = {path: path.read_bytes() for path in blobs.rglob('*') if path.is_file()}
    original = retention_scan.current_retention_pins
    pin_calls, commits = [], []
    clock = [0.0]

    def slow_pins(*args, **kwargs):
        pin_calls.append(True)
        clock[0] += 2 * STORAGE_LANE_SLICE_MS / 1000
        return original(*args, **kwargs)

    monkeypatch.setattr(retention_scan, 'current_retention_pins', slow_pins)
    # 只模拟pin超时，避免宿主目录/JSON读取抢先耗尽本测试的25ms预算。
    monkeypatch.setattr(storage_lanes, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    gc.collect()
    before = _fd_count(root=setup[0].root)
    cursor, rows = {'phase': 'candidates', 'kind': 'script', 'after': ''}, []
    for _ in range(8):
        pending = any(path.exists() for path in paths)
        pins_before, commits_before = len(pin_calls), len(commits)
        row = history.maintain_asset_auxiliary_lane(request(setup, cursor=cursor, commits=commits))
        validate_lane_receipt(row)
        rows.append(row)
        assert len(pin_calls) - pins_before <= 1
        if pending:
            assert row['deleted_items'] == 1
            assert len(pin_calls) - pins_before == len(commits) - commits_before == 1
        else:
            # candidates穷尽后仍须经过原extractions空阶段；无工作不取得写门，不伪称做了删除。
            assert row['deleted_items'] == 0 and len(commits) == commits_before
        if row['complete']:
            break
        assert row['cursor'] != cursor
        cursor = json.loads(json.dumps(row['cursor']))
    assert rows[-1]['complete'] is True and all(not path.exists() for path in paths)
    assert sum(row['deleted_items'] for row in rows) == len(paths)
    assert sum(row['freed_bytes'] for row in rows) == expected_bytes
    assert all(path.read_bytes() == body for path, body in preserved.items())
    assert history._AUXILIARY_READ_METER.get() is None
    gc.collect()
    delta = _fd_count(root=setup[0].root) - before
    record_property('fd_delta', delta)
    record_property('pin_calls', len(pin_calls))
    assert delta == 0


# LLM: 使用真实领域锁或固定父FD的unlink错误；合法空片必须续跑至观察点，全部原FD/关闭式恢复断言保留。
# 函数用途: 在普通和扫描耗尽前两片的情况下验证锁忙/删除失败均释放FD并可恢复。
@pytest.mark.parametrize('empty_slices', [0, 2])
@pytest.mark.parametrize('condition', ['lock_busy', 'unlink_failure'])
def test_auxiliary_failure_paths_close_descriptors(setup, monkeypatch, record_property, condition, empty_slices):
    import os
    from contextlib import nullcontext
    from types import SimpleNamespace

    from agent_py_agent.agent.user_space import storage_lanes

    path = evolution_request(setup, 'fd-check')
    original, failed = os.unlink, []
    clock, delayed = [0.0], []
    auxiliary_window = history._auxiliary_window

    # LLM: 只给预算时钟注入有限扫描延迟，真实目录、记录、锁、pin与unlink保持不变。
    # 函数用途: 制造合法零候选片，确保错误路径测试不依赖25毫秒内到达unlink。
    def directory_scan(*args, **kwargs):
        result = auxiliary_window(*args, **kwargs)
        if len(delayed) < empty_slices:
            delayed.append(True)
            clock[0] += storage_lanes.STORAGE_LANE_SLICE_MS / 1000 + 1
        return result

    if empty_slices:
        monkeypatch.setattr(storage_lanes, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
        monkeypatch.setattr(history, '_auxiliary_window', directory_scan)

    # LLM: 能力检查保留，仅在实际目标父FD的unlink处报错，不吞其它文件操作。
    # 函数用途: 证明到达了真实删除观察点，而非把预算延期当删除失败。
    def fail_unlink(name, *args, **kwargs):
        if name == path.name and kwargs.get('dir_fd') is not None:
            failed.append(True)
            raise OSError('simulated unlink failure')
        return original(name, *args, **kwargs)

    if condition == 'unlink_failure':
        monkeypatch.setattr(os, 'unlink', fail_unlink)
        # 能力不变，错误必须发生在实际unlink观察点而非平台能力检查阶段。
        monkeypatch.setattr(os, 'supports_dir_fd', os.supports_dir_fd | {fail_unlink})
    lock = held_lock(locked_json_path(path)) if condition == 'lock_busy' else nullcontext()
    with lock:
        gc.collect()
        before = _fd_count(root=setup[0].root)
        cursor, rows = {}, []
        for _ in range(100):
            row = history.maintain_asset_auxiliary_lane(request(setup, cursor=cursor))
            validate_lane_receipt(row)
            rows.append(row)
            gc.collect()
            delta = _fd_count(root=setup[0].root) - before
            assert delta == 0 and path.exists() and row['deleted_items'] == 0
            assert not row['complete'] and row['cursor']['after'] == ''
            assert history._AUXILIARY_READ_METER.get() is None
            if row['skipped'].get('lock_busy') or failed:
                break
            # 没到故障点只能是扫描预算耗尽；不能静默接受其它跳过原因或错误。
            assert row['items'] == 0 and not row['errors'] and not row['skipped']
            cursor = json.loads(json.dumps(row['cursor']))
        else:
            pytest.fail(f'auxiliary failure observation never reached: {rows!r}')
        record_property('fd_delta', delta)
        assert [r['items'] for r in rows[:empty_slices]] == [0] * empty_slices
        if condition == 'lock_busy':
            assert row['skipped']['lock_busy'] == 1
        else:
            assert failed and row['errors']
    monkeypatch.setattr(os, 'unlink', original)
    assert sum(item['deleted_items'] for item in drain(setup, cursor=row['cursor'])) == 1
    assert not path.exists()


# LLM: 使用真实域外关闭与域内持有验证计数边界，不允许 scoped 观察把真正泄漏也忽略。
# 函数用途: 证明无关 FD 清理不扰动基线，而夹具内多留一个 FD 仍可检出。
def test_auxiliary_fd_observation_ignores_foreign_cleanup_but_detects_growth(setup, tmp_path):
    root = setup[0].root
    with (tmp_path / 'outside.txt').open('w') as foreign:
        before = _fd_count(root=root)
        foreign.close()
        assert _fd_count(root=root) == before
        with (root / 'held.txt').open('w'):
            assert _fd_count(root=root) == before + 1
    assert _fd_count(root=root) == before


# LLM: 原unlink先删后同步；目录同步失败时仍要把已确认删除量记入回执，并报告同步错误。
# 函数用途: 验证辅助请求删除后发生fsync错误的实际计量。
def test_auxiliary_unlink_fsync_failure_counts_deleted_bytes(setup, monkeypatch):
    import os
    import stat

    path = evolution_request(setup, 'fsync')
    expected = path.stat().st_size
    original = os.fsync
    # LLM: 仅拒绝目录同步，所有JSON仍来自真实writer。
    # 函数用途: 注入unlink后目录同步错误。
    def fail_directory_fsync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError('directory sync failure')
        return original(descriptor)
    monkeypatch.setattr(os, 'fsync', fail_directory_fsync)
    result = history.maintain_asset_auxiliary_lane(request(setup))
    assert not path.exists() and result['freed_bytes'] == expected and result['deleted_items'] == 1
    assert result['errors']


# LLM: 同时钩住旧bytes入口和新snapshot入口，均在真实读后发布新COMMITTED代次；不允许解析旧值配新身份。
# 函数用途: 验证提炼退休读后替换不能删除新状态。
def test_extraction_read_identity_cannot_delete_replacement(setup, monkeypatch):
    from agent_py_agent.agent.assets import turn_extractor as extractor

    home, now = setup
    path = automatic(setup)
    original_value = json.loads(path.read_text())
    replacement = dict(original_value, status='COMMITTED', revision=original_value['revision'] + 1)
    parts = ('data', 'asset_extractions', path.name)
    original_bytes = extractor.read_bytes_beneath
    original_snapshot = history._auxiliary_snapshot
    changed = []
    # LLM: 用真实原子writer发表替代叶，不修改被读FD或伪造其身份。
    # 函数用途: 在读后查用间隙替换当前记录。
    def publish_new():
        changed.append(True)
        write_evolution_json(home.owner_home_dir, parts, replacement)
    # LLM: 旧实现的实际bytes观察点，供反例适用于旧入口。
    # 函数用途: 读取旧正文后原子发表新状态。
    def bytes_then_replace(*args, **kwargs):
        raw = original_bytes(*args, **kwargs)
        publish_new()
        return raw
    # LLM: 新实现必须将同一FD的值和身份一起交给删除复核。
    # 函数用途: 读取快照后发表另一个真实inode。
    def snapshot_then_replace(*args, **kwargs):
        result = original_snapshot(*args, **kwargs)
        publish_new()
        return result
    monkeypatch.setattr(extractor, 'read_bytes_beneath', bytes_then_replace)
    monkeypatch.setattr(history, '_auxiliary_snapshot', snapshot_then_replace)
    freed = extractor.retire_expired_extraction_draft(home.owner_home_dir, path.stem,
        now=now, expected_revision=original_value['revision'])
    assert changed == [True] and freed == 0 and json.loads(path.read_text()) == replacement


# LLM: 原目录已消失时，旧叶续点不能带到新目录；真实锁忙叶必须留在下一片窗口内。
# 函数用途: 验证跨候选目录切换不会越过未决叶。
def test_missing_candidate_directory_resets_leaf_cursor(setup):
    path = candidate(setup, 'bbb')
    with held_lock(locked_json_path(path)):
        first = history.maintain_asset_auxiliary_lane(request(setup,
            cursor={'phase': 'candidates', 'kind': 'script', 'after': 'aaa/z.json'}))
        assert not first['complete'] and first['cursor']['after'] == 'bbb/'
        assert first['skipped']['lock_busy'] == 1 and path.exists()
    rows = drain(setup, cursor=first['cursor'])
    assert not path.exists() and sum(row['deleted_items'] for row in rows) == 1


# LLM: 扫描与提交复读都是真实FD读取，同一片累计不得超过总预算；不足时下一片只准备一个名字并重核。
# 函数用途: 验证完整读取计量和小预算下的持续推进。
def test_auxiliary_commit_rereads_share_budget_and_resume(setup):
    paths = [evolution_request(setup, f'quota-{index}') for index in range(3)]
    sizes = [path.stat().st_size for path in paths]
    calls = []
    budget = sum(sizes)
    first = history.maintain_asset_auxiliary_lane(replace(request(setup, commits=calls),
        budget=StorageLaneBudget(128, budget, None)))
    assert first['bytes'] == budget and calls == [] and not first['complete']
    assert first['deferred_reason'] == 'budget_exhausted' and all(path.exists() for path in paths)
    total = first['freed_bytes']
    cursor = first['cursor']
    for _ in range(20):
        row = history.maintain_asset_auxiliary_lane(replace(request(setup, cursor=cursor),
            budget=StorageLaneBudget(128, budget, None)))
        assert row['bytes'] <= budget
        total += row['freed_bytes']
        cursor = json.loads(json.dumps(row['cursor']))
        if row['complete']:
            break
    assert row['complete'] and total == sum(sizes) and all(not path.exists() for path in paths)
    assert history._AUXILIARY_READ_METER.get() is None


# LLM: pending存在就是保留依据，包括巨大的损坏JSON；不应为了决定保留而整读未知载荷。
# 函数用途: 验证pending探测不消耗未申报的JSON读取量。
def test_large_invalid_pending_is_protected_without_reading(setup):
    home, _ = setup
    path = draft(setup, 'large-pending')
    pending = home.owner_home_dir / 'data' / 'asset_evolution' / 'pending' / 'script' / path.name
    pending.parent.mkdir(parents=True)
    pending.write_bytes(b'{' * 4096)
    size = path.stat().st_size
    result = history.maintain_asset_auxiliary_lane(replace(request(setup,
        cursor={'phase': 'drafts', 'kind': 'script', 'after': ''}), budget=StorageLaneBudget(128, size * 2, None)))
    assert path.exists() and pending.stat().st_size == 4096
    assert result['bytes'] == size and result['protected'] == 1 and result['deleted_items'] == 0
