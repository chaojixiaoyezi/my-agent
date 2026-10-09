import json
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.persona_repository import PersonaMutationRequest
from agent_py_agent.agent.common import json_io
from agent_py_agent.agent.memory_store import agents_migration as migration
from agent_py_agent.agent.memory_store import promotion
from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
from agent_py_agent.agent.memory_store.curator_state import MemoryCuratorStateStore
from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaEnforcer
from agent_py_agent.tests.test_agents_phase0_guard import _OTHER_CHAT, _curator, _observe, _runtime

ROOT = Path(__file__).parent

RULE = '复核证据后再交付。'

def save(name, payload):
    (ROOT / (name + '.json')).write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str))

def setup(tmp_path):
    service, conversations, threads = _runtime(tmp_path)
    repo = service.persona
    repo.mutate(PersonaMutationRequest(target='agents', action='add', content=RULE,
        source='memory-promotion:synthetic:review33'))
    home = SimpleNamespace(owner_home_dir=repo.owner_home, owner_agents_md=repo.path_for('agents'),
        owner_memory_candidates_jsonl=service.candidates.path,
        owner_memory_policy_json=repo.owner_home / 'memory_policy.json')
    request = migration.AgentsCleanupRequest('review33', ('excluded-synthetic',),
        plan_digest=migration.plan_agents_cleanup(persona=repo).plan_digest)
    store = MemoryCuratorStateStore(repo.owner_home / 'memory/curator/state.json')
    return service, conversations, threads, home, request, store

def apply(service, home, request, store):
    return migration.apply_agents_cleanup(home_paths=home, persona=service.persona,
        candidate_service=service.candidates, request=request, curator_state_store=store)

@pytest.mark.parametrize('target', ['long_term', 'user'])
def test_enabled_quota_promotion_cleanup_cannot_form_lock_cycle(tmp_path, monkeypatch, target):
    service, conversations, threads, home, request, store = setup(tmp_path)
    source = _observe((service, conversations), threads[_OTHER_CHAT],
        '个人电脑使用 macOS。', promotion_target='long_term')
    candidate = source
    if target == 'user':
        candidate = service.candidates.observe(CandidateObservation(candidate_type='user_profile',
            content=source.content, subject_key='profile.device.os', scope=MemoryScope('personal', 'personal'),
            origin='user_explicit', source_message_refs=tuple(source.source_message_refs), promotion_target='user'))
        service.review(candidate.candidate_id, approved=True, reviewer='test')
    enforcer = OwnerQuotaEnforcer(service.persona.owner_home, max_bytes=100_000_000)
    service.persona.quota_enforcer = service.long_term.quota_enforcer = enforcer
    policy_held, cleanup_quota, promotion_attempt = (threading.Event() for _ in range(3))
    original_check, original_lock = promotion._ensure_not_excluded, json_io._locked_json_path
    events, results, errors = [], {}, []
    quota = service.persona.owner_home / '.owner-quota'
    policy = home.owner_memory_policy_json
    local = threading.local()

    @contextmanager
    def bounded_real_lock(path, *, blocking=True):
        name = threading.current_thread().name
        held = getattr(local, 'held', [])
        if name == 'r33-promotion' and path == quota:
            promotion_attempt.set()
        if name == 'r33-cleanup' and path == quota:
            cleanup_quota.set()
        events.append({'thread': name, 'event': 'attempt', 'path': path.name, 'held': list(held)})
        end = time.monotonic() + 2
        while True:
            manager = original_lock(path, blocking=False)
            try:
                manager.__enter__()
                break
            except BlockingIOError:
                if not blocking or time.monotonic() >= end:
                    raise TimeoutError(f'{name} waits for {path.name} while holding {held}')  # noqa: B904 原探针异常链与测试 AST 不变
                time.sleep(0.005)
        local.held = [*held, path.name]
        events.append({'thread': name, 'event': 'acquired', 'path': path.name, 'held': list(held)})
        try:
            if name == 'r33-cleanup' and path == quota:
                cleanup_quota.set()
                assert promotion_attempt.wait(5), 'promotion did not reach quota'
            yield
        finally:
            local.held = held
            manager.__exit__(None, None, None)

    def after_policy_check(*args):
        original_check(*args)
        policy_held.set()
        assert cleanup_quota.wait(5), 'cleanup did not reach quota'

    def promote():
        try:
            results['promotion'] = asdict(service.promote(candidate.candidate_id, automatic=target == 'long_term'))
        except BaseException as exc:
            errors.append({'thread': 'promotion', 'type': type(exc).__name__, 'error': str(exc)})

    def cleanup():
        try:
            try:
                results['cleanup'] = apply(service, home, request, store)
            except migration.AgentsCleanupError as exc:
                if exc.reason_code != 'AGENTS_CLEANUP_PLAN_STALE':
                    raise
                fresh = migration.plan_agents_cleanup(persona=service.persona).plan_digest
                results['cleanup'] = apply(service, home, replace(request, plan_digest=fresh), store)
        except BaseException as exc:
            errors.append({'thread': 'cleanup', 'type': type(exc).__name__, 'error': str(exc)})

    monkeypatch.setattr(json_io, '_locked_json_path', bounded_real_lock)
    monkeypatch.setattr(promotion, '_ensure_not_excluded', after_policy_check)
    promoter = threading.Thread(target=promote, name='r33-promotion')
    cleaner = threading.Thread(target=cleanup, name='r33-cleanup')
    promoter.start()
    assert policy_held.wait(5)
    cleaner.start()
    promoter.join(12)
    cleaner.join(12)
    observation = {'events': events, 'errors': errors, 'results': results,
        'both_finished': not promoter.is_alive() and not cleaner.is_alive(),
        'instrumentation': 'real locks; bounded 2s acquisition replaces indefinite wait only for safe diagnostic teardown'}
    save('quota-cycle-' + target, observation)
    assert observation['both_finished']
    assert not errors, observation
    assert results['cleanup']['status'] == 'completed' and results['promotion']['promoted']
    attempts = [e for e in events if e['thread'] == 'r33-promotion' and e['event'] == 'attempt' and e['path'] == 'memory_policy.json']
    assert attempts and all('.owner-quota' in e['held'] for e in attempts), observation

@pytest.fixture(autouse=True)
def _isolated_probe_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
