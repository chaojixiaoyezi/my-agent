"""长执行独占与只读原子观察分离：读取不阻塞、控制不伪受理、同run不重入。"""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import pytest

from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.models import StepExecutionRecord, WorkflowRunRecord
from agent_py_agent.agent.workflows.run_list_index import (
    RunListIndex,
    WorkflowRunIndexRecoveryPending,
)
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore, _RunBusy


def store_with_run(home):
    home.mkdir(mode=0o700, exist_ok=True)
    store = WorkflowRunStore(home)
    record = WorkflowRunRecord('slow-run', 'slow-workflow', 'owner', 'manual', 'running', time.time(), 0, 0,
        steps=(StepExecutionRecord('one', 'one', 'llm_skill_analysis', 'running', 0, 0, 0, False),))
    store.create_run(record, {'descriptor': {'id': 'slow-workflow'}, 'steps': []})
    return store, record


def hold_execution(store, entered, release):
    with store.execution_lock('slow-run'):
        entered.set()
        assert release.wait(5), '测试必须释放原执行锁'


def test_live_execution_readers_return_and_controls_never_create_second_body(tmp_path):
    store, record = store_with_run(tmp_path / 'home')
    entered, release = Event(), Event()
    before = (store.run_dir('slow-run') / 'run.json').read_bytes()
    with ThreadPoolExecutor(max_workers=4) as pool:
        body = pool.submit(hold_execution, store, entered, release)
        assert entered.wait(2)
        try:
            assert pool.submit(store.load_run, 'slow-run').result(timeout=1).status == 'running'
            assert pool.submit(store.list_runs).result(timeout=1)[0].workflow_run_id == 'slow-run'
            assert pool.submit(store.load_workflow_snapshot, 'slow-run').result(timeout=1)['descriptor']['id'] == 'slow-workflow'
            future = pool.submit(WorkflowEngine(store.owner_home).cancel_workflow_run,
                'slow-run', expected_revision=record.revision, operation_key='cancel-while-executing')
            with pytest.raises(_RunBusy) as caught:
                future.result(timeout=1)
            assert isinstance(caught.value, (HostAPIError, RuntimeError, ValueError))
            assert caught.value.code == 'WORKFLOW_RUN_BUSY' and caught.value.outcome == 'not_started'
            # 原执行锁保持唯一执行体；新恢复/执行不能借只读快照取得运行权。
            with pytest.raises(_RunBusy):
                with store.execution_lock('slow-run'):
                    pytest.fail('不应启动第二个执行体')
            assert (store.run_dir('slow-run') / 'run.json').read_bytes() == before
        finally:
            release.set()
            body.result(timeout=2)
    finished = replace(store.load_run('slow-run'), status='succeeded', ended_at=time.time())
    store.save_run(finished)
    with pytest.raises(ValueError, match='WORKFLOW_RUN_TERMINAL|WORKFLOW_RUN_REVISION_CONFLICT'):
        store.save_run(replace(record, status='running'))
    assert store.load_run('slow-run').status == 'succeeded'


def test_legacy_backfill_reads_active_run_without_waiting_for_execution(tmp_path):
    store, _ = store_with_run(tmp_path / 'home')
    index = RunListIndex(store.owner_home)
    with index.connection() as conn:
        conn.execute('DELETE FROM runs WHERE run_id=?', ('slow-run',))
        conn.execute('INSERT OR IGNORE INTO legacy(run_id) VALUES(?)', ('slow-run',))
    entered, release = Event(), Event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        body = pool.submit(hold_execution, store, entered, release)
        assert entered.wait(2)
        try:
            assert pool.submit(store.list_runs).result(timeout=1)[0].workflow_run_id == 'slow-run'
        finally:
            release.set()
            body.result(timeout=2)


def test_read_revision_races_are_bounded_and_fall_back_to_complete_run_row(tmp_path, monkeypatch):
    store, _ = store_with_run(tmp_path / 'home')
    original = store._read
    calls = []

    def changing(run_id, name, **options):
        value = original(run_id, name, **options)
        calls.append(name)
        value['revision'] = len(calls)
        return value

    monkeypatch.setattr(store, '_read', changing)
    observed = store.load_run('slow-run')
    assert observed.revision == 6 and len(calls) == 6
    assert observed.status == 'running'


def test_step_recovery_stays_readonly_and_identity_failure_is_not_hidden(tmp_path):
    store, _ = store_with_run(tmp_path / 'home')
    folder = store.run_dir('slow-run')
    before = (folder / 'run.json').read_bytes()
    step = replace(store.load_run('slow-run').steps[0], status='succeeded', output_snapshot={'value': 1})
    (folder / 'steps/one.json').write_text(json.dumps(step.to_dict()))
    assert store.load_run('slow-run').steps[0].output_snapshot == {'value': 1}
    assert (folder / 'run.json').read_bytes() == before
    raw = step.to_dict()
    raw['step_id'] = 'different'
    (folder / 'steps/one.json').write_text(json.dumps(raw))
    with pytest.raises(ValueError, match='步骤身份'):
        store.load_run('slow-run')


def test_busy_pending_index_never_acks_active_writer_or_blocks_readonly_snapshot(tmp_path):
    store, _ = store_with_run(tmp_path / 'home')
    index = RunListIndex(store.owner_home)
    index.mark_pending('slow-run')
    entered, release = Event(), Event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        body = pool.submit(hold_execution, store, entered, release)
        assert entered.wait(2)
        try:
            future = pool.submit(store.list_runs)
            with pytest.raises(WorkflowRunIndexRecoveryPending) as caught:
                future.result(timeout=1)
            assert caught.value.details['busy_rows'] == 1
            assert index.pending_ids(1) == ['slow-run']
            assert store.load_run('slow-run').status == 'running'
        finally:
            release.set()
            body.result(timeout=2)
    assert store.list_runs()[0].workflow_run_id == 'slow-run'
    assert index.pending_ids(1) == []
