"""反向引用有界发现与原定义提交顺序回归。"""
from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.workflow_store import (
    WorkflowDefinition,
    WorkflowStepSpec,
    WorkflowStore,
)
from agent_py_agent.agent.workflows import reference_index as index
from agent_py_agent.agent.workflows.reference_sources import WorkflowReferenceReader
from agent_py_agent.tests.test_rework_r3_workflow_maintenance_resume import owner  # noqa: F401


def flow(name, target=''):
    return WorkflowDefinition(AssetDescriptor(name, 'workflow', name),
        (WorkflowStepSpec('step', 'step', 'subworkflow' if target else 'condition',
                          subworkflow_ref=target),))


def test_target_page_is_bounded_and_edits_delete_and_recycle_remove_only_actual_refs(tmp_path, monkeypatch):
    store = WorkflowStore(tmp_path)
    for n in range(5):
        store.save_workflow(flow(f'parent-{n}', 'target'))
    reads = []
    original = store._get
    monkeypatch.setattr(store, '_get', lambda key: (reads.append(key), original(key))[1])
    complete, parents = index.read_parents(store, 'target', limit=2)
    assert not complete and len(parents) == 2 and len(reads) == 2
    store.save_workflow(flow('parent-0', 'other'))
    store.delete_workflow('parent-1')
    current = store.get_workflow('parent-2')
    from agent_py_agent.agent.workflows.business_scope import with_state, workflow_state
    state = {**workflow_state(current), 'lifecycle': 'recycled'}
    with store._locked():
        store._save(with_state(current, state))
    assert index.read_parents(store, 'target', limit=10) == (True, ['parent-3', 'parent-4'])
    assert index.read_parents(store, 'other', limit=10) == (True, ['parent-0'])


def test_failed_publication_stays_unknown_until_original_definition_is_reindexed(tmp_path, monkeypatch):
    store = WorkflowStore(tmp_path)
    store.save_workflow(flow('parent', 'old'))
    real = index.publish
    monkeypatch.setattr(index, 'publish', lambda *args: (_ for _ in ()).throw(OSError('index failed')))
    store.save_workflow(flow('parent', 'new'))
    assert index.read_parents(store, 'new', limit=10)[0] is False
    monkeypatch.setattr(index, 'publish', real)
    assert index.discover(store, limit=8)['complete']
    assert index.read_parents(store, 'new', limit=10) == (True, ['parent'])
    assert index.read_parents(store, 'old', limit=10) == (True, [])


def test_legacy_missing_index_discovers_in_pages_and_survives_cursor_eviction(tmp_path, monkeypatch):
    store = WorkflowStore(tmp_path)
    for n in range(9):
        store.save_workflow(flow(f'legacy-{n}', 'target' if n == 8 else ''))
    (tmp_path / 'data' / index._FILENAME).unlink()
    reads = []
    real = store._get
    monkeypatch.setattr(store, '_get', lambda key: (reads.append(key), real(key))[1])
    assert index.read_parents(store, 'target', limit=2) == (False, [])
    assert reads == []
    first = index.discover(store, limit=2)
    assert not first['complete'] and first['scanned'] == 2 and len(reads) <= 2
    # 模拟进程重启：原生目录句柄关闭，持久事实保留，逐页重放而非单次跳过N项。
    for cursor in index._SCANS.values():
        cursor.iterator.close()
    index._SCANS.clear()
    for _ in range(12):
        before = len(reads)
        result = index.discover(store, limit=2)
        assert len(reads) - before <= 2
        if result['complete']:
            break
    assert result['complete']
    assert index.read_parents(store, 'target', limit=2) == (True, ['legacy-8'])


def test_legacy_change_during_scan_does_not_publish_stale_absence(tmp_path):
    store = WorkflowStore(tmp_path)
    for n in range(5):
        store.save_workflow(flow(f'parent-{n}'))
    (tmp_path / 'data' / index._FILENAME).unlink()
    index.discover(store, limit=1)
    store.save_workflow(flow('new-parent', 'target'))
    for _ in range(12):
        if index.discover(store, limit=2)['complete']:
            break
    complete, parents = index.read_parents(store, 'target', limit=10)
    assert complete and parents == ['new-parent']
    reader = WorkflowReferenceReader(tmp_path, workflow_store=store)
    blockers = []
    assert reader._check_subworkflows('target', blockers)
    assert blockers[0]['ref'] == 'new-parent'


def test_original_owner_tick_continues_discovery_within_same_day(owner, monkeypatch):  # noqa: F811
    from agent_py_agent.tests.test_rework_r3_workflow_maintenance_resume import _create, _tick

    for n in range(6):
        _create(owner, f'legacy-{n}')
    (owner.api.store.owner_home / 'data' / index._FILENAME).unlink()
    real = index.discover
    pages = []

    def small_page(store, *, limit):
        page = real(store, limit=2)
        pages.append(page)
        return page

    monkeypatch.setattr(index, 'discover', small_page)
    initial = owner.clock[0]
    state = _tick(owner)
    assert state['maintenance_partial']
    for _ in range(8):
        owner.clock[0] += 1
        state = _tick(owner)
        if state['workflow_maintenance'].get('partial') is not True:
            break
    assert pages[-1]['complete']
    assert owner.clock[0] - initial < 10
    assert all(page['scanned'] <= 2 for page in pages)


def test_batch_existing_definition_updates_index_and_rollback_keeps_old_dependency(tmp_path, monkeypatch):
    import pytest

    from agent_py_agent.agent.assets import workspace_assets as batch

    store = WorkflowStore(tmp_path)
    store.save_workflow(flow('parent', 'old'))
    batch.commit_asset_batch([flow('parent', 'new').to_dict()], owner_home=tmp_path)
    assert index.read_parents(store, 'new', limit=10) == (True, ['parent'])
    assert index.read_parents(store, 'old', limit=10) == (True, [])
    original = batch._put_text
    failures = []

    def fail_once(root, parts, content):
        if tuple(parts) == ('workflows', 'parent', 'workflow.json') and not failures:
            failures.append(True)
            raise OSError('injected batch canonical failure')
        return original(root, parts, content)

    monkeypatch.setattr(batch, '_put_text', fail_once)
    with pytest.raises(OSError):
        batch.commit_asset_batch([flow('parent', 'rolled-back').to_dict()], owner_home=tmp_path)
    assert index.read_parents(store, 'new', limit=10)[0] is False
    assert index.discover(store, limit=8)['complete']
    assert index.read_parents(store, 'new', limit=10) == (True, ['parent'])
    assert index.read_parents(store, 'rolled-back', limit=10) == (True, [])


def test_deleted_parent_and_failed_delete_publication_are_pruned_in_bounded_pages(tmp_path, monkeypatch):
    import shutil

    store = WorkflowStore(tmp_path)
    store.save_workflow(flow('manual-delete', 'target'))
    store.save_workflow(flow('failed-publish-delete', 'target'))
    shutil.rmtree(tmp_path / 'workflows' / 'manual-delete')
    original = index.publish
    monkeypatch.setattr(index, 'publish', lambda *args: (_ for _ in ()).throw(OSError('after delete')))
    assert store.delete_workflow('failed-publish-delete')
    monkeypatch.setattr(index, 'publish', original)
    assert not index.read_parents(store, 'target', limit=10)[0]
    for _ in range(12):
        result = index.discover(store, limit=1)
        assert result['scanned'] <= 1
        if result['complete']:
            break
    assert result['complete']
    assert index.read_parents(store, 'target', limit=10) == (True, [])
    assert index.discovery_status(store)['partial'] is False


def test_discovery_io_in_one_owner_does_not_hold_global_cursor_pool_lock(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    first, second = WorkflowStore(tmp_path / 'first'), WorkflowStore(tmp_path / 'second')
    for store in (first, second):
        store.save_workflow(flow('parent', 'target'))
        (store.owner_home / 'data' / index._FILENAME).unlink()
    reading, release = threading.Event(), threading.Event()
    original = first._get

    def blocked(key):
        reading.set()
        assert release.wait(5)
        return original(key)

    monkeypatch.setattr(first, '_get', blocked)
    with ThreadPoolExecutor(max_workers=2) as executor:
        stalled = executor.submit(index.discover, first, limit=8)
        assert reading.wait(5)
        try:
            other = executor.submit(index.discover, second, limit=8)
            assert other.result(timeout=3)['complete']
        finally:
            release.set()
        assert stalled.result(timeout=5)['complete']


def test_bad_prune_rows_do_not_starve_later_missing_parent_or_spin_forever(tmp_path, monkeypatch):
    import shutil

    store = WorkflowStore(tmp_path)
    for name in ('bad-a', 'bad-b', 'good-tail'):
        store.save_workflow(flow(name, 'target'))
        shutil.rmtree(tmp_path / 'workflows' / name)
    original = store._get
    attempts = []

    def damaged(name):
        attempts.append(name)
        if name.startswith('bad-'):
            raise OSError('injected unreadable parent')
        return original(name)

    monkeypatch.setattr(store, '_get', damaged)
    for _ in range(12):
        page = index.discover(store, limit=1)
        if not index.discovery_status(store)['partial']:
            break
    assert not page['complete'] and page['errors'] == 2
    assert page['reason_code'] == 'WORKFLOW_REFERENCE_DEFINITION_UNREADABLE'
    assert attempts.count('bad-a') == attempts.count('bad-b') == 1
    assert 'good-tail' in attempts
    with index._connection(store) as conn:
        assert conn.execute("SELECT 1 FROM parents WHERE parent='good-tail'").fetchone() is None
        assert len(conn.execute('SELECT parent FROM refs').fetchall()) == 2


def test_direct_creation_then_original_store_write_does_not_absorb_invalidation(tmp_path):
    import json

    store = WorkflowStore(tmp_path)
    store.save_workflow(flow('existing'))
    # 模拟turn_extractor同root锁下仅允许新目录的发布；随后的正常写不能吞掉新增目录事实。
    direct = flow('extracted', 'target')
    with store._locked():
        root = tmp_path / 'workflows' / 'extracted'
        root.mkdir()
        (root / 'workflow.json').write_text(json.dumps(direct.descriptor.to_dict() | {
            'steps': [step.to_dict() for step in direct.steps], 'body_markdown': ''}))
    store.save_workflow(flow('existing'))
    assert index.read_parents(store, 'target', limit=10) == (False, [])
    assert index.discover(store, limit=8)['complete']
    assert index.read_parents(store, 'target', limit=10) == (True, ['extracted'])
