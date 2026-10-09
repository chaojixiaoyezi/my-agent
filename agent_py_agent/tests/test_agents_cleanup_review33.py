"""Review33：真实隔离仓库与 flock，不启动 Gateway，不读真实 owner。"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.persona_repository import PersonaMutationRequest
from agent_py_agent.agent.common import json_io
from agent_py_agent.agent.memory_store import agents_migration as migration, promotion
from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
from agent_py_agent.agent.memory_store.curator_state import MemoryCuratorStateStore
from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaEnforcer
from agent_py_agent.tests.test_agents_phase0_guard import _OTHER_CHAT, _observe, _runtime


def _setup(tmp_path):
    service, conversations, threads = _runtime(tmp_path)
    repo = service.persona
    repo.mutate(PersonaMutationRequest(target="agents", action="add", content="复核证据后再交付。",
        source="memory-promotion:synthetic:review33"))
    home = SimpleNamespace(owner_home_dir=repo.owner_home, owner_agents_md=repo.path_for("agents"),
        owner_memory_candidates_jsonl=service.candidates.path,
        owner_memory_policy_json=repo.owner_home / "memory_policy.json")
    request = migration.AgentsCleanupRequest("review33", ("excluded-synthetic",),
        plan_digest=migration.plan_agents_cleanup(persona=repo).plan_digest)
    store = MemoryCuratorStateStore(repo.owner_home / "memory/curator/state.json")
    return SimpleNamespace(service=service, conversations=conversations, threads=threads,
        home=home, request=request, store=store)


def _apply(env):
    return migration.apply_agents_cleanup(home_paths=env.home, persona=env.service.persona,
        candidate_service=env.service.candidates, request=env.request, curator_state_store=env.store)


def _candidate(env, target):
    service = env.service
    source = _observe((service, env.conversations), env.threads[_OTHER_CHAT],
        "个人电脑使用 macOS。", promotion_target="long_term")
    if target == "long_term":
        return source
    candidate = service.candidates.observe(CandidateObservation(
        candidate_type="user_profile" if target == "user" else "soul_change", content=source.content,
        subject_key="profile.device.os", scope=MemoryScope("personal", "personal"), origin="user_explicit",
        source_message_refs=tuple(source.source_message_refs), promotion_target=target))
    service.review(candidate.candidate_id, approved=True, reviewer="test")
    return candidate


class _LockProbe:
    def __init__(self, env):
        self.env = env
        self.real_lock = json_io._locked_json_path
        self.local = threading.local()
        self.policy_held = threading.Event()
        self.cleanup_attempt = threading.Event()
        self.events, self.errors, self.results = [], [], {}

    @contextmanager
    def lock(self, path, *, blocking=True):
        name = threading.current_thread().name
        held = getattr(self.local, "held", [])
        if name == "r33-cleanup" and path.name == ".owner-quota":
            self.cleanup_attempt.set()
        self.events.append((name, "attempt", path.name, tuple(held)))
        manager = self._acquire(path, blocking)
        self.local.held = [*held, path.name]
        try:
            yield
        finally:
            self.local.held = held
            manager.__exit__(None, None, None)

    def _acquire(self, path, blocking):
        end = time.monotonic() + 2
        while True:
            manager = self.real_lock(path, blocking=False)
            try:
                manager.__enter__()
                return manager
            except BlockingIOError:
                if not blocking or time.monotonic() >= end:
                    raise TimeoutError(f"{path.name} blocked while holding {getattr(self.local, 'held', [])}") from None
                time.sleep(0.005)

    def checked(self, service, candidate):
        promotion._ensure_not_excluded_original(service, candidate)
        self.policy_held.set()
        # 新锁序下 cleaner 不可能取得 quota；等待真实请求，不人为要求反向顺序。
        assert self.cleanup_attempt.wait(5)

    def run(self, kind, action):
        try:
            self.results[kind] = action()
        except BaseException as exc:
            self.errors.append((kind, exc))


def _cleanup_after_concurrent_persona(env):
    try:
        return _apply(env)
    except migration.AgentsCleanupError as exc:
        if exc.reason_code != "AGENTS_CLEANUP_PLAN_STALE":
            raise
        # USER/SOUL 与 AGENTS 共用版本尾；并发真实写入后必须重新 dry-run，不能绕过 CAS。
        env.request = replace(env.request, plan_digest=migration.plan_agents_cleanup(persona=env.service.persona).plan_digest)
        return _apply(env)


@pytest.mark.parametrize("target", ["long_term", "user", "soul"])
def test_enabled_quota_promotion_cleanup_cannot_form_lock_cycle(tmp_path, monkeypatch, target):
    env = _setup(tmp_path)
    candidate = _candidate(env, target)
    enforcer = OwnerQuotaEnforcer(env.service.persona.owner_home, max_bytes=100_000_000)
    env.service.persona.quota_enforcer = env.service.long_term.quota_enforcer = enforcer
    env.service.candidates.quota_enforcer = enforcer
    env.service.long_term.candidate_service = env.service.candidates
    probe = _LockProbe(env)
    monkeypatch.setattr(promotion, "_ensure_not_excluded_original", promotion._ensure_not_excluded, raising=False)
    monkeypatch.setattr(promotion, "_ensure_not_excluded", probe.checked)
    monkeypatch.setattr(json_io, "_locked_json_path", probe.lock)
    promoter = threading.Thread(name="r33-promotion", target=probe.run, args=("promotion",
        lambda: env.service.promote(candidate.candidate_id, automatic=target == "long_term", confirmed=True)))
    cleaner = threading.Thread(name="r33-cleanup", target=probe.run,
        args=("cleanup", lambda: _cleanup_after_concurrent_persona(env)))
    promoter.start()
    try:
        assert probe.policy_held.wait(5)
        cleaner.start()
    finally:
        promoter.join(12)
        if cleaner.ident is not None:
            cleaner.join(12)
    assert not promoter.is_alive() and not cleaner.is_alive()
    assert not probe.errors, (probe.events, probe.errors)
    assert probe.results["cleanup"]["status"] == "completed"
    assert probe.results["promotion"].promoted
    policy_attempts = [held for name, event, path, held in probe.events
        if name == "r33-promotion" and event == "attempt" and path == "memory_policy.json"]
    assert policy_attempts and all(".owner-quota" in held for held in policy_attempts)


@pytest.mark.parametrize("action", ["add", "replace", "remove"])
def test_quota_admitted_conflict_replace_and_hard_delete(tmp_path, monkeypatch, action):
    env = _setup(tmp_path)
    quota = OwnerQuotaEnforcer(env.service.persona.owner_home, max_bytes=100_000_000)
    env.service.persona.quota_enforcer = env.service.long_term.quota_enforcer = quota
    env.service.candidates.quota_enforcer = quota
    env.service.long_term.candidate_service = env.service.candidates
    first = _candidate(env, "long_term")
    assert env.service.promote(first.candidate_id, automatic=True).promoted
    entry = env.service.long_term.all()[0]
    source = _observe((env.service, env.conversations), env.threads[_OTHER_CHAT],
        "个人电脑使用 Linux。", promotion_target="long_term")
    env.service.review(source.candidate_id, approved=True, reviewer="test", proposed_action=action,
        target_entry_id=entry.entry_id if action != "add" else "")
    probe = _LockProbe(env)
    monkeypatch.setattr(json_io, "_locked_json_path", probe.lock)
    result = env.service.promote(source.candidate_id)
    if action == "add":
        assert result.status == "blocked_conflict" and not result.promoted
        assert env.service.candidates.get(source.candidate_id).status == "blocked_conflict"
        return
    assert result.promoted
    if action == "remove":
        assert env.service.long_term.all() == []
        assert env.service.candidates.get(first.candidate_id).content == ""
        return
    assert env.service.long_term.all()[0].content == source.content


def test_migration_release_write_failure_cannot_report_success_and_wedge_forever(tmp_path, monkeypatch):
    env = _setup(tmp_path)
    writes = []
    def fail_commit(_commit):
        writes.append("injected")
        raise OSError("synthetic transient state release write failure")
    with monkeypatch.context() as patch:
        patch.setattr(env.store, "commit_success", fail_commit)
        result = _apply(env)
    lease = env.store.load().active_lease
    future = datetime.fromisoformat(lease["expires_at"]) + timedelta(hours=1)
    acquired = env.store.acquire(reason="migration", config_revision="test", lease_seconds=300, now=future)
    assert writes
    assert acquired is not None, "completed migration cannot leave an indefinitely protected lease"
    assert result["status"] == "completed" and result["ok"] is False
    assert result["lease_cleanup"]["release_error_class"] == "OSError"
    assert result["lease_cleanup"]["retired"] is True
