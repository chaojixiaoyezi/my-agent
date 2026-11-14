"""R2 生命周期闭环回归测试：回滚历史采纳保留、遗留记录补全绑定与打击纪元严格相等。"""
from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability import skill_learning_store as store_module
from agent_py_agent.agent.capability import skill_service
from agent_py_agent.agent.capability.skill_learning_host_feedback import observe_pack_verification
from agent_py_agent.agent.capability.skill_learning_maintenance import (
    LearningObservationSource,
    maintain_learning,
    observe_learning,
)
from agent_py_agent.agent.capability.skill_learning_prompt import SkillLearningDecision
from agent_py_agent.agent.capability.skill_learning_publish import (
    PublishRequest,
    publish_learned_skill,
    revert_learned_skill,
)
from agent_py_agent.agent.capability.skill_learning_store import (
    SkillLearningStore,
    skill_content_sha256,
)
from agent_py_agent.agent.capability.skill_learning_versions import freeze_learned_snapshot
from agent_py_agent.agent.capability.skill_usage_ledger import (
    learning_usage_binding,
    record_skill_usage_event,
)
from agent_py_agent.agent.capability.skill_usage_receipts import (
    read_usage_aggregate,
    record_exact_negative,
    record_exact_use,
)
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import runtime_db_path
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home, home_paths
from agent_py_agent.agent.user_space.owner_policy import resolve_effective_owner_policy
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    ensure_owner_home,
    home_paths_with_owner,
)

BASE_BODY = """---
name: sample
description: Deterministic workflow skill
---

# Steps

1. Inspect the explicit structural inputs.
2. Produce deterministic outputs cleanly.
"""

COOL_SECONDS = 604_800
ARCHIVE_SECONDS = 1_296_000
EVICT_SECONDS = 1_296_000
MAX_SKILLS = 50
BATCH_ENTRIES = 128


def _store_with_runtime(root: Path) -> tuple[object, RuntimeRepository, SkillLearningStore]:
    home = ensure_my_agent_home(root / "home")
    repo = RuntimeRepository(runtime_db_path(home.owner_home_dir))
    store = SkillLearningStore.for_home(home, usage_runtime_provider=lambda: repo)
    return home, repo, store


def _make_owner_home(root: Path):
    base = home_paths(root / "data")
    owner = ensure_owner_home(base.data_dir, OwnerIdentity.local_main())
    hp = home_paths_with_owner(base, owner)
    (Path(hp.owner_home_dir) / "skills").mkdir(parents=True, exist_ok=True)
    Path(hp.shared_skills_dir).mkdir(parents=True, exist_ok=True)
    Path(hp.shared_builtin_dir).mkdir(parents=True, exist_ok=True)
    repo = RuntimeRepository(runtime_db_path(hp.owner_home_dir))
    store = SkillLearningStore.for_home(hp, usage_runtime_provider=lambda: repo)
    return hp, repo, store


def _decision(name: str, *, suffix: str = "v1") -> SkillLearningDecision:
    return SkillLearningDecision(
        decision="create",
        reason="deterministic reusable workflow",
        name=name,
        description=f"Reusable helper for {name}",
        body=f"{BASE_BODY}\n\n# skill:{name}:{suffix}\n",
        when_to_use=f"when handling {name}",
        tags=(name, "workflow"),
    )


def _publish(store: SkillLearningStore, name: str, *, suffix: str = "v1", update_from=None):
    dec = _decision(name, suffix=suffix)
    expected_sha256 = ""
    expected_revision = None
    if update_from is not None:
        dec = replace(dec, decision="update", update_target=name)
        expected_sha256 = update_from.sha256
        expected_revision = update_from.revision
    return publish_learned_skill(
        store,
        PublishRequest(dec, expected_revision=expected_revision, expected_sha256=expected_sha256),
    )


def _agent_for(home, repo: RuntimeRepository, run_id: str, *, task_id: str = "", request_id: str = ""):
    params = SimpleNamespace(
        run_id=run_id,
        task_id=task_id,
        request_id=request_id,
        attempt_id="attempt-1",
        task_attributes={},
    )
    return SimpleNamespace(
        config=SimpleNamespace(enable_self_learning=True),
        home_paths=home,
        runtime_db=repo,
        _current_run_params=params,
    )


def _deliver_and_adopt(
    store: SkillLearningStore,
    rec,
    *,
    task_id: str,
    run_id: str,
    event: str = "manual_get",
    epoch: int | None = None,
) -> None:
    inc = str(rec.maintenance.get("incarnation_id") or "")
    act = str(rec.maintenance.get("activation_id") or "")
    strike_epoch = int(rec.maintenance.get("strike_epoch", 0) or 0) if epoch is None else epoch
    record_skill_usage_event(
        store,
        event=event,
        skill_id=f"owner:{rec.name}",
        source="learned",
        kind="skill",
        task_id=task_id,
        chars=240,
        body_chars=240,
        content_sha256=rec.sha256,
        incarnation_id=inc,
        activation_id=act,
        strike_epoch=strike_epoch,
    )
    observe_learning(
        store,
        rec.name,
        sha256=rec.sha256,
        task_id=task_id,
        outcome="adopted",
        source=LearningObservationSource(
            authority="model",
            run_id=run_id,
            incarnation_id=inc,
            activation_id=act,
            strike_epoch=strike_epoch,
        ),
    )


def _patch_maint(store: SkillLearningStore, name: str, *, remove_keys: tuple[str, ...] = (), **updates: object) -> None:
    with store.state_lock():
        reg = store.load_registry()
        rec = reg.skills[name]
        maint = dict(rec.maintenance)
        for key in remove_keys:
            maint.pop(key, None)
        maint.update(updates)
        store.save_registry(replace(reg, skills={**reg.skills, name: replace(rec, maintenance=maint)}))


def _maintain_once(store: SkillLearningStore, *, mode: str = "autonomous", now: float | None = None):
    return maintain_learning(
        store,
        mode=mode,
        cool_seconds=COOL_SECONDS,
        archive_seconds=ARCHIVE_SECONDS,
        evict_seconds=EVICT_SECONDS,
        max_skills=MAX_SKILLS,
        protection_complete=True,
        now=time.time() + 10.0 if now is None else now,
        batch_count=BATCH_ENTRIES,
        batch_ms=5000,
    )


def _converge(store: SkillLearningStore, *, now: float, max_passes: int = 25) -> list[dict[str, object]]:
    reports: list[dict[str, object]] = []
    for _ in range(max_passes):
        report = _maintain_once(store, mode="autonomous", now=now)
        reports.append(report)
        if not report["backlog"]:
            return reports
    raise AssertionError(f"maintenance did not converge within {max_passes} passes")


# ---------------------------------------------------------------------------
# P1-02: 回滚与崩溃恢复必须在清空 observations 前保留 _historical_adoptions
# ---------------------------------------------------------------------------


def test_p1_02_manual_revert_preserves_historical_adoptions_and_avoids_b_false_strike(tmp_path: Path) -> None:
    home, repo, store = _store_with_runtime(tmp_path / "manual")
    _publish(store, "skill-a", suffix="v1")
    a_v1 = store.load_registry().skills["skill-a"]
    _publish(store, "skill-a", suffix="v2", update_from=a_v1)
    _publish(store, "skill-b", suffix="v1")

    a_v2 = store.load_registry().skills["skill-a"]
    b_v1 = store.load_registry().skills["skill-b"]

    for idx in range(1, 4):
        _deliver_and_adopt(store, a_v2, task_id=f"task-ab-{idx}", run_id=f"run-ab-{idx}")
        _deliver_and_adopt(store, b_v1, task_id=f"task-ab-{idx}", run_id=f"run-ab-{idx}")

    revert_learned_skill(store, "skill-a")
    reg_after = store.load_registry()
    a_reverted = reg_after.skills["skill-a"]

    assert a_reverted.version == 1
    assert a_reverted.sha256 == a_v1.sha256
    assert a_reverted.maintenance["observations"] == []
    assert a_reverted.maintenance["strike_epoch"] == 0
    assert a_reverted.maintenance["independent_uses_coverage"] == "exact"
    hist = a_reverted.maintenance["historical_adoptions"]
    assert {(item["run_id"], item["sha256"]) for item in hist} == {
        (f"run-ab-{idx}", a_v2.sha256) for idx in range(1, 4)
    }

    # 无目标确定性 pack failure 落在曾同时采纳 {A-v2, B} 的 run 上时，不能误击打 B。
    agent_ab = _agent_for(home, repo, "run-ab-1", task_id="task-ab-1")
    observe_pack_verification(agent_ab, agent_ab._current_run_params, SimpleNamespace(status="failed", valid=False), "p1")
    assert store.load_registry().skills["skill-b"].maintenance["hurt_count"] == 0

    # 纯 B 运行上的无目标确定性 pack failure 必须精确击打 B 1 次。
    _deliver_and_adopt(store, b_v1, task_id="task-b-only", run_id="run-b-only")
    agent_b = _agent_for(home, repo, "run-b-only", task_id="task-b-only")
    observe_pack_verification(agent_b, agent_b._current_run_params, SimpleNamespace(status="failed", valid=False), "p2")
    assert store.load_registry().skills["skill-b"].maintenance["hurt_count"] == 1


def test_p1_02_recover_lifecycle_rollback_preserves_historical_adoptions_and_avoids_b_false_strike(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, repo, store = _store_with_runtime(tmp_path / "recover")
    _publish(store, "skill-a", suffix="v1")
    a_v1 = store.load_registry().skills["skill-a"]
    _publish(store, "skill-a", suffix="v2", update_from=a_v1)
    _publish(store, "skill-b", suffix="v1")

    a_v2 = store.load_registry().skills["skill-a"]
    b_v1 = store.load_registry().skills["skill-b"]

    for idx in range(1, 4):
        _deliver_and_adopt(store, a_v2, task_id=f"task-rec-{idx}", run_id=f"run-rec-{idx}")
        _deliver_and_adopt(store, b_v1, task_id=f"task-rec-{idx}", run_id=f"run-rec-{idx}")

    real_fsync = store_module.os.fsync
    live_path = store.skill_path("skill-a", record=a_v2)

    def _fail_post_replace_sync(fd: int) -> None:
        real_fsync(fd)
        if live_path.exists() and skill_content_sha256(live_path.read_text(encoding="utf-8")) == a_v1.sha256:
            raise OSError("simulated I/O error during parent sync")

    monkeypatch.setattr(store_module.os, "fsync", _fail_post_replace_sync)
    with pytest.raises(OSError, match="simulated I/O error"):
        revert_learned_skill(store, "skill-a")

    pending = store.load_registry().pending_operation
    assert pending is not None and pending["kind"] == "rollback"

    monkeypatch.setattr(store_module.os, "fsync", real_fsync)
    recovery_report = store.recover_lifecycle()
    assert recovery_report["status"] == "recovered"
    recovered = store.load_registry()
    assert recovered.pending_operation is None
    a_recovered = recovered.skills["skill-a"]
    assert a_recovered.version == 1
    assert a_recovered.sha256 == a_v1.sha256
    assert a_recovered.maintenance["observations"] == []
    assert a_recovered.maintenance["strike_epoch"] == 0
    assert a_recovered.maintenance["independent_uses_coverage"] == "exact"
    assert {(item["run_id"], item["sha256"]) for item in a_recovered.maintenance["historical_adoptions"]} == {
        (f"run-rec-{idx}", a_v2.sha256) for idx in range(1, 4)
    }

    agent_ab = _agent_for(home, repo, "run-rec-2", task_id="task-rec-2")
    observe_pack_verification(agent_ab, agent_ab._current_run_params, SimpleNamespace(status="failed", valid=False), "p1")
    assert store.load_registry().skills["skill-b"].maintenance["hurt_count"] == 0

    _deliver_and_adopt(store, b_v1, task_id="task-b-only-rec", run_id="run-b-only-rec")
    agent_b = _agent_for(home, repo, "run-b-only-rec", task_id="task-b-only-rec")
    observe_pack_verification(agent_b, agent_b._current_run_params, SimpleNamespace(status="failed", valid=False), "p2")
    assert store.load_registry().skills["skill-b"].maintenance["hurt_count"] == 1


def test_p1_02_proven_stable_rollback_on_third_strike_preserves_historical_adoptions(tmp_path: Path) -> None:
    home, repo, store = _store_with_runtime(tmp_path / "stable_rb")
    _publish(store, "skill-c", suffix="v1")
    c_v1 = store.load_registry().skills["skill-c"]
    for idx in range(1, 4):
        _deliver_and_adopt(store, c_v1, task_id=f"c-v1-{idx}", run_id=f"rc-v1-{idx}")
    _maintain_once(store)
    c_v1_est = store.load_registry().skills["skill-c"]
    assert c_v1_est.maintenance["quality"] == "established"

    _publish(store, "skill-c", suffix="v2", update_from=c_v1_est)
    c_v2 = store.load_registry().skills["skill-c"]
    _deliver_and_adopt(store, c_v2, task_id="c-v2-1", run_id="rc-v2-1")
    _deliver_and_adopt(store, c_v2, task_id="c-v2-2", run_id="rc-v2-2")

    for strike in range(1, 4):
        neg_task = f"c-v2-neg-{strike}"
        _deliver_and_adopt(store, c_v2, task_id=neg_task, run_id=f"rc-v2-neg-{strike}", epoch=strike - 1)
        observe_learning(
            store,
            "skill-c",
            sha256=c_v2.sha256,
            task_id=neg_task,
            outcome="ineffective",
            source=LearningObservationSource(
                authority="host",
                receipt_id=f"rcpt-neg-{strike}",
                incarnation_id=str(c_v2.maintenance["incarnation_id"]),
                activation_id=str(c_v2.maintenance["activation_id"]),
                strike_epoch=strike - 1,
            ),
        )

    c_restored = store.load_registry().skills["skill-c"]
    assert c_restored.version == 1
    assert c_restored.maintenance["quality"] == "established"
    assert c_restored.maintenance["strike_epoch"] == 0
    assert c_restored.maintenance["observations"] == []
    assert {
        (item["run_id"], item["sha256"]) for item in c_restored.maintenance["historical_adoptions"]
    } >= {("rc-v1-1", c_v1.sha256), ("rc-v2-1", c_v2.sha256), ("rc-v2-2", c_v2.sha256)}


def test_p1_02_p1_04_proven_stable_revert_and_recover_preserve_adoptions_and_reset_epoch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """覆盖 revert_learned_skill 与 _recover_rollback_locked 的 proven-stable 分支。"""
    home, _repo, store = _store_with_runtime(tmp_path / "stable_revert")
    del home
    for tag in ("direct", "crash"):
        name = f"skill-{tag}"
        _publish(store, name, suffix="v1")
        v1 = store.load_registry().skills[name]
        for idx in range(1, 4):
            _deliver_and_adopt(store, v1, task_id=f"{tag}-v1-{idx}", run_id=f"r-{tag}-v1-{idx}")
        _maintain_once(store)
        v1_est = store.load_registry().skills[name]
        assert v1_est.maintenance["quality"] == "established"

        _publish(store, name, suffix="v2", update_from=v1_est)
        v2 = store.load_registry().skills[name]
        _deliver_and_adopt(store, v2, task_id=f"{tag}-v2-1", run_id=f"r-{tag}-v2-1")
        # 先在 v2 上制造 1 次打击让 v2.strike_epoch 推进到 1，再执行回滚。
        observe_learning(
            store,
            name,
            sha256=v2.sha256,
            task_id=f"{tag}-v2-1",
            outcome="ineffective",
            source=LearningObservationSource(
                authority="host",
                receipt_id=f"rcpt-{tag}-1",
                incarnation_id=str(v2.maintenance["incarnation_id"]),
                activation_id=str(v2.maintenance["activation_id"]),
                strike_epoch=0,
            ),
        )
        assert store.load_registry().skills[name].maintenance["strike_epoch"] == 1

        if tag == "direct":
            revert_learned_skill(store, name)
        else:
            real_fsync = store_module.os.fsync
            live_path = store.skill_path(name, record=v2)

            def _fail_sync(fd: int, *, _real=real_fsync, _path=live_path, _sha=v1.sha256) -> None:
                _real(fd)
                if _path.exists() and skill_content_sha256(_path.read_text(encoding="utf-8")) == _sha:
                    raise OSError("simulated I/O error during stable revert sync")

            monkeypatch.setattr(store_module.os, "fsync", _fail_sync)
            with pytest.raises(OSError, match="simulated I/O error"):
                revert_learned_skill(store, name)
            monkeypatch.setattr(store_module.os, "fsync", real_fsync)
            assert store.recover_lifecycle()["status"] == "recovered"

        restored = store.load_registry().skills[name]
        assert restored.version == 1
        assert restored.maintenance["quality"] == "established"
        assert restored.maintenance["quality_basis"] == "rollback_established"
        assert restored.maintenance["strike_epoch"] == 0
        assert restored.maintenance["observations"] == []
        assert (f"r-{tag}-v2-1", v2.sha256) in {
            (item["run_id"], item["sha256"]) for item in restored.maintenance["historical_adoptions"]
        }


# ---------------------------------------------------------------------------
# P1-03: 遗留 learned 记录在写授权 ownership 维护内补齐绑定，读路径/observe 不造 ID
# ---------------------------------------------------------------------------


def test_p1_03_legacy_record_binding_only_in_write_maintenance_and_end_to_end_lifecycle(tmp_path: Path) -> None:
    hp, repo, store = _make_owner_home(tmp_path)
    _publish(store, "legacy-unused", suffix="v1")
    _publish(store, "legacy-used", suffix="v1")

    now = time.time() + 30.0
    hist_sample = [{"authority": "model", "outcome": "adopted", "run_id": "old-run", "sha256": "a" * 64}]
    obs_sample = [{"authority": "model", "outcome": "adopted", "run_id": "cur-run", "sha256": "b" * 64}]
    _patch_maint(
        store,
        "legacy-unused",
        remove_keys=("incarnation_id", "activation_id", "independent_uses", "uses_since_hurt"),
        ownership="machine",
        state="trial",
        quality="trial",
        baseline_at=now - 500.0,
        independent_uses_coverage="unknown",
    )
    _patch_maint(
        store,
        "legacy-used",
        incarnation_id="",
        activation_id="   ",
        ownership="machine",
        state="active",
        quality="trial",
        baseline_at=now - 1000.0,
        ever_used=True,
        last_used_at=now - 100.0,
        pinned=True,
        observations=obs_sample,
        historical_adoptions=hist_sample,
        independent_uses_coverage="unknown",
    )

    # 只读快照、usage binding 提取与 observe 模式严禁造 ID 或改写登记。
    svc = skill_service.SkillsService(
        home_paths=hp,
        workspace_root=tmp_path / "ws",
        policy_provider=lambda: resolve_effective_owner_policy(hp),
        learning_recommended_limit_provider=lambda: 50,
    )
    snap = freeze_learned_snapshot(svc.snapshot_for(), hp)
    dummy_agent = SimpleNamespace(home_paths=hp, runtime_db=repo)
    for skill_name in ("legacy-unused", "legacy-used"):
        entry = next(item for item in snap.entries if item.name == skill_name)
        assert "_learning_usage_binding" not in entry.asset_fields
        assert "incarnation_id" not in learning_usage_binding(dummy_agent, entry)

    observe_report = _maintain_once(store, mode="observe", now=now)
    assert observe_report["changed"] == []
    reg_after_observe = store.load_registry()
    assert not str(reg_after_observe.skills["legacy-unused"].maintenance.get("incarnation_id") or "").strip()
    assert not str(reg_after_observe.skills["legacy-used"].maintenance.get("activation_id") or "").strip()

    # 写授权自主维护在 ownership 阶段补齐缺失 ID 并保留既有历史边界。
    write_report = _maintain_once(store, mode="autonomous", now=now)
    assert {"legacy-unused", "legacy-used"} <= set(write_report["changed"])

    reg_bound = store.load_registry()
    unused_rec = reg_bound.skills["legacy-unused"]
    used_rec = reg_bound.skills["legacy-used"]

    unused_inc = unused_rec.maintenance["incarnation_id"]
    unused_act = unused_rec.maintenance["activation_id"]
    used_inc = used_rec.maintenance["incarnation_id"]
    used_act = used_rec.maintenance["activation_id"]
    assert len(unused_inc) == 32 and len(unused_act) == 32
    assert len(used_inc) == 32 and len(used_act) == 32
    assert unused_rec.maintenance["independent_uses_coverage"] == "exact"
    assert used_rec.maintenance["independent_uses_coverage"] == "lower_bound"
    assert used_rec.maintenance["ever_used"] is True
    assert used_rec.maintenance["last_used_at"] == now - 100.0
    assert used_rec.maintenance["pinned"] is True
    assert used_rec.maintenance["observations"] == obs_sample
    assert used_rec.maintenance["historical_adoptions"] == hist_sample
    assert used_rec.maintenance["state"] == "active"
    assert used_rec.maintenance["quality"] == "trial"
    assert used_rec.maintenance["baseline_at"] == now - 1000.0
    assert unused_rec.maintenance["independent_uses"] == 0
    assert unused_rec.maintenance["uses_since_hurt"] == 0
    assert unused_rec.maintenance["hurt_count"] == 0
    assert unused_rec.maintenance["strike_epoch"] == 0

    # 再次维护必须保持 ID 稳定不变。
    _maintain_once(store, mode="autonomous", now=now + 5.0)
    reg_stable = store.load_registry()
    assert reg_stable.skills["legacy-unused"].maintenance["incarnation_id"] == unused_inc
    assert reg_stable.skills["legacy-unused"].maintenance["activation_id"] == unused_act
    assert reg_stable.skills["legacy-used"].maintenance["incarnation_id"] == used_inc
    assert reg_stable.skills["legacy-used"].maintenance["activation_id"] == used_act

    # 绑定后 3 次全新独立正文投递建立质量，随后第 3 次宿主负向反馈按合同退场封禁。
    for idx in range(1, 4):
        record_skill_usage_event(
            store,
            event="manual_get",
            skill_id="owner:legacy-unused",
            source="learned",
            kind="skill",
            task_id=f"leg-use-{idx}",
            chars=240,
            body_chars=240,
            content_sha256=unused_rec.sha256,
            incarnation_id=unused_inc,
            activation_id=unused_act,
            strike_epoch=0,
        )
    _maintain_once(store, mode="autonomous", now=now + 10.0)
    established_leg = store.load_registry().skills["legacy-unused"]
    assert established_leg.maintenance["quality"] == "established"
    assert established_leg.maintenance["state"] == "active"

    for strike in range(1, 4):
        neg_task = f"leg-neg-{strike}"
        record_skill_usage_event(
            store,
            event="manual_get",
            skill_id="owner:legacy-unused",
            source="learned",
            kind="skill",
            task_id=neg_task,
            chars=240,
            body_chars=240,
            content_sha256=unused_rec.sha256,
            incarnation_id=unused_inc,
            activation_id=unused_act,
            strike_epoch=strike - 1,
        )
        observe_learning(
            store,
            "legacy-unused",
            sha256=unused_rec.sha256,
            task_id=neg_task,
            outcome="ineffective",
            source=LearningObservationSource(
                authority="host",
                receipt_id=f"leg-rcpt-{strike}",
                incarnation_id=unused_inc,
                activation_id=unused_act,
                strike_epoch=strike - 1,
            ),
        )
    final_reg = store.load_registry()
    assert "legacy-unused" not in final_reg.skills
    assert "legacy-unused" in final_reg.blocked_names


@pytest.mark.timeout(180)
def test_p1_03_275_legacy_skills_convergence_binds_survivors_and_keeps_known_used(tmp_path: Path) -> None:
    _home, _repo, store = _store_with_runtime(tmp_path / "conv275")
    names = [f"leg-{index:03d}" for index in range(275)]
    now = time.time() + 60.0

    # 先创建 275 个未绑定遗留技能，其中前 22 条带已知历史使用标记。
    for idx, name in enumerate(names):
        _publish(store, name)
        extra_updates: dict[str, object] = {
            "ownership": "machine",
            "baseline_at": now - (275 - idx) * 10.0,
            "ever_used": idx < 22,
            "independent_uses_coverage": "unknown",
        }
        if idx < 22:
            extra_updates["last_used_at"] = now - 3600.0
        _patch_maint(
            store,
            name,
            remove_keys=("incarnation_id", "activation_id", "independent_uses", "uses_since_hurt", "last_used_at"),
            **extra_updates,
        )

    _converge(store, now=now, max_passes=25)
    reg = store.load_registry()
    live = {
        name
        for name, rec in reg.skills.items()
        if rec.layout != "owner"
        and rec.maintenance.get("state") != "archived"
        and rec.maintenance.get("ownership") not in {"owner_layout", "user_modified"}
    }
    known_used = set(names[:22])
    assert len(live) == MAX_SKILLS
    assert known_used <= live

    # 所有经所有权核验为 machine 托管的存活与归档记录均须完成稳定 ID 绑定。
    machine_records = [
        rec
        for rec in reg.skills.values()
        if rec.layout != "owner" and rec.maintenance.get("ownership") == "machine"
    ]
    assert len(machine_records) == 275
    for rec in machine_records:
        assert str(rec.maintenance.get("incarnation_id") or "").strip()
        assert str(rec.maintenance.get("activation_id") or "").strip()

    first_ids = {rec.name: (rec.maintenance["incarnation_id"], rec.maintenance["activation_id"]) for rec in machine_records}
    _converge(store, now=now + 1.0, max_passes=25)
    reg2 = store.load_registry()
    for name, pair in first_ids.items():
        rec2 = reg2.skills[name]
        assert (rec2.maintenance["incarnation_id"], rec2.maintenance["activation_id"]) == pair


# ---------------------------------------------------------------------------
# P1-04: 回滚重置 strike_epoch=0 及回执 epoch 严格相等
# ---------------------------------------------------------------------------


def test_p1_04_rollback_resets_strike_epoch_and_enforces_exact_epoch_equality(tmp_path: Path) -> None:
    _home, repo, store = _store_with_runtime(tmp_path / "epoch_rb")
    _publish(store, "epoch-skill", suffix="v1")
    v1 = store.load_registry().skills["epoch-skill"]
    for idx in range(1, 4):
        _deliver_and_adopt(store, v1, task_id=f"e-v1-{idx}", run_id=f"re-v1-{idx}")
    _maintain_once(store)
    v1_est = store.load_registry().skills["epoch-skill"]
    assert v1_est.maintenance["quality"] == "established"

    _publish(store, "epoch-skill", suffix="v2", update_from=v1_est)
    v2 = store.load_registry().skills["epoch-skill"]
    for strike in range(1, 4):
        neg_task = f"e-v2-neg-{strike}"
        _deliver_and_adopt(store, v2, task_id=neg_task, run_id=f"re-v2-neg-{strike}", epoch=strike - 1)
        observe_learning(
            store,
            "epoch-skill",
            sha256=v2.sha256,
            task_id=neg_task,
            outcome="ineffective",
            source=LearningObservationSource(
                authority="host",
                receipt_id=f"e-v2-rcpt-{strike}",
                incarnation_id=str(v2.maintenance["incarnation_id"]),
                activation_id=str(v2.maintenance["activation_id"]),
                strike_epoch=strike - 1,
            ),
        )

    restored = store.load_registry().skills["epoch-skill"]
    assert restored.version == 1
    assert restored.maintenance["quality"] == "established"
    assert restored.maintenance["hurt_count"] == 0
    assert restored.maintenance["uses_since_hurt"] == 0
    assert restored.maintenance["strike_epoch"] == 0

    # 恢复后的 v1 受 1 次负反馈：hurt_count=1, strike_epoch 0 -> 1, quality 降为 trial。
    _deliver_and_adopt(store, restored, task_id="e-restored-neg-1", run_id="re-restored-neg-1", epoch=0)
    observe_learning(
        store,
        "epoch-skill",
        sha256=restored.sha256,
        task_id="e-restored-neg-1",
        outcome="ineffective",
        source=LearningObservationSource(
            authority="host",
            receipt_id="e-restored-rcpt-1",
            incarnation_id=str(restored.maintenance["incarnation_id"]),
            activation_id=str(restored.maintenance["activation_id"]),
            strike_epoch=0,
        ),
    )
    after_s1 = store.load_registry().skills["epoch-skill"]
    assert after_s1.maintenance["hurt_count"] == 1
    assert after_s1.maintenance["strike_epoch"] == 1
    assert after_s1.maintenance["quality"] == "trial"

    # 4 次携带过期/错位 epoch=2 的延迟投递均不得计入 uses_since_hurt。
    for idx in range(1, 5):
        _deliver_and_adopt(store, after_s1, task_id=f"stale-e2-{idx}", run_id=f"r-stale-{idx}", epoch=2)
    _maintain_once(store)
    after_stale = store.load_registry().skills["epoch-skill"]
    assert after_stale.maintenance["uses_since_hurt"] == 0
    assert after_stale.maintenance["quality"] == "trial"
    agg = read_usage_aggregate(
        repo,
        incarnation_id=str(after_stale.maintenance["incarnation_id"]),
        activation_id=str(after_stale.maintenance["activation_id"]),
        content_sha256=after_stale.sha256,
    )
    assert agg is not None and agg["uses_since_hurt"] == 0

    # 当前 epoch=1 严格遵循 3 + hurt_count = 4 阶梯升级。
    for idx in range(1, 4):
        _deliver_and_adopt(store, after_stale, task_id=f"cur-e1-{idx}", run_id=f"r-e1-{idx}", epoch=1)
    _maintain_once(store)
    mid_e1 = store.load_registry().skills["epoch-skill"]
    assert mid_e1.maintenance["uses_since_hurt"] == 3
    assert mid_e1.maintenance["quality"] == "trial"

    _deliver_and_adopt(store, mid_e1, task_id="cur-e1-4", run_id="r-e1-4", epoch=1)
    _maintain_once(store)
    re_est = store.load_registry().skills["epoch-skill"]
    assert re_est.maintenance["uses_since_hurt"] == 4
    assert re_est.maintenance["quality"] == "established"

    # 第 2 次负反馈后 strike_epoch 1 -> 2，需 3 + 2 = 5 次当前 epoch=2 使用方能恢复 established。
    _deliver_and_adopt(store, re_est, task_id="e-restored-neg-2", run_id="re-restored-neg-2", epoch=1)
    observe_learning(
        store,
        "epoch-skill",
        sha256=re_est.sha256,
        task_id="e-restored-neg-2",
        outcome="ineffective",
        source=LearningObservationSource(
            authority="host",
            receipt_id="e-restored-rcpt-2",
            incarnation_id=str(re_est.maintenance["incarnation_id"]),
            activation_id=str(re_est.maintenance["activation_id"]),
            strike_epoch=1,
        ),
    )
    after_s2 = store.load_registry().skills["epoch-skill"]
    assert after_s2.maintenance["hurt_count"] == 2
    assert after_s2.maintenance["strike_epoch"] == 2
    assert after_s2.maintenance["quality"] == "trial"

    for idx in range(1, 5):
        _deliver_and_adopt(store, after_s2, task_id=f"cur-e2-{idx}", run_id=f"r-e2-{idx}", epoch=2)
    _maintain_once(store)
    assert store.load_registry().skills["epoch-skill"].maintenance["uses_since_hurt"] == 4
    assert store.load_registry().skills["epoch-skill"].maintenance["quality"] == "trial"

    _deliver_and_adopt(store, after_s2, task_id="cur-e2-5", run_id="r-e2-5", epoch=2)
    _maintain_once(store)
    assert store.load_registry().skills["epoch-skill"].maintenance["uses_since_hurt"] == 5
    assert store.load_registry().skills["epoch-skill"].maintenance["quality"] == "established"


def test_p1_04_duplicate_same_task_receipt_same_vs_next_and_future_epoch(tmp_path: Path) -> None:
    _home, repo, store = _store_with_runtime(tmp_path / "dup_epoch")
    _publish(store, "dup-skill", suffix="v1")
    rec = store.load_registry().skills["dup-skill"]
    inc = str(rec.maintenance["incarnation_id"])
    act = str(rec.maintenance["activation_id"])

    # 1) epoch=0 首次计数
    first = record_exact_use(
        store,
        {
            "event": "manual_get",
            "kind": "skill",
            "incarnation_id": inc,
            "activation_id": act,
            "content_sha256": rec.sha256,
            "task_key": "task-shared",
            "body_chars": 240,
            "strike_epoch": 0,
            "at": 1000.0,
        },
        runtime=repo,
    )
    assert first["status"] == "counted"
    assert first["uses_since_hurt_incremented"] is True
    assert first["aggregate"]["uses_since_hurt"] == 1

    # 2) 同 epoch=0 同任务重复回执不增 uses_since_hurt 与 independent_uses
    dup_same = record_exact_use(
        store,
        {
            "event": "manual_get",
            "kind": "skill",
            "incarnation_id": inc,
            "activation_id": act,
            "content_sha256": rec.sha256,
            "task_key": "task-shared",
            "body_chars": 240,
            "strike_epoch": 0,
            "at": 1001.0,
        },
        runtime=repo,
    )
    assert dup_same["status"] == "duplicate_refreshed"
    assert dup_same["independent_uses_incremented"] is False
    assert dup_same["uses_since_hurt_incremented"] is False
    assert dup_same["aggregate"]["uses_since_hurt"] == 1
    assert dup_same["aggregate"]["independent_uses"] == 1

    # 3) 负反馈推动 strike_epoch 0 -> 1，uses_since_hurt 清零
    neg = record_exact_negative(
        store,
        incarnation_id=inc,
        activation_id=act,
        content_sha256=rec.sha256,
        task_id="neg-1",
        receipt_id="rcpt-1",
        at=1002.0,
        runtime=repo,
    )
    assert neg["strike_epoch"] == 1

    # 4) 同任务携带错位未来 epoch=2 严禁记入新轮 uses_since_hurt
    dup_future = record_exact_use(
        store,
        {
            "event": "manual_get",
            "kind": "skill",
            "incarnation_id": inc,
            "activation_id": act,
            "content_sha256": rec.sha256,
            "task_key": "task-shared",
            "body_chars": 240,
            "strike_epoch": 2,
            "at": 1003.0,
        },
        runtime=repo,
    )
    assert dup_future["uses_since_hurt_incremented"] is False
    assert dup_future["aggregate"]["uses_since_hurt"] == 0

    # 5) 同任务在精确匹配当前 epoch=1 时允许在新周期记分 1 次，再次重复不再增加
    dup_next = record_exact_use(
        store,
        {
            "event": "manual_get",
            "kind": "skill",
            "incarnation_id": inc,
            "activation_id": act,
            "content_sha256": rec.sha256,
            "task_key": "task-shared",
            "body_chars": 240,
            "strike_epoch": 1,
            "at": 1004.0,
        },
        runtime=repo,
    )
    assert dup_next["status"] == "counted_new_epoch"
    assert dup_next["independent_uses_incremented"] is False
    assert dup_next["uses_since_hurt_incremented"] is True
    assert dup_next["aggregate"]["uses_since_hurt"] == 1

    dup_next_again = record_exact_use(
        store,
        {
            "event": "manual_get",
            "kind": "skill",
            "incarnation_id": inc,
            "activation_id": act,
            "content_sha256": rec.sha256,
            "task_key": "task-shared",
            "body_chars": 240,
            "strike_epoch": 1,
            "at": 1005.0,
        },
        runtime=repo,
    )
    assert dup_next_again["uses_since_hurt_incremented"] is False
    assert dup_next_again["aggregate"]["uses_since_hurt"] == 1
