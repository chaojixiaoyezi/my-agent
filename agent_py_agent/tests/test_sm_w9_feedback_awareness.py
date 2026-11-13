# LLM: W9 端到端合同回归：验证主会话反馈工具（Channel A/remove/restore）、V2-1 UserConfigTool 实时生效与 self_map 真实 telemetry。
# 模块用途: 校验 SkillFeedbackTool、skill_control_service、self_map_skills、self_map_memory 与 error_taxonomy 契约。
from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.agent_core.runtime_mixin import _bind_main_agent_authority
from agent_py_agent.agent.capability import (
    self_map_memory,
    self_map_skills,
    skill_feedback_tool,
)
from agent_py_agent.agent.capability.skill_learning_host_feedback import (
    observe_command_verification,
)
from agent_py_agent.agent.capability.skill_learning_maintenance import (
    LearningObservationSource,
    observe_learning,
)
from agent_py_agent.agent.capability.skill_learning_prompt import (
    parse_skill_learning_decision,
)
from agent_py_agent.agent.capability.skill_learning_publish import (
    PublishRequest,
    publish_learned_skill,
)
from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
from agent_py_agent.agent.capability.skill_service import SkillsService
from agent_py_agent.agent.capability.skill_usage_ledger import record_skill_usage_event
from agent_py_agent.agent.capability.skill_usage_receipts import read_usage_aggregate
from agent_py_agent.agent.contracts.error_taxonomy import ERROR_CONTRACTS
from agent_py_agent.agent.conversation.control_commands import (
    parse_conversation_control,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.io import write_json_file
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.request_binding import GatewayTaskBindingWriter
from agent_py_agent.agent.gateway_parts.skill_control_service import (
    SkillControlHost,
    run_skill_control,
)
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import runtime_db_path
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.settings.config import load_config
from agent_py_agent.agent.settings.live_switches import register_live_config
from agent_py_agent.agent.settings.parameter_registry import EFFECT_IMMEDIATE
from agent_py_agent.agent.tooling.models import (
    IdempotencyPolicy,
    ToolInvocationContext,
    tool_effect_for_runtime_policy,
)
from agent_py_agent.agent.tooling.self_map_registry import SelfMapReadContext
from agent_py_agent.agent.tooling.user_config_tool import UserConfigTool
from agent_py_agent.agent.user_space.home_layout import home_paths as make_home_paths
from agent_py_agent.agent.user_space.owner_policy import resolve_effective_owner_policy
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    ensure_owner_home,
    home_paths_with_owner,
)
from agent_py_agent.tests.test_skill_learning import BODY, _output


def _setup_owner(tmp_path: Path, *, execution_mode: str = "managed"):
    base = make_home_paths(tmp_path / "home")
    owner = ensure_owner_home(base.data_dir, OwnerIdentity.local_main())
    hp = home_paths_with_owner(base, owner)
    (Path(hp.owner_home_dir) / "skills").mkdir(parents=True, exist_ok=True)
    Path(hp.shared_skills_dir).mkdir(parents=True, exist_ok=True)
    Path(hp.shared_builtin_dir).mkdir(parents=True, exist_ok=True)
    repo = None
    if execution_mode != "local_unmanaged":
        repo = RuntimeRepository(runtime_db_path(hp.owner_home_dir))
    store = SkillLearningStore.for_home(
        hp,
        usage_runtime_provider=(lambda: repo) if repo is not None else None,
    )
    return hp, repo, store


def _publish_skill(
    store: SkillLearningStore,
    name: str = "csv-merge-by-date",
    *,
    when_to_use: str = "按日期合并多个 CSV 文件时",
    update_from=None,
):
    raw = _output(name=name, when_to_use=when_to_use)
    data = json.loads(raw)
    if update_from is not None:
        data["decision"] = "update"
        data["update_target"] = name
        data["body"] = BODY + "\n- 步骤补充：执行第二轮校验\n"
    updatable = frozenset({update_from.name}) if update_from is not None else frozenset()
    parsed = parse_skill_learning_decision(json.dumps(data, ensure_ascii=False), updatable)
    request = PublishRequest(
        parsed,
        {"request_key": f"req-{name}-{'u' if update_from else 'c'}", "task_id": "t-pub"},
        expected_revision=update_from.revision if update_from is not None else None,
        expected_sha256=update_from.sha256 if update_from is not None else "",
    )
    publish_learned_skill(store, request)
    return store.load_registry().skills[name]


def _make_readonly_agent(
    tmp_path: Path,
    hp,
    *,
    repo: RuntimeRepository | None = None,
    config: object | None = None,
    enable_self_learning: bool = True,
    execution_mode: str = "managed",
):
    cfg = config or SimpleNamespace(
        enable_self_learning=enable_self_learning,
        self_learning_maintenance_mode="autonomous",
        self_learning_max_skills=50,
        self_learning_recommended_limit=50,
        self_learning_recommended_explore_percent=20,
        self_learning_merge_similarity_percent=70,
        self_learning_cool_seconds=604800,
        self_learning_archive_seconds=1296000,
        self_learning_evict_seconds=1296000,
        self_learning_daily_limit=5,
        memory_lesson_archive_days=15,
        execution_mode=execution_mode,
    )
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    skills_svc = SkillsService(
        home_paths=hp,
        workspace_root=ws,
        policy_provider=lambda: resolve_effective_owner_policy(hp),
    )
    return SimpleNamespace(
        home_paths=hp,
        config=cfg,
        runtime_db=repo,
        tools=SimpleNamespace(workspace_root=ws),
        skills_service=skills_svc,
        _current_skill_snapshot=skills_svc.snapshot_for(force_reload=True),
        _current_run_params=RunParams(
            run_id="run-main-1",
            task_id="task-main-1",
            request_id="req-main-1",
            attempt_id="att-1",
            task_attributes={"root_task_id": "task-main-1"},
        ),
    )


def _make_real_agent(
    tmp_path: Path,
    *,
    enable_self_learning: bool = True,
    execution_mode: str = "managed",
    config: AgentConfig | None = None,
):
    cfg = config or AgentConfig(
        model_backend="echo",
        my_agent_home=str(tmp_path / "home"),
        my_agent_owner_provider="local",
        my_agent_owner_kind="main",
        my_agent_owner_id="local/main",
        execution_mode=execution_mode,
        enable_self_learning=enable_self_learning,
    )
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    agent = SimpleAgent(cfg, ws)
    repo = getattr(getattr(agent, "subagents", None), "runtime_db", None)
    if repo is None and execution_mode != "local_unmanaged":
        repo = RuntimeRepository(runtime_db_path(agent.home_paths.owner_home_dir))
    store = SkillLearningStore.for_home(
        agent.home_paths,
        usage_runtime_provider=(lambda: repo) if repo is not None else None,
    )
    return agent, repo, store


def _activate_main_turn(
    agent: SimpleAgent,
    tmp_path: Path,
    *,
    run_id: str,
    task_id: str,
    request_id: str,
    prev_claim: dict | None = None,
) -> dict:
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {"channel": "local", "channel_conversation_id": "main", "owner_id": str(agent.home_paths.owner_id)}
    )
    if prev_claim is not None:
        store.claims.finish({"thread_id": thread.thread_id, "claim_id": prev_claim["claim_id"], "status": "finished"})
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": task_id, "goal": "main turn", "status": "active"})
    paths = gateway_paths_from_root(tmp_path / "base-gateway")
    proc_path = paths.processing / f"{request_id}.json"
    exec_attempt = f"transport-{request_id}"
    write_json_file(
        proc_path,
        {
            "id": request_id,
            "status": "processing",
            "turn_phase": "open",
            "execution_attempt_id": exec_attempt,
        },
    )
    writer = GatewayTaskBindingWriter(proc_path, request_id, execution_attempt_id=exec_attempt)
    writer.bind_conversation_claim(thread.thread_id)
    claim = store.claims.acquire(
        {"thread_id": thread.thread_id, "task_id": f"gateway:{request_id}", "reason": "gateway_foreground_turn"}
    )
    agent._current_run_params = _bind_main_agent_authority(
        agent,
        RunParams(
            run_id=run_id,
            task_id=task_id,
            request_id=request_id,
            source="gateway",
            context_scope="conversation",
            task_attributes={
                "conversation_thread_id": thread.thread_id,
                "conversation_task_id": task_id,
                "root_task_id": task_id,
            },
            conversation_task_binding_callback=writer,
        ),
    )
    return claim


def _record_delivery(store: SkillLearningStore, rec, *, task_id: str, event: str = "manual_get") -> None:
    record_skill_usage_event(
        store,
        event=event,
        skill_id=f"owner:{rec.name}",
        source="learned",
        kind="skill",
        task_id=task_id,
        chars=200,
        body_chars=200,
        content_sha256=rec.sha256,
        incarnation_id=str(rec.maintenance.get("incarnation_id") or ""),
        activation_id=str(rec.maintenance.get("activation_id") or ""),
        strike_epoch=int(rec.maintenance.get("strike_epoch", 0) or 0),
    )


def test_w9_error_taxonomy_registers_all_required_codes():
    required = {
        "CANNOT_REMOVE_NON_LEARNED_SKILL",
        "SKILL_LEARNING_EVIDENCE_INCOMPLETE",
        "SKILL_LEARNING_NOT_LEARNED",
        "SKILL_LEARNING_USER_MODIFIED",
        "SKILL_LEARNING_ARCHIVE_EXPIRED",
        "SKILL_LEARNING_LIMIT_REACHED",
        "SKILL_LEARNING_REVISION_CONFLICT",
    }
    assert required <= set(ERROR_CONTRACTS)
    contract = ERROR_CONTRACTS["CANNOT_REMOVE_NON_LEARNED_SKILL"]
    assert contract.retryable is False
    assert contract.category == "capability"
    assert contract.recovery_hint


def test_skill_feedback_tool_schema_and_channel_a_documentation():
    spec = skill_feedback_tool.SkillFeedbackTool.model_spec
    assert spec.input_schema["properties"]["action"]["enum"] == [
        "status",
        "adopted",
        "remove",
        "restore",
        "ineffective",
    ]
    assert set(spec.input_schema["properties"]) == {"action", "skill_id"}
    assert spec.input_schema["additionalProperties"] is False
    assert "通道 A" in spec.description and "无需向用户" in spec.description

    policy = skill_feedback_tool.SkillFeedbackTool.runtime_policy
    assert tool_effect_for_runtime_policy(policy, {"action": "status"}) == "read_only"
    for mut_action in ("adopted", "remove", "restore", "ineffective"):
        assert tool_effect_for_runtime_policy(policy, {"action": mut_action}) == "mutating"
    assert policy.idempotency_policy == IdempotencyPolicy("operation")


def test_skill_feedback_validate_invocation_is_pure_and_blocks_child_on_execute(tmp_path: Path):
    cold_home = tmp_path / "cold-home"
    tool = skill_feedback_tool.SkillFeedbackTool(SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=cold_home)))

    bad_shape = tool.validate_invocation({"action": "remove", "skill_id": ""}, None)
    assert bad_shape is not None and bad_shape.ok is False and bad_shape.effect_outcome == "not_started"
    assert not cold_home.exists()

    ok_shape = tool.validate_invocation({"action": "ineffective", "skill_id": "owner:demo"}, None)
    assert ok_shape is None
    assert not cold_home.exists()

    hp, repo, _ = _setup_owner(tmp_path)
    child_agent = _make_readonly_agent(tmp_path, hp, repo=repo)
    child_agent._current_run_params = RunParams(
        run_id="run-child-1",
        task_id="task-child-1",
        task_attributes={"context_scope": "task_local", "parent_run_id": "run-main-1"},
        context_scope="task_local",
    )
    for params in (
        {"action": "status"},
        {"action": "ineffective", "skill_id": "owner:demo"},
        {"action": "remove", "skill_id": "owner:demo"},
        {"action": "restore", "skill_id": "owner:demo"},
    ):
        res = skill_feedback_tool.SkillFeedbackTool(child_agent).execute(params)
        assert res.ok is False
        assert res.effect_outcome == "not_started"
        body = res.result_envelope["skill_feedback"]
        assert body["side_effect_status"] == "not_started"
        assert "pending_learning_proposals" not in body


def test_skill_feedback_status_v2_4_omits_pending_proposals_and_stays_readonly(tmp_path: Path):
    hp, _, store = _setup_owner(tmp_path, execution_mode="local_unmanaged")
    _publish_skill(store, "csv-merge-by-date")
    agent = _make_readonly_agent(
        tmp_path,
        hp,
        repo=None,
        enable_self_learning=False,
        execution_mode="local_unmanaged",
    )
    db_file = runtime_db_path(hp.owner_home_dir)
    assert not db_file.exists()

    res = skill_feedback_tool.SkillFeedbackTool(agent).execute({"action": "status"})
    assert res.ok is True
    assert res.effect_outcome == ""
    assert not db_file.exists()

    raw = json.loads(res.output)
    assert "pending_learning_proposals" not in raw
    assert "pending_learning_proposals_count" not in raw
    assert raw["self_learning_maintenance_mode"] == "autonomous"
    assert raw["learned_skills_count"] == 1 and raw["learned_skills"][0]["name"] == "csv-merge-by-date"
    fb = res.result_envelope["skill_feedback"]
    assert "pending_learning_proposals" not in fb
    assert fb["code"] == "OK"

    single_res = skill_feedback_tool.SkillFeedbackTool(agent).execute(
        {"action": "status", "skill_id": "owner:csv-merge-by-date"}
    )
    assert single_res.ok is True and single_res.effect_outcome == ""
    single_fb = single_res.result_envelope["skill_feedback"]
    assert "pending_learning_proposals" not in single_fb
    assert single_fb["lifecycle"]["hurt_count"] == 0
    assert single_fb["lifecycle"]["retire_at_hurt_count"] == 3
    cov = single_fb["lifecycle"]["coverage"]
    assert isinstance(cov.get("as_of"), str) and datetime.fromisoformat(cov["as_of"]).tzinfo is not None


def test_skill_feedback_remove_and_restore_explicit_human_flow_and_non_learned_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    agent, _repo, store = _make_real_agent(tmp_path, enable_self_learning=False)
    hp = agent.home_paths
    rec = _publish_skill(store, "csv-merge-by-date")
    arch_rec = _publish_skill(store, "csv-archived-demo")
    exp_rec = _publish_skill(store, "csv-expired-demo")
    cap_rec = _publish_skill(store, "csv-cap-blocked-demo")
    pinned_rec = _publish_skill(store, "csv-pinned-live")
    user_skill_dir = Path(hp.owner_home_dir) / "skills" / "manual-custom"
    user_skill_dir.mkdir(parents=True, exist_ok=True)
    (user_skill_dir / "SKILL.md").write_text("---\nname: manual-custom\ndescription: d\n---\nbody\n", encoding="utf-8")

    # 显式 remove/restore 在 enable_self_learning=False 时仍可用，但 adopted 拒绝模型写入。
    _activate_main_turn(agent, tmp_path, run_id="run-main-1", task_id="task-main-1", request_id="req-main-1")
    agent._current_skill_snapshot = agent.skills_service.snapshot_for(force_reload=True)
    tool = skill_feedback_tool.SkillFeedbackTool(agent)

    refuse_builtin = tool.execute({"action": "remove", "skill_id": "builtin:foo"})
    assert refuse_builtin.ok is False
    assert refuse_builtin.error_code == "CANNOT_REMOVE_NON_LEARNED_SKILL"
    assert refuse_builtin.effect_outcome == "not_started"
    assert refuse_builtin.result_envelope["skill_feedback"]["side_effect_status"] == "not_started"

    refuse_user = tool.execute({"action": "remove", "skill_id": "owner:manual-custom"})
    assert refuse_user.ok is False
    assert refuse_user.error_code == "CANNOT_REMOVE_NON_LEARNED_SKILL"
    assert refuse_user.effect_outcome == "not_started"

    refuse_adopted = tool.execute({"action": "adopted", "skill_id": f"owner:{rec.name}"})
    assert refuse_adopted.ok is False
    assert refuse_adopted.effect_outcome == "not_started"

    removed = tool.execute({"action": "remove", "skill_id": f"owner:{rec.name}"})
    assert removed.ok is True, removed.output
    assert removed.effect_outcome == ""
    assert removed.result_envelope["skill_feedback"]["side_effect_status"] == "committed"
    assert not store.skill_path(rec.name).exists()
    assert rec.name in store.load_registry().blocked_names

    # 已归档自学习技能在 enable_self_learning=False 时仍可通过 restore 恢复；过期或满配受保护时精准报错。
    with store.state_lock():
        reg = store.load_registry()
        reg, _ = store._transition_archive_locked(reg, arch_rec.name, reason_code="cool_archive")
        reg, _ = store._transition_archive_locked(reg, exp_rec.name, now=1.0, reason_code="cool_archive")
        store._transition_archive_locked(reg, cap_rec.name, reason_code="cool_archive")
    restored = tool.execute({"action": "restore", "skill_id": f"owner:{arch_rec.name}"})
    assert restored.ok is True, restored.output
    assert restored.effect_outcome == ""
    assert store.skill_path(arch_rec.name).exists()

    expired = tool.execute({"action": "restore", "skill_id": f"owner:{exp_rec.name}"})
    assert expired.ok is False
    assert expired.error_code == "SKILL_LEARNING_ARCHIVE_EXPIRED"
    assert expired.effect_outcome == "not_started"

    # 上限为 1 且在役全员受 pin 保护时，restore 触发 SKILL_LEARNING_LIMIT_REACHED。
    with store.state_lock():
        reg = store.load_registry()
        reg, _ = store._transition_archive_locked(reg, arch_rec.name, reason_code="cool_archive")
        pinned = store.load_registry().skills[pinned_rec.name]
        pinned_maint = {**pinned.maintenance, "pinned": True}
        from dataclasses import replace as _dc_replace

        reg2 = store.load_registry()
        store.save_registry(
            _dc_replace(reg2, skills={**reg2.skills, pinned_rec.name: _dc_replace(pinned, maintenance=pinned_maint)})
        )
    agent.config.self_learning_max_skills = 1
    limit_err = tool.execute({"action": "restore", "skill_id": f"owner:{cap_rec.name}"})
    assert limit_err.ok is False
    assert limit_err.error_code == "SKILL_LEARNING_LIMIT_REACHED"
    assert limit_err.effect_outcome == "not_started"
    agent.config.self_learning_max_skills = 50

    # 若 W1 提交后仅 ledger_appended=False，工具外层必须汇报 effect_outcome="unknown" 与 side_effect_status="partial"。
    monkeypatch.setattr(
        skill_feedback_tool,
        "apply_learned_skill_action",
        lambda _s, name, _act: {
            "code": "removed",
            "skill_name": name,
            "version": 1,
            "sha256": pinned_rec.sha256,
            "committed": True,
            "outcome": "committed",
            "ledger_appended": False,
            "ledger_error": "OSError",
        },
    )
    partial = tool.execute({"action": "remove", "skill_id": f"owner:{pinned_rec.name}"})
    assert partial.ok is False
    assert partial.effect_outcome == "unknown"
    assert partial.result_envelope["skill_feedback"]["side_effect_status"] == "partial"


def test_skill_feedback_ineffective_channel_a_dedup_and_three_strikes(tmp_path: Path):
    agent, repo, store = _make_real_agent(tmp_path, enable_self_learning=True)
    v1 = _publish_skill(store, "tri-strike")
    for idx in (1, 2):
        _record_delivery(store, v1, task_id=f"establish-{idx}")
        observe_learning(
            store,
            v1.name,
            sha256=v1.sha256,
            task_id=f"establish-{idx}",
            outcome="effective",
            source=LearningObservationSource(
                authority="host",
                receipt_id=f"rcpt-est-{idx}",
            ),
        )
    v1_est = store.load_registry().skills["tri-strike"]
    v2 = _publish_skill(store, "tri-strike", update_from=v1_est)
    solo = _publish_skill(store, "solo-strike")
    claim = _activate_main_turn(agent, tmp_path, run_id="run-main-1", task_id="task-main-1", request_id="req-main-1")
    agent._current_skill_snapshot = agent.skills_service.snapshot_for(force_reload=True)
    tool = skill_feedback_tool.SkillFeedbackTool(agent)

    # 无送达回执时拒绝无效扣分。
    no_receipt = tool.execute({"action": "ineffective", "skill_id": "owner:tri-strike"})
    assert no_receipt.ok is False
    assert no_receipt.effect_outcome == "not_started"
    assert int(store.load_registry().skills["tri-strike"].maintenance.get("hurt_count") or 0) == 0

    # 通道 A 记录第 1 次打击并同任务去重；通道 C 在同任务也不叠加第 2 次打击。
    _record_delivery(store, v2, task_id="task-main-1")
    hit1 = tool.execute({"action": "ineffective", "skill_id": "owner:tri-strike"})
    assert hit1.ok is True, hit1.output
    assert hit1.effect_outcome == ""
    fb1 = hit1.result_envelope["skill_feedback"]
    assert fb1["code"] == "OK" and fb1["lifecycle"]["hurt_count"] == 1
    assert "pending_learning_proposals" not in hit1.output

    dedup = tool.execute({"action": "ineffective", "skill_id": "owner:tri-strike"})
    assert dedup.ok is True, dedup.output
    fb_dup = dedup.result_envelope["skill_feedback"]
    assert fb_dup["code"] == "DEDUPLICATED" and fb_dup["lifecycle"]["hurt_count"] == 1

    # 同任务经过通道 C 也不得变成 2 次打击。
    observe_command_verification(
        agent,
        agent._current_run_params,
        {"ok": False, "command": "pytest", "exit_code": 1, "target_skill": "owner:tri-strike"},
    )
    assert int(store.load_registry().skills["tri-strike"].maintenance.get("hurt_count") or 0) == 1

    # 第二、第三个不同任务连续 ineffective 后，第 3 次触发回滚到已证实稳定的 v1。
    for idx in (2, 3):
        claim = _activate_main_turn(
            agent,
            tmp_path,
            run_id=f"run-main-{idx}",
            task_id=f"task-main-{idx}",
            request_id=f"req-main-{idx}",
            prev_claim=claim,
        )
        _record_delivery(store, v2, task_id=f"task-main-{idx}")
        agent._current_skill_snapshot = agent.skills_service.snapshot_for(force_reload=True)
        res = tool.execute({"action": "ineffective", "skill_id": "owner:tri-strike"})
        assert res.ok is True, res.output
        if idx == 3:
            assert res.result_envelope["skill_feedback"]["code"] == "ROLLED_BACK"
    assert store.load_registry().skills["tri-strike"].version == 1

    # 确认总累计聚合与账本不互相矛盾。
    agg = read_usage_aggregate(
        repo,
        incarnation_id=str(v2.maintenance.get("incarnation_id") or ""),
        activation_id=str(v2.maintenance.get("activation_id") or ""),
        content_sha256=v2.sha256,
    )
    assert agg is not None and agg["hurt_count"] == 3

    # 版本冲突：快照停在旧版但磁盘已升级时，adopted/ineffective 拒绝并返回 SKILL_LEARNING_REVISION_CONFLICT。
    agent._current_skill_snapshot = agent.skills_service.snapshot_for(force_reload=True)
    v3 = _publish_skill(store, "tri-strike", update_from=store.load_registry().skills["tri-strike"])
    stale_adopted = tool.execute({"action": "adopted", "skill_id": "owner:tri-strike"})
    assert stale_adopted.ok is False
    assert stale_adopted.error_code == "SKILL_LEARNING_REVISION_CONFLICT"
    assert stale_adopted.effect_outcome == "not_started"
    assert v3.version >= 2

    # 显式人类 ineffective 在 enable_self_learning=False 时仍保留，且无稳定前驱第 3 次退场 (RETIRED)。
    agent.config.enable_self_learning = False
    for idx in (4, 5, 6):
        claim = _activate_main_turn(
            agent,
            tmp_path,
            run_id=f"run-main-{idx}",
            task_id=f"task-main-{idx}",
            request_id=f"req-main-{idx}",
            prev_claim=claim,
        )
        _record_delivery(store, solo, task_id=f"task-main-{idx}")
        agent._current_skill_snapshot = agent.skills_service.snapshot_for(force_reload=True)
        res = tool.execute({"action": "ineffective", "skill_id": "owner:solo-strike"})
        assert res.ok is True, res.output
        if idx == 6:
            assert res.result_envelope["skill_feedback"]["code"] == "RETIRED"
    assert solo.name in store.load_registry().blocked_names


def test_v2_1_user_config_tool_live_policy_and_self_map_telemetry(tmp_path: Path):
    cfg_path = tmp_path / "user_config.yaml"
    cfg_path.write_text('agent_name: "w9-e2e"\n', encoding="utf-8")
    stat_before = cfg_path.stat()
    os.utime(cfg_path, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns - 5_000_000_000))

    config = load_config(cfg_path)
    config.model_backend = "echo"
    config.my_agent_home = str(tmp_path / "home")
    config.my_agent_owner_provider = "local"
    config.my_agent_owner_kind = "main"
    config.my_agent_owner_id = "local/main"
    config.execution_mode = "managed"
    register_live_config(config)

    main_agent, _repo, store = _make_real_agent(tmp_path, config=config)
    hp = main_agent.home_paths
    _publish_skill(store, "live-skill")

    # 放入一个 user-owned skill 与一个 shared skill 核验豁免上限计数。
    user_dir = Path(hp.owner_home_dir) / "skills" / "user-only"
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / "SKILL.md").write_text("---\nname: user-only\ndescription: u\n---\nx\n", encoding="utf-8")
    (Path(hp.shared_skills_dir) / "shared-only").mkdir(parents=True, exist_ok=True)
    (Path(hp.shared_skills_dir) / "shared-only" / "SKILL.md").write_text("---\nname: shared-only\ndescription: s\n---\nx\n", encoding="utf-8")
    main_agent._current_skill_snapshot = main_agent.skills_service.snapshot_for(force_reload=True)
    main_agent.memory_lessons = SimpleNamespace(_read_heat_state=lambda: ("missing", "heat_state_absent"))

    cfg_tool = UserConfigTool(main_agent)
    for key, val in (
        ("self_learning_maintenance_mode", "observe"),
        ("self_learning_max_skills", "12"),
        ("memory_lesson_archive_days", "30"),
        ("enable_self_learning", "false"),
    ):
        out = cfg_tool.execute({"action": "set", "key": key, "value": val, "reason": "w9 live check"})
        assert out.ok, (key, out.output)
        assert json.loads(out.output)["effect_when"] == EFFECT_IMMEDIATE

    inv = ToolInvocationContext(main_agent.tools.runtime_snapshot(allowed_tools=["self_map", "skill_search", "remember"]))
    ctx = SelfMapReadContext(main_agent, inv, "admin", False, "", "main", hp.owner_id, "admin")

    skills_proj = self_map_skills.skill_projection(ctx)
    skills_tel = self_map_skills.skill_telemetry(ctx)
    assert isinstance(skills_tel["coverage"].get("as_of"), str)
    assert datetime.fromisoformat(skills_tel["coverage"]["as_of"]).tzinfo is not None
    assert skills_proj.counts["live_learned_count"] == 1
    assert skills_proj.counts["max_learned_skills"] == 12
    assert skills_proj.counts["user_owned_count"] >= 1
    assert skills_proj.counts["shared_count"] >= 1
    assert skills_proj.counts["retire_at_hurt_count"] == 3
    assert "enable_self_learning=False" in skills_proj.summary
    assert "maintenance_mode=observe" in skills_proj.summary

    # lesson_heat.json 缺失时 MemoryTelemetry 保持 None + 覆盖 unavailable 且顶层 completeness="partial"，绝不编造 0。
    mem_proj = self_map_memory.memory_projection(ctx)
    mem_tel = self_map_memory.memory_telemetry(ctx)
    assert mem_proj.completeness == "partial"
    assert mem_proj.errors == ()
    assert isinstance(mem_tel["coverage"].get("as_of"), str)
    assert datetime.fromisoformat(mem_tel["coverage"]["as_of"]).tzinfo is not None
    assert mem_proj.counts["lessons_active_count"] is None
    assert mem_proj.counts["lessons_archived_count"] is None
    assert mem_proj.counts["lesson_archive_days"] == 30
    assert "lessons_active_count" in mem_proj.count_reasons

    # lesson_heat.json 损坏或 I/O 异常时顶层 completeness="partial" 且返回安全错误码。
    main_agent.memory_lessons = SimpleNamespace(_read_heat_state=lambda: ("corrupt", "heat_state_invalid_json"))
    mem_corrupt = self_map_memory.memory_projection(ctx)
    assert mem_corrupt.completeness == "partial"
    assert mem_corrupt.errors == ("LESSON_HEAT_CORRUPT",)

    main_agent.memory_lessons = SimpleNamespace(_read_heat_state=lambda: ("error", "OSError"))
    mem_err = self_map_memory.memory_projection(ctx)
    assert mem_err.completeness == "partial"
    assert mem_err.errors == ("LESSON_HEAT_UNAVAILABLE",)

    # lesson_heat.json 存在但 archive_count_coverage 未完成时，仅 active/routed 精确计数，archived 仍保持 None。
    main_agent.memory_lessons = SimpleNamespace(
        _read_heat_state=lambda: (
            "ok",
            {
                "rows": {"l1": {"status": "active"}, "l2": {"status": "archived"}},
                "routed_ids": ["l1"],
                "lessons_archived_count": 0,
                "archive_count_coverage": {"status": "partial", "reason": "scan_budget_exhausted"},
            },
        )
    )
    mem_partial = self_map_memory.memory_projection(ctx)
    assert mem_partial.completeness == "partial"
    assert mem_partial.errors == ()
    assert mem_partial.counts["lessons_active_count"] == 1
    assert mem_partial.counts["lessons_routed_count"] == 1
    assert mem_partial.counts["lessons_archived_count"] is None
    assert "lessons_archived_count" in mem_partial.count_reasons

    # 覆盖完整时顶层 completeness="complete" 且精确计数全部就绪。
    main_agent.memory_lessons = SimpleNamespace(
        _read_heat_state=lambda: (
            "ok",
            {
                "rows": {"l1": {"status": "active"}, "l2": {"status": "archived"}},
                "routed_ids": ["l1"],
                "lessons_archived_count": 1,
                "archive_count_coverage": {"status": "complete", "reason": "full_scan"},
            },
        )
    )
    mem_complete = self_map_memory.memory_projection(ctx)
    assert mem_complete.completeness == "complete"
    assert mem_complete.errors == ()
    assert mem_complete.counts["lessons_archived_count"] == 1
    mem_complete_tel = self_map_memory.memory_telemetry(ctx)
    assert isinstance(mem_complete_tel["coverage"].get("as_of"), str)
    assert datetime.fromisoformat(mem_complete_tel["coverage"]["as_of"]).tzinfo is not None

    # 子代理 / 非主拥有者严禁经 UserConfigTool 修改全局策略。
    non_main_home = SimpleNamespace(owner_provider="feishu", owner_kind="users", owner_id="ou_non_admin")
    denied = UserConfigTool(SimpleNamespace(home_paths=non_main_home, config=config)).execute(
        {"action": "set", "key": "enable_self_learning", "value": "true"}
    )
    assert denied.ok is False and denied.error_code == "TOOL_PERMISSION_DENIED"


def test_self_map_and_skill_control_v2_4_and_host_contract(tmp_path: Path):
    hp, _, _ = _setup_owner(tmp_path, execution_mode="local_unmanaged")
    cfg = AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), execution_mode="local_unmanaged")
    host = SkillControlHost(hp, cfg)
    assert host.home_paths is hp and host.config is cfg
    assert host.capability_config_path == "" and host.actor_id == "" and host.usage_runtime_provider is None

    cmd = parse_conversation_control("/skills", reject_unknown_slash=True)
    assert cmd is not None
    rendered = run_skill_control(host, cmd)
    assert "待确认学习候选" not in rendered
    assert "自主学习维护：mode=autonomous" in rendered

    # memory_summary 保持总览轻量预算且隔离受限作用域。
    agent = _make_readonly_agent(tmp_path, hp, execution_mode="local_unmanaged")
    inv = ToolInvocationContext(SimpleNamespace(allowed_tool_names=frozenset({"self_map"})))
    restricted_ctx = SelfMapReadContext(agent, inv, "task", True, "run-child", "task_local", hp.owner_id, "admin")
    assert self_map_memory.memory_summary(restricted_ctx).completeness == "partial"
    assert self_map_memory.memory_projection(restricted_ctx).completeness == "partial"


def test_skill_feedback_usage_binding_conflict_reheat_and_frozen_snapshot(tmp_path: Path):
    """同体重新激活（activation_id 变更）与历史脱敏快照（frozen_path 剥离绑定）均须拒绝并报 REVISION_CONFLICT。"""
    agent, _repo, store = _make_real_agent(tmp_path, enable_self_learning=True)
    rec = _publish_skill(store, "binding-check")
    _activate_main_turn(agent, tmp_path, run_id="run-bind-1", task_id="task-bind-1", request_id="req-bind-1")
    _record_delivery(store, rec, task_id="task-bind-1")

    live_snapshot = agent.skills_service.snapshot_for(force_reload=True)
    live_entry = live_snapshot.resolve(f"owner:{rec.name}")
    assert live_entry is not None
    stale_act_entry = replace(
        live_entry,
        asset_fields={
            **dict(live_entry.asset_fields),
            "_learning_usage_binding": {
                "incarnation_id": str(rec.maintenance["incarnation_id"]),
                "activation_id": "old-activation-before-reheat",
                "strike_epoch": int(rec.maintenance.get("strike_epoch") or 0),
                "content_sha256": rec.sha256,
            },
        },
    )
    agent._current_skill_snapshot = replace(
        live_snapshot,
        entries=tuple(stale_act_entry if e.stable_id == live_entry.stable_id else e for e in live_snapshot.entries),
    )

    tool = skill_feedback_tool.SkillFeedbackTool(agent)
    for act in ("adopted", "ineffective"):
        res = tool.execute({"action": act, "skill_id": f"owner:{rec.name}"})
        assert res.ok is False, (act, res.output)
        assert res.error_code == "SKILL_LEARNING_REVISION_CONFLICT"
        assert res.effect_outcome == "not_started"
        assert res.result_envelope["skill_feedback"]["side_effect_status"] == "not_started"
    assert int(store.load_registry().skills[rec.name].maintenance.get("hurt_count") or 0) == 0

    # 历史版本恢复视图：resolve_learned_revision 设置 frozen_path 且剥离 _learning_usage_binding。
    frozen_fields = dict(live_entry.asset_fields)
    frozen_fields.pop("_learning_usage_binding", None)
    frozen_entry = replace(
        live_entry,
        frozen_path="versions/v1/SKILL.md",
        asset_fields=frozen_fields,
    )
    agent._current_skill_snapshot = replace(
        live_snapshot,
        entries=tuple(frozen_entry if e.stable_id == live_entry.stable_id else e for e in live_snapshot.entries),
    )
    for act in ("adopted", "ineffective"):
        res = tool.execute({"action": act, "skill_id": f"owner:{rec.name}"})
        assert res.ok is False, (act, res.output)
        assert res.error_code == "SKILL_LEARNING_REVISION_CONFLICT"
        assert res.effect_outcome == "not_started"
        assert res.result_envelope["skill_feedback"]["side_effect_status"] == "not_started"
    assert int(store.load_registry().skills[rec.name].maintenance.get("hurt_count") or 0) == 0

    # 恢复精确绑定快照后，adopted 正常落账并携带有效生命周期 coverage ISO 字符串。
    agent._current_skill_snapshot = live_snapshot
    ok_adopted = tool.execute({"action": "adopted", "skill_id": f"owner:{rec.name}"})
    assert ok_adopted.ok is True, ok_adopted.output
    cov = ok_adopted.result_envelope["skill_feedback"]["lifecycle"]["coverage"]
    assert isinstance(cov.get("as_of"), str) and datetime.fromisoformat(cov["as_of"]).tzinfo is not None


def test_self_map_memory_unmocked_canonical_lesson_heat_fallback(tmp_path: Path):
    """未绑定 agent.memory_lessons 时，回退路径须从规范 routing/lesson_heat.json 读取并校验 schema/symlink。"""
    agent, _repo, _store = _make_real_agent(tmp_path, enable_self_learning=False)
    hp = agent.home_paths
    agent.memory_lessons = None

    inv = ToolInvocationContext(agent.tools.runtime_snapshot(allowed_tools=["self_map", "remember"]))
    ctx = SelfMapReadContext(agent, inv, "admin", False, "", "main", hp.owner_id, "admin")

    # 1) 缺失状态
    missing_proj = self_map_memory.memory_projection(ctx)
    assert missing_proj.completeness == "partial"
    assert missing_proj.errors == ()
    assert missing_proj.counts["lessons_active_count"] is None

    # 2) 规范路径 memory/routing/lesson_heat.json 完整有效文档
    heat_path = Path(hp.owner_memory_routing_index_md).parent / "lesson_heat.json"
    heat_path.parent.mkdir(parents=True, exist_ok=True)
    heat_doc = {
        "schema_version": "my-agent.lesson-heat.v1",
        "generation": "gen-w9-test",
        "updated_at": 1700000000.0,
        "rows": {
            "lesson-a": {
                "path": "memory/lessons/lesson-a.md",
                "subject_key": "s-a",
                "scope_type": "user",
                "scope_key": "main",
                "status": "active",
                "revision": 1,
                "created_at": "2026-11-13T18:00:00+00:00",
                "updated_at": "2026-11-13T18:00:00+00:00",
                "last_hit_at": 1700000000.0,
                "hits": 1,
                "unrouted_at": 0.0,
                "superseded_at": 0.0,
            },
            "lesson-b": {
                "path": "memory/lessons/lesson-b.md",
                "subject_key": "s-b",
                "scope_type": "user",
                "scope_key": "main",
                "status": "superseded",
                "revision": 1,
                "created_at": "2026-11-13T17:00:00+00:00",
                "updated_at": "2026-11-13T17:30:00+00:00",
                "last_hit_at": 1699000000.0,
                "hits": 0,
                "unrouted_at": 0.0,
                "superseded_at": 1700000000.0,
            },
        },
        "routed_ids": ["lesson-a"],
        "lessons_archived_count": 1,
        "archive_count_coverage": {
            "status": "complete",
            "reason": "full_scan",
            "as_of": "2026-11-13T18:00:00+00:00",
        },
    }
    heat_path.write_text(json.dumps(heat_doc, ensure_ascii=False), encoding="utf-8")
    ok_proj = self_map_memory.memory_projection(ctx)
    assert ok_proj.completeness == "complete"
    assert ok_proj.errors == ()
    assert ok_proj.counts["lessons_active_count"] == 1
    assert ok_proj.counts["lessons_routed_count"] == 1
    assert ok_proj.counts["lessons_archived_count"] == 1

    # 3) 损坏文档返回 partial + LESSON_HEAT_CORRUPT
    heat_path.write_text('{"schema_version": "wrong"}', encoding="utf-8")
    corrupt_proj = self_map_memory.memory_projection(ctx)
    assert corrupt_proj.completeness == "partial"
    assert corrupt_proj.errors == ("LESSON_HEAT_CORRUPT",)
    assert corrupt_proj.counts["lessons_active_count"] is None

    # 4) 软链接同样被拦截并返回 partial + LESSON_HEAT_CORRUPT
    outside = tmp_path / "outside-heat.json"
    outside.write_text(json.dumps(heat_doc, ensure_ascii=False), encoding="utf-8")
    heat_path.unlink()
    heat_path.symlink_to(outside)
    symlink_proj = self_map_memory.memory_projection(ctx)
    assert symlink_proj.completeness == "partial"
    assert symlink_proj.errors == ("LESSON_HEAT_CORRUPT",)
    assert symlink_proj.counts["lessons_active_count"] is None
