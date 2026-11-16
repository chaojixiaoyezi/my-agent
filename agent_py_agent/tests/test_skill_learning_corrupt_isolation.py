"""损坏兄弟 Skill/草稿只隔离自身；真实发布与 FIFO 保持安全推进。"""
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability import skill_learning as learning
from agent_py_agent.agent.capability.skill_learning_promotion import propose_or_promote_skill
from agent_py_agent.agent.capability.skill_learning_prompt import OUTPUT_SCHEMA_VERSION
from agent_py_agent.agent.capability.skill_learning_proposals import list_learning_proposals
from agent_py_agent.agent.capability.skill_learning_publish import (
    PublishRequest,
    SkillLearningGateError,
    publish_learned_skill,
)
from agent_py_agent.agent.capability.skill_learning_request import build_learning_request
from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.tests.test_skill_learning import _ctx, _FakeBackend, _output
from agent_py_agent.tests.test_wave7_autonomous_learning_closure import decision


def _store(tmp_path):
    return SkillLearningStore.for_home(ensure_my_agent_home(tmp_path / 'home'))


def _seed(store, name):
    publish_learned_skill(store, PublishRequest(decision(name), max_skills=50))
    return store.skill_path(name)


def _entry(store, name):
    return SimpleNamespace(name=name, source='owner', stable_id='owner:' + name,
                           description='已登记方法', path=store.skill_path(name))


def test_invalid_utf8_material_and_similarity_isolate_single_skill(tmp_path):
    store = _store(tmp_path)
    broken = _seed(store, 'broken-method')
    _seed(store, 'healthy-method')
    broken.write_bytes(b'\xff\xfe')
    registry_before = store.registry_path.read_bytes()
    request = {'used_skill_ids': ['owner:broken-method', 'owner:healthy-method']}
    material = learning._material(store, request, (_entry(store, 'broken-method'), _entry(store, 'healthy-method')))
    assert {row['name'] for row in material.updatable_skills} == {'healthy-method'}
    assert learning._updatable_by_name(store, store.load_registry(), 'broken-method') is None
    similar = learning._similar_managed_skills(store, decision('new-method'), threshold_percent=70)
    assert {row['name'] for row in similar} == {'healthy-method'}
    assert broken.read_bytes() == b'\xff\xfe'
    assert store.registry_path.read_bytes() == registry_before


@pytest.mark.parametrize('limit, expected', [(50, 'published'), (1, 'blocked')])
def test_actual_publisher_counts_unknown_as_occupied_without_touching_it(tmp_path, limit, expected):
    store = _store(tmp_path)
    existing = _seed(store, 'unknown-method')
    registry = store.load_registry()
    row = registry.skills['unknown-method']
    with store.state_lock():
        store.save_registry(replace(registry, skills={row.name: replace(row,
            maintenance={**row.maintenance, 'ownership': 'unknown'})}))
    previous = existing.read_bytes()
    request = PublishRequest(decision('independent-method'), max_skills=limit)
    if expected == 'published':
        assert propose_or_promote_skill(store, request).event == 'published'
        assert store.skill_path('independent-method').is_file()
    else:
        with pytest.raises(SkillLearningGateError) as exc:
            propose_or_promote_skill(store, request)
        assert exc.value.code == 'SKILL_LEARNING_EVIDENCE_INCOMPLETE'
        assert not store.skill_path('independent-method').exists()
    assert existing.read_bytes() == previous
    assert store.load_registry().skills['unknown-method'].maintenance['ownership'] == 'unknown'


def test_corrupt_old_draft_does_not_precede_verified_publish_but_manual_remains_strict(tmp_path):
    store = _store(tmp_path)
    folder = store.directory / 'proposals'
    folder.mkdir(parents=True)
    corrupt = folder / ('learning-proposal-' + 'a' * 32 + '.json')
    corrupt.write_bytes(b'{broken-json')
    assert propose_or_promote_skill(store, PublishRequest(decision('independent-method'))).event == 'published'
    assert corrupt.read_bytes() == b'{broken-json'
    with pytest.raises(ValueError):
        list_learning_proposals(store)
    with pytest.raises(ValueError):
        propose_or_promote_skill(store, PublishRequest(decision('manual-method')), autonomous_verified=False)


def test_three_fifo_requests_pass_corrupt_skill_and_draft_without_spinning(tmp_path):
    store = _store(tmp_path)
    broken = _seed(store, 'broken-method')
    broken.write_bytes(b'\xff\xfe')
    proposals = store.directory / 'proposals'
    proposals.mkdir(parents=True)
    corrupt = proposals / ('learning-proposal-' + 'b' * 32 + '.json')
    corrupt.write_bytes(b'{bad-draft')
    backend = _FakeBackend(*[_output(name='independent-method') for _ in range(3)])
    runtime = learning.SkillLearningRuntime(backend=backend,
        snapshot_provider=lambda: SimpleNamespace(entries=(_entry(store, 'broken-method'),)))
    service = learning.SkillLearningService(store,
        learning.SkillLearningSettings(autonomous_verified=True, merge_similarity_percent=0), runtime)
    for number in range(3):
        request = build_learning_request(_ctx('isolated-' + str(number)), 1)
        request['trajectory_fingerprint'] = 'isolated-input-' + str(number)
        request['used_skill_ids'] = ['owner:broken-method']
        assert store.enqueue(request) == 'queued'
    results = [service._run_first() for _ in range(3)]
    assert [result.status for result in results] == ['published', 'skipped', 'skipped']
    assert len(backend.prompts) == 3
    assert not store.pending_requests()
    assert store.load_registry().daily_calls == 3
    assert corrupt.read_bytes() == b'{bad-draft' and broken.read_bytes() == b'\xff\xfe'


def test_pre_model_material_failure_uses_bounded_attempts_without_call_budget(tmp_path, monkeypatch):
    store = _store(tmp_path)
    backend = _FakeBackend()
    service = learning.SkillLearningService(store, learning.SkillLearningSettings(),
        learning.SkillLearningRuntime(backend=backend, snapshot_provider=lambda: SimpleNamespace(entries=())))
    store.enqueue(build_learning_request(_ctx('bad-material'), 1))
    def fail(*args):
        raise ValueError('synthetic material failure')
    monkeypatch.setattr(learning, '_material', fail)
    results = [service._run_first() for _ in range(learning.MAX_REQUEST_ATTEMPT_COUNT)]
    assert [result.status for result in results] == ['retry'] * (learning.MAX_REQUEST_ATTEMPT_COUNT - 1) + ['failed']
    assert not store.pending_requests()
    assert store.load_registry().daily_calls == 0 and not backend.prompts



def test_post_model_decode_failure_advances_attempts_but_write_uncertainty_does_not(tmp_path, monkeypatch):
    store = _store(tmp_path)
    backend = _FakeBackend(*[_output() for _ in range(learning.MAX_REQUEST_ATTEMPT_COUNT + 1)])
    service = learning.SkillLearningService(store, learning.SkillLearningSettings(),
        learning.SkillLearningRuntime(backend=backend, snapshot_provider=lambda: SimpleNamespace(entries=())))
    store.enqueue(build_learning_request(_ctx('bad-decode'), 1))
    def bad_decode(*args, **kwargs):
        raise UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'synthetic corrupt Skill')
    monkeypatch.setattr(service, '_decide', bad_decode)
    results = [service._run_first() for _ in range(learning.MAX_REQUEST_ATTEMPT_COUNT)]
    assert results[-1].status == 'failed' and not store.pending_requests()
    assert store.load_registry().daily_calls == learning.MAX_REQUEST_ATTEMPT_COUNT
    store.enqueue(build_learning_request(_ctx('write-unknown'), 1))
    def unknown_write(*args, **kwargs):
        raise OSError('synthetic write uncertainty')
    monkeypatch.setattr(service, '_decide', unknown_write)
    with pytest.raises(OSError):
        service._run_first()
    pending = store.pending_requests()
    assert len(pending) == 1
    assert store.read_request(pending[0]).get('attempts', 0) == 0



@pytest.mark.parametrize('limit, expired, changed', [(50, False, False), (1, False, False),
                                                    (50, True, False), (50, False, True)])
def test_restore_with_spare_registered_capacity_retains_ownership_hash_and_grace_guards(
        tmp_path, limit, expired, changed):
    from agent_py_agent.agent.capability.skill_learning_maintenance import control_learning
    from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStoreError
    store = _store(tmp_path)
    _seed(store, 'archived-method')
    sibling = _seed(store, 'unknown-method')
    with store.state_lock():
        registry, _ = store._transition_archive_locked(store.load_registry(), 'archived-method', now=1000)
        row = registry.skills['unknown-method']
        store.save_registry(replace(registry, skills={**registry.skills,
            row.name: replace(row, maintenance={**row.maintenance, 'ownership': 'unknown'})}))
    archive = store.directory / 'archived' / 'archived-method' / 'SKILL.md'
    original_archive = archive.read_bytes()
    original_sibling = sibling.read_bytes()
    if changed:
        archive.write_bytes(b'user changed archived method')
    if limit == 50 and not expired and not changed:
        result = control_learning(store, 'archived-method', operation='restore',
            max_skills=limit, now=1001, evict_seconds=10)
        assert result['maintenance']['state'] in {'active', 'trial'}
        assert store.skill_path('archived-method').read_bytes() == original_archive
        assert not archive.exists()
    else:
        with pytest.raises((SkillLearningGateError, SkillLearningStoreError)) as exc:
            control_learning(store, 'archived-method', operation='restore', max_skills=limit,
                now=1011 if expired else 1001, evict_seconds=10)
        if expired:
            assert exc.value.code == 'SKILL_LEARNING_ARCHIVE_EXPIRED'
        elif limit == 1:
            assert exc.value.code == 'SKILL_LEARNING_EVIDENCE_INCOMPLETE'
        else:
            assert exc.value.code == 'SKILL_LEARNING_USER_MODIFIED'
        assert archive.exists() and not store.skill_path('archived-method').exists()
    assert sibling.read_bytes() == original_sibling
    assert store.load_registry().skills['unknown-method'].maintenance['ownership'] == 'unknown'
