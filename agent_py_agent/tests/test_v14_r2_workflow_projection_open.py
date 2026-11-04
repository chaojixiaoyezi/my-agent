"""V14F-06：工作流运行私有投影的打开不受同目录写者影响，投影失败不阻塞也不否认 run.json 规范提交。"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent.workflows import run_list_index, run_store, run_summary_index
from agent_py_agent.agent.workflows.models import WorkflowRunRecord
from agent_py_agent.agent.workflows.run_list_index import RunListIndex, index_connection
from agent_py_agent.agent.workflows.run_summary_index import RunSummaryIndex


# LLM: 同一进程同一观察点比较描述符数；连接、目录FD与锁FD都必须词法关闭。
# 函数用途: 返回当前进程打开的描述符数量。
def _fd_count():
    return len(os.listdir('/proc/self/fd' if Path('/proc/self/fd').is_dir() else '/dev/fd'))


# LLM: 只包住真实投影连接入口，记录异常后原样抛出；不替换存储行为，也不伪造任何记录。
# 函数用途: 让被 commit_run 吞掉的投影错误在测试中可见。
def _observe_projection(monkeypatch, failures):
    for module in (run_list_index, run_summary_index):
        real = module.index_connection

        @contextmanager
        def observed(owner_home, filename, real=real):
            try:
                with real(owner_home, filename) as conn:
                    yield conn
            except Exception as exc:
                failures.append(f'{filename}: {exc!r}')
                raise

        monkeypatch.setattr(module, 'index_connection', observed)


# LLM: 真实 DTO 的最小合法运行，状态推进全部经 WorkflowRunStore。
# 函数用途: 构造一个排队中的运行记录。
def _record(run_id, started_at=1):
    return WorkflowRunRecord(run_id, 'flow', 'alice', 'manual', 'queued', started_at, 0, 0)


# LLM: 两线程分别推进同 Owner 的真实 run.json 并反复打开摘要投影，同时在 data/ 增删兄弟文件，至少持续 2 秒。
# 函数用途: 复现祖先 ctime 比较把正常并发误判为路径替换，并约束规范写永不因投影失败。
def test_sibling_churn_never_reports_replacement_or_fails_canonical_writes(tmp_path, monkeypatch):
    home = tmp_path / 'owner'
    home.mkdir()
    store = run_store.WorkflowRunStore(home)
    summary = RunSummaryIndex(home)
    with summary.connection():
        pass
    failures, projection, written, cycles = [], [], [], []
    _observe_projection(monkeypatch, projection)
    before = _fd_count()
    started = time.monotonic()
    deadline = started + 2.0

    def transitions():
        number = 0
        while time.monotonic() < deadline:
            record = _record(f'run-{number:05d}', number + 1)
            try:
                store.create_run(record)
                running = replace(record, status='running')
                store.save_run(running)
                store.save_run(replace(running, status='succeeded'))
            except Exception as exc:
                failures.append(f'canonical {record.workflow_run_id}: {exc!r}')
                return
            written.append(record.workflow_run_id)
            number += 1

    def churn():
        number = 0
        while time.monotonic() < deadline:
            sibling = home / 'data' / f'sibling-{number % 4}.tmp'
            try:
                sibling.write_bytes(b'sibling')
                with summary.connection() as conn:
                    conn.execute('SELECT count(*) FROM summary_assets').fetchone()
                sibling.unlink()
            except Exception as exc:
                failures.append(f'summary: {exc!r}')
                return
            cycles.append(number)
            number += 1

    threads = [threading.Thread(target=transitions), threading.Thread(target=churn)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads)
    assert time.monotonic() - started >= 2.0
    assert failures == []
    assert projection == []
    assert len(written) >= 3 and len(cycles) >= 3
    assert _fd_count() == before
    runs = store.list_runs(status='succeeded', limit=len(written))
    assert sorted(run.workflow_run_id for run in runs) == sorted(written)


# LLM: 损坏投影只影响候选页；save_run 仍写 run.json，下一次列表读从规范运行目录重建并返回全部运行。
# 函数用途: 验证非忙碌 SQLite 错误使投影失效重建，而不是让每次规范写永久失败。
def test_corrupt_projection_never_blocks_save_and_next_list_rebuilds(tmp_path):
    store = run_store.WorkflowRunStore(tmp_path)
    records = [_record(f'corrupt-{number}', number + 1) for number in range(3)]
    for record in records:
        store.create_run(record)
    projection = tmp_path / 'data' / 'workflow_run_index.sqlite3'
    projection.write_bytes(b'damaged projection page ' * 64)
    before = _fd_count()
    running = replace(records[1], status='running')
    store.save_run(running)
    assert running.revision == 2
    assert store.load_run('corrupt-1').status == 'running'
    assert sorted(run.workflow_run_id for run in store.list_runs()) == ['corrupt-0', 'corrupt-1', 'corrupt-2']
    assert [run.workflow_run_id for run in store.list_runs(status='running')] == ['corrupt-1']
    assert sorted(run.workflow_run_id for run in store.list_runs(status='queued')) == ['corrupt-0', 'corrupt-2']
    with closing(sqlite3.connect(projection)) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 2
    assert _fd_count() == before


# LLM: 规范提交后一次模拟 I/O 错误只留下待发表行；save_run 正常返回，下一次读取重放并收敛到新状态。
# 函数用途: 验证提交后的投影失败不否认已经成功的规范写。
def test_publication_error_after_commit_returns_normally_and_converges(tmp_path, monkeypatch):
    store = run_store.WorkflowRunStore(tmp_path)
    record = _record('publish-run')
    store.create_run(record)
    publish = RunListIndex.publish_rows
    failed = []

    def flaky(index, rows):
        if not failed:
            failed.append(rows[0]['workflow_run_id'])
            raise OSError('simulated I/O error')
        return publish(index, rows)

    before = _fd_count()
    running = replace(record, status='running')
    with monkeypatch.context() as point:
        point.setattr(RunListIndex, 'publish_rows', flaky)
        store.save_run(running)
    assert failed == ['publish-run'] and running.revision == 2
    assert store.load_run('publish-run').status == 'running'
    assert RunListIndex(tmp_path).pending_ids() == ['publish-run']
    assert [run.workflow_run_id for run in store.list_runs(status='running')] == ['publish-run']
    assert store.list_runs(status='queued') == []
    assert RunListIndex(tmp_path).pending_ids() == []
    assert _fd_count() == before


# LLM: 投影叶为符号链接、data 目录为符号链接或叶有两个硬链接时仍拒绝打开；外部库字节不变，规范写照常完成。
# 函数用途: 验证新的私有打开方式保留链接拒绝，失效清理也不触碰外部文件。
@pytest.mark.parametrize('link', ['symlink', 'parent_symlink', 'hardlink'])
def test_linked_projection_is_still_rejected(tmp_path, link):
    home = tmp_path / 'owner'
    foreign = tmp_path / 'foreign'
    foreign.mkdir()
    foreign_db = foreign / 'workflow_run_index.sqlite3'
    with closing(sqlite3.connect(foreign_db)) as conn, conn:
        conn.execute('CREATE TABLE private(value)')
        conn.execute("INSERT INTO private VALUES('untouched')")
    foreign_bytes = foreign_db.read_bytes()
    home.mkdir()
    if link == 'parent_symlink':
        (home / 'data').symlink_to(foreign, target_is_directory=True)
    else:
        (home / 'data').mkdir()
        target = home / 'data' / 'workflow_run_index.sqlite3'
        if link == 'symlink':
            target.symlink_to(foreign_db)
        else:
            os.link(foreign_db, target)
    before = _fd_count()
    with pytest.raises(ValueError):
        with index_connection(home, 'workflow_run_index.sqlite3'):
            pytest.fail('linked projection was exposed')
    assert _fd_count() == before
    if link != 'parent_symlink':
        store = run_store.WorkflowRunStore(home)
        store.create_run(_record('linked-run'))
        assert store.load_run('linked-run').revision == 1
        assert _fd_count() == before
    assert foreign_db.read_bytes() == foreign_bytes
