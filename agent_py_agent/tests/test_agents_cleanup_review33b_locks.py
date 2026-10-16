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
from agent_py_agent.tests.memory_confirmation_fixtures import confirm_candidate
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

def apply(probe, request):
    return migration.apply_agents_cleanup(home_paths=probe.home, persona=probe.service.persona,
        candidate_service=probe.service.candidates, request=request, curator_state_store=probe.store)


def _promotion_candidate(runtime, target):
    service, conversations, threads, _home, _request, _store = runtime
    source = _observe((service, conversations), threads[_OTHER_CHAT],
        '个人电脑使用 macOS。', promotion_target='long_term')
    candidate = source
    if target == 'user':
        candidate = service.candidates.observe(CandidateObservation(candidate_type='user_profile',
            content=source.content, subject_key='profile.device.os', scope=MemoryScope('personal', 'personal'),
            origin='user_explicit', source_message_refs=tuple(source.source_message_refs), promotion_target='user'))
        service.review(candidate.candidate_id, approved=True, reviewer='test')
    return candidate


def _try_real_lock(original_lock, path):
    manager = original_lock(path, blocking=False)
    try:
        manager.__enter__()
        return manager, None
    except BlockingIOError as error:
        return None, error


class _QuotaCycleProbe:
    def __init__(self, runtime, target):
        self.service, _conversations, _threads, self.home, self.request, self.store = runtime
        self.candidate = _promotion_candidate(runtime, target)
        self.target = target
        enforcer = OwnerQuotaEnforcer(self.service.persona.owner_home, max_bytes=100_000_000)
        self.service.persona.quota_enforcer = self.service.long_term.quota_enforcer = enforcer
        self.policy_held, self.cleanup_quota, self.promotion_attempt = (threading.Event() for _ in range(3))
        self.original_check, self.original_lock = promotion._ensure_not_excluded, json_io._locked_json_path
        self.events, self.results, self.errors = [], {}, []
        self.quota = self.service.persona.owner_home / '.owner-quota'
        self.local = threading.local()

    def _wait_for_lock(self, path, blocking):
        end = time.monotonic() + 2
        while True:
            manager, error = _try_real_lock(self.original_lock, path)
            if manager is not None:
                return manager
            if not blocking or time.monotonic() >= end:
                name = threading.current_thread().name
                held = getattr(self.local, 'held', [])
                raise TimeoutError(f'{name} waits for {path.name} while holding {held}') from error
            time.sleep(0.005)

    @contextmanager
    def bounded_real_lock(self, path, *, blocking=True):
        name = threading.current_thread().name
        held = getattr(self.local, 'held', [])
        if name == 'r33-promotion' and path == self.quota:
            self.promotion_attempt.set()
        if name == 'r33-cleanup' and path == self.quota:
            self.cleanup_quota.set()
        self.events.append({'thread': name, 'event': 'attempt', 'path': path.name, 'held': list(held)})
        manager = self._wait_for_lock(path, blocking)
        self.local.held = [*held, path.name]
        self.events.append({'thread': name, 'event': 'acquired', 'path': path.name, 'held': list(held)})
        try:
            if name == 'r33-cleanup' and path == self.quota:
                self.cleanup_quota.set()
                assert self.promotion_attempt.wait(5), 'promotion did not reach quota'
            yield
        finally:
            self.local.held = held
            manager.__exit__(None, None, None)

    def after_policy_check(self, *args):
        self.original_check(*args)
        self.policy_held.set()
        assert self.cleanup_quota.wait(5), 'cleanup did not reach quota'

    def promote(self):
        try:
            self.results['promotion'] = asdict(confirm_candidate(self.service, self.candidate.candidate_id) if self.target == 'long_term' else self.service.promote(self.candidate.candidate_id))
        except BaseException as exc:
            self.errors.append({'thread': 'promotion', 'type': type(exc).__name__, 'error': str(exc)})

    def _apply_with_stale_replan(self):
        try:
            return apply(self, self.request)
        except migration.AgentsCleanupError as exc:
            if exc.reason_code != 'AGENTS_CLEANUP_PLAN_STALE':
                raise
            fresh = migration.plan_agents_cleanup(persona=self.service.persona).plan_digest
            return apply(self, replace(self.request, plan_digest=fresh))

    def cleanup(self):
        try:
            self.results['cleanup'] = self._apply_with_stale_replan()
        except BaseException as exc:
            self.errors.append({'thread': 'cleanup', 'type': type(exc).__name__, 'error': str(exc)})

    def install(self, monkeypatch):
        monkeypatch.setattr(json_io, '_locked_json_path', self.bounded_real_lock)
        monkeypatch.setattr(promotion, '_ensure_not_excluded', self.after_policy_check)


@pytest.mark.parametrize('target', ['long_term', 'user'])
def test_enabled_quota_promotion_cleanup_cannot_form_lock_cycle(tmp_path, monkeypatch, target):
    probe = _QuotaCycleProbe(setup(tmp_path), target)
    events, results, errors, policy_held = probe.events, probe.results, probe.errors, probe.policy_held
    probe.install(monkeypatch)
    promoter = threading.Thread(target=probe.promote, name='r33-promotion')
    cleaner = threading.Thread(target=probe.cleanup, name='r33-cleanup')
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
