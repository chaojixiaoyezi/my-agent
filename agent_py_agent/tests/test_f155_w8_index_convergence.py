import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.user_space.home_index_rebuild import (
    maintain_home_indexes_lane,
    reconcile_home_index_page,
)
from agent_py_agent.agent.user_space.home_indexes import (
    TaskIndexRef,
    latest_run_refs,
    latest_task_refs,
    publish_workspace_refs,
    register_task_ref,
)
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.storage_lanes import StorageLaneBudget, validate_lane_receipt


def setup_workspace(tmp_path, status='DONE', run_status=None):
    home = replace(home_paths(tmp_path / 'home'), owner_id='local/main')
    root = home.owner_home_dir / 'runs' / 'day' / 'task'
    (root / 'work').mkdir(parents=True)
    state = dict(task_id='task', primary_run_id='run', status=status)
    if run_status:
        state['run_status'] = run_status
    (root / 'work/state.json').write_text(json.dumps(state))
    return home, root


def test_committed_done_cannot_be_republished_active(tmp_path):
    home, root = setup_workspace(tmp_path)
    assert publish_workspace_refs(home, task_id='task', run_id='run', root=root)['status'] == 'published'
    assert latest_task_refs(home)[0]['status'] == 'done'
    assert latest_run_refs(home)[0]['status'] == 'done'
    size = home.global_index_active_tasks_jsonl.stat().st_size
    publish_workspace_refs(home, task_id='task', run_id='run', root=root)
    assert home.global_index_active_tasks_jsonl.stat().st_size == size
    assert publish_workspace_refs(home, task_id='task', run_id='old', root=root)['status'] == 'protected'


def test_task_and_slice_status_remain_distinct(tmp_path):
    home, root = setup_workspace(tmp_path, 'RUNNING', 'DONE')
    publish_workspace_refs(home, task_id='task', run_id='run', root=root)
    assert latest_task_refs(home)[0]['status'] == 'active'
    assert latest_run_refs(home)[0]['status'] == 'done'


def test_page_preview_apply_owner_cursor_and_budget(tmp_path):
    home, root = setup_workspace(tmp_path)
    register_task_ref(home, TaskIndexRef(owner_id=home.owner_id, task_id='task', task_path=root, status='active', title='task'))
    before = home.global_index_active_tasks_jsonl.read_bytes()
    budget = StorageLaneBudget(1, 4096, None)
    preview = reconcile_home_index_page(home, owner_id=home.owner_id, budget=budget)
    assert preview['items'] == 1 and preview['repaired'] == 1
    assert home.global_index_active_tasks_jsonl.read_bytes() == before
    applied = reconcile_home_index_page(home, owner_id=home.owner_id, budget=budget, apply=True)
    assert applied['repaired'] == 1
    assert latest_task_refs(home)[0]['status'] == 'done'
    rejected = reconcile_home_index_page(home, owner_id='local/other', cursor=preview['next_cursor'])
    assert rejected['errors'][0]['code'] == 'INVALID_INDEX_CURSOR'


def test_lane_without_coordinator_does_no_work(tmp_path):
    home, _ = setup_workspace(tmp_path)
    request = SimpleNamespace(home=home, kind='global_indexes', coordinator=False, cursor={})
    receipt = validate_lane_receipt(maintain_home_indexes_lane(request))
    assert receipt['items'] == receipt['bytes'] == 0


def test_reconcile_commit_gate_rechecks_resumed_generation(tmp_path):
    home, root = setup_workspace(tmp_path)
    register_task_ref(home, TaskIndexRef(owner_id=home.owner_id, task_id='task', task_path=root, status='active'))

    def resume_before_commit(operation):
        (root / 'work/state.json').write_text(json.dumps(dict(task_id='task', primary_run_id='next', status='RUNNING')))
        return operation()

    report = reconcile_home_index_page(home, owner_id=home.owner_id, apply=True,
                                      budget=StorageLaneBudget(1, 4096, None),
                                      commit_transition=resume_before_commit)
    assert report['protected'] == 1
    assert report['effect_outcome'] == 'not_started'
    assert latest_task_refs(home)[0]['status'] == 'active'


def test_sparse_long_history_bounds_visited_work(tmp_path):
    home, root = setup_workspace(tmp_path)
    path = home.global_index_active_tasks_jsonl
    path.parent.mkdir(parents=True)
    row = dict(owner_id='local/other', task_id='task', task_path=str(root), status='active')
    path.write_text((json.dumps(row) + '\n') * 36500)
    report = reconcile_home_index_page(home, owner_id=home.owner_id,
                                      budget=StorageLaneBudget(7, 4096, None))
    assert report['items'] == 7
    assert report['bytes'] <= 4096
    assert report['partial'] and report['next_cursor']
    second = reconcile_home_index_page(home, owner_id=home.owner_id, cursor=report['next_cursor'],
                                      budget=StorageLaneBudget(7, 4096, None))
    assert json.loads(second['next_cursor'])['offset'] > json.loads(report['next_cursor'])['offset']


def test_rebuild_snapshot_rejects_concurrent_append(tmp_path):
    import pytest

    from agent_py_agent.agent.user_space.home_index_rebuild import _index_source_stamp
    from agent_py_agent.agent.user_space.home_indexes import replace_home_index_snapshots
    home, root = setup_workspace(tmp_path)
    paths = (home.global_index_owners_jsonl, home.global_index_active_tasks_jsonl,
             home.global_index_active_runs_jsonl, home.global_index_active_agents_jsonl)
    stamps = {str(path): _index_source_stamp(path) for path in paths}
    register_task_ref(home, TaskIndexRef(owner_id=home.owner_id, task_id='task', task_path=root, status='done'))
    before = home.global_index_active_tasks_jsonl.read_bytes()
    with pytest.raises(RuntimeError, match='INDEX_REBUILD_SOURCE_CHANGED'):
        replace_home_index_snapshots(home, owners=(), task_refs=(), run_refs=(), agent_refs=(),
                                     expected_source_stamps=stamps)
    assert home.global_index_active_tasks_jsonl.read_bytes() == before


def test_physical_compaction_resumes_and_preserves_appended_tail(tmp_path):
    from agent_py_agent.agent.user_space.home_index_compact import compact_index_page
    home, _ = setup_workspace(tmp_path)
    path = home.global_index_active_tasks_jsonl
    path.parent.mkdir(parents=True)
    rows = [dict(owner_id='local/main', task_id=str(n % 5), status=str(n)) for n in range(100)]
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    cursor = {}
    appended = False
    for _ in range(200):
        receipt = compact_index_page(path, cursor=cursor, budget=StorageLaneBudget(7, 2048, None))
        assert receipt['items'] <= 7 and receipt['bytes'] <= 2048
        cursor = receipt['cursor']
        if cursor.get('phase') == 'commit' and not appended:
            with path.open('a') as sink:
                sink.write(json.dumps(dict(owner_id='local/other', task_id='tail', status='DONE')) + '\n')
            appended = True
        if receipt['complete']:
            break
    else:
        raise AssertionError('bounded compaction did not converge')
    kept = [json.loads(raw) for raw in path.read_text().splitlines()]
    assert kept[:5] == rows[-5:]
    assert kept[-1]['task_id'] == 'tail'
    assert appended


def test_committed_execution_proof_survives_active_task_projection(tmp_path):
    home, root = setup_workspace(tmp_path, 'RUNNING')
    state_path = root / 'work/state.json'
    state = json.loads(state_path.read_text())
    state.update(terminal_run_id='run', runtime_status='ok', finished_at='2026-11-16T00:00:00Z')
    state_path.write_text(json.dumps(state))
    publish_workspace_refs(home, task_id='task', run_id='run', root=root)
    assert latest_task_refs(home)[0]['status'] == 'active'
    assert latest_run_refs(home)[0]['status'] == 'done'


def test_compaction_discards_replaced_source_generation(tmp_path):
    from agent_py_agent.agent.user_space.home_index_compact import compact_index_page
    home, _ = setup_workspace(tmp_path)
    path = home.global_index_active_tasks_jsonl
    path.parent.mkdir(parents=True)
    path.write_text((json.dumps(dict(owner_id='local/main', task_id='x')) + '\n') * 10)
    receipt = compact_index_page(path, cursor={}, budget=StorageLaneBudget(2, 4096, None))
    replacement = path.with_suffix('.new')
    replacement.write_text(json.dumps(dict(owner_id='local/other', task_id='new')) + '\n')
    replacement.replace(path)
    before = path.read_bytes()
    rejected = compact_index_page(path, cursor=receipt['cursor'], budget=StorageLaneBudget(2, 4096, None))
    assert rejected['complete'] is False and rejected['cursor'] == {}
    assert rejected.get('errors', []) == []
    assert rejected['skipped'] == {'source_generation_changed': 1}
    assert rejected['deferred_reason'] == 'compaction_source_generation_changed_restart_scan'
    assert validate_lane_receipt(rejected) == rejected
    assert path.read_bytes() == before
    assert not list(path.parent.glob('*.stage.sqlite3'))
    cursor = rejected['cursor']
    for _ in range(8):
        resumed = compact_index_page(path, cursor=cursor, budget=StorageLaneBudget(2, 4096, None))
        cursor = resumed['cursor']
        assert resumed.get('errors', []) == []
        if resumed['complete']:
            break
    else:
        pytest.fail('来源稳定后压缩未在原预算内完成重扫')
    assert path.read_bytes() == before


def test_owner_compactions_share_source_without_identical_replacement_churn(tmp_path):
    from agent_py_agent.agent.user_space.home_index_compact import compact_index_page

    home, _ = setup_workspace(tmp_path)
    path = home.global_index_active_tasks_jsonl
    path.parent.mkdir(parents=True)
    rows = [dict(owner_id='local/main', task_id=str(n % 3), status=str(n)) for n in range(12)]
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    cursors = [{}, {}]
    done = [False, False]
    deferred = []
    for _ in range(32):
        for owner in range(2):
            if done[owner]:
                continue
            result = compact_index_page(path, cursor=cursors[owner], budget=StorageLaneBudget(2, 4096, None))
            assert result['items'] <= 2 and result['bytes'] <= 4096
            assert result.get('errors', []) == []
            if result.get('deferred_reason'):
                deferred.append(result['deferred_reason'])
            cursors[owner] = result['cursor']
            done[owner] = result['complete']
        if all(done):
            break
    else:
        pytest.fail('两个owner的真实分片压缩未收敛')
    assert deferred == ['compaction_source_generation_changed_restart_scan']
    assert [json.loads(raw) for raw in path.read_text().splitlines()] == rows[-3:]
    identity = path.stat()
    cursor = {}
    for _ in range(16):
        result = compact_index_page(path, cursor=cursor, budget=StorageLaneBudget(2, 4096, None))
        cursor = result['cursor']
        if result['complete']:
            break
    else:
        pytest.fail('无重复记录的下一轮压缩未完成')
    assert path.stat().st_ino == identity.st_ino
    assert not list(path.parent.glob('*.stage.sqlite3'))
    assert not list(path.parent.glob('*.stage.jsonl'))


def test_external_append_cannot_leave_old_publisher_cache_authoritative(tmp_path):
    home, root = setup_workspace(tmp_path)
    publish_workspace_refs(home, task_id='task', run_id='run', root=root)
    # 模拟另一进程旧投影追加；原进程必须重新发表 canonical DONE。
    with home.global_index_active_tasks_jsonl.open('a') as sink:
        sink.write(json.dumps(dict(owner_id=home.owner_id, task_id='task', task_path=str(root), status='active')) + '\n')
    publish_workspace_refs(home, task_id='task', run_id='run', root=root)
    assert latest_task_refs(home)[0]['status'] == 'done'


def test_late_archive_waiting_on_canonical_lock_observes_resumed_run(tmp_path):
    import threading

    from agent_py_agent.agent.common.json_io import locked_json_path
    home, root = setup_workspace(tmp_path)
    state_path = root / 'work/state.json'
    entered = threading.Event()
    receipts = []

    def late_archive():
        entered.set()
        receipts.append(publish_workspace_refs(home, task_id='task', run_id='run', root=root))

    with locked_json_path(state_path):
        worker = threading.Thread(target=late_archive)
        worker.start()
        assert entered.wait(2)
        state_path.write_text(json.dumps(dict(task_id='task', primary_run_id='next', status='RUNNING')))
    worker.join(3)
    assert not worker.is_alive()
    assert receipts[0]['status'] == 'protected'
    assert not home.global_index_active_tasks_jsonl.exists()
