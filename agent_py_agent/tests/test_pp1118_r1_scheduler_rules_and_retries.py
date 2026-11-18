"""PP1118 Round-1 Rework R5：调度规则 postcheck、定义代次守卫、未知结果重试拦截与事件批次重试回归测试。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.backends.tool_schema import tool_model_spec_to_anthropic_tool
from agent_py_agent.agent.contracts.tool_input_schema import validate_tool_input
from agent_py_agent.agent.conversation.models import ConversationThread
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.scheduler.active_run_closeout import (
    SCHEDULED_TASK_FOLLOW_UP_UNREADABLE,
    SCHEDULED_TASK_TOOL_OUTCOME_UNKNOWN,
    SchedulerRunOutcome,
    close_active_run,
)
from agent_py_agent.agent.scheduler.repository import (
    SchedulerJobCreateRequest,
    SchedulerRepository,
)
from agent_py_agent.agent.scheduler.schedule import ScheduleValidationError
from agent_py_agent.agent.scheduler.service import SchedulerRunClaim, SchedulerService
from agent_py_agent.agent.scheduler.tool import ScheduleTool
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    resolve_owner_home,
)


def _setup_owner(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    store = ConversationStore(tmp_path / "conversation")
    store.threads.write(
        ConversationThread(
            thread_id="thread-1",
            canonical_user_id="user-1",
            cwd=str(workspace),
        )
    )
    repository = SchedulerRepository(
        tmp_path / "scheduler",
        owner_provider="feishu",
        owner_kind="user",
        owner_id="user-1",
        default_timezone="Asia/Shanghai",
    )
    service = SchedulerService(repository, conversation_store=store)
    agent = SimpleNamespace(
        scheduler_repository=repository,
        scheduler_service=service,
        config=SimpleNamespace(timezone="Asia/Shanghai", retention_scan_slice_bytes=65536),
        _current_run_params=RunParams(
            request_id="req-r5",
            task_id="task-r5",
            task_attributes={"conversation_thread_id": "thread-1", "conversation_task_id": "task-r5"},
        ),
        current_skill_snapshot=lambda: SimpleNamespace(resolve=lambda names: ([], list(names))),
        conversation_store=store,
    )
    return workspace, store, repository, service, agent


def _claim(repository: SchedulerRepository, run_id: str, *, now: float) -> SchedulerRunClaim:
    row = repository.claim_run(run_id, lease_seconds=300, now=now)
    assert row is not None
    return SchedulerRunClaim(run_id=str(row["run_id"]), claim_id=str(row["claim_id"]), run=row)


def _event_schedule(*, capacity: int = 16) -> dict[str, object]:
    return {
        "kind": "event_source",
        "source": {
            "channel": "feishu",
            "event_type": "message",
            "app_id": "receiver-app",
            "tenant_key": "chat-tenant",
            "chat_id": "group-1",
            "sender_type": "bot",
            "sender_id_type": "app_id",
            "sender_ids": ["source-app"],
            "proofs": [{"sender_id": "source-app", "source_message_id": "quoted-message"}],
        },
        "min_interval_seconds": 60,
        "max_batch_events": capacity,
    }


def _synthetic_event(message_id: str, *, content: str, created_at: float) -> dict[str, object]:
    return {
        "channel": "feishu",
        "event_type": "message",
        "app_id": "receiver-app",
        "tenant_key": "chat-tenant",
        "chat_id": "group-1",
        "sender_type": "bot",
        "sender_id_type": "app_id",
        "sender_id": "source-app",
        "message_id": message_id,
        "ingress_id": f"ing-{message_id}",
        "created_at": created_at,
        "content": content,
    }


def _setup_group_event_env(tmp_path):
    workspace = tmp_path / "group_ws"
    workspace.mkdir(parents=True, exist_ok=True)
    identity = OwnerIdentity("feishu", "group", "group-1")
    home = home_paths_with_owner(home_paths(tmp_path), resolve_owner_home(tmp_path, identity))
    store = ConversationStore(home.owner_home_dir / "conversation")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "human-1",
            "channel": "feishu",
            "channel_conversation_id": "group-1",
            "channel_user_id": "human-1",
            "owner_id": home.owner_id,
            "owner_home": str(home.owner_home_dir),
            "cwd": str(workspace),
        }
    )
    repository = SchedulerRepository(
        home.owner_home_dir / "scheduler",
        owner_provider=identity.provider,
        owner_kind=identity.owner_kind,
        owner_id=home.owner_id,
        default_timezone="Asia/Shanghai",
    )
    service = SchedulerService(repository, conversation_store=store)
    return workspace, store, thread, repository, service


def test_p1_05_unknown_effect_codes_allow_investigation_and_forbid_retry(tmp_path) -> None:
    """P1-05：close_active_run 及直接未知副作用错误码保留只读调查、标记 effect_unknown=True，禁止自动 retry_once。"""
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            my_agent_home=str(tmp_path / "agent_home"),
            orphan_supervision_interval_seconds=0,
        ),
        tmp_path / "agent_ws",
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "conv-p1-05",
            "channel_user_id": "user-1",
            "now": 900.0,
        }
    )
    job, _ = agent.scheduler_repository.create_job(
        SchedulerJobCreateRequest(
            name="unknown-tool-outcome-job",
            prompt="execute scheduled step",
            thread_id=thread.thread_id,
            schedule={"kind": "every", "every_seconds": 600, "anchor_at": 1000.0},
            now=950.0,
            policy={
                "on_failure": {
                    "investigate": True,
                    "after_failures": 1,
                    "retry_once": True,
                    "prompt": "只读核查工具未知状态。",
                }
            },
        )
    )
    due_run = agent.scheduler_repository.reserve_due_runs(now=1000.0)[0]
    claimed = _claim(agent.scheduler_repository, str(due_run["run_id"]), now=1001.0)
    agent.scheduler_repository.mark_run_running(claimed.run_id, claimed.claim_id, now=1002.0)
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": claimed.run_id,
            "goal": "execute scheduled step",
            "status": "active",
            "now": 1002.0,
        }
    )

    terminal = close_active_run(
        agent.scheduler_service,
        claimed,
        SchedulerRunOutcome(
            response="step interrupted",
            runtime_status="unfinished",
            runtime_reason="TOOL_OPERATION_OUTCOME_UNKNOWN",
        ),
        now=1005.0,
    )
    assert terminal is not None
    assert terminal["status"] == "failed"
    assert terminal["error_code"] == SCHEDULED_TASK_TOOL_OUTCOME_UNKNOWN
    assert store.tasks.load(claimed.run_id).status == "blocked"

    persisted_job = agent.scheduler_repository.get_job(job["job_id"])
    streak = persisted_job["failure_streak"]
    assert streak["count"] == 1
    assert streak["retry_run_id"] == ""
    inv_id = streak["investigation_run_id"]
    assert inv_id

    active_runs = agent.scheduler_repository.queued_runs(now=2000.0)
    assert [row["run_id"] for row in active_runs] == [inv_id]
    inv_run = agent.scheduler_repository.get_active_run(inv_id)
    assert inv_run is not None
    assert inv_run["trigger"] == "failure_investigation"
    assert inv_run["failure_context"]["error_code"] == SCHEDULED_TASK_TOOL_OUTCOME_UNKNOWN
    assert inv_run["failure_context"]["effect_unknown"] is True

    # 直接以 TOOL_OPERATION_OUTCOME_UNKNOWN 与 SCHEDULED_TASK_FOLLOW_UP_UNREADABLE 收口同样不触发自动重试。
    _ws, _st, repo2, svc2, _ag = _setup_owner(tmp_path / "direct_codes")
    for idx, err_code in enumerate(("TOOL_OPERATION_OUTCOME_UNKNOWN", SCHEDULED_TASK_FOLLOW_UP_UNREADABLE)):
        j, _ = repo2.create_job(
            SchedulerJobCreateRequest(
                name=f"direct-unknown-{idx}",
                prompt="work",
                thread_id="thread-1",
                schedule={"kind": "every", "every_seconds": 600, "anchor_at": 1000.0},
                now=1000.0,
                policy={"on_failure": {"investigate": True, "after_failures": 1, "retry_once": True}},
            )
        )
        r = repo2.reserve_manual_run(j["job_id"], now=1010.0 + idx)
        c = _claim(repo2, str(r["run_id"]), now=1011.0 + idx)
        svc2.finish(c, status="failed", error_code=err_code, now=1015.0 + idx)
        j_state = repo2.get_job(j["job_id"])
        assert j_state["failure_streak"]["retry_run_id"] == ""
        inv = repo2.get_active_run(j_state["failure_streak"]["investigation_run_id"])
        assert inv is not None
        assert inv["failure_context"]["effect_unknown"] is True


def test_p1_06_postcheck_bumps_generation_and_stale_or_paused_runs_preserve_new_definition(tmp_path) -> None:
    """P1-06 / F07：修改 postcheck 递增定义代次；旧代/已暂停/已取消运行不污染新定义，同事务自动暂停仍预约调查。"""
    _ws, _store, repository, service, _agent = _setup_owner(tmp_path)
    job, _ = repository.create_job(
        SchedulerJobCreateRequest(
            name="gen-guard-job",
            prompt="v1 prompt",
            thread_id="thread-1",
            schedule={"kind": "every", "every_seconds": 300, "anchor_at": 1000.0},
            now=1000.0,
            policy={
                "on_failure": {
                    "investigate": True,
                    "after_failures": 1,
                    "retry_once": True,
                    "prompt": "只读调查。",
                }
            },
        )
    )
    assert job["definition_generation"] == 1

    # 仅更新名称不改变定义代次；新增/修改 postcheck 必须推进 definition_generation。
    renamed = repository.update_job(
        job["job_id"],
        patch={"name": "gen-guard-job-renamed"},
        expected_version=job["version"],
        now=1001.0,
    )
    assert renamed["definition_generation"] == 1

    with_postcheck = repository.update_job(
        job["job_id"],
        patch={"postcheck": {"artifacts": [{"path": "out.txt", "min_bytes": 4}]}},
        expected_version=renamed["version"],
        now=1002.0,
    )
    assert with_postcheck["definition_generation"] == 2

    # 先领取两个代次 2（不同前后顺序）的运行：领取 gen-2 run_stale_fail 后，将任务升级到代次 3。
    run_stale_fail = repository.reserve_manual_run(job["job_id"], now=1010.0)
    claim_stale_fail = _claim(repository, str(run_stale_fail["run_id"]), now=1011.0)
    repository.mark_run_running(claim_stale_fail.run_id, claim_stale_fail.claim_id, now=1011.0)

    gen3_job = repository.update_job(
        job["job_id"],
        patch={
            "prompt": "v3 replacement business prompt",
            "postcheck": {"artifacts": [{"path": "out-v3.txt", "min_bytes": 8}]},
        },
        expected_version=with_postcheck["version"],
        now=1012.0,
    )
    assert gen3_job["definition_generation"] == 3

    # 旧代次 2 的失败运行收口：只写历史，绝不创建代次 3 的 failure_streak、调查或带新 prompt 的重试！
    term_stale = service.finish(
        claim_stale_fail,
        status="failed",
        error_code="SCRIPT_EXIT_NONZERO",
        now=1015.0,
    )
    assert term_stale is not None
    assert term_stale["definition_generation"] == 2
    after_stale_fail = repository.get_job(job["job_id"])
    assert after_stale_fail.get("failure_streak") is None
    assert repository.queued_runs(now=2000.0) == []

    # 测试晚到的旧代次成功（done）绝不清除已存在的 failure_streak；唯有当前代次成功才可清除。
    gen4_job = repository.update_job(
        job["job_id"],
        patch={
            "prompt": "v4 business prompt",
            "policy": {"on_failure": {"investigate": True, "after_failures": 2, "retry_once": False}},
        },
        expected_version=after_stale_fail["version"],
        now=1020.0,
    )
    assert gen4_job["definition_generation"] == 4

    # 先让代次 4 产生 1 次真实失败，建立 failure_streak(count=1)。
    run_gen4_fail = repository.reserve_manual_run(job["job_id"], now=1021.0)
    claim_gen4_fail = _claim(repository, str(run_gen4_fail["run_id"]), now=1022.0)
    service.finish(claim_gen4_fail, status="failed", error_code="E_GEN4", now=1023.0)
    assert repository.get_job(job["job_id"])["failure_streak"]["count"] == 1

    # 预约并启动另一轮代次 4 运行，在其在途期间将任务定义升级到代次 5。
    run_stale_done = repository.reserve_manual_run(job["job_id"], now=1025.0)
    claim_stale_done = _claim(repository, str(run_stale_done["run_id"]), now=1026.0)
    repository.mark_run_running(claim_stale_done.run_id, claim_stale_done.claim_id, now=1026.0)

    gen5_job = repository.update_job(
        job["job_id"],
        patch={"prompt": "v5 replacement business prompt"},
        expected_version=repository.get_job(job["job_id"])["version"],
        now=1027.0,
    )
    assert gen5_job["definition_generation"] == 5

    # 旧代次 4 的在途成功运行晚到收口：不得清除代次 5 上的 failure_streak！
    service.finish(claim_stale_done, status="done", response="late old gen ok", now=1030.0)
    after_stale_done = repository.get_job(job["job_id"])
    assert after_stale_done["failure_streak"] is not None
    assert after_stale_done["failure_streak"]["count"] == 1
    assert after_stale_done["failure_streak"]["started_run_id"] == run_gen4_fail["run_id"]

    # 当前代次 5 的真实成功运行（产物存在且满足 postcheck）收口：正常清除 failure_streak。
    (_ws / "out-v3.txt").write_text("healthy-v5-output", encoding="utf-8")
    run_gen5_done = repository.reserve_manual_run(job["job_id"], now=1032.0)
    claim_gen5_done = _claim(repository, str(run_gen5_done["run_id"]), now=1033.0)
    service.finish(claim_gen5_done, status="done", response="current gen ok", now=_ws.joinpath("out-v3.txt").stat().st_mtime + 1.0)
    assert repository.get_job(job["job_id"])["failure_streak"] is None

    # 显式取消（cancel_job_runs）后的在途运行失败不得递增新 streak 或触发重试。
    job_cancel, _ = repository.create_job(
        SchedulerJobCreateRequest(
            name="cancel-guard-job",
            prompt="run cancel check",
            thread_id="thread-1",
            schedule={"kind": "every", "every_seconds": 300, "anchor_at": 1000.0},
            now=1000.0,
            policy={"on_failure": {"investigate": True, "after_failures": 1, "retry_once": True}},
        )
    )
    r_can = repository.reserve_manual_run(job_cancel["job_id"], now=1010.0)
    c_can = _claim(repository, str(r_can["run_id"]), now=1011.0)
    repository.mark_run_running(c_can.run_id, c_can.claim_id, now=1011.0)
    repository.cancel_job_runs(job_cancel["job_id"], expected_version=job_cancel["version"])
    service.finish(c_can, status="failed", error_code="CANCELLED_MIDWAY", now=1015.0)
    assert repository.get_job(job_cancel["job_id"]).get("failure_streak") is None
    assert repository.queued_runs(now=2000.0) == []

    # 同事务触发自动暂停（failure_pause_after=1）的当前代次失败：必须预约单次调查，且不预约 retry_once；
    # 随后在 paused 状态下完成该准确 investigation_run_id 时，investigation_status 正常更新为 done。
    job_autopause, _ = repository.create_job(
        SchedulerJobCreateRequest(
            name="autopause-investigate-job",
            prompt="run autopause check",
            thread_id="thread-1",
            schedule={"kind": "every", "every_seconds": 300, "anchor_at": 1000.0},
            now=1000.0,
            policy={
                "failure_pause_after": 1,
                "on_failure": {"investigate": True, "after_failures": 1, "retry_once": True},
            },
        )
    )
    r_ap = repository.reserve_manual_run(job_autopause["job_id"], now=1010.0)
    c_ap = _claim(repository, str(r_ap["run_id"]), now=1011.0)
    service.finish(c_ap, status="failed", error_code="E_AUTOPAUSE", now=1015.0)
    paused_state = repository.get_job(job_autopause["job_id"])
    assert paused_state["status"] == "paused"
    assert paused_state["failure_streak"]["count"] == 1
    assert paused_state["failure_streak"]["retry_run_id"] == ""
    inv_ap_id = paused_state["failure_streak"]["investigation_run_id"]
    assert inv_ap_id

    c_inv_ap = _claim(repository, inv_ap_id, now=1016.0)
    service.finish(c_inv_ap, status="done", response="investigated after autopause", now=1020.0)
    final_ap = repository.get_job(job_autopause["job_id"])
    assert final_ap["status"] == "paused"
    assert final_ap["failure_streak"]["investigation_run_id"] == inv_ap_id
    assert final_ap["failure_streak"]["investigation_status"] == "done"


def test_p1_07_composite_monitor_rule_postcheck_and_independent_rule_streaks(tmp_path) -> None:
    """P1-07 / F05 + F06：复合监控 rules[] 支持 postcheck 校验/迁移，且按准确 rule_id 独立维护 streak 与重试/调查。"""
    workspace, _store, repository, service, agent = _setup_owner(tmp_path)
    tool = ScheduleTool(agent)
    surface = tool_model_spec_to_anthropic_tool(tool.model_spec)["input_schema"]

    # 1. 非法 rule postcheck 在登记前即被拒绝。
    with pytest.raises(ScheduleValidationError, match="postcheck"):
        repository.create_job(
            SchedulerJobCreateRequest(
                name="bad-rule-postcheck",
                prompt="",
                thread_id="thread-1",
                schedule={},
                now=1000.0,
                rules=[
                    {
                        "id": "r_bad",
                        "name": "非法后验",
                        "use_llm": True,
                        "prompt": "check",
                        "schedule": {"kind": "every", "every_seconds": 300, "anchor_at": 1000.0},
                        "postcheck": {"artifacts": []},
                    }
                ],
            )
        )

    # 2. 通过公开 ScheduleTool 创建包含两项独立规则的复合监控（r_probe 纯工具 + r_report 带 postcheck 与 on_failure）。
    create_params = {
        "action": "create",
        "name": "composite-monitor",
        "rules": [
            {
                "id": "r_probe",
                "name": "高频探测",
                "use_llm": False,
                "tool": {"name": "read_file", "arguments": {"path": "probe.json"}},
                "schedule": {"kind": "every", "every_seconds": 120, "anchor_at": 1000.0, "timezone": "UTC"},
                "policy": {"on_failure": {"investigate": False, "retry_once": False}},
            },
            {
                "id": "r_report",
                "name": "产物生成与核验",
                "use_llm": True,
                "prompt": "生成 daily-report.json 并确保 valid=true",
                "schedule": {"kind": "every", "every_seconds": 600, "anchor_at": 1000.0, "timezone": "UTC"},
                "postcheck": {
                    "artifacts": [
                        {
                            "path": "daily-report.json",
                            "min_bytes": 2,
                            "json_pointer": "/valid",
                            "equals": True,
                        }
                    ]
                },
                "policy": {
                    "failure_backoff_seconds": 90,
                    "on_failure": {
                        "investigate": True,
                        "after_failures": 1,
                        "retry_once": True,
                        "prompt": "只读核查 r_report 缺失产物。",
                    },
                },
            },
        ],
    }
    validation = validate_tool_input(create_params, surface)
    assert validation.ok is True, validation.issues
    created = json.loads(tool.execute(create_params).output)
    assert created["ok"] is True
    job_id = created["job"]["job_id"]

    persisted = repository.get_job(job_id)
    assert "policy" not in persisted
    assert "prompt" not in persisted
    assert "postcheck" not in persisted
    assert persisted.get("failure_streak") is None
    rules_by_id = {r["id"]: r for r in persisted["rules"]}
    assert rules_by_id["r_probe"]["postcheck"] is None
    assert rules_by_id["r_report"]["postcheck"]["artifacts"][0]["path"] == "daily-report.json"
    assert rules_by_id["r_report"]["definition_generation"] == 1

    # 3. r_report 运行成功返回，但 daily-report.json 缺失 -> postcheck 判为 failed，触发 r_report 专属调查与重试。
    r_rep_1 = repository.reserve_manual_run(job_id, rule_id="r_report", now=1010.0)
    assert r_rep_1["postcheck"]["artifacts"][0]["path"] == "daily-report.json"
    c_rep_1 = _claim(repository, str(r_rep_1["run_id"]), now=1011.0)
    term_rep_1 = service.finish(c_rep_1, status="done", response="claimed done without file", now=1015.0)
    assert term_rep_1 is not None
    assert term_rep_1["status"] == "failed"
    assert term_rep_1["error_code"] == "SCHEDULER_POSTCHECK_FAILED"

    after_rep_fail = repository.get_job(job_id)
    assert after_rep_fail.get("failure_streak") is None
    rules_after_fail = {r["id"]: r for r in after_rep_fail["rules"]}
    assert rules_after_fail["r_probe"].get("failure_streak") is None
    rep_streak = rules_after_fail["r_report"]["failure_streak"]
    assert rep_streak["count"] == 1
    inv_id = rep_streak["investigation_run_id"]
    retry_id = rep_streak["retry_run_id"]
    assert inv_id and retry_id

    inv_run = repository.get_active_run(inv_id)
    retry_run = repository.get_active_run(retry_id)
    assert inv_run is not None and retry_run is not None
    assert inv_run["rule_id"] == "r_report"
    assert inv_run["trigger"] == "failure_investigation"
    assert inv_run["prompt"] == "只读核查 r_report 缺失产物。"
    assert retry_run["rule_id"] == "r_report"
    assert retry_run["trigger"] == "failure_retry"
    assert retry_run["use_llm"] is True
    assert retry_run["prompt"] == "生成 daily-report.json 并确保 valid=true"
    assert retry_run["postcheck"]["artifacts"][0]["path"] == "daily-report.json"
    assert retry_run["scheduled_for"] == pytest.approx(1105.0)

    # 4. 另一条规则 r_probe 独立运行并成功（done）：绝不清除 r_report 的 failure_streak！
    r_prb_1 = repository.reserve_manual_run(job_id, rule_id="r_probe", now=1020.0)
    c_prb_1 = _claim(repository, str(r_prb_1["run_id"]), now=1021.0)
    service.finish(c_prb_1, status="done", response='{"ok": true}', now=1022.0)

    after_probe_done = repository.get_job(job_id)
    rules_after_probe = {r["id"]: r for r in after_probe_done["rules"]}
    assert rules_after_probe["r_probe"].get("failure_streak") is None
    assert rules_after_probe["r_report"]["failure_streak"] == rep_streak

    # 5. 完成 r_report 的调查运行，并重启仓库重新加载校验持久化完整性。
    c_inv = _claim(repository, inv_id, now=1025.0)
    service.finish(c_inv, status="done", response="missing daily-report.json", now=1028.0)

    reloaded_repo = SchedulerRepository(
        tmp_path / "scheduler",
        owner_provider="feishu",
        owner_kind="user",
        owner_id="user-1",
        default_timezone="Asia/Shanghai",
    )
    assert reloaded_repo.corruption_report()["corrupt"] is False
    reloaded_rules = {r["id"]: r for r in reloaded_repo.get_job(job_id)["rules"]}
    assert reloaded_rules["r_report"]["failure_streak"]["investigation_status"] == "done"
    assert reloaded_rules["r_report"]["failure_streak"]["count"] == 1

    # 6. 写入合法产物后完成 retry_run -> postcheck 通过，r_report 的 failure_streak 归零清除。
    (workspace / "daily-report.json").write_text('{"valid": true}\n', encoding="utf-8")
    c_retry = _claim(repository, retry_id, now=1106.0)
    term_retry = service.finish(c_retry, status="done", response="fixed", now=1108.0)
    assert term_retry is not None and term_retry["status"] == "done"
    cleared_rules = {r["id"]: r for r in repository.get_job(job_id)["rules"]}
    assert cleared_rules["r_report"]["failure_streak"] is None

    # 7. 修改单条规则的 postcheck 只递增该规则的 definition_generation，并测试单任务向复合规则迁移保留 postcheck。
    updated_rules = []
    for raw_rule in create_params["rules"]:
        copied = dict(raw_rule)
        if copied["id"] == "r_report":
            copied["postcheck"] = {"artifacts": [{"path": "daily-report-v2.json", "min_bytes": 4}]}
        updated_rules.append(copied)
    cur_job = repository.get_job(job_id)
    bumped_job = repository.update_job(
        job_id,
        patch={"rules": updated_rules},
        expected_version=cur_job["version"],
        now=1200.0,
    )
    bumped_rules = {r["id"]: r for r in bumped_job["rules"]}
    assert bumped_rules["r_probe"]["definition_generation"] == 1
    assert bumped_rules["r_report"]["definition_generation"] == 2

    from agent_py_agent.agent.scheduler.monitor_state import update_monitor

    simple_job, _ = repository.create_job(
        SchedulerJobCreateRequest(
            name="simple-to-migrate",
            prompt="simple prompt",
            thread_id="thread-1",
            schedule={"kind": "every", "every_seconds": 300, "anchor_at": 1000.0},
            execution={"mode": "isolated"},
            now=1000.0,
            postcheck={"artifacts": [{"path": "migrated.txt", "min_bytes": 1}]},
        )
    )
    # 直接通过 update_monitor 隐式将单任务迁入 rule1（不显式传 patch['rules']），验证 _ACTION_FIELDS 自动搬迁 postcheck。
    auto_migrated = update_monitor(simple_job, {"name": "auto-migrated-monitor"}, 1005.0)
    assert "postcheck" not in auto_migrated
    assert auto_migrated["rules"][0]["postcheck"]["artifacts"][0]["path"] == "migrated.txt"

    # 通过公开 repository.update_job 迁移为复合规则并验证下一次预约冻结准确的 postcheck 与代次。
    migrated = repository.update_job(
        simple_job["job_id"],
        patch={
            "name": "migrated-monitor",
            "rules": [
                {
                    "id": "rule1",
                    "name": "migrated-rule",
                    "use_llm": True,
                    "prompt": "simple prompt",
                    "execution": {"mode": "isolated"},
                    "schedule": {"kind": "every", "every_seconds": 300, "anchor_at": 1000.0},
                    "postcheck": {"artifacts": [{"path": "migrated.txt", "min_bytes": 1}]},
                }
            ],
        },
        expected_version=simple_job["version"],
        now=1005.0,
    )
    assert "postcheck" not in migrated
    assert migrated["rules"][0]["postcheck"]["artifacts"][0]["path"] == "migrated.txt"
    assert migrated["rules"][0]["definition_generation"] == 2
    migrated_run = repository.reserve_manual_run(simple_job["job_id"], rule_id="rule1", now=1006.0)
    assert migrated_run["definition_generation"] == 2
    assert migrated_run["postcheck"]["artifacts"][0]["path"] == "migrated.txt"


def test_f08_event_source_retry_preserves_frozen_batch_and_leaves_newer_events_pending(tmp_path) -> None:
    """F08：event_source 确定失败触发 retry_once 时，原样继承冻结的 event_batch、代次与渲染后 prompt，不消费新到事件。"""
    _ws, _store, thread, repository, service = _setup_group_event_env(tmp_path)
    job, _ = repository.create_job(
        SchedulerJobCreateRequest(
            name="event-retry-job",
            prompt="研判外部告警消息",
            thread_id=thread.thread_id,
            schedule=_event_schedule(),
            creator={"channel": "feishu", "actor_id": "human-1"},
            now=1000.0,
            policy={
                "failure_backoff_seconds": 60,
                "on_failure": {
                    "investigate": True,
                    "after_failures": 1,
                    "retry_once": True,
                    "prompt": "只读核查事件处理失败原因。",
                },
            },
        )
    )

    # 投递第 1 条事件并预约 event run。
    assert (
        repository.accept_source_event(
            _synthetic_event("msg-origin-1", content="ORIGIN_BATCH_PAYLOAD_1", created_at=1005.0),
            now=1005.0,
        )["status"]
        == "accepted"
    )
    reserved = repository.reserve_event_runs(now=1010.0)
    assert len(reserved) == 1
    orig_run = reserved[0]
    assert [item["message_id"] for item in orig_run["event_batch"]] == ["msg-origin-1"]
    assert "ORIGIN_BATCH_PAYLOAD_1" in orig_run["prompt"]

    claim1 = _claim(repository, str(orig_run["run_id"]), now=1011.0)
    repository.mark_run_running(claim1.run_id, claim1.claim_id, now=1011.0)

    # 在首轮事件运行期间，新到 2 条后续事件进入源队列 pending。
    for idx in (2, 3):
        outcome = repository.accept_source_event(
            _synthetic_event(f"msg-newer-{idx}", content=f"NEWER_PAYLOAD_{idx}", created_at=1012.0 + idx),
            now=1012.0 + idx,
        )
        assert outcome["status"] == "accepted"

    # 首轮运行确定性失败 -> 同时预约只读调查与携带原事件批次的单次重试。
    service.finish(claim1, status="failed", error_code="SCHEDULER_EXECUTION_TARGET_FAILED", now=1020.0)

    job_after_fail = repository.get_job(job["job_id"])
    assert [e["message_id"] for e in job_after_fail["event_source_state"][""]["pending"]] == [
        "msg-newer-2",
        "msg-newer-3",
    ]
    streak = job_after_fail["failure_streak"]
    inv_id = streak["investigation_run_id"]
    retry_id = streak["retry_run_id"]
    assert inv_id and retry_id

    inv_run = repository.get_active_run(inv_id)
    assert inv_run is not None
    assert "event_source" not in inv_run
    assert "event_batch" not in inv_run
    assert inv_run["failure_context"]["effect_unknown"] is False

    retry_run = repository.get_active_run(retry_id)
    assert retry_run is not None
    assert retry_run["trigger"] == "failure_retry"
    assert retry_run["event_source"] == orig_run["event_source"]
    assert retry_run["definition_generation"] == orig_run["event_source"]["generation"]
    assert retry_run["event_batch"] == orig_run["event_batch"]
    assert [item["message_id"] for item in retry_run["event_batch"]] == ["msg-origin-1"]
    assert retry_run["prompt"] == orig_run["prompt"]
    assert "ORIGIN_BATCH_PAYLOAD_1" in retry_run["prompt"]
    assert "NEWER_PAYLOAD_2" not in retry_run["prompt"]
    assert repository.event_run_is_current(retry_run) is True

    # 完成调查运行；重试仍在队列中时，新事件保持在 pending 不被提前抽走。
    c_inv = _claim(repository, inv_id, now=1021.0)
    service.finish(c_inv, status="done", response="investigated event failure", now=1022.0)
    assert repository.reserve_event_runs(now=1079.0) == []

    # 同一失败链中若 retry 再次失败，不得产生第二次重试。
    c_retry = _claim(repository, retry_id, now=1081.0)
    repository.mark_run_running(c_retry.run_id, c_retry.claim_id, now=1081.0)
    service.finish(c_retry, status="failed", error_code="SCHEDULER_EXECUTION_TARGET_FAILED", now=1085.0)
    job_after_retry_fail = repository.get_job(job["job_id"])
    assert job_after_retry_fail["failure_streak"]["count"] == 2
    assert job_after_retry_fail["failure_streak"]["retry_run_id"] == retry_id
    assert repository.queued_runs(now=2000.0) == []

    # 退避结束后，之前积压的 2 条新事件正常按序预约出下一批 event run。
    next_batch_runs = repository.reserve_event_runs(now=1300.0)
    assert len(next_batch_runs) == 1
    assert [item["message_id"] for item in next_batch_runs[0]["event_batch"]] == [
        "msg-newer-2",
        "msg-newer-3",
    ]


def test_composite_rule_autopause_disabled_rule_and_event_rule_retry(tmp_path) -> None:
    """验证复合监控规则同事务自动暂停后的调查状态回写、停用规则拒绝重试，以及事件规则的 postcheck 代次与重试批次继承。"""
    _ws, _store, thread, repository, service = _setup_group_event_env(tmp_path)
    rules = [
        {
            "id": "r_autopause",
            "name": "单次失败暂停规则",
            "use_llm": True,
            "prompt": "执行关键巡检",
            "schedule": {"kind": "every", "every_seconds": 300, "anchor_at": 1000.0, "timezone": "UTC"},
            "policy": {
                "failure_pause_after": 1,
                "on_failure": {"investigate": True, "after_failures": 1, "retry_once": True},
            },
        },
        {
            "id": "r_event",
            "name": "群事件规则",
            "use_llm": True,
            "prompt": "研判规则事件",
            "schedule": _event_schedule(),
            "postcheck": {"artifacts": [{"path": "evt-out.txt", "min_bytes": 1}]},
            "policy": {"on_failure": {"investigate": False, "retry_once": True}},
        },
    ]
    job, _ = repository.create_job(
        SchedulerJobCreateRequest(
            name="composite-event-and-autopause",
            prompt="",
            thread_id=thread.thread_id,
            schedule={},
            creator={"channel": "feishu", "actor_id": "human-1"},
            rules=rules,
            now=1000.0,
        )
    )

    # 1. r_autopause 失败触发同事务停用该规则：仍预约调查，不预约重试；规则已停用时完成调查仍能更新 investigation_status。
    r_ap = repository.reserve_manual_run(job["job_id"], rule_id="r_autopause", now=1005.0)
    c_ap = _claim(repository, str(r_ap["run_id"]), now=1006.0)
    service.finish(c_ap, status="failed", error_code="E_RULE_PAUSE", now=1010.0)
    state_1 = {r["id"]: r for r in repository.get_job(job["job_id"])["rules"]}
    assert state_1["r_autopause"]["enabled"] is False
    assert state_1["r_autopause"]["failure_streak"]["retry_run_id"] == ""
    inv_id = state_1["r_autopause"]["failure_streak"]["investigation_run_id"]
    assert inv_id
    c_inv = _claim(repository, inv_id, now=1011.0)
    service.finish(c_inv, status="done", response="rule autopause root cause", now=1012.0)
    state_2 = {r["id"]: r for r in repository.get_job(job["job_id"])["rules"]}
    assert state_2["r_autopause"]["failure_streak"]["investigation_status"] == "done"

    # 2. r_event 事件规则确定失败时继承冻结批次；随后修改其 postcheck 会推进 event_source_state['r_event'].generation。
    repository.accept_source_event(
        _synthetic_event("msg-rule-evt-1", content="RULE_EVENT_CONTENT_1", created_at=1015.0),
        now=1015.0,
    )
    evt_run = repository.reserve_event_runs(now=1020.0)[0]
    assert evt_run["rule_id"] == "r_event"
    c_evt = _claim(repository, str(evt_run["run_id"]), now=1021.0)
    repository.mark_run_running(c_evt.run_id, c_evt.claim_id, now=1021.0)
    service.finish(c_evt, status="failed", error_code="E_EVT_FAIL", now=1025.0)

    state_3 = {r["id"]: r for r in repository.get_job(job["job_id"])["rules"]}
    retry_evt_id = state_3["r_event"]["failure_streak"]["retry_run_id"]
    assert retry_evt_id
    retry_evt = repository.get_active_run(retry_evt_id)
    assert retry_evt is not None
    assert retry_evt["rule_id"] == "r_event"
    assert [e["message_id"] for e in retry_evt["event_batch"]] == ["msg-rule-evt-1"]
    assert "RULE_EVENT_CONTENT_1" in retry_evt["prompt"]

    cur_job = repository.get_job(job["job_id"])
    assert cur_job["event_source_state"]["r_event"]["generation"] == 1
    patched_rules = [dict(r) for r in rules]
    patched_rules[0]["enabled"] = False
    patched_rules[1] = {
        **patched_rules[1],
        "postcheck": {"artifacts": [{"path": "evt-out-v2.txt", "min_bytes": 2}]},
    }
    updated_job = repository.update_job(
        job["job_id"],
        patch={"rules": patched_rules},
        expected_version=cur_job["version"],
        now=1030.0,
    )
    assert updated_job["event_source_state"]["r_event"]["generation"] == 2
    assert repository.event_run_is_current(retry_evt) is False
