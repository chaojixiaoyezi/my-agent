"""W2 合同测试：版本绑定精确使用回执、通道 C 归因、V2-2 任务级去重、3/4/5 阶梯升降级与回滚/退役。"""
from __future__ import annotations

import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.method_carry import _MethodCarryRequest, _record_carried_usage
from agent_py_agent.agent.capability.pack_verifier_runner import PackVerificationResult
from agent_py_agent.agent.capability.skill_learning_host_feedback import observe_pack_verification
from agent_py_agent.agent.capability.skill_learning_maintenance import (
    LearningObservationSource,
    maintain_learning,
    observe_learning,
)
from agent_py_agent.agent.capability.skill_learning_publish import (
    PublishRequest,
    SkillLearningGateError,
    publish_learned_skill,
)
from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
from agent_py_agent.agent.capability.skill_usage_ledger import (
    learning_usage_binding,
    load_usage_evidence,
    record_skill_usage_event,
)
from agent_py_agent.agent.capability.skill_usage_receipts import (
    canonical_negative_key,
    canonical_task_key,
    has_body_delivery_receipt,
    read_usage_aggregate,
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


def _incarnation(rec) -> str:
    return str(rec.maintenance.get("incarnation_id") or "")


def _activation(rec) -> str:
    return str(rec.maintenance.get("activation_id") or "")


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


def _source(
    rec,
    *,
    authority: str = "model",
    receipt_id: str = "rcpt-1",
    actor_id: str = "",
    run_id: str = "",
) -> LearningObservationSource:
    resolved_actor = actor_id or ("actor-1" if authority == "human" else "")
    return LearningObservationSource(
        authority=authority,
        receipt_id=receipt_id,
        actor_id=resolved_actor,
        run_id=run_id,
        incarnation_id=_incarnation(rec),
        activation_id=_activation(rec),
        strike_epoch=int(rec.maintenance.get("strike_epoch", 0) or 0),
    )


def _record_real_use(store: SkillLearningStore, rec, *, task_id: str, event: str = "manual_get", epoch: int | None = None):
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
        incarnation_id=_incarnation(rec),
        activation_id=_activation(rec),
        strike_epoch=strike_epoch,
    )


def test_w2_v2_2_task_key_priority_and_cross_channel_same_task_dedup(tmp_path):
    """V2-2: root_task_id -> task_id -> request_id -> receipt_id 优先级与 A+B+C 同任务去重 <= 1 次打击。"""
    home, repo, store = _store_with_runtime(tmp_path)
    _publish(store, "alpha")
    rec = store.load_registry().skills["alpha"]

    assert canonical_negative_key(
        root_task_id="root-1",
        task_id="sub-1",
        request_id="req-1",
        receipt_id="rcpt-1",
    ) == "task:root-1"
    assert canonical_negative_key(task_id="sub-1", request_id="req-1", receipt_id="rcpt-1") == "task:sub-1"
    assert canonical_negative_key(request_id="req-1", receipt_id="rcpt-1") == "request:req-1"
    assert canonical_negative_key(receipt_id="rcpt-1") == "receipt:rcpt-1"
    assert canonical_task_key(task_id="task-9", request_id="req-9") == "task:task-9"
    assert canonical_task_key(request_id="req-9") == "request:req-9"

    # 同 root_task_id 下多次低层 record_exact_negative 只算 1 次。
    r1 = record_exact_negative(
        store,
        incarnation_id=_incarnation(rec),
        activation_id=_activation(rec),
        content_sha256=rec.sha256,
        root_task_id="root-shared",
        task_id="child-a",
        request_id="req-a",
        receipt_id="rcpt-a",
        runtime=repo,
    )
    r2 = record_exact_negative(
        store,
        incarnation_id=_incarnation(rec),
        activation_id=_activation(rec),
        content_sha256=rec.sha256,
        root_task_id="root-shared",
        task_id="child-b",
        request_id="req-b",
        receipt_id="rcpt-b",
        runtime=repo,
    )
    assert r1["status"] == "accepted" and r1["hurt_count"] == 1
    assert r2["status"] == "duplicate" and r2["hurt_count"] == 1

    # 新建第二个技能 beta：同 task_id 下先后触发通道 B (host/self_report)、通道 A (human) 与通道 C (pack failed)，合起来仅记 1 次。
    _publish(store, "beta")
    beta = store.load_registry().skills["beta"]
    record_skill_usage_event(
        store,
        event="manual_get",
        skill_id="owner:beta",
        source="learned",
        kind="skill",
        task_id="same-task-1",
        chars=200,
        body_chars=200,
        content_sha256=beta.sha256,
        incarnation_id=_incarnation(beta),
        activation_id=_activation(beta),
        strike_epoch=0,
    )
    # Channel B (host / structured self_report negative on same task)
    observe_learning(
        store,
        "beta",
        sha256=beta.sha256,
        task_id="same-task-1",
        outcome="ineffective",
        source=_source(beta, authority="host", receipt_id="chan-b"),
    )
    # Channel A (human) on same task
    observe_learning(
        store,
        "beta",
        sha256=beta.sha256,
        task_id="same-task-1",
        outcome="ineffective",
        source=_source(beta, authority="human", receipt_id="chan-a"),
    )
    # Channel C (deterministic verifier failure) on same task
    agent = _agent_for(home, repo, "same-task-1", task_id="same-task-1")
    observe_pack_verification(
        agent,
        agent._current_run_params,
        PackVerificationResult(verifier_id="pytest", package_id="pkg-beta", status="failed", valid=False),
        "chan-c",
    )
    beta_after = store.load_registry().skills["beta"]
    assert beta_after.maintenance["hurt_count"] == 1
    assert beta_after.maintenance["strike_epoch"] == 1


def test_w2_v2_2_channel_c_body_delivery_attribution_and_transient_filter(tmp_path):
    """V2-2 通道 C：仅凭正文送达回执（无自愿 adopted）归因确定性校验失败，忽略环境/瞬时失败，且忽略 body_chars=0。"""
    home, repo, store = _store_with_runtime(tmp_path)
    _publish(store, "gamma")
    rec = store.load_registry().skills["gamma"]

    # 1) body_chars == 0 仅引用注入：不得建立 SQLite 送达回执，通道 C 不归因。
    record_skill_usage_event(
        store,
        event="host_injected",
        skill_id="owner:gamma",
        source="learned",
        kind="skill",
        task_id="t-ref-only",
        chars=60,
        body_chars=0,
        content_sha256=rec.sha256,
        incarnation_id=_incarnation(rec),
        activation_id=_activation(rec),
        strike_epoch=0,
    )
    assert not has_body_delivery_receipt(
        repo,
        incarnation_id=_incarnation(rec),
        activation_id=_activation(rec),
        content_sha256=rec.sha256,
        task_id="t-ref-only",
    )
    ref_agent = _agent_for(home, repo, "t-ref-only", task_id="t-ref-only")
    observe_pack_verification(
        ref_agent,
        ref_agent._current_run_params,
        PackVerificationResult(verifier_id="pytest", package_id="pkg", status="failed", valid=False),
        "ref-only-k",
    )
    assert store.load_registry().skills["gamma"].maintenance.get("hurt_count", 0) == 0

    # 2) host_injected 正文实际投递（无 adopted）：瞬时/环境错误忽略，确定性失败归因 1 次打击。
    record_skill_usage_event(
        store,
        event="host_injected",
        skill_id="owner:gamma",
        source="learned",
        kind="skill",
        task_id="t-body-1",
        chars=220,
        body_chars=220,
        content_sha256=rec.sha256,
        incarnation_id=_incarnation(rec),
        activation_id=_activation(rec),
        strike_epoch=0,
    )
    assert has_body_delivery_receipt(
        repo,
        incarnation_id=_incarnation(rec),
        activation_id=_activation(rec),
        content_sha256=rec.sha256,
        task_id="t-body-1",
    )
    body_agent = _agent_for(home, repo, "t-body-1", task_id="t-body-1")
    for reason in ("network_error", "timeout", "provider_unavailable", "rate_limit"):
        observe_pack_verification(
            body_agent,
            body_agent._current_run_params,
            PackVerificationResult(
                verifier_id="pytest",
                package_id="pkg",
                status="failed",
                valid=False,
                reason_code=reason,
            ),
            f"env-{reason}",
        )
    assert store.load_registry().skills["gamma"].maintenance.get("hurt_count", 0) == 0

    observe_pack_verification(
        body_agent,
        body_agent._current_run_params,
        PackVerificationResult(
            verifier_id="pytest",
            package_id="pkg",
            status="failed",
            valid=False,
            reason_code="assertion_failed",
        ),
        "det-fail-1",
    )
    assert store.load_registry().skills["gamma"].maintenance["hurt_count"] == 1

    # 3) manual_get (skill_get) 正文投递（无 adopted）在 request_id 维度同样可归因（即使外层 params 带不同 task_id）。
    record_skill_usage_event(
        store,
        event="manual_get",
        skill_id="owner:gamma",
        source="learned",
        kind="skill",
        request_id="req-body-2",
        chars=220,
        body_chars=220,
        content_sha256=rec.sha256,
        incarnation_id=_incarnation(rec),
        activation_id=_activation(rec),
        strike_epoch=1,
    )
    req_agent = _agent_for(home, repo, "run-2", task_id="task-other", request_id="req-body-2")
    observe_pack_verification(
        req_agent,
        req_agent._current_run_params,
        PackVerificationResult(verifier_id="pytest", package_id="pkg", status="failed", valid=False),
        "det-fail-2",
    )
    assert store.load_registry().skills["gamma"].maintenance["hurt_count"] == 2


def test_w2_3_4_5_strike_ladder_and_late_epoch_filtering(tmp_path):
    """3/4/5 阶梯晋升：3 次确立；第 1 次负向后需 4 次；第 2 次负向后需 5 次；旧 strike_epoch 迟到使用不进新 epoch 计数。"""
    _, repo, store = _store_with_runtime(tmp_path)

    # A. 无伤 3 次真实独立使用直接确立（established + active）。
    _publish(store, "clean3")
    clean = store.load_registry().skills["clean3"]
    for idx in range(2):
        _record_real_use(store, clean, task_id=f"c-{idx}")
    assert store.load_registry().skills["clean3"].maintenance["quality"] == "trial"
    _record_real_use(store, clean, task_id="c-2")
    clean_done = store.load_registry().skills["clean3"]
    assert clean_done.maintenance["quality"] == "established"
    assert clean_done.maintenance["state"] == "active"
    assert clean_done.maintenance["uses_since_hurt"] == 3

    # B. 第 1 次负向 -> 需 4 次；第 2 次负向 -> 需 5 次；正向不降 hurt_count。
    _publish(store, "ladder")
    lad = store.load_registry().skills["ladder"]
    _record_real_use(store, lad, task_id="u0-1")
    observe_learning(
        store,
        "ladder",
        sha256=lad.sha256,
        task_id="hurt-1",
        outcome="adopted",
        source=_source(lad, authority="model", receipt_id="adopt-h1"),
    )
    observe_learning(
        store,
        "ladder",
        sha256=lad.sha256,
        task_id="hurt-1",
        outcome="ineffective",
        source=_source(lad, authority="human", receipt_id="r-h1"),
    )
    lad = store.load_registry().skills["ladder"]
    assert lad.maintenance["hurt_count"] == 1
    assert lad.maintenance["strike_epoch"] == 1
    assert lad.maintenance["uses_since_hurt"] == 0

    # 迟到的 epoch=0 使用只能增 lifetime independent_uses，不得计入 epoch=1 的 uses_since_hurt。
    _record_real_use(store, lad, task_id="late-epoch0", epoch=0)
    lad = store.load_registry().skills["ladder"]
    assert lad.maintenance["uses_since_hurt"] == 0
    assert lad.maintenance["independent_uses"] == 2

    # 正向 observe_learning 不减少 hurt_count。
    observe_learning(
        store,
        "ladder",
        sha256=lad.sha256,
        task_id="pos-1",
        outcome="adopted",
        source=_source(lad, authority="model", receipt_id="adopt-p1"),
    )
    observe_learning(
        store,
        "ladder",
        sha256=lad.sha256,
        task_id="pos-1",
        outcome="effective",
        source=_source(lad, authority="human", receipt_id="r-p1"),
    )
    assert store.load_registry().skills["ladder"].maintenance["hurt_count"] == 1

    # 3 次新使用仍保持 trial，第 4 次才晋升 established。
    for idx in range(3):
        _record_real_use(store, lad, task_id=f"u1-{idx}", epoch=1)
    assert store.load_registry().skills["ladder"].maintenance["quality"] == "trial"
    _record_real_use(store, lad, task_id="u1-3", epoch=1)
    lad_est = store.load_registry().skills["ladder"]
    assert lad_est.maintenance["quality"] == "established"
    assert lad_est.maintenance["state"] == "active"
    assert lad_est.maintenance["uses_since_hurt"] == 4

    # 第 2 次负向后需 5 次新独立使用才重新确立。
    observe_learning(
        store,
        "ladder",
        sha256=lad_est.sha256,
        task_id="hurt-2",
        outcome="adopted",
        source=_source(lad_est, authority="model", receipt_id="adopt-h2"),
    )
    observe_learning(
        store,
        "ladder",
        sha256=lad_est.sha256,
        task_id="hurt-2",
        outcome="ineffective",
        source=_source(lad_est, authority="human", receipt_id="r-h2"),
    )
    lad2 = store.load_registry().skills["ladder"]
    assert lad2.maintenance["hurt_count"] == 2
    assert lad2.maintenance["strike_epoch"] == 2
    assert lad2.maintenance["uses_since_hurt"] == 0
    for idx in range(4):
        _record_real_use(store, lad2, task_id=f"u2-{idx}", epoch=2)
    assert store.load_registry().skills["ladder"].maintenance["quality"] == "trial"
    _record_real_use(store, lad2, task_id="u2-4", epoch=2)
    lad_final = store.load_registry().skills["ladder"]
    assert lad_final.maintenance["quality"] == "established"
    assert lad_final.maintenance["uses_since_hurt"] == 5

    agg = read_usage_aggregate(
        repo,
        incarnation_id=_incarnation(lad_final),
        activation_id=_activation(lad_final),
        content_sha256=lad_final.sha256,
    )
    assert agg is not None and agg["hurt_count"] == 2 and agg["strike_epoch"] == 2 and agg["uses_since_hurt"] == 5


def test_w2_third_strike_rollback_or_retire_and_crash_recovery(tmp_path):
    """第 3 次打击：有 proven previous_stable 时回滚并保留 historical_adoptions；无 stable 时退役阻断；崩溃重放恢复一致。"""
    _, repo, store = _store_with_runtime(tmp_path)

    # Case 1: 有已确立 v1（含 observations/historical_adoptions）后再升级 v2 进入 trial，v2 第 3 次负向回滚回 v1。
    _publish(store, "rollback-sk", suffix="v1")
    v1_rec = store.load_registry().skills["rollback-sk"]
    for idx in range(3):
        _record_real_use(store, v1_rec, task_id=f"rb-use-{idx}")
    v1_est = store.load_registry().skills["rollback-sk"]
    assert v1_est.maintenance["quality"] == "established"
    observe_learning(
        store,
        "rollback-sk",
        sha256=v1_est.sha256,
        task_id="rb-v1-adopt",
        outcome="adopted",
        source=_source(v1_est, authority="model", receipt_id="rcpt-preserve-1", run_id="run-preserve-1"),
    )

    v1_for_update = store.load_registry().skills["rollback-sk"]
    _publish(store, "rollback-sk", suffix="v2", update_from=v1_for_update)
    v2_rec = store.load_registry().skills["rollback-sk"]
    assert v2_rec.maintenance.get("previous_stable") is not None
    assert v2_rec.sha256 != v1_est.sha256
    for idx in range(3):
        observe_learning(
            store,
            "rollback-sk",
            sha256=v2_rec.sha256,
            task_id=f"rb-hurt-{idx}",
            outcome="adopted",
            source=_source(v2_rec, authority="model", receipt_id=f"rb-adopt-{idx}"),
        )
        observe_learning(
            store,
            "rollback-sk",
            sha256=v2_rec.sha256,
            task_id=f"rb-hurt-{idx}",
            outcome="ineffective",
            source=_source(v2_rec, authority="human", receipt_id=f"rb-r-{idx}"),
        )
    rolled = store.load_registry().skills["rollback-sk"]
    assert rolled.sha256 == v1_est.sha256
    assert _activation(rolled) != _activation(v1_rec)
    assert _activation(rolled) != _activation(v2_rec)
    assert not rolled.maintenance.get("previous_stable")
    assert rolled.maintenance["quality"] == "established"
    assert rolled.maintenance["quality_basis"] == "rollback_established"
    preserved_runs = {str(item.get("run_id") or "") for item in rolled.maintenance.get("historical_adoptions", [])}
    assert "run-preserve-1" in preserved_runs
    assert "v1" in (store.learned_root / "rollback-sk" / "SKILL.md").read_text(encoding="utf-8")

    # Case 2: 无 proven previous_stable 的 trial 技能第 3 次负向直接退役并拉黑名称与任务族；mode='observe' 不屏蔽人工负向。
    _publish(store, "retire-sk", suffix="v1")
    ret_rec = store.load_registry().skills["retire-sk"]
    assert maintain_learning(store, mode="observe", cool_seconds=1, now=time.time() + 100)["mode"] == "observe"
    for idx in range(3):
        observe_learning(
            store,
            "retire-sk",
            sha256=ret_rec.sha256,
            task_id=f"ret-hurt-{idx}",
            outcome="adopted",
            source=_source(ret_rec, authority="model", receipt_id=f"ret-adopt-{idx}"),
        )
        observe_learning(
            store,
            "retire-sk",
            sha256=ret_rec.sha256,
            task_id=f"ret-hurt-{idx}",
            outcome="ineffective",
            source=_source(ret_rec, authority="human", receipt_id=f"ret-r-{idx}"),
        )
    reg_after = store.load_registry()
    assert "retire-sk" not in reg_after.skills
    assert "retire-sk" in reg_after.blocked_names
    assert reg_after.blocked_task_families
    with pytest.raises(SkillLearningGateError) as fam_exc:
        _publish(store, "same-family-blocked")
    assert fam_exc.value.code == "SKILL_LEARNING_FAMILY_BLOCKED"

    # Case 3a: 崩溃前 SQLite 已记满 hurt_count==3 但 registry 尚未退役 -> observe_learning 重放直接收口，不需第 4 次打击。
    _, repo_obs, store_obs = _store_with_runtime(tmp_path / "crash_obs")
    _publish(store_obs, "crash-obs")
    c_obs = store_obs.load_registry().skills["crash-obs"]
    for idx in range(3):
        record_exact_negative(
            store_obs,
            incarnation_id=_incarnation(c_obs),
            activation_id=_activation(c_obs),
            content_sha256=c_obs.sha256,
            task_id=f"crash-obs-{idx}",
            runtime=repo_obs,
        )
    observe_learning(
        store_obs,
        "crash-obs",
        sha256=c_obs.sha256,
        task_id="crash-obs-2",
        outcome="ineffective",
        source=_source(c_obs, authority="host", receipt_id="c-obs-replay"),
    )
    assert "crash-obs" not in store_obs.load_registry().skills
    assert "crash-obs" in store_obs.load_registry().blocked_names

    # Case 3b: maintain_learning 恢复阶段（recover）同样无需第 4 次打击即可收口 SQLite hurt_count==3。
    _, repo_m, store_m = _store_with_runtime(tmp_path / "crash_maint")
    _publish(store_m, "crash-maint")
    c_m = store_m.load_registry().skills["crash-maint"]
    for idx in range(3):
        record_exact_negative(
            store_m,
            incarnation_id=_incarnation(c_m),
            activation_id=_activation(c_m),
            content_sha256=c_m.sha256,
            task_id=f"crash-m-{idx}",
            runtime=repo_m,
        )
    report = maintain_learning(store_m, mode="autonomous", protection_complete=True, now=time.time())
    assert report["changed"] == ["crash-maint"]
    assert "crash-maint" not in store_m.load_registry().skills
    assert "crash-maint" in store_m.load_registry().blocked_names


def test_w2_conflict_rejection_unbound_legacy_and_real_body_delivery_gating(tmp_path):
    """版本冲突拒绝、未绑定旧记录保护、以及正文投递门控（冷/归档再热、method_carry 真实正文 vs 纯引用、4 轴覆盖）。"""
    home, repo, store = _store_with_runtime(tmp_path)
    _publish(store, "gating")
    rec = store.load_registry().skills["gating"]
    inc = _incarnation(rec)
    act = _activation(rec)

    # 版本/激活身份冲突必报 SKILL_LEARNING_REVISION_CONFLICT。
    with pytest.raises(SkillLearningGateError) as sha_exc:
        observe_learning(
            store,
            "gating",
            sha256="f" * 64,
            task_id="bad-sha",
            outcome="ineffective",
            source=_source(rec, authority="human", receipt_id="bad-1"),
        )
    assert sha_exc.value.code == "SKILL_LEARNING_REVISION_CONFLICT"

    with pytest.raises(SkillLearningGateError) as act_exc:
        observe_learning(
            store,
            "gating",
            sha256=rec.sha256,
            task_id="bad-act",
            outcome="ineffective",
            source=LearningObservationSource(
                authority="human",
                receipt_id="bad-2",
                actor_id="actor-1",
                incarnation_id=inc,
                activation_id="wrong-activation",
            ),
        )
    assert act_exc.value.code == "SKILL_LEARNING_REVISION_CONFLICT"

    # 非真实正文送达途径（manual_search / capability_package）均不得增加 SQLite 精确使用。
    record_skill_usage_event(
        store,
        event="manual_search",
        skill_id="owner:gating",
        source="learned",
        kind="skill",
        task_id="t-search",
        chars=80,
        body_chars=80,
        content_sha256=rec.sha256,
        incarnation_id=inc,
        activation_id=act,
        strike_epoch=0,
    )
    record_skill_usage_event(
        store,
        event="manual_get",
        skill_id="pkg:not-skill",
        source="package",
        kind="capability_package",
        task_id="t-pkg",
        chars=120,
        body_chars=120,
        content_sha256=rec.sha256,
        incarnation_id=inc,
        activation_id=act,
        strike_epoch=0,
    )
    assert (
        read_usage_aggregate(
            repo,
            incarnation_id=inc,
            activation_id=act,
            content_sha256=rec.sha256,
        )
        is None
    )

    # method_carry：纯引用兜底（body_chars=0 / "本会话用过的方法..."）不得触发使用计数，正文实际注入才附带 binding 并入账。
    skill_id = "owner:gating"
    entry = SimpleNamespace(
        stable_id=skill_id,
        name=rec.name,
        content_sha256=rec.sha256,
        metadata={
            "learning_record": {
                "incarnation_id": inc,
                "activation_id": act,
                "strike_epoch": 0,
            }
        },
    )
    agent = _agent_for(home, repo, "carry-run", task_id="carry-task-1", request_id="carry-req-1")
    binding = learning_usage_binding(agent, entry)
    assert binding["incarnation_id"] == inc
    assert binding["activation_id"] == act
    assert binding["content_sha256"] == rec.sha256
    assert binding["strike_epoch"] == 0

    req = _MethodCarryRequest(
        agent=agent,
        params=agent._current_run_params,
        thread_id="thread-1",
        generation=1,
        scope=SimpleNamespace(skills=SimpleNamespace(resolve=lambda _sid: entry)),
        authority=None,
        max_tokens=2000,
        delivery_meta={
            ("skill", skill_id): {"body_chars": 0},
        },
    )
    _record_carried_usage(req, [({"kind": "skill", "stable_id": skill_id}, "本会话用过的方法：gating")])
    assert not has_body_delivery_receipt(
        repo,
        incarnation_id=inc,
        activation_id=act,
        content_sha256=rec.sha256,
        task_id="carry-task-1",
    )

    # 先冷却该技能，再通过 method_carry 真实正文注入触发再热（cooled -> trial）并写入 SQLite 回执。
    with store.state_lock():
        reg = store.load_registry()
        cooled = replace(
            reg.skills["gating"],
            maintenance={**reg.skills["gating"].maintenance, "state": "cooled"},
        )
        store.save_registry(replace(reg, skills={**reg.skills, "gating": cooled}))

    req_body = _MethodCarryRequest(
        agent=agent,
        params=agent._current_run_params,
        thread_id="thread-1",
        generation=1,
        scope=SimpleNamespace(skills=SimpleNamespace(resolve=lambda _sid: entry)),
        authority=None,
        max_tokens=2000,
        delivery_meta={
            ("skill", skill_id): {"body_chars": 180},
        },
    )
    _record_carried_usage(req_body, [({"kind": "skill", "stable_id": skill_id}, f"{BODY}\n# carry body")])
    reheated = store.load_registry().skills["gating"]
    assert reheated.maintenance["state"] == "trial"
    assert has_body_delivery_receipt(
        repo,
        incarnation_id=inc,
        activation_id=act,
        content_sha256=rec.sha256,
        task_id="carry-task-1",
    )

    evidence = load_usage_evidence(store)
    assert set(evidence["coverage"]) == {
        "lifetime_summary",
        "delta_events",
        "version_attribution",
        "distinct_receipts",
    }
    assert evidence["by_skill"]["gating"]["activation_id"] == act
    assert evidence["by_skill"]["gating"]["independent_uses"] == 1

    # 未绑定历史技能（缺失 incarnation_id/activation_id）：exact use/negative 拒绝计入，且不自动晋升或三击退役。
    with store.state_lock():
        reg = store.load_registry()
        legacy_maint = dict(reg.skills["gating"].maintenance)
        legacy_maint["incarnation_id"] = ""
        legacy_maint["activation_id"] = ""
        legacy = replace(reg.skills["gating"], maintenance=legacy_maint)
        store.save_registry(replace(reg, skills={**reg.skills, "gating": legacy}))
    assert (
        record_exact_use(
            store,
            {
                "event": "manual_get",
                "kind": "skill",
                "body_chars": 100,
                "content_sha256": legacy.sha256,
                "task_id": "legacy-t",
                "incarnation_id": "",
                "activation_id": "",
            },
            runtime=repo,
        )["status"]
        == "ignored"
    )
    legacy_after = store.load_registry().skills["gating"]
    assert legacy_after.maintenance["quality"] == "trial"
    assert json.loads((store.directory / "usage_summary.json").read_text(encoding="utf-8"))["totals"]["effective_uses"] >= 1
