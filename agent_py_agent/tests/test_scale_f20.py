"""W3：真实 canonical 历史下的列表预算、顺序、发布与迁移。"""
from __future__ import annotations

import errno
import importlib.util
import json
import os
import sys
from dataclasses import replace

import pytest

from agent_py_agent.agent.workflows import run_store
from agent_py_agent.agent.workflows.models import WorkflowRunRecord


# LLM: 合成历史只在隔离临时目录；列表始终使用正式 Store，不替换读取实现。
# 函数用途: 快速建立真实文件，候选版用生产批量发布函数建立对应投影。
def _seed(home, count, *, indexed=True):
    rows = []
    root = home / 'data' / 'workflow_runs'
    root.mkdir(parents=True, exist_ok=True)
    for n in range(count):
        record = WorkflowRunRecord(f'run-{n:05d}', f'flow-{n % 3}', 'alice', 'manual',
                                   'succeeded' if n % 2 else 'failed', float(n // 2), 0, 0)
        data = run_store._run_payload(record)
        folder = root / record.workflow_run_id
        folder.mkdir()
        (folder / 'run.json').write_text(json.dumps(data), encoding='utf-8')
        rows.append(data)
    module = 'agent_py_agent.agent.workflows.run_list_index'
    if indexed and importlib.util.find_spec(module) is not None:
        from agent_py_agent.agent.workflows.run_list_index import RunListIndex
        index = RunListIndex(home)
        index.publish_rows(rows)
    return rows


# LLM: 观测原 nofollow reader 的实际调用与字节，不把返回条数等同于读取预算。
# 函数用途: 统计所有运行 JSON 与步骤读取，保留真实 reader 行为。
def _observe(monkeypatch):
    read = run_store.read_text_beneath
    observations = {'run_files': 0, 'step_files': 0, 'bytes': 0}

    def counted(root, parts, *args, **kwargs):
        result = read(root, parts, *args, **kwargs)
        if len(parts) >= 4 and parts[:2] == ('data', 'workflow_runs'):
            observations['run_files' if parts[-1] == 'run.json' else 'step_files'] += 1
            observations['bytes'] += len(result.encode()) if result is not None else 0
        return result

    monkeypatch.setattr(run_store, 'read_text_beneath', counted)
    read_bytes = run_store.read_bytes_beneath

    def counted_bytes(root, parts, *args, **kwargs):
        result = read_bytes(root, parts, *args, **kwargs)
        if len(parts) >= 4 and parts[:2] == ('data', 'workflow_runs'):
            observations['run_files' if parts[-1] == 'run.json' else 'step_files'] += 1
            observations['bytes'] += len(result) if result is not None else 0
        return result

    monkeypatch.setattr(run_store, 'read_bytes_beneath', counted_bytes)
    module = 'agent_py_agent.agent.workflows.run_list_index'
    if importlib.util.find_spec(module) is not None:
        from agent_py_agent.agent.workflows import run_list_index
        monkeypatch.setattr(run_list_index, 'read_text_beneath', counted)
    return observations


@pytest.fixture(scope='module', params=[1000, 10000])
def history(tmp_path_factory, request):
    home = tmp_path_factory.mktemp('f20-' + str(request.param))
    return home, _seed(home, request.param)


# LLM: 固定页与过滤条件，历史只增长 run；每次循环必须归还全部真实 FD。
# 函数用途: 核对 1k→10k 历史的文件预算、排序及全部四种过滤。
@pytest.mark.parametrize('workflow,status', [(None, None), ('flow-1', None),
                                            (None, 'failed'), ('flow-1', 'succeeded')])
def test_list_page_reads_at_most_limit_plus_one(history, monkeypatch, workflow, status):
    home, rows = history
    store = run_store.WorkflowRunStore(home)
    expected = sorted((r for r in rows if (workflow is None or r['workflow_id'] == workflow)
                       and (status is None or r['status'] == status)),
                      key=lambda r: (-r['started_at'], r['workflow_run_id']))[:50]
    observed = _observe(monkeypatch)
    fd_dir = '/proc/self/fd' if sys.platform == 'linux' else '/dev/fd'
    before = len(os.listdir(fd_dir))
    actual = store.list_runs(workflow_id=workflow, status=status, limit=50)
    assert len(os.listdir(fd_dir)) - before == 0
    assert [r.workflow_run_id for r in actual] == [r['workflow_run_id'] for r in expected]
    assert observed['run_files'] <= 51, observed
    assert observed['step_files'] == 0, observed


# LLM: 列表投影必须随后跟踪 canonical 提交；终态保护与 CAS 不因索引新增而改变。
# 函数用途: 核对真实 create/save 路径的过滤变更与同时间排序。
def test_public_save_publishes_latest_filter_and_keeps_cas(tmp_path):
    store = run_store.WorkflowRunStore(tmp_path)
    record = WorkflowRunRecord('a', 'flow', 'alice', 'manual', 'queued', 1, 0, 0)
    store.create_run(record)
    assert [r.workflow_run_id for r in store.list_runs(status='queued')] == ['a']
    store.save_run(replace(record, status='succeeded'))
    assert store.list_runs(status='queued') == []
    assert [r.status for r in store.list_runs(status='succeeded')] == ['succeeded']
    with pytest.raises(ValueError, match='WORKFLOW_RUN_REVISION_CONFLICT'):
        store.save_run(record)


# LLM: 小样本按锚点的有序目录稳定排序比较，页内不能悄悄改变相同时间的顺序。
# 函数用途: 比较完整记录的兼容形状与排序。
def test_small_history_matches_anchor_records_and_order(tmp_path):
    rows = _seed(tmp_path, 17)
    store = run_store.WorkflowRunStore(tmp_path)
    expected = sorted((store.load_run(r['workflow_run_id']) for r in rows),
                      key=lambda r: r.started_at, reverse=True)[:8]
    assert store.list_runs(limit=8) == expected


# LLM: 一次模拟发布前/后错误；canonical 已提交，重启列表不得漏行或重复推进业务版本。
# 函数用途: 验证 commit marker 在 SQLite 发布与 ACK 间的幂等恢复。
@pytest.mark.parametrize('published', [False, True])
def test_canonical_commit_marker_replays_after_publication_error(tmp_path, monkeypatch, published):
    from agent_py_agent.agent.workflows.run_list_index import RunListIndex
    store = run_store.WorkflowRunStore(tmp_path)
    record = WorkflowRunRecord('marker-run', 'flow', 'alice', 'manual', 'queued', 1, 0, 0)
    store.create_run(record)
    publish = RunListIndex.publish_rows
    errors = []

    def stop(index, rows):
        if not errors:
            errors.append('publish')
            if published:
                publish(index, rows)
            raise OSError(errno.EIO, 'simulated I/O error')
        return publish(index, rows)

    with monkeypatch.context() as point:
        point.setattr(RunListIndex, 'publish_rows', stop)
        store.save_run(replace(record, status='running'))
    latest = store.load_run('marker-run')
    assert latest.status == 'running' and latest.revision == 2
    assert RunListIndex(tmp_path).pending_ids() == ['marker-run']
    restarted = run_store.WorkflowRunStore(tmp_path)
    assert [r.workflow_run_id for r in restarted.list_runs(status='running')] == ['marker-run']
    assert restarted.list_runs(status='queued') == []
    assert restarted.load_run('marker-run').revision == latest.revision
    assert RunListIndex(tmp_path).pending_ids() == []


# LLM: 回填队列在进程间持久化；每次只发布 32 个旧行，同时精确合并尚未修复的余集。
# 函数用途: 验证迁移可续跑、不在首请求整批修复，以及迁移期间的列表完整性。
def test_legacy_backfill_advances_fixed_batches_and_merges_remainder(tmp_path):
    from agent_py_agent.agent.workflows.run_list_index import RunListIndex
    rows = _seed(tmp_path, 100, indexed=False)
    expected = sorted(rows, key=lambda r: (-r['started_at'], r['workflow_run_id']))[:7]
    for remaining in (68, 36, 4, 0):
        current = run_store.WorkflowRunStore(tmp_path)
        assert [r.workflow_run_id for r in current.list_runs(limit=7)] == [r['workflow_run_id'] for r in expected]
        assert len(RunListIndex(tmp_path).legacy_ids()) == remaining


# LLM: 实际执行 SQLite 字节码与 EXPLAIN，不凭索引存在或 SQL 字符串断言复杂度。
# 函数用途: 验证四种过滤和后继页在 LIMIT 前没有历史全表遍历或临时排序。
def test_all_filter_plans_and_keysets_have_bounded_vm_steps(history):
    from agent_py_agent.agent.workflows.run_list_index import RunListIndex
    home, _ = history
    index = RunListIndex(home)
    for workflow, status in ((None, None), ('flow-1', None), (None, 'failed'), ('flow-1', 'succeeded')):
        for after in (None, (-100.0, 'run-00200')):
            sql, args = index.query(workflow, status, after=after, limit=51)
            with index.connection() as conn:
                plan = [row[3] for row in conn.execute('EXPLAIN QUERY PLAN ' + sql, args)]
                assert not any('TEMP B-TREE' in line for line in plan), plan
                assert all('USING' in line for line in plan), plan
                steps = []
                conn.set_progress_handler(lambda: steps.append(1) or 0, 1)
                try:
                    rows = conn.execute(sql, args).fetchall()
                finally:
                    conn.set_progress_handler(None, 0)
                assert len(rows) <= 51 and len(steps) < 2500, (plan, len(steps))
                print({'workflow': workflow, 'status': status, 'after': after, 'plan': plan, 'vm_steps': len(steps)})


# LLM: 独立步骤只在显式 detail 入口读，原列表没有权利从额外步骤叶构造新工作。
# 函数用途: 核对真实引擎产物的列表/详情文件访问边界。
def test_list_does_not_open_steps_but_detail_does(tmp_path, monkeypatch):
    from agent_py_agent.agent.workflows.engine import WorkflowEngine
    from agent_py_agent.tests.test_v14_workflow_claim_recovery import _definition
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda *args: {'value': 7})
    assert engine.run_workflow(_definition(), workflow_run_id='detail-run').status == 'succeeded'
    observed = _observe(monkeypatch)
    assert engine.store.list_runs()[0].status == 'succeeded'
    assert observed['step_files'] == 0
    assert engine.store.load_run('detail-run').steps[0].status == 'succeeded'
    assert observed['step_files'] == 1


# LLM: 工具与 Broker 都经过真实协议校验和同一 Owner Store，不伪造对端列表或序列化回执。
# 函数用途: 验证公开工具与插件遥测的列表往返和 Owner 隔离。
def test_real_workflows_tool_and_plugin_broker_list_roundtrip(tmp_path):
    from agent_py_agent.agent.conversation.host_actor import HostChannelActor
    from agent_py_agent.agent.gateway_parts.plugin_broker_api import dispatch_plugin_broker_request
    from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool
    from agent_py_agent.agent.workflows.business_scope import WorkflowScopeService
    from agent_py_agent.agent.workflows.engine import WorkflowEngine
    from agent_py_agent.tests.test_v14_workflow_claim_recovery import _definition
    home = tmp_path / 'owners' / 'providers' / 'test' / 'users' / 'alice'
    owner = 'providers/test/users/alice'
    home.mkdir(parents=True)
    engine = WorkflowEngine(home, analysis_handler=lambda *args: {'value': 7})
    result = engine.run_workflow(_definition(), owner_id=owner, workflow_run_id='wire-list')
    assert result.status == 'succeeded'
    actor = HostChannelActor(owner, 'source-thread', 'source-request', 'local', 'local', 'user')
    api = WorkflowScopeService(home, owner_id=owner,
        authorize=lambda current, action, ref: current == actor)
    tool = WorkflowsTool(home, engine=engine, business_service=api, business_actor_provider=lambda: actor)
    response = tool.execute({'action': 'workflow_runs.list', 'workflow_ref': 'recovery-flow', 'limit': 50})
    assert response.ok, response.output
    decoded = json.loads(json.dumps(response.result_envelope))
    assert [r['workflow_run_id'] for r in decoded['items']] == ['wire-list']
    telemetry = dispatch_plugin_broker_request(home, owner_id=owner, action='telemetry.workflows.runs.list',
        payload={'workflow_id': 'recovery-flow', 'status': 'succeeded', 'limit': 50})
    assert telemetry['ok'] and [r['workflow_run_id'] for r in telemetry['data']] == ['wire-list']
    foreign = tmp_path / 'owners' / 'providers' / 'test' / 'users' / 'bob'
    foreign.mkdir()
    hidden = dispatch_plugin_broker_request(foreign, owner_id='providers/test/users/bob',
        action='telemetry.workflows.runs.list', payload={})
    assert hidden['ok'] and hidden['data'] == []


# LLM: 已提交但尚未投影的积压不得静默漏项；单次仅重放固定批次，后续请求单调收敛。
# 函数用途: 验证超过恢复窗口时先明确拒绝，再通过同一公开入口恢复完整列表。
def test_commit_backlog_is_bounded_and_never_silently_returns_partial_list(tmp_path, monkeypatch):
    from agent_py_agent.agent.workflows.run_list_index import RunListIndex
    store = run_store.WorkflowRunStore(tmp_path)
    with RunListIndex(tmp_path).connection():
        pass

    def unavailable(*args):
        raise OSError(errno.EIO, 'simulated I/O error')

    with monkeypatch.context() as point:
        point.setattr(RunListIndex, 'publish_rows', unavailable)
        for n in range(40):
            record = WorkflowRunRecord(f'pending-{n}', 'flow', 'alice', 'manual', 'queued', n, 0, 0)
            store.create_run(record)
    with pytest.raises(RuntimeError, match='WORKFLOW_RUN_INDEX_RECOVERY_PENDING'):
        store.list_runs()
    assert len(RunListIndex(tmp_path).pending_ids()) == 8
    assert len(run_store.WorkflowRunStore(tmp_path).list_runs()) == 40


# LLM: 持有真实校验 FD，跨连接替换后恢复原路径仍必须拒绝；不对外部库发出业务 SQL。
# 函数用途: 验证 SQLite 打开前后父目录 ABA 与数据库叶 ABA 均不会通过投影入口。
@pytest.mark.parametrize('replacement', ['parent', 'leaf'])
def test_sqlite_connection_rejects_actual_open_path_replacement(tmp_path, monkeypatch, replacement):
    import sqlite3

    from agent_py_agent.agent.workflows.run_list_index import index_connection
    filename = 'workflow_run_index.sqlite3'
    (tmp_path / 'owner').mkdir()
    with index_connection(tmp_path / 'owner', filename) as conn:
        conn.execute('CREATE TABLE canonical(value)')
    foreign = tmp_path / 'foreign'
    foreign.mkdir()
    with sqlite3.connect(foreign / filename) as conn:
        conn.execute('CREATE TABLE private(value)')
        conn.execute("INSERT INTO private VALUES('untouched')")
    foreign_bytes = (foreign / filename).read_bytes()
    data = tmp_path / 'owner' / 'data'
    victim = data if replacement == 'parent' else data / filename
    saved = victim.with_name(victim.name + '-saved')
    target = foreign if replacement == 'parent' else foreign / filename
    connect = sqlite3.connect
    opened = []

    def replaced(*args, **kwargs):
        victim.rename(saved)
        victim.symlink_to(target, target_is_directory=replacement == 'parent')
        try:
            current = connect(*args, **kwargs)
            opened.append(current)
            return current
        finally:
            victim.unlink()
            saved.rename(victim)

    fd_dir = '/proc/self/fd' if sys.platform == 'linux' else '/dev/fd'
    before = len(os.listdir(fd_dir))
    with monkeypatch.context() as point:
        point.setattr(sqlite3, 'connect', replaced)
        with pytest.raises(PermissionError, match='DATASOURCE_SQLITE_PATH_REPLACED'):
            with index_connection(tmp_path / 'owner', filename):
                pytest.fail('replaced SQLite connection was exposed')
    assert len(os.listdir(fd_dir)) == before
    assert (foreign / filename).read_bytes() == foreign_bytes
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        opened[0].execute('SELECT 1')


# LLM: 数据库与所有附属叶在准入时均拒绝符号链接，不能借投影读取越过 Owner 根。
# 函数用途: 核对实际符号链接拒绝，保留外部文件原样。
@pytest.mark.parametrize('suffix', ['', '-wal', '-shm', '-journal'])
def test_sqlite_index_rejects_linked_database_or_sidecar(tmp_path, suffix):
    from agent_py_agent.agent.workflows.run_list_index import index_connection
    home = tmp_path / 'owner'
    data = home / 'data'
    data.mkdir(parents=True)
    foreign = tmp_path / 'foreign'
    foreign.write_bytes(b'private bytes')
    target = data / ('workflow_run_index.sqlite3' + suffix)
    target.symlink_to(foreign)
    with pytest.raises((ValueError, PermissionError, OSError)):
        with index_connection(home, 'workflow_run_index.sqlite3'):
            pytest.fail('linked SQLite leaf was exposed')
    assert foreign.read_bytes() == b'private bytes'


# LLM: 此用例仅覆盖多链接元数据的准入分支；不把模拟 stat 结果称为实际硬链接 IO 验证。
# 函数用途: 在禁止建立硬链接的执行环境中保留链接数拒绝条件的定向回归。
@pytest.mark.parametrize('suffix', ['', '-wal', '-shm', '-journal'])
def test_sqlite_index_rejects_multiple_link_metadata(tmp_path, monkeypatch, suffix):
    from pathlib import Path

    from agent_py_agent.agent.workflows.run_list_index import index_connection
    home = tmp_path / 'owner'
    data = home / 'data'
    data.mkdir(parents=True)
    target = data / ('workflow_run_index.sqlite3' + suffix)
    target.write_bytes(b'private bytes')
    lstat = Path.lstat

    def observed(path, *args, **kwargs):
        result = lstat(path, *args, **kwargs)
        if path == target:
            fields = list(result)
            fields[3] = 2
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(Path, 'lstat', observed)
    with pytest.raises(ValueError, match='单链接普通文件'):
        with index_connection(home, 'workflow_run_index.sqlite3'):
            pytest.fail('multiple-link metadata was accepted')
    assert target.read_bytes() == b'private bytes'


# LLM: 原子写在重命名前真实退出仅留下未发表临时叶；它没有授权阻塞已有运行列表。
# 函数用途: 验证旧提交暂存文件遗留不会把恢复入口永久锁在积压状态。
def test_unpublished_temporary_marker_after_process_exit_does_not_block_list(tmp_path):
    import subprocess

    _seed(tmp_path, 1)
    code = '''import os, sys
from agent_py_agent.agent.common import nofollow_fs as fs
supported = fs._supports_dir_fd()
fs._supports_dir_fd = lambda: supported
os.rename = os.replace = lambda *args, **kwargs: os._exit(17)
fs.write_text_atomic_beneath(sys.argv[1], ('data', 'workflow_run_pending', '0'*64+'.json'), '{"run_id":"unpublished"}')
'''
    child = subprocess.run([sys.executable, '-c', code, str(tmp_path)], capture_output=True,
                           text=True, env=dict(os.environ), timeout=10)
    assert child.returncode == 17, child.stderr
    assert list((tmp_path / 'data' / 'workflow_run_pending').iterdir())
    assert [r.workflow_run_id for r in run_store.WorkflowRunStore(tmp_path).list_runs()] == ['run-00000']


# LLM: 只交错迁移，不改变任何canonical业务字段；索引与余集不能出现观察点缝隙。
# 函数用途: 复现列表取候选后，另一迁移者完成最后一批时的漏项。
def test_legacy_snapshot_survives_concurrent_final_batch_migration(tmp_path, monkeypatch):
    from agent_py_agent.agent.workflows.run_list_index import RunListIndex
    _seed(tmp_path, 34, indexed=False)
    page = RunListIndex.page
    points = []

    def interleave(index, *args, **kwargs):
        rows = page(index, *args, **kwargs)
        if not points:
            points.append('after-index-page')
            RunListIndex(tmp_path).backfill(run_store.WorkflowRunStore(tmp_path))
        return rows

    monkeypatch.setattr(RunListIndex, 'page', interleave)
    assert [r.workflow_run_id for r in run_store.WorkflowRunStore(tmp_path).list_runs(limit=1)] == ['run-00032']
    assert points == ['after-index-page']


# LLM: 未发表候选叶可以排在正式旧spool之前；批次必须对正式输入计数，不能永远返回临时叶。
# 函数用途: 核对原生和portable目录入口都能越过临时叶并归还全部描述符。
@pytest.mark.parametrize('portable', [False, True])
def test_pending_window_does_not_starve_formal_records(tmp_path, monkeypatch, portable):
    from contextlib import contextmanager

    from agent_py_agent.agent.workflows import run_list_index as index

    parts = ('.run-summary-spool', 'script', 'probe')
    parent = tmp_path.joinpath(*parts)
    parent.mkdir(parents=True)
    for number in range(35):
        (parent / f'.{number:032x}.json.tmp-{number:032x}').write_text('{}')
    filename = 'f' * 32 + '.json'
    (parent / filename).write_text('{}')
    scandir = os.scandir

    # LLM: 只固定真实目录条目的顺序以重现临时叶在前，不替换叶或读取结果。
    # 函数用途: 让同一个有效输入在两个平台分支中都位于临时叶之后。
    @contextmanager
    def staged_first(path):
        with scandir(path) as entries:
            yield iter(sorted(entries, key=lambda entry: entry.name))

    monkeypatch.setattr(os, 'scandir', staged_first)
    monkeypatch.setattr(index, '_supports_dir_fd', lambda: not portable)
    fd_dir = '/proc/self/fd' if sys.platform == 'linux' else '/dev/fd'
    before = len(os.listdir(fd_dir))
    assert index.pending_names(tmp_path, parts, 1) == [filename]
    assert len(os.listdir(fd_dir)) == before
    assert len(list(parent.iterdir())) == 36


@pytest.mark.parametrize('existing_index', [False, True])
def test_legacy_system_leaves_and_old_polluted_index_self_heal(tmp_path, existing_index):
    from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool
    from agent_py_agent.agent.workflows.run_list_index import RunListIndex

    _seed(tmp_path, 2, indexed=False)
    folder = tmp_path / 'data/workflow_runs'
    noise = folder / '.DS_Store'
    noise.write_bytes(b'finder metadata')
    (folder / 'ordinary-file').write_text('not a run directory')
    (folder / 'alias-run').symlink_to(folder / 'run-00000', target_is_directory=True)
    index = RunListIndex(tmp_path)
    if existing_index:
        with index.connection() as db:
            db.executemany('INSERT OR IGNORE INTO legacy VALUES(?)',
                [(name,) for name in ('.DS_Store', 'ordinary-file', 'alias-run')])
    store = run_store.WorkflowRunStore(tmp_path)
    status = WorkflowsTool(tmp_path).execute({'action': 'status', 'workflow_id': 'flow-1'})
    assert status.ok, status.result_envelope
    assert [r.workflow_run_id for r in store.list_runs()] == ['run-00000', 'run-00001']
    assert [r.workflow_run_id for r in store.list_runs(workflow_id='flow-1')] == ['run-00001']
    assert index.legacy_ids() == []
    assert noise.read_bytes() == b'finder metadata'
    assert (folder / 'ordinary-file').is_file() and (folder / 'alias-run').is_symlink()
    with index.connection() as db:
        assert db.execute('SELECT count(*) FROM legacy').fetchone()[0] == 0


def test_legacy_valid_corrupt_run_is_not_silently_discarded(tmp_path):
    _seed(tmp_path, 1, indexed=False)
    path = tmp_path / 'data/workflow_runs/run-00000/run.json'
    path.write_text('{broken')
    with pytest.raises(json.JSONDecodeError, match='property name'):
        run_store.WorkflowRunStore(tmp_path).list_runs()
    assert path.read_text() == '{broken'


def test_legacy_pollution_over_one_batch_does_not_break_remaining_union(tmp_path, monkeypatch):
    from agent_py_agent.agent.workflows import run_list_index
    from agent_py_agent.agent.workflows.run_list_index import RunListIndex

    _seed(tmp_path, 1, indexed=False)
    index = RunListIndex(tmp_path)
    with index.connection() as db:
        db.executemany('INSERT INTO legacy VALUES(?)', [(f'.noise-{n:03}',) for n in range(40)])
    calls = []
    original = run_list_index._legacy_run_directory
    monkeypatch.setattr(run_list_index, '_legacy_run_directory', lambda home, name:
        (calls.append(name), original(home, name))[1])
    store = run_store.WorkflowRunStore(tmp_path)
    assert len(store.list_runs()) == 1
    assert len(calls) == 32
    with index.connection() as db:
        assert db.execute('SELECT count(*) FROM legacy').fetchone()[0] == 9
    assert len(store.list_runs()) == 1
    with index.connection() as db:
        assert db.execute('SELECT count(*) FROM legacy').fetchone()[0] == 0


def test_index_initialization_does_not_stat_each_legacy_directory(tmp_path, monkeypatch):
    from agent_py_agent.agent.workflows.run_list_index import RunListIndex

    _seed(tmp_path, 100, indexed=False)
    folder = tmp_path / 'data/workflow_runs'
    original = os.lstat
    examined = []

    def observed(path, *args, **kwargs):
        if isinstance(path, (str, os.PathLike)) and os.path.dirname(os.fspath(path)) == str(folder):
            examined.append(os.fspath(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, 'lstat', observed)
    index = RunListIndex(tmp_path)
    with index.connection():
        pass
    assert len(index.legacy_ids()) == 100
    assert examined == []


def test_legacy_name_and_runtime_path_share_underscore_id_semantics(tmp_path):
    from agent_py_agent.agent.workflows.models import safe_path, safe_path_part
    from agent_py_agent.agent.workflows.run_list_index import _legacy_run_name

    for name in ('_legacy-run', 'run.1', 'run-1', '7', '_' * 128):
        assert safe_path_part(name) == name
        assert safe_path(tmp_path, name).name == name
        assert _legacy_run_name(name)
    for name in ('.DS_Store', '..', 'a/b', '', 'x' * 129, None):
        with pytest.raises(ValueError, match='无效的资产或步骤编号'):
            safe_path_part(name)
        assert not _legacy_run_name(name)
