"""生命周期发布前后单次同步错误的登记保留与冷启恢复回归。"""
from __future__ import annotations

import errno
from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent.capability import skill_learning_store as store_module
from agent_py_agent.agent.capability.skill_learning_publish import render_learned_skill
from agent_py_agent.agent.capability.skill_learning_store import (
    CODE_EVIDENCE_INCOMPLETE,
    LearnedSkill,
    SkillLearningRegistry,
    SkillLearningStore,
    SkillLearningStoreError,
)
from agent_py_agent.agent.capability.skill_snapshot import skill_content_sha256
from agent_py_agent.tests.test_sm_w2_usage_strikes import _publish, _store_with_runtime
from agent_py_agent.tests.test_wave7_autonomous_learning_closure import decision


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
