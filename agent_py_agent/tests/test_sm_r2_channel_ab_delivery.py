from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.capability.pack_verifier_runner import PackVerificationResult
from agent_py_agent.agent.capability.skill_feedback_tool import SkillFeedbackTool
from agent_py_agent.agent.capability.skill_learning import (
    CODE_INEFFECTIVE_RECORDED,
    CODE_INEFFECTIVE_UNATTRIBUTED,
    SkillLearningRuntime,
    SkillLearningService,
    SkillLearningSettings,
)
from agent_py_agent.agent.capability.skill_learning_host_feedback import (
    observe_pack_verification,
)
from agent_py_agent.agent.capability.skill_learning_maintenance import (
    LearningObservationSource,
    observe_learning,
)
from agent_py_agent.agent.capability.skill_search_tool import SkillSearchTool
from agent_py_agent.agent.capability.skill_usage_ledger import record_skill_usage_event
from agent_py_agent.agent.capability.skill_usage_receipts import (
    has_body_delivery_receipt,
    read_usage_aggregate,
)
from agent_py_agent.tests.test_skill_learning import BODY, _FakeBackend
from agent_py_agent.tests.test_sm_w3_intake_runtime import _finalize_ctx
from agent_py_agent.tests.test_sm_w9_feedback_awareness import (
    _activate_main_turn,
    _make_real_agent,
    _publish_skill,
    _record_delivery,
)
from agent_py_agent.tests.test_sm_w10_closed_loop import _ineffective_output


def _refresh_snapshot(agent) -> None:
    agent._current_skill_snapshot = agent.skills_service.snapshot_for(force_reload=True)


def _main_thread(agent):
    return agent.conversation_store.threads.get_or_create(
        {
            "channel": "local",
            "channel_conversation_id": "main",
            "owner_id": str(agent.home_paths.owner_id),
        }
    )


def _skill_hurt_count(store, record) -> int:
    maint = dict(record.maintenance)
    agg = read_usage_aggregate(
        store._runtime_repository(),
        incarnation_id=str(maint["incarnation_id"]),
        activation_id=str(maint["activation_id"]),
        content_sha256=record.sha256,
    )
    return int(agg["hurt_count"]) if agg else 0


def _record_host_delivery(
    agent,
    record,
    *,
    thread_id: str,
    task_id: str,
    request_id: str,
    event: str = "host_injected",
    kind: str = "skill",
    body_chars: int = 320,
    incarnation_id: str | None = None,
    activation_id: str | None = None,
    content_sha256: str | None = None,
) -> None:
    maint = dict(record.maintenance)
    record_skill_usage_event(
        agent,
        event=event,
        skill_id=f"owner:{record.name}",
        source="learned",
        kind=kind,
        request_id=request_id,
        thread_id=thread_id,
        task_id=task_id,
        chars=body_chars,
        body_chars=body_chars,
        task_key=task_id,
        content_sha256=record.sha256 if content_sha256 is None else content_sha256,
        incarnation_id=str(maint["incarnation_id"]) if incarnation_id is None else incarnation_id,
        activation_id=str(maint["activation_id"]) if activation_id is None else activation_id,
        strike_epoch=int(maint.get("strike_epoch") or 0),
    )


def _seed_prior_adoptions(store, record, *, count: int = 2) -> None:
    maint = dict(record.maintenance)
    for idx in range(1, count + 1):
        observe_learning(
            store,
            record.name,
            sha256=record.sha256,
            task_id=f"seed-adopt-task-{idx}",
            outcome="adopted",
            source=LearningObservationSource(
                authority="model",
                run_id=f"seed-adopt-run-{idx}",
                receipt_id=f"seed-adopt:{idx}",
                incarnation_id=str(maint["incarnation_id"]),
                activation_id=str(maint["activation_id"]),
            ),
        )


def _make_summarizer(store, repo, tmp_path: Path, backend: _FakeBackend, name: str) -> SkillLearningService:
    settings = SkillLearningSettings(min_tool_rounds=3, max_skills=50)
    runtime = SkillLearningRuntime(
        owner_id="local:main",
        deployment_root=tmp_path / "dep",
        backend=backend,
        snapshot_provider=lambda: SimpleNamespace(
            entries=(
                SimpleNamespace(
                    name=name,
                    source="owner",
                    description="r2 test skill",
                    path=store.skill_path(name),
                    stable_id=f"owner:{name}",
                ),
            )
        ),
        guard_config=None,
        usage_runtime_provider=lambda: repo,
    )
    return SkillLearningService(store, settings, runtime)


def _assert_rejected_channel_a(res) -> None:
    assert res.ok is False
    assert res.error_code == "SKILL_FEEDBACK_UNAVAILABLE"
    assert res.effect_outcome == "not_started"
    sf = res.result_envelope["skill_feedback"]
    assert sf["side_effect_status"] == "not_started"
    assert sf["code"] == "PRECONDITION_NOT_MET"


def test_case1_channel_a_rejects_adopted_without_body_delivery_across_three_tasks(
    tmp_path: Path,
) -> None:
    agent, _, store = _make_real_agent(tmp_path)
    rec = _publish_skill(store, name="lesson-a-adopted-only")
    _refresh_snapshot(agent)

    claim = None
    for idx in range(1, 4):
        claim = _activate_main_turn(
            agent,
            tmp_path,
            run_id=f"run-c1-{idx}",
            task_id=f"task-c1-{idx}",
            request_id=f"req-c1-{idx}",
            prev_claim=claim,
        )
        adopted = SkillFeedbackTool(agent).execute(
            {"action": "adopted", "skill_id": f"owner:{rec.name}"}
        )
        assert adopted.ok is True

        rejected = SkillFeedbackTool(agent).execute(
            {"action": "ineffective", "skill_id": f"owner:{rec.name}"}
        )
        _assert_rejected_channel_a(rejected)

    assert _skill_hurt_count(store, rec) == 0
    assert store.skill_path(rec.name).exists()
    reg = store.load_registry()
    assert rec.name not in reg.blocked_names
    latest = reg.skills.get(rec.name)
    assert latest is not None
    assert latest.maintenance.get("state") != "retired"


def test_case2_channel_b_rejects_unattributed_or_missing_receipt_even_with_adopted(
    tmp_path: Path,
) -> None:
    _, repo, store = _make_real_agent(tmp_path)
    rec = _publish_skill(store, name="lesson-b-unattributed")
    maint = dict(rec.maintenance)
    _seed_prior_adoptions(store, rec, count=2)

    backend = _FakeBackend(
        _ineffective_output(rec.name),
        _ineffective_output(rec.name),
        _ineffective_output(rec.name),
    )
    summarizer = _make_summarizer(store, repo, tmp_path, backend, rec.name)

    # First two finalize requests omit owner:skill from used_skill_ids while holding adopted rows;
    # third includes owner:skill in used_skill_ids AND has an adopted row, but lacks a body receipt.
    cases = [
        ("task-b-1", ()),
        ("task-b-2", (f"capability:{rec.name}", f"builtin:{rec.name}")),
        ("task-b-3", (f"owner:{rec.name}",)),
    ]
    results = []
    for idx, (task_id, used_ids) in enumerate(cases, start=1):
        observe_learning(
            store,
            rec.name,
            sha256=rec.sha256,
            task_id=task_id,
            outcome="adopted",
            source=LearningObservationSource(
                authority="model",
                run_id=f"run-b-{idx}",
                receipt_id=f"adopt-b:{task_id}",
                incarnation_id=str(maint["incarnation_id"]),
                activation_id=str(maint["activation_id"]),
            ),
        )
        assert (
            summarizer.enqueue_from_finalize(
                _finalize_ctx(
                    f"run-b-{idx}",
                    task_id=task_id,
                    root_task_id=task_id,
                    used_skill_ids=used_ids,
                )
            )
            == "queued"
        )
        results.append(summarizer.run_pending())

    assert len(results) == 3
    for res in results:
        assert res.status == "rejected"
        assert res.code == CODE_INEFFECTIVE_UNATTRIBUTED

    assert _skill_hurt_count(store, rec) == 0
    assert store.skill_path(rec.name).exists()
    reg = store.load_registry()
    assert rec.name not in reg.blocked_names
    latest = reg.skills.get(rec.name)
    assert latest is not None
    assert latest.maintenance.get("state") != "retired"


def test_case3_channel_a_accepts_current_and_same_thread_delivery_rejects_other_or_noise(
    tmp_path: Path,
) -> None:
    agent, _, store = _make_real_agent(tmp_path)
    rec_current = _publish_skill(store, name="lesson-cur-get")
    rec_prev_get = _publish_skill(store, name="lesson-prev-get")
    rec_host_inj = _publish_skill(store, name="lesson-host-inj")
    rec_noise = _publish_skill(store, name="lesson-other-and-noise")
    _refresh_snapshot(agent)

    # Task 1 on main thread: manual_get for both rec_current and rec_prev_get.
    claim = _activate_main_turn(
        agent,
        tmp_path,
        run_id="run-c3-1",
        task_id="task-c3-1",
        request_id="req-c3-1",
    )
    thread = _main_thread(agent)
    get_cur = SkillSearchTool(agent).execute({"action": "get", "skill_id": f"owner:{rec_current.name}"})
    assert get_cur.ok is True
    get_prev = SkillSearchTool(agent).execute({"action": "get", "skill_id": f"owner:{rec_prev_get.name}"})
    assert get_prev.ok is True

    # Current-task delivery accepted by Channel A in Task 1.
    res_cur = SkillFeedbackTool(agent).execute(
        {"action": "ineffective", "skill_id": f"owner:{rec_current.name}"}
    )
    assert res_cur.ok is True
    assert res_cur.result_envelope["skill_feedback"]["code"] == "OK"
    assert _skill_hurt_count(store, rec_current) == 1

    # Task 1 also receives genuine same-thread host_injected delivery for rec_host_inj.
    _record_host_delivery(
        agent,
        rec_host_inj,
        thread_id=thread.thread_id,
        task_id="task-c3-1",
        request_id="req-c3-1",
        event="host_injected",
        body_chars=240,
    )

    # Seed noise & foreign-thread deliveries for rec_noise (none valid for main thread).
    other_thread = agent.conversation_store.threads.get_or_create(
        {
            "channel": "local",
            "channel_conversation_id": "other-thread-c3",
            "owner_id": str(agent.home_paths.owner_id),
        }
    )
    assert other_thread.thread_id != thread.thread_id
    _record_host_delivery(
        agent,
        rec_noise,
        thread_id=other_thread.thread_id,
        task_id="task-other-thread",
        request_id="req-other-thread",
        event="manual_get",
        body_chars=240,
    )
    _record_host_delivery(
        agent,
        rec_noise,
        thread_id="",
        task_id="task-empty-thread",
        request_id="req-empty-thread",
        event="manual_get",
        body_chars=240,
    )
    _record_host_delivery(
        agent,
        rec_noise,
        thread_id=thread.thread_id,
        task_id="task-c3-zero",
        request_id="req-c3-zero",
        event="manual_get",
        body_chars=0,
    )
    _record_host_delivery(
        agent,
        rec_noise,
        thread_id=thread.thread_id,
        task_id="task-c3-pkg",
        request_id="req-c3-pkg",
        event="host_injected",
        kind="capability_package",
        body_chars=240,
    )
    _record_host_delivery(
        agent,
        rec_noise,
        thread_id=thread.thread_id,
        task_id="task-c3-search",
        request_id="req-c3-search",
        event="manual_search",
        body_chars=240,
    )

    # Task 2 on SAME main thread: no new get in Task 2.
    claim = _activate_main_turn(
        agent,
        tmp_path,
        run_id="run-c3-2",
        task_id="task-c3-2",
        request_id="req-c3-2",
        prev_claim=claim,
    )
    res_prev = SkillFeedbackTool(agent).execute(
        {"action": "ineffective", "skill_id": f"owner:{rec_prev_get.name}"}
    )
    assert res_prev.ok is True
    assert res_prev.result_envelope["skill_feedback"]["code"] == "OK"
    assert _skill_hurt_count(store, rec_prev_get) == 1

    res_host = SkillFeedbackTool(agent).execute(
        {"action": "ineffective", "skill_id": f"owner:{rec_host_inj.name}"}
    )
    assert res_host.ok is True
    assert res_host.result_envelope["skill_feedback"]["code"] == "OK"
    assert _skill_hurt_count(store, rec_host_inj) == 1

    res_noise = SkillFeedbackTool(agent).execute(
        {"action": "ineffective", "skill_id": f"owner:{rec_noise.name}"}
    )
    _assert_rejected_channel_a(res_noise)
    assert _skill_hurt_count(store, rec_noise) == 0


def test_case4_same_thread_dedups_within_complaint_task_and_counts_distinct_tasks(
    tmp_path: Path,
) -> None:
    agent, _, store = _make_real_agent(tmp_path)
    rec = _publish_skill(store, name="lesson-c4-complaints")
    _refresh_snapshot(agent)

    # Delivery occurs once in Task 1 on main thread.
    claim = _activate_main_turn(
        agent,
        tmp_path,
        run_id="run-c4-deliver",
        task_id="task-c4-deliver",
        request_id="req-c4-deliver",
    )
    assert SkillSearchTool(agent).execute({"action": "get", "skill_id": f"owner:{rec.name}"}).ok is True

    # Complaint Task 2: calling twice must deduplicate to 1 strike.
    claim = _activate_main_turn(
        agent,
        tmp_path,
        run_id="run-c4-k1a",
        task_id="task-c4-k1",
        request_id="req-c4-k1a",
        prev_claim=claim,
    )
    first = SkillFeedbackTool(agent).execute(
        {"action": "ineffective", "skill_id": f"owner:{rec.name}"}
    )
    assert first.ok is True
    assert first.result_envelope["skill_feedback"]["code"] == "OK"
    assert _skill_hurt_count(store, rec) == 1

    claim = _activate_main_turn(
        agent,
        tmp_path,
        run_id="run-c4-k1b",
        task_id="task-c4-k1",
        request_id="req-c4-k1b",
        prev_claim=claim,
    )
    dup = SkillFeedbackTool(agent).execute(
        {"action": "ineffective", "skill_id": f"owner:{rec.name}"}
    )
    assert dup.ok is True
    assert dup.result_envelope["skill_feedback"]["code"] == "DEDUPLICATED"
    assert _skill_hurt_count(store, rec) == 1

    # Distinct complaint Tasks 3 and 4 in the same thread count separately (2 then 3 -> retired).
    claim = _activate_main_turn(
        agent,
        tmp_path,
        run_id="run-c4-k2",
        task_id="task-c4-k2",
        request_id="req-c4-k2",
        prev_claim=claim,
    )
    second = SkillFeedbackTool(agent).execute(
        {"action": "ineffective", "skill_id": f"owner:{rec.name}"}
    )
    assert second.ok is True
    assert second.result_envelope["skill_feedback"]["code"] == "OK"
    assert _skill_hurt_count(store, rec) == 2

    claim = _activate_main_turn(
        agent,
        tmp_path,
        run_id="run-c4-k3",
        task_id="task-c4-k3",
        request_id="req-c4-k3",
        prev_claim=claim,
    )
    third = SkillFeedbackTool(agent).execute(
        {"action": "ineffective", "skill_id": f"owner:{rec.name}"}
    )
    assert third.ok is True
    assert third.result_envelope["skill_feedback"]["code"] == "RETIRED"


def test_case5_stale_frozen_or_mismatched_activation_binding_cannot_transfer(
    tmp_path: Path,
) -> None:
    agent, _, store = _make_real_agent(tmp_path)
    rec_v1 = _publish_skill(store, name="lesson-c5-ver")
    _refresh_snapshot(agent)

    claim = _activate_main_turn(
        agent,
        tmp_path,
        run_id="run-c5-1",
        task_id="task-c5-1",
        request_id="req-c5-1",
    )
    assert SkillSearchTool(agent).execute({"action": "get", "skill_id": f"owner:{rec_v1.name}"}).ok is True
    stale_snapshot = agent._current_skill_snapshot

    # Publish v2 (new sha256 & new activation_id on same skill_name).
    rec_v2 = _publish_skill(store, name="lesson-c5-ver", update_from=rec_v1)

    # 1) Stale snapshot from v1 cannot submit feedback once v2 is live.
    claim = _activate_main_turn(
        agent,
        tmp_path,
        run_id="run-c5-2",
        task_id="task-c5-2",
        request_id="req-c5-2",
        prev_claim=claim,
    )
    agent._current_skill_snapshot = stale_snapshot
    stale_res = SkillFeedbackTool(agent).execute(
        {"action": "ineffective", "skill_id": f"owner:{rec_v2.name}"}
    )
    assert stale_res.ok is False
    assert stale_res.error_code == "SKILL_LEARNING_REVISION_CONFLICT"

    # 2) Frozen entry lacking verified binding or pointing outside owner store is rejected.
    _refresh_snapshot(agent)
    frozen_snap = agent._current_skill_snapshot
    orig_entry = frozen_snap.resolve(f"owner:{rec_v2.name}")
    assert orig_entry is not None
    frozen_copy_path = tmp_path / "frozen" / rec_v2.name / "SKILL.md"
    frozen_copy_path.parent.mkdir(parents=True, exist_ok=True)
    frozen_copy_path.write_text(BODY, encoding="utf-8")
    broken_entry = replace(
        orig_entry,
        frozen_path=str(frozen_copy_path),
        asset_fields={
            k: v
            for k, v in dict(orig_entry.asset_fields).items()
            if k != "_learning_usage_binding"
        },
    )
    agent._current_skill_snapshot = replace(
        frozen_snap,
        entries=tuple(
            broken_entry if e.stable_id == orig_entry.stable_id else e
            for e in frozen_snap.entries
        ),
    )
    frozen_res = SkillFeedbackTool(agent).execute(
        {"action": "ineffective", "skill_id": f"owner:{rec_v2.name}"}
    )
    assert frozen_res.ok is False
    assert frozen_res.error_code == "SKILL_LEARNING_REVISION_CONFLICT"

    # 3) Live v2 snapshot with ONLY v1's same-thread delivery (different activation/sha) is rejected.
    _refresh_snapshot(agent)
    no_v2_delivery = SkillFeedbackTool(agent).execute(
        {"action": "ineffective", "skill_id": f"owner:{rec_v2.name}"}
    )
    _assert_rejected_channel_a(no_v2_delivery)
    assert _skill_hurt_count(store, rec_v2) == 0


def test_case6_three_channels_dedup_three_tasks_rollback_and_channel_c_adopted_intact(
    tmp_path: Path,
) -> None:
    agent, repo, store = _make_real_agent(tmp_path)
    agent.runtime_db = repo
    name = "lesson-c6-tri"
    rec_v1 = _publish_skill(store, name=name)
    for idx in (1, 2, 3):
        _record_delivery(store, rec_v1, task_id=f"est-{idx}")
        observe_learning(
            store,
            name,
            sha256=rec_v1.sha256,
            task_id=f"est-{idx}",
            outcome="effective",
            source=LearningObservationSource(authority="host", receipt_id=f"est:{idx}"),
        )
    rec_v2 = _publish_skill(store, name=name, update_from=store.load_registry().skills[name])
    _refresh_snapshot(agent)

    # Task 1: real delivery + Channel A + Channel B + Channel C -> deduplicate to 1 strike.
    claim = _activate_main_turn(
        agent,
        tmp_path,
        run_id="run-c6-1",
        task_id="task-c6-1",
        request_id="req-c6-1",
    )
    assert SkillSearchTool(agent).execute({"action": "get", "skill_id": f"owner:{rec_v2.name}"}).ok is True
    assert has_body_delivery_receipt(
        store._runtime_repository(),
        incarnation_id=str(rec_v2.maintenance["incarnation_id"]),
        activation_id=str(rec_v2.maintenance["activation_id"]),
        content_sha256=rec_v2.sha256,
        task_id="task-c6-1",
        request_id="req-c6-1",
    )

    ch_a = SkillFeedbackTool(agent).execute(
        {"action": "ineffective", "skill_id": f"owner:{rec_v2.name}"}
    )
    assert ch_a.ok is True
    assert ch_a.result_envelope["skill_feedback"]["code"] == "OK"

    summarizer = _make_summarizer(store, repo, tmp_path, _FakeBackend(_ineffective_output(name)), name)
    assert (
        summarizer.enqueue_from_finalize(
            _finalize_ctx(
                "run-c6-1-bg",
                task_id="task-c6-1",
                root_task_id="task-c6-1",
                used_skill_ids=(f"owner:{name}",),
            )
        )
        == "queued"
    )
    ch_b = summarizer.run_pending()
    assert ch_b.status == "ineffective"
    assert ch_b.code == CODE_INEFFECTIVE_RECORDED

    observe_pack_verification(
        agent,
        agent._current_run_params,
        PackVerificationResult(
            verifier_id="pytest",
            package_id="pkg-c6-1",
            status="failed",
            valid=False,
        ),
        "pk-c6-1",
    )
    assert _skill_hurt_count(store, rec_v2) == 1

    # Tasks 2 and 3 (distinct tasks with Channel A complaints) trigger rollback to v1!
    for idx in (2, 3):
        claim = _activate_main_turn(
            agent,
            tmp_path,
            run_id=f"run-c6-{idx}",
            task_id=f"task-c6-{idx}",
            request_id=f"req-c6-{idx}",
            prev_claim=claim,
        )
        res = SkillFeedbackTool(agent).execute(
            {"action": "ineffective", "skill_id": f"owner:{rec_v2.name}"}
        )
        assert res.ok is True
        expected_code = "OK" if idx == 2 else "ROLLED_BACK"
        assert res.result_envelope["skill_feedback"]["code"] == expected_code

    restored = store.load_registry().skills.get(name)
    assert restored is not None
    assert restored.sha256 == rec_v1.sha256

    # Channel C retains adopted-only rule even without a body delivery receipt!
    rec_c_only = _publish_skill(store, name="lesson-c6-channel-c-adopted")
    _refresh_snapshot(agent)
    claim = _activate_main_turn(
        agent,
        tmp_path,
        run_id="task-c6-chc",
        task_id="task-c6-chc",
        request_id="req-c6-chc",
        prev_claim=claim,
    )
    adopt_only = SkillFeedbackTool(agent).execute(
        {"action": "adopted", "skill_id": f"owner:{rec_c_only.name}"}
    )
    assert adopt_only.ok is True
    assert not has_body_delivery_receipt(
        store._runtime_repository(),
        incarnation_id=str(rec_c_only.maintenance["incarnation_id"]),
        activation_id=str(rec_c_only.maintenance["activation_id"]),
        content_sha256=rec_c_only.sha256,
        task_id="task-c6-chc",
        request_id="req-c6-chc",
    )
    observe_pack_verification(
        agent,
        agent._current_run_params,
        PackVerificationResult(
            verifier_id="pytest",
            package_id="pkg-c6-chc",
            status="failed",
            valid=False,
        ),
        "pk-c6-chc",
    )
    assert _skill_hurt_count(store, rec_c_only) == 1


def test_case7_subagent_and_restricted_context_remain_denied(tmp_path: Path) -> None:
    agent, _, store = _make_real_agent(tmp_path)
    rec = _publish_skill(store, name="lesson-c7-subagent")
    _refresh_snapshot(agent)
    _activate_main_turn(
        agent,
        tmp_path,
        run_id="run-c7-main",
        task_id="task-c7-main",
        request_id="req-c7-main",
    )

    tool = SkillFeedbackTool(agent)
    params = {"action": "ineffective", "skill_id": f"owner:{rec.name}"}
    val_err = tool.validate_invocation(
        params,
        SimpleNamespace(context_scope="task_local", child=True, subagent_run_id="sub-c7-1"),
    )
    assert val_err is not None
    assert val_err.error_code == "TOOL_PERMISSION_DENIED"
    assert val_err.effect_outcome == "not_started"
    assert val_err.result_envelope["skill_feedback"]["side_effect_status"] == "not_started"
    assert val_err.result_envelope["skill_feedback"]["code"] == "CHILD_CONTEXT_FORBIDDEN"

    agent._current_run_params = replace(
        agent._current_run_params,
        context_scope="task_local",
        task_attributes={
            **dict(agent._current_run_params.task_attributes or {}),
            "agent_run_id": "sub-c7-1",
        },
    )
    exec_res = tool.execute(params)
    assert exec_res.ok is False
    assert exec_res.error_code == "TOOL_PERMISSION_DENIED"
    assert exec_res.effect_outcome == "not_started"
    assert exec_res.result_envelope["skill_feedback"]["side_effect_status"] == "not_started"
    assert exec_res.result_envelope["skill_feedback"]["code"] == "CHILD_CONTEXT_FORBIDDEN"
