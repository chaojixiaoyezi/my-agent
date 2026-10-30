# LLM: 仅在pytest临时SQLite与正文目录测试在线FTS，不以LIKE兜底、Mock提交次数或编译结果替代真实索引证据。
# 模块用途: 钉住空间预检、门外正文读取、旧active保留及分批恢复。
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.local_storage import maintenance
from agent_py_agent.agent.local_storage.store import LocalStore


# LLM: 构造真实FTS和三份规范正文，样本写入发生在观测维护过程之前。
# 函数用途: 为每个测试准备可直接MATCH命中的原active索引。
def seeded(tmp_path):
    store = LocalStore(tmp_path / 'local.sqlite3')
    assert store.fts_available
    for index in range(3):
        store.upsert_record(source_type='memory', source_id=f'm-{index}',
                            title=f'title-{index}', content=f'original needle payload {index}')
    return store


# LLM: 用真实DB检查变更前后事实，只有可用空间返回值被固定为零。
# 函数用途: 预检失败必须先于清表、shadow建立或pending清除。
def test_fts_low_space_preserves_active_and_pending(tmp_path, monkeypatch):
    store = seeded(tmp_path)
    with store._connection() as conn:
        before = [tuple(item) for item in conn.execute('SELECT * FROM records_fts ORDER BY id')]
        conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('records_fts_rebuild_pending','true')")
        conn.commit()
    monkeypatch.setattr(maintenance.os, 'statvfs', lambda *_: SimpleNamespace(f_bavail=0, f_frsize=4096))
    with pytest.raises(sqlite3.OperationalError, match='space'):
        store.rebuild_fts()
    with store._connection() as conn:
        assert [tuple(item) for item in conn.execute('SELECT * FROM records_fts ORDER BY id')] == before
        assert conn.execute("SELECT value FROM metadata WHERE key='records_fts_rebuild_pending'").fetchone()[0] == 'true'


# LLM: 正文读取时尝试另一真实连接的零等待写锁；旧BEGIN包住全量正文读取会被客观检出。
# 函数用途: 证明准备正文发生在短写事务之外，而不是靠SQL语句数推测锁已缩短。
def test_fts_body_reads_outside_sqlite_write_transaction(tmp_path, monkeypatch):
    store = seeded(tmp_path)
    original = store._read_content
    observations = []

    # LLM: 只申请并立即回滚无DML事务，不改记录、事件或正文。
    # 函数用途: 观测被测维护器是否霸占SQLite写锁。
    def observed(item):
        connection = sqlite3.connect(store.db_path, timeout=0)
        try:
            try:
                connection.execute('BEGIN IMMEDIATE')
                observations.append(True)
            except sqlite3.OperationalError:
                observations.append(False)
            finally:
                connection.rollback()
        finally:
            connection.close()
        return original(item)

    monkeypatch.setattr(store, '_read_content', observed)
    assert store.rebuild_fts() == 3
    assert observations and all(observations)


# LLM: trace来自真实SQLite执行；维护不能先清唯一active，且普通重建不可执行VACUUM。
# 函数用途: 守住影子发布入口，不允许旧整表DELETE继续藏在同步包装中。
def test_fts_does_not_clear_active_or_vacuum(tmp_path, monkeypatch):
    store = seeded(tmp_path)
    original = store._connection
    statements = []

    # LLM: 复用真实连接生命周期，仅安装SQLite官方trace回调，不替代SQL结果。
    # 函数用途: 捕获实际维护SQL以排除清空active和全库VACUUM。
    @contextmanager
    def observed():
        with original() as connection:
            connection.set_trace_callback(statements.append)
            yield connection

    monkeypatch.setattr(store, '_connection', observed)
    assert store.rebuild_fts() == 3
    normalized = [item.strip().upper() for item in statements]
    assert 'DELETE FROM RECORDS_FTS' not in normalized
    assert not any(item.startswith('VACUUM') for item in normalized)


# LLM: 显式step每拍一行，持久job可由新实例续用；构造器若自动恢复完成，也必须沿同job而非丢弃进度。
# 函数用途: 真实检查批次之间旧索引仍能查、恢复身份不变且最终新active完整。
def test_fts_step_keeps_active_and_resumes_job(tmp_path):
    store = seeded(tmp_path)
    step = getattr(store, 'rebuild_fts_step', None)
    assert callable(step), 'bounded resumable FTS maintenance step is missing'
    report = step(max_rows=1, max_bytes=1048576, max_ms=500)
    assert report['status'] == 'partial' and report['copied'] == 1
    job_id = report['job_id']
    assert len(store._search_fts('needle', limit=10, source_type=None, visibility=None)) == 3
    with store._connection() as conn:
        assert conn.execute("SELECT value FROM metadata WHERE key='records_fts_rebuild_pending'").fetchone()[0] == 'true'
    reopened = LocalStore(store.db_path)
    for _ in range(20):
        report = reopened.rebuild_fts_step(max_rows=1, max_bytes=1048576, max_ms=500)
        assert report['job_id'] == job_id
        if report['status'] == 'complete':
            break
    assert report['status'] == 'complete' and report['copied'] == 3
    assert len(reopened._search_fts('needle', limit=10, source_type=None, visibility=None)) == 3


# LLM: 旧无参准备钩子属于一次同步重建；后续每批仍必须经过真实全字段和正文验证。
# 函数用途: 保留同步准备钩子一次调用合同，同时防止为兼容而跳过批次CAS。
def test_fts_sync_prepares_once_but_validates_every_batch(tmp_path, monkeypatch):
    store = seeded(tmp_path)
    original_prepare = store._prepare_fts_rebuild
    original_validate = store._validate_fts_rebuild
    prepared, validated = [], []

    # LLM: 不替换快照内容，只统计旧准备入口的真实调用。
    # 函数用途: 观察同步入口的一次性准备钩子。
    def prepare():
        prepared.append(True)
        return original_prepare()

    # LLM: 所有实际验证继续执行，不允许重建驱动用跳过CAS换取兼容。
    # 函数用途: 观察每个真实提交批次的验证类型。
    def validate(conn, snapshot):
        validated.append(snapshot.mode)
        return original_validate(conn, snapshot)

    monkeypatch.setattr(store, '_prepare_fts_rebuild', prepare)
    monkeypatch.setattr(store, '_validate_fts_rebuild', validate)
    assert store.rebuild_fts() == 3
    assert len(prepared) == 1
    assert {'copy', 'delta', 'verify'} <= set(validated)


# LLM: FTS的id列为UNINDEXED；按id逐条删除会把影子复制变成二次方扫描。
# 函数用途: 用真实SQL trace检查每条影子写入按rowid删除，而非扫描全部文档。
def test_fts_shadow_replaces_use_indexed_rowid(tmp_path, monkeypatch):
    store = seeded(tmp_path)
    original = store._connection
    statements = []

    # LLM: 保持真实连接和SQL，只安装SQLite trace旁听。
    # 函数用途: 捕获影子维护实际执行语句。
    @contextmanager
    def observed():
        with original() as connection:
            connection.set_trace_callback(statements.append)
            yield connection

    monkeypatch.setattr(store, '_connection', observed)
    assert store.rebuild_fts() == 3
    deletes = [' '.join(sql.upper().split()) for sql in statements
               if sql.lstrip().upper().startswith('DELETE FROM MAINTENANCE_FTS_SHADOW ')]
    assert deletes
    assert all('WHERE ID =' not in sql for sql in deletes)


# LLM: 全部样本均在临时目录，真实SQLite及原正文writer建立active，不使用LIKE代替FTS。
# 函数用途: 准备独立的FTS修订回归样本。
def _fts_revision_store(tmp_path, count=5):
    store = LocalStore(tmp_path / 'index.db')
    assert store.fts_available
    for index in range(count):
        store.upsert_record(source_type='memory', source_id=str(index), record_id=f'revision-{index}',
                            title=f'title-{index}', content=f'oldneedle{index} original body')
    return store


# LLM: 只查询真实持久单槽任务，不从实例内存推断完成。
# 函数用途: 取得当前FTS任务状态。
def _fts_revision_job(store):
    with store._connection() as conn:
        return dict(conn.execute('SELECT * FROM maintenance_fts_job').fetchone())


# LLM: 直接MATCH当前active，不允许LIKE或preview兜底掩盖未回填。
# 函数用途: 返回指定词的实际FTS身份集合。
def _fts_revision_match(store, term):
    with store._connection() as conn:
        return {row[0] for row in conn.execute(
            'SELECT id FROM records_fts WHERE records_fts MATCH ?', (f'"{term}"',))}


# LLM: 另一线程真实占旁锁，构造器应保留旧active和同job而非因维护竞争失败。
# 函数用途: 复现在线维护锁竞争期间正常reopen。
def test_fts_revision_busy_maintenance_lock_preserves_reopen(tmp_path):
    import threading

    from agent_py_agent.agent.common.json_io import locked_json_path

    store = _fts_revision_store(tmp_path)
    assert store.rebuild_fts_step(max_rows=1, max_ms=1000, start_new=True)['status'] == 'partial'
    before = _fts_revision_job(store)
    ready, release, errors = threading.Event(), threading.Event(), []

    # LLM: 仅持维护旁锁，不占SQLite写锁，不更改任务。
    # 函数用途: 制造真实跨线程维护竞争窗口。
    def blocker():
        try:
            with locked_json_path(store.db_path.with_name(store.db_path.name + '.fts-rebuild')):
                ready.set()
                assert release.wait(10)
        except BaseException as exc:
            errors.append(exc)
            ready.set()

    thread = threading.Thread(target=blocker)
    thread.start()
    try:
        assert ready.wait(10) and not errors
        reopened = LocalStore(store.db_path)
        assert reopened._fts_needs_rebuild
        assert _fts_revision_match(reopened, 'oldneedle0') == {'revision-0'}
        assert _fts_revision_job(reopened) == before
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive() and not errors


# LLM: 非锁获取处的忙异常必须原样传播，不能被友好延期吞掉。
# 函数用途: 保留prepare、validate及replace叶的原故障注入合同。
@pytest.mark.parametrize('leaf', ['_prepare_fts_rebuild', '_validate_fts_rebuild', '_replace_fts_row'])
def test_fts_revision_non_lock_blocking_errors_are_not_wrapped(tmp_path, monkeypatch, leaf):
    store = _fts_revision_store(tmp_path, count=2)
    injected = BlockingIOError(f'injected {leaf}')

    # LLM: 只替换精确原叶，异常对象本身是检查目标。
    # 函数用途: 在获得维护锁后注入真实异常类别。
    def fail(*args, **kwargs):
        raise injected

    monkeypatch.setattr(store, leaf, fail)
    with pytest.raises(BlockingIOError) as caught:
        store.rebuild_fts()
    assert caught.value is injected
    assert _fts_revision_match(store, 'oldneedle0') == {'revision-0'}


# LLM: 旧active故意使用不同rowid并有重复ID；退休映射不能取新manifest的rid。
# 函数用途: 检查真实旧FTS映射与UPDATE、DELETE和REPLACE的有索引擦除。
def test_fts_revision_retired_map_uses_actual_old_fts_rowids(tmp_path):
    store = _fts_revision_store(tmp_path)
    with store._connection() as conn:
        rows = [tuple(row) for row in conn.execute('SELECT rowid,id,title,content FROM records_fts')]
        for rid, identity, title, content in rows:
            conn.execute('DELETE FROM records_fts WHERE rowid=?', (rid,))
            conn.execute('INSERT INTO records_fts(rowid,id,title,content) VALUES(?,?,?,?)',
                         (10000 + rid, identity, title, content))
        conn.execute('INSERT INTO records_fts(rowid,id,title,content) VALUES(20000,?,?,?)',
                     ('revision-0', 'duplicate', 'duplicate old token'))
        expected = {tuple(row) for row in conn.execute('SELECT rowid,id FROM records_fts')}
        conn.commit()
    assert store.rebuild_fts() == 5
    with store._connection() as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='maintenance_fts_retired_map'").fetchone(), 'retirement needs an indexed old-rowid map'
        mapped = {tuple(row) for row in conn.execute('SELECT fts_rid,record_id FROM maintenance_fts_retired_map')}
        assert mapped == expected
        sql = [row[0] for row in conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name IN ('maintenance_fts_ri','maintenance_fts_ru','maintenance_fts_rd')")]
        assert len(sql) == 3
        assert all('DELETE FROM maintenance_fts_retired WHERE rowid IN' in item for item in sql)
        plan = conn.execute('EXPLAIN QUERY PLAN SELECT fts_rid FROM maintenance_fts_retired_map INDEXED BY maintenance_fts_retired_map_id WHERE job_id=? AND epoch=? AND record_id IN (SELECT ?)',
                            (_fts_revision_job(store)['job_id'], _fts_revision_job(store)['verified_epoch'], 'absent')).fetchall()
        assert 'SEARCH' in ' '.join(row[3] for row in plan).upper()
    store.upsert_record(source_type='memory', source_id='0', record_id='revision-0',
                        title='changed', content='replacement body')
    assert store.delete_record('revision-1')
    with store._connection() as conn:
        conn.execute("UPDATE records SET id='revision-renamed' WHERE id='revision-2'")
        rid = conn.execute("SELECT rowid FROM records WHERE id='revision-3'").fetchone()[0]
        cursor = conn.execute("SELECT * FROM records WHERE id='revision-4'")
        columns, values = [column[0] for column in cursor.description], tuple(cursor.fetchone())
        conn.execute('INSERT OR REPLACE INTO records(rowid,' + ','.join(columns) + ') VALUES('
                     + ','.join('?' for _ in range(len(columns) + 1)) + ')', (rid, *values))
        conn.commit()
        assert conn.execute('SELECT COUNT(*) FROM maintenance_fts_retired').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM maintenance_fts_retired_map').fetchone()[0] == 0


# LLM: 单次业务写无退休命中时，SQLite工作不能随无关退休文档规模线性增长。
# 函数用途: 用真实SQLite指令数检出退休触发器对UNINDEXED列全扫。
def test_fts_revision_retired_write_work_does_not_scale_with_unrelated_rows(tmp_path):
    counts = []
    for size in (16, 4096):
        store = _fts_revision_store(tmp_path / str(size), count=1)
        with store._connection() as conn:
            conn.executemany('INSERT INTO records_fts(rowid,id,title,content) VALUES(?,?,?,?)',
                ((10000 + i, f'orphan-{i}', 'orphan', 'old orphan text') for i in range(size)))
            conn.commit()
        assert store.rebuild_fts() == 1
        with store._connection() as conn:
            conn.execute("UPDATE records SET visibility=visibility WHERE id='revision-0'")
            conn.commit()
            ticks = [0]

            # LLM: 只统计真实SQLite虚拟机工作，不以耗时猜扫描成本。
            # 函数用途: 计数一次实际业务更新的指令。
            def progress():
                ticks[0] += 1
                return 0

            conn.set_progress_handler(progress, 1)
            try:
                conn.execute("UPDATE records SET visibility=visibility WHERE id='revision-0'")
                conn.commit()
            finally:
                conn.set_progress_handler(None, 0)
            counts.append(ticks[0])
    assert counts[1] < counts[0] * 3 + 200


# LLM: 清理退休副本的前置拍不能消耗同步prepare钩子；所有实际准备与CAS仍执行。
# 函数用途: 验证两次重建之间先退休时钩子恰好一次。
def test_fts_revision_sync_prepare_once_after_retiring(tmp_path, monkeypatch):
    store = _fts_revision_store(tmp_path, count=3)
    with store._connection() as conn:
        conn.executemany('INSERT INTO records_fts(rowid,id,title,content) VALUES(?,?,?,?)',
            ((10000 + i, f'orphan-{i}', 'orphan', 'old orphan text') for i in range(300)))
        conn.commit()
    assert store.rebuild_fts() == 3
    calls = {'hook': 0, 'prepare': 0, 'validate': 0}
    old_hook, old_prepare, old_validate = store._prepare_fts_rebuild, maintenance._fts_prepare, store._validate_fts_rebuild
    prepared = set()

    # LLM: 计数之后仍走原扩展钩子。
    # 函数用途: 观察原一次准备入口。
    def hook():
        calls['hook'] += 1
        return old_hook()

    # LLM: 快照来自实际内部准备，预算及来源检查不变。
    # 函数用途: 记录逐批真实准备。
    def prepare(current):
        calls['prepare'] += 1
        snapshot = old_prepare(current)
        prepared.add(id(snapshot))
        return snapshot

    # LLM: 原CAS必须真实运行，不能用mock成功替代。
    # 函数用途: 核对每拍验证消费了真实准备快照。
    def validate(conn, snapshot):
        calls['validate'] += 1
        assert id(snapshot) in prepared
        return old_validate(conn, snapshot)

    monkeypatch.setattr(store, '_prepare_fts_rebuild', hook)
    monkeypatch.setattr(maintenance, '_fts_prepare', prepare)
    monkeypatch.setattr(store, '_validate_fts_rebuild', validate)
    assert store.rebuild_fts() == 3
    assert calls['hook'] == 1
    assert calls['prepare'] >= calls['validate'] >= 3
    assert calls['prepare'] > calls['hook']


# LLM: 映射分批提交后再改SQL和正文，必须回退复核；重开应恢复同job而非换新任务。
# 函数用途: 验证旧active映射的epoch、目录fence及持久恢复。
def test_fts_revision_map_fences_rewind_and_resume_same_job(tmp_path):
    store = _fts_revision_store(tmp_path, count=8)
    store.rebuild_fts_step(max_rows=1, max_ms=1000, start_new=True)
    for _ in range(1000):
        job = _fts_revision_job(store)
        if job['phase'] == 'map' and job['verify_cursor'] is not None:
            break
        store.rebuild_fts_step(max_rows=1, max_ms=1000)
    else:
        raise AssertionError('did not reach partially committed map phase')
    store.upsert_record(source_type='memory', source_id='0', record_id='revision-0',
                        title='changed', content='newneedle0 changed body')
    path = store._content_file('revision-1')
    replacement = path.with_suffix('.replacement')
    replacement.write_text('bodyonlyneedle changed without SQL', encoding='utf-8')
    replacement.replace(path)
    with store._connection() as conn:
        expected = {tuple(row) for row in conn.execute('SELECT rowid,id FROM records_fts')}
    store.rebuild_fts_step(max_rows=1, max_ms=1000)
    rewound = _fts_revision_job(store)
    assert rewound['phase'] == 'delta' and rewound['epoch'] > job['epoch']
    reopened = LocalStore(store.db_path)
    completed = _fts_revision_job(reopened)
    assert completed['job_id'] == job['job_id'] and completed['phase'] == 'complete'
    assert not reopened._fts_needs_rebuild
    assert _fts_revision_match(reopened, 'newneedle0') == {'revision-0'}
    assert _fts_revision_match(reopened, 'bodyonlyneedle') == {'revision-1'}
    with reopened._connection() as conn:
        assert {tuple(row) for row in conn.execute('SELECT fts_rid,record_id FROM maintenance_fts_retired_map')} == expected
        assert {tuple(row) for row in conn.execute('SELECT DISTINCT job_id,epoch FROM maintenance_fts_retired_map')} == {(job['job_id'], completed['verified_epoch'])}


# LLM: 首行或中间行的合法4MiB正文不得令完整驱动永久饥饿；显式step仍受硬字节准入约束。
# 函数用途: 验证结构化预算拒绝、门外读取准入及同步或自动恢复的真实MATCH。
@pytest.mark.parametrize('position', ['first', 'middle'])
@pytest.mark.parametrize('driver', ['full', 'reopen'])
def test_fts_revision_large_document_full_and_automatic_resume(tmp_path, monkeypatch, position, driver):
    marker = ' deepbudgetneedle '
    body = 'x' * (4 * 1024 * 1024 - len(marker)) + marker
    store = LocalStore(tmp_path / 'index.db')
    order = ['large', 'small-a', 'small-b'] if position == 'first' else ['small-a', 'large', 'small-b']
    for name in order:
        store.upsert_record(source_type='memory', source_id=name, record_id=name,
                            title=name, content=body if name == 'large' else 'small original text')
    original_read, large_reads = store._read_content, []

    # LLM: 预算拒绝应在完整水合正文前发生，正式读取仍由原叶执行。
    # 函数用途: 统计大正文实际读取。
    def read(row):
        if row['id'] == 'large':
            large_reads.append(True)
        return original_read(row)

    monkeypatch.setattr(store, '_read_content', read)
    for index in range(4):
        try:
            store.rebuild_fts_step(max_rows=1, max_bytes=8 * 1024 * 1024,
                                   max_ms=1000, start_new=index == 0)
        except sqlite3.OperationalError as exc:
            assert getattr(exc, 'required_bytes', 0) > 8 * 1024 * 1024
            assert getattr(exc, 'limit_bytes', 0) == 8 * 1024 * 1024
            assert getattr(exc, 'stage', None) == 'body'
            break
    else:
        raise AssertionError('large body was not rejected by explicit hard budget')
    assert not large_reads
    before = _fts_revision_job(store)
    assert before['phase'] == 'copy'
    if driver == 'full':
        assert store.rebuild_fts() == 3
        completed_store = store
        assert large_reads
    else:
        completed_store = LocalStore(store.db_path)
    completed = _fts_revision_job(completed_store)
    assert completed['job_id'] == before['job_id'] and completed['phase'] == 'complete'
    assert completed['copied'] == 3 and not completed_store._fts_needs_rebuild
    assert _fts_revision_match(completed_store, 'deepbudgetneedle') == {'large'}


# LLM: SQL上界固定但首拍必须无下界；仅改真实records的rowid，不向影子手工补数据或用LIKE兜底。
# 函数用途: 用每拍一行与实际MATCH覆盖负rowid、零和正整数的完整复制及恢复身份。
def test_fts_r1_first_copy_has_no_lower_bound(tmp_path, monkeypatch):
    store = _fts_revision_store(tmp_path, count=3)
    with store._connection() as conn:
        for index, rid in enumerate((-5, 0, 1)):
            conn.execute('UPDATE records SET rowid=? WHERE id=?', (rid, f'revision-{index}'))
        conn.commit()
    original = store._prepare_fts_rebuild
    cursors = []

    # LLM: 观察原准备快照，不修改游标或行集合；None只能表示尚未开始，0是合法已提交续点。
    # 函数用途: 收集首拍及后续拍传入的持久游标和真实选中rowid。
    def prepare():
        snapshot = original()
        if snapshot.mode == 'copy':
            cursors.append((snapshot.job['cursor'], snapshot.selected))
        return snapshot

    monkeypatch.setattr(store, '_prepare_fts_rebuild', prepare)
    job_id = None
    for expected_rid in (-5, 0, 1):
        report = store.rebuild_fts_step(max_rows=1, max_ms=1000)
        job_id = job_id or report['job_id']
        assert report['job_id'] == job_id and report['status'] == 'partial'
        assert _fts_revision_job(store)['cursor'] == expected_rid
    assert cursors == [(None, (-5,)), (-5, (0,)), (0, (1,))]
    for _ in range(100):
        report = store.rebuild_fts_step(max_rows=1, max_ms=1000)
        assert report['job_id'] == job_id
        if report['status'] == 'complete':
            break
    assert report['status'] == 'complete' and report['copied'] == 3
    for index in range(3):
        assert _fts_revision_match(store, f'oldneedle{index}') == {f'revision-{index}'}
    with store._connection() as conn:
        assert {tuple(row) for row in conn.execute('SELECT rowid,id FROM records_fts')} == {
            (-5, 'revision-0'), (0, 'revision-1'), (1, 'revision-2')}


# LLM: 实际状态机以delta表示基线完成、以map表示发表前；不能伪造不存在的ready_to_swap状态。
# 函数用途: 只通过公开分批入口到达指定切换边界，失败时明确拒绝继续测试。
def _fts_r1_reach_boundary(store, phase):
    for _ in range(100):
        store.rebuild_fts_step(max_rows=1, max_ms=1000)
        job = _fts_revision_job(store)
        if job['phase'] == phase:
            return job
        assert job['phase'] != 'complete', 'crossed requested FTS boundary'
    raise AssertionError(f'did not reach {phase}')


# LLM: 正式writer与records触发器产生持久delta；覆盖插入、更新、删除，不手工构造队列或替换数据库结果。
# 函数用途: 在基线末拍或发表前制造真实业务变化，验证有界排队、同任务恢复及最终MATCH。
@pytest.mark.parametrize('phase', ['delta', 'map'])
@pytest.mark.parametrize('change', ['insert', 'update', 'delete'])
def test_fts_r1_boundary_deltas_survive_until_applied(tmp_path, phase, change):
    store = _fts_revision_store(tmp_path, count=3)
    before = _fts_r1_reach_boundary(store, phase)
    if change == 'delete':
        assert store.delete_record('revision-0')
    else:
        identity = 'inserted' if change == 'insert' else 'revision-0'
        source = 'new' if change == 'insert' else '0'
        store.upsert_record(source_type='memory', source_id=source, record_id=identity,
                            title='changed', content='boundarynewneedle changed body')
    with store._connection() as conn:
        queued = [tuple(row) for row in conn.execute('SELECT * FROM maintenance_fts_delta ORDER BY seq')]
    assert queued
    report = store.rebuild_fts_step(max_rows=1, max_ms=1000)
    assert report['job_id'] == before['job_id'] and report['status'] == 'partial'
    with store._connection() as conn:
        remaining = [tuple(row) for row in conn.execute('SELECT * FROM maintenance_fts_delta ORDER BY seq')]
        assert remaining == (queued if phase == 'map' else queued[1:])
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='maintenance_fts_shadow'").fetchone()
        assert conn.execute("SELECT value FROM metadata WHERE key='records_fts_rebuild_pending'").fetchone()[0] == 'true'
    reopened = LocalStore(store.db_path)
    completed = _fts_revision_job(reopened)
    assert completed['job_id'] == before['job_id'] and completed['phase'] == 'complete'
    assert completed['copied'] == (2 if change == 'delete' else 4 if change == 'insert' else 3)
    if change == 'delete':
        assert not _fts_revision_match(reopened, 'oldneedle0')
    else:
        assert _fts_revision_match(reopened, 'boundarynewneedle') == {identity}
        assert _fts_revision_match(reopened, 'oldneedle0') == ({'revision-0'} if change == 'insert' else set())
    assert _fts_revision_match(reopened, 'oldneedle1') == {'revision-1'}
    with reopened._connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM maintenance_fts_delta').fetchone()[0] == 0


# LLM: 字节拒绝发生在正文水合前，队列不清且active不换；较大预算重试仍必须消费同一持久任务。
# 函数用途: 验证发表边界出现大增量时不能用清空delta或忽略预算来宣布完成。
def test_fts_r1_delta_byte_budget_retains_queue_and_active(tmp_path):
    store = _fts_revision_store(tmp_path, count=3)
    before = _fts_r1_reach_boundary(store, 'map')
    store.upsert_record(source_type='memory', source_id='0', record_id='revision-0',
                        title='large change', content='budgetdeltaneedle ' * 4096)
    store.rebuild_fts_step(max_rows=1, max_ms=1000)
    with store._connection() as conn:
        queue = [tuple(row) for row in conn.execute('SELECT * FROM maintenance_fts_delta ORDER BY seq')]
        active = [tuple(row) for row in conn.execute('SELECT * FROM records_fts ORDER BY rowid')]
    job = _fts_revision_job(store)
    assert job['phase'] == 'delta' and queue
    with pytest.raises(maintenance._FtsByteBudget) as caught:
        store.rebuild_fts_step(max_rows=1, max_bytes=1024, max_ms=1000)
    assert caught.value.stage == 'body' and caught.value.required_bytes > 1024
    with store._connection() as conn:
        assert [tuple(row) for row in conn.execute('SELECT * FROM maintenance_fts_delta ORDER BY seq')] == queue
        assert [tuple(row) for row in conn.execute('SELECT * FROM records_fts ORDER BY rowid')] == active
    assert _fts_revision_job(store) == job
    reopened = LocalStore(store.db_path)
    assert _fts_revision_job(reopened)['job_id'] == before['job_id']
    assert _fts_revision_job(reopened)['phase'] == 'complete'
    assert _fts_revision_match(reopened, 'budgetdeltaneedle') == {'revision-0'}


# LLM: 准备后真实writer改变epoch必须保留原CAS失败，绝不能为自动排delta放宽旧快照验证。
# 函数用途: 在最终map准备与BEGIN之间写入，验证旧快照不发表、delta保留且同入口重试恢复。
def test_fts_r1_write_after_final_prepare_preserves_cas_and_delta(tmp_path, monkeypatch):
    store = _fts_revision_store(tmp_path, count=3)
    before = _fts_r1_reach_boundary(store, 'map')
    original = store._prepare_fts_rebuild
    injected = []

    # LLM: 只在实际最终批次后注入一次正式写入，返回原始快照让真实CAS判断变化。
    # 函数用途: 精确制造门外准备后、门内验证前的业务竞争。
    def prepare():
        snapshot = original()
        if snapshot.mode == 'map' and snapshot.finished and not injected:
            store.upsert_record(source_type='memory', source_id='0', record_id='revision-0',
                                title='concurrent', content='finalcasneedle concurrent body')
            injected.append(True)
        return snapshot

    monkeypatch.setattr(store, '_prepare_fts_rebuild', prepare)
    for _ in range(100):
        try:
            report = store.rebuild_fts_step(max_rows=1, max_ms=1000)
        except BlockingIOError as exc:
            assert str(exc) == 'local_store_rebuild_snapshot_changed'
            break
        assert report['status'] != 'complete'
    else:
        raise AssertionError('final snapshot CAS did not reject concurrent write')
    assert injected == [True]
    with store._connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM maintenance_fts_delta').fetchone()[0] > 0
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='maintenance_fts_shadow'").fetchone()
    assert _fts_revision_job(store)['phase'] == 'map'
    assert store.rebuild_fts() == 3
    assert _fts_revision_job(store)['job_id'] == before['job_id']
    assert _fts_revision_match(store, 'finalcasneedle') == {'revision-0'}


# LLM: 真实最终发表叶必须处于BEGIN IMMEDIATE内，另一连接不能插入窗口写；观测不替代原发表或故障注入。
# 函数用途: 在最终换名入口同时核对事务、空delta和真实SQLite写锁。
def test_fts_r1_publish_checks_delta_under_immediate_transaction(tmp_path, monkeypatch):
    store = _fts_revision_store(tmp_path, count=3)
    original = maintenance._fts_publish
    observed = []

    # LLM: 所有真实DDL、事件和完成标志仍由原叶执行；第二连接只尝试写锁并回滚，不改业务数据。
    # 函数用途: 旁听最终发表的准确观察点，不从启动或SQL日志推断原子性。
    def publish(current, conn, snapshot):
        assert current is store and conn.in_transaction
        assert conn.execute('SELECT COUNT(*) FROM maintenance_fts_delta').fetchone()[0] == 0
        other = sqlite3.connect(store.db_path, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                other.execute('BEGIN IMMEDIATE')
        finally:
            other.rollback()
            other.close()
        observed.append(True)
        return original(current, conn, snapshot)

    monkeypatch.setattr(maintenance, '_fts_publish', publish)
    assert store.rebuild_fts() == 3
    assert observed == [True]


# LLM: 只让原准备器的协作deadline过期，不伪造selected或SQL结果；时间耗尽不能确认任何尚未应用的delta。
# 函数用途: 验证发表前回退后遇到空时间预算时不清队列、不换表且可通过原入口重试。
def test_fts_r1_expired_delta_budget_keeps_persistent_progress(tmp_path, monkeypatch):
    store = _fts_revision_store(tmp_path, count=3)
    before = _fts_r1_reach_boundary(store, 'map')
    store.upsert_record(source_type='memory', source_id='0', record_id='revision-0',
                        title='late', content='timedeltaneedle changed body')
    store.rebuild_fts_step(max_rows=1, max_ms=1000)
    original = store._prepare_fts_rebuild
    job = _fts_revision_job(store)
    with store._connection() as conn:
        queue = [tuple(row) for row in conn.execute('SELECT * FROM maintenance_fts_delta ORDER BY seq')]
    assert job['phase'] == 'delta' and queue

    # LLM: 仍执行原准备与预算判据；ContextVar修改仅活于一次测试调用栈并在finally恢复。
    # 函数用途: 为真实delta准备制造确定的协作时间耗尽，而不依赖机器速度。
    def expired_prepare():
        rows, size, _deadline = maintenance._FTS_BATCH_LIMITS.get()
        token = maintenance._FTS_BATCH_LIMITS.set((rows, size, maintenance.time.monotonic() - 1))
        try:
            return original()
        finally:
            maintenance._FTS_BATCH_LIMITS.reset(token)

    with monkeypatch.context() as patch:
        patch.setattr(store, '_prepare_fts_rebuild', expired_prepare)
        result = store.rebuild_fts_step(max_rows=1, max_ms=1000)
    assert result['status'] == 'partial' and not result['progressed']
    assert _fts_revision_job(store) == job
    with store._connection() as conn:
        assert [tuple(row) for row in conn.execute('SELECT * FROM maintenance_fts_delta ORDER BY seq')] == queue
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='maintenance_fts_shadow'").fetchone()
    assert store.rebuild_fts() == 3
    assert _fts_revision_job(store)['job_id'] == before['job_id']
    assert _fts_revision_match(store, 'timedeltaneedle') == {'revision-0'}
