"""W1 合同测试：归属校验、容量准入与 R-2 置换、可恢复状态机转换、显式动作及崩毁恢复。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability import skill_learning_store as store_module
from agent_py_agent.agent.capability.skill_learning_migration import adopt_committed_lessons
from agent_py_agent.agent.capability.skill_learning_promotion import (
    promote_verified_skill,
    propose_or_promote_skill,
)
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
    MISSING,
    SkillLearningRegistry,
    SkillLearningStore,
    SkillLearningStoreError,
    is_proven_stable_version,
    skill_family_fingerprint,
)
from agent_py_agent.agent.capability.skill_proposals import SkillProposalError
from agent_py_agent.agent.capability.skill_snapshot import skill_content_sha256
from agent_py_agent.agent.runtime_db.schema import runtime_db_path
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaExceeded, OwnerQuotaProjection
from agent_py_agent.tests.test_skill_learning import BODY
from agent_py_agent.tests.test_wave7_autonomous_learning_closure import decision


def _home_and_store(tmp_path: Path, *, usage_runtime_provider=None) -> tuple[object, SkillLearningStore]:
    home = ensure_my_agent_home(tmp_path / "home")
    store = SkillLearningStore.for_home(home, usage_runtime_provider=usage_runtime_provider)
    return home, store


def _publish(store: SkillLearningStore, name: str, *, body: str | None = None, **req_kwargs: object):
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
    _publish(store, "alpha")

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
    _publish(store, "live-trial")
    _publish(store, "disabled-live")
    _publish(store, "archived-skill")
    _publish(store, "user-edited")
    _publish(store, "hand-flagged")
    _publish(store, "owner-rec")

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
    _publish(unknown_store, "s1")
    _set_maint(unknown_store, "s1", ownership="unknown")
    with pytest.raises(SkillLearningStoreError) as exc1:
        unknown_store.managed_records(unknown_store.load_registry())
    assert exc1.value.code == CODE_EVIDENCE_INCOMPLETE

    # 2) 未恢复 pending_operation (phase="moved") -> CODE_EVIDENCE_INCOMPLETE
    _, pending_store = _home_and_store(tmp_path / "pending")
    _publish(pending_store, "s1")
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
    _publish(big_store, "s1")
    big_store.skill_path("s1").write_text("x" * (MAX_SKILL_BYTES + 16), encoding="utf-8")
    with pytest.raises(SkillLearningStoreError) as exc_big:
        big_store.managed_records(big_store.load_registry())
    assert exc_big.value.code == CODE_EVIDENCE_INCOMPLETE

    # 3) 无未决操作时，手动删除的 live 正文不再占用托管槽位。
    _, missing_store = _home_and_store(tmp_path / "missing")
    _publish(missing_store, "s1")
    missing_store.skill_path("s1").unlink()
    assert "s1" not in missing_store.managed_records(missing_store.load_registry())

    # 4) 托管技能 live 文件为符号链接
    _, sym_store = _home_and_store(tmp_path / "symlink")
    _publish(sym_store, "s1")
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
    _publish(bad_utf8, "s1")
    bad_utf8.skill_path("s1").write_bytes(b"\xff\xfe\xfd")
    with pytest.raises(SkillLearningStoreError) as exc5:
        bad_utf8.managed_records(bad_utf8.load_registry())
    assert exc5.value.code == CODE_EVIDENCE_INCOMPLETE

    # 6) 不可读权限文件（非 root 下）
    if os.geteuid() != 0:
        _, unreadable = _home_and_store(tmp_path / "chmod")
        _publish(unreadable, "s1")
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
    _publish(store, "a", config_provider=lambda: cfg)
    _publish(store, "b", config_provider=lambda: cfg)

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
        _publish(store, "c", config_provider=providers.read_cfg, protection_provider=providers.read_prot)
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
        _publish(
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
        _publish(
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
            _publish(
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
        _publish(store, name, max_skills=10)

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
    ev1 = _publish(
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
    ev2 = _publish(
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
    ev3 = _publish(
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
    _publish(store, "slot-1", max_skills=2)
    _publish(store, "slot-2", max_skills=2)
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
        return _publish(
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
    _publish(store, "trial-only", body=f"{BODY}\n\n# trial-v1")
    r1 = store.load_registry().skills["trial-only"]
    assert not is_proven_stable_version(
        {"version": r1.version, "sha256": r1.sha256, "established_at": r1.maintenance.get("established_at", 0.0)}
    )
    upd_v2 = replace(decision("trial-only"), decision="update", update_target="trial-only", body=f"{BODY}\n\n# trial-v2")
    publish_learned_skill(store, PublishRequest(upd_v2, expected_revision=r1.revision, expected_sha256=r1.sha256))
    r2 = store.load_registry().skills["trial-only"]
    assert r2.maintenance.get("previous_stable") is None

    # 2) 已证实 established v1 -> v2 -> v3 必须持续保留 v1 为 previous_stable。
    _publish(store, "proven-s", body=f"{BODY}\n\n# stable-v1")
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
    _publish(store, "retire-me", body=same_family_body)
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
        _publish(store, "renamed-clone", body=same_family_body)
    assert exc_fam.value.code == CODE_FAMILY_BLOCKED


def test_explicit_actions_report_and_s1_migration_isolation(tmp_path: Path, monkeypatch):
    _, store = _home_and_store(tmp_path / "actions")
    _publish(store, "act-skill", body=f"{BODY}\n\n# v1")
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
        _publish(store, "act-clone", body=f"{BODY}\n\n# clone")
    assert exc_removed_fam.value.code == CODE_FAMILY_BLOCKED

    # learned_skills_report 暴露 managed_count 与 coverage，且证据不全时显式上报 unavailable。
    _publish(store, "rep-skill", body="1. Inspect manifest checksums.\n2. Record verified signature.")
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
    _publish(store, "alpha", body=f"{BODY}\n\n# alpha-v1")
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
        _publish(store, "beta", body=f"{BODY}\n\n# beta-v1")

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
    _publish(store, "keep-user", body=f"{BODY}\n\n# v1")
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
    _publish(store, "multi-ver", body=f"{BODY}\n\n# v1")
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
    _publish(store, "post-commit", body="1. Compare archive ledger.\n2. Emit final manifest.")
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
    _publish(store, "rb-trial", body=f"{BODY}\n\n# v1")
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
    _publish(fresh, "rb-proven", body=f"{BODY}\n\n# v1")
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
    _publish(store, "live-extra")
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

    _publish(store, "arch-extra", body=f"{BODY}\n\n# arch")
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
    _publish(store, "crash-extra", body=f"{BODY}\n\n# crash-extra")
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
    _publish(store, "victim-user", body=f"{BODY}\n\n# victim-user")
    _publish(store, "victim-prot", body=f"{BODY}\n\n# victim-prot")
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
        _publish(
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
    _publish(store, "symlink-retire", body=f"{BODY}\n\n# symlink-retire")
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
