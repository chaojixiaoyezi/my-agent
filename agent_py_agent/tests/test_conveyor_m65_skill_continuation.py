import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability import asset_promotion
from agent_py_agent.agent.capability.skill_learning import (
    SkillLearningRuntime,
    SkillLearningService,
    SkillLearningSettings,
)
from agent_py_agent.agent.capability.skill_learning_prompt import parse_skill_learning_decision
from agent_py_agent.agent.capability.skill_learning_proposals import list_learning_proposals
from agent_py_agent.agent.capability.skill_learning_publish import (
    PublishRequest,
    publish_learned_skill,
)
from agent_py_agent.agent.capability.skill_learning_request import build_learning_request
from agent_py_agent.agent.capability.skill_learning_store import (
    SkillLearningRegistry,
    SkillLearningStore,
)
from agent_py_agent.agent.capability.skill_learning_validation import (
    default_machine_promotion_verifier,
)
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.tests.test_skill_learning import BODY, NAME, _ctx, _FakeBackend, _output


def _runtime(tmp_path, backend):
    store = SkillLearningStore.for_home(ensure_my_agent_home(tmp_path / 'owner'))
    root = tmp_path / 'deployment'
    settings = SkillLearningSettings(autonomous_verified=True, maintenance_interval_seconds=0)
    runtime = SkillLearningRuntime(backend=backend, deployment_root=root,
                                   snapshot_provider=lambda: SimpleNamespace(entries=()))
    return store, root, settings, runtime


def test_resume_after_merge_result_uses_one_merge_and_one_quota_admission(tmp_path, monkeypatch):
    first = _output(name='csv-merge-copy')
    merged = _output(decision='update', update_target=NAME, body=BODY + '\n\n## Extra check\n\nInspect the header.')
    backend = _FakeBackend(first, merged, first, merged)
    store, root, settings, runtime = _runtime(tmp_path, backend)
    seed = PublishRequest(parse_skill_learning_decision(_output(), frozenset()), {'run_id': 'seed'}, frozenset())
    publish_learned_skill(store, seed)
    service = SkillLearningService(store, settings, runtime)
    assert service.enqueue_from_finalize(_ctx('merge-run')) == 'queued'

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(asset_promotion, 'propose_or_promote_skill', interrupt)
        with pytest.raises(KeyboardInterrupt):
            service.run_pending()
    assert len(backend.prompts) == 2
    resumed = SkillLearningService(store, replace(settings, daily_limit=1, global_daily_limit=1), runtime).run_pending()
    assert resumed.status == 'updated'
    assert (len(backend.prompts), store.load_registry().daily_calls,
            json.loads((root / 'data/skill_learning_budget.json').read_text())['calls']) == (2, 1, 1)
    assert store.load_registry().skills[NAME].version == 2


def test_automatic_resumable_verification_stays_queued(tmp_path):
    backend = _FakeBackend(_output(), _output())
    store, _, settings, runtime = _runtime(tmp_path, backend)
    results = []

    def verify(publish, sha):
        results.append(sha)
        if len(results) == 1:
            return {'outcome': 'resumable', 'reason': 'verification_pending'}
        return {'status': 'passed', 'executed': True, 'contract_valid': True,
                'receipt_id': 'verifier-test', 'sha256': sha, 'output': {}}

    service = SkillLearningService(store, settings, replace(runtime, promotion_verifier=verify))
    assert service.enqueue_from_finalize(_ctx('verify-run')) == 'queued'
    assert service.run_pending().status == 'retry'
    assert store.pending_requests() and not list_learning_proposals(store)
    assert service.run_pending().status == 'published'
    assert len(backend.prompts) == 1 and results[0] == results[1]


def test_verifier_permanent_rejection_routes_by_persisted_source(tmp_path):
    text = _output(body='Inspect the header and compare each column with the expected names. ' * 4)
    decision = parse_skill_learning_decision(text, frozenset())
    receipt = default_machine_promotion_verifier(PublishRequest(decision))
    assert (receipt['outcome'], receipt['reason']) == ('permanent_reject', 'skill_procedure_unstructured')
    backend = _FakeBackend(text, text)
    store, _, settings, runtime = _runtime(tmp_path, backend)
    service = SkillLearningService(store, settings, runtime)
    assert service.enqueue_from_finalize(_ctx('automatic')) == 'queued'
    assert service.run_pending().status == 'rejected'
    assert list_learning_proposals(store) == []
    explicit = build_learning_request(_ctx('explicit'), 3)
    assert store.enqueue({**explicit, 'requested_by': 'user'}) == 'queued'
    assert service.run_pending().status == 'proposed'
    [proposal] = list_learning_proposals(store)
    assert proposal['request']['requested_by'] == 'user'
    assert store.load_registry().skills == {}


def test_admission_recovers_between_deployment_and_owner_writes(tmp_path, monkeypatch):
    backend = _FakeBackend(_output())
    store, root, settings, runtime = _runtime(tmp_path, backend)
    settings = replace(settings, daily_limit=1, global_daily_limit=1)
    service = SkillLearningService(store, settings, runtime)
    assert service.enqueue_from_finalize(_ctx('admission')) == 'queued'
    [path] = store.pending_requests()
    original = store.read_request(path)

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(store, '_record_daily_admission_locked', interrupt)
        with pytest.raises(KeyboardInterrupt):
            service.run_pending()
    budget_path = root / 'data/skill_learning_budget.json'
    assert json.loads(budget_path.read_text())['calls'] == 1
    assert store.load_registry().daily_calls == 0 and backend.prompts == []
    assert store.read_request(path)['attempt_id'] == original['attempt_id']
    restarted = SkillLearningStore(store.directory, store.skills_root)
    assert SkillLearningService(restarted, settings, runtime).run_pending().status == 'published'
    assert restarted.load_registry().daily_calls == 1 and len(backend.prompts) == 1
    assert json.loads(budget_path.read_text())['calls'] == 1
    assert json.loads(budget_path.read_text())['pending'] == {}


def test_merge_with_unconfirmed_result_does_not_dispatch_again(tmp_path):
    backend = _FakeBackend(_output(name='csv-merge-copy'), KeyboardInterrupt())
    store, _, settings, runtime = _runtime(tmp_path, backend)
    publish_learned_skill(store, PublishRequest(parse_skill_learning_decision(_output(), frozenset())))
    service = SkillLearningService(store, settings, runtime)
    assert service.enqueue_from_finalize(_ctx('merge-outcome')) == 'queued'
    with pytest.raises(KeyboardInterrupt):
        service.run_pending()
    [path] = store.pending_requests()
    pending = store.read_request(path)
    assert pending['stage'] == 'merge_inflight'
    assert pending['merge_target']['bindings'][0][0] == NAME
    result = SkillLearningService(store, settings, runtime).run_pending()
    assert result.code == 'SKILL_LEARNING_MERGE_DEFERRED' and result.effect_outcome == 'unknown'
    assert len(backend.prompts) == 2 and store.load_registry().daily_calls == 1


def test_registry_migrates_m52_protection_scan_on_next_save(tmp_path):
    store, _, _, _ = _runtime(tmp_path, _FakeBackend())
    scan = {'generation': 'source-generation', 'after': {'scheduler': 25}}
    payload = SkillLearningRegistry(import_operations={'protection_scan': scan, 'other': {}}).to_record()
    payload.pop('protection_scan')
    with store.state_lock():
        store.directory.mkdir(parents=True, exist_ok=True)
        store.registry_path.write_text(json.dumps(payload))
        record = store.load_registry()
        assert record.protection_scan == scan
        store.save_registry(replace(record, daily_calls=1))
    updated = json.loads(store.registry_path.read_text())
    assert updated['protection_scan'] == scan and updated['import_operations'] == {'other': {}}
