"""W2 合同测试：V2-3 活跃索引保护、80/20 探索推荐、7d/15d/15d 独立生命周期、人工控制与有界游标维护。"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.skill_learning_maintenance import (
    control_learning,
    learning_reconcile_due,
    maintain_learning,
    recommended_learning,
)
from agent_py_agent.agent.capability.skill_learning_proposals import propose_learning
from agent_py_agent.agent.capability.skill_learning_protection import learning_protection_for_agent
from agent_py_agent.agent.capability.skill_learning_publish import (
    PublishRequest,
    SkillLearningGateError,
    publish_learned_skill,
)
from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
from agent_py_agent.agent.capability.skill_usage_receipts import (
    aggregate_key,
    cleanup_retired_aggregates,
    has_body_delivery_receipt,
    negative_receipt_key,
    read_usage_aggregate,
    receipt_key,
    record_exact_negative,
    record_exact_use,
)
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import runtime_db_path
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.tests.test_skill_learning import BODY
from agent_py_agent.tests.test_wave7_autonomous_learning_closure import decision


def _store_with_runtime(tmp_path) -> tuple[object, RuntimeRepository, SkillLearningStore]:
    home = ensure_my_agent_home(tmp_path / "home")
    repo = RuntimeRepository(runtime_db_path(home.owner_home_dir))
    store = SkillLearningStore.for_home(home, usage_runtime_provider=lambda: repo)
    return home, repo, store


def _publish(store: SkillLearningStore, name: str, *, suffix: str = "v1", update_from=None):
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
    home, repo, store = _store_with_runtime(tmp_path)
    _publish(store, "prop-sk")
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

    _publish(store, "stale-if-complete")
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
    _, _, store = _store_with_runtime(tmp_path)
    for name in (
        "pin-sk",
        "fresh-old",
        "fresh-new",
        "unk-sk",
        "est-low",
        "est-high",
        "trial-used",
    ):
        _publish(store, name)

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
    _, _, store2 = _store_with_runtime(tmp_path / "only_fresh")
    for n in ("f1", "f2", "f3"):
        _publish(store2, n)
        _patch_maint(store2, n, ever_used=False)
    assert recommended_learning(store2.load_registry(), limit=2, explore_percent=20) == {"f2", "f3"}


def test_w2_independent_idle_lifecycle_observe_mode_and_control_actions(tmp_path):
    """7d/15d/15d 独立阶段与 0 禁用、idle 取 max(baseline,last_used,restored)、observe 只读、control_learning restore 与 cap 置换。"""
    _, _, store = _store_with_runtime(tmp_path)

    # 1. cool_seconds=0 时 archive_seconds 仍能独立生效，且驱逐不写入 blocked_names/blocked_task_families。
    _publish(store, "skip-cool")
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
    _publish(store, "live-victim")
    _patch_maint(store, "live-victim", baseline_at=100.0, modified_at=100.0, last_used_at=100.0)
    _publish(store, "to-restore")
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

    _, repo, store = _store_with_runtime(tmp_path)
    for name in ("a-corrupt", "b-stale", "c-active", "d-archived"):
        _publish(store, name)

    # 升级 b-stale 产生版本历史用于版本修剪，并为 active + retired 记录写入 SQLite 明细回执。
    b_v1 = store.load_registry().skills["b-stale"]
    b_v2_evt = _publish(store, "b-stale", suffix="v2", update_from=b_v1)
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
