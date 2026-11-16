"""长历史线程只读投影和当前绑定枚举的行为回归。"""
from dataclasses import replace

from agent.conversation.models import ConversationThread
from agent.conversation.store_layout import ConversationStorage
from agent.conversation.store_threads import ThreadStore
from agent.conversation.thread_read_index import ThreadReadIndex


def store(tmp_path):
    return ThreadStore(ConversationStorage(tmp_path / 'conversations'))


def put(threads, name, stamp, workspace=''):
    row = ConversationThread(canonical_user_id="test", thread_id=name, updated_at=stamp, created_at=stamp, workspace_root=workspace)
    threads.write(row)
    return row


def test_writes_publish_reusable_index_and_recent_order(tmp_path, monkeypatch):
    threads = store(tmp_path)
    for value in range(25):
        put(threads, f'thread-{value:03}', value)
    # 显式穷举仍保留旧接口；热查询不允许落回目录glob。
    assert len(threads.list_report(limit=0)[0]) == 25
    from pathlib import Path
    monkeypatch.setattr(Path, 'glob', lambda *a, **k: (_ for _ in ()).throw(AssertionError('full scan')))
    rows, _ = threads.list_report(limit=3)
    assert [row.updated_at for row in rows] == [22, 23, 24]
    assert [row.thread_id for row in ThreadStore(threads.storage).list(limit=3)] == [row.thread_id for row in rows]


def test_workspace_filter_before_limit_and_atomic_update(tmp_path):
    threads = store(tmp_path)
    first = put(threads, 'thread-first', 1, str(tmp_path / 'one'))
    put(threads, 'thread-other', 99, str(tmp_path / 'two'))
    rows, _ = threads.list_report(limit=1, workspace_dir=str(tmp_path / 'one'))
    assert [row.thread_id for row in rows] == [first.thread_id]
    threads.update_atomic(first.thread_id, lambda row: replace(row, updated_at=100))
    assert threads.list(limit=1)[0].thread_id == first.thread_id


def test_deleted_and_corrupt_candidates_revalidate_canonical(tmp_path):
    threads = store(tmp_path)
    put(threads, 'thread-missing', 99)
    put(threads, 'thread-broken', 98)
    threads.storage.thread_path('thread-missing').unlink()
    threads.storage.thread_path('thread-broken').write_text('{bad')
    rows, errors = threads.list_report(limit=10)
    assert not rows
    assert any(error.get('thread_id') == 'thread-broken' for error in errors)


def test_legacy_directory_backfill_is_partial_then_converges(tmp_path):
    threads = store(tmp_path)
    for value in range(150):
        row = ConversationThread(canonical_user_id="test", thread_id=f'thread-{value:03}', updated_at=value)
        threads.storage.thread_path(row.thread_id).write_text(__import__('json').dumps(row.to_dict()))
    rows, errors = threads.list_report(limit=2)
    assert len(rows) <= 2
    assert any(error.get('error_code') == 'THREAD_INDEX_PARTIAL' for error in errors)
    index = ThreadReadIndex(threads)
    for _ in range(10):
        if index.maintain()['coverage'] == 'complete':
            break
    else:
        raise AssertionError('backfill did not converge')
    assert [row.updated_at for row in threads.list(limit=2)] == [148, 149]


def test_current_binding_catalog_does_not_load_isolated_history(tmp_path, monkeypatch):
    threads = store(tmp_path)
    put(threads, 'thread-bound', 1)
    for value in range(15):
        put(threads, f'thread-isolated-{value}', value)
    from agent.gateway_parts.io import write_json_file_atomic
    write_json_file_atomic(threads.storage.bindings_path, {'web\x1fchat\x1fuser': 'thread-bound'})
    reads = []
    original = threads.load_report
    def load(thread_id):
        reads.append(thread_id)
        return original(thread_id)
    monkeypatch.setattr(threads, 'load_report', load)
    rows, errors = threads.bound_threads_report()
    assert not errors
    assert [row.thread_id for row in rows] == ['thread-bound']
    assert reads == ['thread-bound']


def test_pruning_old_generation_is_bounded_and_resumable(tmp_path):
    threads = store(tmp_path)
    put(threads, 'thread-kept', 1)
    index = ThreadReadIndex(threads)
    with index._connection() as conn:
        for value in range(300):
            conn.execute('INSERT OR REPLACE INTO entries VALUES(?,?,?,?,?)',
                         (f'thread-stale-{value}.json', 0, '', 'missing', -1))
    index.maintain()
    with index._connection() as conn:
        remaining = conn.execute('SELECT COUNT(*) FROM entries WHERE generation=-1').fetchone()[0]
    assert remaining >= 172
    for _ in range(5):
        index.maintain()
    with index._connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM entries WHERE generation=-1').fetchone()[0] == 0


def test_lane_zero_budget_has_no_projection_side_effect(tmp_path):
    from types import SimpleNamespace

    from agent.conversation.thread_read_index import maintain_thread_index_lane
    from agent.user_space.storage_lanes import StorageLaneBudget, StorageLaneRequest

    request = StorageLaneRequest(kind='thread_index', home=SimpleNamespace(owner_home_dir=tmp_path),
        now=0, cursor={}, budget=StorageLaneBudget(0, 0, None), legal_hold=False,
        coordinator=False, pressure=None, commit=None)
    result = maintain_thread_index_lane(request)
    assert result['items'] == result['bytes'] == 0
    assert list(tmp_path.iterdir()) == []


def test_shared_budget_backfill_yields_without_losing_pending_names(tmp_path):
    from agent.conversation.thread_read_index import _LaneBudget
    from agent.user_space.storage_lanes import StorageLaneBudget

    threads = store(tmp_path)
    import json
    for value in range(9):
        row = ConversationThread(canonical_user_id='test', thread_id=f'thread-budget-{value}', updated_at=value)
        threads.storage.thread_path(row.thread_id).write_text(json.dumps(row.to_dict()))
    index = ThreadReadIndex(threads)
    for _ in range(30):
        budget = _LaneBudget(StorageLaneBudget(1, 1024 * 1024, None))
        outcome = index.maintain(budget=budget)
        assert budget.entries <= 1
        if outcome['coverage'] == 'complete':
            break
    else:
        raise AssertionError('single-entry shared budget did not converge')
    assert len(threads.list(limit=20)) == 9


def test_scheduler_point_proof_rereads_original_row_and_protects_unknown(tmp_path):
    from agent.scheduler.history_index import SchedulerHistoryIndex

    from agent_py_agent.agent.scheduler.repository import SchedulerRunFinish
    from agent_py_agent.tests.test_scheduler_repository import _claimed_due_run, _repository

    repository = _repository(tmp_path)
    claim = _claimed_due_run(repository)
    terminal = repository.finish_run(claim['run_id'], claim['claim_id'], SchedulerRunFinish('done', delivery_status='suppressed', now=1010))
    index = SchedulerHistoryIndex(repository.history_path, repository.owner, repository._parse_run, read_only=True)
    proof = index.terminal_run_proof(claim['run_id'], thread_id=claim['thread_id'], cutoff=2000)
    assert proof['status'] == 'settled'
    assert proof['source_generation'] >= 1
    assert proof['source_position'] > 0
    assert index.terminal_run_proof(claim['run_id'], thread_id='thread-other', cutoff=2000)['status'] == 'unknown'
    repository._history_index.append([{**terminal, 'status': 'unknown'}])
    assert index.terminal_run_proof(claim['run_id'], thread_id=claim['thread_id'], cutoff=2000)['status'] == 'unknown'


def test_scheduler_readonly_open_missing_index_does_not_create_directory(tmp_path):
    from agent.scheduler.history_index import SchedulerHistoryIndex

    source = tmp_path / 'absent' / 'history.jsonl'
    index = SchedulerHistoryIndex(source, {'id': 'owner'}, lambda raw, **kw: (raw, ''), read_only=True)
    proof = index.terminal_run_proof('srun_missing', thread_id='thread-missing', cutoff=2)
    assert proof['status'] == 'unknown'
    assert not source.parent.exists()


def test_scheduler_proof_rejects_corrupt_and_changed_canonical_source(tmp_path):
    from agent.scheduler.history_index import SchedulerHistoryIndex

    from agent_py_agent.agent.scheduler.repository import SchedulerRunFinish
    from agent_py_agent.tests.test_scheduler_repository import _claimed_due_run, _repository

    repository = _repository(tmp_path)
    claim = _claimed_due_run(repository)
    repository.finish_run(claim['run_id'], claim['claim_id'], SchedulerRunFinish('done', delivery_status='suppressed', now=1010))
    index = SchedulerHistoryIndex(repository.history_path, repository.owner, repository._parse_run, read_only=True)
    repository.history_path.write_text('{invalid}\n')
    proof = index.terminal_run_proof(claim['run_id'], thread_id=claim['thread_id'], cutoff=2000)
    assert proof['status'] == 'unknown'
    assert proof['reason'] == 'SCHEDULER_HISTORY_INDEX_INCOMPLETE'


def test_scheduled_isolated_proof_requires_exact_owner_and_run(tmp_path):
    from types import SimpleNamespace

    from agent.conversation.thread_read_index import scheduled_thread_terminal_proof

    from agent_py_agent.agent.scheduler.repository import (
        SchedulerJobCreateRequest,
        SchedulerRunFinish,
    )
    from agent_py_agent.tests.test_scheduler_repository import _every, _repository

    repository = _repository(tmp_path)
    repository.create_job(SchedulerJobCreateRequest(name='isolated', prompt='task', thread_id='thread-origin',
        source_request_id='request-isolated', schedule=_every(), execution={'mode': 'isolated'}, now=900))
    run = repository.reserve_due_runs(now=1000)[0]
    claim = repository.claim_run(run['run_id'], now=1001, lease_seconds=10)
    repository.finish_run(run['run_id'], claim['claim_id'], SchedulerRunFinish('done', delivery_status='suppressed', now=1010))
    home = SimpleNamespace(owner_id='u-1', owner_provider='feishu', owner_kind='user',
        owner_home_dir=repository.root.parent, owner_scheduler_history_jsonl=repository.history_path)
    thread = ConversationThread(canonical_user_id='test', thread_id=run['thread_id'],
        owner_id=home.owner_id, owner_home=str(home.owner_home_dir),
        metadata={'thread_kind': 'scheduled', 'scheduler_run_id': run['run_id']})
    import time

    from agent.memory_store.retention_scan_budget import RetentionScanBudget

    budget = RetentionScanBudget(10, 1048576, time.monotonic() + 10)
    proof = scheduled_thread_terminal_proof(home, thread, 2000, scan_budget=budget)
    assert proof['status'] == 'settled'
    assert proof['bytes_read'] == budget.size > 0
    assert scheduled_thread_terminal_proof(home, replace(thread, owner_id='other'), 2000)['status'] == 'unknown'


def test_scheduler_proof_accounts_snapshots_and_shares_byte_budget(tmp_path):
    import time

    from agent.memory_store.retention_scan_budget import RetentionScanBudget
    from agent.scheduler.history_index import SchedulerHistoryIndex

    from agent_py_agent.agent.scheduler.repository import SchedulerRunFinish
    from agent_py_agent.tests.test_scheduler_repository import _claimed_due_run, _repository

    repository = _repository(tmp_path)
    claim = _claimed_due_run(repository)
    repository.finish_run(claim['run_id'], claim['claim_id'],
                          SchedulerRunFinish('done', delivery_status='suppressed', now=1010))
    index = SchedulerHistoryIndex(repository.history_path, repository.owner, repository._parse_run, read_only=True)
    size = repository.history_path.stat().st_size
    expected = 4 * min(4096, size) + size
    budget = RetentionScanBudget(10, expected, time.monotonic() + 10)
    proof = index.terminal_run_proof(claim['run_id'], thread_id=claim['thread_id'], cutoff=2000,
                                     scan_budget=budget)
    assert proof['status'] == 'settled'
    assert proof['bytes_read'] == budget.size == expected
    assert proof['byte_scope'] == 'canonical_source'
    second = index.terminal_run_proof(claim['run_id'], thread_id=claim['thread_id'], cutoff=2000,
                                      scan_budget=budget)
    assert second['status'] == 'unknown'
    assert second['reason'] == 'SCHEDULER_PROOF_BUDGET_EXHAUSTED'
    assert second['bytes_read'] == 0
    assert budget.size == expected


def test_scheduler_proof_partial_reads_charge_and_deadline_never_reset(tmp_path):
    import time

    from agent.memory_store.retention_scan_budget import RetentionScanBudget
    from agent.scheduler.history_index import SchedulerHistoryIndex

    from agent_py_agent.agent.scheduler.repository import SchedulerRunFinish
    from agent_py_agent.tests.test_scheduler_repository import _claimed_due_run, _repository

    repository = _repository(tmp_path)
    claim = _claimed_due_run(repository)
    repository.finish_run(claim['run_id'], claim['claim_id'],
                          SchedulerRunFinish('done', delivery_status='suppressed', now=1010))
    index = SchedulerHistoryIndex(repository.history_path, repository.owner, repository._parse_run, read_only=True)
    size = repository.history_path.stat().st_size
    budget = RetentionScanBudget(10, 4 * min(4096, size), time.monotonic() + 10)
    result = index.terminal_run_proof(claim['run_id'], thread_id=claim['thread_id'], cutoff=2000,
                                      scan_budget=budget)
    assert result['status'] == 'unknown'
    assert result['reason'] == 'SCHEDULER_PROOF_BUDGET_EXHAUSTED'
    assert result['bytes_read'] == budget.size == 2 * min(4096, size)
    expired = RetentionScanBudget(10, 1048576, time.monotonic() - 1)
    result = index.terminal_run_proof(claim['run_id'], thread_id=claim['thread_id'], cutoff=2000,
                                      scan_budget=expired, deadline=time.monotonic() + 10)
    assert result['reason'] == 'SCHEDULER_PROOF_BUDGET_EXHAUSTED'
    assert result['bytes_read'] == expired.size == 0
