"""摘要历史扩容、稳定顺序、事务重放及旧格式迁移的本地回归。"""
import inspect
import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_py_agent.agent.assets.descriptor import AssetDescriptor, RuntimeSummary, clean_json
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.workflow_store import WorkflowDefinition, WorkflowStore
from agent_py_agent.agent.workflows import run_summary_spool as spool


def next_run_summary(summary, status, duration_ms, run_at=None, last_error=""):
    if not isinstance(status, str) or not status.strip():
        raise ValueError("运行状态不能为空")
    if run_at is None:
        run_at = datetime.now(timezone.utc).isoformat()
    if status in {"failed", "error", "timeout", "timed_out"}:
        failures = summary.consecutive_failures + 1
        resolved_error = clean_json(str(last_error or summary.last_error or ""))
    else:
        failures = 0
        resolved_error = clean_json(str(last_error or ""))
    return RuntimeSummary(run_at, status, duration_ms, summary.run_count + 1, failures, resolved_error)


# LLM: 合成Owner祖先限制全局发现，所有运行记录只属于临时测试资产。
# 函数用途: 创建两种公开存储入口及统一摘要读写适配。
def asset(tmp_path, kind):
    home = tmp_path / 'app/owners/local/sample'
    descriptor = AssetDescriptor('sample', kind, '摘要样本')
    if kind == 'script':
        store = ScriptStore(home)
        store.save_script(descriptor, 'print(1)')
        def get():
            return store.get_script('sample')['runtime_summary']
    else:
        store = WorkflowStore(home)
        store.save_workflow(WorkflowDefinition(descriptor, ()))
        def get():
            return store.get_workflow('sample').descriptor.runtime_summary.to_dict()
    return store, get


# LLM: 私有代次只从实际存储快照读取，公共投影隐藏字段不能用来重建存储身份。
# 函数用途: 为批量历史准备和旧内部spool回归取得当前生命周期metadata。
def summary_metadata(store, kind):
    value = store._summary_snapshot('sample')
    return value['metadata'] if kind == 'script' else value.descriptor.metadata


# LLM: 旧定义空代次沿原目录，新定义的迁移测试叶按创建代次隔离。
# 函数用途: 定位测试资产当前生命周期的内部spool目录。
def spool_directory(store, kind):
    from agent_py_agent.agent.workflows.run_summary_index import generation
    return store.owner_home.joinpath(*spool._parts(kind, 'sample', generation(summary_metadata(store, kind))))


# LLM: 观察实际文件读及字节，不用耗时阈值代替复杂度约束。
# 函数用途: 统计一次公开get的Python文件IO并核对FD寿命。
def observed_get(get, monkeypatch):
    from agent_py_agent.agent.common.nofollow_fs import _supports_dir_fd

    observed = {'files': 0, 'bytes': 0}
    original_open, original_read, original_close = os.open, os.read, os.close
    capability = _supports_dir_fd()
    opened = set()

    def opening(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        opened.add(fd)
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            observed['files'] += 1
        return fd

    def reading(*args, **kwargs):
        data = original_read(*args, **kwargs)
        observed['bytes'] += len(data)
        return data

    def closing(fd):
        original_close(fd)
        opened.discard(fd)

    with monkeypatch.context() as patch:
        # 计数包装不是平台能力变化；保留原open的dir_fd声明，不能误切portable分支。
        if original_open in os.supports_dir_fd:
            patch.setattr(os, 'supports_dir_fd', os.supports_dir_fd | {opening})
        patch.setattr(os, 'open', opening)
        patch.setattr(os, 'read', reading)
        patch.setattr(os, 'close', closing)
        assert _supports_dir_fd() == capability
        result = get()
    assert opened == set()
    return result, observed


# LLM: 历史经正式批量导入准备，每个观察点仍由真实非阻塞完成推进并走公开get。
# 函数用途: 在60秒预算内验证1k和10k历史的固定读取预算及定义不增长。
@pytest.mark.timeout(60)
@pytest.mark.parametrize('kind', ['script', 'workflow'])
def test_public_get_budget_1k_to_10k(tmp_path, monkeypatch, kind):
    store, get = asset(tmp_path, kind)
    definition = store.owner_home / store.collection / 'sample' / store.filename
    original = definition.read_bytes()
    observations = []
    from agent_py_agent.agent.workflows.run_summary_index import RunSummaryIndex
    index = RunSummaryIndex(store.owner_home)
    metadata = summary_metadata(store, kind)
    for start, end in ((0, 1000), (1000, 10000)):
        for batch in range(start, end - 1, 1000):
            records = [spool.normalize_record(dict(status='succeeded', duration_ms=i,
                run_at=f'{i:08d}', run_id=f'run-{i:08d}')) for i in range(batch, min(batch + 1000, end - 1))]
            index.ingest(kind, 'sample', RuntimeSummary(), metadata, records)
        store.record_run_summary('sample', 'succeeded', end - 1, f'{end - 1:08d}',
                                 blocking=False, run_id=f'run-{end - 1:08d}')
        result, observed = observed_get(get, monkeypatch)
        print(kind, end, observed, flush=True)
        assert result['run_count'] == end
        assert result['last_duration_ms'] == end - 1
        assert observed['files'] <= 16, observed
        assert observed['bytes'] <= 32768, observed
        observations.append(observed)
        assert definition.read_bytes() == original
    assert observations[1]['files'] == observations[0]['files']
    assert observations[1]['bytes'] == observations[0]['bytes']
    assert not (store.owner_home / '.run-summary-spool').exists()


# LLM: 插入次序与摘要排序刻意不同，失败空错误继承及成功清空沿next_run_summary。
# 函数用途: 验证唯一run_id、乱序到达和错误语义等价于稳定排序后的原fold。
@pytest.mark.timeout(60)
@pytest.mark.parametrize('kind', ['script', 'workflow'])
def test_order_and_replay_equal_sorted_fold(tmp_path, kind):
    store, get = asset(tmp_path, kind)
    records = [
        dict(run_id='a', run_at='001', status='failed', duration_ms=1, last_error='first'),
        dict(run_id='b', run_at='002', status='succeeded', duration_ms=2, last_error=''),
        dict(run_id='c', run_at='003', status='timeout', duration_ms=3, last_error='latest'),
        dict(run_id='d', run_at='003', status='error', duration_ms=4, last_error=''),
        dict(run_id='e', run_at='004', status='failed', duration_ms=5, last_error=''),
    ]
    expected = RuntimeSummary()
    for record in records:
        expected = next_run_summary(expected, record['status'], record['duration_ms'], record['run_at'], record['last_error'])
    for i in (4, 0, 3, 2, 1, 4, 1):
        store.record_run_summary('sample', blocking=False, **records[i])
    assert get() == expected.to_dict()
    reopened = type(store)(store.owner_home)
    reopened.record_run_summary('sample', blocking=False, **records[0])
    assert get() == expected.to_dict()
    from agent_py_agent.agent.workflows.run_summary_index import RunSummaryIndex
    with RunSummaryIndex(store.owner_home).connection() as conn:
        assert conn.execute('SELECT count(*) FROM summary_runs').fetchone()[0] == len(records)
        assert conn.execute('SELECT count(*) FROM summary_assets').fetchone()[0] == 1


# LLM: 旧定义已含累计值与部分凭据，未ACK叶中同时包含重复与新观察。
# 函数用途: 验证旧metadata和spool固定批次迁移、重启续传及ACK前后重放不双算。
@pytest.mark.timeout(60)
def test_legacy_metadata_spool_bounded_resume_and_ack(tmp_path, monkeypatch):
    store, get = asset(tmp_path, 'script')
    ids = [f'old-{i}' for i in range(300)]
    baseline = RuntimeSummary('000', 'succeeded', 1, len(ids), 0, '')
    store.save_script(AssetDescriptor('sample', 'script', '旧摘要', runtime_summary=baseline,
        metadata={'_run_summary_spool': {'schema_version': 'run_summary_spool.v1', 'applied_run_ids': ids}}), 'print(1)')
    # 合成真正的升级前定义：旧版本尚无生命周期字段，spool沿原三段目录恢复。
    definition_path = store.owner_home / 'scripts/sample/script.json'
    definition = json.loads(definition_path.read_text())
    definition['metadata'].pop('_run_summary_generation', None)
    definition_path.write_text(json.dumps(definition), encoding='utf-8')
    for i in range(70):
        spool.append(store.owner_home, 'script', 'sample',
            dict(run_id=f'new-{i:03d}', run_at=f'{i+1:03d}', status='failed', duration_ms=i, last_error=''))
    spool.append(store.owner_home, 'script', 'sample',
        dict(run_id=ids[-1], run_at='000', status='succeeded', duration_ms=1))
    unlink = spool.unlink_file_beneath
    attempts = []

    def unavailable(*args, **kwargs):
        attempts.append(args)
        raise OSError('simulated ACK I/O error')

    monkeypatch.setattr(spool, 'unlink_file_beneath', unavailable)
    with pytest.raises(OSError):
        store.fold_run_summaries('sample')
    assert attempts
    partial = get()['run_count']
    assert len(ids) < partial <= len(ids) + 32
    monkeypatch.setattr(spool, 'unlink_file_beneath', unlink)
    reopened = ScriptStore(store.owner_home)
    for _ in range(12):
        reopened.fold_run_summaries('sample')
    assert get()['run_count'] == len(ids) + 70
    assert get()['consecutive_failures'] == 70
    assert list(spool_directory(store, 'script').glob('*.json')) == []
    raw = json.loads((store.owner_home / 'scripts/sample/script.json').read_text())
    assert '_run_summary_spool' not in raw['metadata']
    reopened.record_run_summary('sample', 'succeeded', 1, '000', blocking=False, run_id=ids[-1])
    assert get()['run_count'] == len(ids) + 70


# LLM: 防止未来改动把runner探测变成接受任意参数或把全历史读回公开投影。
# 函数用途: 固定公开签名及聚合读取边界，非法函数夹具必须被拒绝。
@pytest.mark.timeout(60)
def test_summary_signature_and_projection_guard():
    def valid_signature(method):
        parameters = inspect.signature(method).parameters
        return all(name in parameters for name in ('run_at', 'last_error', 'blocking', 'run_id')) and not any(
            item.kind == item.VAR_KEYWORD for item in parameters.values())

    assert valid_signature(ScriptStore.record_run_summary)
    assert valid_signature(WorkflowStore.record_run_summary)
    assert not valid_signature(lambda **kwargs: None)


# LLM: 子进程在真实SQLite事务内或提交后退出，不把模拟返回值当耐久重放证据。
# 函数用途: 验证新完成不依赖spool且提交前后退出均最终只累计一次。
@pytest.mark.timeout(60)
@pytest.mark.parametrize('phase', ['transaction', 'committed'])
def test_sqlite_process_exit_and_retry(tmp_path, phase):
    store, get = asset(tmp_path, 'script')
    code = '''import os, sys
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.workflows.run_summary_index import RunSummaryIndex
store = ScriptStore(sys.argv[1])
if sys.argv[2] == 'transaction':
    RunSummaryIndex._apply = staticmethod(lambda *args: os._exit(17))
else:
    original = RunSummaryIndex.ingest
    def stop(self, *args, **kwargs):
        original(self, *args, **kwargs)
        os._exit(17)
    RunSummaryIndex.ingest = stop
store.record_run_summary('sample', 'failed', 7, '001', blocking=False, run_id='durable')
'''
    completed = subprocess.run([sys.executable, '-c', code, str(store.owner_home), phase],
                               capture_output=True, text=True, env=dict(os.environ), timeout=10)
    assert completed.returncode == 17, completed.stderr
    assert get()['run_count'] == (0 if phase == 'transaction' else 1)
    reopened = ScriptStore(store.owner_home)
    reopened.record_run_summary('sample', 'failed', 7, '001', blocking=False, run_id='durable')
    assert get()['run_count'] == 1
    assert get()['last_duration_ms'] == 7
    assert not (store.owner_home / '.run-summary-spool').exists()


# LLM: SQL观察证明新完成仅写自己的凭据和聚合，公开读取不访问历史运行表。
# 函数用途: 固定事务写集及公开读口的SQL边界。
@pytest.mark.timeout(60)
def test_single_row_aggregate_write_and_direct_read(tmp_path, monkeypatch):
    from agent_py_agent.agent.workflows.run_summary_index import RunSummaryIndex
    store, get = asset(tmp_path, 'script')
    store.record_run_summary('sample', 'succeeded', 1, '001', blocking=False, run_id='a')
    connection = RunSummaryIndex.connection
    changes, statements = [], []

    @contextmanager
    def traced(self):
        with connection(self) as conn:
            before = conn.total_changes
            conn.set_trace_callback(statements.append)
            yield conn
            changes.append(conn.total_changes - before)

    monkeypatch.setattr(RunSummaryIndex, 'connection', traced)
    store.record_run_summary('sample', 'failed', 2, '002', blocking=False, run_id='b')
    assert changes == [2]
    statements.clear()
    assert get()['run_count'] == 2
    assert not any('summary_runs' in sql for sql in statements)
    assert sum('SELECT state FROM summary_assets' in sql for sql in statements) == 1


# LLM: 摘要事务不能改变执行定义身份或偷改CAS版本；显式fold仍沿原资产写事务递增版本。
# 函数用途: 核对脚本及工作流的引用、版本冲突和普通编辑后的摘要保持。
@pytest.mark.timeout(60)
@pytest.mark.parametrize('kind', ['script', 'workflow'])
def test_definition_refs_and_revision_cas(tmp_path, kind):
    store, get = asset(tmp_path, kind)
    if kind == 'script':
        before = store.get_script('sample')
        reference, revision = before['execution_definition_ref'], before['revision']
    else:
        before = store.get_workflow('sample')
        reference, revision = before.execution_definition_ref, before.descriptor.revision
    store.record_run_summary('sample', 'succeeded', 1, '001', blocking=False, run_id='stable')
    if kind == 'script':
        after = store.get_script('sample')
        assert (after['execution_definition_ref'], after['revision']) == (reference, revision)
    else:
        after = store.get_workflow('sample')
        assert (after.execution_definition_ref, after.descriptor.revision) == (reference, revision)
    store.fold_run_summaries('sample')
    with pytest.raises(ValueError, match='REVISION_CONFLICT'):
        if kind == 'script':
            store.save_script(AssetDescriptor('sample', kind, '编辑'), 'print(2)', expected_revision=revision)
        else:
            store.save_workflow(before, expected_revision=revision)
    store.record_run_summary('sample', 'succeeded', 1, '001', blocking=False, run_id='stable')
    assert get()['run_count'] == 1


# LLM: 旧随机叶不保证枚举顺序，跨批发现更早重复观察时仍须等于原稳定排序fold。
# 函数用途: 验证迁移批次顺序不改变首次身份、最后状态、失败尾段和错误继承。
@pytest.mark.timeout(60)
def test_legacy_duplicate_order_across_batches(tmp_path, monkeypatch):
    store, get = asset(tmp_path, 'script')
    records = [dict(run_id='duplicate', run_at='999', status='succeeded', duration_ms=9, last_error='late')]
    records.extend(dict(run_id=f'run-{i}', run_at=f'{i:03d}', status='failed', duration_ms=i, last_error='')
                   for i in range(1, 32))
    records.extend([
        dict(run_id='duplicate', run_at='000', status='failed', duration_ms=0, last_error='first'),
        dict(run_id='reset', run_at='010', status='succeeded', duration_ms=0, last_error='')])
    for i, record in enumerate(records):
        from agent_py_agent.agent.workflows.run_summary_index import generation
        spool.write_text_atomic_beneath(store.owner_home,
            (*spool._parts('script', 'sample', generation(summary_metadata(store, 'script'))), f'{i:032x}.json'), json.dumps(record))
    directory = spool_directory(store, 'script')
    monkeypatch.setattr(spool, 'pending_names', lambda home, parts, size: sorted(os.listdir(directory))[:size])
    store.fold_run_summaries('sample')
    assert get()['run_count'] == 32
    store.fold_run_summaries('sample')
    seen, expected = set(), RuntimeSummary()
    for record in sorted(records, key=lambda value: (value['run_at'], value['run_id'])):
        if record['run_id'] not in seen:
            seen.add(record['run_id'])
            expected = next_run_summary(expected, record['status'], record['duration_ms'], record['run_at'], record['last_error'])
    assert get() == expected.to_dict()


# LLM: 本例只隔离验证DDL可重入性，真实文件子进程退出不提交user_version；连接路径边界另由集成回归验证。
# 函数用途: 在任一建表前缀落盘后重开同一SQLite文件，保证初始化与首次完成可续传。
@pytest.mark.timeout(60)
@pytest.mark.parametrize('prefix', [1, 4, 5])
def test_initialization_reopens_after_partial_ddl(tmp_path, monkeypatch, prefix):
    from agent_py_agent.agent.workflows import run_summary_index as module
    database = tmp_path / 'partial.sqlite3'
    code = '''import ast, os, sqlite3, sys
from pathlib import Path
from agent_py_agent.agent.workflows import run_summary_index as module
tree = ast.parse(Path(module.__file__).read_text())
call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
    and isinstance(node.func, ast.Attribute) and node.func.attr == 'executescript')
statements = [value.strip() for value in call.args[0].value.split(';') if value.strip()]
conn = sqlite3.connect(sys.argv[1])
for sql in statements[:int(sys.argv[2])]:
    conn.execute(sql)
os._exit(17)
'''
    completed = subprocess.run([sys.executable, '-c', code, str(database), str(prefix)],
                               capture_output=True, text=True, env=dict(os.environ), timeout=10)
    assert completed.returncode == 17, completed.stderr

    @contextmanager
    def ddl_connection(owner_home, filename):
        conn = sqlite3.connect(database)
        conn.row_factory = sqlite3.Row
        try:
            assert conn.execute('PRAGMA user_version').fetchone()[0] == 0
            with conn:
                yield conn
        finally:
            conn.close()

    monkeypatch.setattr(module, 'index_connection', ddl_connection)
    index = module.RunSummaryIndex(tmp_path)
    result, complete = index.ingest('script', 'sample', RuntimeSummary(), {}, [
        spool.normalize_record(dict(run_id='first', run_at='001', status='succeeded', duration_ms=1))])
    assert complete and result.run_count == 1
    with sqlite3.connect(database) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM summary_runs').fetchone()[0] == 1


# LLM: 本夹具只隔离摘要事务语义，不作为安全连接路径或原生平台验收；父级另跑未替换helper的完整回归。
# 函数用途: 为生命周期交错测试提供真实SQLite事务，避免把父链权限问题混入业务断言。
@pytest.fixture
def summary_transaction_layer(monkeypatch):
    from agent_py_agent.agent.workflows import run_summary_index as module

    @contextmanager
    def transaction(owner_home, filename):
        path = Path(owner_home) / 'data' / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    monkeypatch.setattr(module, 'index_connection', transaction)


# LLM: 删除后同ID是新生命周期，不能继承旧聚合、凭据或尚未迁移的旧叶。
# 函数用途: 验证脚本及工作流重新创建后摘要归零，旧run_id在新资产中可以正常累计一次。
@pytest.mark.timeout(60)
@pytest.mark.parametrize('kind', ['script', 'workflow'])
def test_delete_recreate_isolates_summary_lifecycle(tmp_path, kind):
    store, get = asset(tmp_path, kind)
    store.record_run_summary('sample', 'failed', 1, '001', blocking=True, run_id='reused')
    spool.append(store.owner_home, kind, 'sample',
                 dict(run_id='old-pending', run_at='003', status='failed', duration_ms=9))
    if kind == 'script':
        assert store.delete_script('sample')
        store.save_script(AssetDescriptor('sample', kind, '新生命周期'), 'print(1)')
    else:
        assert store.delete_workflow('sample')
        store.save_workflow(WorkflowDefinition(AssetDescriptor('sample', kind, '新生命周期'), ()))
    assert get()['run_count'] == 0
    store.record_run_summary('sample', 'succeeded', 2, '002', blocking=False, run_id='reused')
    assert get()['run_count'] == 1
    assert get()['last_status'] == 'succeeded'
    assert get()['last_duration_ms'] == 2


# LLM: 旧完成先拿到源快照，再由Event安排删除和重建，不能把旧快照的结果写进新生命周期。
# 函数用途: 确定性交错验证两种资产的并发旧完成不会污染同ID新资产。
@pytest.mark.timeout(60)
@pytest.mark.parametrize('kind', ['script', 'workflow'])
def test_captured_old_completion_stays_in_old_lifecycle(tmp_path, monkeypatch, kind):
    store, get = asset(tmp_path, kind)
    entered, release = threading.Event(), threading.Event()
    original = store._summary_source

    def captured(identifier):
        selected = original(identifier)
        entered.set()
        assert release.wait(5)
        return selected

    with ThreadPoolExecutor(1) as pool:
        monkeypatch.setattr(store, '_summary_source', captured)
        old = pool.submit(store.record_run_summary, 'sample', 'failed', 99, '999',
                          blocking=False, run_id='old-completion')
        try:
            assert entered.wait(2)
            fresh = type(store)(store.owner_home)
            if kind == 'script':
                assert fresh.delete_script('sample')
                fresh.save_script(AssetDescriptor('sample', kind, '新生命周期'), 'print(1)')
            else:
                assert fresh.delete_workflow('sample')
                fresh.save_workflow(WorkflowDefinition(AssetDescriptor('sample', kind, '新生命周期'), ()))
            fresh.record_run_summary('sample', 'succeeded', 2, '002', blocking=False, run_id='fresh')
        finally:
            release.set()
        old.result(timeout=3)
    assert get()['run_count'] == 1
    assert get()['last_status'] == 'succeeded'
    assert get()['last_duration_ms'] == 2


# LLM: 强制能力分支时仍使用仓库真实portable读取，不把snapshot重试降级成跟随路径。
# 函数用途: 核对两种资产在无dir_fd能力时均能取得安全摘要来源快照。
@pytest.mark.timeout(60)
@pytest.mark.parametrize('kind', ['script', 'workflow'])
def test_summary_snapshot_portable_branch(tmp_path, monkeypatch, kind, summary_transaction_layer):
    from agent_py_agent.agent.assets import script_store, workflow_store
    from agent_py_agent.agent.common import nofollow_fs
    from agent_py_agent.agent.workflows import run_list_index
    store, get = asset(tmp_path, kind)
    module = script_store if kind == 'script' else workflow_store
    connections = []
    connect = run_list_index.sqlite3.connect

    def counted_connect(*args, **kwargs):
        connections.append(True)
        return connect(*args, **kwargs)

    # 此合同验证portable只读快照，不把无dirfd能力借给后续strict私有SQLite写事务。
    with monkeypatch.context() as portable:
        portable.setattr(nofollow_fs, '_supports_dir_fd', lambda: False)
        portable.setattr(run_list_index, '_supports_dir_fd', lambda: False)
        portable.setattr(module, '_supports_dir_fd', lambda: False, raising=False)
        portable.setattr(run_list_index.sqlite3, 'connect', counted_connect)
        selected = store._summary_snapshot('sample')
        assert not connections
    assert (selected['id'] if kind == 'script' else selected.descriptor.id) == 'sample'
    store.record_run_summary('sample', 'succeeded', 1, '001', blocking=False, run_id='portable')
    spool.append(store.owner_home, kind, 'sample',
                 dict(run_id='legacy', run_at='002', status='failed', duration_ms=2))
    store.fold_run_summaries('sample')
    assert get()['run_count'] == 2
    assert get()['last_status'] == 'failed'
    assert list(spool_directory(store, kind).glob('*.json')) == []


# LLM: 使用正式runner、子进程、SQLite和ScriptsTool，不替换执行结果或摘要读口。
# 函数用途: 核对真实脚本执行后经scripts.get读到一次且仅一次的最新摘要。
@pytest.mark.timeout(60)
def test_real_script_summary_scripts_get_roundtrip(tmp_path):
    from agent_py_agent.agent.tooling.scripts_tool import ScriptsTool
    from agent_py_agent.agent.workflows.script_runner import ScriptRunner

    store, _ = asset(tmp_path, 'script')
    store.save_script(AssetDescriptor('sample', 'script', '真实摘要往返'),
                      'print(\'{"value": 7}\')')
    result = ScriptRunner(store.owner_home).run_script('sample')
    assert result.ok and result.exit_code == 0, result
    assert result.output_data == {'value': 7}
    outcome = ScriptsTool(store.owner_home).execute({'action': 'get', 'script_id': 'sample'})
    assert outcome.ok, outcome
    public = json.loads(outcome.output)
    assert public['runtime_summary']['run_count'] == 1
    assert public['runtime_summary']['last_status'] == 'succeeded'
    assert public['runtime_summary']['last_duration_ms'] == result.duration_ms
    assert '_run_summary_generation' not in public['metadata']
    reopened = ScriptsTool(store.owner_home).execute({'action': 'get', 'script_id': 'sample'})
    assert reopened.ok
    assert json.loads(reopened.output)['runtime_summary'] == public['runtime_summary']


# LLM: 已同步的定义读回与get形状一致；并发新聚合不属于本次提交快照，内部代次仍只留在规范定义。
# 函数用途: 验证创建、同步完成后再编辑的公开形状一致，且保存不抹去已持久化摘要。
@pytest.mark.timeout(60)
def test_script_save_readback_matches_public_get(tmp_path):
    store, get = asset(tmp_path, 'script')
    saved = store.save_script(AssetDescriptor('sample', 'script', '初次读回'), 'print(1)')
    assert saved == store.get_script('sample')
    assert '_run_summary_generation' not in saved['metadata']
    generation = summary_metadata(store, 'script')['_run_summary_generation']
    store.record_run_summary('sample', 'succeeded', 3, '001', blocking=True, run_id='completed')
    edited = store.save_script(AssetDescriptor('sample', 'script', '完成后编辑'), 'print(2)')
    assert edited == store.get_script('sample')
    assert edited['runtime_summary']['run_count'] == get()['run_count'] == 1
    assert summary_metadata(store, 'script')['_run_summary_generation'] == generation
