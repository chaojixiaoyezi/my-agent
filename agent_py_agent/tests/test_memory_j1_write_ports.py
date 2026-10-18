"""J1原资源/原scope的准确末写、锁和native透传；不接真实HTTP或模型。"""
from __future__ import annotations

import json
from contextvars import copy_context

import pytest

from agent_py_agent.agent.common import global_write_commit as commits
from agent_py_agent.agent.common import json_io
from agent_py_agent.agent.common.global_write_guard import GlobalWriteInWorkerError
from agent_py_agent.agent.common.owner_write_errors import (
    LocalControlCoordinatorUnavailable,
    OwnerManagementIngressUnavailable,
)
from agent_py_agent.agent.local_storage.store import LocalStore
from agent_py_agent.agent.memory_store import candidate_commit_lock as locks
from agent_py_agent.agent.memory_store import candidates as candidate_module
from agent_py_agent.agent.memory_store import operations
from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.jsonl import MemoryRecord
from agent_py_agent.agent.memory_store.owner_services import (
    ExplicitRememberServices,
    remember_explicit,
)
from agent_py_agent.agent.memory_store.promotion import (
    LocalStoreToolEvidenceVerifier,
    MemoryPromotionDependencies,
    MemoryPromotionService,
)
from agent_py_agent.agent.settings.embedding_profile import embedding_identity
from agent_py_agent.agent.user_space.owner_quota import (
    OwnerQuotaAdmission,
    OwnerQuotaEnforcer,
    OwnerQuotaUnavailable,
)
from agent_py_agent.tests.test_local_store_coordinator_io import Door
from agent_py_agent.tests.test_memory_promotion_v2 import _runtime

ERRORS = [commits.GlobalWriteCommitUnavailable, GlobalWriteInWorkerError,
          LocalControlCoordinatorUnavailable, OwnerManagementIngressUnavailable]


# LLM: 测试观察走原typed schema/身份，不手写候选成功记录；不模拟业务dispatcher。
# 函数用途: 构造原明确记忆候选以核状态机与写叶。
def observation(content="候选正文"):
    return CandidateObservation(candidate_type="long_term_fact", content=content,
        subject_key="manual.fact.test", scope=MemoryScope("personal", "personal", "本地", ""),
        origin="reviewed", confidence=1.0, proposed_action="add", promotion_target="long_term", observation_id="manual:test")


# LLM: 真实候选/配额对象，fake仅外层同步末门；scope退出不向对象保存回调。
# 函数用途: 创建C策略候选仓库，供逐writer撤权和原锁序测试。
def resources(tmp_path):
    door = Door()
    quota = OwnerQuotaEnforcer(tmp_path, max_bytes=10000000, coordinator_io=True)
    with commits.global_write_commit_scope(door):
        service = CandidateService(tmp_path / "memory/candidates.jsonl", quota_enforcer=quota, coordinator_io=True)
    return service, quota, door


@pytest.mark.parametrize("error_type", ERRORS)
@pytest.mark.parametrize("offset", [1, 2, 3])
def test_candidate_native_failure_each_quota_lock_candidate_lock_raw_writer(tmp_path, error_type, offset):
    service, _quota, door = resources(tmp_path)
    door.error = error_type("actual_original_failure")
    door.reject_at = door.calls + offset
    with commits.global_write_commit_scope(door), pytest.raises(error_type) as caught:
        service.observe(observation())
    assert caught.value is door.error
    assert door.calls == door.reject_at
    assert not service.path.exists()


def test_c_constructor_and_late_copied_scope_require_original_source(tmp_path):
    with pytest.raises(commits.GlobalWriteCommitUnavailable):
        CandidateService(tmp_path / "uncreated/candidates.jsonl", coordinator_io=True)
    assert not (tmp_path / "uncreated").exists()
    service, _quota, door = resources(tmp_path)
    with commits.global_write_commit_scope(door):
        copied = copy_context()
    with pytest.raises(commits.GlobalWriteCommitUnavailable):
        copied.run(service.list)


def test_ordinary_quota_io_error_stays_original_wrapping(tmp_path):
    service, _quota, door = resources(tmp_path)
    door.error = OSError("ordinary physical birth failure")
    door.reject_at = door.calls + 1
    with commits.global_write_commit_scope(door), pytest.raises(OwnerQuotaUnavailable) as caught:
        service.observe(observation())
    assert caught.value.__cause__ is door.error


def test_ordinary_quota_runtime_error_is_not_upgraded_to_source(tmp_path):
    service, _quota, door = resources(tmp_path)
    door.error = RuntimeError("ordinary runtime")
    door.reject_at = door.calls + 1
    with commits.global_write_commit_scope(door), pytest.raises(OwnerQuotaUnavailable) as caught:
        service.observe(observation())
    assert caught.value.__cause__ is door.error


def test_candidate_default_and_c_have_same_identity_merge_and_byte_encoding(tmp_path, monkeypatch):
    monkeypatch.setattr(candidate_module, "utc_now_iso", lambda: "2026-10-17T00:00:00+00:00")
    default = CandidateService(tmp_path / "default/candidates.jsonl")
    service, quota, door = resources(tmp_path / "coordinator")
    first = default.observe(observation())
    again = default.observe(observation())
    with commits.global_write_commit_scope(door):
        c_first = service.observe(observation())
        c_again = service.observe(observation())
    assert first == c_first and again == c_again
    assert service.path.read_bytes() == default.path.read_bytes()
    assert service.path.stat().st_mode & 0o777 == 0o600
    assert not any(callable(value) for value in vars(service).values())
    assert not any(callable(value) for value in vars(quota).values())


def test_merge_quota_scan_and_file_lock_waits_stay_outside_root(tmp_path, monkeypatch):
    service, _quota, door = resources(tmp_path)
    original_merge = candidate_module.merge_candidate_observations
    original_check = OwnerQuotaAdmission.check
    original_flock = json_io._flock_descriptor
    def merge(*args, **kwargs):
        assert not door.inside
        return original_merge(*args, **kwargs)
    def check(self, *args, **kwargs):
        assert not door.inside
        return original_check(self, *args, **kwargs)
    def flock(*args, **kwargs):
        assert not door.inside
        return original_flock(*args, **kwargs)
    monkeypatch.setattr(candidate_module, "merge_candidate_observations", merge)
    monkeypatch.setattr(OwnerQuotaAdmission, "check", check)
    monkeypatch.setattr(json_io, "_flock_descriptor", flock)
    with commits.global_write_commit_scope(door):
        service.observe(observation())


def test_locked_snapshot_reuses_original_owned_candidate_lock_for_mark(tmp_path):
    service, quota, door = resources(tmp_path)
    with commits.global_write_commit_scope(door):
        item = service.observe(observation())
        approved = service.transition(item, "approved", reviewer="test")
        with quota.admission() as admission, service.locked_snapshot(approved):
            assert service.path.resolve() in locks._OWNED.paths
            promoted = service.mark_promoted_admitted(approved.candidate_id, admission, ("test", "memory#entry", ""))
            assert promoted.status == "promoted"
        assert service.path.resolve() not in locks._OWNED.paths
        assert service.get(item.candidate_id).status == "promoted"


def test_candidate_raw_replace_failure_keeps_original_file(tmp_path, monkeypatch):
    service, _quota, door = resources(tmp_path)
    with commits.global_write_commit_scope(door):
        item = service.observe(observation())
    before = service.path.read_bytes()
    error = PermissionError("raw physical failure")
    def fail(*args, **kwargs):
        assert kwargs["replace_once"] is True
        raise error
    monkeypatch.setattr(candidate_module, "write_private_text_file_atomic_unlocked", fail)
    with commits.global_write_commit_scope(door), pytest.raises(PermissionError) as caught:
        service.transition(item, "approved", reviewer="test")
    assert caught.value is error
    assert service.path.read_bytes() == before


def test_c_ops_matches_default_byte_format_and_original_4096_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(operations.time, "time", lambda: 100.0)
    record = MemoryRecord(role="user", content="正文", attributes={}, entry_id="e", version=1, action="add", source="test")
    first, second = tmp_path / "default/ops.jsonl", tmp_path / "c/ops.jsonl"
    operations.append_memory_operation_events(first, [record])
    operations.append_memory_purge_event(first, entry_ids={"e"}, content_hashes={"hash"})
    door = Door()
    with commits.global_write_commit_scope(door):
        operations.append_memory_operation_events(second, [record], coordinator_io=True)
        operations.append_memory_purge_event(second, entry_ids={"e"}, content_hashes={"hash"}, coordinator_io=True)
    assert first.read_bytes() == second.read_bytes()
    assert operations._MAX_OPERATION_EVENTS == 4096
    with commits.global_write_commit_scope(door):
        for number in range(5):
            operations._append_operation_record(second, {"n": number}, max_records=3, coordinator_io=True)
    assert [json.loads(line)["n"] for line in second.read_text().splitlines()] == [2, 3, 4]


@pytest.mark.parametrize("offset", [1, 2, 3])
def test_ops_each_directory_lock_raw_writer_rejects_same_native(tmp_path, offset):
    target = tmp_path / "ops.jsonl"
    target.write_text('{"old": true}\n', encoding="utf-8")
    door = Door()
    door.reject_at = offset
    with commits.global_write_commit_scope(door), pytest.raises(LocalControlCoordinatorUnavailable) as caught:
        operations.append_memory_purge_event(target, entry_ids={"e"}, content_hashes={"h"}, coordinator_io=True)
    assert caught.value is door.error
    assert target.read_text() == '{"old": true}\n'


def test_default_candidate_quota_policy_ops_never_enter_new_global_guard(tmp_path, monkeypatch):
    def forbidden(*args):
        raise AssertionError("default entered C global guard")
    monkeypatch.setattr(commits, "require_global_writer", forbidden)
    quota = OwnerQuotaEnforcer(tmp_path, max_bytes=10000000)
    service = CandidateService(tmp_path / "candidates.jsonl", quota_enforcer=quota)
    item = service.observe(observation())
    service.transition(item, "approved", reviewer="test")
    operations.append_memory_purge_event(tmp_path / "ops.jsonl", entry_ids={"e"}, content_hashes={"h"})


# LLM: 用原完整promotion fixture，只将获批J1资源换为真实C策略；formal/JSONL仍未接C，测试不声称入口ready。
# 函数用途: 验证原领域锁/付费顺序不因末门拆分变化。
def promotion_resources(tmp_path):
    old, conversations, _thread = _runtime(tmp_path)
    candidates, quota, door = resources(tmp_path / "c-j1")
    old.long_term.quota_enforcer = quota
    deps = MemoryPromotionDependencies(candidates=candidates, long_term=old.long_term,
        persona=old.persona, lessons=old.lessons, hot=old.hot, message_verifier=old.message_verifier,
        tool_verifier=old.tool_verifier, exclusion_store=conversations)
    service = MemoryPromotionService(dependencies=deps, coordinator_io=True)
    return service, quota, door


def test_original_locks_hold_across_same_sync_embedding_outside_root(tmp_path):
    service, quota, door = promotion_resources(tmp_path)
    seen = []
    class Embedder:
        protocol = "openai_compatible_embeddings"
        model = "j1-offline"
        api_base = "https://embedding.example.invalid/v1"
        dim = 2
        def embed(self, texts):
            assert not door.inside
            assert service.candidates.path.resolve() in locks._OWNED.paths
            assert json_io._path_lock(quota._lock_anchor).lock.locked()
            seen.extend(texts)
            return [[1.0, 0.0] for _ in texts]
    service.long_term._embedder = Embedder()
    service.long_term._vector_identity = embedding_identity("j1-offline", service.long_term._embedder)
    with commits.global_write_commit_scope(door):
        saved = remember_explicit(ExplicitRememberServices(service.candidates, service, service.long_term), "明确记忆事实")
    assert seen == ["明确记忆事实"] and saved.content == "明确记忆事实"


@pytest.mark.parametrize("error_type", ERRORS)
def test_condense_actual_native_is_not_washed_into_promoted_success(tmp_path, monkeypatch, error_type):
    service, _quota, door = promotion_resources(tmp_path)
    error = error_type("actual condense source failure")
    def fail():
        raise error
    monkeypatch.setattr(service.long_term, "condense", fail)
    with commits.global_write_commit_scope(door), pytest.raises(error_type) as caught:
        remember_explicit(ExplicitRememberServices(service.candidates, service, service.long_term), "原正式写已完成")
    assert caught.value is error
    assert service.long_term.all()
    assert json.loads(service.candidates.path.read_text().splitlines()[0])["status"] == "promoted"


def test_ordinary_condense_oserror_keeps_original_success(tmp_path, monkeypatch):
    service, _quota, door = promotion_resources(tmp_path)
    def fail():
        raise OSError("ordinary condense")
    monkeypatch.setattr(service.long_term, "condense", fail)
    with commits.global_write_commit_scope(door):
        saved = remember_explicit(ExplicitRememberServices(service.candidates, service, service.long_term), "原普通治理失败")
    assert saved.content == "原普通治理失败"


@pytest.mark.parametrize("error_type", ERRORS)
def test_actual_c_ledger_evidence_source_failure_cannot_become_missing_evidence(tmp_path, error_type):
    error = error_type("actual ledger birth")
    door = Door()
    with commits.global_write_commit_scope(door):
        store = LocalStore(tmp_path / "ledger.db", coordinator_io=True)
    door.error = error
    door.reject_at = door.calls + 1
    verifier = LocalStoreToolEvidenceVerifier(store, owner_id="user")
    with commits.global_write_commit_scope(door), pytest.raises(error_type) as caught:
        verifier.verify({"owner_id": "user", "run_id": "r", "operation_id": "op"})
    assert caught.value is error


def test_common_source_errors_are_same_gateway_public_classes():
    from agent_py_agent.agent.gateway_parts.multiprocess_local_control import (
        LocalControlCoordinatorUnavailable as old_control,
    )
    from agent_py_agent.agent.gateway_parts.owner_management_control import (
        OwnerManagementIngressUnavailable as old_ingress,
    )
    assert old_control is LocalControlCoordinatorUnavailable
    assert old_ingress is OwnerManagementIngressUnavailable
