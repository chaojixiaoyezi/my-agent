from __future__ import annotations

import inspect
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability import (
    learned_metadata_projection,
    skill_learning_versions,
    skill_service,
    turn_recommendations,
)
from agent_py_agent.agent.capability.skill_learning_maintenance import control_learning
from agent_py_agent.agent.capability.skill_learning_prompt import SkillLearningDecision
from agent_py_agent.agent.capability.skill_learning_publish import (
    PublishRequest,
    publish_learned_skill,
    revert_learned_skill,
)
from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
from agent_py_agent.agent.capability.skill_snapshot import SkillSnapshot, SkillSnapshotEntry
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_policy import resolve_effective_owner_policy
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    ensure_owner_home,
    home_paths_with_owner,
)


def _make_owner_home(root: Path, owner_id: str = "main"):
    base = home_paths(root / "data")
    identity = OwnerIdentity.local_main() if owner_id == "main" else OwnerIdentity.provider_user("test", owner_id)
    owner = ensure_owner_home(base.data_dir, identity)
    hp = home_paths_with_owner(base, owner)
    (Path(hp.owner_home_dir) / "skills").mkdir(parents=True, exist_ok=True)
    Path(hp.shared_skills_dir).mkdir(parents=True, exist_ok=True)
    Path(hp.shared_builtin_dir).mkdir(parents=True, exist_ok=True)
    return hp


def _make_service(
    hp,
    workspace: Path,
    *,
    limit_box: list[int] | None = None,
    explore_box: list[int] | None = None,
    pass_explore_provider: bool = True,
) -> skill_service.SkillsService:
    limits = limit_box if limit_box is not None else [50]
    explores = explore_box if explore_box is not None else [0]
    kwargs: dict[str, object] = {
        "home_paths": hp,
        "workspace_root": workspace,
        "policy_provider": lambda: resolve_effective_owner_policy(hp),
        "learning_recommended_limit_provider": lambda: limits[0],
    }
    if pass_explore_provider:
        kwargs["learning_explore_percent_provider"] = lambda: explores[0]
    return skill_service.SkillsService(**kwargs)


def _publish(store: SkillLearningStore, name: str, body: str, *, description: str = "learned helper"):
    cfg = SimpleNamespace(enable_self_learning=True, self_learning_maintenance_mode="autonomous")
    decision = SkillLearningDecision(
        decision="create",
        reason="deterministic reusable workflow",
        name=name,
        description=description,
        body=body,
    )
    return publish_learned_skill(
        store,
        PublishRequest(decision=decision, config_provider=lambda: cfg),
    )


def _update(store: SkillLearningStore, name: str, body: str, expected_revision: int | None = None, *, description: str = "learned helper v2"):
    cfg = SimpleNamespace(enable_self_learning=True, self_learning_maintenance_mode="autonomous")
    current = store.load_registry().skills[name]
    decision = SkillLearningDecision(
        decision="update",
        reason="improved deterministic workflow",
        update_target=name,
        name=name,
        description=description,
        body=body,
    )
    return publish_learned_skill(
        store,
        PublishRequest(
            decision=decision,
            expected_revision=current.revision if expected_revision is None else expected_revision,
            expected_sha256=current.sha256,
            config_provider=lambda: cfg,
        ),
    )


def _save_registry(store: SkillLearningStore, registry) -> None:
    with store.state_lock():
        store.save_registry(registry)


def _write_skill_dir(base_dir: Path, name: str, description: str, body: str) -> Path:
    skill_dir = base_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / "SKILL.md"
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _clear_learning_memo_and_public_caches():
    skill_learning_versions._LEARNING_STATE_MEMO.clear()
    with skill_service._PROCESS_SKILL_CACHE_LOCK:
        skill_service._PROCESS_BUILTIN_ROOT_CACHE.clear()
        skill_service._PROCESS_SKILL_DIR_CACHE.clear()
    yield
    skill_learning_versions._LEARNING_STATE_MEMO.clear()
    with skill_service._PROCESS_SKILL_CACHE_LOCK:
        skill_service._PROCESS_BUILTIN_ROOT_CACHE.clear()
        skill_service._PROCESS_SKILL_DIR_CACHE.clear()


def test_constructor_signature_and_legacy_explore_default(tmp_path: Path):
    params = list(inspect.signature(skill_service.SkillsService.__init__).parameters.values())
    assert params[-1].name == "learning_explore_percent_provider"
    assert params[-1].default is None

    hp = _make_owner_home(tmp_path)
    store = SkillLearningStore.for_home(hp)
    _publish(store, "alpha-skill", "Alpha body")

    svc = _make_service(hp, tmp_path / "ws", pass_explore_provider=False)
    assert svc.learning_explore_percent_provider() == 0
    snap = svc.snapshot_for()
    entry = next(item for item in snap.entries if item.name == "alpha-skill")
    assert entry.recommended is True
    assert entry.frozen_path


def test_bookkeeping_counter_writes_preserve_snapshot_identity_and_fingerprint(tmp_path: Path):
    hp = _make_owner_home(tmp_path)
    store = SkillLearningStore.for_home(hp)
    _publish(store, "alpha-skill", "Alpha body")
    _publish(store, "beta-skill", "Beta body")

    svc = _make_service(hp, tmp_path / "ws", limit_box=[50], explore_box=[0])
    cold = svc.snapshot_for()
    fp_before = skill_learning_versions.learning_state_fingerprint(hp)

    # 仅修改账本计数器/时间戳，不改变语义投影与推荐成员集合。
    reg = store.load_registry()
    alpha = reg.skills["alpha-skill"]
    updated_alpha = replace(
        alpha,
        updated_at="2026-11-14T12:00:00Z",
        maintenance={
            **alpha.maintenance,
            "independent_uses": int(alpha.maintenance.get("independent_uses", 0)) + 3,
            "helped_count": int(alpha.maintenance.get("helped_count", 0)) + 2,
            "uses_since_hurt": 2,
            "last_used_at": 1700000123.0,
            "observations": [{"kind": "helped", "at": 1700000123.0}],
        },
    )
    updated_reg = replace(
        reg,
        daily_date="2026-11-14",
        daily_calls=reg.daily_calls + 4,
        daily_history={**reg.daily_history, "2026-11-14": 4},
        last_maintenance_at=1700000999.0,
        usage_watermark=42,
        maintenance_cursor={"phase": "usage", "after_name": "alpha-skill"},
        assets={"pkg-note": {"count": 7}},
        skills={**reg.skills, "alpha-skill": updated_alpha},
    )
    _save_registry(store,updated_reg)

    fp_after = skill_learning_versions.learning_state_fingerprint(hp)
    assert fp_after == fp_before
    hot = svc.snapshot_for()
    assert hot is cold


def test_rank_boundary_crossing_and_live_policy_providers(tmp_path: Path):
    hp = _make_owner_home(tmp_path)
    store = SkillLearningStore.for_home(hp)
    _publish(store, "proven-skill", "Proven body")
    _publish(store, "fresh-skill", "Fresh body")

    reg = store.load_registry()
    proven = reg.skills["proven-skill"]
    fresh = reg.skills["fresh-skill"]
    _save_registry(
        store,
        replace(
            reg,
            skills={
                "proven-skill": replace(
                    proven,
                    maintenance={
                        **proven.maintenance,
                        "quality": "established",
                        "established_at": 1700000050.0,
                        "last_used_at": 1700000100.0,
                        "ever_used": True,
                        "independent_uses": 5,
                    },
                ),
                "fresh-skill": replace(
                    fresh,
                    maintenance={
                        **fresh.maintenance,
                        "quality": "trial",
                        "ever_used": False,
                        "independent_uses": 0,
                        "baseline_at": 1700000500.0,
                    },
                ),
            },
        ),
    )

    limit_box = [1]
    explore_box = [0]
    svc = _make_service(hp, tmp_path / "ws", limit_box=limit_box, explore_box=explore_box)

    snap1 = svc.snapshot_for()
    by_name1 = {e.name: e for e in snap1.entries}
    assert by_name1["proven-skill"].recommended is True
    assert by_name1["fresh-skill"].recommended is False

    # 动态修改探索比例与推荐上限（模拟 UserConfigTool 实时修改），不重建 SkillsService、不重读 registry.json。
    load_calls: list[str] = []
    orig_load = SkillLearningStore.load_registry

    def _counting_load(self):
        load_calls.append("load")
        return orig_load(self)

    explore_box[0] = 100
    orig = SkillLearningStore.load_registry
    try:
        SkillLearningStore.load_registry = _counting_load
        snap2 = svc.snapshot_for()
    finally:
        SkillLearningStore.load_registry = orig
    assert load_calls == []
    assert snap2 is not snap1
    by_name2 = {e.name: e for e in snap2.entries}
    assert by_name2["fresh-skill"].recommended is True
    assert by_name2["proven-skill"].recommended is False

    # 切回 explore=0 且扩大 limit=2，两个全部进入推荐。
    explore_box[0] = 0
    limit_box[0] = 2
    snap3 = svc.snapshot_for()
    by_name3 = {e.name: e for e in snap3.entries}
    assert by_name3["proven-skill"].recommended is True
    assert by_name3["fresh-skill"].recommended is True

    # 在 limit=1, explore=0 下跨越推荐排名边界：fresh 变为更高 last_used_at 的 established，必须失效并切换。
    limit_box[0] = 1
    snap4 = svc.snapshot_for()
    assert {e.name for e in snap4.entries if e.recommended} == {"proven-skill"}

    reg_now = store.load_registry()
    fresh_now = reg_now.skills["fresh-skill"]
    _save_registry(store,
        replace(
            reg_now,
            skills={
                **reg_now.skills,
                "fresh-skill": replace(
                    fresh_now,
                    maintenance={
                        **fresh_now.maintenance,
                        "quality": "established",
                        "established_at": 1700000900.0,
                        "last_used_at": 1700000999.0,
                        "ever_used": True,
                        "independent_uses": 9,
                    },
                ),
            },
        )
    )
    snap5 = svc.snapshot_for()
    assert snap5 is not snap4
    assert {e.name for e in snap5.entries if e.recommended} == {"fresh-skill"}


def test_lifecycle_import_activation_and_strike_epoch_invalidation(tmp_path: Path):
    hp = _make_owner_home(tmp_path)
    store = SkillLearningStore.for_home(hp)
    _publish(store, "cycle-skill", "Body version one")
    svc = _make_service(hp, tmp_path / "ws")

    # 1) preparing 导入态隐藏；同 name/hash/state 切为 committed 后立刻可见。
    reg = store.load_registry()
    _save_registry(store,
        replace(
            reg,
            import_operations={"op-1": {"status": "preparing", "target_names": ["cycle-skill"]}},
        )
    )
    hidden_snap = svc.snapshot_for()
    assert all(e.name != "cycle-skill" for e in hidden_snap.entries)

    _save_registry(store,
        replace(
            store.load_registry(),
            import_operations={"op-1": {"status": "committed", "target_names": ["cycle-skill"]}},
        )
    )
    visible_snap = svc.snapshot_for()
    entry_v1 = next(e for e in visible_snap.entries if e.name == "cycle-skill")
    binding_v1 = entry_v1.asset_fields["_learning_usage_binding"]
    assert set(binding_v1) == {"incarnation_id", "activation_id", "content_sha256", "strike_epoch"}
    assert binding_v1["strike_epoch"] == 0

    # 2) 同正文同 hash 下仅更换 activation_id，快照与绑定必须刷新。
    reg = store.load_registry()
    rec = reg.skills["cycle-skill"]
    _save_registry(store,
        replace(
            reg,
            skills={
                **reg.skills,
                "cycle-skill": replace(
                    rec,
                    maintenance={**rec.maintenance, "activation_id": "act-reheated-same-body"},
                ),
            },
        )
    )
    reheated_snap = svc.snapshot_for()
    assert reheated_snap is not visible_snap
    entry_reheated = next(e for e in reheated_snap.entries if e.name == "cycle-skill")
    assert entry_reheated.asset_fields["_learning_usage_binding"]["activation_id"] == "act-reheated-same-body"

    # 3) 同正文同 hash 同 activation_id 下仅提升 strike_epoch，快照与绑定必须刷新。
    reg = store.load_registry()
    rec = reg.skills["cycle-skill"]
    _save_registry(store,
        replace(
            reg,
            skills={
                **reg.skills,
                "cycle-skill": replace(
                    rec,
                    maintenance={**rec.maintenance, "strike_epoch": 2},
                ),
            },
        )
    )
    epoch_snap = svc.snapshot_for()
    assert epoch_snap is not reheated_snap
    entry_epoch = next(e for e in epoch_snap.entries if e.name == "cycle-skill")
    assert entry_epoch.asset_fields["_learning_usage_binding"]["strike_epoch"] == 2

    # 4) update -> disable -> enable -> archive -> restore -> rollback 全链路均即时生效。
    _update(store, "cycle-skill", "Body version two", expected_revision=rec.revision)
    upd_snap = svc.snapshot_for()
    upd_entry = next(e for e in upd_snap.entries if e.name == "cycle-skill")
    assert upd_entry.frozen_path.endswith("v2.md")
    assert upd_entry.content_sha256 != entry_v1.content_sha256

    control_learning(store, "cycle-skill", operation="disable")
    dis_snap = svc.snapshot_for()
    dis_entry = next(e for e in dis_snap.entries if e.name == "cycle-skill")
    assert dis_entry.enabled is False and dis_entry.recommended is False

    control_learning(store, "cycle-skill", operation="enable")
    ena_snap = svc.snapshot_for()
    assert next(e for e in ena_snap.entries if e.name == "cycle-skill").enabled is True

    # archive -> restore
    with store.state_lock():
        store._transition_archive_locked(store.load_registry(), "cycle-skill", now=1700001000.0)
    arch_snap = svc.snapshot_for()
    assert all(e.name != "cycle-skill" for e in arch_snap.entries)

    control_learning(store, "cycle-skill", operation="restore", now=1700001100.0)
    restored_snap = svc.snapshot_for()
    assert next(e for e in restored_snap.entries if e.name == "cycle-skill").enabled is True

    # rollback (revert)
    reg_before_rev = store.load_registry()
    rec_before_rev = reg_before_rev.skills["cycle-skill"]
    # revert 需要前序已证实版本元数据（established_at）或回滚前稳定记录；这里补全 previous_stable 以验证真实 revert_learned_skill。
    _save_registry(store,
        replace(
            reg_before_rev,
            skills={
                **reg_before_rev.skills,
                "cycle-skill": replace(
                    rec_before_rev,
                    maintenance={
                        **rec_before_rev.maintenance,
                        "previous_stable": {
                            "version": 1,
                            "revision": 1,
                            "sha256": entry_v1.content_sha256,
                            "established_at": 1700000100.0,
                        },
                    },
                ),
            },
        )
    )
    revert_learned_skill(store, "cycle-skill")
    rev_snap = svc.snapshot_for()
    rev_entry = next(e for e in rev_snap.entries if e.name == "cycle-skill")
    assert rev_entry.content_sha256 == entry_v1.content_sha256


def test_owner_invalidation_keeps_other_owner_builtin_and_shared_caches_intact(tmp_path: Path):
    base = home_paths(tmp_path / "data")
    hp_a = home_paths_with_owner(base, ensure_owner_home(base.data_dir, OwnerIdentity.provider_user("test", "owner-a")))
    hp_b = home_paths_with_owner(base, ensure_owner_home(base.data_dir, OwnerIdentity.provider_user("test", "owner-b")))
    for hp in (hp_a, hp_b):
        (Path(hp.owner_home_dir) / "skills").mkdir(parents=True, exist_ok=True)
    Path(base.shared_skills_dir).mkdir(parents=True, exist_ok=True)
    Path(base.shared_builtin_dir).mkdir(parents=True, exist_ok=True)

    _write_skill_dir(Path(base.shared_builtin_dir), "core-builtin", "Core builtin", "Builtin instructions")
    _write_skill_dir(Path(base.shared_skills_dir), "shared-helper", "Shared helper", "Shared instructions")

    svc_a = _make_service(hp_a, tmp_path / "ws-a")
    svc_b = _make_service(hp_b, tmp_path / "ws-b")

    snap_a1 = svc_a.snapshot_for()
    snap_b1 = svc_b.snapshot_for()

    with skill_service._PROCESS_SKILL_CACHE_LOCK:
        builtin_keys_before = dict(skill_service._PROCESS_BUILTIN_ROOT_CACHE)
        dir_items_before = {k: id(v) for k, v in skill_service._PROCESS_SKILL_DIR_CACHE.items()}

    assert builtin_keys_before
    assert dir_items_before

    # Owner A 发布新的自学技能触发 learning_state 失效。
    read_calls: list[Path] = []
    orig_read_item = skill_service._read_manifest_item

    def _spy_read_item(root, resolved):
        read_calls.append(resolved)
        return orig_read_item(root, resolved)

    orig_impl = skill_service._read_manifest_item
    try:
        skill_service._read_manifest_item = _spy_read_item
        store_a = SkillLearningStore.for_home(hp_a)
        _publish(store_a, "owner-a-only", "Owner A learned body")
        snap_a2 = svc_a.snapshot_for()
        assert snap_a2 is not snap_a1
        assert any(e.name == "owner-a-only" for e in snap_a2.entries)

        # Owner B 的快照保持原对象身份，且 Owner A 的刷新未重读 builtin/shared 目录正文。
        snap_b2 = svc_b.snapshot_for()
        assert snap_b2 is snap_b1
    finally:
        skill_service._read_manifest_item = orig_impl

    assert all(
        not p.is_relative_to(Path(base.shared_builtin_dir))
        and not p.is_relative_to(Path(base.shared_skills_dir))
        for p in read_calls
    )

    with skill_service._PROCESS_SKILL_CACHE_LOCK:
        builtin_keys_after = dict(skill_service._PROCESS_BUILTIN_ROOT_CACHE)
        dir_items_after = {k: id(v) for k, v in skill_service._PROCESS_SKILL_DIR_CACHE.items()}

    # 进程级 builtin 与 shared 缓存未被 Owner A 的学习状态变更连坐清空。
    assert set(builtin_keys_before).issubset(set(builtin_keys_after))
    for k, old_state in builtin_keys_before.items():
        assert builtin_keys_after[k].digest == old_state.digest
    for k, obj_id in dir_items_before.items():
        assert dir_items_after.get(k) == obj_id

    # 显式 clear_cache / force_reload 仍会清理公开缓存并刷新。
    svc_a.clear_cache()
    svc_a.snapshot_for()
    with skill_service._PROCESS_SKILL_CACHE_LOCK:
        dir_items_forced = {k: id(v) for k, v in skill_service._PROCESS_SKILL_DIR_CACHE.items()}
    assert dir_items_forced != dir_items_before


def test_stat_memo_bounded_lru_no_change_cost_and_corrupt_recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(skill_learning_versions.cache_capacity, "resident_owners", lambda floor=0: 2)

    owners = []
    for idx in range(3):
        hp = _make_owner_home(tmp_path / f"owner-{idx}", f"u{idx}")
        store = SkillLearningStore.for_home(hp)
        _publish(store, f"skill-{idx}", f"Body {idx}")
        owners.append((hp, store))

    for hp, _store in owners:
        assert skill_learning_versions.learning_state_fingerprint(hp)

    # 有界 LRU 容量为 2，第 3 个 owner 插入后淘汰最旧的 owner-0。
    assert len(skill_learning_versions._LEARNING_STATE_MEMO) == 2
    memo_keys = {k[0] for k in skill_learning_versions._LEARNING_STATE_MEMO}
    assert owners[0][1].directory.expanduser().resolve(strict=False) not in memo_keys

    # 无变化回合只做 stat 检查，不重读正文也不重新解析 registry。
    hp2, store2 = owners[2]
    svc2 = _make_service(hp2, tmp_path / "ws2")
    snap_cold = svc2.snapshot_for()
    monkeypatch.setattr(SkillLearningStore, "load_registry", lambda self: pytest.fail("unexpected registry parse"))
    monkeypatch.setattr(skill_learning_versions, "read_text_beneath", lambda *a, **kw: pytest.fail("unexpected body read"))
    snap_hot = svc2.snapshot_for()
    assert snap_hot is snap_cold

    # 恢复标准方法后测试损坏 registry.json：绝不长期缓存不可用状态，修复后立即可恢复。
    monkeypatch.undo()
    monkeypatch.setattr(skill_learning_versions.cache_capacity, "resident_owners", lambda floor=0: 2)
    raw_valid = store2.registry_path.read_text(encoding="utf-8")
    store2.registry_path.write_text("{invalid json", encoding="utf-8")

    assert skill_learning_versions.learning_state_fingerprint(hp2) == "learning-state-unavailable"
    corrupt_snap = svc2.snapshot_for()
    assert all(e.name != "skill-2" for e in corrupt_snap.entries)

    store2.registry_path.write_text(raw_valid, encoding="utf-8")
    assert skill_learning_versions.learning_state_fingerprint(hp2) not in {"", "learning-state-unavailable"}
    recovered_snap = svc2.snapshot_for()
    recovered_entry = next(e for e in recovered_snap.entries if e.name == "skill-2")
    assert recovered_entry.frozen_path

    # 不修改 registry.json，仅移除再恢复版本正文 v1.md，stat memo 也应实时失效与自愈。
    ver_file = store2.versions_dir / "skill-2" / "v1.md"
    ver_body = ver_file.read_text(encoding="utf-8")
    ver_file.unlink()
    unfrozen_snap = svc2.snapshot_for()
    unfrozen_entry = next(e for e in unfrozen_snap.entries if e.name == "skill-2")
    assert not unfrozen_entry.frozen_path
    assert "_learning_usage_binding" not in unfrozen_entry.asset_fields

    ver_file.write_text(ver_body, encoding="utf-8")
    repaired_snap = svc2.snapshot_for()
    repaired_entry = next(e for e in repaired_snap.entries if e.name == "skill-2")
    assert repaired_entry.frozen_path
    assert "_learning_usage_binding" in repaired_entry.asset_fields


def test_binding_privacy_absent_ids_historical_restricted_exempt_and_turn_omission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    hp = _make_owner_home(tmp_path)
    store = SkillLearningStore.for_home(hp)
    _publish(store, "norm-skill", "Normal body v1")
    norm_rec1 = store.load_registry().skills["norm-skill"]
    v1_sha = norm_rec1.sha256
    _update(store, "norm-skill", "Normal body v2", expected_revision=norm_rec1.revision)

    _publish(store, "cooled-skill", "Cooled body")
    _publish(store, "no-id-skill", "No id body")
    _publish(store, "exempt-owner-skill", "Owner layout body")
    _publish(store, "user-edited-skill", "User edited original")

    reg = store.load_registry()
    # 设置 cooled 状态
    cooled = reg.skills["cooled-skill"]
    cooled = replace(cooled, maintenance={**cooled.maintenance, "state": "cooled"})
    # 清除持久化 ID，校验读取时不会现场伪造 UUID
    noid = reg.skills["no-id-skill"]
    noid_maint = {k: v for k, v in noid.maintenance.items() if k not in {"incarnation_id", "activation_id", "strike_epoch"}}
    noid = replace(noid, maintenance=noid_maint)
    # 迁移为 owner_layout 并移至 skills/<name>/SKILL.md
    owner_rec = reg.skills["exempt-owner-skill"]
    owner_target = store.skills_root / "exempt-owner-skill" / "SKILL.md"
    owner_target.parent.mkdir(parents=True, exist_ok=True)
    old_owner_learned = store.learned_root / "exempt-owner-skill" / "SKILL.md"
    owner_target.write_text(old_owner_learned.read_text(encoding="utf-8"), encoding="utf-8")
    old_owner_learned.unlink()
    owner_rec = replace(owner_rec, layout="owner", maintenance={**owner_rec.maintenance, "ownership": "owner_layout"})
    # 用户手工接管修改正文，豁免于自动学习生命周期
    edited_file = store.learned_root / "user-edited-skill" / "SKILL.md"
    edited_file.write_text(edited_file.read_text(encoding="utf-8") + "\n# User edit\n", encoding="utf-8")

    _save_registry(store,
        replace(
            reg,
            skills={
                **reg.skills,
                "cooled-skill": cooled,
                "no-id-skill": noid,
                "exempt-owner-skill": owner_rec,
            },
        )
    )

    svc = _make_service(hp, tmp_path / "ws")
    snap = svc.snapshot_for()
    by_name = {e.name: e for e in snap.entries}

    # 1) 普通冻结条目：私有 4 字段绑定存在，公开 to_dict/to_ref 不泄露。
    norm = by_name["norm-skill"]
    assert "_learning_usage_binding" in norm.asset_fields
    binding = norm.asset_fields["_learning_usage_binding"]
    assert set(binding) == {"incarnation_id", "activation_id", "content_sha256", "strike_epoch"}
    assert "_learning_usage_binding" not in norm.to_dict()
    assert "_learning_usage_binding" not in norm.to_dict(include_path=True)
    assert "_learning_usage_binding" not in norm.to_ref()

    # 2) 缺失持久化 ID 不在读侧伪造，保持缺失。
    no_id_entry = by_name["no-id-skill"]
    assert no_id_entry.frozen_path
    assert "_learning_usage_binding" not in no_id_entry.asset_fields

    # 3) 历史版本 restricted 与 resolve_learned_revision 清除 asset_fields，不泄漏当前激活绑定。
    hist_resolved = skill_learning_versions.resolve_learned_revision(norm, v1_sha)
    assert hist_resolved is not None and "_learning_usage_binding" not in hist_resolved.asset_fields
    restricted = snap.restricted([norm.stable_id], expected_sha256={norm.stable_id: v1_sha})
    assert restricted.entries[0].content_sha256 == v1_sha
    assert "_learning_usage_binding" not in restricted.entries[0].asset_fields

    # 4) 豁免条目（layout=owner 或用户改写正文）无 frozen_path 与绑定，保留普通推荐身份。
    exempt_owner = by_name["exempt-owner-skill"]
    user_edited = by_name["user-edited-skill"]
    assert not exempt_owner.frozen_path and "_learning_usage_binding" not in exempt_owner.asset_fields
    assert not user_edited.frozen_path and "_learning_usage_binding" not in user_edited.asset_fields

    # 5) cooled 条目仍可按技能检索命中（enabled=True），但 recommended=False，从 turn_recommendations 省略。
    cooled_entry = by_name["cooled-skill"]
    assert cooled_entry.enabled is True and cooled_entry.recommended is False

    from agent_py_agent.agent.assets import monitor_adapter, workspace_assets

    monkeypatch.setattr(
        workspace_assets.WorkspaceAssetOverlay,
        "list_assets",
        lambda *args: [{"id": "s1", "kind": "script", "name": "entry", "enabled": True, "metadata": {}}],
    )
    monkeypatch.setattr(monitor_adapter, "list_monitor_descriptors", lambda *args: [])
    runtime = SimpleNamespace(availability=SimpleNamespace(available=True), exposure=SimpleNamespace(model_visible=True))
    runtime_snap = SimpleNamespace(runtime=lambda name: runtime)
    fake_agent = SimpleNamespace(
        home_paths=hp,
        tools=SimpleNamespace(workspace_root=tmp_path / "ws"),
        current_skill_snapshot=lambda: snap,
    )
    rows, _monitors, _home = turn_recommendations.inventory_for_agent(
        fake_agent,
        runtime_snapshot=runtime_snap,
        allowed_tools={"scripts", "skill_search"},
    )
    recommended_names = {r["name"] for r in rows}
    assert "norm-skill" in recommended_names
    assert "cooled-skill" not in recommended_names
    assert "exempt-owner-skill" in recommended_names

    # 6) learned_metadata_batch：一次 registry 加载、canonical skill_path、拒绝归档/用户改写且不读正文。
    load_count = []
    orig_load = SkillLearningStore.load_registry

    def _spy_load(self):
        load_count.append(1)
        return orig_load(self)

    monkeypatch.setattr(SkillLearningStore, "load_registry", _spy_load)
    monkeypatch.setattr(skill_learning_versions, "read_text_beneath", lambda *a, **k: pytest.fail("metadata batch must not read body"))
    batch = skill_learning_versions.frozen_learned_metadata_batch([norm, cooled_entry, no_id_entry])
    assert len(load_count) == 1
    assert batch[norm.stable_id]["authorized"] is True
    assert batch[cooled_entry.stable_id]["authorized"] is True
    assert batch[no_id_entry.stable_id]["authorized"] is True

    # 标记 user_modified 后 metadata projection 拒绝对冻结旧版本继续授权。
    reg_um = orig_load(store)
    _save_registry(
        store,
        replace(
            reg_um,
            skills={
                **reg_um.skills,
                "norm-skill": replace(
                    reg_um.skills["norm-skill"],
                    maintenance={**reg_um.skills["norm-skill"].maintenance, "ownership": "user_modified"},
                ),
            },
        ),
    )
    batch_after = learned_metadata_projection.learned_metadata_batch([norm])
    assert batch_after[norm.stable_id]["authorized"] is False


def test_single_arg_learning_state_fingerprint_monkeypatch_and_force_reload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    hp = _make_owner_home(tmp_path)
    store = SkillLearningStore.for_home(hp)
    _publish(store, "alpha-skill", "Alpha body")

    gen = ["g1"]
    monkeypatch.setattr(skill_learning_versions, "learning_state_fingerprint", lambda h: gen[0])
    svc = _make_service(hp, tmp_path / "ws")

    s1 = svc.snapshot_for()
    assert svc.snapshot_for() is s1

    gen[0] = "g2"
    s2 = svc.snapshot_for()
    assert s2 is not s1

    s3 = svc.snapshot_for(force_reload=True)
    assert s3 is not s2
    assert isinstance(s3, SkillSnapshot)
    assert all(isinstance(e, SkillSnapshotEntry) for e in s3.entries)
