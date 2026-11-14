"""Wave 3 consolidated tests for shard c4 (test_consolidated_c4_g1.py).

Sources:
- agent_py_agent/tests/test_sm_rework_lifecycle_sync.py
- agent_py_agent/tests/test_sm_rework_quality.py
- agent_py_agent/tests/test_sm_w1_store_transitions.py
- agent_py_agent/tests/test_sm_w2_bounded_maintenance.py
- agent_py_agent/tests/test_sm_w5_carrier_hygiene.py
- agent_py_agent/tests/test_sm_w6_lesson_repository.py
- agent_py_agent/tests/test_sm_w7_recall_bounds.py
"""

from __future__ import annotations

import copy
import errno
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RuntimeContextRequest
from agent_py_agent.agent.agent_core.runtime.loop_support import (
    _confirm_final_memory_access,
    _formal_memories_for_request,
    _prepare_runtime_context,
    _refresh_recall_candidates,
)
from agent_py_agent.agent.capability import (
    skill_learning_maintenance as maintenance,
)
from agent_py_agent.agent.capability import (
    skill_learning_proposals,
    skill_proposals,
)
from agent_py_agent.agent.capability import (
    skill_learning_store as store_module,
)
from agent_py_agent.agent.capability.asset_promotion import register_asset
from agent_py_agent.agent.capability.skill_learning_host_feedback import (
    observe_command_verification,
)
from agent_py_agent.agent.capability.skill_learning_maintenance import (
    control_learning,
    learning_reconcile_due,
    maintain_learning,
    recommended_learning,
)
from agent_py_agent.agent.capability.skill_learning_migration import adopt_committed_lessons
from agent_py_agent.agent.capability.skill_learning_promotion import (
    promote_verified_skill,
    propose_or_promote_skill,
)
from agent_py_agent.agent.capability.skill_learning_prompt import SkillLearningDecision
from agent_py_agent.agent.capability.skill_learning_proposals import (
    list_learning_proposals,
    propose_learning,
    prune_learning_proposals,
    resolve_learning_proposal,
)
from agent_py_agent.agent.capability.skill_learning_protection import learning_protection_for_agent
from agent_py_agent.agent.capability.skill_learning_publish import (
    CODE_FAMILY_BLOCKED,
    CODE_LEARNING_DISABLED,
    CODE_LIMIT_REACHED,
    PublishRequest,
    SkillLearningGateError,
    _retire_third_strike_locked,
    _rollback_to_proven_stable_locked,
    publish_learned_skill,
    remove_learned_skill,
    render_learned_skill,
)
from agent_py_agent.agent.capability.skill_learning_report import (
    apply_learned_skill_action,
    learned_skills_report,
)
from agent_py_agent.agent.capability.skill_learning_store import (
    CODE_EVIDENCE_INCOMPLETE,
    CODE_MAINTENANCE_OBSERVE_ONLY,
    MAX_PENDING_REQUESTS,
    MISSING,
    LearnedSkill,
    SkillLearningRegistry,
    SkillLearningStore,
    SkillLearningStoreError,
    is_proven_stable_version,
    skill_family_fingerprint,
)
from agent_py_agent.agent.capability.skill_learning_validation import _validate_skill_procedure
from agent_py_agent.agent.capability.skill_proposals import (
    PROPOSAL_COMMITTED,
    PROPOSAL_REJECTED,
    SkillProposalError,
    SkillProposalService,
    lesson_carrier,
    render_lesson_draft,
)
from agent_py_agent.agent.capability.skill_snapshot import skill_content_sha256
from agent_py_agent.agent.capability.skill_usage_receipts import (
    aggregate_key,
    cleanup_retired_aggregates,
    has_body_delivery_receipt,
    read_usage_aggregate,
    receipt_key,
    record_exact_negative,
    record_exact_use,
)
from agent_py_agent.agent.common import global_write_commit as commits
from agent_py_agent.agent.common.nofollow_fs import NoFollowPathError
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.memory_routing import RoutedMemoryContext
from agent_py_agent.agent.memory_store import (
    _jsonl_indexing as indexing_module,
)
from agent_py_agent.agent.memory_store import (
    jsonl as jsonl_module,
)
from agent_py_agent.agent.memory_store import (
    lessons as lesson_module,
)
from agent_py_agent.agent.memory_store.candidate_models import _MAX_CONTENT_CHARS, MemoryCandidate
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.generation_manifest import ContextBudget
from agent_py_agent.agent.memory_store.jsonl import (
    _SCOPE_PAIRS_CACHE_MAX_ENTRIES,
    JsonlMemory,
    MemoryRecord,
    MemorySubjectConflict,
)
from agent_py_agent.agent.memory_store.lessons import (
    HOT_SCHEMA_VERSION,
    LESSON_SCHEMA_VERSION,
    HotRuleRecord,
    HotRuleRepository,
    LessonHeatUnavailableError,
    LessonRecord,
    LessonRepository,
    LessonRoutingPendingError,
    _render_hot,
    _render_lesson,
)
from agent_py_agent.agent.memory_store.operations import memory_content_hash
from agent_py_agent.agent.memory_store.recall import (
    MemoryRecallScope,
    hot_memory_records,
    routed_lesson_records,
    runtime_long_term_scope,
)
from agent_py_agent.agent.prompting_parts.memory_context import BudgetedMemoryRecords
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import runtime_db_path
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.manager_runner_result_payload import RecordRunnerResultParams
from agent_py_agent.agent.subagents.models import SubAgentParsedOutput, SubAgentTask
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaExceeded, OwnerQuotaProjection
from agent_py_agent.tests.test_local_store_coordinator_io import Door
from agent_py_agent.tests.test_skill_learning import BODY
from agent_py_agent.tests.test_skill_proposals import seed_proposals_from_candidates
from agent_py_agent.tests.test_sm_w2_usage_strikes import (
    _publish,
    _record_real_use,
    _store_with_runtime,
)
from agent_py_agent.tests.test_wave7_autonomous_learning_closure import decision

# === Consolidated from: agent_py_agent/tests/test_sm_rework_lifecycle_sync.py ===
# 生命周期发布前后单次同步错误的登记保留与冷启恢复回归。

def _prepare_case(tmp_path: Path, kind: str):
    home, repo, store = _store_with_runtime(tmp_path)
    _publish(store, "alpha")
    original = store.load_registry().skills["alpha"]
    if kind in {"restore", "evict"}:
        with store.state_lock():
            store._transition_archive_locked(store.load_registry(), "alpha", now=10.0)
    elif kind == "rollback":
        _publish(store, "alpha", suffix="v2", update_from=original)
    return home, repo, store, original


def _mutate(store: SkillLearningStore, kind: str, original: LearnedSkill) -> None:
    registry = store.load_registry()
    if kind == "archive":
        store._transition_archive_locked(registry, "alpha")
    elif kind == "restore":
        store._transition_restore_locked(registry, "alpha")
    elif kind == "evict":
        store._transition_evict_locked(registry, "alpha")
    elif kind == "remove":
        store._transition_remove_locked(registry, "alpha")
    elif kind in {"displace_first", "displace_second"}:
        text = render_learned_skill(decision("gamma"))
        candidate = LearnedSkill(
            "gamma", 1, skill_content_sha256(text),
            "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z",
        )
        store._transition_displace_publish_locked(
            registry, victim_name="alpha", candidate_record=candidate, candidate_text=text,
        )
    else:
        assert kind == "rollback"
        current = registry.skills["alpha"]
        target = store.read_version("alpha", 1)
        assert target is not None
        store._transition_rollback_locked(
            registry,
            current=current,
            next_record=replace(current, version=1, revision=current.revision + 1, sha256=original.sha256),
            target_version=1,
            target_sha256=original.sha256,
            target_text=target,
        )


def _published_layout(store: SkillLearningStore, kind: str, original: LearnedSkill) -> bool:
    live = store.skill_path("alpha", record=original)
    archived = store.archived_skill_path("alpha")
    if kind in {"archive", "displace_first"}:
        return archived.exists() and not live.exists()
    if kind == "restore":
        return live.exists() and not archived.exists()
    if kind in {"evict", "remove"}:
        source = archived if kind == "evict" else live
        return not source.exists() and store.removed_dir.exists() and bool(tuple(store.removed_dir.iterdir()))
    if kind == "displace_second":
        return store.skill_path("gamma").exists() and archived.exists()
    assert kind == "rollback"
    return live.exists() and skill_content_sha256(live.read_text(encoding="utf-8")) == original.sha256


def _assert_pending_layout(store: SkillLearningStore, registry: SkillLearningRegistry, kind: str) -> None:
    """未决意图须准确解释正文位置与旧登记之间的差异，不能把中间态当作干净登记。"""
    op = registry.pending_operation
    assert op is not None
    current = registry.skills["alpha"]
    assert op["expected_sha256"] == current.sha256
    assert op["expected_revision"] == current.revision
    assert op["phase"] == ("moved" if kind == "displace_second" else "prepared")
    root, parts = store._resolve_op_rel(str(op["target_rel"]))
    target = root.joinpath(*parts)
    if kind == "rollback":
        assert skill_content_sha256((target / "SKILL.md").read_text(encoding="utf-8")) == op["candidate_sha256"]
    else:
        src_root, src_parts = store._resolve_op_rel(str(op["source_rel"]))
        assert not src_root.joinpath(*src_parts).exists()
        assert skill_content_sha256((target / "SKILL.md").read_text(encoding="utf-8")) == current.sha256
    if kind in {"displace_first", "displace_second"}:
        assert "gamma" not in registry.skills
        if kind == "displace_first":
            stage_root, stage_parts = store._resolve_op_rel(str(op["candidate_rel"]))
            candidate_path = stage_root.joinpath(*stage_parts, "SKILL.md")
            assert not store.skill_path("gamma").exists()
        else:
            candidate_path = store.skill_path("gamma")
        assert skill_content_sha256(candidate_path.read_text(encoding="utf-8")) == op["candidate_sha256"]
    with pytest.raises(SkillLearningStoreError) as exc_info:
        store.managed_records(registry, include_archived=True)
    assert exc_info.value.code == CODE_EVIDENCE_INCOMPLETE
    assert exc_info.value.detail["operation_id"] == op["operation_id"]


def _assert_committed_layout(store: SkillLearningStore, registry: SkillLearningRegistry) -> None:
    assert registry.pending_operation is None
    for name, record in registry.skills.items():
        path = store.canonical_skill_path(name, record=record)
        assert path.is_file()
        assert skill_content_sha256(path.read_text(encoding="utf-8")) == record.sha256
        assert record.maintenance.get("ownership") not in {"unknown", "user_modified"}
    assert store.managed_records(registry, include_archived=True) == registry.skills
    assert not store.skill_path("gamma").exists() or "gamma" in registry.skills


@pytest.mark.parametrize(
    ("kind", "sync_number"),
    [
        (kind, sync_number)
        for kind in ("archive", "restore", "displace_first", "displace_second", "evict", "remove")
        for sync_number in (1, 2)
    ] + [("rollback", 1)],
)
def test_post_mutation_sync_error_preserves_pending_and_recovers(tmp_path: Path, monkeypatch, kind, sync_number):
    home, repo, store, original = _prepare_case(tmp_path, kind)
    baseline = store.load_registry()
    real_fsync = store_module.os.fsync
    matching_syncs = 0
    pending_at_error = None

    def fsync_once(fd):
        nonlocal matching_syncs, pending_at_error
        if _published_layout(store, kind, original):
            matching_syncs += 1
            if matching_syncs == sync_number:
                pending_at_error = store.load_registry().pending_operation
                raise OSError(errno.EIO, "simulated I/O error")
        return real_fsync(fd)

    with monkeypatch.context() as scoped:
        scoped.setattr(store_module.os, "fsync", fsync_once)
        with pytest.raises(OSError) as exc_info:
            with store.state_lock():
                _mutate(store, kind, original)
    assert exc_info.value.errno == errno.EIO
    assert matching_syncs >= sync_number
    assert pending_at_error is not None
    before = store.load_registry()
    assert before.pending_operation == pending_at_error
    assert before.skills == baseline.skills
    assert before.assets == baseline.assets
    assert getattr(exc_info.value, "_skill_lifecycle_published", False) is True
    assert _published_layout(store, kind, original)
    _assert_pending_layout(store, before, kind)

    fresh = SkillLearningStore.for_home(home, usage_runtime_provider=lambda: repo)
    recovery = fresh.recover_lifecycle()
    assert recovery["status"] == "recovered"
    assert recovery["completed"] is True and recovery["remaining"] is False
    assert recovery["operation_id"] == pending_at_error["operation_id"]
    after = fresh.load_registry()
    _assert_committed_layout(fresh, after)
    if kind in {"evict", "remove"}:
        assert after.skills == {}
        assert not fresh.skill_path("alpha").exists()
        assert not fresh.archived_skill_path("alpha").exists()
        assert not (fresh.versions_dir / "alpha").exists()
        assert bool(tuple(fresh.removed_dir.iterdir())) is (kind == "remove")
        assert ("alpha" in after.blocked_names) is (kind == "remove")
    else:
        assert set(after.skills) == ({"alpha", "gamma"} if kind.startswith("displace") else {"alpha"})
        alpha = after.skills["alpha"]
        assert alpha.last_operation_id == pending_at_error["operation_id"]
        assert alpha.revision == baseline.skills["alpha"].revision + 1
        assert (alpha.maintenance["state"] == "archived") is (kind in {"archive", "displace_first", "displace_second"})
        if kind == "rollback":
            assert alpha.version == 1 and alpha.sha256 == original.sha256
    assert fresh.recover_lifecycle()["status"] == "idle"
    assert fresh.load_registry() == after


@pytest.mark.parametrize("kind", ["archive", "rollback"])
def test_pre_mutation_error_clears_pending_and_keeps_original_files(tmp_path: Path, monkeypatch, kind):
    _, _, store, original = _prepare_case(tmp_path, kind)
    baseline = store.load_registry()
    pending_at_error = []
    real_rename = store_module.os.rename
    real_replace = store_module.os.replace

    def fail_before_rename(src, dst, **kwargs):
        if src == dst == "alpha":
            pending_at_error.append(store.load_registry().pending_operation)
            raise OSError(errno.EIO, "simulated I/O error")
        return real_rename(src, dst, **kwargs)

    def fail_before_replace(src, dst, **kwargs):
        if Path(dst) == store.skill_path("alpha", record=original):
            pending_at_error.append(store.load_registry().pending_operation)
            raise OSError(errno.EIO, "simulated I/O error")
        return real_replace(src, dst, **kwargs)

    with monkeypatch.context() as scoped:
        if kind == "archive":
            # 包装仍转发原 dir_fd 能力，保持严格读取的能力判定不变。
            supported = store_module.os.supports_dir_fd
            scoped.setattr(store_module.os, "rename", fail_before_rename)
            scoped.setattr(
                store_module.os, "supports_dir_fd",
                {fail_before_rename if function is real_rename else function for function in supported},
            )
        else:
            scoped.setattr(store_module.os, "replace", fail_before_replace)
        with pytest.raises(OSError) as exc_info:
            with store.state_lock():
                _mutate(store, kind, original)
    assert exc_info.value.errno == errno.EIO
    assert len(pending_at_error) == 1 and pending_at_error[0] is not None
    assert pending_at_error[0]["phase"] == "prepared"
    assert not getattr(exc_info.value, "_skill_lifecycle_published", False)
    after = store.load_registry()
    assert after == baseline
    _assert_committed_layout(store, after)
    assert not store.archived_skill_path("alpha").exists()
    assert tuple(store.skill_path("alpha").parent.glob(".SKILL.md.*.tmp")) == ()
    assert store.recover_lifecycle()["status"] == "idle"


# === Consolidated from: agent_py_agent/tests/test_sm_rework_quality.py ===
# 质量确立只认版本绑定的真实使用，目录缺失与读取失败分别收敛。

def _source(record, *, authority, receipt_id, run_id=""):
    return maintenance.LearningObservationSource(
        authority=authority,
        actor_id="reviewer" if authority == "human" else "",
        receipt_id=receipt_id,
        run_id=run_id,
        incarnation_id=record.maintenance["incarnation_id"],
        activation_id=record.maintenance["activation_id"],
        strike_epoch=record.maintenance["strike_epoch"],
    )


def _adopt(store, record, task_id, run_id):
    maintenance.observe_learning(
        store, record.name, sha256=record.sha256, task_id=task_id,
        outcome="adopted", source=_source(record, authority="model", receipt_id="", run_id=run_id),
    )


def _two_verified_uses(home, repo, store, authority):
    for number in (1, 2):
        record = store.load_registry().skills["alpha"]
        task_id, run_id = f"use-{number}", f"run-{number}"
        _record_real_use(store, record, task_id=task_id)
        _adopt(store, record, task_id, run_id)
        if authority == "host":
            params = SimpleNamespace(
                run_id=run_id, attempt_id="attempt", task_id=task_id,
                request_id=f"request-{number}", task_attributes={},
            )
            agent = SimpleNamespace(
                config=SimpleNamespace(enable_self_learning=True), home_paths=home,
                runtime_db=repo, _current_run_params=params,
            )
            assert observe_command_verification(
                agent, params, {"id": number, "status": "passed", "exit_code": 0},
            ) == []
        else:
            maintenance.observe_learning(
                store, record.name, sha256=record.sha256, task_id=task_id,
                outcome="effective", source=_source(
                    record, authority="human", receipt_id=f"review-{number}", run_id=run_id,
                ),
            )
    return store.load_registry().skills["alpha"]


def _three_strikes(store):
    result = None
    for number in (1, 2, 3):
        record = store.load_registry().skills["alpha"]
        task_id = f"failure-{number}"
        _record_real_use(store, record, task_id=task_id)
        result = maintenance.observe_learning(
            store, record.name, sha256=record.sha256, task_id=task_id,
            outcome="ineffective", source=_source(
                record, authority="host", receipt_id=f"negative-{number}",
            ),
        )
        if number < 3:
            current = store.load_registry().skills["alpha"]
            assert current.maintenance["hurt_count"] == number
            assert current.maintenance["quality"] == "trial"
            assert current.maintenance["uses_since_hurt"] == 0
    return result


@pytest.mark.parametrize("authority", ["host", "human"])
def test_two_real_uses_and_positive_observations_remain_trial(tmp_path, authority):
    home, repo, store = _store_with_runtime(tmp_path)
    _publish(store, "alpha")
    record = _two_verified_uses(home, repo, store, authority)
    data = record.maintenance
    assert data["quality"] == "trial"
    assert data["state"] == "trial"
    assert data["uses_since_hurt"] == data["helped_count"] == 2
    assert "established_at" not in data
    assert data["last_used_at"] > 0
    facts = [row for row in data["observations"] if row["authority"] == authority]
    assert len(facts) == 2
    assert {row["task_id"] for row in facts} == {"use-1", "use-2"}
    assert all(row["outcome"] == "effective" and row["sha256"] == record.sha256 for row in facts)


@pytest.mark.parametrize("authority", ["host", "human"])
def test_update_before_third_use_has_no_previous_stable(tmp_path, authority):
    home, repo, store = _store_with_runtime(tmp_path)
    _publish(store, "alpha")
    v1 = _two_verified_uses(home, repo, store, authority)
    _publish(store, "alpha", suffix="v2", update_from=v1)
    v2 = store.load_registry().skills["alpha"]
    assert v2.sha256 != v1.sha256
    assert v2.maintenance["quality"] == "trial"
    assert v2.maintenance.get("previous_stable") is None


@pytest.mark.parametrize("authority", ["host", "human"])
def test_third_strike_retires_instead_of_restoring_unproven_v1(tmp_path, authority):
    home, repo, store = _store_with_runtime(tmp_path)
    _publish(store, "alpha")
    v1 = _two_verified_uses(home, repo, store, authority)
    _publish(store, "alpha", suffix="v2", update_from=v1)
    result = _three_strikes(store)
    assert result["action"] == "retired"
    assert result["state"] == "retired" and result["hurt_count"] == 3
    registry = store.load_registry()
    assert "alpha" not in registry.skills
    assert "alpha" in registry.blocked_names
    assert v1.maintenance["task_family_fingerprint"] in registry.blocked_task_families
    assert not (store.learned_root / "alpha" / "SKILL.md").exists()


@pytest.mark.parametrize("authority", ["host", "human"])
def test_third_real_use_establishes_and_supplies_proven_rollback(tmp_path, authority):
    home, repo, store = _store_with_runtime(tmp_path)
    _publish(store, "alpha")
    v1 = _two_verified_uses(home, repo, store, authority)
    _record_real_use(store, v1, task_id="use-3")
    established = store.load_registry().skills["alpha"]
    assert established.maintenance["quality"] == "established"
    assert established.maintenance["quality_basis"] == "uses_threshold"
    assert established.maintenance["uses_since_hurt"] == 3
    assert established.maintenance["established_at"] > 0
    v1_body = store.skill_path("alpha").read_bytes()
    _publish(store, "alpha", suffix="v2", update_from=established)
    v2 = store.load_registry().skills["alpha"]
    assert v2.maintenance["quality"] == "trial"
    assert v2.maintenance["previous_stable"] == {
        "version": established.version, "revision": established.revision,
        "sha256": established.sha256, "established_at": established.maintenance["established_at"],
    }
    assert _three_strikes(store)["action"] == "rolled_back"
    registry = store.load_registry()
    rolled = registry.skills["alpha"]
    assert rolled.sha256 == established.sha256
    assert rolled.maintenance["quality"] == "established"
    assert rolled.maintenance["quality_basis"] == "rollback_established"
    assert rolled.maintenance["activation_id"] != v2.maintenance["activation_id"]
    assert "alpha" not in registry.blocked_names
    assert store.skill_path("alpha").read_bytes() == v1_body


@pytest.mark.parametrize("hurt_count", [1, 2])
def test_positive_observations_do_not_shorten_four_or_five_use_epoch(tmp_path, hurt_count):
    _, _, store = _store_with_runtime(tmp_path)
    _publish(store, "alpha")
    initial = store.load_registry().skills["alpha"]
    for number in (1, 2):
        _record_real_use(store, initial, task_id=f"initial-{number}")
    for number in range(1, hurt_count + 1):
        record = store.load_registry().skills["alpha"]
        task_id = f"negative-{number}"
        _adopt(store, record, task_id, f"negative-run-{number}")
        maintenance.observe_learning(
            store, record.name, sha256=record.sha256, task_id=task_id,
            outcome="ineffective", source=_source(record, authority="host", receipt_id=task_id),
        )
        data = store.load_registry().skills["alpha"].maintenance
        assert data["hurt_count"] == data["strike_epoch"] == number
        assert data["uses_since_hurt"] == 0
    required = 3 + hurt_count
    for number in range(1, required):
        record = store.load_registry().skills["alpha"]
        task_id = f"fresh-{number}"
        _record_real_use(store, record, task_id=task_id)
        _adopt(store, record, task_id, f"fresh-run-{number}")
        maintenance.observe_learning(
            store, record.name, sha256=record.sha256, task_id=task_id,
            outcome="effective", source=_source(record, authority="human", receipt_id=task_id),
        )
        data = store.load_registry().skills["alpha"].maintenance
        assert data["quality"] == "trial"
        assert data["uses_since_hurt"] == number
        assert data["hurt_count"] == hurt_count
    _record_real_use(store, store.load_registry().skills["alpha"], task_id="final-use")
    data = store.load_registry().skills["alpha"].maintenance
    assert data["quality"] == "established" and data["quality_basis"] == "uses_threshold"
    assert data["uses_since_hurt"] == required and data["hurt_count"] == hurt_count


@pytest.mark.parametrize("archived", [False, True])
@pytest.mark.parametrize("empty", [False, True], ids=["absent", "empty"])
def test_missing_managed_directory_is_user_modified(tmp_path, archived, empty):
    _, _, store = _store_with_runtime(tmp_path)
    _publish(store, "alpha")
    if archived:
        with store.state_lock():
            store._transition_archive_locked(store.load_registry(), "alpha", now=100.0)
    record = store.load_registry().skills["alpha"]
    path = store.directory / "archived" / "alpha" / "SKILL.md" if archived else store.skill_path("alpha")
    path.unlink()
    if not empty:
        path.parent.rmdir()
    assert maintenance._verify_single_ownership(store, record) == "user_modified"


@pytest.mark.parametrize("empty", [False, True], ids=["absent", "empty"])
def test_maintenance_releases_deleted_directory_slot_and_preserves_history(tmp_path, empty):
    _, _, store = _store_with_runtime(tmp_path)
    _publish(store, "alpha")
    record = store.load_registry().skills["alpha"]
    version_body = store.read_version("alpha", record.revision)
    path = store.skill_path("alpha")
    path.unlink()
    if not empty:
        path.parent.rmdir()
    report = maintenance.maintain_learning(
        store, mode="autonomous", protection_complete=True, batch_count=100, batch_ms=5000,
    )
    assert report["backlog"] is False
    registry = store.load_registry()
    handed = registry.skills["alpha"]
    assert handed.maintenance["ownership"] == "user_modified"
    assert handed.sha256 == record.sha256 and handed.revision == record.revision
    assert store.read_version("alpha", record.revision) == version_body
    assert store.managed_records(registry) == {}
    assert registry.live_count(managed_names=store.managed_records(registry)) == 0
    assert not path.exists()


def test_directory_read_error_remains_unknown(tmp_path, monkeypatch):
    _, _, store = _store_with_runtime(tmp_path)
    _publish(store, "alpha")
    record = store.load_registry().skills["alpha"]

    def unavailable(*args):
        raise OSError("directory unavailable")

    monkeypatch.setattr(maintenance, "list_names_beneath", unavailable)
    assert maintenance._has_extra_dir_entries(store.skills_root, ("learned", "alpha")) == (False, True)
    assert maintenance._verify_single_ownership(store, record) == "unknown"


def test_directory_link_error_remains_unknown(tmp_path):
    _, _, store = _store_with_runtime(tmp_path)
    _publish(store, "alpha")
    record = store.load_registry().skills["alpha"]
    path = store.skill_path("alpha")
    path.unlink()
    path.parent.rmdir()
    destination = tmp_path / "linked-directory"
    destination.mkdir()
    path.parent.symlink_to(destination, target_is_directory=True)
    assert maintenance._has_extra_dir_entries(store.skills_root, ("learned", "alpha")) == (False, True)
    assert maintenance._verify_single_ownership(store, record) == "unknown"


@pytest.mark.parametrize("ownership", ["user_modified", "owner_layout"])
def test_ownership_handoff_is_terminal_without_directory_probe(tmp_path, monkeypatch, ownership):
    _, _, store = _store_with_runtime(tmp_path)
    _publish(store, "alpha")
    original = store.load_registry().skills["alpha"]
    record = replace(original, maintenance={**original.maintenance, "ownership": ownership})

    def unexpected(*args):
        raise AssertionError("terminal ownership must not probe the directory")

    monkeypatch.setattr(maintenance, "_has_extra_dir_entries", unexpected)
    assert maintenance._verify_single_ownership(store, record) == ownership


# === Consolidated from: agent_py_agent/tests/test_sm_w1_store_transitions.py ===
# W1 合同测试：归属校验、容量准入与 R-2 置换、可恢复状态机转换、显式动作及崩毁恢复。

def _home_and_store(tmp_path: Path, *, usage_runtime_provider=None) -> tuple[object, SkillLearningStore]:
    home = ensure_my_agent_home(tmp_path / "home")
    store = SkillLearningStore.for_home(home, usage_runtime_provider=usage_runtime_provider)
    return home, store


def _publish_w1(store: SkillLearningStore, name: str, *, body: str | None = None, **req_kwargs: object):
    text_body = body if body is not None else f"{BODY}\n\n# skill:{name}"
    dec = replace(
        decision(name),
        body=text_body,
        when_to_use=f"when handling {name}",
        tags=(name, "csv"),
    )
    return publish_learned_skill(store, PublishRequest(dec, **req_kwargs))


def _prove_skill(store: SkillLearningStore, name: str, *, established_at: float = 1000.0) -> None:
    with store.state_lock():
        reg = store.load_registry()
        rec = reg.skills[name]
        updated = replace(
            rec,
            maintenance={
                **rec.maintenance,
                "state": "active",
                "quality": "established",
                "established_at": established_at,
            },
        )
        store.save_registry(replace(reg, skills={**reg.skills, name: updated}))


def _set_maint(store: SkillLearningStore, name: str, **updates: object) -> None:
    with store.state_lock():
        reg = store.load_registry()
        rec = reg.skills[name]
        store.save_registry(
            replace(reg, skills={**reg.skills, name: replace(rec, maintenance={**rec.maintenance, **updates})})
        )


def test_constructor_lazy_usage_provider_path_check_and_bridge_forwarding(tmp_path: Path, monkeypatch):
    home = ensure_my_agent_home(tmp_path / "home")
    expected_db = runtime_db_path(home.owner_home_dir).expanduser().resolve(strict=False)
    calls: list[object] = []

    def provider():
        calls.append("called")
        return SimpleNamespace(db_path=expected_db)

    store = SkillLearningStore.for_home(home, usage_runtime_provider=provider)
    assert calls == []
    assert not expected_db.exists()

    # 只读操作绝不触发 runtime provider，也不会在只读路径创建数据库。
    assert store.has_requests() is False
    reg = store.load_registry()
    assert store.managed_records(reg) == {}
    assert calls == []
    assert not expected_db.exists()

    # 桥接签名必须完整转发非 MISSING 字段并省略未传字段。
    captured: list[tuple[object, dict[str, object]]] = []

    def fake_record_skill_usage_event(store_arg, **kwargs):
        captured.append((store_arg, kwargs))

    monkeypatch.setattr(
        "agent_py_agent.agent.capability.skill_usage_ledger.record_skill_usage_event",
        fake_record_skill_usage_event,
    )
    store.record_skill_usage(
        event="host_injected",
        skill_id="owner:skill-a",
        source="runtime",
        task_key="tk-1",
        incarnation_id="inc-1",
        activation_id="act-1",
        strike_epoch=2,
        body_chars=128,
        content_sha256="abc123",
    )
    assert len(captured) == 1
    assert captured[0][0] is store
    assert captured[0][1] == {
        "event": "host_injected",
        "skill_id": "owner:skill-a",
        "source": "runtime",
        "kind": "skill",
        "body_chars": 128,
        "content_sha256": "abc123",
        "task_key": "tk-1",
        "incarnation_id": "inc-1",
        "activation_id": "act-1",
        "strike_epoch": 2,
    }
    assert all(val is not MISSING for val in captured[0][1].values())

    # 路径校验必须拒绝指向其他 owner 的 runtime_repo。
    wrong_store = SkillLearningStore.for_home(
        home,
        usage_runtime_provider=lambda: SimpleNamespace(db_path=tmp_path / "other" / "runtime.db"),
    )
    with pytest.raises(SkillLearningStoreError) as exc_info:
        wrong_store._runtime_repository()
    assert exc_info.value.code == CODE_EVIDENCE_INCOMPLETE


def test_has_requests_and_skill_path_avoid_registry_or_body_reads(tmp_path: Path, monkeypatch):
    _, store = _home_and_store(tmp_path)
    _publish_w1(store, "alpha")

    # 即使 registry.json 或请求体损坏，has_requests 只查合法请求文件名且不解析内容。
    store.registry_path.write_text("{corrupt-json", encoding="utf-8")
    req_dir = store.directory / "requests"
    req_dir.mkdir(parents=True, exist_ok=True)
    (req_dir / "temp.tmp").write_text("ignored", encoding="utf-8")
    assert store.has_requests() is False
    req_file = req_dir / "0000000000001-0123456789abcdef.json"
    req_file.write_text("{not-json", encoding="utf-8")
    assert store.has_requests() is True
    req_file.unlink()
    overflow = store.directory / "requests_overflow"
    overflow.mkdir(parents=True, exist_ok=True)
    (overflow / "0000000000002-0123456789abcdef.json").write_text("{not-json", encoding="utf-8")
    assert store.has_requests() is True

    # 传入 record 时 skill_path 不调用 load_registry，即使处于 archived 仍返回 live 路径。
    monkeypatch.setattr(store, "load_registry", lambda: (_ for _ in ()).throw(AssertionError("unexpected load_registry")))
    owner_rec = replace(
        store_module.LearnedSkill("manual-x", 1, "sha", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", layout="owner")
    )
    learned_archived = replace(
        store_module.LearnedSkill(
            "alpha",
            1,
            "sha",
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
            layout="learned",
            maintenance={"state": "archived"},
        )
    )
    assert store.skill_path("manual-x", record=owner_rec) == store.skills_root / "manual-x" / "SKILL.md"
    assert store.skill_path("alpha", record=learned_archived) == store.learned_root / "alpha" / "SKILL.md"
    assert store.archived_skill_path("alpha") == store.archived_dir / "alpha" / "SKILL.md"
    assert store.canonical_skill_path("alpha", record=learned_archived) == store.archived_dir / "alpha" / "SKILL.md"


def test_managed_records_and_live_count_classification_and_fail_closed_evidence(tmp_path: Path):
    _, store = _home_and_store(tmp_path)
    _publish_w1(store, "live-trial")
    _publish_w1(store, "disabled-live")
    _publish_w1(store, "archived-skill")
    _publish_w1(store, "user-edited")
    _publish_w1(store, "hand-flagged")
    _publish_w1(store, "owner-rec")

    _set_maint(store, "disabled-live", state="disabled")
    with store.state_lock():
        reg = store.load_registry()
        reg, _ = store._transition_archive_locked(reg, "archived-skill", now=100.0)
        skills = dict(reg.skills)
        skills["hand-flagged"] = replace(
            skills["hand-flagged"],
            maintenance={**skills["hand-flagged"].maintenance, "ownership": "user_modified"},
        )
        skills["owner-rec"] = replace(skills["owner-rec"], layout="owner")
        store.save_registry(replace(reg, skills=skills))

    # 在磁盘上修改 user-edited 正文，确认 regular file 内容不一致会从托管槽位豁免。
    store.learned_root.joinpath("user-edited", "SKILL.md").write_text("# modified by user\n", encoding="utf-8")

    reg = store.load_registry()
    managed_live = store.managed_records(reg, include_archived=False)
    assert set(managed_live) == {"live-trial", "disabled-live"}
    assert reg.live_count(managed_names=managed_live.keys()) == 2
    managed_all = store.managed_records(reg, include_archived=True)
    assert set(managed_all) == {"live-trial", "disabled-live", "archived-skill"}
    # disabled 占用 live 槽位，archived 不占用 live 槽位。
    assert reg.live_count(managed_names=managed_all.keys()) == 2

    # 下列每种异常均须显式失败 CODE_EVIDENCE_INCOMPLETE。
    # 1) ownership == "unknown"
    _, unknown_store = _home_and_store(tmp_path / "unknown")
    _publish_w1(unknown_store, "s1")
    _set_maint(unknown_store, "s1", ownership="unknown")
    with pytest.raises(SkillLearningStoreError) as exc1:
        unknown_store.managed_records(unknown_store.load_registry())
    assert exc1.value.code == CODE_EVIDENCE_INCOMPLETE

    # 2) 未恢复 pending_operation (phase="moved") -> CODE_EVIDENCE_INCOMPLETE
    _, pending_store = _home_and_store(tmp_path / "pending")
    _publish_w1(pending_store, "s1")
    with pending_store.state_lock():
        r = pending_store.load_registry()
        rec_s1 = r.skills["s1"]
        pending_store.save_registry(
            replace(
                r,
                pending_operation={
                    "operation_id": "op-1",
                    "kind": "archive",
                    "phase": "moved",
                    "skill_name": "s1",
                    "expected_revision": rec_s1.revision,
                    "expected_sha256": rec_s1.sha256,
                    "source_rel": "learned/s1",
                    "target_rel": "archived/s1",
                    "versions_rel": "versions/s1",
                    "created_at": 1.0,
                },
            )
        )
    with pytest.raises(SkillLearningStoreError) as exc2:
        pending_store.managed_records(pending_store.load_registry())
    assert exc2.value.code == CODE_EVIDENCE_INCOMPLETE

    # 3) 超出单文件字节预算 -> CODE_EVIDENCE_INCOMPLETE
    from agent_py_agent.agent.capability.skills import MAX_SKILL_BYTES

    _, big_store = _home_and_store(tmp_path / "oversize")
    _publish_w1(big_store, "s1")
    big_store.skill_path("s1").write_text("x" * (MAX_SKILL_BYTES + 16), encoding="utf-8")
    with pytest.raises(SkillLearningStoreError) as exc_big:
        big_store.managed_records(big_store.load_registry())
    assert exc_big.value.code == CODE_EVIDENCE_INCOMPLETE

    # 3) 无未决操作时，手动删除的 live 正文不再占用托管槽位。
    _, missing_store = _home_and_store(tmp_path / "missing")
    _publish_w1(missing_store, "s1")
    missing_store.skill_path("s1").unlink()
    assert "s1" not in missing_store.managed_records(missing_store.load_registry())

    # 4) 托管技能 live 文件为符号链接
    _, sym_store = _home_and_store(tmp_path / "symlink")
    _publish_w1(sym_store, "s1")
    outside = tmp_path / "outside.md"
    outside.write_text("# outside\n", encoding="utf-8")
    p = sym_store.skill_path("s1")
    p.unlink()
    p.symlink_to(outside)
    with pytest.raises(SkillLearningStoreError) as exc4:
        sym_store.managed_records(sym_store.load_registry())
    assert exc4.value.code == CODE_EVIDENCE_INCOMPLETE

    # 5) 损坏的 UTF-8 内容
    _, bad_utf8 = _home_and_store(tmp_path / "utf8")
    _publish_w1(bad_utf8, "s1")
    bad_utf8.skill_path("s1").write_bytes(b"\xff\xfe\xfd")
    with pytest.raises(SkillLearningStoreError) as exc5:
        bad_utf8.managed_records(bad_utf8.load_registry())
    assert exc5.value.code == CODE_EVIDENCE_INCOMPLETE

    # 6) 不可读权限文件（非 root 下）
    if os.geteuid() != 0:
        _, unreadable = _home_and_store(tmp_path / "chmod")
        _publish_w1(unreadable, "s1")
        skill_file = unreadable.skill_path("s1")
        skill_file.chmod(0o000)
        try:
            with pytest.raises(SkillLearningStoreError) as exc6:
                unreadable.managed_records(unreadable.load_registry())
            assert exc6.value.code == CODE_EVIDENCE_INCOMPLETE
        finally:
            skill_file.chmod(0o600)


def test_live_config_provider_observe_and_protection_gates(tmp_path: Path):
    _, store = _home_and_store(tmp_path)
    cfg = SimpleNamespace(
        enable_self_learning=True,
        self_learning_maintenance_mode="autonomous",
        self_learning_max_skills=2,
        runtime_guard_policy=None,
    )
    _publish_w1(store, "a", config_provider=lambda: cfg)
    _publish_w1(store, "b", config_provider=lambda: cfg)

    # 实时切换 enable_self_learning=False 对 publish 与 promote_verified_skill 均立即生效，
    # 且包含不可深拷贝 Lock 的 provider 对象也不会破坏 deepcopy。
    class _LockedProviders:
        def __init__(self) -> None:
            self._lock = threading.Lock()

        def read_cfg(self):
            return cfg

        def read_prot(self):
            return {"protection_complete": True, "protected_ids": ()}

    providers = _LockedProviders()
    cfg.enable_self_learning = False
    with pytest.raises(SkillLearningGateError) as exc_off:
        _publish_w1(store, "c", config_provider=providers.read_cfg, protection_provider=providers.read_prot)
    assert exc_off.value.code == CODE_LEARNING_DISABLED
    with pytest.raises(SkillLearningGateError) as exc_prom_off:
        promote_verified_skill(
            store,
            PublishRequest(
                replace(decision("c"), body=f"{BODY}\n\n# skill:c"),
                config_provider=providers.read_cfg,
                protection_provider=providers.read_prot,
            ),
        )
    assert exc_prom_off.value.code == CODE_LEARNING_DISABLED
    assert set(store.load_registry().skills) == {"a", "b"}

    # 恢复启用但切换为 observe 模式：到达上限时拒绝且不置换。
    cfg.enable_self_learning = True
    cfg.self_learning_maintenance_mode = "observe"
    with pytest.raises(SkillLearningGateError) as exc_obs:
        _publish_w1(
            store,
            "c",
            config_provider=lambda: cfg,
            protection_provider=lambda: {"protection_complete": True, "protected_ids": ()},
        )
    assert exc_obs.value.code == CODE_MAINTENANCE_OBSERVE_ONLY
    assert set(store.load_registry().skills) == {"a", "b"}

    # 切回 autonomous，但 protection 不完整 -> CODE_EVIDENCE_INCOMPLETE 且不置换。
    cfg.self_learning_maintenance_mode = "autonomous"
    with pytest.raises(SkillLearningGateError) as exc_inc:
        _publish_w1(
            store,
            "c",
            config_provider=lambda: cfg,
            protection_provider=lambda: {"protection_complete": False, "protected_ids": ()},
        )
    assert exc_inc.value.code == CODE_EVIDENCE_INCOMPLETE
    assert set(store.load_registry().skills) == {"a", "b"}

    # 保护列表涵盖全部在役技能（支持通配符与 owner:name） -> CODE_LIMIT_REACHED 且不置换。
    for protected in (("*",), ("a", "owner:b")):
        with pytest.raises(SkillLearningGateError) as exc_lim:
            _publish_w1(
                store,
                "c",
                config_provider=lambda: cfg,
                protection_provider=lambda p=protected: {"protection_complete": True, "protected_ids": p},
            )
        assert exc_lim.value.code == CODE_LIMIT_REACHED
        assert set(store.load_registry().skills) == {"a", "b"}

    # propose_or_promote_skill 不在 preflight 误删，支持 deepcopy(publish) 且将 StoreError 规范转为 GateError。
    _set_maint(store, "a", independent_uses_coverage="exact", baseline_at=10.0)
    _set_maint(store, "b", independent_uses_coverage="exact", baseline_at=20.0)
    cand_req = PublishRequest(
        replace(decision("cand-c"), body=f"{BODY}\n\n# skill:cand-c"),
        config_provider=lambda: cfg,
        protection_provider=lambda: {"protection_complete": True, "protected_ids": ("owner:b",)},
    )
    ev = propose_or_promote_skill(store, cand_req)
    assert ev.event == "published" and ev.skill_name == "cand-c"
    reg = store.load_registry()
    assert reg.skills["a"].maintenance["state"] == "archived"
    assert "cand-c" in reg.skills and "b" in reg.skills

    # 当 store 存在损坏证据时，propose_or_promote_skill 将 StoreError 规范转为 SkillLearningGateError。
    _set_maint(store, "b", ownership="unknown")
    with pytest.raises(SkillLearningGateError) as exc_prop:
        propose_or_promote_skill(
            store,
            PublishRequest(replace(decision("cand-d"), body=f"{BODY}\n\n# skill:cand-d"), config_provider=lambda: cfg),
        )
    assert exc_prop.value.code == CODE_EVIDENCE_INCOMPLETE


def test_r2_victim_ranking_displacement_extra_files_and_user_edit_handoff(tmp_path: Path, monkeypatch):
    _, store = _home_and_store(tmp_path)
    for name in ("tier2-active", "unknown-cov", "tier1-cooled", "tier0-unused-new", "tier0-unused-old"):
        _publish_w1(store, name, max_skills=10)

    _set_maint(
        store,
        "tier2-active",
        state="active",
        quality="established",
        established_at=100.0,
        independent_uses=5,
        independent_uses_coverage="exact",
        last_used_at=50.0,
    )
    _set_maint(store, "unknown-cov", independent_uses_coverage="unknown", baseline_at=1.0)
    _set_maint(
        store,
        "tier1-cooled",
        state="cooled",
        independent_uses=2,
        independent_uses_coverage="exact",
        last_used_at=10.0,
    )
    _set_maint(store, "tier0-unused-new", independent_uses_coverage="exact", baseline_at=200.0)
    _set_maint(store, "tier0-unused-old", independent_uses_coverage="exact", baseline_at=100.0)

    # 给第一顺位淘汰者附加辅助文件，验证置换归档完整保留扩展目录文件。
    extra = store.learned_root / "tier0-unused-old" / "notes.txt"
    extra.write_text("keep-me", encoding="utf-8")

    # 置换 1：命中 tier0-unused-old（同为 proven-never-used 中 baseline 更早）。
    ev1 = _publish_w1(
        store,
        "new-1",
        max_skills=5,
        protection_provider=lambda: {"protection_complete": True, "protected_ids": ()},
    )
    assert ev1.skill_name == "new-1"
    reg1 = store.load_registry()
    assert reg1.skills["tier0-unused-old"].maintenance["state"] == "archived"
    assert float(reg1.skills["tier0-unused-old"].maintenance.get("archived_at", 0.0)) > 0.0
    assert not (store.learned_root / "tier0-unused-old").exists()
    assert store.archived_dir.joinpath("tier0-unused-old", "notes.txt").read_text(encoding="utf-8") == "keep-me"

    # 置换 2：在锁定后、真实淘汰前模拟用户手改 tier0-unused-new，验证安全让位并续选 tier1-cooled，
    #   绝不覆盖或丢失用户的手动编辑。
    original_transition = store._transition_displace_publish_locked
    triggered = False

    def race_once(registry, *, victim_name, candidate_record, candidate_text, candidate_assets=None, now=None):
        nonlocal triggered
        if not triggered and victim_name == "tier0-unused-new":
            triggered = True
            store.skill_path("tier0-unused-new").write_text("# user edited right now\n", encoding="utf-8")
        return original_transition(
            registry,
            victim_name=victim_name,
            candidate_record=candidate_record,
            candidate_text=candidate_text,
            candidate_assets=candidate_assets,
            now=now,
        )

    monkeypatch.setattr(store, "_transition_displace_publish_locked", race_once)
    ev2 = _publish_w1(
        store,
        "new-2",
        max_skills=5,
        protection_provider=lambda: {"protection_complete": True, "protected_ids": ()},
    )
    assert ev2.skill_name == "new-2"
    reg2 = store.load_registry()
    assert store.skill_path("tier0-unused-new").read_text(encoding="utf-8") == "# user edited right now\n"
    assert reg2.skills["tier0-unused-new"].maintenance["ownership"] == "user_modified"
    # tier0-unused-new 转为用户手改后退出机器配额统计，live 降至 4 < 5，因此 new-2 直接准入且不误删 tier1-cooled。
    assert reg2.skills["tier1-cooled"].maintenance["state"] == "cooled"

    # 置换 3：此时在役托管满 5 槽（保护新发布的 new-1/new-2），应先于 unknown-cov 与 tier2-active 淘汰 tier1-cooled
    # （unknown-cov 因 coverage="unknown" 不进 Tier 0）。
    ev3 = _publish_w1(
        store,
        "new-3",
        body="1. Inspect archived index.\n2. Emit final status.",
        max_skills=5,
        protection_provider=lambda: {
            "protection_complete": True,
            "protected_ids": ("owner:new-1", "owner:new-2"),
        },
    )
    assert ev3.skill_name == "new-3"
    reg3 = store.load_registry()
    assert reg3.skills["tier1-cooled"].maintenance["state"] == "archived"
    assert reg3.skills["unknown-cov"].maintenance["state"] == "trial"
    assert reg3.skills["tier2-active"].maintenance["state"] == "active"
    assert reg3.skills["tier2-active"].maintenance["quality"] == "established"


def test_interleaved_publish_and_restore_never_exceeds_live_cap(tmp_path: Path):
    _, store = _home_and_store(tmp_path)
    _publish_w1(store, "slot-1", max_skills=2)
    _publish_w1(store, "slot-2", max_skills=2)
    with store.state_lock():
        reg = store.load_registry()
        reg, _ = store._transition_archive_locked(reg, "slot-2", now=10.0)
        store.save_registry(reg)
    _set_maint(store, "slot-1", independent_uses_coverage="exact", baseline_at=1.0)

    barrier = threading.Barrier(2)

    def do_restore():
        barrier.wait(timeout=5.0)
        with store.state_lock():
            r = store._prepare_clean_registry_locked(store.load_registry())
            managed = store.managed_records(r, include_archived=False)
            if r.live_count(managed_names=managed.keys()) < 2 and r.skills["slot-2"].maintenance["state"] == "archived":
                r, _ = store._transition_restore_locked(r, "slot-2", now=20.0)
                return True
        return False

    def do_publish():
        barrier.wait(timeout=5.0)
        return _publish_w1(
            store,
            "slot-3",
            max_skills=2,
            protection_provider=lambda: {"protection_complete": True, "protected_ids": ()},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_restore = pool.submit(do_restore)
        f_publish = pool.submit(do_publish)
        f_restore.result(timeout=10.0)
        f_publish.result(timeout=10.0)

    final_reg = store.load_registry()
    managed_final = store.managed_records(final_reg, include_archived=False)
    assert final_reg.live_count(managed_names=managed_final.keys()) <= 2


def test_proven_previous_stable_rollback_and_third_strike_retirement(tmp_path: Path):
    _, store = _home_and_store(tmp_path)
    # 1) 未证实 trial v1 -> v2 -> v3 绝不写入 previous_stable。
    _publish_w1(store, "trial-only", body=f"{BODY}\n\n# trial-v1")
    r1 = store.load_registry().skills["trial-only"]
    assert not is_proven_stable_version(
        {"version": r1.version, "sha256": r1.sha256, "established_at": r1.maintenance.get("established_at", 0.0)}
    )
    upd_v2 = replace(decision("trial-only"), decision="update", update_target="trial-only", body=f"{BODY}\n\n# trial-v2")
    publish_learned_skill(store, PublishRequest(upd_v2, expected_revision=r1.revision, expected_sha256=r1.sha256))
    r2 = store.load_registry().skills["trial-only"]
    assert r2.maintenance.get("previous_stable") is None

    # 2) 已证实 established v1 -> v2 -> v3 必须持续保留 v1 为 previous_stable。
    _publish_w1(store, "proven-s", body=f"{BODY}\n\n# stable-v1")
    _prove_skill(store, "proven-s", established_at=1234.5)
    v1 = store.load_registry().skills["proven-s"]
    v1_act = v1.maintenance["activation_id"]
    v1_inc = v1.maintenance["incarnation_id"]
    publish_learned_skill(
        store,
        PublishRequest(
            replace(decision("proven-s"), decision="update", update_target="proven-s", body=f"{BODY}\n\n# trial-v2"),
            expected_revision=v1.revision,
            expected_sha256=v1.sha256,
        ),
    )
    v2 = store.load_registry().skills["proven-s"]
    assert v2.maintenance["previous_stable"]["version"] == 1
    assert v2.maintenance["previous_stable"]["sha256"] == v1.sha256
    assert float(v2.maintenance["previous_stable"]["established_at"]) == 1234.5
    assert v2.maintenance["incarnation_id"] == v1_inc
    assert v2.maintenance["activation_id"] != v1_act

    publish_learned_skill(
        store,
        PublishRequest(
            replace(decision("proven-s"), decision="update", update_target="proven-s", body=f"{BODY}\n\n# trial-v3"),
            expected_revision=v2.revision,
            expected_sha256=v2.sha256,
        ),
    )
    v3 = store.load_registry().skills["proven-s"]
    assert v3.maintenance["previous_stable"]["version"] == 1
    assert v3.maintenance["previous_stable"]["sha256"] == v1.sha256

    # 3) _rollback_to_proven_stable_locked 精确恢复 v1、established 质量、清空 strike 与新 activation_id。
    with store.state_lock():
        reg = store.load_registry()
        _, rolled = _rollback_to_proven_stable_locked(store, reg, v3, now=2000.0)
    assert rolled.sha256 == v1.sha256
    assert rolled.maintenance["state"] == "active"
    assert rolled.maintenance["quality"] == "established"
    assert float(rolled.maintenance["established_at"]) == 1234.5
    assert rolled.maintenance["incarnation_id"] == v1_inc
    assert rolled.maintenance["activation_id"] not in {v1_act, v3.maintenance["activation_id"]}
    assert "previous_stable" not in rolled.maintenance
    assert rolled.maintenance.get("strike_count", 0) == 0
    assert skill_content_sha256(store.skill_path("proven-s").read_text(encoding="utf-8")) == v1.sha256

    # 4) _retire_third_strike_locked 封禁名字与家族、清理仅 skill 类型资产、保留 script/workflow 资产。
    same_family_body = f"{BODY}\n\n# family-lock-target"
    _publish_w1(store, "retire-me", body=same_family_body)
    target_family = skill_family_fingerprint(render_learned_skill(replace(decision("retire-me"), body=same_family_body)))
    with store.state_lock():
        reg = store.load_registry()
        reg = replace(
            reg,
            assets={
                "owner:retire-me": {"kind": "skill", "asset_id": "owner:retire-me"},
                "retire-me-script": {"kind": "script", "asset_id": "retire-me", "name": "retire-me"},
                "wf-keep": {"kind": "workflow", "asset_id": "owner:retire-me"},
            },
        )
        store.save_registry(reg)
        reg = _retire_third_strike_locked(store, reg, reg.skills["retire-me"], now=3000.0)

    assert "retire-me" not in reg.skills
    assert not store.skill_path("retire-me").exists()
    assert not store.archived_skill_path("retire-me").exists()
    assert "retire-me" in reg.blocked_names
    assert target_family in reg.blocked_task_families
    assert "owner:retire-me" not in reg.assets
    assert "retire-me-script" in reg.assets and "wf-keep" in reg.assets

    # 新名字但同家族实现体同样被封禁拒绝。
    with pytest.raises(SkillLearningGateError) as exc_fam:
        _publish_w1(store, "renamed-clone", body=same_family_body)
    assert exc_fam.value.code == CODE_FAMILY_BLOCKED


def test_explicit_actions_report_and_s1_migration_isolation(tmp_path: Path, monkeypatch):
    _, store = _home_and_store(tmp_path / "actions")
    _publish_w1(store, "act-skill", body=f"{BODY}\n\n# v1")
    v1 = store.load_registry().skills["act-skill"]
    publish_learned_skill(
        store,
        PublishRequest(
            replace(decision("act-skill"), decision="update", update_target="act-skill", body=f"{BODY}\n\n# v2"),
            expected_revision=v1.revision,
            expected_sha256=v1.sha256,
        ),
    )

    # apply_learned_skill_action 在主变更提交后若账本追加失败（如配额不足），须上报 committed=True + ledger_appended=False。
    monkeypatch.setattr(
        store,
        "append_event",
        lambda _ev: (_ for _ in ()).throw(
            OwnerQuotaExceeded(OwnerQuotaProjection(used_bytes=10, projected_bytes=20, max_bytes=15))
        ),
    )
    res = apply_learned_skill_action(store, "act-skill", "revert")
    assert res["code"] == "reverted"
    assert res["committed"] is True
    assert res["outcome"] == "committed"
    assert res["ledger_appended"] is False
    assert res["ledger_error"] == "OwnerQuotaExceeded"
    assert store.load_registry().skills["act-skill"].version == 1

    # v1 上直接 remove_learned_skill：保留 removed/<name>-<stamp> 供人查证，清空 versions/<name>。
    rem = remove_learned_skill(store, "act-skill")
    assert rem.event == "removed"
    assert not (store.learned_root / "act-skill").exists()
    assert not (store.versions_dir / "act-skill").exists()
    assert any(p.name.startswith("act-skill-") for p in store.removed_dir.iterdir())

    # 用户明确 remove 后，同名与同方法家族均受阻；全新步骤家族的技能不受影响。
    with pytest.raises(SkillLearningGateError) as exc_removed_fam:
        _publish_w1(store, "act-clone", body=f"{BODY}\n\n# clone")
    assert exc_removed_fam.value.code == CODE_FAMILY_BLOCKED

    # learned_skills_report 暴露 managed_count 与 coverage，且证据不全时显式上报 unavailable。
    _publish_w1(store, "rep-skill", body="1. Inspect manifest checksums.\n2. Record verified signature.")
    ok_report = learned_skills_report(store, SimpleNamespace(enable_self_learning=True))
    assert ok_report["managed_count"] == 1
    assert ok_report["coverage"]["status"] == "complete"
    store.skill_path("rep-skill").unlink()
    missing_report = learned_skills_report(store, SimpleNamespace(enable_self_learning=True))
    assert missing_report["managed_count"] == 0
    assert missing_report["coverage"]["status"] == "complete"
    # 正文缺失只释放计数，不改登记；损坏正文仍须保持原证据不全断言。
    assert "rep-skill" in store.load_registry().skills
    store.skill_path("rep-skill").write_bytes(b"\xff\xfe\xfd")
    bad_report = learned_skills_report(store, SimpleNamespace(enable_self_learning=True))
    assert bad_report["managed_count"] is None
    assert bad_report["coverage"]["status"] == "unavailable"
    assert bad_report["coverage"]["code"] == CODE_EVIDENCE_INCOMPLETE

    # S1 adopt_committed_lessons 失败隔离：一条源失效不阻塞后续有效条目，且不伪造 established_at。
    _, mig_store = _home_and_store(tmp_path / "migration")
    for skill_name in ("broken-s1", "valid-s1"):
        s_dir = mig_store.skills_root / skill_name
        s_dir.mkdir(parents=True, exist_ok=True)
        (s_dir / "SKILL.md").write_text(f"---\nname: {skill_name}\ndescription: s1\n---\n\n{BODY}\n", encoding="utf-8")

    def make_prop(skill_name: str, pid: str):
        text = (mig_store.skills_root / skill_name / "SKILL.md").read_text(encoding="utf-8")
        sha = skill_content_sha256(text)
        return SimpleNamespace(
            proposal_id=pid,
            target=SimpleNamespace(skill_name=skill_name),
            receipt={
                "skill_path": str(mig_store.skills_root / skill_name / "SKILL.md"),
                "skill_sha256": sha,
            },
            draft=SimpleNamespace(sha256=sha),
            source=SimpleNamespace(run_ids=("run-s1",), marker=skill_name),
        )

    fake_props = SimpleNamespace(
        candidates_path=tmp_path / "migration" / "cands.json",
        list=lambda status="committed": [make_prop("broken-s1", "p-bad"), make_prop("valid-s1", "p-ok")],
    )

    def fake_verify(_candidates, src):
        if src.marker == "broken-s1":
            raise SkillProposalError("SOURCE_MISSING")
        return None

    monkeypatch.setattr("agent_py_agent.agent.capability.skill_proposals._verify_source", fake_verify)
    monkeypatch.setattr("agent_py_agent.agent.memory_store.candidates.CandidateService", lambda path: object())

    adopted = adopt_committed_lessons(mig_store, fake_props)
    assert adopted == ["valid-s1"]
    adopted_rec = mig_store.load_registry().skills["valid-s1"]
    assert adopted_rec.maintenance["quality"] == "trial"
    assert float(adopted_rec.maintenance.get("established_at", 0.0)) == 0.0
    assert adopted_rec.maintenance["incarnation_id"]
    assert adopted_rec.maintenance["activation_id"]
    assert adopted_rec.maintenance["task_family_fingerprint"]


def _run_crash_subprocess(home_root: Path, script_body: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "HOME": str(home_root),
        "MY_AGENT_HOME": str(home_root),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return subprocess.run([sys.executable, "-c", script_body], env=env, capture_output=True, text=True, check=False)


@pytest.mark.parametrize("kind", ["archive", "restore", "displace_publish", "evict", "remove", "rollback"])
@pytest.mark.parametrize("crash_point", ["after_prepared", "after_move_before_commit"])
def test_real_subprocess_crash_recovery_converges_for_all_six_transitions(
    tmp_path: Path, kind: str, crash_point: str
):
    home, store = _home_and_store(tmp_path)
    _publish_w1(store, "alpha", body=f"{BODY}\n\n# alpha-v1")
    if kind == "rollback":
        v1 = store.load_registry().skills["alpha"]
        publish_learned_skill(
            store,
            PublishRequest(
                replace(decision("alpha"), decision="update", update_target="alpha", body=f"{BODY}\n\n# alpha-v2"),
                expected_revision=v1.revision,
                expected_sha256=v1.sha256,
            ),
        )
    elif kind in {"restore", "evict"}:
        with store.state_lock():
            reg = store.load_registry()
            reg, _ = store._transition_archive_locked(reg, "alpha", now=10.0)
            store.save_registry(reg)
    elif kind == "displace_publish":
        _publish_w1(store, "beta", body=f"{BODY}\n\n# beta-v1")

    script = f"""
import os
from pathlib import Path
from dataclasses import replace
from agent_py_agent.agent.capability import skill_learning_store as store_mod
from agent_py_agent.agent.capability.skill_learning_store import LearnedSkill, SkillLearningStore
from agent_py_agent.agent.capability.skill_learning_publish import render_learned_skill
from agent_py_agent.agent.capability.skill_snapshot import skill_content_sha256
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.tests.test_wave7_autonomous_learning_closure import decision

home = ensure_my_agent_home(Path({str(home.root)!r}))
store = SkillLearningStore.for_home(home)
orig_save = store.save_registry
orig_move = store._move_dir_nofollow
orig_replace = store_mod.replace_skill_file

def crashing_save(reg):
    phase = (reg.pending_operation or {{}}).get("phase", "")
    if {crash_point!r} == "after_prepared" and phase == "prepared":
        orig_save(reg)
        os._exit(77)
    if {crash_point!r} == "after_move_before_commit":
        if {kind!r} == "displace_publish" and phase == "moved":
            orig_save(reg)
            os._exit(77)
        if {kind!r} in ("evict", "remove") and phase == "cleanup":
            orig_save(reg)
            os._exit(77)
    return orig_save(reg)

def crashing_move(*args, **kwargs):
    orig_move(*args, **kwargs)
    if {crash_point!r} == "after_move_before_commit" and {kind!r} in ("archive", "restore"):
        os._exit(77)

def crashing_replace(*args, **kwargs):
    orig_replace(*args, **kwargs)
    if {crash_point!r} == "after_move_before_commit" and {kind!r} == "rollback":
        os._exit(77)

store.save_registry = crashing_save
store._move_dir_nofollow = crashing_move
store_mod.replace_skill_file = crashing_replace
with store.state_lock():
    reg = store.load_registry()
    kind = {kind!r}
    if kind == "archive":
        store._transition_archive_locked(reg, "alpha", now=50.0)
    elif kind == "restore":
        store._transition_restore_locked(reg, "alpha", now=50.0)
    elif kind == "evict":
        store._transition_evict_locked(reg, "alpha", now=50.0, block=True, block_family="fam-1")
    elif kind == "remove":
        store._transition_remove_locked(reg, "alpha", now=50.0)
    elif kind == "rollback":
        cur = reg.skills["alpha"]
        v1_text = store.read_version("alpha", 1) or ""
        next_rec = replace(cur, version=cur.revision + 1, revision=cur.revision + 1, sha256=skill_content_sha256(v1_text))
        store._transition_rollback_locked(
            reg, current=cur, next_record=next_rec, target_version=1,
            target_sha256=skill_content_sha256(v1_text), target_text=v1_text,
        )
    elif kind == "displace_publish":
        dec = replace(decision("gamma"), body="1. step one\\n2. step two\\n\\n# gamma-new", when_to_use="gamma")
        cand_text = render_learned_skill(dec)
        cand_rec = LearnedSkill("gamma", 1, skill_content_sha256(cand_text), "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")
        store._transition_displace_publish_locked(
            reg, victim_name="alpha", candidate_record=cand_rec, candidate_text=cand_text, now=50.0
        )
"""
    proc = _run_crash_subprocess(home.root, script)
    assert proc.returncode == 77, proc.stderr

    # 使用全新 store 实例验证冷启恢复前向收敛：恢复后无 pending_operation，且 managed_records 恢复完整证据。
    fresh = SkillLearningStore.for_home(home)
    report = fresh.recover_lifecycle()
    assert report["remaining"] is False and report["completed"] is True
    recovered = fresh.load_registry()
    assert recovered.pending_operation is None
    _ = fresh.managed_records(recovered, include_archived=True)

    if kind == "archive":
        assert recovered.skills["alpha"].maintenance["state"] == "archived"
        assert not fresh.skill_path("alpha").exists()
        assert fresh.archived_skill_path("alpha").is_file()
    elif kind == "restore":
        assert recovered.skills["alpha"].maintenance["state"] == "trial"
        assert fresh.skill_path("alpha").is_file()
        assert not fresh.archived_skill_path("alpha").exists()
    elif kind == "displace_publish":
        assert recovered.skills["alpha"].maintenance["state"] == "archived"
        assert fresh.archived_skill_path("alpha").is_file()
        assert not fresh.skill_path("alpha").exists()
        assert "gamma" in recovered.skills
        assert fresh.skill_path("gamma").is_file()
    elif kind == "evict":
        assert "alpha" not in recovered.skills
        assert "alpha" in recovered.blocked_names
        assert not fresh.archived_skill_path("alpha").exists()
        assert not (fresh.versions_dir / "alpha").exists()
    elif kind == "remove":
        assert "alpha" not in recovered.skills
        assert not fresh.skill_path("alpha").exists()
        assert not (fresh.versions_dir / "alpha").exists()
        assert any(p.name.startswith("alpha-") for p in fresh.removed_dir.iterdir())
    elif kind == "rollback":
        assert recovered.skills["alpha"].version == 1
        assert recovered.skills["alpha"].revision == 3
        assert recovered.skills["alpha"].sha256 == skill_content_sha256(fresh.read_version("alpha", 1) or "")


def test_crash_with_user_edit_before_move_safely_hands_off_without_overwriting(tmp_path: Path):
    home, store = _home_and_store(tmp_path)
    _publish_w1(store, "keep-user", body=f"{BODY}\n\n# v1")
    script = f"""
import os
from pathlib import Path
from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

home = ensure_my_agent_home(Path({str(home.root)!r}))
store = SkillLearningStore.for_home(home)
orig_save = store.save_registry

def crash_prepared(reg):
    orig_save(reg)
    if (reg.pending_operation or {{}}).get("phase") == "prepared":
        os._exit(77)

store.save_registry = crash_prepared
with store.state_lock():
    store._transition_archive_locked(store.load_registry(), "keep-user", now=10.0)
"""
    proc = _run_crash_subprocess(home.root, script)
    assert proc.returncode == 77, proc.stderr

    # 崩溃后恢复前用户编辑了原文件：recover_lifecycle 必须安全交权并保留用户文件。
    store.skill_path("keep-user").write_text("# user edited during downtime\n", encoding="utf-8")
    fresh = SkillLearningStore.for_home(home)
    report = fresh.recover_lifecycle()
    assert report["remaining"] is False
    recovered = fresh.load_registry()
    assert recovered.pending_operation is None
    assert recovered.skills["keep-user"].maintenance["ownership"] == "user_modified"
    assert fresh.skill_path("keep-user").read_text(encoding="utf-8") == "# user edited during downtime\n"


def test_bounded_incremental_cleanup_post_commit_error_and_additive_registry_compat(tmp_path: Path, monkeypatch):
    _, store = _home_and_store(tmp_path)
    _publish_w1(store, "multi-ver", body=f"{BODY}\n\n# v1")
    for ver in (2, 3, 4):
        cur = store.load_registry().skills["multi-ver"]
        publish_learned_skill(
            store,
            PublishRequest(
                replace(decision("multi-ver"), decision="update", update_target="multi-ver", body=f"{BODY}\n\n# v{ver}"),
                expected_revision=cur.revision,
                expected_sha256=cur.sha256,
            ),
        )

    # remove + batch_count=1：只做有界清理，再通过 recover_lifecycle(batch_count=1) 逐批收敛清零。
    with store.state_lock():
        reg = store.load_registry()
        reg, _ = store._transition_remove_locked(reg, "multi-ver", now=100.0, cleanup_budget=(1, 0, 0))
    assert store.load_registry().pending_operation is not None
    step1 = store.recover_lifecycle(batch_count=1)
    assert step1["actions"] == 1
    assert step1["remaining"] is True
    while store.load_registry().pending_operation is not None:
        store.recover_lifecycle(batch_count=1)
    assert not (store.versions_dir / "multi-ver").exists()

    # 提交后 save_registry 抛错（写盘后异常）绝不回滚已提交版本。
    _publish_w1(store, "post-commit", body="1. Compare archive ledger.\n2. Emit final manifest.")
    v1 = store.load_registry().skills["post-commit"]
    orig_save = store.save_registry

    def fail_after_write(registry_obj):
        orig_save(registry_obj)
        raise OSError("simulated post-commit write notice")

    monkeypatch.setattr(store, "save_registry", fail_after_write)
    with pytest.raises(SkillLearningGateError) as exc_pc:
        publish_learned_skill(
            store,
            PublishRequest(
                replace(
                    decision("post-commit"),
                    decision="update",
                    update_target="post-commit",
                    body="1. Compare archive ledger.\n2. Emit final manifest.\n3. Verify digest.",
                ),
                expected_revision=v1.revision,
                expected_sha256=v1.sha256,
            ),
        )
    assert exc_pc.value.detail["committed"] is True
    monkeypatch.setattr(store, "save_registry", orig_save)
    assert store.load_registry().skills["post-commit"].revision == 2
    assert skill_content_sha256(store.skill_path("post-commit").read_text(encoding="utf-8")) != v1.sha256

    # 加法兼容旧 registry：保留未知扩展字段与历史 hurt_count > 3。
    raw = json.loads(store.registry_path.read_text(encoding="utf-8"))
    raw["skills"]["post-commit"]["maintenance"]["hurt_count"] = 5
    raw["skills"]["post-commit"]["maintenance"]["future_hint"] = "preserve-me"
    store.registry_path.write_text(json.dumps(raw), encoding="utf-8")
    loaded = store.load_registry()
    assert loaded.skills["post-commit"].maintenance["hurt_count"] == 5
    assert loaded.skills["post-commit"].maintenance["future_hint"] == "preserve-me"
    assert isinstance(loaded, SkillLearningRegistry)


def test_p1_recover_rollback_does_not_promote_unproven_trial_and_clears_verification(tmp_path: Path):
    home, store = _home_and_store(tmp_path)
    # 场景 A：v1 已证实 -> v2 试用 -> v3 试用；人工回滚 v3->v2 时崩溃（previous_stable 仍指 v1），
    # 恢复绝不能把未证实的 v2 晋升为 established，也不能伪造 established_at。
    _publish_w1(store, "rb-trial", body=f"{BODY}\n\n# v1")
    with store.state_lock():
        reg = store.load_registry()
        v1 = reg.skills["rb-trial"]
        v1_stable = {
            "version": 1,
            "revision": 1,
            "sha256": v1.sha256,
            "established_at": 1000.0,
        }
        reg = replace(
            reg,
            skills={
                **reg.skills,
                "rb-trial": replace(
                    v1,
                    maintenance={
                        **v1.maintenance,
                        "state": "active",
                        "quality": "established",
                        "quality_basis": "verified",
                        "established_at": 1000.0,
                    },
                ),
            },
        )
        store.save_registry(reg)
    for ver in (2, 3):
        cur = store.load_registry().skills["rb-trial"]
        publish_learned_skill(
            store,
            PublishRequest(
                replace(decision("rb-trial"), decision="update", update_target="rb-trial", body=f"{BODY}\n\n# v{ver}"),
                expected_revision=cur.revision,
                expected_sha256=cur.sha256,
            ),
        )
    v2_text = store.read_version("rb-trial", 2) or ""
    v2_sha = skill_content_sha256(v2_text)
    with store.state_lock():
        reg = store.load_registry()
        cur = reg.skills["rb-trial"]
        store.save_registry(
            replace(
                reg,
                skills={
                    **reg.skills,
                    "rb-trial": replace(
                        cur,
                        maintenance={**cur.maintenance, "previous_verification": {"note": "stale-v3"}},
                    ),
                },
            )
        )
    script_a = f"""
import os
from pathlib import Path
from agent_py_agent.agent.capability.skill_learning_publish import revert_learned_skill
from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

home = ensure_my_agent_home(Path({str(home.root)!r}))
store = SkillLearningStore.for_home(home)
orig_save = store.save_registry

def crash_pending(reg_obj):
    orig_save(reg_obj)
    if reg_obj.pending_operation is not None:
        os._exit(77)

store.save_registry = crash_pending
revert_learned_skill(store, "rb-trial")
"""
    proc_a = _run_crash_subprocess(home.root, script_a)
    assert proc_a.returncode == 77, proc_a.stderr

    fresh = SkillLearningStore.for_home(home)
    report = fresh.recover_lifecycle()
    assert report["completed"] is True
    rec = fresh.load_registry().skills["rb-trial"]
    assert rec.version == 2 and rec.sha256 == v2_sha
    assert rec.maintenance["quality"] == "trial"
    assert rec.maintenance["quality_basis"] == "manual_revert"
    assert "established_at" not in rec.maintenance
    assert rec.maintenance["previous_stable"] == v1_stable
    assert rec.maintenance["task_family_fingerprint"] == skill_family_fingerprint(v2_text)

    # 场景 B：v1 已证实 -> v2 试用；回退到已证实 v1 时崩溃，恢复后保留原 established_at 并清理 previous_verification。
    _publish_w1(fresh, "rb-proven", body=f"{BODY}\n\n# v1")
    with fresh.state_lock():
        reg = fresh.load_registry()
        v1p = reg.skills["rb-proven"]
        fresh.save_registry(
            replace(
                reg,
                skills={
                    **reg.skills,
                    "rb-proven": replace(
                        v1p,
                        maintenance={
                            **v1p.maintenance,
                            "state": "active",
                            "quality": "established",
                            "quality_basis": "verified",
                            "established_at": 1000.0,
                        },
                    ),
                },
            )
        )
    curp = fresh.load_registry().skills["rb-proven"]
    publish_learned_skill(
        fresh,
        PublishRequest(
            replace(decision("rb-proven"), decision="update", update_target="rb-proven", body=f"{BODY}\n\n# v2"),
            expected_revision=curp.revision,
            expected_sha256=curp.sha256,
        ),
    )
    v1p_text = fresh.read_version("rb-proven", 1) or ""
    v1p_sha = skill_content_sha256(v1p_text)
    with fresh.state_lock():
        reg = fresh.load_registry()
        curp = reg.skills["rb-proven"]
        fresh.save_registry(
            replace(
                reg,
                skills={
                    **reg.skills,
                    "rb-proven": replace(
                        curp,
                        maintenance={**curp.maintenance, "previous_verification": {"note": "clear-me"}},
                    ),
                },
            )
        )
    script_b = f"""
import os
from pathlib import Path
from agent_py_agent.agent.capability.skill_learning_publish import _rollback_to_proven_stable_locked
from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

home = ensure_my_agent_home(Path({str(home.root)!r}))
store = SkillLearningStore.for_home(home)
orig_save = store.save_registry

def crash_proven(reg_obj):
    orig_save(reg_obj)
    if reg_obj.pending_operation is not None:
        os._exit(77)

store.save_registry = crash_proven
with store.state_lock():
    reg = store.load_registry()
    _rollback_to_proven_stable_locked(store, reg, reg.skills["rb-proven"], now=2500.0)
"""
    proc_b = _run_crash_subprocess(home.root, script_b)
    assert proc_b.returncode == 77, proc_b.stderr

    fresh2 = SkillLearningStore.for_home(home)
    fresh2.recover_lifecycle()
    recp = fresh2.load_registry().skills["rb-proven"]
    assert recp.version == 1 and recp.sha256 == v1p_sha
    assert recp.maintenance["quality"] == "established"
    assert recp.maintenance["established_at"] == 1000.0
    assert "previous_stable" not in recp.maintenance
    assert "previous_verification" not in recp.maintenance
    assert recp.maintenance["task_family_fingerprint"] == skill_family_fingerprint(v1p_text)


def test_p1_evict_preserves_extra_user_files_on_sync_and_crash_recovery(tmp_path: Path):
    home, store = _home_and_store(tmp_path)
    # 1) 在役与归档目录中用户新增额外文件/子目录：同步驱逐必须拒绝删除并交权 user_modified。
    _publish_w1(store, "live-extra")
    (store.learned_root / "live-extra" / "notes.txt").write_text("user notes", encoding="utf-8")
    (store.learned_root / "live-extra" / "scripts").mkdir()
    (store.learned_root / "live-extra" / "scripts" / "tool.py").write_text("print(1)\n", encoding="utf-8")
    with store.state_lock():
        with pytest.raises(SkillLearningStoreError) as exc_live:
            store._transition_evict_locked(store.load_registry(), "live-extra", block=True, now=100.0)
    assert exc_live.value.code == "SKILL_LEARNING_USER_MODIFIED"
    assert (store.learned_root / "live-extra" / "notes.txt").read_text(encoding="utf-8") == "user notes"
    assert (store.learned_root / "live-extra" / "scripts" / "tool.py").exists()
    reg_live = store.load_registry()
    assert reg_live.skills["live-extra"].maintenance["ownership"] == "user_modified"
    assert "live-extra" not in reg_live.blocked_names

    _publish_w1(store, "arch-extra", body=f"{BODY}\n\n# arch")
    with store.state_lock():
        store._transition_archive_locked(store.load_registry(), "arch-extra", now=100.0)
    (store.archived_dir / "arch-extra" / "extra.txt").write_text("keep archive extra", encoding="utf-8")
    with store.state_lock():
        with pytest.raises(SkillLearningStoreError) as exc_arch:
            store._transition_evict_locked(store.load_registry(), "arch-extra", now=110.0)
    assert exc_arch.value.code == "SKILL_LEARNING_USER_MODIFIED"
    assert (store.archived_dir / "arch-extra" / "extra.txt").read_text(encoding="utf-8") == "keep archive extra"
    assert store.load_registry().skills["arch-extra"].maintenance["ownership"] == "user_modified"

    # 2) 崩溃恢复场景（移入墓碑前后）：目录含额外用户文件时必须恢复原位并交权 user_modified，不删除用户文件。
    _publish_w1(store, "crash-extra", body=f"{BODY}\n\n# crash-extra")
    script = f"""
import os
from pathlib import Path
from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

home = ensure_my_agent_home(Path({str(home.root)!r}))
store = SkillLearningStore.for_home(home)
orig_move = store._move_dir_nofollow
calls = 0

def drop_extra_and_crash(src_root, src_parts, dst_root, dst_parts):
    global calls
    calls += 1
    orig_move(src_root, src_parts, dst_root, dst_parts)
    (dst_root.joinpath(*dst_parts) / "user_extra.txt").write_text("do not delete", encoding="utf-8")
    os._exit(77)

store._move_dir_nofollow = drop_extra_and_crash
with store.state_lock():
    store._transition_evict_locked(store.load_registry(), "crash-extra", now=200.0)
"""
    proc = _run_crash_subprocess(home.root, script)
    assert proc.returncode == 77, proc.stderr
    fresh = SkillLearningStore.for_home(home)
    report = fresh.recover_lifecycle()
    assert report["completed"] is True
    assert (fresh.learned_root / "crash-extra" / "user_extra.txt").read_text(encoding="utf-8") == "do not delete"
    assert (fresh.learned_root / "crash-extra" / "SKILL.md").exists()
    rec_crash = fresh.load_registry()
    assert rec_crash.pending_operation is None
    assert rec_crash.skills["crash-extra"].maintenance["ownership"] == "user_modified"


def test_p1_displace_abort_leaves_no_orphan_assets_and_retire_fail_closed(tmp_path: Path, monkeypatch):
    home, store = _home_and_store(tmp_path)
    # 1) 置换中止（首个受害者转为用户手改，唯一剩余技能受保护 -> CODE_LIMIT_REACHED）：
    # 绝不能把未发布候选的 trial 资产条留在 registry.assets 中。
    _publish_w1(store, "victim-user", body=f"{BODY}\n\n# victim-user")
    _publish_w1(store, "victim-prot", body=f"{BODY}\n\n# victim-prot")
    orig_transition = store._transition_displace_publish_locked

    def modify_victim_once(registry, *, victim_name, candidate_record, candidate_text, candidate_assets=None, now=None):
        if victim_name == "victim-user":
            store.skill_path("victim-user").write_text("# user took over\n", encoding="utf-8")
        return orig_transition(
            registry,
            victim_name=victim_name,
            candidate_record=candidate_record,
            candidate_text=candidate_text,
            candidate_assets=candidate_assets,
            now=now,
        )

    monkeypatch.setattr(store, "_transition_displace_publish_locked", modify_victim_once)
    with pytest.raises(SkillLearningGateError) as exc_lim:
        _publish_w1(
            store,
            "cand-abort",
            body="1. Run abort check.\n2. Keep assets clean.",
            max_skills=1,
            protection_provider=lambda: {"protection_complete": True, "protected_ids": ("owner:victim-prot",)},
        )
    assert exc_lim.value.code == CODE_LIMIT_REACHED
    reg_after = store.load_registry()
    assert "cand-abort" not in reg_after.skills
    assert not any(
        isinstance(row, dict) and row.get("asset_id") in {"owner:cand-abort", "cand-abort"}
        for row in reg_after.assets.values()
    )
    monkeypatch.setattr(store, "_transition_displace_publish_locked", orig_transition)

    # 1b) 崩溃后恢复中止置换（写 pending_operation 后进程停止且暂存失效）：同样不留孤立资产条目。
    script_abort = f"""
import os
from pathlib import Path
from agent_py_agent.agent.capability.skill_learning_publish import PublishRequest, publish_learned_skill
from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.tests.test_wave7_autonomous_learning_closure import decision
from dataclasses import replace

home = ensure_my_agent_home(Path({str(home.root)!r}))
store = SkillLearningStore.for_home(home)
orig_save = store.save_registry

def crash_on_displace_prepared(reg_obj):
    orig_save(reg_obj)
    if reg_obj.pending_operation and reg_obj.pending_operation.get("kind") == "displace_publish":
        os._exit(77)

store.save_registry = crash_on_displace_prepared
publish_learned_skill(
    store,
    PublishRequest(
        replace(decision("cand-crash-abort"), body="1. Run crash abort check.\\n2. Keep assets clean.", when_to_use="cand-crash-abort"),
        max_skills=1,
        protection_provider=lambda: {{"protection_complete": True, "protected_ids": ()}},
    ),
)
"""
    proc_abort = _run_crash_subprocess(home.root, script_abort)
    assert proc_abort.returncode == 77, proc_abort.stderr
    # 模拟用户同时接管受害者，迫使恢复阶段中止本次置换。
    store.skill_path("victim-prot").write_text("# user edited during displace crash\n", encoding="utf-8")
    fresh_abort = SkillLearningStore.for_home(home)
    abort_report = fresh_abort.recover_lifecycle()
    assert abort_report["completed"] is True
    reg_crash_abort = fresh_abort.load_registry()
    assert reg_crash_abort.pending_operation is None
    assert "cand-crash-abort" not in reg_crash_abort.skills
    assert not any(
        isinstance(row, dict) and row.get("asset_id") in {"owner:cand-crash-abort", "cand-crash-abort"}
        for row in reg_crash_abort.assets.values()
    )

    # 2) _retire_third_strike_locked 在任务家族为空且磁盘为符号链接时必须结构化 fail-closed。
    _publish_w1(store, "symlink-retire", body=f"{BODY}\n\n# symlink-retire")
    with store.state_lock():
        reg = store.load_registry()
        cur = reg.skills["symlink-retire"]
        reg = replace(
            reg,
            skills={
                **reg.skills,
                "symlink-retire": replace(cur, maintenance={**cur.maintenance, "task_family_fingerprint": ""}),
            },
        )
        store.save_registry(reg)
        skill_file = store.skill_path("symlink-retire")
        target_outside = tmp_path / "outside.md"
        target_outside.write_text("# outside\n", encoding="utf-8")
        skill_file.unlink()
        skill_file.symlink_to(target_outside)
        with pytest.raises(SkillLearningStoreError) as exc_sym:
            _retire_third_strike_locked(store, store.load_registry(), store.load_registry().skills["symlink-retire"])
        assert exc_sym.value.code == CODE_EVIDENCE_INCOMPLETE


def test_resolve_live_policy_keeps_agent_config_over_runtime_guard_policy(tmp_path: Path):
    """Real AgentConfig carries RuntimeGuardPolicy on .runtime_guard_policy; never overwrite guard_config with it."""
    from agent_py_agent.agent.settings.config import AgentConfig
    from agent_py_agent.agent.settings.runtime_guard_config import RuntimeGuardPolicy

    _, store = _home_and_store(tmp_path)
    real_cfg = AgentConfig(
        enable_self_learning=True,
        self_learning_maintenance_mode="autonomous",
        self_learning_max_skills=10,
        skill_guard_max_files=50,
        skill_guard_max_size_kb=256,
        runtime_guard_policy=RuntimeGuardPolicy(),
    )
    ev1 = publish_learned_skill(
        store,
        PublishRequest(
            replace(decision("real-cfg-pub"), body=f"{BODY}\n\n# real-cfg-pub"),
            guard_config=real_cfg,
            config_provider=lambda: real_cfg,
        ),
    )
    assert ev1.event == "published"

    ev2 = promote_verified_skill(
        store,
        PublishRequest(
            replace(decision("real-cfg-prom"), body=f"{BODY}\n\n# real-cfg-prom"),
            guard_config=real_cfg,
            config_provider=lambda: real_cfg,
        ),
    )
    assert ev2 is not None and ev2.event == "published"


# === Consolidated from: agent_py_agent/tests/test_sm_w2_bounded_maintenance.py ===
# W2 合同测试：V2-3 活跃索引保护、80/20 探索推荐、7d/15d/15d 独立生命周期、人工控制与有界游标维护。

def _store_with_runtime_w2(tmp_path) -> tuple[object, RuntimeRepository, SkillLearningStore]:
    home = ensure_my_agent_home(tmp_path / "home")
    repo = RuntimeRepository(runtime_db_path(home.owner_home_dir))
    store = SkillLearningStore.for_home(home, usage_runtime_provider=lambda: repo)
    return home, repo, store


def _publish_w2(store: SkillLearningStore, name: str, *, suffix: str = "v1", update_from=None):
    dec_kwargs: dict[str, object] = {
        "body": f"{BODY}\n\n# skill:{name}:{suffix}",
        "when_to_use": f"when handling {name}",
        "tags": (name, "csv"),
    }
    expected_sha256 = ""
    expected_revision = None
    if update_from is not None:
        dec_kwargs["decision"] = "update"
        dec_kwargs["update_target"] = name
        expected_sha256 = update_from.sha256
        expected_revision = update_from.revision
    dec = replace(decision(name), **dec_kwargs)
    return publish_learned_skill(
        store,
        PublishRequest(dec, expected_revision=expected_revision, expected_sha256=expected_sha256),
    )


def _patch_maint(store: SkillLearningStore, name: str, **updates: object) -> None:
    with store.state_lock():
        reg = store.load_registry()
        rec = reg.skills[name]
        updated = replace(rec, maintenance={**rec.maintenance, **updates})
        store.save_registry(replace(reg, skills={**reg.skills, name: updated}))


def test_w2_v2_3_active_index_protection_never_scans_cold_threads_and_pauses_on_truncation(tmp_path):
    """V2-3：有 2000 个冷线程时严禁调用 threads.list_report；仅扫描活跃索引与提案，预算耗尽返回 incomplete 并暂停破坏性迁移。"""
    home, repo, store = _store_with_runtime_w2(tmp_path)
    _publish_w2(store, "prop-sk")
    prop_rec = store.load_registry().skills["prop-sk"]
    propose_learning(
        store,
        PublishRequest(
            replace(
                decision("prop-sk"),
                decision="update",
                update_target="prop-sk",
                body=f"{BODY}\n\n# proposed v2",
            ),
            expected_revision=prop_rec.revision,
            expected_sha256=prop_rec.sha256,
        ),
    )

    # 制造 2000 个冷线程 JSON 文件，同时让 threads.list_report 一旦被调就立即报错。
    cold_dir = home.owner_home_dir / "conversations" / "threads"
    cold_dir.mkdir(parents=True, exist_ok=True)
    for idx in range(2000):
        (cold_dir / f"cold-{idx:04d}.json").write_text('{"id":"cold"}', encoding="utf-8")

    def _forbidden_full_scan(*_args, **_kwargs):
        raise AssertionError("V2-3 forbids full threads.list_report history scan")

    agent = SimpleNamespace(
        home_paths=home,
        runtime_db=repo,
        config=SimpleNamespace(
            retention_scan_slice_entries=128,
            retention_scan_slice_bytes=8_388_608,
            retention_scan_slice_ms=25,
        ),
        conversation_store=SimpleNamespace(
            threads=SimpleNamespace(list_report=_forbidden_full_scan),
            tasks=SimpleNamespace(
                unsettled_report=lambda: (
                    [
                        SimpleNamespace(
                            status="running",
                            to_dict=lambda: {"status": "running", "skill_refs": [{"stable_id": "owner:task-sk"}]},
                        ),
                        SimpleNamespace(
                            status="completed",
                            to_dict=lambda: {"status": "completed", "skill_refs": [{"stable_id": "owner:done-sk"}]},
                        ),
                    ],
                    (),
                )
            ),
        ),
        scheduler_repository=SimpleNamespace(
            list_jobs=lambda **_kw: (
                [
                    {
                        "skill_refs": [{"stable_id": "owner:sched-sk"}],
                        "payload": {"scheduler_skill_refs": [{"stable_id": "owner:sched-payload"}]},
                    }
                ],
                (),
            )
        ),
        subagents=SimpleNamespace(
            list_recovery_runs_report=lambda **_kw: SimpleNamespace(
                load_errors=(),
                runs=[
                    SimpleNamespace(
                        status="running",
                        attributes={"skill_snapshot_refs": [{"stable_id": "owner:subagent-sk"}]},
                    ),
                    SimpleNamespace(
                        status="DONE",
                        attributes={"skill_snapshot_refs": [{"stable_id": "owner:ended-subagent"}]},
                    ),
                ],
            )
        ),
    )

    result = learning_protection_for_agent(agent)
    assert result["protection_complete"] is True
    assert set(result["protected_ids"]) == {
        "owner:prop-sk",
        "owner:sched-payload",
        "owner:sched-sk",
        "owner:subagent-sk",
        "owner:task-sk",
    }

    # 预算截断时必须返回 protection_complete=False，且 maintain_learning(mode='autonomous') 保守跳过降冷/归档/驱逐。
    many_tasks = [{"status": "running", "skill_refs": [{"stable_id": f"owner:t-{i}"}]} for i in range(10)]
    agent_small_budget = SimpleNamespace(
        home_paths=home,
        runtime_db=repo,
        config=SimpleNamespace(
            retention_scan_slice_entries=3,
            retention_scan_slice_bytes=8_388_608,
            retention_scan_slice_ms=25,
        ),
        conversation_store=SimpleNamespace(
            threads=SimpleNamespace(list_report=_forbidden_full_scan),
            tasks=SimpleNamespace(unsettled_report=lambda: (many_tasks, ())),
        ),
        scheduler_repository=SimpleNamespace(list_jobs=lambda **_kw: ([], ())),
        subagents=SimpleNamespace(
            list_recovery_runs_report=lambda **_kw: SimpleNamespace(load_errors=(), runs=[])
        ),
    )
    truncated = learning_protection_for_agent(agent_small_budget)
    assert truncated["protection_complete"] is False

    _publish_w2(store, "stale-if-complete")
    _patch_maint(store, "stale-if-complete", baseline_at=100.0, modified_at=100.0)
    paused = maintain_learning(
        store,
        mode="autonomous",
        cool_seconds=10,
        archive_seconds=20,
        evict_seconds=30,
        protected_ids=truncated["protected_ids"],
        protection_complete=truncated["protection_complete"],
        now=1000.0,
    )
    assert paused["protection_complete"] is False
    assert paused["changed"] == []
    assert store.load_registry().skills["stale-if-complete"].maintenance["state"] == "trial"


def test_w2_recommended_learning_80_20_explore_proven_backfill_and_unknown(tmp_path):
    """recommended_learning：pinned 优先 -> ceil(rem*20%) ever_used=False 最新优先 -> 成熟高活兜底 -> 双向补齐 -> 0% 旧排序与 None 不误当未用。"""
    _, _, store = _store_with_runtime_w2(tmp_path)
    for name in (
        "pin-sk",
        "fresh-old",
        "fresh-new",
        "unk-sk",
        "est-low",
        "est-high",
        "trial-used",
    ):
        _publish_w2(store, name)

    _patch_maint(store, "pin-sk", pinned=True, state="active", quality="established", ever_used=True, last_used_at=1.0)
    _patch_maint(store, "fresh-old", ever_used=False, modified_at=100.0)
    _patch_maint(store, "fresh-new", ever_used=False, modified_at=300.0)
    # ever_used=None / missing（历史未知）绝不能当成未试用探索候选。
    _patch_maint(store, "unk-sk", ever_used=None, modified_at=999.0, state="trial", quality="trial", independent_uses=0)
    _patch_maint(
        store,
        "est-low",
        state="active",
        quality="established",
        ever_used=True,
        last_used_at=200.0,
        independent_uses=4,
    )
    _patch_maint(
        store,
        "est-high",
        state="active",
        quality="established",
        ever_used=True,
        last_used_at=500.0,
        independent_uses=6,
    )
    _patch_maint(store, "trial-used", state="trial", quality="trial", ever_used=True, last_used_at=400.0, independent_uses=2)

    reg = store.load_registry()

    # limit=6 -> pinned 1 占掉后 remaining=5 -> explore_slots = ceil(5*20/100)=1 -> fresh-new(最新未用) + 4 proven(est-high, est-low, trial-used, unk-sk)。
    rec_6 = recommended_learning(reg, limit=6, explore_percent=20)
    assert "pin-sk" in rec_6
    assert "fresh-new" in rec_6
    assert "fresh-old" not in rec_6  # explore 槽位仅 1 且 proven 足以填满，fresh-old 不抢占。
    assert {"est-high", "est-low", "trial-used", "unk-sk"} <= rec_6

    # 若 limit=7，proven 不够时，双向补齐会把剩余 fresh-old 补进推荐集。
    rec_7 = recommended_learning(reg, limit=7, explore_percent=20)
    assert rec_7 == {"pin-sk", "fresh-new", "fresh-old", "unk-sk", "est-low", "est-high", "trial-used"}

    # limit=3 -> pin-sk(1) + rem=2 -> ceil(2*20/100)=1 explore(fresh-new) + 1 proven(est-high)。
    rec_3 = recommended_learning(reg, limit=3, explore_percent=20)
    assert rec_3 == {"pin-sk", "fresh-new", "est-high"}

    # explore_percent=0 保持纯成熟/历史排序（不强制把 fresh-new 塞进探索槽）。
    rec_0 = recommended_learning(reg, limit=3, explore_percent=0)
    assert rec_0 == {"pin-sk", "est-high", "est-low"}

    # 若没有 proven，只有 fresh 技能时，proven 空缺会自动回填 fresh。
    _, _, store2 = _store_with_runtime_w2(tmp_path / "only_fresh")
    for n in ("f1", "f2", "f3"):
        _publish_w2(store2, n)
        _patch_maint(store2, n, ever_used=False)
    assert recommended_learning(store2.load_registry(), limit=2, explore_percent=20) == {"f2", "f3"}


def test_w2_independent_idle_lifecycle_observe_mode_and_control_actions(tmp_path):
    """7d/15d/15d 独立阶段与 0 禁用、idle 取 max(baseline,last_used,restored)、observe 只读、control_learning restore 与 cap 置换。"""
    _, _, store = _store_with_runtime_w2(tmp_path)

    # 1. cool_seconds=0 时 archive_seconds 仍能独立生效，且驱逐不写入 blocked_names/blocked_task_families。
    _publish_w2(store, "skip-cool")
    _patch_maint(store, "skip-cool", baseline_at=100.0, modified_at=100.0, last_used_at=0.0, restored_at=0.0)
    # 先 observe 验证零改动。
    obs = maintain_learning(
        store,
        mode="observe",
        cool_seconds=0,
        archive_seconds=100,
        evict_seconds=200,
        protection_complete=True,
        now=250.0,
    )
    assert obs["would_cool"] == []
    assert obs["would_archive"] == ["skip-cool"]
    assert obs["changed"] == []
    assert (store.learned_root / "skip-cool").is_dir()

    # autonomous 执行归档（直接 trial -> archived 跳过冷却）。
    arch_res = maintain_learning(
        store,
        mode="autonomous",
        cool_seconds=0,
        archive_seconds=100,
        evict_seconds=200,
        protection_complete=True,
        now=250.0,
        batch_ms=5000,
    )
    assert arch_res["backlog"] is False
    assert "skip-cool" in arch_res["changed"]
    archived_rec = store.load_registry().skills["skip-cool"]
    assert archived_rec.maintenance["state"] == "archived"
    assert archived_rec.maintenance["archived_at"] == 250.0
    assert store.archived_skill_path("skip-cool").is_file()

    # evict_seconds=0 禁用驱逐；设为 200 且超期后驱逐且不进黑名单。
    no_evict = maintain_learning(
        store,
        mode="autonomous",
        cool_seconds=0,
        archive_seconds=100,
        evict_seconds=0,
        protection_complete=True,
        now=10_000.0,
        batch_ms=5000,
    )
    assert no_evict["backlog"] is False
    assert no_evict["evicted"] == [] and "skip-cool" in store.load_registry().skills

    evicted = maintain_learning(
        store,
        mode="autonomous",
        cool_seconds=0,
        archive_seconds=100,
        evict_seconds=200,
        protection_complete=True,
        now=500.0,
        batch_ms=5000,
    )
    assert evicted["backlog"] is False
    assert evicted["evicted"] == ["skip-cool"]
    reg_after_evict = store.load_registry()
    assert "skip-cool" not in reg_after_evict.skills
    assert "skip-cool" not in reg_after_evict.blocked_names
    assert reg_after_evict.blocked_task_families == ()

    # 2. restore：归档额外文件触发 SKILL_LEARNING_USER_MODIFIED；过期触发 SKILL_LEARNING_ARCHIVE_EXPIRED；
    #    restore 触发 max_skills=1 置换时刷新被换出技能的 archived_at=now，并更新 restored_at 刷新 idle 基准。
    _publish_w2(store, "live-victim")
    _patch_maint(store, "live-victim", baseline_at=100.0, modified_at=100.0, last_used_at=100.0)
    _publish_w2(store, "to-restore")
    _patch_maint(store, "to-restore", baseline_at=100.0, modified_at=100.0, last_used_at=100.0)
    arch_restore_res = maintain_learning(
        store,
        mode="autonomous",
        cool_seconds=0,
        archive_seconds=50,
        evict_seconds=500,
        protected_ids=("owner:live-victim",),
        protection_complete=True,
        now=200.0,
        batch_ms=5000,
    )
    assert arch_restore_res["backlog"] is False
    assert store.load_registry().skills["to-restore"].maintenance["state"] == "archived"

    # 2a. extra file guard in archive dir
    extra_file = store.archived_skill_path("to-restore").parent / "extra.txt"
    extra_file.write_text("manual edit", encoding="utf-8")
    with pytest.raises(SkillLearningGateError) as mod_exc:
        control_learning(
            store,
            "to-restore",
            operation="restore",
            protection_complete=True,
            now=250.0,
            evict_seconds=500,
        )
    assert mod_exc.value.code == "SKILL_LEARNING_USER_MODIFIED"
    extra_file.unlink()

    # 2b. archive grace expired -> SKILL_LEARNING_ARCHIVE_EXPIRED
    with pytest.raises(SkillLearningGateError) as exp_exc:
        control_learning(
            store,
            "to-restore",
            operation="restore",
            protection_complete=True,
            now=800.0,
            evict_seconds=500,
        )
    assert exp_exc.value.code == "SKILL_LEARNING_ARCHIVE_EXPIRED"

    # 2c. protection_complete=False under cap -> SKILL_LEARNING_EVIDENCE_INCOMPLETE
    with pytest.raises(SkillLearningGateError) as inc_exc:
        control_learning(
            store,
            "to-restore",
            operation="restore",
            max_skills=1,
            protection_complete=False,
            now=300.0,
            evict_seconds=500,
        )
    assert inc_exc.value.code == "SKILL_LEARNING_EVIDENCE_INCOMPLETE"

    # 2d. live-victim 受保护时无法置换 -> SKILL_LEARNING_LIMIT_REACHED
    with pytest.raises(SkillLearningGateError) as lim_exc:
        control_learning(
            store,
            "to-restore",
            operation="restore",
            max_skills=1,
            protected_ids=("owner:live-victim",),
            protection_complete=True,
            now=300.0,
            evict_seconds=500,
        )
    assert lim_exc.value.code == "SKILL_LEARNING_LIMIT_REACHED"

    # 2e. 通过 config_provider 提供 max_skills=1, evict_seconds=500 且解除 victim 保护：restore 成功并换出 victim，重置其 archived_at=300.0
    before_usage_bytes = (store.directory / "usage_summary.json").read_bytes() if (store.directory / "usage_summary.json").exists() else b""
    restored = control_learning(
        store,
        "to-restore",
        operation="restore",
        protection_complete=True,
        now=300.0,
        config_provider=lambda: SimpleNamespace(self_learning_max_skills=1, self_learning_evict_seconds=500),
    )
    assert restored["maintenance"]["state"] == "trial"
    reg_r = store.load_registry()
    assert reg_r.skills["to-restore"].maintenance["restored_at"] == 300.0
    assert reg_r.skills["live-victim"].maintenance["state"] == "archived"
    assert reg_r.skills["live-victim"].maintenance["archived_at"] == 300.0
    # 恢复不得伪造 usage 事件。
    after_usage_bytes = (store.directory / "usage_summary.json").read_bytes() if (store.directory / "usage_summary.json").exists() else b""
    assert after_usage_bytes == before_usage_bytes

    # restored_at=300.0 保护其不因 modified_at=100.0 在 now=360.0 (idle=60 < 100) 被立即再归档。
    assert (
        maintain_learning(
            store,
            mode="observe",
            cool_seconds=0,
            archive_seconds=100,
            evict_seconds=500,
            protection_complete=True,
            now=360.0,
        )["would_archive"]
        == []
    )


def test_w2_reconcile_due_bounded_cursor_maintenance_and_sqlite_receipt_cleanup(tmp_path):
    """learning_reconcile_due 动态计算、batch_count=2 分片游标续扫、首条坏记录不饿死后续技能、以及 SQLite 已退役回执分序清扫。"""
    assert learning_reconcile_due(1000.0, 1200.0, interval_seconds=0) is False
    assert learning_reconcile_due(0.0, 1200.0, interval_seconds=600) is True
    assert learning_reconcile_due(1000.0, 1500.0, interval_seconds=600) is False
    assert learning_reconcile_due(1000.0, 1600.0, interval_seconds=600) is True

    _, repo, store = _store_with_runtime_w2(tmp_path)
    for name in ("a-corrupt", "b-stale", "c-active", "d-archived"):
        _publish_w2(store, name)

    # 升级 b-stale 产生版本历史用于版本修剪，并为 active + retired 记录写入 SQLite 明细回执。
    b_v1 = store.load_registry().skills["b-stale"]
    b_v2_evt = _publish_w2(store, "b-stale", suffix="v2", update_from=b_v1)
    b_v2 = store.load_registry().skills["b-stale"]
    c_rec = store.load_registry().skills["c-active"]
    d_rec = store.load_registry().skills["d-archived"]

    retired_pair = (str(b_v1.maintenance["incarnation_id"]), str(b_v1.maintenance["activation_id"]), b_v1.sha256)
    active_pair = (str(c_rec.maintenance["incarnation_id"]), str(c_rec.maintenance["activation_id"]), c_rec.sha256)
    archived_pair = (str(d_rec.maintenance["incarnation_id"]), str(d_rec.maintenance["activation_id"]), d_rec.sha256)

    for pair, tname in ((retired_pair, "ret-t"), (active_pair, "act-t"), (archived_pair, "arc-t")):
        used_at = 990.0 if tname == "act-t" else 10.0
        record_exact_use(
            store,
            {
                "event": "manual_get",
                "kind": "skill",
                "body_chars": 100,
                "content_sha256": pair[2],
                "task_id": f"{tname}-1",
                "incarnation_id": pair[0],
                "activation_id": pair[1],
                "strike_epoch": 0,
                "at": used_at,
            },
            runtime=repo,
        )
        record_exact_negative(
            store,
            incarnation_id=pair[0],
            activation_id=pair[1],
            content_sha256=pair[2],
            task_id=f"{tname}-hurt",
            at=used_at,
            runtime=repo,
        )

    # 构造“坏首条记录”：把 a-corrupt 的 SKILL.md 删掉使其所有权校验出错/未知，确认不会堵死后续 b-stale 的归档或回执清扫。
    (store.learned_root / "a-corrupt" / "SKILL.md").unlink()
    _patch_maint(store, "b-stale", baseline_at=10.0, modified_at=10.0, last_used_at=0.0, restored_at=0.0)
    _patch_maint(store, "c-active", baseline_at=990.0, modified_at=990.0, last_used_at=990.0)
    # 把 d-archived 先归档并设 updated timestamp 保护起来。
    _patch_maint(store, "d-archived", state="trial", baseline_at=10.0, modified_at=10.0)
    setup_res = maintain_learning(
        store,
        mode="autonomous",
        cool_seconds=0,
        archive_seconds=100,
        evict_seconds=10000,
        protected_ids=("owner:a-corrupt", "owner:b-stale", "owner:c-active"),
        protection_complete=True,
        now=500.0,
        batch_ms=5000,
    )
    assert setup_res["backlog"] is False
    assert store.load_registry().skills["d-archived"].maintenance["state"] == "archived"

    # 分片执行 maintain_learning(batch_count=2)：每次只做有界工作并持久化游标，直到 backlog=False。
    seen_phases: list[str] = []
    final_report = None
    for step in range(20):
        res = maintain_learning(
            store,
            mode="autonomous",
            cool_seconds=100,
            archive_seconds=200,
            evict_seconds=10_000,
            protected_ids=("owner:c-active",),
            protection_complete=True,
            now=1000.0 + step,
            batch_count=2,
            batch_ms=5000,
            keep_versions=1,
        )
        if res["cursor"].get("phase"):
            seen_phases.append(str(res["cursor"]["phase"]))
        if not res["backlog"]:
            final_report = res
            break
    assert final_report is not None
    assert final_report["backlog"] is False
    assert final_report["cursor"] == {}
    assert store.load_registry().last_maintenance_at >= 1000.0
    assert seen_phases  # 中间确实经过游标分片检查点。
    # 坏首条 a-corrupt 保留或安全标 user_modified，不饿死 b-stale 已被归档。
    assert store.load_registry().skills["b-stale"].maintenance["state"] == "archived"
    # retired_pair 明细与汇总已被清扫，而 active_pair & archived_pair 仍完整保留！
    assert read_usage_aggregate(repo, incarnation_id=retired_pair[0], activation_id=retired_pair[1], content_sha256=retired_pair[2]) is None
    assert not has_body_delivery_receipt(repo, incarnation_id=retired_pair[0], activation_id=retired_pair[1], content_sha256=retired_pair[2], task_id="ret-t-1")
    assert read_usage_aggregate(repo, incarnation_id=active_pair[0], activation_id=active_pair[1], content_sha256=active_pair[2]) is not None
    assert has_body_delivery_receipt(repo, incarnation_id=active_pair[0], activation_id=active_pair[1], content_sha256=active_pair[2], task_id="act-t-1")
    assert read_usage_aggregate(repo, incarnation_id=archived_pair[0], activation_id=archived_pair[1], content_sha256=archived_pair[2]) is not None
    assert b_v2_evt and b_v2

    # 直接单测 cleanup_retired_aggregates 分批删除顺序（sm-use -> sm-hurt -> sm-use-total）：
    orphan = ("inc-orphan", "act-orphan", "e" * 64)
    record_exact_use(
        store,
        {
            "event": "manual_get",
            "kind": "skill",
            "body_chars": 50,
            "content_sha256": orphan[2],
            "task_id": "orp-1",
            "incarnation_id": orphan[0],
            "activation_id": orphan[1],
            "strike_epoch": 0,
        },
        runtime=repo,
    )
    neg_orphan = record_exact_negative(
        store,
        incarnation_id=orphan[0],
        activation_id=orphan[1],
        content_sha256=orphan[2],
        task_id="orp-h",
        runtime=repo,
    )
    keep_pairs = {
        (active_pair[0], active_pair[1]),
        (archived_pair[0], archived_pair[1]),
    }
    use_k = receipt_key(orphan[0], orphan[1], orphan[2], "task:orp-1")
    hurt_k = neg_orphan["receipt_key"]
    agg_k = aggregate_key(orphan[0], orphan[1], orphan[2])

    def _has_meta(k: str) -> bool:
        with repo.transaction() as conn:
            return conn.execute("SELECT 1 FROM metadata WHERE key = ?", (k,)).fetchone() is not None

    after_cursor = ""
    deleted_steps: list[tuple[bool, bool, bool]] = []
    for _ in range(20):
        step_res = cleanup_retired_aggregates(repo, keep_pairs=keep_pairs, after_entry=after_cursor, max_entries=1)
        if step_res["deleted"] > 0:
            deleted_steps.append(
                (
                    _has_meta(use_k),
                    _has_meta(hurt_k),
                    _has_meta(agg_k),
                )
            )
        after_cursor = step_res["after_entry"]
        if step_res["complete"]:
            break
    # 严格按 sm-use -> sm-hurt -> sm-use-total 顺序删除：只有明细全部清完才最后删汇总锚点。
    assert deleted_steps == [
        (False, True, True),
        (False, False, True),
        (False, False, False),
    ]
    # keep_pairs 中活跃与归档记录依然完好。
    assert read_usage_aggregate(repo, incarnation_id=active_pair[0], activation_id=active_pair[1], content_sha256=active_pair[2]) is not None
    assert read_usage_aggregate(repo, incarnation_id=archived_pair[0], activation_id=archived_pair[1], content_sha256=archived_pair[2]) is not None


def test_w2_manual_learned_dir_deletion_converges_and_unblocks_publish(tmp_path):
    """手动删除托管 learned/archived 目录或清空 SKILL.md 后交还 user_modified 并释放槽位以放行新发布。"""
    _, _, store = _store_with_runtime_w2(tmp_path)
    _publish_w2(store, "alpha")
    _publish_w2(store, "beta")

    shutil.rmtree(store.learned_root / "alpha")
    assert "alpha" not in store.managed_records(store.load_registry())

    first = maintain_learning(
        store,
        mode="autonomous",
        cool_seconds=604800,
        archive_seconds=1296000,
        evict_seconds=1296000,
        protection_complete=True,
        batch_ms=5000,
    )
    assert first["backlog"] is False
    assert store.load_registry().skills["alpha"].maintenance["ownership"] == "user_modified"

    gamma_dec = replace(
        decision("gamma"),
        body=f"{BODY}\n\n# skill:gamma:v1",
        when_to_use="when handling gamma",
        tags=("gamma", "csv"),
    )
    gamma_evt = publish_learned_skill(store, PublishRequest(gamma_dec, max_skills=2))
    assert gamma_evt.event == "published"
    assert {"beta", "gamma"} <= set(store.managed_records(store.load_registry()))

    second = maintain_learning(
        store,
        mode="autonomous",
        cool_seconds=604800,
        archive_seconds=1296000,
        evict_seconds=1296000,
        protection_complete=True,
        batch_ms=5000,
    )
    assert second["backlog"] is False
    assert store.load_registry().skills["alpha"].maintenance["ownership"] == "user_modified"

    # 同类情形 1：已归档目录被手动整目录删除后，维护收敛为 user_modified 且不再纳入托管集合。
    _patch_maint(store, "beta", baseline_at=10.0, modified_at=10.0, last_used_at=0.0, restored_at=0.0)
    archived = maintain_learning(
        store,
        mode="autonomous",
        cool_seconds=0,
        archive_seconds=10,
        evict_seconds=10000,
        protected_ids=("owner:gamma",),
        protection_complete=True,
        now=500.0,
        batch_ms=5000,
    )
    assert archived["backlog"] is False
    assert store.load_registry().skills["beta"].maintenance["state"] == "archived"

    shutil.rmtree(store.archived_skill_path("beta").parent)
    assert "beta" not in store.managed_records(store.load_registry(), include_archived=True)

    after_arch_del = maintain_learning(
        store,
        mode="autonomous",
        cool_seconds=604800,
        archive_seconds=1296000,
        evict_seconds=1296000,
        protection_complete=True,
        batch_ms=5000,
    )
    assert after_arch_del["backlog"] is False
    reg_arch = store.load_registry()
    assert reg_arch.skills["beta"].maintenance["ownership"] == "user_modified"
    assert "beta" not in store.managed_records(reg_arch, include_archived=True)

    # 同类情形 2：仅删除 SKILL.md 留下空目录时，同样收敛为 user_modified 并从托管记录释放。
    _publish_w2(store, "delta")
    (store.learned_root / "delta" / "SKILL.md").unlink()
    assert (store.learned_root / "delta").is_dir()

    after_empty = maintain_learning(
        store,
        mode="autonomous",
        cool_seconds=604800,
        archive_seconds=1296000,
        evict_seconds=1296000,
        protection_complete=True,
        batch_ms=5000,
    )
    assert after_empty["backlog"] is False
    reg_empty = store.load_registry()
    assert reg_empty.skills["delta"].maintenance["ownership"] == "user_modified"
    assert "delta" not in store.managed_records(reg_empty)


# === Consolidated from: agent_py_agent/tests/test_sm_w5_carrier_hygiene.py ===
# W5 离线合同测试：lesson_carrier 互斥分类、runner 独家路由与 S1/S3 有界裁剪。

def _make_candidate(
    content: str,
    *,
    candidate_id: str = "memory-candidate-w5test000001",
    candidate_type: str = "lesson",
    origin: str = "subagent_lesson",
    scope: dict[str, str] | None = None,
    status: str = "pending_review",
    source_task_ids: list[str] | None = None,
    source_run_ids: list[str] | None = None,
) -> MemoryCandidate:
    default_scope = {"scope_type": "project", "scope_key": "project:root", "applies_when": "并发写入共享文件时"}
    return MemoryCandidate(
        schema_version="my-agent.memory-candidate.v1",
        candidate_id=candidate_id,
        candidate_type=candidate_type,
        content=content,
        subject_key="subagent.lesson.sample",
        scope=default_scope if scope is None else scope,
        origin=origin,
        evidence_refs=[{"source_ref": "/tmp/run-1/lessons.jsonl#lesson-1"}],
        source_message_refs=[],
        source_tool_refs=[],
        source_artifact_refs=[],
        source_task_ids=["project-root"] if source_task_ids is None else source_task_ids,
        source_run_ids=["run-1"] if source_run_ids is None else source_run_ids,
        observed_at="2026-11-13T00:00:00Z",
        last_observed_at="2026-11-13T00:00:00Z",
        valid_from="",
        valid_until="",
        occurrence_count=1,
        confidence=0.8,
        proposed_action="add",
        target_entry_id="",
        conflicts_with=[],
        promotion_target="lesson",
        status=status,
        reviewer="",
        review_note="",
        created_at="2026-11-13T00:00:00Z",
        updated_at="2026-11-13T00:00:00Z",
        promoted_at="",
        promotion_ref="",
        content_hash=memory_content_hash(content) if content else "",
    )


def _runner_runtime(tmp_path: Path) -> SimpleNamespace:
    home = ensure_my_agent_home(tmp_path / "home")
    candidates = CandidateService(home.owner_memory_candidates_jsonl)
    manager = SubAgentManager(tmp_path / "subagents", candidate_service=candidates)
    s1_service = SkillProposalService(home)
    learning_store = SkillLearningStore.for_home(home)
    return SimpleNamespace(
        home=home,
        candidates=candidates,
        manager=manager,
        s1_service=s1_service,
        learning_store=learning_store,
    )


def _s3_decision(name: str) -> SkillLearningDecision:
    return SkillLearningDecision(
        decision="create",
        reason="复用已验证排查流程",
        name=name,
        description=f"自动化技能 {name}",
        when_to_use="定位存储冲突时使用",
        tags=("storage", "verification"),
        body=f"# {name}\n\n## 适用场景\n\n定位存储冲突时使用。\n\n## 步骤\n\n1. 先读取当前版本。\n2. 再执行比较交换。\n",
    )


def _seed_s3_proposal(
    store: SkillLearningStore,
    *,
    name: str,
    request_key: str,
    created_at_ns: int | None = None,
    register_candidate_asset: bool = True,
) -> dict:
    decision = _s3_decision(name)
    publish = PublishRequest(
        decision=decision,
        request={"request_key": request_key, "run_id": f"run-{name}", "task_id": f"task-{name}"},
    )
    event = propose_learning(store, publish)
    proposal_id = event.reason
    if created_at_ns is not None:
        path = store.directory / "proposals" / f"{proposal_id}.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["created_at_ns"] = created_at_ns
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    if register_candidate_asset:
        register_asset(
            store,
            kind="skill",
            asset_id=name,
            asset=asdict(decision),
            source={"origin": "S3", "proposal_id": proposal_id},
            state="candidate",
        )
    return next(row for row in list_learning_proposals(store) if row["proposal_id"] == proposal_id)


def _enqueue_s3_request(store: SkillLearningStore, request_key: str, idx: int = 1) -> str:
    return store.enqueue(
        {
            "request_key": request_key,
            "run_id": f"run-req-{idx}",
            "task_id": f"task-req-{idx}",
            "prompt": f"整理经验请求 {idx}",
            "existing_skills": [],
        }
    )


# ---- 1. lesson_carrier 纯有界分类测试 ----


def test_lesson_carrier_distinguishes_raw_inputs_and_never_inflates_draft_template() -> None:
    declarative = _make_candidate("修改共享状态前先读取当前版本，再用精确版本做比较交换写入。")
    multi_line_declarative = _make_candidate("注意保持锁序一致。\n不要在锁内做耗时的模型调用。")
    numbered = _make_candidate("1. 先读取当前版本。\n2. 用精确版本做比较交换写入。")
    code_block = _make_candidate("执行定向回归检查：\n```bash\npytest -q agent_py_agent/tests/test_skill_proposals.py\n```")
    oversized = _make_candidate("1. 步骤一\n2. " + ("字" * _MAX_CONTENT_CHARS))
    missing_scope_empty = _make_candidate("1. 先读版本\n2. 再写盘", scope={})
    missing_applies_when = _make_candidate(
        "1. 先读版本\n2. 再写盘",
        scope={"scope_type": "project", "scope_key": "project:root", "applies_when": ""},
    )
    wrong_type = _make_candidate("1. 先读版本\n2. 再写盘", candidate_type="long_term_fact")

    assert lesson_carrier(declarative) == "lesson"
    assert lesson_carrier(multi_line_declarative) == "lesson"
    # render_lesson_draft 会包裹标题和 "## 适用场景" 节，但分类器只看原始 content，不能被模板抬升为 skill。
    draft = render_lesson_draft(declarative, "lesson-000000000000")
    _validate_skill_procedure({"body": draft.body, "when_to_use": draft.when_to_use})
    assert lesson_carrier(declarative) == "lesson"

    assert lesson_carrier(numbered) == "skill"
    assert lesson_carrier(code_block) == "skill"
    assert lesson_carrier(oversized) == "lesson"
    assert lesson_carrier(missing_scope_empty) == "lesson"
    assert lesson_carrier(missing_applies_when) == "lesson"
    assert lesson_carrier(wrong_type) == "lesson"

    for malformed in (
        None,
        "1. step\n2. step",
        {"content": "1. a\n2. b"},
        SimpleNamespace(candidate_type="lesson", content="1. a\n2. b", scope={"scope_type": "project", "scope_key": "p", "applies_when": "x"}),
        _make_candidate("   \n  "),
        _make_candidate("1. a\n2. b", scope="invalid"),  # type: ignore[arg-type]
    ):
        assert lesson_carrier(malformed) == "lesson"  # type: ignore[arg-type]


# ---- 2. runner_result_service 独家路由与 V2-4 防弹窗 ----


def test_runner_exclusive_carrier_routing_never_creates_s1_popups(tmp_path: Path) -> None:
    ctx = _runner_runtime(tmp_path)
    ctx.manager.skill_proposals = ctx.s1_service
    received: list[list[str]] = []

    def _enqueue(candidates):
        ids = [item.candidate_id for item in candidates]
        received.append(ids)
        return {
            "queued": [{"candidate_id": cid, "request_key": "aa11bb22cc33dd44", "status": "queued"} for cid in ids],
            "refused": [],
            "deferred": [],
        }

    ctx.manager.enqueue_lesson_candidates = _enqueue
    task = ctx.manager.create_run(goal="排查并沉淀工作流经验", thought="记录", plan=["检查"])
    decl = "修改共享文件前先核对版本号。"
    proc = "1. 先读取当前版本。\n2. 执行比较交换更新。"
    structured = SubAgentParsedOutput(found=True, ok=True, status="DONE", summary="完成", lessons=[decl, proc])

    result = ctx.manager.runner_result.record_runner_result(
        RecordRunnerResultParams(
            run_id=task.id,
            dry_run=False,
            ok=True,
            message="完成",
            status="DONE",
            turn_end_reason="completed",
            structured_output=structured,
        )
    )
    saved = ctx.manager.load(task.id)
    work_log = Path(saved.work_log_file).read_text(encoding="utf-8")
    recorded_candidates = ctx.candidates.list()

    assert result.ok is True and saved.status == "DONE"
    assert len(recorded_candidates) == 2
    assert len(received) == 1 and len(received[0]) == 1
    proc_cand = next(item for item in recorded_candidates if item.content == proc)
    assert received[0] == [proc_cand.candidate_id]
    assert "skill_carrier_queued=1 skill_carrier_duplicate=0 skill_carrier_deferred=0 skill_carrier_refused=0" in work_log
    assert "skill_proposals" not in work_log
    assert not ctx.home.owner_skill_proposals_dir.exists()


def test_runner_missing_disabled_or_failing_callback_defers_without_s1_fallback(tmp_path: Path) -> None:
    proc = "1. 先检查环境。\n2. 再执行写入。"

    # A. 回调缺失（即使挂着 S1 skill_proposals 也绝不回退生成 S1 待确认提案）
    ctx_missing = _runner_runtime(tmp_path / "missing")
    ctx_missing.manager.skill_proposals = ctx_missing.s1_service
    task_a = ctx_missing.manager.create_run(goal="验证缺失回调", thought="t", plan=["p"])
    res_a = ctx_missing.manager.runner_result.record_runner_result(
        RecordRunnerResultParams(
            run_id=task_a.id,
            dry_run=False,
            ok=True,
            message="ok",
            status="DONE",
            turn_end_reason="completed",
            structured_output=SubAgentParsedOutput(found=True, ok=True, status="DONE", summary="ok", lessons=[proc]),
        )
    )
    log_a = Path(ctx_missing.manager.load(task_a.id).work_log_file).read_text(encoding="utf-8")
    assert res_a.ok is True
    assert len(ctx_missing.candidates.list()) == 1
    assert "skill_carrier_deferred=1" in log_a and "SKILL_CARRIER_UNAVAILABLE" in log_a
    assert not ctx_missing.home.owner_skill_proposals_dir.exists()

    # B. 回调返回 disabled deferred / 抛错均不影响交付、保留源候选且零 S1 弹窗
    ctx_err = _runner_runtime(tmp_path / "error")
    ctx_err.manager.skill_proposals = ctx_err.s1_service
    ctx_err.manager.enqueue_lesson_candidates = lambda items: {
        "queued": [],
        "refused": [],
        "deferred": [{"candidate_id": item.candidate_id, "code": "SKILL_LEARNING_DISABLED", "reason": "learning off"} for item in items],
    }
    task_b = ctx_err.manager.create_run(goal="验证延迟回执", thought="t", plan=["p"])
    res_b = ctx_err.manager.runner_result.record_runner_result(
        RecordRunnerResultParams(
            run_id=task_b.id,
            dry_run=False,
            ok=True,
            message="ok",
            status="DONE",
            turn_end_reason="completed",
            structured_output=SubAgentParsedOutput(found=True, ok=True, status="DONE", summary="ok", lessons=[proc]),
        )
    )
    log_b = Path(ctx_err.manager.load(task_b.id).work_log_file).read_text(encoding="utf-8")
    assert res_b.ok is True
    assert len(ctx_err.candidates.list()) == 1
    assert "skill_carrier_deferred=1" in log_b
    assert not ctx_err.home.owner_skill_proposals_dir.exists()

    # C. 失败任务即使包含步骤文本也不触发自动入队
    called_on_fail = []
    ctx_fail = _runner_runtime(tmp_path / "failed")
    ctx_fail.manager.enqueue_lesson_candidates = lambda items: called_on_fail.append(items)
    task_c = ctx_fail.manager.create_run(goal="验证失败不入队", thought="t", plan=["p"])
    ctx_fail.manager.runner_result.record_runner_result(
        RecordRunnerResultParams(
            run_id=task_c.id,
            dry_run=False,
            ok=False,
            message="failed",
            status="FAILED",
            turn_end_reason="failed",
            structured_output=SubAgentParsedOutput(found=True, ok=False, status="FAILED", summary="failed", lessons=[proc]),
        )
    )
    assert called_on_fail == []
    assert not ctx_fail.home.owner_skill_proposals_dir.exists()


# ---- 3. S3 prune_learning_proposals 有界裁剪与崩溃恢复 ----


def test_s3_prune_protects_main_and_overflow_queues_respects_stale_zero_and_preserves_trial_assets(
    tmp_path: Path,
) -> None:
    home = ensure_my_agent_home(tmp_path / "home")
    store = SkillLearningStore.for_home(home)
    old_ns = 1_000_000_000 * 1_000_000
    now_sec = 1_000_200.0

    # 放入 21 条排队请求：第 1 条进主区前排，第 21 条使前排/溢出区同时存在排队键
    for idx in range(MAX_PENDING_REQUESTS + 1):
        _enqueue_s3_request(store, f"{idx:016x}", idx)
    hot_key = f"{0:016x}"
    overflow_paths = store.overflow_requests()
    assert len(overflow_paths) == 1
    overflow_key = overflow_paths[0].stem.partition("-")[2]

    kept_hot = _seed_s3_proposal(store, name="skill-hot-queued", request_key=hot_key, created_at_ns=old_ns)
    kept_overflow = _seed_s3_proposal(store, name="skill-overflow-queued", request_key=overflow_key, created_at_ns=old_ns)
    expired_pending = _seed_s3_proposal(store, name="skill-expired-pending", request_key="aaaa000000000001", created_at_ns=old_ns)
    fresh_pending = _seed_s3_proposal(
        store, name="skill-fresh-pending", request_key="aaaa000000000002", created_at_ns=int((now_sec - 5) * 1e9)
    )
    committed = _seed_s3_proposal(store, name="skill-committed-trial", request_key="aaaa000000000003", created_at_ns=old_ns)
    resolve_learning_proposal(store, committed["proposal_id"], 1, decision="confirm", actor_id="user")
    rejected = _seed_s3_proposal(store, name="skill-rejected-cand", request_key="aaaa000000000004", created_at_ns=old_ns)
    resolve_learning_proposal(store, rejected["proposal_id"], 1, decision="reject", actor_id="user")

    # stale_seconds=0：仅关闭 pending 过期，已裁决且未排队的 resolved 记录可清理，trial 正式资产纹丝不动。
    res_zero = prune_learning_proposals(store, now=now_sec, stale_seconds=0, batch_count=10)
    assert res_zero["expired"] == 0 and res_zero["resolved"] == 2 and res_zero["pruned"] == 2
    remaining_ids = {row["proposal_id"] for row in list_learning_proposals(store)}
    assert remaining_ids == {
        kept_hot["proposal_id"],
        kept_overflow["proposal_id"],
        expired_pending["proposal_id"],
        fresh_pending["proposal_id"],
    }
    reg_after_zero = store.load_registry()
    assert "skill-committed-trial" in reg_after_zero.skills
    assert (home.owner_home_dir / "skills" / "learned" / "skill-committed-trial" / "SKILL.md").is_file()
    assert any(
        row.get("state") == "trial" and row.get("asset_id") == "owner:skill-committed-trial"
        for row in reg_after_zero.assets.values()
    )
    assert not any(
        any(ref.get("proposal_id") == rejected["proposal_id"] for ref in row.get("provenance", []))
        for row in reg_after_zero.assets.values()
    )

    # 正 stale_seconds=60：仅过期未排队的陈旧 pending；主区与溢出区排队中的提案及新鲜 pending 全部保留。
    res_pos = prune_learning_proposals(store, now=now_sec, stale_seconds=60, batch_count=10)
    assert res_pos["expired"] == 1 and res_pos["pruned"] == 1 and res_pos["assets_removed"] == 1
    assert {row["proposal_id"] for row in list_learning_proposals(store)} == {
        kept_hot["proposal_id"],
        kept_overflow["proposal_id"],
        fresh_pending["proposal_id"],
    }


def test_s3_prune_crash_recovery_corrupt_tolerance_and_batch_convergence(tmp_path: Path, monkeypatch) -> None:
    home = ensure_my_agent_home(tmp_path / "home")
    store = SkillLearningStore.for_home(home)
    old_ns = 1_000_000_000 * 1_000_000
    now_sec = 1_000_500.0
    seeded = [
        _seed_s3_proposal(store, name=f"skill-batch-{i}", request_key=f"bbbb0000000000{i:02x}", created_at_ns=old_ns + i)
        for i in range(5)
    ]
    # 写入一个损坏提案文件，验证单条坏记录不会阻塞后续有效裁剪。
    bad_file = store.directory / "proposals" / f"learning-proposal-{'f' * 32}.json"
    bad_file.write_text("{corrupt", encoding="utf-8")

    # 模拟删除过程中在意图日志写入后中断（绕过 _apply_plan 的 OSError 自动回滚，模拟进程突然退出）。
    orig_unlink = skill_learning_proposals.unlink_file_beneath
    crash_once = {"armed": True}

    def _interrupt_after_one(root, parts):
        orig_unlink(root, parts)
        if crash_once["armed"] and parts != skill_learning_proposals._INTENT:
            crash_once["armed"] = False
            raise RuntimeError("simulated process exit during prune")

    monkeypatch.setattr(skill_learning_proposals, "unlink_file_beneath", _interrupt_after_one)
    with pytest.raises(RuntimeError, match="simulated process exit"):
        prune_learning_proposals(store, now=now_sec, stale_seconds=60, batch_count=2)
    monkeypatch.undo()
    assert (store.directory / ".proposals-intent.json").exists()

    # 下次调用自动通过既有 .proposals-intent.json 恢复被中断的那批删除及资产清理，并继续推进剩余批次。
    r1 = prune_learning_proposals(store, now=now_sec, stale_seconds=60, batch_count=2)
    assert not (store.directory / ".proposals-intent.json").exists()
    assert r1["pruned"] == 2 and r1["corrupt_skipped"] >= 1
    r2 = prune_learning_proposals(store, now=now_sec, stale_seconds=60, batch_count=2)
    assert r2["pruned"] == 1 and r2["has_more"] is False
    r3 = prune_learning_proposals(store, now=now_sec, stale_seconds=60, batch_count=2)
    assert r3["pruned"] == 0 and r3["remaining"] == 0

    for row in seeded:
        assert not (store.directory / "proposals" / f"{row['proposal_id']}.json").exists()
    assert store.load_registry().assets == {}


# ---- 4. S1 prune_orphaned 裁决标记保留、批收敛与锁序验证 ----


def _seed_s1(ctx: SimpleNamespace, lesson: str, run_id: str) -> tuple[str, str]:
    task = SubAgentTask(
        id=run_id,
        root_id=f"root-{run_id}",
        goal="测试 S1 提案生命周期",
        thought="",
        plan=[],
        output_json=f"/artifacts/{run_id}/output.json",
        updated_at=100.0,
    )
    recorded = ctx.manager.memory_candidates.record_result_candidates(task, lessons=[lesson], findings=[])
    [proposal] = seed_proposals_from_candidates(ctx.s1_service, recorded)
    return proposal.proposal_id, recorded[0].candidate_id


def test_s1_prune_orphaned_rules_decision_markers_corrupt_tolerance_and_batch_convergence(tmp_path: Path) -> None:
    ctx = _runner_runtime(tmp_path)
    p_active, _c_active = _seed_s1(ctx, "保留活跃来源提案的正文内容。", "run-active")
    p_committed, c_committed = _seed_s1(ctx, "保留已确认裁决标记的经验内容。", "run-committed")
    p_rejected, c_rejected = _seed_s1(ctx, "保留已拒绝裁决标记的经验内容。", "run-rejected")
    p_inactive, c_inactive = _seed_s1(ctx, "来源停用时应保留待重试的经验。", "run-inactive")
    p_missing, c_missing = _seed_s1(ctx, "来源彻底删除后应清理的提案一。", "run-missing")
    p_redacted, _c_redacted = _seed_s1(ctx, "来源正文脱敏后应清理的提案二。", "run-redacted")
    p_changed, c_changed = _seed_s1(ctx, "来源哈希改写后应清理的提案三。", "run-changed")

    # 确认 committed 与 rejected 后删源，两者仍必须保留作为用户裁决标记。
    committed_item = ctx.s1_service.show(p_committed)
    assert ctx.s1_service.confirm(p_committed, committed_item.revision).ok
    rejected_item = ctx.s1_service.show(p_rejected)
    assert ctx.s1_service.reject(p_rejected, rejected_item.revision).ok

    for cid in (c_committed, c_rejected, c_inactive, c_missing):
        ctx.candidates.transition(cid, "rejected", reviewer="admin")
    ctx.candidates.delete_terminal([c_committed, c_rejected, c_missing])
    ctx.candidates.redact_content(content_hashes={ctx.s1_service.show(p_redacted).source.content_hash})

    # 改写 p_changed 源候选内容与 hash
    with ctx.candidates._locked_candidates():
        from agent_py_agent.agent.memory_store import candidates as cand_mod

        rows = ctx.candidates._load_unlocked()
        idx = rows.index_for(c_changed)
        new_text = "来源已经被其他写入者改写。"
        rows[idx] = replace(rows[idx], content=new_text, content_hash=memory_content_hash(new_text))
        cand_mod._write_candidates_unlocked(ctx.candidates.path, rows, admission=cand_mod._NullAdmission())

    # 插入一条无关 S3 候选资产与一条损坏 S1 提案文件
    register_asset(
        ctx.learning_store,
        kind="skill",
        asset_id="s3-keep-asset",
        asset={"name": "s3-keep-asset", "body": "1. a\n2. b"},
        source={"origin": "S3", "proposal_id": "learning-proposal-" + "0" * 32},
        state="candidate",
    )
    (ctx.home.owner_skill_proposals_dir / ("a" * 24 + ".json")).write_text("{broken json", encoding="utf-8")

    # 分批收敛：共有 3 条可清理（missing, redacted, changed），batch_count=2 需两轮清完。
    assert ctx.s1_service.prune_orphaned(batch_count=2) == 2
    assert ctx.s1_service.prune_orphaned(batch_count=2) == 1
    assert ctx.s1_service.prune_orphaned(batch_count=2) == 0

    existing_files = {p.stem for p in ctx.home.owner_skill_proposals_dir.glob("*.json") if not p.name.startswith(".")}
    assert {p_active, p_committed, p_rejected, p_inactive}.issubset(existing_files)
    assert existing_files.isdisjoint({p_missing, p_redacted, p_changed})
    assert ctx.s1_service.show(p_committed).status == PROPOSAL_COMMITTED
    assert ctx.s1_service.show(p_rejected).status == PROPOSAL_REJECTED

    # 验证已清理的 S1 candidate 资产被清除，而 committed trial 资产与 S3 资产完整保留。
    reg = ctx.learning_store.load_registry()
    all_asset_props = {
        ref.get("proposal_id")
        for row in reg.assets.values()
        for ref in row.get("provenance", [])
        if isinstance(ref, dict)
    }
    assert all_asset_props.isdisjoint({p_missing, p_redacted, p_changed})
    assert p_committed in all_asset_props
    assert ("learning-proposal-" + "0" * 32) in all_asset_props


def test_s1_prune_retains_unreadable_source_proposals(tmp_path: Path) -> None:
    ctx = _runner_runtime(tmp_path)
    p_id, _ = _seed_s1(ctx, "账本损坏不可读时应保留待恢复的经验。", "run-unreadable")
    ctx.candidates.path.write_text("{not-valid-jsonl\n", encoding="utf-8")

    assert ctx.s1_service.prune_orphaned(batch_count=10) == 0
    assert (ctx.home.owner_skill_proposals_dir / f"{p_id}.json").is_file()


def test_s1_prune_releases_directory_lock_before_learning_state_lock_concurrency(tmp_path: Path, monkeypatch) -> None:
    ctx = _runner_runtime(tmp_path)
    ids = [_seed_s1(ctx, f"并发锁序检查提案 {i}。", f"run-lock-{i}") for i in range(4)]
    for cid in [c for _, c in ids[:2]]:
        ctx.candidates.transition(cid, "rejected", reviewer="admin")
    ctx.candidates.delete_terminal([c for _, c in ids[:2]])

    s1_lock_held = False
    state_lock_attempted_while_s1_held = False
    inside_state_lock = threading.Event()
    s1_lock_released = threading.Event()
    allow_state_lock_exit = threading.Event()
    errors: list[BaseException] = []

    orig_locked = ctx.s1_service._locked

    from contextlib import contextmanager

    @contextmanager
    def _tracked_s1_locked():
        nonlocal s1_lock_held
        with orig_locked():
            s1_lock_held = True
            try:
                yield
            finally:
                s1_lock_held = False
                s1_lock_released.set()

    monkeypatch.setattr(ctx.s1_service, "_locked", _tracked_s1_locked)

    orig_for_home = SkillLearningStore.for_home

    def _tracked_for_home(home_paths):
        nonlocal state_lock_attempted_while_s1_held
        store = orig_for_home(home_paths)
        if s1_lock_held:
            state_lock_attempted_while_s1_held = True
        return store

    monkeypatch.setattr(SkillLearningStore, "for_home", staticmethod(_tracked_for_home))

    # 线程 B 持有 learning store.state_lock() 期间，线程 A 仍可完整执行并释放 S1 锁，然后等待 state_lock 放行完成资产清理，无任何反向死锁。
    def _hold_learning_state():
        try:
            with ctx.learning_store.state_lock():
                inside_state_lock.set()
                assert allow_state_lock_exit.wait(timeout=5.0)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def _run_s1_prune():
        try:
            assert inside_state_lock.wait(timeout=5.0)
            assert ctx.s1_service.prune_orphaned(batch_count=2) == 2
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t_state = threading.Thread(target=_hold_learning_state)
    t_prune = threading.Thread(target=_run_s1_prune)
    t_state.start()
    t_prune.start()
    assert s1_lock_released.wait(timeout=5.0)
    allow_state_lock_exit.set()
    t_prune.join(timeout=5.0)
    t_state.join(timeout=5.0)

    assert not t_prune.is_alive() and not t_state.is_alive()
    assert errors == []
    assert state_lock_attempted_while_s1_held is False
    assert {p for p, _ in ids[:2]}.isdisjoint({p.stem for p in ctx.home.owner_skill_proposals_dir.glob("*.json")})
    # 确认 S1 锁从未与 learning state_lock 嵌套（静态覆盖：even with fake _verify_source）
    assert hasattr(skill_proposals, "lesson_carrier")


# === Consolidated from: agent_py_agent/tests/test_sm_w6_lesson_repository.py ===
# W6 Lesson/HOT 生命周期、热度路由与永久非破坏性归档综合行为测试。
#
# 全部使用 pytest tmp_path、临时测试替身与线程事件屏障，不触碰真实宿主目录或网络。

def _repo(tmp_path: Path, *, coordinator_io: bool = False) -> tuple[LessonRepository, HotRuleRepository]:
    memory_dir = tmp_path / "memory"
    return (
        LessonRepository(
            memory_dir / "lessons",
            memory_dir / "routing" / "INDEX.md",
            coordinator_io=coordinator_io,
        ),
        HotRuleRepository(memory_dir / "memory-hot.md", coordinator_io=coordinator_io),
    )


def _cand(
    cid: str,
    *,
    subject_key: str = "testing.lifecycle",
    content: str = "先复现失败边界，再实施最小修改。",
    scope_type: str = "personal",
    scope_key: str = "personal",
    occurrence_count: int = 2,
    proposed_action: str = "add",
    promotion_target: str = "lesson",
    candidate_type: str = "lesson",
    target_entry_id: str = "",
) -> MemoryCandidate:
    now_iso = "2026-10-01T00:00:00+00:00"
    return MemoryCandidate(
        schema_version="my-agent.candidate.v1",
        candidate_id=cid,
        candidate_type=candidate_type,
        content=content,
        subject_key=subject_key,
        scope={"scope_type": scope_type, "scope_key": scope_key},
        origin="reviewed",
        evidence_refs=[{"kind": "task", "id": f"t-{cid}-1"}, {"kind": "run", "id": f"r-{cid}-1"}],
        source_message_refs=[],
        source_tool_refs=[],
        source_artifact_refs=[],
        source_task_ids=[f"task-{cid}-1", f"task-{cid}-2"],
        source_run_ids=[f"run-{cid}-1", f"run-{cid}-2"],
        observed_at=now_iso,
        last_observed_at=now_iso,
        valid_from=now_iso,
        valid_until="",
        occurrence_count=occurrence_count,
        confidence=0.92,
        proposed_action=proposed_action,
        target_entry_id=target_entry_id,
        conflicts_with=[],
        promotion_target=promotion_target,
        status="approved",
        reviewer="admin",
        review_note="",
        created_at=now_iso,
        updated_at=now_iso,
        promoted_at="",
        promotion_ref="",
    )


def _hot_cand(
    cid: str,
    *,
    target_lesson_id: str,
    subject_key: str = "testing.lifecycle",
    rule: str = "提交前跑定向测试。",
    occurrence_count: int = 3,
) -> MemoryCandidate:
    return _cand(
        cid,
        subject_key=subject_key,
        content=rule,
        occurrence_count=occurrence_count,
        promotion_target="hot",
        candidate_type="hot_rule",
        target_entry_id=target_lesson_id,
    )


def _make_record(
    index: int,
    *,
    subject_key: str | None = None,
    content: str | None = None,
    updated_at: str = "2026-10-01T00:00:00+00:00",
    created_at: str = "2026-10-01T00:00:00+00:00",
    status: str = "active",
    revision: int = 1,
    superseded_by: str = "",
    superseded_at: str = "",
) -> tuple[LessonRecord, str]:
    lid = f"lesson-{index:04d}{index:04x}"
    subj = subject_key or f"subj.case{index:04d}"
    fname = f"subj-case{index:04d}-{lid[-8:]}.md"
    rec = LessonRecord(
        schema_version=lesson_module.LESSON_SCHEMA_VERSION,
        lesson_id=lid,
        candidate_id=f"cand-{index}",
        subject_key=subj,
        scope={"scope_type": "personal", "scope_key": "personal"},
        content=content or f"教训条目 #{index}",
        occurrence_count=2,
        evidence_groups=(f"run:r{index}", f"task:t{index}"),
        source_task_ids=(f"t{index}",),
        source_run_ids=(f"r{index}",),
        created_at=created_at,
        updated_at=updated_at,
        path=f"memory/lessons/{fname}",
        status=status,
        revision=revision,
        superseded_by=superseded_by,
        superseded_at=superseded_at,
    )
    return rec, fname


def _seed_raw_lesson(repo: LessonRepository, rec: LessonRecord) -> Path:
    path = repo.lessons_dir / Path(rec.path).name
    path.write_text(_render_lesson(rec), encoding="utf-8")
    return path


def _assert_valid_heat_schema(heat: dict[str, object]) -> None:
    required_top = {
        "schema_version",
        "generation",
        "updated_at",
        "rows",
        "routed_ids",
        "lessons_archived_count",
        "archive_count_coverage",
    }
    assert required_top <= set(heat)
    assert heat["schema_version"] == lesson_module.LESSON_HEAT_SCHEMA_VERSION
    assert isinstance(heat["generation"], str) and heat["generation"]
    assert isinstance(heat["updated_at"], (int, float))
    assert isinstance(heat["routed_ids"], list)
    cov = heat["archive_count_coverage"]
    assert isinstance(cov, dict) and cov["status"] in {"complete", "partial", "unavailable"}
    rows = heat["rows"]
    assert isinstance(rows, dict)
    expected_row_keys = {
        "path",
        "subject_key",
        "scope_type",
        "scope_key",
        "status",
        "revision",
        "created_at",
        "updated_at",
        "last_hit_at",
        "hits",
        "unrouted_at",
        "superseded_at",
    }
    for lid, row in rows.items():
        assert isinstance(lid, str) and lid
        assert set(row) == expected_row_keys
        assert row["status"] in {"active", "superseded"}
        assert isinstance(row["revision"], int) and row["revision"] >= 1
        assert isinstance(row["hits"], int) and row["hits"] >= 0


# ---------------------------------------------------------------------------
# 1. read_refs 批量规范直读与边界校验
# ---------------------------------------------------------------------------


def test_read_refs_empty_and_deduped_disk_reads(tmp_path: Path, monkeypatch) -> None:
    repo, _ = _repo(tmp_path)
    r1 = repo.promote(_cand("r1", subject_key="alpha.topic", content="第一条规范内容"))
    r2 = repo.promote(_cand("r2", subject_key="beta.topic", content="第二条规范内容"))

    calls: list[tuple[Path, tuple[str, ...]]] = []
    real_read = lesson_module.read_text_beneath

    def counting_read(root: Path, rel: tuple[str, ...]) -> str | None:
        calls.append((Path(root), rel))
        return real_read(root, rel)

    monkeypatch.setattr(lesson_module, "read_text_beneath", counting_read)

    # 空输入必须零磁盘 I/O。
    assert repo.read_refs([]) == {}
    assert calls == []

    # 同一文件多种合法 ref（带与不带 #lesson_id 重复）只允许打开一次。
    refs = [
        r1.path,
        f"{r1.path}#{r1.lesson_id}",
        r1.path,
        r2.path,
        f"{r2.path}#{r2.lesson_id}",
    ]
    loaded = repo.read_refs(refs)
    assert set(loaded) == {r1.path, f"{r1.path}#{r1.lesson_id}", r2.path, f"{r2.path}#{r2.lesson_id}"}
    assert loaded[r1.path] == r1
    assert loaded[f"{r1.path}#{r1.lesson_id}"] == r1
    assert loaded[r2.path] == r2
    assert len(calls) == 2


@pytest.mark.parametrize(
    "bad_ref",
    [
        "",
        " ",
        "../lessons/a-01234567.md",
        "memory/lessons/../hot.md",
        "memory/lessons/sub/a-01234567.md",
        "memory/lessons/archive/2026-01/a-01234567.md",
        "memory/other/a-01234567.md",
        "memory/lessons/topic.bak.md",
        "memory/lessons/topic.md",
        "memory/lessons/a-01234567.md#",
        "memory/lessons/a-01234567.md#a/b",
        "memory/lessons/a-01234567.md#a#b",
        "memory/lessons/a-01234567.md\n",
        "memory/lessons/a-01234567.md\x00",
        "memory\\lessons\\a-01234567.md",
    ],
)
def test_read_refs_rejects_invalid_refs_even_mixed_with_valid(tmp_path: Path, bad_ref: str) -> None:
    repo, _ = _repo(tmp_path)
    valid = repo.promote(_cand("mix1", subject_key="valid.topic"))
    with pytest.raises(ValueError):
        repo.read_refs([valid.path, bad_ref])


def test_read_refs_rejects_symlinks_missing_is_omitted_and_mismatches_fail(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    rec = repo.promote(_cand("sym1", subject_key="sym.topic"))
    missing_ref = "memory/lessons/missing-deadbeef.md"
    assert repo.read_refs([missing_ref]) == {}

    # 片段 ID 不匹配直接报错。
    with pytest.raises(ValueError, match="LESSON_HEADER_ID_MISMATCH"):
        repo.read_refs([f"{rec.path}#lesson-wrongid"])

    # 头部 path 与文件名不一致直接报错。
    tampered_name = "tampered-01234567.md"
    (repo.lessons_dir / tampered_name).write_text(_render_lesson(rec), encoding="utf-8")
    with pytest.raises(ValueError, match="LESSON_HEADER_PATH_MISMATCH"):
        repo.read_refs([f"memory/lessons/{tampered_name}"])
    (repo.lessons_dir / tampered_name).unlink()

    # 未迁移环境下 legacy 无 marker 文件容忍跳过；migration.json 存在后损坏直接报错。
    legacy_name = "legacy-01234567.md"
    (repo.lessons_dir / legacy_name).write_text("# legacy note\n", encoding="utf-8")
    assert repo.read_refs([f"memory/lessons/{legacy_name}"]) == {}
    (repo.lessons_dir.parent / "migration.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="requires migration"):
        repo.read_refs([f"memory/lessons/{legacy_name}"])
    (repo.lessons_dir / legacy_name).unlink()

    # 符号链接文件与符号链接目录均以 NoFollowPathError 拒绝。
    outside = tmp_path / "outside.md"
    outside.write_text("external", encoding="utf-8")
    sym_file = repo.lessons_dir / "symlinked-01234567.md"
    try:
        os.symlink(outside, sym_file)
    except OSError:
        pytest.skip("当前环境不支持创建 symlink")
    with pytest.raises(NoFollowPathError):
        repo.read_refs(["memory/lessons/symlinked-01234567.md"])


# ---------------------------------------------------------------------------
# 2. note_hits 零磁盘 I/O 与维护并发交错不丢信号
# ---------------------------------------------------------------------------


def test_note_hits_zero_disk_io_dedupes_and_survives_concurrent_maintain(
    tmp_path: Path, monkeypatch
) -> None:
    repo, _ = _repo(tmp_path)
    r1 = repo.promote(_cand("h1", subject_key="hit.one", content="命中第一条"))
    r2 = repo.promote(_cand("h2", subject_key="hit.two", content="命中第二条"))

    io_events: list[str] = []
    real_write = lesson_module.write_private_text_file_atomic_unlocked
    real_read = lesson_module.read_text_beneath

    def spy_write(path, *args, **kwargs):
        io_events.append(f"write:{Path(path).name}")
        return real_write(path, *args, **kwargs)

    def spy_read(root, rel):
        io_events.append(f"read:{rel}")
        return real_read(root, rel)

    monkeypatch.setattr(lesson_module, "write_private_text_file_atomic_unlocked", spy_write)
    monkeypatch.setattr(lesson_module, "read_text_beneath", spy_read)

    hit_t1 = datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc)
    repo.note_hits([r1.lesson_id, r1.lesson_id, " ", r2.lesson_id], at=hit_t1)
    assert io_events == [], "note_hits 不允许做任何同步磁盘读写"

    # 恢复原读写后做一次并发 maintain 交错验证：在 _drain_pending_hits 之后写入窗口追加的命中必须保留至下一轮。
    monkeypatch.undo()
    inside_compute = threading.Event()
    proceed_maintain = threading.Event()
    errors: list[BaseException] = []
    real_compute = lesson_module._compute_routing_from_rows

    def slow_compute(*args, **kwargs):
        inside_compute.set()
        assert proceed_maintain.wait(timeout=5.0)
        return real_compute(*args, **kwargs)

    monkeypatch.setattr(lesson_module, "_compute_routing_from_rows", slow_compute)

    def run_maintain() -> None:
        try:
            repo.maintain(now=datetime(2026, 10, 5, 11, 0, tzinfo=timezone.utc))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    worker = threading.Thread(target=run_maintain)
    worker.start()
    assert inside_compute.wait(timeout=5.0)
    hit_t2 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    repo.note_hits([r2.lesson_id], at=hit_t2)
    proceed_maintain.set()
    worker.join(timeout=5.0)
    assert not worker.is_alive() and not errors
    monkeypatch.undo()

    heat_mid = json.loads(repo.heat_path.read_text(encoding="utf-8"))
    assert heat_mid["rows"][r1.lesson_id]["hits"] == 1
    assert heat_mid["rows"][r2.lesson_id]["hits"] == 1

    # 下一轮 maintain 吸收并发期间暂存的 hit_t2。
    repo.maintain(now=datetime(2026, 10, 5, 12, 1, tzinfo=timezone.utc))
    heat_after = json.loads(repo.heat_path.read_text(encoding="utf-8"))
    assert heat_after["rows"][r1.lesson_id]["hits"] == 1
    assert heat_after["rows"][r2.lesson_id]["hits"] == 2
    assert heat_after["rows"][r2.lesson_id]["last_hit_at"] == pytest.approx(hit_t2.timestamp())


# ---------------------------------------------------------------------------
# 3. active_for_subject + promote_with_receipt 冲突、scope 规范与并发互斥
# ---------------------------------------------------------------------------


def test_active_for_subject_fail_closed_and_alias_scope_conflict(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    assert repo.active_for_subject("empty.topic", {"scope_type": "personal", "scope_key": "personal"}) is None

    # project scope 规范别名 project:alpha / task:alpha / alpha 等价对齐。
    first, changed1 = repo.promote_with_receipt(
        _cand(
            "scope-a",
            subject_key="deploy.scope",
            content="先构建产物再验证端口。",
            scope_type="project",
            scope_key="alpha",
        )
    )
    assert changed1 is True
    found = repo.active_for_subject("deploy.scope", {"scope_type": "project", "scope_key": "project:alpha"})
    assert found == first

    # 同主题、同规范正文但不同 candidate_id 幂等复用既有记录。
    reused, changed2 = repo.promote_with_receipt(
        _cand(
            "scope-b",
            subject_key="deploy.scope",
            content="  先构建产物再验证端口。  ",
            scope_type="project",
            scope_key="task:alpha",
        )
    )
    assert changed2 is False and reused.lesson_id == first.lesson_id

    # 同主题（别名 scope）、不同正文触发 MemorySubjectConflict。
    with pytest.raises(MemorySubjectConflict) as conflict:
        repo.promote_with_receipt(
            _cand(
                "scope-c",
                subject_key="deploy.scope",
                content="完全不同的替代方案正文。",
                scope_type="project",
                scope_key="task:alpha",
            )
        )
    assert conflict.value.subject_key == "deploy.scope"
    assert conflict.value.entry_id == first.lesson_id

    # 损坏 lesson_heat.json 时必须 fail-closed；执行 maintain() 自愈后恢复。
    repo.heat_path.write_text("{corrupt json", encoding="utf-8")
    with pytest.raises(LessonHeatUnavailableError):
        repo.active_for_subject("deploy.scope", {"scope_type": "project", "scope_key": "alpha"})

    # 删掉 lesson_heat.json 但仍存在规范 .md 文件时同样 fail-closed，避免误判“无同主题”。
    repo.heat_path.unlink()
    with pytest.raises(LessonHeatUnavailableError):
        repo.active_for_subject("deploy.scope", {"scope_type": "project", "scope_key": "alpha"})

    repo.maintain()
    assert repo.active_for_subject("deploy.scope", {"scope_type": "project", "scope_key": "alpha"}) == first


def test_concurrent_promote_same_subject_allows_only_one_active_lesson(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    barrier = threading.Barrier(2)
    results: list[tuple[int, str, str | None]] = []
    lock = threading.Lock()

    def worker(idx: int) -> None:
        barrier.wait(timeout=5.0)
        try:
            rec, changed = repo.promote_with_receipt(
                _cand(
                    f"conc-{idx}",
                    subject_key="conc.subject",
                    content=f"并发分支 {idx} 的独立内容。",
                )
            )
            outcome = (idx, "ok" if changed else "reused", rec.lesson_id)
        except MemorySubjectConflict as exc:
            outcome = (idx, "conflict", exc.entry_id)
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker, args=(i,)) for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)
        assert not t.is_alive()

    statuses = sorted(item[1] for item in results)
    assert statuses == ["conflict", "ok"]
    assert len(repo.list()) == 1


# ---------------------------------------------------------------------------
# 4. supersede_with_receipt CAS、HOT 退役、协调门重放与并发竞争
# ---------------------------------------------------------------------------


def test_supersede_validation_lifecycle_and_hot_retirement(tmp_path: Path) -> None:
    repo, hot = _repo(tmp_path)
    old = repo.promote(_cand("sup-1", subject_key="net.timeout", content="旧规则正文：超时重试 3 次。"))
    other = repo.promote(_cand("keep-1", subject_key="disk.sync", content="另一主题保留不变。"))

    hot_old, _ = hot.promote_with_receipt(
        _hot_cand("hot-old", target_lesson_id=old.lesson_id, subject_key="net.timeout", rule="旧 HOT：超时重试 3 次。"),
        lesson=old,
    )
    hot_other, _ = hot.promote_with_receipt(
        _hot_cand("hot-other", target_lesson_id=other.lesson_id, subject_key="disk.sync", rule="保留 HOT：落盘执行 fsync。"),
        lesson=other,
    )
    assert {r.hot_id for r in hot.list()} == {hot_old.hot_id, hot_other.hot_id}

    cand_new = _cand("sup-2", subject_key="net.timeout", content="新规则正文：超时使用指数退避。")

    # 未传 expected_revision / 传错误版本 / 传 remove / 错 scope / 不存在目标均明确拒绝。
    with pytest.raises(ValueError, match="SUPERSESSION_EXPECTED_REVISION_REQUIRED"):
        repo.supersede_with_receipt(cand_new, target_lesson_id=old.lesson_id, hot=hot, expected_revision=None)
    with pytest.raises(ValueError, match="LESSON_REVISION_CONFLICT"):
        repo.supersede_with_receipt(cand_new, target_lesson_id=old.lesson_id, hot=hot, expected_revision=99)
    with pytest.raises(KeyError):
        repo.supersede_with_receipt(cand_new, target_lesson_id="lesson-nonexistent", hot=hot, expected_revision=1)
    with pytest.raises(ValueError, match="SUPERSESSION_SCOPE_OR_SUBJECT_MISMATCH"):
        repo.supersede_with_receipt(
            _cand("sup-bad-subj", subject_key="wrong.subject", content="不匹配主题"),
            target_lesson_id=old.lesson_id,
            hot=hot,
            expected_revision=old.revision,
        )
    with pytest.raises(ValueError, match="LESSON_ACTION_UNSUPPORTED"):
        repo.supersede_with_receipt(
            _cand("sup-rem", subject_key="net.timeout", content="remove action", proposed_action="remove"),
            target_lesson_id=old.lesson_id,
            hot=hot,
            expected_revision=old.revision,
        )

    # 正常替代：产生 rev=2 新教训、旧教训保留原正文改 status=superseded、旧 HOT 退役且不抄给继任者。
    successor, changed = repo.supersede_with_receipt(
        cand_new,
        target_lesson_id=old.lesson_id,
        hot=hot,
        expected_revision=old.revision,
    )
    assert changed is True
    assert successor.status == "active"
    assert successor.revision == old.revision + 1
    assert successor.supersedes == (old.lesson_id,)
    assert successor.superseded_by == ""

    old_after = repo.get(old.lesson_id)
    assert old_after.status == "superseded"
    assert old_after.superseded_by == successor.lesson_id
    assert old_after.superseded_at == successor.created_at
    assert old_after.content == old.content

    assert repo.active_for_subject("net.timeout", old.scope) == successor
    hot_remaining = hot.list()
    assert [row.hot_id for row in hot_remaining] == [hot_other.hot_id]
    assert "旧 HOT" not in hot.path.read_text(encoding="utf-8")

    # 再次对已被替代的旧 lesson 发起另一份 candidate 替代，必须冲突拒绝。
    with pytest.raises(ValueError, match="LESSON_REVISION_CONFLICT"):
        repo.supersede_with_receipt(
            _cand("sup-3", subject_key="net.timeout", content="迟来的第二次替代。"),
            target_lesson_id=old.lesson_id,
            hot=hot,
            expected_revision=old.revision,
        )


@pytest.mark.parametrize("offset", [1, 2, 3, 4, 5, 6, 7])
def test_supersede_coordinator_interruption_and_replay_converge_idempotently(
    tmp_path: Path, offset: int
) -> None:
    door = Door()
    with commits.global_write_commit_scope(door):
        repo, hot = _repo(tmp_path / "case", coordinator_io=True)
        old, _ = repo.promote_with_receipt(_cand("door-old", subject_key="door.sup", content="初始内容"))
        hot.promote_with_receipt(
            _hot_cand("door-hot", target_lesson_id=old.lesson_id, subject_key="door.sup", rule="旧规则要退役。"),
            lesson=old,
        )

    cand_new = _cand("door-new", subject_key="door.sup", content="替代后新内容。")
    door.reject_at = door.calls + offset
    with commits.global_write_commit_scope(door), pytest.raises(RuntimeError):
        repo.supersede_with_receipt(
            cand_new,
            target_lesson_id=old.lesson_id,
            hot=hot,
            expected_revision=old.revision,
        )

    # 撤掉阻塞门后重放相同调用，必须收敛到完全一致的确定性结果，且绝不报错或产生第三份教训。
    door.reject_at = None
    with commits.global_write_commit_scope(door):
        rec_replay, _ = repo.supersede_with_receipt(
            cand_new,
            target_lesson_id=old.lesson_id,
            hot=hot,
            expected_revision=old.revision,
        )

    expected_sid = (
        "lesson-"
        + hashlib.sha256(
            f"{cand_new.candidate_id}\0supersede\0{old.lesson_id}\0{old.revision}".encode()
        ).hexdigest()[:20]
    )
    assert rec_replay.lesson_id == expected_sid
    assert repo.get(old.lesson_id).status == "superseded"
    assert repo.get(old.lesson_id).superseded_by == expected_sid
    assert repo.active_for_subject("door.sup", old.scope) == rec_replay
    assert hot.list() == []
    assert expected_sid in repo.routing_index_path.read_text(encoding="utf-8")


def test_supersede_routing_pending_error_replay(tmp_path: Path, monkeypatch) -> None:
    repo, hot = _repo(tmp_path)
    old = repo.promote(_cand("pend-1", subject_key="pend.subj", content="旧正文"))
    cand_new = _cand("pend-2", subject_key="pend.subj", content="新正文")

    real_write = lesson_module.write_private_text_file_atomic_unlocked

    def fail_index(path, *args, **kwargs):
        if Path(path) == repo.routing_index_path:
            raise OSError("disk full on routing index")
        return real_write(path, *args, **kwargs)

    monkeypatch.setattr(lesson_module, "write_private_text_file_atomic_unlocked", fail_index)
    with pytest.raises(LessonRoutingPendingError) as caught:
        repo.supersede_with_receipt(
            cand_new,
            target_lesson_id=old.lesson_id,
            hot=hot,
            expected_revision=old.revision,
        )
    assert caught.value.formal_memory_changed is True

    monkeypatch.undo()
    replayed, changed = repo.supersede_with_receipt(
        cand_new,
        target_lesson_id=old.lesson_id,
        hot=hot,
        expected_revision=old.revision,
    )
    assert changed is False
    assert repo.active_for_subject("pend.subj", old.scope) == replayed


def test_concurrent_supersede_race_has_single_winner(tmp_path: Path) -> None:
    repo, hot = _repo(tmp_path)
    old = repo.promote(_cand("race-old", subject_key="race.subj", content="原始竞争正文"))
    barrier = threading.Barrier(2)
    outcomes: list[tuple[int, str]] = []
    lock = threading.Lock()

    def contender(idx: int) -> None:
        barrier.wait(timeout=5.0)
        try:
            rec, _ = repo.supersede_with_receipt(
                _cand(f"race-{idx}", subject_key="race.subj", content=f"竞争者 {idx}"),
                target_lesson_id=old.lesson_id,
                hot=hot,
                expected_revision=old.revision,
            )
            tag = ("ok", rec.lesson_id)
        except ValueError as exc:
            assert "LESSON_REVISION_CONFLICT" in str(exc)
            tag = ("conflict", "")
        with lock:
            outcomes.append((idx, tag[0]))

    threads = [threading.Thread(target=contender, args=(i,)) for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)
        assert not t.is_alive()

    assert sorted(status for _, status in outcomes) == ["conflict", "ok"]
    active = [r for r in repo.list() if r.status == "active"]
    assert len(active) == 1 and active[0].revision == 2


# ---------------------------------------------------------------------------
# 5. HOT 置顶、归档双向守卫与不完整 pin_loader 拒归档
# ---------------------------------------------------------------------------


def test_hot_pin_protection_and_archive_before_hot_guard(tmp_path: Path) -> None:
    repo, hot = _repo(tmp_path)
    t0 = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
    cold_pinned, _ = _make_record(0, subject_key="pinned.subj", content="被 HOT 置顶的冷教训")
    cold_unpinned, _ = _make_record(1, subject_key="unpinned.subj", content="未被置顶的冷教训")
    _seed_raw_lesson(repo, cold_pinned)
    _seed_raw_lesson(repo, cold_unpinned)

    # 先让一条被 HOT 引用，另一条通过超量路由跌出 INDEX.md。
    for i in range(2, 50):
        rec, _ = _make_record(
            i,
            updated_at=f"2026-10-02T00:00:{i:02d}+00:00",
            content="填充路由预算的长文本内容。" * 4,
        )
        _seed_raw_lesson(repo, rec)

    # HOT-before-archive：即便不传显式 pin_loader，或由动态 pin_loader 读取 HOT，置顶教训不得归档。
    hot.promote_with_receipt(
        _hot_cand("hot-pin-1", target_lesson_id=cold_pinned.lesson_id, subject_key="pinned.subj", rule="始终置顶保留"),
        lesson=cold_pinned,
    )
    repo.maintain(now=t0, archive_days=15, batch_ms=5000)

    # 不完整 pin_loader 必须拒绝一切归档并标记 coverage=partial。
    res_partial = repo.maintain(
        now=t0 + timedelta(days=20),
        archive_days=15,
        pin_loader=lambda: ([], False),
        batch_ms=5000,
    )
    assert res_partial["archived"] == 0
    assert res_partial["coverage"]["status"] == "partial"
    assert (repo.lessons_dir / Path(cold_unpinned.path).name).exists()

    # 完整动态 pin_loader 允许冷条目归档，但保住 HOT 置顶条目。
    res_ok = repo.maintain(
        now=t0 + timedelta(days=20),
        archive_days=15,
        pin_loader=lambda: ([row.lesson_ref for row in hot.list()], True),
        batch_ms=5000,
    )
    assert res_ok["coverage"]["status"] == "complete"
    assert (repo.lessons_dir / Path(cold_pinned.path).name).exists()
    assert not (repo.lessons_dir / Path(cold_unpinned.path).name).exists()

    # Archive-before-HOT：对已归档冷教训继续晋升 HOT 或替代必须明确拒绝。
    with pytest.raises(ValueError, match="HOT_TARGET_ARCHIVED_OR_MISSING"):
        hot.promote_with_receipt(
            _hot_cand("hot-after-arch", target_lesson_id=cold_unpinned.lesson_id, subject_key="unpinned.subj", rule="不能绑已归档教训"),
            lesson=cold_unpinned,
        )
    with pytest.raises(KeyError):
        repo.supersede_with_receipt(
            _cand("sup-after-arch", subject_key="unpinned.subj", content="不能替代已归档"),
            target_lesson_id=cold_unpinned.lesson_id,
            hot=hot,
            expected_revision=cold_unpinned.revision,
        )


# ---------------------------------------------------------------------------
# 6. 15 天边界、命中与回流重置时钟、锁隔离、双活性收敛与不递归归档读
# ---------------------------------------------------------------------------


def test_maintain_15_day_boundaries_clocks_locks_permanent_archive_and_non_recursive_reads(
    tmp_path: Path,
) -> None:
    repo, hot = _repo(tmp_path)
    t0 = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)

    # 构造 50 条足以挤出尾部冷教训的活跃记录 + 1 条已替代教训。
    for i in range(50):
        rec, _ = _make_record(
            i,
            updated_at=f"2026-10-01T01:00:{i:02d}+00:00",
            content="填充用教训正文。" * 6,
        )
        _seed_raw_lesson(repo, rec)

    cold_rec, cold_fname = _make_record(900, subject_key="cold.clock", content="冷教训主体")
    _seed_raw_lesson(repo, cold_rec)
    sup_rec, sup_fname = _make_record(
        901,
        subject_key="sup.clock",
        status="superseded",
        superseded_by="lesson-successor",
        superseded_at=t0.isoformat(timespec="seconds"),
    )
    _seed_raw_lesson(repo, sup_rec)
    # 故意放置一个活跃/遗留 .lock 文件，严禁被归档搬走。
    stale_lock = repo.lessons_dir / f"{cold_fname}.lock"
    stale_lock.write_text('{"pid": 999999}', encoding="utf-8")

    # 首次 maintain 对缺失时间戳做保守重建：时钟自 t0 开始，当天两者均保留。
    repo.maintain(now=t0, archive_days=15, batch_ms=5000)
    assert (repo.lessons_dir / cold_fname).exists()
    assert (repo.lessons_dir / sup_fname).exists()

    # 差 1 秒满 15 天仍保留。
    just_before = t0 + timedelta(days=15) - timedelta(seconds=1)
    res_before = repo.maintain(now=just_before, archive_days=15, batch_ms=5000)
    assert res_before["archived"] == 0
    assert (repo.lessons_dir / cold_fname).exists()
    assert (repo.lessons_dir / sup_fname).exists()

    # 第 10 天给 cold_rec 一次命中，刷新活跃时钟：26 条未命中的出局填充 + sup_rec 满 15 天归档，cold_rec 仍保留。
    hit_time = t0 + timedelta(days=10)
    repo.note_hits([cold_rec.lesson_id], at=hit_time)
    at_15d = t0 + timedelta(days=15)
    res_15d = repo.maintain(now=at_15d, archive_days=15, batch_ms=5000)
    assert res_15d["archived"] == 27
    assert not (repo.lessons_dir / sup_fname).exists()
    sup_archive_path = repo.lessons_dir / "archive" / f"{at_15d.year:04d}-{at_15d.month:02d}" / sup_fname
    assert sup_archive_path.exists()
    assert (repo.lessons_dir / cold_fname).exists()
    assert stale_lock.exists(), ".lock 侧车严禁被搬进 archive/"
    assert not list((repo.lessons_dir / "archive").rglob("*.lock"))

    # 令 cold_rec 多次命中回流 INDEX.md 后再冷却，时钟从重新出局时重新算，而非继承旧 unrouted_at。
    reenter_time = t0 + timedelta(days=16)
    repo.note_hits([cold_rec.lesson_id], at=reenter_time)
    repo.maintain(now=reenter_time, archive_days=15, batch_ms=5000)
    heat_reenter = json.loads(repo.heat_path.read_text(encoding="utf-8"))
    assert cold_rec.lesson_id in heat_reenter["routed_ids"]
    assert heat_reenter["rows"][cold_rec.lesson_id]["unrouted_at"] == 0.0

    # archive_days=0 仅关闭自动归档，仍同步 heat 与归档总数。
    res_disabled = repo.maintain(now=t0 + timedelta(days=100), archive_days=0, batch_ms=5000)
    assert res_disabled["archived"] == 0
    assert (repo.lessons_dir / cold_fname).exists()
    assert res_disabled["lessons_archived_count"] == 27

    # 真正进入 +10 年后（期间不再命中），若前面未归档的冷教训超过窗口可被归档，但已有归档文件永久保留不删。
    far_future = t0 + timedelta(days=3650)
    repo.maintain(now=far_future, archive_days=15, batch_ms=5000)
    assert sup_archive_path.exists(), "永久归档文件十年后仍必须保留"

    # 活跃读接口（list/get/read_refs/rebuild_routing_index）严禁递归进 archive/。
    # 在 archive 内放一个不可读/损坏文件也绝不能拖垮活跃读。
    (repo.lessons_dir / "archive" / "2020-01").mkdir(parents=True, exist_ok=True)
    (repo.lessons_dir / "archive" / "2020-01" / "corrupt-deadbeef.md").write_text("bad", encoding="utf-8")
    live_ids = {r.lesson_id for r in repo.list()}
    assert sup_rec.lesson_id not in live_ids
    with pytest.raises(KeyError):
        repo.get(sup_rec.lesson_id)
    assert repo.read_refs([sup_rec.path]) == {}
    repo.rebuild_routing_index()
    idx_text = repo.routing_index_path.read_text(encoding="utf-8")
    assert sup_rec.lesson_id not in idx_text


def test_maintain_reconciles_manual_restore_duplicate_active_authority(tmp_path: Path) -> None:
    repo, hot = _repo(tmp_path)
    rev1 = repo.promote(_cand("dup-1", subject_key="dup.restore", content="版本 1 旧内容"))
    rev2, _ = repo.supersede_with_receipt(
        _cand("dup-2", subject_key="dup.restore", content="版本 2 新内容"),
        target_lesson_id=rev1.lesson_id,
        hot=hot,
        expected_revision=rev1.revision,
    )
    later = datetime.now(timezone.utc) + timedelta(days=20)
    repo.maintain(now=later, archive_days=15)
    archived = repo.lessons_dir / "archive" / f"{later.year:04d}-{later.month:02d}" / Path(rev1.path).name
    assert archived.exists()

    # 模拟运维把旧版 rev1 文件手工改回 active 拷贝回 lessons/，造成同一 subject/scope 双活。
    restored_rev1 = _render_lesson(
        LessonRecord(
            **{
                **rev1.__dict__,
                "status": "active",
                "superseded_by": "",
                "superseded_at": "",
            }
        )
    )
    (repo.lessons_dir / Path(rev1.path).name).write_text(restored_rev1, encoding="utf-8")
    repo.maintain(now=later, archive_days=15)

    assert repo.get(rev2.lesson_id).status == "active"
    assert repo.get(rev1.lesson_id).status == "superseded"
    assert repo.get(rev1.lesson_id).superseded_by == rev2.lesson_id
    assert repo.active_for_subject("dup.restore", rev2.scope) == repo.get(rev2.lesson_id)


# ---------------------------------------------------------------------------
# 7. 大规模有界切片、schema 校验与 Rule #6 缺失自愈
# ---------------------------------------------------------------------------


def test_maintain_bounded_slices_scale_and_rule6_self_healing(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    # 创建 260 条活跃 lesson + 120 条已归档文件，按小切片推进，直到 backlog=False。
    for i in range(260):
        rec, _ = _make_record(i, updated_at=f"2026-10-01T00:{i // 60:02d}:{i % 60:02d}+00:00")
        _seed_raw_lesson(repo, rec)
    bucket = repo.lessons_dir / "archive" / "2026-08"
    bucket.mkdir(parents=True, exist_ok=True)
    for j in range(120):
        rec, fname = _make_record(1000 + j)
        (bucket / fname).write_text(_render_lesson(rec), encoding="utf-8")

    now_dt = datetime(2026, 10, 10, 0, 0, tzinfo=timezone.utc)
    slices: list[dict[str, object]] = []
    for _ in range(30):
        step = repo.maintain(now=now_dt, archive_days=15, batch_count=60, batch_bytes=16_384)
        slices.append(step)
        assert step["scanned"] <= 60
        if not step["backlog"]:
            break
    assert slices[-1]["backlog"] is False
    assert slices[0]["backlog"] is True
    assert slices[-1]["coverage"]["status"] == "complete"
    assert slices[-1]["lessons_active_count"] == 260
    assert slices[-1]["lessons_archived_count"] == 120

    heat = json.loads(repo.heat_path.read_text(encoding="utf-8"))
    _assert_valid_heat_schema(heat)

    # 极端字节预算（1 字节）也必须每次至少推进 1 条，绝不卡死零前进。
    tiny_slice = repo.maintain(now=now_dt, archive_days=0, batch_bytes=1, batch_count=100)
    assert tiny_slice["scanned"] >= 1

    # AGENTS.md 第 6 条：任意删除 lesson_heat.json、INDEX.md 或单条 lesson 文件均平滑自愈不报错（默认切片内分步完成全量自愈）。
    first_file = next(repo.lessons_dir.glob("*.md"))
    first_file.unlink()
    repo.heat_path.unlink()
    repo.routing_index_path.unlink()
    healed: dict[str, object] = {}
    for _ in range(10):
        healed = repo.maintain(now=now_dt, archive_days=0)
        if not healed["backlog"]:
            break
    assert healed["backlog"] is False
    assert healed["coverage"]["status"] == "complete"
    assert healed["lessons_active_count"] == 259
    assert healed["lessons_archived_count"] == 120
    assert repo.heat_path.exists() and repo.routing_index_path.exists()


def test_maintain_invalidates_stale_archive_count_when_archive_move_stops_mid_slice(
    tmp_path: Path,
) -> None:
    repo, _ = _repo(tmp_path)
    t0 = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
    bucket = repo.lessons_dir / "archive" / "2026-08"
    bucket.mkdir(parents=True, exist_ok=True)
    for j in range(3):
        rec, fname = _make_record(2000 + j)
        (bucket / fname).write_text(_render_lesson(rec), encoding="utf-8")

    # 在 lessons/ 中放置 6 条已替代待归档教训，并先记录完整归档计数 3。
    for i in range(6):
        rec, _ = _make_record(
            i,
            status="superseded",
            superseded_by="lesson-successor",
            superseded_at=t0.isoformat(timespec="seconds"),
        )
        _seed_raw_lesson(repo, rec)

    initial = repo.maintain(now=t0, archive_days=15)
    assert initial["lessons_archived_count"] == 3
    assert initial["coverage"]["status"] == "complete"

    # 分片执行 archive_move（6 条 active 扫描消耗 6 次后余 2 次预算），在搬走 2 条后耗尽并作废旧归档计数。
    mid = repo.maintain(now=t0 + timedelta(days=20), archive_days=15, batch_count=8)
    assert mid["cursor"]["phase"] == "archive_move"
    assert mid["archived"] == 2
    assert mid["backlog"] is True
    assert mid["lessons_archived_count"] is None
    heat_mid = json.loads(repo.heat_path.read_text(encoding="utf-8"))
    assert heat_mid["lessons_archived_count"] is None
    assert heat_mid["archive_count_coverage"]["status"] == "partial"
    assert heat_mid["archive_count_coverage"]["reason"] == "archive_moved_pending_recount"

    # 验证 batch_bytes=1 在 archive_move 阶段也会真实计字节并仅推进 1 条。
    byte_slice = repo.maintain(
        now=t0 + timedelta(days=20),
        archive_days=15,
        batch_count=100,
        batch_bytes=1,
    )
    assert byte_slice["archived"] == 1
    assert byte_slice["backlog"] is True
    assert byte_slice["lessons_archived_count"] is None

    final: dict[str, object] = {}
    for _ in range(10):
        final = repo.maintain(now=t0 + timedelta(days=20), archive_days=15, batch_count=2)
        if not final["backlog"]:
            break
    assert final["backlog"] is False
    assert final["coverage"]["status"] == "complete"
    assert final["lessons_archived_count"] == 9


def test_maintain_preserves_note_hits_for_unscanned_tail_lessons_during_sliced_active_scan(
    tmp_path: Path,
) -> None:
    repo, _ = _repo(tmp_path)
    records = []
    for i in range(10):
        rec, _ = _make_record(i, updated_at=f"2026-10-01T00:00:{i:02d}+00:00")
        _seed_raw_lesson(repo, rec)
        records.append(rec)

    tail_rec = records[-1]
    hit_dt = datetime(2026, 10, 5, 8, 30, tzinfo=timezone.utc)
    repo.note_hits([tail_rec.lesson_id], at=hit_dt)

    # 第一片仅扫描前 4 条即停止在 phase='active'，尾部命中的信号必须回填并保留到扫全之后。
    t0 = datetime(2026, 10, 6, 0, 0, tzinfo=timezone.utc)
    first = repo.maintain(now=t0, archive_days=0, batch_count=4)
    assert first["cursor"]["phase"] == "active"
    assert first["backlog"] is True

    last: dict[str, object] = {}
    for _ in range(10):
        last = repo.maintain(now=t0, archive_days=0, batch_count=4)
        if not last["backlog"]:
            break
    assert last["backlog"] is False
    assert last["coverage"]["status"] == "complete"
    heat = json.loads(repo.heat_path.read_text(encoding="utf-8"))
    assert heat["rows"][tail_rec.lesson_id]["hits"] == 1
    assert heat["rows"][tail_rec.lesson_id]["last_hit_at"] == pytest.approx(hit_dt.timestamp())


def test_supersede_wraps_hot_projection_failure_in_lesson_routing_pending_error(
    tmp_path: Path, monkeypatch
) -> None:
    repo, hot = _repo(tmp_path)
    old = repo.promote(_cand("hotfail-old", subject_key="hotfail.subj", content="旧正文"))
    hot.promote_with_receipt(
        _hot_cand("hotfail-rule", target_lesson_id=old.lesson_id, subject_key="hotfail.subj", rule="待退役 HOT 规则"),
        lesson=old,
    )
    cand_new = _cand("hotfail-new", subject_key="hotfail.subj", content="新正文")
    expected_sid = (
        "lesson-"
        + hashlib.sha256(
            f"{cand_new.candidate_id}\0supersede\0{old.lesson_id}\0{old.revision}".encode()
        ).hexdigest()[:20]
    )

    real_write = lesson_module.write_private_text_file_atomic_unlocked

    def fail_hot(path, *args, **kwargs):
        if Path(path) == hot.path:
            raise OSError("simulated hot file write failure")
        return real_write(path, *args, **kwargs)

    monkeypatch.setattr(lesson_module, "write_private_text_file_atomic_unlocked", fail_hot)
    with pytest.raises(LessonRoutingPendingError) as caught:
        repo.supersede_with_receipt(
            cand_new,
            target_lesson_id=old.lesson_id,
            hot=hot,
            expected_revision=old.revision,
        )
    assert caught.value.formal_memory_changed is True
    assert caught.value.promotion_ref.endswith("#" + expected_sid)
    assert isinstance(caught.value.__cause__, OSError)
    assert repo.get(old.lesson_id).status == "superseded"
    assert repo.get(expected_sid).status == "active"

    # 恢复写能力后重放幂等完成 HOT 退役与 heat/routing 对账。
    monkeypatch.undo()
    replayed, changed = repo.supersede_with_receipt(
        cand_new,
        target_lesson_id=old.lesson_id,
        hot=hot,
        expected_revision=old.revision,
    )
    assert changed is False and replayed.lesson_id == expected_sid
    assert hot.list() == []
    assert repo.active_for_subject("hotfail.subj", old.scope) == replayed


# === Consolidated from: agent_py_agent/tests/test_sm_w7_recall_bounds.py ===
# W7 与 V2-5 行为验收测试：有界 lesson/HOT 读取、generation 级 scope_pairs LRU 投影、
# 非 touch read_ids 刷新、缺损自愈与按最终入选去重记命中。
#
# 全部基于 tmp_path 与隔离环境运行，不修改真机 ~/.my-agent。

def _repos(root: Path) -> tuple[LessonRepository, HotRuleRepository, JsonlMemory]:
    memory_dir = root / "memory"
    return (
        LessonRepository(
            memory_dir / "lessons",
            memory_dir / "routing" / "INDEX.md",
        ),
        HotRuleRepository(memory_dir / "memory-hot.md"),
        JsonlMemory(memory_dir / "long_term.jsonl"),
    )


def _lesson_candidate(
    cid: str,
    *,
    subject_key: str,
    content: str,
    scope_type: str = "project",
    scope_key: str = "project:alpha",
    occurrence_count: int = 3,
) -> MemoryCandidate:
    now_iso = datetime.now(timezone.utc).isoformat()
    return MemoryCandidate(
        schema_version="my-agent.candidate.v1",
        candidate_id=cid,
        candidate_type="lesson",
        content=content,
        subject_key=subject_key,
        scope={"scope_type": scope_type, "scope_key": scope_key},
        origin="reviewed",
        evidence_refs=[{"kind": "task", "id": f"t-{cid}-1"}, {"kind": "run", "id": f"r-{cid}-1"}],
        source_message_refs=[],
        source_tool_refs=[],
        source_artifact_refs=[],
        source_task_ids=[f"task-{cid}-1", f"task-{cid}-2"],
        source_run_ids=[f"run-{cid}-1", f"run-{cid}-2"],
        observed_at=now_iso,
        last_observed_at=now_iso,
        valid_from=now_iso,
        valid_until="",
        occurrence_count=occurrence_count,
        confidence=0.95,
        proposed_action="add",
        target_entry_id="",
        conflicts_with=[],
        promotion_target="lesson",
        status="approved",
        reviewer="admin",
        review_note="",
        created_at=now_iso,
        updated_at=now_iso,
        promoted_at="",
        promotion_ref="",
    )


def _hot_candidate(
    cid: str,
    *,
    target_lesson_id: str,
    subject_key: str,
    rule: str,
    occurrence_count: int = 4,
) -> MemoryCandidate:
    now_iso = datetime.now(timezone.utc).isoformat()
    return MemoryCandidate(
        schema_version="my-agent.candidate.v1",
        candidate_id=cid,
        candidate_type="hot_rule",
        content=rule,
        subject_key=subject_key,
        scope={"scope_type": "project", "scope_key": "project:alpha"},
        origin="reviewed",
        evidence_refs=[{"kind": "task", "id": f"t-{cid}-1"}, {"kind": "run", "id": f"r-{cid}-1"}],
        source_message_refs=[],
        source_tool_refs=[],
        source_artifact_refs=[],
        source_task_ids=[f"task-{cid}-1", f"task-{cid}-2"],
        source_run_ids=[f"run-{cid}-1", f"run-{cid}-2"],
        observed_at=now_iso,
        last_observed_at=now_iso,
        valid_from=now_iso,
        valid_until="",
        occurrence_count=occurrence_count,
        confidence=0.95,
        proposed_action="add",
        target_entry_id=target_lesson_id,
        conflicts_with=[],
        promotion_target="hot",
        status="approved",
        reviewer="admin",
        review_note="",
        created_at=now_iso,
        updated_at=now_iso,
        promoted_at="",
        promotion_ref="",
    )


def _write_canonical_lesson_file(
    lessons_dir: Path,
    *,
    index: int,
    status: str = "active",
    scope_type: str = "project",
    scope_key: str = "project:alpha",
    subject_key: str | None = None,
    lesson_id: str | None = None,
    content: str | None = None,
) -> LessonRecord:
    lessons_dir.mkdir(parents=True, exist_ok=True)
    hex8 = f"{index:08x}"
    filename = f"topic-{index:04d}-{hex8}.md"
    rel_path = f"memory/lessons/{filename}"
    now_iso = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    record = LessonRecord(
        schema_version=LESSON_SCHEMA_VERSION,
        lesson_id=lesson_id or f"lesson-{index:04d}",
        candidate_id=f"cand-{index:04d}",
        subject_key=subject_key or f"subj.{index:04d}",
        scope={"scope_type": scope_type, "scope_key": scope_key},
        content=content or f"教训内容 {index}",
        occurrence_count=2,
        evidence_groups=("task:a", "task:b"),
        source_task_ids=("a", "b"),
        source_run_ids=("r1", "r2"),
        created_at=now_iso,
        updated_at=now_iso,
        path=rel_path,
        status=status,
    )
    (lessons_dir / filename).write_text(_render_lesson(record), encoding="utf-8")
    return record


def test_hot_and_routed_recall_read_only_selected_canonical_refs_across_500_live_and_archived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lessons, hot, _memory = _repos(tmp_path)
    (tmp_path / "memory" / "migration.json").write_text('{"version": 2}\n', encoding="utf-8")

    # 500 条 live 正式 lesson + 60 条永久归档 lesson
    live: list[LessonRecord] = []
    for i in range(500):
        live.append(_write_canonical_lesson_file(lessons.lessons_dir, index=i))
    archive_dir = lessons.lessons_dir / "archive" / "2026-01"
    for i in range(500, 560):
        _write_canonical_lesson_file(archive_dir, index=i, status="archived")

    # 写 2 条共享同一 lesson 的 HOT 规则与 1 条引用另一 lesson 的 HOT 规则
    target_a = live[10]
    target_b = live[200]
    now_dt = datetime.now(timezone.utc)
    hot_rules = [
        HotRuleRecord(
            schema_version=HOT_SCHEMA_VERSION,
            hot_id="hot-1",
            candidate_id="cand-h1",
            lesson_id=target_a.lesson_id,
            lesson_ref=f"{target_a.path}#{target_a.lesson_id}",
            rule="先看边界。",
            occurrence_count=5,
            evidence_groups=("task:a", "task:b"),
            promoted_at=(now_dt + timedelta(seconds=3)).isoformat(),
        ),
        HotRuleRecord(
            schema_version=HOT_SCHEMA_VERSION,
            hot_id="hot-2",
            candidate_id="cand-h2",
            lesson_id=target_a.lesson_id,
            lesson_ref=f"{target_a.path}#{target_a.lesson_id}",
            rule="同源复核。",
            occurrence_count=4,
            evidence_groups=("task:a", "task:b"),
            promoted_at=(now_dt + timedelta(seconds=2)).isoformat(),
        ),
        HotRuleRecord(
            schema_version=HOT_SCHEMA_VERSION,
            hot_id="hot-3",
            candidate_id="cand-h3",
            lesson_id=target_b.lesson_id,
            lesson_ref=f"{target_b.path}#{target_b.lesson_id}",
            rule="先跑回归。",
            occurrence_count=3,
            evidence_groups=("task:a", "task:b"),
            promoted_at=(now_dt + timedelta(seconds=1)).isoformat(),
        ),
    ]
    hot.path.write_text(_render_hot(hot_rules), encoding="utf-8")

    read_calls: list[list[str]] = []
    orig_read_refs = LessonRepository.read_refs

    def spy_read_refs(self, refs):
        batch = list(refs)
        read_calls.append(batch)
        return orig_read_refs(self, batch)

    monkeypatch.setattr(
        LessonRepository,
        "list",
        lambda self: (_ for _ in ()).throw(AssertionError("full lesson list() must not be called")),
    )
    monkeypatch.setattr(LessonRepository, "read_refs", spy_read_refs)

    scope = MemoryRecallScope.from_runtime(task_id="alpha")

    # 1. 空 selection 零磁盘/read_refs 读取
    assert routed_lesson_records(lessons, read_paths=[], scope=scope) == []
    assert read_calls == []

    # 2. routed 只读取去重后的规范路径，保持 (subject_key, lesson_id) 确定顺序
    routed = routed_lesson_records(
        lessons,
        read_paths=[live[350].path, live[10].path, live[350].path],
        scope=scope,
    )
    assert [row.entry_id for row in routed] == [live[10].lesson_id, live[350].lesson_id]
    assert read_calls == [[live[350].path, live[10].path]]

    # 3. HOT 只读取预算后去重的规范 ref（2 个去重 ref，而不是 500 个）
    read_calls.clear()
    hot_records = hot_memory_records(hot, lessons, scope=scope)
    assert [row.entry_id for row in hot_records] == ["hot-1", "hot-2", "hot-3"]
    assert read_calls == [[hot_rules[0].lesson_ref, hot_rules[2].lesson_ref]]


def test_hot_and_routed_reject_superseded_mismatched_id_wrong_scope_and_noncanonical_refs(
    tmp_path: Path,
) -> None:
    lessons, hot, _memory = _repos(tmp_path)
    valid = lessons.promote(
        _lesson_candidate("c-valid", subject_key="alpha.ok", content="有效教训"),
    )
    old = lessons.promote(
        _lesson_candidate("c-old", subject_key="alpha.superseded", content="旧教训"),
    )
    successor, _ = lessons.supersede_with_receipt(
        _lesson_candidate("c-new", subject_key="alpha.superseded", content="新教训"),
        target_lesson_id=old.lesson_id,
        hot=hot,
        expected_revision=old.revision,
    )
    wrong_scope = lessons.promote(
        _lesson_candidate(
            "c-scope",
            subject_key="company.only",
            content="仅限公司作用域",
            scope_type="company",
            scope_key="company:acme",
        ),
    )
    mismatch_target = _write_canonical_lesson_file(
        lessons.lessons_dir,
        index=900,
        lesson_id="actual-lesson-900",
        subject_key="alpha.mismatch",
    )

    now_iso = datetime.now(timezone.utc).isoformat()
    hot.path.write_text(
        _render_hot(
            [
                HotRuleRecord(
                    schema_version=HOT_SCHEMA_VERSION,
                    hot_id="hot-ok",
                    candidate_id="ch-1",
                    lesson_id=valid.lesson_id,
                    lesson_ref=f"{valid.path}#{valid.lesson_id}",
                    rule="有效短规则。",
                    occurrence_count=5,
                    evidence_groups=("t1", "t2"),
                    promoted_at=now_iso,
                ),
                HotRuleRecord(
                    schema_version=HOT_SCHEMA_VERSION,
                    hot_id="hot-superseded",
                    candidate_id="ch-2",
                    lesson_id=old.lesson_id,
                    lesson_ref=f"{old.path}#{old.lesson_id}",
                    rule="已替代规则不应进入。",
                    occurrence_count=4,
                    evidence_groups=("t1", "t2"),
                    promoted_at=now_iso,
                ),
                HotRuleRecord(
                    schema_version=HOT_SCHEMA_VERSION,
                    hot_id="hot-wrong-id-frag",
                    candidate_id="ch-3",
                    lesson_id="different-id",
                    lesson_ref=f"{mismatch_target.path}#{mismatch_target.lesson_id}",
                    rule="fragment 与 rule.lesson_id 不一致。",
                    occurrence_count=4,
                    evidence_groups=("t1", "t2"),
                    promoted_at=now_iso,
                ),
                HotRuleRecord(
                    schema_version=HOT_SCHEMA_VERSION,
                    hot_id="hot-wrong-id-no-frag",
                    candidate_id="ch-4",
                    lesson_id="different-id-2",
                    lesson_ref=mismatch_target.path,
                    rule="lesson_id 与文件头不一致。",
                    occurrence_count=4,
                    evidence_groups=("t1", "t2"),
                    promoted_at=now_iso,
                ),
                HotRuleRecord(
                    schema_version=HOT_SCHEMA_VERSION,
                    hot_id="hot-wrong-scope",
                    candidate_id="ch-5",
                    lesson_id=wrong_scope.lesson_id,
                    lesson_ref=f"{wrong_scope.path}#{wrong_scope.lesson_id}",
                    rule="跨 scope 不应进入。",
                    occurrence_count=4,
                    evidence_groups=("t1", "t2"),
                    promoted_at=now_iso,
                ),
                HotRuleRecord(
                    schema_version=HOT_SCHEMA_VERSION,
                    hot_id="hot-noncanonical",
                    candidate_id="ch-6",
                    lesson_id=valid.lesson_id,
                    lesson_ref="../escape-path.md",
                    rule="非规范引用不得进入。",
                    occurrence_count=4,
                    evidence_groups=("t1", "t2"),
                    promoted_at=now_iso,
                ),
            ]
        ),
        encoding="utf-8",
    )

    scope = MemoryRecallScope.from_runtime(task_id="alpha")
    hot_out = hot_memory_records(hot, lessons, scope=scope)
    assert [row.entry_id for row in hot_out] == ["hot-ok"]

    routed_out = routed_lesson_records(
        lessons,
        read_paths=[
            old.path,
            successor.path,
            wrong_scope.path,
            "../noncanonical.md",
            "memory/lessons/draft.md",
        ],
        scope=scope,
    )
    assert [row.entry_id for row in routed_out] == [successor.lesson_id]


def test_read_ids_non_touch_order_and_refresh_avoids_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lessons, hot, memory = _repos(tmp_path)
    now = time.time()
    r1 = memory.add(
        role="user",
        content="项目事实一",
        kind="fact",
        attributes={"origin": "user_explicit", "scope_type": "project", "scope_key": "project:alpha"},
    )
    r2 = memory.add(
        role="user",
        content="个人事实二",
        kind="fact",
        attributes={"origin": "user_explicit", "scope_type": "personal", "scope_key": "personal"},
    )
    r_other = memory.add(
        role="user",
        content="其他项目事实",
        kind="fact",
        attributes={"origin": "user_explicit", "scope_type": "project", "scope_key": "project:beta"},
    )
    r_removed = memory.add(
        role="user",
        content="将被删除",
        kind="fact",
        attributes={"origin": "user_explicit", "scope_type": "personal", "scope_key": "personal"},
    )
    r_expired = memory.add_record(
        MemoryRecord(
            role="user",
            content="已过期事实",
            kind="fact",
            attributes={"origin": "user_explicit", "scope_type": "personal", "scope_key": "personal"},
            expires_at=now - 10.0,
        )
    )
    memory.remove(r_removed.entry_id)
    memory.migrate_tiers()
    memory.rebuild_read_index()

    assert memory.read_ids([]) == []
    got = memory.read_ids([r2.entry_id, r1.entry_id, r1.entry_id, r_removed.entry_id, r_expired.entry_id, "missing"])
    assert [row.entry_id for row in got] == [r2.entry_id, r1.entry_id]
    assert all(row.last_accessed_at == 0.0 for row in got)
    assert memory._pending_access == {}

    # 禁止调用 all()，用 _refresh_recall_candidates 分别刷新 1 个和 3 个 ID
    monkeypatch.setattr(
        JsonlMemory,
        "all",
        lambda self: (_ for _ in ()).throw(AssertionError("memory.all() must not be called during refresh")),
    )
    scope = MemoryRecallScope.from_runtime(task_id="alpha")
    hot_lesson = lessons.promote(
        _lesson_candidate("c-hl", subject_key="alpha.hl", content="热教训本体"),
    )
    hot.promote(
        _hot_candidate("ch-hl", target_lesson_id=hot_lesson.lesson_id, subject_key="alpha.hl", rule="热短规"),
        lesson=hot_lesson,
    )
    # 同时往 JSONL 写入一个与 hot 同 ID 的普通记录以测试退役回落
    same_id_jsonl = memory.add_record(
        MemoryRecord(
            role="user",
            content="旧 JSONL 同 ID",
            kind="fact",
            entry_id="hot-shadow",
            attributes={"origin": "user_explicit", "scope_type": "personal", "scope_key": "personal"},
        )
    )
    memory.migrate_tiers()
    memory.rebuild_read_index()

    agent = SimpleNamespace(
        memory=memory,
        memory_hot=hot,
        memory_lessons=lessons,
        config=SimpleNamespace(home_lesson_stale_caveat_days=7.0),
    )

    refreshed_1 = _refresh_recall_candidates(agent, [r1], [], scope)
    assert [row.entry_id for row in refreshed_1] == [r1.entry_id]
    assert memory._pending_access == {}

    fixed_ids = {"hot-shadow"}
    refreshed_3 = _refresh_recall_candidates(
        agent,
        [same_id_jsonl, r2, r_other, r1],
        [],
        scope,
        fixed_source_ids=fixed_ids,
        budget=ContextBudget(12000, 0),
    )
    # r_other 不在 alpha scope 内；hot-shadow 已不在 HOT 中，应从 fixed_ids 移出以便最终确认
    assert [row.entry_id for row in refreshed_3] == [same_id_jsonl.entry_id, r2.entry_id, r1.entry_id]
    assert "hot-shadow" not in fixed_ids
    assert memory._pending_access == {}

    # 更新版本并删除一条，再刷新应同步反映最新正文并移除已删记录
    r2_v2 = memory.replace(r2.entry_id, "个人事实二-新版")
    memory.remove(r1.entry_id)
    memory.migrate_tiers()
    memory.rebuild_read_index()
    refreshed_after = _refresh_recall_candidates(
        agent,
        [r2, r1],
        [],
        scope,
        budget=ContextBudget(12000, 0),
    )
    assert [(row.entry_id, row.content, row.version) for row in refreshed_after] == [
        (r2.entry_id, "个人事实二-新版", r2_v2.version),
    ]


def test_read_ids_stale_index_unreadable_segment_and_incomplete_bounded_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _lessons, _hot, memory = _repos(tmp_path)
    r1 = memory.add(
        role="user",
        content="正式记忆",
        kind="fact",
        attributes={"origin": "user_explicit", "scope_type": "personal", "scope_key": "personal"},
    )
    memory.migrate_tiers()
    memory.rebuild_read_index()

    # 1. 有界扫描覆盖不全（index 不可用且 partial=True）必须显式区别于证明为空 []
    idx_path = memory.path.parent / "long_term.jsonl.read_index.sqlite3"
    idx_path.unlink()
    monkeypatch.setattr(
        indexing_module,
        "bounded_authority_records",
        lambda *_args, **_kwargs: ([], {"partial": True, "scanned_records": 1, "scanned_bytes": 100}),
    )
    with pytest.raises(ValueError, match="MEMORY_AUTHORITY_INCOMPLETE"):
        memory.read_ids([r1.entry_id])

    # 2. 索引失效 + 单个分片不可读，不得静默复活旧文件已删数据或假装空库
    monkeypatch.undo()
    seg_dir = memory.path.parent / "long_term.jsonl.generations"
    for seg in seg_dir.rglob("*.jsonl"):
        seg.unlink()
    with pytest.raises(ValueError, match="MEMORY_GENERATION_UNREADABLE"):
        memory.read_ids([r1.entry_id])


def test_scope_pairs_warm_hit_zero_segment_stat_no_deepcopy_no_all_and_invalidation_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _lessons, _hot, memory = _repos(tmp_path)
    t0 = time.time()
    fake_clock = [t0]
    monkeypatch.setattr(time, "time", lambda: fake_clock[0])

    old_ts = t0 - 365 * 86400
    memory.add_record(
        MemoryRecord(
            role="user",
            content="冷数据项目",
            kind="fact",
            created_at=old_ts,
            updated_at=old_ts,
            last_accessed_at=old_ts,
            attributes={"scope_type": "project", "scope_key": "project:cold"},
        )
    )
    memory.add_record(
        MemoryRecord(
            role="user",
            content="临期项目",
            kind="fact",
            expires_at=t0 + 120.0,
            attributes={"scope_type": "project", "scope_key": "project:expiring"},
        )
    )
    memory.add(
        role="user",
        content="其他领域",
        kind="fact",
        attributes={"scope_type": "company", "scope_key": "company:acme"},
    )
    memory.migrate_tiers()
    memory.rebuild_read_index()

    # 冷启动重建
    cold_pairs = memory.scope_pairs("project")
    assert cold_pairs == (("project", "project:cold"), ("project", "project:expiring"))

    # 热命中禁止 deepcopy、禁止 all()、禁止 stat .generations 下的分片
    stat_calls: list[str] = []
    orig_stat = Path.stat

    def spy_stat(self, *args, **kwargs):
        stat_calls.append(str(self))
        return orig_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", spy_stat)
    monkeypatch.setattr(
        jsonl_module,
        "deepcopy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("deepcopy forbidden on warm scope_pairs")),
    )
    monkeypatch.setattr(
        copy,
        "deepcopy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("deepcopy forbidden on warm scope_pairs")),
    )
    monkeypatch.setattr(
        JsonlMemory,
        "all",
        lambda self: (_ for _ in ()).throw(AssertionError("all() forbidden on scope_pairs")),
    )

    warm_pairs = memory.scope_pairs("project")
    assert warm_pairs is cold_pairs
    assert not any("long_term.jsonl.generations" in p for p in stat_calls)

    # 写入新记录立即失效并包含新作用域
    monkeypatch.undo()
    monkeypatch.setattr(time, "time", lambda: fake_clock[0])
    added = memory.add(
        role="user",
        content="新写入项目",
        kind="fact",
        attributes={"scope_type": "project", "scope_key": "project:fresh"},
    )
    assert memory.scope_pairs("project") == (
        ("project", "project:cold"),
        ("project", "project:expiring"),
        ("project", "project:fresh"),
    )

    # 删除记录立即失效
    memory.remove(added.entry_id)
    assert memory.scope_pairs("project") == (("project", "project:cold"), ("project", "project:expiring"))

    # 注入时钟越过最近 expires_at（无需 sleep）自动失效重算
    fake_clock[0] = t0 + 121.0
    assert memory.scope_pairs("project") == (("project", "project:cold"),)

    # 写入变更使缓存失效后，部分扫描重建失败不得写入空元组缓存；恢复后应立即算回真实范围
    added_2 = memory.add(
        role="user",
        content="变更触发重算",
        kind="fact",
        attributes={"scope_type": "project", "scope_key": "project:after_partial"},
    )
    assert added_2.entry_id
    monkeypatch.setattr(
        indexing_module,
        "bounded_authority_records",
        lambda *_args, **_kwargs: ([], {"partial": True, "scanned_records": 1, "scanned_bytes": 100}),
    )
    with pytest.raises(ValueError, match="MEMORY_SCOPE_PROJECTION_UNAVAILABLE"):
        memory.scope_pairs("project")
    assert "project" not in memory._scope_pairs_cache
    # runtime_long_term_scope 在不可用时降级保留基础 runtime scope，不崩召回
    degraded_scope = runtime_long_term_scope(memory, task_id="base_task")
    assert ("project", "project:base_task") in degraded_scope.keys

    monkeypatch.undo()
    monkeypatch.setattr(time, "time", lambda: fake_clock[0])
    assert memory.scope_pairs("project") == (("project", "project:after_partial"), ("project", "project:cold"))


def test_scope_pairs_cache_strictly_bounded_ordered_dict_lru(tmp_path: Path) -> None:
    _lessons, _hot, memory = _repos(tmp_path)
    total = _SCOPE_PAIRS_CACHE_MAX_ENTRIES + 12
    for i in range(total):
        assert memory.scope_pairs(f"scope_type_{i}") == ()

    assert isinstance(memory._scope_pairs_cache, OrderedDict)
    assert len(memory._scope_pairs_cache) == _SCOPE_PAIRS_CACHE_MAX_ENTRIES
    assert "scope_type_0" not in memory._scope_pairs_cache
    assert f"scope_type_{total - 1}" in memory._scope_pairs_cache


def test_manual_deletion_self_healing_v2_5(tmp_path: Path) -> None:
    lessons, hot, memory = _repos(tmp_path)
    lesson = lessons.promote(
        _lesson_candidate("c-del", subject_key="self.heal", content="删除自愈验证"),
    )
    hot.promote(
        _hot_candidate("ch-del", target_lesson_id=lesson.lesson_id, subject_key="self.heal", rule="自愈规则"),
        lesson=lesson,
    )
    r1 = memory.add(
        role="user",
        content="需要持久化的项目记忆",
        kind="fact",
        attributes={"origin": "user_explicit", "scope_type": "project", "scope_key": "project:alpha"},
    )
    memory.migrate_tiers()
    memory.rebuild_read_index()

    scope = MemoryRecallScope.from_runtime(task_id="alpha")
    assert memory.scope_pairs("project") == (("project", "project:alpha"),)
    assert len(memory.read_ids([r1.entry_id])) == 1
    assert len(hot_memory_records(hot, lessons, scope=scope)) == 1
    assert len(routed_lesson_records(lessons, read_paths=[lesson.path], scope=scope)) == 1

    # 1. 手动删墓碑文件：runtime_long_term_scope 温和降级，缓存失效
    for tb in memory.path.parent.glob("*storage_tombstones*"):
        tb.unlink()
    degraded = runtime_long_term_scope(memory, task_id="alpha")
    assert ("project", "project:alpha") in degraded.keys

    # 2. 手动删整块 memory/ 目录：scope_pairs / read_ids / hot / routed 全部自愈返回空集不抛致命异常
    shutil.rmtree(tmp_path / "memory")
    assert memory.scope_pairs("project") == ()
    assert memory.read_ids([r1.entry_id]) == []
    assert hot_memory_records(hot, lessons, scope=scope) == []
    assert routed_lesson_records(lessons, read_paths=[lesson.path], scope=scope) == []

    # 3. 手动删整个 owner 目录：同样干净自愈
    shutil.rmtree(tmp_path)
    assert memory.scope_pairs("project") == ()
    assert memory.read_ids([r1.entry_id]) == []
    assert hot_memory_records(hot, lessons, scope=scope) == []
    assert routed_lesson_records(lessons, read_paths=[lesson.path], scope=scope) == []


def test_final_hit_counting_once_per_turn_and_zero_for_excluded_or_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lessons, hot, memory = _repos(tmp_path)
    l1 = lessons.promote(
        _lesson_candidate("c-hit-1", subject_key="hit.one", content="命中的教训一"),
    )
    l2 = lessons.promote(
        _lesson_candidate("c-hit-2", subject_key="hit.two", content="命中的教训二"),
    )
    l_rejected_budget = lessons.promote(
        _lesson_candidate("c-hit-budget", subject_key="hit.zzz", content="超预算教训" * 4000),
    )
    l_wrong_scope = lessons.promote(
        _lesson_candidate(
            "c-hit-scope",
            subject_key="hit.other",
            content="非本作用域教训",
            scope_type="company",
            scope_key="company:other",
        ),
    )
    h1 = hot.promote(
        _hot_candidate("ch-hit-1", target_lesson_id=l1.lesson_id, subject_key="hit.one", rule="短规则一"),
        lesson=l1,
    )

    hit_calls: list[list[str]] = []
    orig_note_hits = LessonRepository.note_hits

    def spy_note_hits(self, lesson_ids, *, at=None):
        batch = list(lesson_ids)
        hit_calls.append(batch)
        return orig_note_hits(self, batch, at=at)

    monkeypatch.setattr(LessonRepository, "note_hits", spy_note_hits)

    agent = SimpleNamespace(
        memory=memory,
        memory_hot=hot,
        memory_lessons=lessons,
        config=SimpleNamespace(home_lesson_stale_caveat_days=7.0),
    )
    scope = MemoryRecallScope.from_runtime(task_id="alpha")
    hot_recs = hot_memory_records(hot, lessons, scope=scope)
    routed_recs = routed_lesson_records(
        lessons,
        read_paths=[l1.path, l2.path, l_rejected_budget.path, l_wrong_scope.path],
        scope=scope,
    )
    assert [row.entry_id for row in hot_recs] == [h1.hot_id]
    assert [row.entry_id for row in routed_recs] == [l1.lesson_id, l2.lesson_id, l_rejected_budget.lesson_id]

    heat_path = tmp_path / "memory" / "routing" / "lesson_heat.json"
    heat_before_text = heat_path.read_text(encoding="utf-8")
    heat_before_mtime_ns = heat_path.stat().st_mtime_ns

    index_path = str(tmp_path / "memory" / "routing" / "INDEX.md")
    # 1. 仅选中 h1、l1、l2，跳过超大 l_rejected_budget；同源 HOT+routed l1 只计 1 次
    final_budgeted = _formal_memories_for_request(
        agent,
        RuntimeContextRequest("检查边界", None, None),
        RoutedMemoryContext(
            enabled=True,
            index_path=index_path,
            receipts=[
                {"path": l1.path, "status": "read"},
                {"path": l2.path, "status": "read"},
                {"path": l_rejected_budget.path, "status": "read"},
                {"path": l_wrong_scope.path, "status": "read"},
            ],
            findings=[],
        ),
        recall_scope=scope,
        long_term_memories=[],
        skip_formal_recall=False,
    )
    selected_ids = {row.entry_id for row in final_budgeted}
    # 确认总候选不跨预算后触发唯一一次 note_hits
    assert l1.lesson_id in selected_ids and l2.lesson_id in selected_ids
    assert l_rejected_budget.lesson_id not in selected_ids
    assert len(hit_calls) == 1
    assert hit_calls[0] == [l1.lesson_id, l2.lesson_id]
    assert set(lessons._pending_hits_map()) == {l1.lesson_id, l2.lesson_id}
    assert all(count == 1 for count, _epoch in lessons._pending_hits_map().values())
    assert heat_path.read_text(encoding="utf-8") == heat_before_text
    assert heat_path.stat().st_mtime_ns == heat_before_mtime_ns

    # 2. 验证超大预算截断被拒绝的 lesson 不计入命中（直接用明确 Budget 测试）
    hit_calls.clear()
    _confirm_final_memory_access(
        agent,
        BudgetedMemoryRecords(hot_recs[:1]),  # 仅 h1
        scope,
        {h1.hot_id},
        RoutedMemoryContext(enabled=True, index_path=index_path, receipts=[], findings=[]),
    )
    assert hit_calls == [[l1.lesson_id]]
    assert l_rejected_budget.lesson_id not in lessons._pending_hits_map()
    assert l_wrong_scope.lesson_id not in lessons._pending_hits_map()

    # 3. 隔离上下文（task_local / control_plane）不得触发 note_hits
    hit_calls.clear()
    for isolated_scope in ("task_local", "control_plane"):
        _formal_memories_for_request(
            agent,
            RuntimeContextRequest("隔离执行", None, None, context_scope=isolated_scope),
            RoutedMemoryContext(
                enabled=True,
                index_path=index_path,
                receipts=[{"path": l1.path, "status": "read"}],
                findings=[],
            ),
            recall_scope=scope,
            long_term_memories=[],
            skip_formal_recall=True,
        )
    assert hit_calls == []
    # 纯 _prepare_runtime_context 在 task_local 下也不会计命中
    real_agent = SimpleAgent(
        AgentConfig(enable_tools=False),
        tmp_path,
    )
    real_agent.memory = memory
    real_agent.memory_hot = hot
    real_agent.memory_lessons = lessons
    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.runtime.loop_support.build_routed_memory_context",
        lambda *_args, **_kwargs: RoutedMemoryContext(
            enabled=True,
            index_path=index_path,
            receipts=[{"path": l1.path, "status": "read"}],
            findings=[],
        ),
    )
    ctx = _prepare_runtime_context(
        real_agent,
        RuntimeContextRequest("隔离执行", None, None, context_scope="task_local"),
    )
    assert ctx is not None
    assert hit_calls == []
    # lesson_heat.json 始终零磁盘写
    assert heat_path.read_text(encoding="utf-8") == heat_before_text
    assert heat_path.stat().st_mtime_ns == heat_before_mtime_ns
    assert json.loads(json.dumps(list(lessons._pending_hits_map().keys()))) == [l1.lesson_id, l2.lesson_id]
