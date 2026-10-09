"""review33b: isolated real state, real threads/children; no live owner or Gateway."""
import hashlib
import json
import subprocess
import sys
import threading
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from agent_py_agent.agent.memory_store import agents_migration as migration
from agent_py_agent.agent.memory_store import curator_lease, curator_state
from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaAdmission, OwnerQuotaEnforcer
from agent_py_agent.tests.test_agents_cleanup_review33 import _apply, _candidate, _setup

ROOT = Path(__file__).parent


def save(name, payload):
    (ROOT / (name + '.json')).write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def directory(env):
    return env.service.persona.owner_home / 'persona/migrations/agents-migration-review33'


def business_bytes(env):
    paths = [env.service.persona.path_for('agents'), env.service.persona.versions_path,
             directory(env) / 'archive.jsonl', env.home.owner_memory_policy_json]
    paths.extend((directory(env) / 'snapshot').rglob('*'))
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths if path.is_file()}


def test_completed_pending_recovers_same_id_when_record_storage_recovers(tmp_path, monkeypatch):
    env = _setup(tmp_path)
    def fail_record(_path, _data):
        raise OSError('isolated transient final diagnostic storage failure')
    with monkeypatch.context() as patch:
        patch.setattr(migration, 'write_private_json_file_atomic_unlocked', fail_record)
        first = _apply(env)
    before = business_bytes(env)
    assert first['status'] == 'completed'
    assert env.store.load().active_lease == {}, 'release itself must already be durable'
    pending_before = json.loads((directory(env) / 'record.json').read_text())
    replay = _apply(env)
    pending_after = json.loads((directory(env) / 'record.json').read_text())
    save('recovered-record-replay', {'first': first, 'before': pending_before,
        'replay': replay, 'after': pending_after, 'lease': env.store.load().active_lease,
        'business_unchanged': before == business_bytes(env)})
    assert before == business_bytes(env), 'cleanup-only replay must not repeat deletion/archive'
    assert replay['ok'] is True, 'storage recovered and exact lease already absent; pending needs reconciliation'
    assert not pending_after.get('lease_cleanup_pending')


def test_retired_old_lease_replay_recovers_without_touching_new_lease(tmp_path, monkeypatch):
    env = _setup(tmp_path)
    def fail_release(_request):
        raise OSError('isolated transient release failure')
    with monkeypatch.context() as patch:
        patch.setattr(env.store, 'commit_success', fail_release)
        first = _apply(env)
    old = env.store.load().active_lease
    assert old['phase'] == 'interrupted'
    future = datetime.fromisoformat(old['expires_at']) + timedelta(hours=1)
    acquired = env.store.acquire(reason='admin', config_revision='new-owner-run', lease_seconds=300, now=future)
    assert acquired is not None
    lease_bytes = env.store.path.read_bytes()
    before = business_bytes(env)
    replay = _apply(env)
    save('retired-replay-new-lease', {'first': first, 'old_lease': old, 'replay': replay,
        'replacement': env.store.load().active_lease, 'new_lease_unchanged': env.store.path.read_bytes() == lease_bytes,
        'business_unchanged': before == business_bytes(env)})
    assert env.store.path.read_bytes() == lease_bytes, 'old cleanup must not release a replacement lease'
    assert before == business_bytes(env)
    assert replay['ok'] is True, 'old exact lease no longer exists, but completed replay is permanently false'


CHILD = r'''
import json, os, sys
from pathlib import Path
from types import SimpleNamespace
from agent_py_agent.agent.capability.persona_repository import PersonaRepository
from agent_py_agent.agent.memory_store import agents_migration as m
from agent_py_agent.agent.memory_store.curator_state import MemoryCuratorStateStore
p=json.loads(sys.argv[1]); root=Path(p['owner'])
repo=PersonaRepository(owner_home=root, agents_path=root/'AGENTS.md', soul_path=root/'SOUL.md', user_path=root/'USER.md')
home=SimpleNamespace(owner_home_dir=root, owner_agents_md=root/'AGENTS.md',
    owner_memory_candidates_jsonl=Path(p['candidates']), owner_memory_policy_json=root/'memory_policy.json')
stage=p['stage']
if stage=='after_completed':
    original=m._write_record
    def crash(directory, record):
        original(directory, record)
        if record.get('status')=='completed': os._exit(73)
    m._write_record=crash
else:
    name='_archive_planned' if stage=='after_archive' else '_remove_batch'
    original=getattr(m,name)
    def crash(*args, **kwargs):
        original(*args, **kwargs)
        os._exit(73)
    setattr(m,name,crash)
m.apply_agents_cleanup(home_paths=home, persona=repo, candidate_service=None,
    request=m.AgentsCleanupRequest(**p['request']),
    curator_state_store=MemoryCuratorStateStore(root/'memory/curator/state.json'))
raise AssertionError('crash seam not reached')
'''


@pytest.mark.parametrize('stage', ['after_archive', 'after_delete', 'after_completed'])
def test_real_process_crash_same_id_recovery_no_duplicate_business(tmp_path, stage):
    env = _setup(tmp_path)
    args = {'owner': str(env.service.persona.owner_home), 'candidates': str(env.service.candidates.path),
            'request': asdict(env.request), 'stage': stage}
    child = subprocess.run([sys.executable, '-c', CHILD, json.dumps(args)], text=True, capture_output=True, timeout=20)
    assert child.returncode == 73, (child.returncode, child.stdout, child.stderr)
    old_lease = env.store.load().active_lease
    assert curator_state.stale_lease_reclaimable(old_lease), 'real child must be confirmed dead'
    archive_before = (directory(env) / 'archive.jsonl').read_bytes()
    env.request = replace(env.request, plan_digest='')
    replay = _apply(env)
    record = json.loads((directory(env) / 'record.json').read_text())
    history = env.service.persona.history('agents')
    deletes = [row for row in history if row.get('source') == 'migration:review33']
    current_lease = env.store.load().active_lease
    save('crash-' + stage, {'child_exit': child.returncode, 'confirmed_dead': True,
        'old_lease': old_lease, 'replay': replay, 'record': record, 'current_lease': current_lease,
        'delete_versions': len(deletes), 'archive_unchanged': archive_before == (directory(env) / 'archive.jsonl').read_bytes()})
    assert archive_before == (directory(env) / 'archive.jsonl').read_bytes()
    assert len(deletes) == 1, 'exactly one durable deletion batch after crash and replay'
    assert env.service.persona.list_entries('agents')['entries'] == []
    assert replay['ok'] is True
    assert current_lease == {}, 'same migration-id must finish cleanup of its dead exact holder'


@pytest.mark.parametrize('primary_kind', ['completed', 'body_error'])
def test_unconfirmed_heartbeat_join_never_releases_while_real_thread_alive(tmp_path, monkeypatch, primary_kind):
    env = _setup(tmp_path)
    entered, unblock = threading.Event(), threading.Event()
    threads, releases = [], []
    original_renew, original_join = curator_lease.renew_curator_lease, threading.Thread.join
    original_run, original_commit = migration._run_cleanup, env.store.commit_success
    primary = RuntimeError('body finished with isolated error')
    interrupted = KeyboardInterrupt('isolated join interruption')
    def gated_renew(store, lease_id, seconds):
        if threading.current_thread().name == 'curator-lease-heartbeat':
            threads.append(threading.current_thread())
            entered.set()
            assert unblock.wait(10), 'probe must unblock its real heartbeat thread'
        return original_renew(store, lease_id, seconds)
    def run(context):
        report = original_run(context)
        assert entered.wait(5)
        if primary_kind == 'body_error':
            raise primary
        return report
    def interrupted_join(thread, *args, **kwargs):
        if thread.name == 'curator-lease-heartbeat':
            assert thread.is_alive()
            raise interrupted
        return original_join(thread, *args, **kwargs)
    def trace_release(request):
        releases.append(request.lease_id)
        return original_commit(request)
    monkeypatch.setattr(migration, '_MIGRATION_LEASE_SECONDS', 0)
    monkeypatch.setattr(curator_lease, 'renew_curator_lease', gated_renew)
    monkeypatch.setattr(threading.Thread, 'join', interrupted_join)
    monkeypatch.setattr(migration, '_run_cleanup', run)
    monkeypatch.setattr(env.store, 'commit_success', trace_release)
    try:
        with pytest.raises(BaseException) as caught:
            _apply(env)
        expected = primary if primary_kind == 'body_error' else interrupted
        assert caught.value is expected
        lease = env.store.load().active_lease
        save('join-' + primary_kind, {'exception': type(caught.value).__name__,
            'stop_marker': caught.value.__dict__.get('lease_heartbeat_stop_error_class'),
            'cleanup_detail': caught.value.__dict__.get('lease_cleanup'),
            'thread_alive': [thread.is_alive() for thread in threads], 'release_attempts': releases,
            'lease': lease, 'record': json.loads((directory(env) / 'record.json').read_text())})
        assert threads and all(thread.is_alive() for thread in threads)
        assert not releases, 'unconfirmed join cannot authorize a lease release'
        assert lease.get('phase') == 'executing'
    finally:
        unblock.set()
        for thread in threads:
            original_join(thread, 5)
            assert not thread.is_alive()


@pytest.mark.parametrize('target', ['long_term', 'user'])
def test_parallel_owner_admissions_are_local_to_exact_repository(tmp_path, monkeypatch, target):
    for name in ('owner-a', 'owner-b'):
        (tmp_path / name).mkdir()
    envs = [_setup(tmp_path / name) for name in ('owner-a', 'owner-b')]
    candidates = [_candidate(env, target) for env in envs]
    for env in envs:
        quota = OwnerQuotaEnforcer(env.service.persona.owner_home, max_bytes=100_000_000)
        env.service.persona.quota_enforcer = env.service.long_term.quota_enforcer = quota
        env.service.candidates.quota_enforcer = quota
    observations, errors, results = [], [], []
    barrier = threading.Barrier(2)
    check = OwnerQuotaAdmission.check
    def checked(admission, changes):
        rows = list(changes)
        observations.append({'thread': threading.current_thread().name,
            'owner': str(admission._enforcer.owner_root), 'paths': [str(item.path) for item in rows]})
        return check(admission, rows)
    def promote(index):
        try:
            barrier.wait(5)
            results.append(envs[index].service.promote(candidates[index].candidate_id, automatic=target == 'long_term'))
        except BaseException as exc:
            errors.append(repr(exc))
    monkeypatch.setattr(OwnerQuotaAdmission, 'check', checked)
    workers = [threading.Thread(name='owner-' + str(i), target=promote, args=(i,)) for i in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(10)
    assert all(not worker.is_alive() for worker in workers)
    assert not errors and len(results) == 2 and all(result.promoted for result in results)
    for i, env in enumerate(envs):
        own = [row for row in observations if row['thread'] == 'owner-' + str(i)]
        assert own
        for row in own:
            assert Path(row['owner']) == env.service.persona.owner_home
            assert all(Path(path).is_relative_to(env.service.persona.owner_home) for path in row['paths'])
    save('owner-isolation-' + target, {'observations': observations, 'promoted': [r.to_dict() for r in results]})


@pytest.fixture(autouse=True)
def _isolated_probe_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
