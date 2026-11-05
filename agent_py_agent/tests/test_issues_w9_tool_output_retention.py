# LLM: 使用真实externalizer、RuntimeRepository和原锁验证Owner正文退休；只在临时home构造时间及故障。
# 模块用途: 覆盖工具输出到期、引用保护、游标、竞争、故障恢复与expired读取合同。
import json
import os
import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_py_agent.agent.common.json_io import locked_json_path, write_private_text_file_atomic
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.memory_archive import tool_output_retention as retention
from agent_py_agent.agent.memory_archive.artifact.reader import (
    ReadToolOutputArtifactRequest,
    read_tool_output_artifact,
)
from agent_py_agent.agent.memory_archive.tool_output_externalizer import (
    ExternalizeToolOutputRequest,
    externalize_tool_output_record,
)
from agent_py_agent.agent.runtime_db.repository import (
    _QUIESCENT_ATTEMPT_STATUSES,
    RuntimeRepository,
)
from agent_py_agent.agent.runtime_db.schema import runtime_db_path
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home, home_paths
from agent_py_agent.agent.user_space.storage_lanes import (
    StorageLaneBudget,
    StorageLaneRequest,
    validate_lane_receipt,
)


# LLM: 所有测试使用真实Owner布局但不初始化真实用户配置。
# 函数用途: 构造临时home、权威库和固定观察时刻。
@pytest.fixture
def setup(tmp_path):
    home = ensure_my_agent_home(tmp_path / 'home')
    owner = home.owner_home_dir
    owner.mkdir(parents=True, exist_ok=True)
    repo = RuntimeRepository(runtime_db_path(owner))
    return home, repo, datetime.now(timezone.utc)


# LLM: attempt先经真实创建和终结入口落库，非终态特殊值仅用于未知状态故障样本。
# 函数用途: 生成有权威attempt的真实归档并同步回填包装和索引创建时间。
def artifact(setup, name='call', *, blob=False, age=31, status='done', task_root=False):
    home, repo, now = setup
    chain = repo.record_run_creation(owner_id=home.owner_id, run_id='run-' + name, goal='retention fixture')
    old = now - timedelta(days=age)
    if status == 'done':
        repo.settle_agent_attempt(agent_run_id=chain['agent_run_id'], attempt_id=chain['attempt_id'], now=old.timestamp())
    elif status != 'running':
        with repo.transaction() as conn:
            if status == 'missing':
                conn.execute('DELETE FROM agent_attempts WHERE attempt_id = ?', (chain['attempt_id'],))
            else:
                conn.execute('UPDATE agent_attempts SET status = ?, ended_at = 0 WHERE attempt_id = ?', (status, chain['attempt_id']))
    root = home.owner_home_dir
    if task_root:
        root = root / 'runs' / '2026-11-05' / name / 'work'
    record = externalize_tool_output_record(ExternalizeToolOutputRequest(root=root, tool='sample', call_id=name,
        output='中文输出' * (150000 if blob else 100), ok=True, run_id='run-' + name,
        attempt_id=chain['attempt_id'], min_chars=1, preview_chars=10))
    path = Path(record['output_path'])
    value = json.loads(path.read_text())
    value['created_at'] = old.isoformat()
    write_private_text_file_atomic(path, json.dumps(value, ensure_ascii=False))
    index = path.parent / 'index.jsonl'
    rows = [json.loads(line) for line in index.read_text().splitlines()]
    for row in rows:
        if row.get('path') == str(path):
            row['created_at'] = old.isoformat()
    write_private_text_file_atomic(index, ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows))
    os.utime(path, (old.timestamp(), old.timestamp()))
    return path, value


# LLM: 入口是冻结DTO，协调器spy只记录真实提交而不替换领域写者。
# 函数用途: 创建一次新预算的维护请求。
def request(setup, *, cursor=None, items=128, now=None, hold=False, commits=None):
    home, _, instant = setup
    commit = None
    if commits is not None:
        # LLM: 原样执行变更并留下写门调用事实。
        # 函数用途: 观察本片提交次数。
        def commit(operation):
            commits.append(True)
            return operation()
    return StorageLaneRequest('tool_outputs', home, now or instant.timestamp(), cursor or {},
        StorageLaneBudget(items, None, None), hold, commits is not None, None, commit)


# LLM: 每次重建DTO模拟进程重启，游标必须可JSON往返；不放宽失败和完成断言。
# 函数用途: 执行到一轮扫描结束并汇总真实回执。
def drain(setup, *, items=128, cursor=None, now=None):
    rows = []
    for _ in range(50):
        result = retention.maintain_owner_tool_outputs_lane(request(setup, cursor=cursor, items=items, now=now))
        validate_lane_receipt(result)
        rows.append(result)
        if result['complete']:
            return rows
        cursor = json.loads(json.dumps(result['cursor']))
    pytest.fail('tool output lane failed to finish in bounded slices: ' + repr(rows[-1]))


# LLM: 使用另一个真实线程持原锁，不以猴补锁返回值伪造竞争。
# 函数用途: 在测试断言期间保持指定包装锁繁忙。
@contextmanager
def held_lock(path):
    ready, release = threading.Event(), threading.Event()
    # LLM: 仅持锁并等待测试退出，异常后也释放。
    # 函数用途: 提供确定性的锁竞争窗口。
    def worker():
        with locked_json_path(path):
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


# LLM: 两种正文与索引小调用来自同一真实writer；退休只影响正文，索引字节不变。
# 函数用途: 验证退休证据、expired读取和index-only兼容。
def test_real_inline_blob_retire_and_index_unchanged(setup):
    inline, _ = artifact(setup, 'inline')
    blob, _ = artifact(setup, 'blob', blob=True)
    home = setup[0]
    small = externalize_tool_output_record(ExternalizeToolOutputRequest(root=home.owner_home_dir, tool='sample',
        call_id='small', output='ok', ok=True, run_id='run-small', attempt_id='small-attempt', min_chars=100, preview_chars=100))
    index = inline.parent / 'index.jsonl'
    before = index.read_bytes()
    results = drain(setup)
    assert sum(row['deleted_items'] for row in results) == 2
    assert not inline.exists() and not blob.exists() and not blob.with_suffix('.blob').exists()
    assert index.read_bytes() == before
    for path in (inline, blob):
        read = read_tool_output_artifact(ReadToolOutputArtifactRequest(home.owner_home_dir, str(path)))
        assert read['error_code'] == 'artifact_expired'
    read = read_tool_output_artifact(ReadToolOutputArtifactRequest(home.owner_home_dir, small['scoped_call_id']))
    assert read['error_code'] == 'artifact_not_externalized'
    retired = [json.loads(line) for path in (inline.parent / 'retired').glob('*.jsonl') for line in path.read_text().splitlines()]
    assert {row['name'] for row in retired} == {inline.name, blob.name}
    assert all(row['version'] == 1 and row['reason'] == 'ended_attempt_ttl' for row in retired)


# LLM: 运行中、未知和缺失权威行都不能由mtime授权删除。
# 函数用途: 覆盖三类不可退休状态且正文仍可读。
@pytest.mark.parametrize('state,reason', [('running', 'attempt_active'), ('unknown', 'attempt_unknown'), ('missing', 'run_state_unknown')])
def test_unsettled_or_missing_attempt_kept(setup, state, reason):
    path, _ = artifact(setup, status=state)
    result = drain(setup)[-1]
    assert path.exists() and result['skipped'][reason] == 1 and result['protected'] == 1
    assert read_tool_output_artifact(ReadToolOutputArtifactRequest(setup[0].owner_home_dir, str(path)))['ok']


# LLM: 真实消息的结构化ref保护原scoped_call_id，不把正文出现相同字符串当引用。
# 函数用途: 验证会话pin存在时保留、移除后下一轮退休。
def test_conversation_pin_then_unpin(setup):
    path, value = artifact(setup)
    home = setup[0]
    store = ConversationStore(home.owner_home_dir / 'conversations')
    thread = store.threads.get_or_create({'channel': 'tui', 'channel_conversation_id': 'pin'})
    store.messages.append({'thread_id': thread.thread_id, 'role': 'assistant', 'content': '引用',
        'metadata': {'artifact_ref': value['scoped_call_id']}})
    # C-PINS扫描权威thread/checkpoint引用；消息引用随thread元数据携带，而非新增全量transcript扫描器。
    store.threads.update_atomic(thread.thread_id, lambda row: replace(row, metadata={'artifact_ref': value['scoped_call_id']}))
    first = drain(setup)
    assert path.exists() and any(row['skipped'].get('pinned') for row in first)
    store.threads.update_atomic(thread.thread_id, lambda row: replace(row, metadata={}))
    second = drain(setup, now=setup[2].timestamp() + 3601)
    assert not path.exists() and sum(row['deleted_items'] for row in second) == 1


# LLM: 故障发生在真实blob unlink之后；包装尚在也必须由已刷盘账本报告expired。
# 函数用途: 验证中断后读取语义及重启幂等回收。
def test_interruption_after_blob_unlink(setup, monkeypatch):
    path, _ = artifact(setup, blob=True)
    original = retention._unlink_retired_file
    # LLM: 仅对blob执行真实删除后抛错，包装不由测试直接删除。
    # 函数用途: 注入三阶段退休的最后阶段故障。
    def interrupted(root, parts, identity, result, **kwargs):
        original(root, parts, identity, result, **kwargs)
        if parts[-1].endswith('.blob'):
            raise OSError('injected after blob unlink')
    monkeypatch.setattr(retention, '_unlink_retired_file', interrupted)
    result = retention.maintain_owner_tool_outputs_lane(request(setup))
    assert path.exists() and not path.with_suffix('.blob').exists()
    assert read_tool_output_artifact(ReadToolOutputArtifactRequest(setup[0].owner_home_dir, str(path)))['error_code'] == 'artifact_expired'
    monkeypatch.setattr(retention, '_unlink_retired_file', original)
    rows = drain(setup, cursor=result['cursor'])
    assert not path.exists() and sum(row['deleted_items'] for row in rows) == 1


# LLM: 每片只检查一个包装，JSON续点不能提前宣布完成或重复累计删除。
# 函数用途: 验证小预算分片进度及真实任务根排除。
def test_cursor_budget_and_task_root_exclusion(setup):
    paths = [artifact(setup, str(index))[0] for index in range(3)]
    task, _ = artifact(setup, 'task-only', task_root=True)
    rows = drain(setup, items=1)
    assert len(rows) >= 3 and all(row['items'] <= 1 for row in rows)
    assert all(not row['complete'] for row in rows[:-1])
    assert sum(row['deleted_items'] for row in rows) == 3
    assert all(not path.exists() for path in paths) and task.exists()


# LLM: 冷Owner和legal_hold均不可取得写门或创建存储目录。
# 函数用途: 验证空目录零写与持有策略。
def test_cold_owner_and_hold_zero_commits(tmp_path, setup):
    home = home_paths(tmp_path / 'cold')
    calls = []
    cold = (home, None, setup[2])
    assert retention.maintain_owner_tool_outputs_lane(request(cold, commits=calls))['complete']
    assert not home.owner_home_dir.exists() and calls == []
    home.owner_home_dir.mkdir(parents=True)
    assert retention.maintain_owner_tool_outputs_lane(request(cold, commits=calls))['complete']
    assert not (home.owner_home_dir / 'blobs').exists() and calls == []
    path, _ = artifact(setup)
    result = retention.maintain_owner_tool_outputs_lane(request(setup, hold=True, commits=calls))
    assert result['deferred_reason'] == 'legal_hold' and path.exists() and calls == []


# LLM: held锁名必须在续点之前保持未决，释放后原名称会重试。
# 函数用途: 覆盖原锁竞争与恢复。
def test_lock_busy_keeps_cursor(setup):
    path, _ = artifact(setup)
    with held_lock(path):
        result = retention.maintain_owner_tool_outputs_lane(request(setup))
        assert result['skipped']['lock_busy'] == 1 and not result['complete']
        assert result['cursor'].get('after', '') == '' and path.exists()
    assert sum(row['deleted_items'] for row in drain(setup, cursor=result['cursor'])) == 1


# LLM: 覆盖项使用原retention.json入口，终态枚举与仓库权威保持一致。
# 函数用途: 验证一天覆盖、young跳过与终态常量合同。
def test_override_and_terminal_contract(setup):
    home = setup[0]
    home.owner_retention_json.parent.mkdir(parents=True, exist_ok=True)
    policy = json.loads(home.owner_retention_json.read_text())
    home.owner_retention_json.write_text(json.dumps({**policy, 'tool_output_days': 1}))
    old, _ = artifact(setup, 'two-day', age=2)
    young, _ = artifact(setup, 'young', age=0)
    rows = drain(setup)
    assert not old.exists() and young.exists()
    assert sum(row['deleted_items'] for row in rows) == 1
    assert retention._ENDED_ATTEMPT_STATUSES == _QUIESCENT_ATTEMPT_STATUSES


# LLM: 缺库/坏库/链接根是未知，不因IO失败放宽保留。
# 函数用途: 验证权威读取失败和路径边界的fail-closed行为。
def test_unreadable_runtime_and_linked_root(setup, tmp_path):
    path, _ = artifact(setup)
    runtime_db_path(setup[0].owner_home_dir).write_bytes(b'not a sqlite database')
    result = drain(setup)[-1]
    assert path.exists() and {'code': 'runtime_db_unreadable'} in result['errors']
    outside = tmp_path / 'outside'
    outside.mkdir()
    cold = home_paths(tmp_path / 'linked')
    cold.owner_home_dir.mkdir(parents=True)
    (cold.owner_home_dir / 'blobs').symlink_to(outside, target_is_directory=True)
    result = retention.maintain_owner_tool_outputs_lane(request((cold, None, setup[2])))
    assert result['errors'] == [{'code': 'tool_outputs_unreadable'}] and list(outside.iterdir()) == []


# LLM: 对真实分片fsync注入失败，任何未持久化证明都不能进入unlink阶段。
# 函数用途: 证伪退休证据刷盘失败后仍删除正文的错误实现。
def test_retired_shard_fsync_failure_keeps_body(setup, monkeypatch):
    path, _ = artifact(setup, blob=True)
    original = retention.os.fsync
    # LLM: 仅本次维护期间拒绝fsync，包装和blob在注入前由真实writer生成。
    # 函数用途: 注入退休证据未能刷盘的故障。
    def fail_fsync(descriptor):
        raise OSError('injected retired shard fsync failure')
    monkeypatch.setattr(retention.os, 'fsync', fail_fsync)
    result = retention.maintain_owner_tool_outputs_lane(request(setup))
    assert path.exists() and path.with_suffix('.blob').exists()
    assert not result['complete'] and result['deleted_items'] == 0
    assert any(error['code'] == 'retired_shard_unwritable' for error in result['errors'])
    monkeypatch.setattr(retention.os, 'fsync', original)
    assert sum(row['deleted_items'] for row in drain(setup, cursor=result['cursor'])) == 1


# LLM: 在写门准入后用原writer原子替换包装；扫描旧inode不能授权删除新代次。
# 函数用途: 证伪扫描与提交之间身份变化被忽略的实现。
def test_identity_change_at_commit_keeps_new_wrapper(setup):
    path, value = artifact(setup)
    initial = path.stat().st_ino
    # LLM: 模拟并发writer先发表新inode，之后才允许维护批次执行。
    # 函数用途: 在真实提交边界构造代次变化。
    def commit(operation):
        write_private_text_file_atomic(path, json.dumps(value, ensure_ascii=False))
        assert path.stat().st_ino != initial
        return operation()
    result = retention.maintain_owner_tool_outputs_lane(replace(request(setup), coordinator=True, commit=commit))
    assert path.exists() and result['deleted_items'] == 0 and result['skipped']['identity_changed'] == 1
    assert not (path.parent / 'retired').exists()
    assert read_tool_output_artifact(ReadToolOutputArtifactRequest(setup[0].owner_home_dir, str(path)))['ok']


# LLM: 包装锁和分片锁不得嵌套；分片锁忙同样是未决，不可丢游标或提前unlink。
# 函数用途: 验证退休账本本身的真实锁竞争和重试。
def test_retired_shard_lock_busy_is_undecided(setup):
    path, value = artifact(setup)
    retired = path.parent / 'retired'
    retired.mkdir(mode=0o700)
    shard = path.parent.joinpath(*retention.retired_shard_parts(value['created_at']))
    with held_lock(shard):
        result = retention.maintain_owner_tool_outputs_lane(request(setup))
        assert path.exists() and not result['complete'] and result['skipped']['lock_busy'] == 1
        assert result['cursor'].get('after', '') == ''
    assert sum(row['deleted_items'] for row in drain(setup, cursor=result['cursor'])) == 1


# LLM: 预置退休分片的符号链接/硬链接时，不仅正文要保留，链接目标字节与权限也不能被触碰。
# 函数用途: 复现并约束审查发现的分片追加路径越界。
@pytest.mark.parametrize('link_kind', ['symlink', 'hardlink'])
def test_shard_link_never_modifies_target(setup, tmp_path, link_kind, monkeypatch, record_property):
    import stat

    path, value = artifact(setup)
    target = tmp_path / 'outside-ledger'
    target.write_bytes(b'outside evidence\n')
    target.chmod(0o640)
    before = target.read_bytes(), stat.S_IMODE(target.stat().st_mode)
    shard = path.parent.joinpath(*retention.retired_shard_parts(value['created_at']))
    shard.parent.mkdir(mode=0o700)
    if link_kind == 'symlink':
        shard.symlink_to(target)
    else:
        try:
            os.link(target, shard)
            record_property('hardlink_fixture', 'real_hardlink')
        except PermissionError:
            from agent_py_agent.agent.common import nofollow_fs

            # 当前入口拒绝创建硬链接时，仅模拟stat的双链接事实；不冒称真实硬链接集成验证。
            record_property('hardlink_fixture', 'metadata_model_after_EPERM_not_real_hardlink')
            shard.write_bytes(before[0])
            shard.chmod(before[1])
            original_stat = os.stat
            # LLM: 只改变目标分片的nlink观察，其余FD/内容/权限与真实writer路径保持。
            # 函数用途: 在无建链权限时验证已知双链接叶的拒绝分支。
            def double_link_stat(path, *args, **kwargs):
                result = original_stat(path, *args, **kwargs)
                if not isinstance(path, int) and os.fspath(path) in (str(shard), shard.name):
                    values = list(result)
                    values[3] = 2
                    return os.stat_result(values)
                return result
            original_append = nofollow_fs.append_text_beneath
            # LLM: 仅在分片追加期间注入双链接stat，避免把其他pin来源的时间字段替换成模型数据。
            # 函数用途: 将元数据模型严格限定于被审查的追加入口。
            def append_with_double_link(*args, **kwargs):
                with monkeypatch.context() as context:
                    context.setattr(os, 'stat', double_link_stat)
                    return original_append(*args, **kwargs)
            monkeypatch.setattr(nofollow_fs, 'append_text_beneath', append_with_double_link)
    result = retention.maintain_owner_tool_outputs_lane(request(setup))
    assert (target.read_bytes(), stat.S_IMODE(target.stat().st_mode)) == before
    assert (shard.read_bytes(), stat.S_IMODE(shard.stat().st_mode)) == before
    assert path.exists() and result['deleted_items'] == 0 and not result['complete']


# LLM: 在清扫写门前替换目录，清扫只能继续使用已核定的原目录FD，不能跟随外部链接。
# 函数用途: 复现并约束审查发现的锁清扫目录替换竞态。
def test_lock_sweep_directory_swap_never_touches_external(setup, tmp_path):
    home, _, now = setup
    directory = home.owner_home_dir / 'blobs' / 'tool_outputs'
    directory.mkdir(parents=True, exist_ok=True)
    original_lock = directory / 'old.json.lock'
    original_lock.write_bytes(b'')
    outside = tmp_path / 'external-locks'
    outside.mkdir()
    outside_lock = outside / original_lock.name
    outside_lock.write_bytes(b'')
    old = now.timestamp() - 90000
    for path in (original_lock, outside_lock):
        os.utime(path, (old, old))
    calls = []
    # LLM: 只在维护已发现真实清扫候选后交换目录，精确复现查用间隙。
    # 函数用途: 将普通路径换成外部链接后执行原提交操作。
    def commit(operation):
        calls.append(True)
        directory.rename(directory.with_name('tool_outputs-previous'))
        directory.symlink_to(outside, target_is_directory=True)
        return operation()
    result = retention.maintain_owner_tool_outputs_lane(replace(request(setup), coordinator=True, commit=commit))
    assert calls and result['complete'] and outside_lock.exists()


# LLM: 目录fsync失败发生于真实unlink之后；失败片与恢复片的释放量必须覆盖实际删除的全部字节。
# 函数用途: 证伪物理删除已发生但回执永久漏计的实现。
def test_freed_bytes_include_unlink_before_directory_fsync_error(setup, monkeypatch):
    import stat

    path, _ = artifact(setup, blob=True)
    expected = path.stat().st_size + path.with_suffix('.blob').stat().st_size
    original = retention.os.fsync
    # LLM: 分片文件fsync仍成功，仅拒绝目录同步，保证故障确在unlink之后。
    # 函数用途: 注入真实目录持久化失败。
    def fail_directory_fsync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError('directory fsync failed after unlink')
        return original(descriptor)
    monkeypatch.setattr(retention.os, 'fsync', fail_directory_fsync)
    first = retention.maintain_owner_tool_outputs_lane(request(setup))
    assert not path.with_suffix('.blob').exists()
    monkeypatch.setattr(retention.os, 'fsync', original)
    rest = drain(setup, cursor=first['cursor'])
    assert not path.exists() and first['freed_bytes'] + sum(row['freed_bytes'] for row in rest) == expected


# LLM: 原生FD目录只读计数，目录枚举自身的临时FD在两次观察中相同且立即关闭。
# 函数用途: 返回本进程实际打开的FD数，不以常量或删除数量代替资源观察。
def _fd_count():
    root = Path('/proc/self/fd') if Path('/proc/self/fd').is_dir() else Path('/dev/fd')
    return len(os.listdir(root))


# LLM: 真实pins前等待两倍时间片，原writer、权威attempt及三阶段退休器全部实际执行。
# 函数用途: 验证慢引用扫描仍持续退休正文、证据先落盘、游标收敛且FD零增量。
def test_slow_pin_snapshot_still_retires_due_outputs(setup, monkeypatch, record_property):
    import time

    from agent_py_agent.agent.memory_store import retention_scan
    from agent_py_agent.agent.user_space.storage_lanes import STORAGE_LANE_SLICE_MS

    artifacts = [artifact(setup, 'slow-inline-a'), artifact(setup, 'slow-blob', blob=True),
                 artifact(setup, 'slow-inline-b')]
    paths = [path for path, _ in artifacts]
    blobs = [path.with_name(value['content_blob']) for path, value in artifacts if value.get('content_blob')]
    index = paths[0].parent / 'index.jsonl'
    index_before = index.read_bytes()
    original = retention_scan.current_retention_pins
    pin_calls, commits = [], []

    def slow_pins(*args, **kwargs):
        pin_calls.append(True)
        time.sleep(2 * STORAGE_LANE_SLICE_MS / 1000)
        return original(*args, **kwargs)

    monkeypatch.setattr(retention_scan, 'current_retention_pins', slow_pins)
    before = _fd_count()
    cursor, rows = {}, []
    for _ in range(8):
        pins_before, commits_before = len(pin_calls), len(commits)
        row = retention.maintain_owner_tool_outputs_lane(request(setup, cursor=cursor, commits=commits))
        validate_lane_receipt(row)
        rows.append(row)
        assert len(pin_calls) - pins_before == 1
        assert len(commits) - commits_before == 1
        assert row['deleted_items'] == 1
        if row['complete']:
            break
        assert any(path.exists() for path in paths)
        assert row['cursor'] != cursor
        cursor = json.loads(json.dumps(row['cursor']))
    assert rows[-1]['complete'] is True and sum(row['deleted_items'] for row in rows) == len(paths)
    assert all(not path.exists() for path in paths + blobs)
    retired = [json.loads(line) for shard in (paths[0].parent / 'retired').glob('*.jsonl')
               for line in shard.read_text().splitlines()]
    assert {row['name'] for row in retired} == {path.name for path in paths}
    assert index.read_bytes() == index_before
    for path in paths:
        assert read_tool_output_artifact(ReadToolOutputArtifactRequest(setup[0].owner_home_dir, str(path)))['error_code'] == 'artifact_expired'
    idle = retention.maintain_owner_tool_outputs_lane(request(setup, cursor=rows[-1]['cursor']))
    assert idle['complete'] and idle['items'] == idle['bytes'] == 0
    assert all(not path.exists() for path in paths + blobs)
    delta = _fd_count() - before
    record_property('fd_delta', delta)
    record_property('pin_calls', len(pin_calls))
    assert delta == 0


# LLM: 原锁真实竞争；unlink错误发生在真实父FD已打开之后，不替换删除或FD计量为假成功。
# 函数用途: 验证锁忙及删除失败不丢续点、不泄漏FD，条件解除后可原样恢复。
@pytest.mark.parametrize('condition', ['lock_busy', 'unlink_failure'])
def test_retirement_failure_paths_close_descriptors(setup, monkeypatch, record_property, condition):
    from contextlib import nullcontext

    path, value = artifact(setup, 'fd-check', blob=True)
    original, failed = os.unlink, []

    def fail_unlink(name, *args, **kwargs):
        if name == value['content_blob'] and kwargs.get('dir_fd') is not None:
            failed.append(True)
            raise OSError('simulated unlink failure')
        return original(name, *args, **kwargs)

    if condition == 'unlink_failure':
        monkeypatch.setattr(os, 'unlink', fail_unlink)
        # 替身仍使用真实dir_fd调用；保留原能力声明，避免被当成平台不支持而提前退出。
        monkeypatch.setattr(os, 'supports_dir_fd', os.supports_dir_fd | {fail_unlink})
    with held_lock(path) if condition == 'lock_busy' else nullcontext():
        before = _fd_count()
        row = retention.maintain_owner_tool_outputs_lane(request(setup))
        delta = _fd_count() - before
        record_property('fd_delta', delta)
        assert delta == 0 and path.exists() and row['deleted_items'] == 0
        assert not row['complete'] and row['cursor'].get('after', '') == ''
        if condition == 'lock_busy':
            assert row['skipped']['lock_busy'] == 1
        else:
            assert failed and row['errors']
            assert retention.retired_tool_output(setup[0].owner_home_dir, path, value)
    monkeypatch.setattr(os, 'unlink', original)
    assert sum(item['deleted_items'] for item in drain(setup, cursor=row['cursor'])) == 1
    assert not path.exists() and not path.with_suffix('.blob').exists()


# LLM: 一个完整窗口的有数据伴生锁不能让排序后部的旧孤儿永久饥饿；清扫续点跨完整pass保存。
# 函数用途: 验证有界canonical锁清扫最终能抵达并删除后部孤儿。
def test_lock_sweep_cursor_advances_past_retained_prefix(setup):
    home, _, now = setup
    root = home.owner_home_dir / 'blobs' / 'tool_outputs'
    root.mkdir(parents=True, exist_ok=True)
    retained = []
    old = now.timestamp() - 90000
    for index in range(128):
        data = root / f'a-{index:03}.json'
        data.write_bytes(b'{}')
        lock = root / (data.name + '.lock')
        lock.write_bytes(b'')
        os.utime(lock, (old, old))
        retained.append((data, lock))
    orphan = root / 'z-orphan.json.lock'
    orphan.write_bytes(b'')
    os.utime(orphan, (old, old))
    cursor = {}
    for step in range(3):
        rows = drain(setup, cursor=cursor, now=now.timestamp() + step * 3601)
        cursor = json.loads(json.dumps(rows[-1]['cursor']))
        if not orphan.exists():
            break
    assert not orphan.exists()
    assert all(data.read_bytes() == b'{}' and lock.exists() for data, lock in retained)
