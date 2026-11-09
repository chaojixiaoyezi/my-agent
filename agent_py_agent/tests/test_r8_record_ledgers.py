"""记录账本增量写入、历史分页与并发终态的行为回归。"""
from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_py_agent.agent.conversation import ConversationStore, store_tasks
from agent_py_agent.agent.conversation import task_history as history
from agent_py_agent.agent.memory_store import candidates as memory
from agent_py_agent.agent.memory_store.candidate_ledger import CandidateRows, read_manifest
from agent_py_agent.tests.test_memory_candidate_daily_v2 import _user_observation
from agent_py_agent.tests.test_memory_candidates import candidate


def _thread(tmp_path):
    store = ConversationStore(tmp_path / 'conversations')
    thread = store.threads.get_or_create({'canonical_user_id': 'isolated-owner', 'channel': 'internal'})
    return store, thread


def test_thread_migration_keeps_full_history_and_only_windows_json(tmp_path):
    store, thread = _thread(tmp_path)
    identifiers = tuple(f'old-{number}' for number in range(520))
    thread = replace(thread, task_ids=identifiers, active_task_ids=(identifiers[0],))
    store.storage.thread_path(thread.thread_id).write_text(json.dumps(thread.to_dict()))
    store.tasks.bind({'thread_id': thread.thread_id, 'task_id': 'new'})
    current = store.threads.require(thread.thread_id)
    assert len(current.task_ids) <= 128 + len(current.active_task_ids)
    assert set(current.active_task_ids) <= set(current.task_ids)
    assert tuple(store.tasks.iter_task_ids(thread.thread_id)) == (*identifiers, 'new')
    assert store.tasks.contains_task(thread.thread_id, identifiers[1])
    assert store.tasks.task_ids_page(thread.thread_id, cursor=0, limit=3) == (identifiers[:3], 3)
    with patch.object(Path, 'open', side_effect=AssertionError('to_dict must not read files')):
        assert current.to_dict()['task_ids'] == list(current.task_ids)


def test_late_active_projection_cannot_resurrect_terminal_task(tmp_path, monkeypatch):
    store, thread = _thread(tmp_path)
    store.tasks.bind({'thread_id': thread.thread_id, 'task_id': 'task'})
    arrived, proceed = threading.Event(), threading.Event()
    original = store_tasks.update_json_file_atomic
    failures = []

    def delayed(path, updater, **kwargs):
        if path == store.storage.thread_path(thread.thread_id) and threading.current_thread().name == 'late-bind':
            arrived.set()
            assert proceed.wait(10)
        return original(path, updater, **kwargs)

    monkeypatch.setattr(store_tasks, 'update_json_file_atomic', delayed)

    def run():
        try:
            store.tasks.update_thread_index(thread.thread_id, 'task', 'active', 2)
        except BaseException as error:
            failures.append(error)

    worker = threading.Thread(target=run, name='late-bind')
    worker.start()
    assert arrived.wait(10)
    store.tasks.update_status({'task_id': 'task', 'status': 'done'})
    proceed.set()
    worker.join(10)
    assert not worker.is_alive() and not failures
    assert store.tasks.load('task').status == 'done'
    assert store.threads.require(thread.thread_id).active_task_ids == ()
    assert store.tasks.contains_task(thread.thread_id, 'task')


def test_thread_partial_migration_failure_keeps_original_history(tmp_path, monkeypatch):
    store, thread = _thread(tmp_path)
    ids = tuple(f'task-{number}' for number in range(140))
    path = store.storage.thread_path(thread.thread_id)
    path.write_text(json.dumps(replace(thread, task_ids=ids).to_dict()))
    before = path.read_bytes()
    original = history.write_private_json_file_atomic_unlocked

    def fail_manifest(target, payload, **kwargs):
        if target.name == 'index.json':
            raise OSError('publication interrupted')
        return original(target, payload, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(history, 'write_private_json_file_atomic_unlocked', fail_manifest)
        with pytest.raises(OSError):
            store.tasks.bind({'thread_id': thread.thread_id, 'task_id': 'next'})
    assert path.read_bytes() == before
    assert tuple(store.tasks.iter_task_ids(thread.thread_id)) == ids
    store.tasks.bind({'thread_id': thread.thread_id, 'task_id': 'next'})
    assert tuple(store.tasks.iter_task_ids(thread.thread_id)) == (*ids, 'next')


def test_candidate_single_updates_never_iterate_complete_history(tmp_path, monkeypatch):
    service = memory.CandidateService(tmp_path / 'candidates.jsonl')
    rows = [candidate(index) for index in range(320)]
    memory._write_candidates_unlocked(service.path, rows, admission=memory._NullAdmission())
    target = rows[123]
    before = {path.name for path in service.path.with_name(service.path.name + '.records').iterdir()}

    def forbidden(_self):
        raise AssertionError('single mutation scanned all candidates')
        yield

    with monkeypatch.context() as scoped:
        scoped.setattr(CandidateRows, '__iter__', forbidden)
        updated = service.transition(target.candidate_id, 'approved', reviewer='isolated-reviewer')
        assert updated.status == 'approved'
        assert service.get(target.candidate_id) == updated
        service.mark_promotion_excluded([target.candidate_id], reason='isolated')
        assert service.stats()['total'] == len(rows)
        assert [row.candidate_id for row in service.readonly_rows(statuses={'approved'})] == [target.candidate_id]
        observed = service.observe(replace(_user_observation(message_id='next'), subject_key='subject.124'))
        assert observed.candidate_id == rows[124].candidate_id
    after = {path.name for path in service.path.with_name(service.path.name + '.records').iterdir()}
    assert len(after) < len(before) + 32
    assert len(service.list()) == len(rows)
    assert read_manifest(service.path)['count'] == len(rows)


def test_candidate_quota_rejection_writes_no_nodes_or_header(tmp_path):
    service = memory.CandidateService(tmp_path / 'candidates.jsonl')
    original = service.observe(_user_observation())
    before = {path.name: path.read_bytes() for path in tmp_path.rglob('*') if path.is_file() and not path.name.endswith('.lock')}

    class Denied:
        def check(self, changes):
            assert len(changes) > 1  # 正文、索引和新头都必须计入峰值准入。
            raise PermissionError('quota rejected')

    with service._locked_candidates():
        rows = service._load_unlocked()
        rows[rows.index_for(original.candidate_id)] = replace(original, promotion_exclusion='denied')
        with pytest.raises(PermissionError):
            memory._write_candidates_unlocked(service.path, rows, admission=Denied())
    after = {path.name: path.read_bytes() for path in tmp_path.rglob('*') if path.is_file() and not path.name.endswith('.lock')}
    assert after == before
    assert service.get(original.candidate_id) == original


def test_candidate_hard_delete_removes_obsolete_plaintext_and_recovers_cleanup(tmp_path, monkeypatch):
    service = memory.CandidateService(tmp_path / 'candidates.jsonl')
    row = service.observe(replace(_user_observation(), content='unique private text r8'))
    from agent_py_agent.agent.common import nofollow_fs
    original = nofollow_fs.unlink_file_beneath
    failed = []

    def interrupted(root, parts):
        if parts[-1].endswith('.row') and not failed:
            failed.append(True)
            raise OSError('cleanup interrupted')
        return original(root, parts)

    with monkeypatch.context() as scoped:
        scoped.setattr(nofollow_fs, 'unlink_file_beneath', interrupted)
        with pytest.raises(OSError):
            service.redact_content(content_hashes=[row.content_hash], reviewer='isolated')
    assert service.get(row.candidate_id).content == ''
    # 同一公开删除重试须先完成原头的清理义务，即使业务状态已脱敏也不能跳过。
    service.redact_content(content_hashes=[row.content_hash], reviewer='isolated')
    assert not any(b'unique private text r8' in path.read_bytes()
                   for path in tmp_path.rglob('*') if path.is_file())


def test_deleted_personal_record_directory_rebuilds_from_new_input(tmp_path):
    import shutil
    root = tmp_path / 'owner'
    store, thread = _thread(root)
    store.tasks.bind({'thread_id': thread.thread_id, 'task_id': 'old'})
    memory.CandidateService(root / 'memory/candidates.jsonl').observe(_user_observation())
    shutil.rmtree(root)
    fresh_store, fresh_thread = _thread(root)
    fresh_store.tasks.bind({'thread_id': fresh_thread.thread_id, 'task_id': 'new'})
    service = memory.CandidateService(root / 'memory/candidates.jsonl')
    assert service.observe(_user_observation()).status
    assert tuple(fresh_store.tasks.iter_task_ids(fresh_thread.thread_id)) == ('new',)


def test_deleted_history_directory_keeps_exact_tasks_and_accepts_new_task(tmp_path):
    import shutil
    store, thread = _thread(tmp_path)
    store.tasks.bind({'thread_id': thread.thread_id, 'task_id': 'old'})
    shutil.rmtree(history.history_root(store.storage.thread_path(thread.thread_id)))
    store.tasks.bind({'thread_id': thread.thread_id, 'task_id': 'new'})
    assert store.tasks.contains_task(thread.thread_id, 'old')
    assert store.tasks.load('new').status == 'active'
    links, errors = store.tasks.list_report(thread.thread_id)
    assert not links and errors[0]['context'] == 'conversation.task_history.read'


def test_candidate_record_family_is_host_readonly_before_creation(tmp_path):
    from agent_py_agent.agent.path_access_policy import host_readonly_paths, host_state_for_path
    base = tmp_path / 'agent-home'
    owner = base / 'owners/providers/test/users/isolated'
    directory = owner / 'memory/candidates.jsonl.records'
    assert host_state_for_path(directory / 'uncreated.row', base) == directory
    assert directory in host_readonly_paths(base, owner)


def test_removed_then_readded_thread_task_has_one_history_position(tmp_path):
    store, thread = _thread(tmp_path)
    store.tasks.bind({'thread_id': thread.thread_id, 'task_id': 'task'})
    latest = store.threads.require(thread.thread_id)
    path = store.storage.thread_path(thread.thread_id)
    history.remove(path, latest, 'task')
    store.tasks.update_thread_index(thread.thread_id, 'task', 'active', 1)
    assert tuple(store.tasks.iter_task_ids(thread.thread_id)) == ('task',)
@pytest.mark.parametrize('bad', ['count', 'json', 'cleanup', 'root'])
def test_cold_curator_empty_current_ledger_is_not_due_and_corrupt_is_unknown(tmp_path, bad):
    from agent_py_agent.agent.memory_api import _cold_curator_activity

    path = tmp_path / 'memory' / 'candidates.jsonl'
    path.parent.mkdir()
    valid = '{"candidate_ledger":"memory-candidates-v2","count":0,"root":null,"cleanup":[]}'
    path.write_text(valid)
    assert _cold_curator_activity(tmp_path) == 'not_due'
    broken = {'count': valid.replace('"count":0', '"count":-1'), 'json': valid + valid,
              'cleanup': valid.replace('"cleanup":[]', '"cleanup":null'),
              'root': valid.replace('"count":0', '"count":1')}[bad]
    path.write_text(broken)
    before = {item.relative_to(tmp_path): item.read_bytes() for item in tmp_path.rglob('*') if item.is_file()}
    assert _cold_curator_activity(tmp_path) == _cold_curator_activity(tmp_path) == 'unknown'
    assert {item.relative_to(tmp_path): item.read_bytes() for item in tmp_path.rglob('*') if item.is_file()} == before


def test_cold_curator_does_not_swallow_unrelated_runtime_failure(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_api import _cold_curator_activity
    from agent_py_agent.agent.memory_store import candidate_ledger

    path = tmp_path / 'memory/candidates.jsonl'
    path.parent.mkdir()
    path.write_text('existing source')
    failure = RuntimeError('unrelated runtime failure')
    def fail(_path):
        raise failure
    monkeypatch.setattr(candidate_ledger, 'read_manifest', fail)
    with pytest.raises(RuntimeError) as caught:
        _cold_curator_activity(tmp_path)
    assert caught.value is failure


def test_candidate_node_cleanup_preserves_real_quota_baseline(tmp_path):
    from agent_py_agent.agent.user_space import owner_quota as quota

    service = memory.CandidateService(tmp_path / 'memory/candidates.jsonl')
    target = service.observe(_user_observation())
    for number in range(160):
        (tmp_path / f'payload-{number}').write_bytes(b'x')
    for _ in range(16):
        result = quota.reconcile_owner_quota_slice(tmp_path, max_entries=128, max_ms=1000)
        if result['complete']:
            break
    assert result['complete'] and quota._read_quota_ledger(tmp_path)['usage_authoritative'] is True
    service.quota_enforcer = quota.OwnerQuotaEnforcer(tmp_path, max_bytes=1024 * 1024)
    service.transition(target.candidate_id, 'approved', reviewer='quota-test')
    ledger = quota._read_quota_ledger(tmp_path)
    assert ledger['dirty_since'] is None and ledger['usage_authoritative'] is True
    assert ledger['used_bytes'] == quota.owner_logical_usage_bytes(tmp_path, exact=True)


def test_candidate_quota_does_not_precredit_obsolete_node_deletion(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.agent.user_space import owner_quota as quota

    body = 'x' * 2000
    service = memory.CandidateService(tmp_path / 'memory/candidates.jsonl')
    target = service.observe(replace(_user_observation(), content=body))
    before = {path: path.read_bytes() for path in (tmp_path / 'memory').rglob('*') if path.is_file()}
    from agent_py_agent.agent.memory_store.candidate_ledger import manifest_text, seal_cleanup
    with service._locked_candidates():
        rows = service._load_unlocked()
        rows[0] = memory._transition_candidate(target, 'approved', reviewer='quota-test')
        root, writes, obsolete = rows.plan()
    family = service.path.with_name(service.path.name + '.records')
    reclaimed = sum((family / name).stat().st_size for name in obsolete if (family / name).exists())
    cleanup = seal_cleanup(writes, obsolete)
    header = manifest_text(root, len(rows), cleanup).encode()
    peak_growth = len(header) - service.path.stat().st_size + sum(
        len(raw) - ((family / name).stat().st_size if (family / name).exists() else 0)
        for name, raw in writes.items())
    # 若把未来删除收益混入首次准入，这个不足以容纳新旧正文共存的配额反而会放行。
    available = (max(0, peak_growth - reclaimed) + peak_growth) // 2
    assert peak_growth - reclaimed < available < peak_growth
    used = quota.owner_logical_usage_bytes(tmp_path, exact=True)
    service.quota_enforcer = quota.OwnerQuotaEnforcer(tmp_path, max_bytes=used + available,
        config=SimpleNamespace(owner_quota_permanent_headroom_percent=0))
    with pytest.raises(quota.OwnerQuotaExceeded):
        service.transition(target.candidate_id, 'approved', reviewer='quota-test')
    assert {path: path.read_bytes() for path in (tmp_path / 'memory').rglob('*') if path.is_file()} == before
    assert service.get(target.candidate_id) == target


def test_pending_condense_control_update_keeps_payload_quota_baseline(tmp_path):
    from agent_py_agent.agent.memory_store.promotion import _write_condense_marker
    from agent_py_agent.agent.user_space import owner_quota as quota

    marker = tmp_path / 'memory/curator/pending_condense.json'
    payload = {'schema': 'memory.pending_condense.v1', 'formal': 'long_term/memory.jsonl',
               'pending': False, 'generation': 'a' * 32, 'through': None}
    _write_condense_marker(marker, payload, False)
    for number in range(160):
        (tmp_path / f'payload-{number}').write_bytes(b'x')
    for _ in range(16):
        report = quota.reconcile_owner_quota_slice(tmp_path, max_entries=128, max_ms=1000)
        if report['complete']:
            break
    assert report['complete'] and quota._read_quota_ledger(tmp_path)['usage_authoritative'] is True
    _write_condense_marker(marker, {**payload, 'pending': True, 'generation': 'b' * 32}, False)
    target = tmp_path / 'memory/allowed.md'
    with quota.OwnerQuotaEnforcer(tmp_path, max_bytes=1024 * 1024).reserve([quota.OwnerQuotaChange(target, 1)]):
        target.write_bytes(b'x')
    ledger = quota._read_quota_ledger(tmp_path)
    assert ledger['usage_authoritative'] is True and ledger['dirty_since'] is None
    # marker仍计费，只排除固定宿主控制写对辅助目录摘要的影响。
    assert quota.owner_logical_usage_bytes(tmp_path, exact=True) >= marker.stat().st_size + 161
