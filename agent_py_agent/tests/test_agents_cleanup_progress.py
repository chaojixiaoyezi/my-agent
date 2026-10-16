"""Migration completion must describe its durable Persona commits across retries."""
import json
from hashlib import sha256

import pytest

from agent_py_agent.agent.capability.persona_repository import (
    PersonaMutationRequest,
    versions_ledger_tail,
)
from agent_py_agent.agent.memory_store import agents_migration as migration
from agent_py_agent.tests.test_agents_cleanup_critical_section import RULE, _apply, _seed, _setup


def _record(repo, name):
    path = migration._migration_dir_for(repo.owner_home, name) / 'record.json'
    return json.loads(path.read_text())


def _assert_complete(repo, name, result, removed):
    record = _record(repo, name)
    assert result['status'] == record['status'] == 'completed'
    assert result['removed_count'] == record['removed_count'] == removed
    assert record['current_sha256'] == sha256(repo.path_for('agents').read_bytes()).hexdigest()
    assert record['versions_tail'] == versions_ledger_tail(repo.versions_path)
    assert record['remaining_count'] == 0


def test_completed_record_uses_postdelete_sha_and_ledger_tail(tmp_path):
    service, _, _, _ = _setup(tmp_path)
    repo = service.persona
    _seed(repo)
    name = 'complete-progress'
    request = migration.AgentsCleanupRequest(name, plan_digest=migration.plan_agents_cleanup(persona=repo).plan_digest)
    result = _apply(service, request)
    _assert_complete(repo, name, result, 1)
    _assert_complete(repo, name, _apply(service, migration.AgentsCleanupRequest(name)), 1)


@pytest.mark.parametrize('fault', ['append', 'progress'])
@pytest.mark.parametrize('user_readd', [False, True])
def test_resume_counts_committed_delete_even_if_progress_write_fails(tmp_path, monkeypatch, fault, user_readd):
    service, _, _, _ = _setup(tmp_path)
    repo = service.persona
    _seed(repo)
    name = 'retry-progress'
    request = migration.AgentsCleanupRequest(name, ('excluded-progress',),
        plan_digest=migration.plan_agents_cleanup(persona=repo).plan_digest)
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise OSError('injected after actual deletion')

    with monkeypatch.context() as patch:
        patch.setattr(migration, '_append_conversations' if fault == 'append' else '_update_record', fail)
        with pytest.raises(OSError, match='injected after actual deletion'):
            _apply(service, request)
    assert calls == [1]
    assert RULE not in repo.path_for('agents').read_text()
    if user_readd:
        repo.mutate(PersonaMutationRequest('agents', 'add', content=RULE))
    result = _apply(service, migration.AgentsCleanupRequest(name))
    _assert_complete(repo, name, result, 1)
    assert (RULE in repo.path_for('agents').read_text()) == user_readd
    _assert_complete(repo, name, _apply(service, migration.AgentsCleanupRequest(name)), 1)


def test_resume_does_not_count_user_deletion_as_migration(tmp_path, monkeypatch):
    service, _, _, _ = _setup(tmp_path)
    repo = service.persona
    entry = _seed(repo)
    name = 'user-delete-progress'
    request = migration.AgentsCleanupRequest(name, plan_digest=migration.plan_agents_cleanup(persona=repo).plan_digest)

    def fail(*args, **kwargs):
        raise OSError('injected before deletion')

    with monkeypatch.context() as patch:
        patch.setattr(migration, '_remove_batch', fail)
        with pytest.raises(OSError, match='before deletion'):
            _apply(service, request)
    repo.mutate(PersonaMutationRequest('agents', 'remove', entry_id=entry['entry_id']))
    result = _apply(service, migration.AgentsCleanupRequest(name))
    _assert_complete(repo, name, result, 0)
