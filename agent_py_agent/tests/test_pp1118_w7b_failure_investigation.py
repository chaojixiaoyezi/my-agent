from __future__ import annotations

import json
from contextlib import closing, contextmanager
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.backends.tool_schema import tool_model_spec_to_anthropic_tool
from agent_py_agent.agent.contracts.tool_input_schema import validate_tool_input
from agent_py_agent.agent.conversation.models import ChannelBinding, ConversationThread, WakeSignal
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.scheduler import execution as sched_exec
from agent_py_agent.agent.scheduler import service as service_module
from agent_py_agent.agent.scheduler.execution import execute_scheduled_target, with_scheduled_work
from agent_py_agent.agent.scheduler.policy import apply_run_policy, normalize_policy
from agent_py_agent.agent.scheduler.postcheck import snapshot_execution_root
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


class _SkillSnapshotStub:
    @staticmethod
    def resolve(names):
        return [], list(names)


@dataclass(frozen=True)
class _ScheduledDeps:
    run_once: object
    terminal_task: object


def _make_agent(
    tmp_path,
    *,
    cwd: str = "",
    owner_kind: str = "user",
    owner_id: str = "user-1",
    with_group_binding: bool = False,
):
    store = ConversationStore(tmp_path / "conversation")
    bindings = (
        (
            ChannelBinding(
                channel="feishu",
                channel_conversation_id="group-1",
                channel_user_id="human-1",
                canonical_user_id="user-1",
                thread_id="thread-1",
            ),
        )
        if with_group_binding
        else ()
    )
    store.threads.write(
        ConversationThread(
            thread_id="thread-1",
            canonical_user_id="user-1",
            cwd=cwd or str(tmp_path / "workspace"),
            channel_bindings=bindings,
        )
    )
    repository = SchedulerRepository(
        tmp_path / "scheduler",
        owner_provider="feishu",
        owner_kind=owner_kind,
        owner_id=owner_id,
        default_timezone="Asia/Shanghai",
    )
    agent = SimpleNamespace(
        scheduler_repository=repository,
        config=SimpleNamespace(timezone="Asia/Shanghai", retention_scan_slice_bytes=4096),
        _current_run_params=RunParams(
            request_id="request-1",
            task_id="task-1",
            task_attributes={"conversation_thread_id": "thread-1", "conversation_task_id": "task-1"},
        ),
        current_skill_snapshot=lambda: _SkillSnapshotStub(),
        conversation_store=store,
    )
    return agent, store, repository


def _setup_service(
    tmp_path,
    *,
    read_bytes: int = 8388608,
    owner_kind: str = "user",
    owner_id: str = "user-1",
    with_group_binding: bool = False,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    agent, store, repository = _make_agent(
        tmp_path,
        cwd=str(workspace),
        owner_kind=owner_kind,
        owner_id=owner_id,
        with_group_binding=with_group_binding,
    )
    repository.config = SimpleNamespace(retention_scan_slice_bytes=read_bytes)
    service = SchedulerService(repository, conversation_store=store)
    return workspace, store, repository, service, agent


def _claim(repository: SchedulerRepository, run_id: str, *, now: float, execution_root=None) -> SchedulerRunClaim:
    row = repository.claim_run(run_id, lease_seconds=300, now=now)
    assert row is not None
    if execution_root is not None:
        repository.mark_run_running(run_id, str(row["claim_id"]), now=now,
                                    execution_root_ref=snapshot_execution_root(execution_root))
    return SchedulerRunClaim(run_id=str(row["run_id"]), claim_id=str(row["claim_id"]), run=row)


def _assert_no_pending_runs(repository: SchedulerRepository) -> None:
    assert repository.queued_runs() == []
    active, _errs = repository.active_runs_by_job()
    assert active == {}


def _payload(outcome) -> dict[str, object]:
    return json.loads(outcome.output)


@contextmanager
def _stub_required_notice_none():
    from agent_py_agent.agent.conversation import required_user_action

    original = required_user_action.required_notice
    required_user_action.required_notice = lambda agent, req: None
    try:
        yield
    finally:
        required_user_action.required_notice = original


def test_service_finish_postcheck_outcomes_and_unlocked_evaluation(tmp_path, monkeypatch) -> None:
    workspace, _store, repository, service, _agent = _setup_service(tmp_path, read_bytes=128)
    artifact = workspace / "report.txt"

    job_req = SchedulerJobCreateRequest(
        name="postcheck-job",
        prompt="generate report",
        thread_id="thread-1",
        schedule={"kind": "every", "every_seconds": 300, "anchor_at": 1000.0},
        now=1000.0,
        postcheck={
            "artifacts": [
                {
                    "path": "report.txt",
                    "min_bytes": 5,
                    "max_age_seconds": 30,
                }
            ]
        },
    )
    job, _ = repository.create_job(job_req)

    # 1. Passing postcheck keeps status=done and evaluates outside _mutation_scope lock.
    artifact.write_text("healthy output payload", encoding="utf-8")
    events: list[str] = []
    lock_depth = 0
    original_scope = repository._mutation_scope
    real_eval = service_module.evaluate_postcheck

    @contextmanager
    def _traced_scope():
        nonlocal lock_depth
        events.append("lock_enter")
        lock_depth += 1
        try:
            with original_scope() as ctx:
                yield ctx
        finally:
            lock_depth -= 1
            events.append("lock_exit")

    def _traced_eval(spec, *, root, now, read_bytes):
        assert lock_depth == 0, "evaluate_postcheck MUST run outside repository._mutation_scope"
        events.append("evaluate_postcheck")
        return real_eval(spec, root=root, now=now, read_bytes=read_bytes)

    monkeypatch.setattr(repository, "_mutation_scope", _traced_scope)
    monkeypatch.setattr(service_module, "evaluate_postcheck", _traced_eval)

    run1 = repository.reserve_manual_run(job["job_id"], now=1010.0)
    claim1 = _claim(repository, run1["run_id"], now=1011.0, execution_root=workspace)
    term1 = service.finish(claim1, status="done", response="ok", now=artifact.stat().st_mtime + 1.0)
    assert term1 is not None
    assert term1["status"] == "done"
    assert term1["postcheck_observation"]["status"] == "passed"
    idx_eval = events.index("evaluate_postcheck")
    idx_first_lock_after = next(i for i, item in enumerate(events) if item == "lock_enter" and i > idx_eval)
    assert idx_eval < idx_first_lock_after

    # 2. Missing file -> SCHEDULER_POSTCHECK_FAILED / business.
    artifact.unlink()
    run2 = repository.reserve_manual_run(job["job_id"], now=1020.0)
    claim2 = _claim(repository, run2["run_id"], now=1021.0, execution_root=workspace)
    term2 = service.finish(claim2, status="done", response="ok", now=1025.0)
    assert term2 is not None
    assert term2["status"] == "failed"
    assert term2["error_code"] == "SCHEDULER_POSTCHECK_FAILED"
    assert term2["failure_kind"] == "business"
    assert term2["postcheck_observation"]["status"] == "failed"

    # 3. Stale output -> SCHEDULER_OUTPUT_STALLED / stalled.
    artifact.write_text("stale payload", encoding="utf-8")
    run3 = repository.reserve_manual_run(job["job_id"], now=1100.0)
    claim3 = _claim(repository, run3["run_id"], now=1101.0, execution_root=workspace)
    far_future = artifact.stat().st_mtime + 100.0
    term3 = service.finish(claim3, status="done", response="ok", now=far_future)
    assert term3 is not None
    assert term3["status"] == "failed"
    assert term3["error_code"] == "SCHEDULER_OUTPUT_STALLED"
    assert term3["failure_kind"] == "stalled"
    assert term3["postcheck_observation"]["status"] == "stalled"

    # 4. Ambiguous / unavailable postcheck -> unknown / SCHEDULER_POSTCHECK_UNAVAILABLE.
    monkeypatch.setattr(
        service_module,
        "evaluate_postcheck",
        lambda spec, *, root, now, read_bytes: {
            "status": "unknown",
            "failure_kind": "postcheck_unavailable",
            "checks": [{"path": "report.txt", "code": "ARTIFACT_UNREADABLE", "observed": {"bytes": 0}}],
            "evaluated_at": now,
        },
    )
    run4 = repository.reserve_manual_run(job["job_id"], now=1200.0)
    claim4 = _claim(repository, run4["run_id"], now=1201.0, execution_root=workspace)
    term4 = service.finish(claim4, status="done", response="ok", now=1205.0)
    assert term4 is not None
    assert term4["status"] == "unknown"
    assert term4["error_code"] == "SCHEDULER_POSTCHECK_UNAVAILABLE"
    assert term4["failure_kind"] == "postcheck_unavailable"


def test_single_failure_investigation_per_streak_and_reset_on_done(tmp_path) -> None:
    _workspace, _store, repository, service, _agent = _setup_service(tmp_path)
    job, _ = repository.create_job(
        SchedulerJobCreateRequest(
            name="streak-job",
            prompt="run main work",
            thread_id="thread-1",
            schedule={"kind": "every", "every_seconds": 300, "anchor_at": 1000.0},
            now=1000.0,
            policy={
                "failure_backoff_seconds": 120,
                "on_failure": {
                    "investigate": True,
                    "after_failures": 2,
                    "retry_once": False,
                    "prompt": "只读核查最近错误并输出建议。",
                },
            },
            execution_target={
                "kind": "script",
                "asset_id": "job_script",
                "execution_definition_ref": {
                    "schema_version": "scheduler_execution_definition.v1",
                    "kind": "script",
                    "asset_id": "job_script",
                    "definition_sha256": "a" * 64,
                },
            },
        )
    )

    # Failure #1: count=1 < after_failures=2 -> no investigation yet.
    run1 = repository.reserve_manual_run(job["job_id"], now=1010.0)
    c1 = _claim(repository, run1["run_id"], now=1011.0)
    service.finish(c1, status="failed", error_code="SCRIPT_EXIT_NONZERO", now=1015.0)
    job_after_1 = repository.get_job(job["job_id"])
    assert job_after_1 is not None
    assert job_after_1["failure_streak"]["count"] == 1
    assert job_after_1["failure_streak"]["investigation_run_id"] == ""
    _assert_no_pending_runs(repository)

    # Failure #2: count=2 >= 2 -> exactly 1 failure_investigation run reserved.
    run2 = repository.reserve_manual_run(job["job_id"], now=1020.0)
    c2 = _claim(repository, run2["run_id"], now=1021.0)
    service.finish(c2, status="failed", error_code="SCRIPT_EXIT_NONZERO", now=1025.0)
    job_after_2 = repository.get_job(job["job_id"])
    assert job_after_2 is not None
    streak2 = job_after_2["failure_streak"]
    assert streak2["count"] == 2
    assert streak2["started_run_id"] == run1["run_id"]
    assert streak2["investigation_status"] == "queued"
    inv_id = streak2["investigation_run_id"]
    assert inv_id

    inv_run = repository.get_active_run(inv_id)
    assert inv_run is not None
    assert inv_run["trigger"] == "failure_investigation"
    assert inv_run["use_llm"] is True
    assert inv_run["tool"] is None
    assert inv_run["execution_target"] is None
    assert inv_run["execution_definition_ref"] is None
    assert inv_run["precheck"] is None
    assert inv_run["postcheck"] is None
    assert inv_run["failure_context"]["failed_run_id"] == run2["run_id"]
    assert inv_run["failure_context"]["effect_unknown"] is False
    assert inv_run["failure_context"]["streak_count"] == 2

    # Completing the failure_investigation run does NOT reset consecutive_failures or trigger recursion.
    c_inv = _claim(repository, inv_id, now=1026.0)
    assert apply_run_policy(job_after_2, {"trigger": "failure_investigation", "status": "done"}, now=1030.0) == job_after_2
    service.finish(c_inv, status="done", response="root cause found", now=1030.0)
    job_after_inv = repository.get_job(job["job_id"])
    assert job_after_inv is not None
    assert job_after_inv["consecutive_failures"] == 2
    assert job_after_inv["backoff_until"] == job_after_2["backoff_until"]
    assert job_after_inv["failure_streak"]["count"] == 2
    assert job_after_inv["failure_streak"]["investigation_run_id"] == inv_id
    assert job_after_inv["failure_streak"]["investigation_status"] == "done"
    _assert_no_pending_runs(repository)

    # Failure #3 in same streak does NOT spawn a second investigation run.
    run3 = repository.reserve_manual_run(job["job_id"], now=1040.0)
    c3 = _claim(repository, run3["run_id"], now=1041.0)
    service.finish(c3, status="failed", error_code="SCRIPT_EXIT_NONZERO", now=1045.0)
    job_after_3 = repository.get_job(job["job_id"])
    assert job_after_3 is not None
    assert job_after_3["failure_streak"]["count"] == 3
    assert job_after_3["failure_streak"]["investigation_run_id"] == inv_id
    _assert_no_pending_runs(repository)

    # Subsequent non-investigation success clears failure_streak.
    run4 = repository.reserve_manual_run(job["job_id"], now=1050.0)
    c4 = _claim(repository, run4["run_id"], now=1051.0)
    service.finish(c4, status="done", response="recovered", now=1055.0)
    job_after_4 = repository.get_job(job["job_id"])
    assert job_after_4 is not None
    assert job_after_4["consecutive_failures"] == 0
    assert job_after_4["failure_streak"] is None


def test_failure_investigation_dispatches_when_paused_in_same_tx_and_on_event_source(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    identity = OwnerIdentity("feishu", "group", "group-1")
    home = home_paths_with_owner(home_paths(tmp_path), resolve_owner_home(tmp_path, identity))
    store = ConversationStore(home.owner_home_dir / "conversation")
    source_thread = store.threads.get_or_create(
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
    event_schedule = {
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
        "max_batch_events": 16,
    }
    job, _ = repository.create_job(
        SchedulerJobCreateRequest(
            name="pause-on-first-failure",
            prompt="handle event",
            thread_id=source_thread.thread_id,
            schedule=event_schedule,
            creator={"channel": "feishu", "actor_id": "human-1"},
            now=1000.0,
            policy={
                "failure_pause_after": 1,
                "on_failure": {"investigate": True, "after_failures": 1, "retry_once": True},
            },
        )
    )
    repository.accept_source_event(
        {
            "channel": "feishu",
            "event_type": "message",
            "app_id": "receiver-app",
            "tenant_key": "chat-tenant",
            "chat_id": "group-1",
            "sender_type": "bot",
            "sender_id_type": "app_id",
            "sender_id": "source-app",
            "message_id": "msg-1",
            "ingress_id": "ing-1",
            "created_at": 1005.0,
            "content": "event payload",
        },
        now=1005.0,
    )
    event_runs = repository.reserve_event_runs(now=1010.0)
    assert len(event_runs) == 1
    c1 = _claim(repository, event_runs[0]["run_id"], now=1011.0)
    service.finish(c1, status="failed", error_code="TOOL_FAILED", now=1015.0)

    paused_job = repository.get_job(job["job_id"])
    assert paused_job is not None
    assert paused_job["status"] == "paused"
    streak = paused_job["failure_streak"]
    assert streak["retry_run_id"] == ""
    inv_id = streak["investigation_run_id"]
    assert inv_id

    inv_run = repository.get_active_run(inv_id)
    assert inv_run is not None
    assert "event_source" not in inv_run
    assert "event_batch" not in inv_run

    # Must still be dispatchable even though paused in the exact same transaction.
    dispatchable = repository.dispatchable_queued_runs(now=1015.0)
    assert [row["run_id"] for row in dispatchable] == [inv_id]
    enqueued_wake_ids = service.enqueue_ready_runs(now=1015.0)
    signals = store.wakes.pending()
    assert len(enqueued_wake_ids) == 1
    assert [sig.wake_signal_id for sig in signals] == enqueued_wake_ids
    assert [sig.root_task_id for sig in signals] == [inv_id]
    wake_result = service.claim_wake(signals[0], now=1016.0)
    assert wake_result.status == "claimed"
    assert wake_result.claim is not None and wake_result.claim.run_id == inv_id


def test_retry_once_only_on_first_failed_never_on_unknown(tmp_path) -> None:
    _workspace, _store, repository, service, _agent = _setup_service(tmp_path)

    # Job A: retry_once=True on failed -> schedules exactly 1 retry at now + max(backoff, 60).
    job_a, _ = repository.create_job(
        SchedulerJobCreateRequest(
            name="retry-on-failed",
            prompt="run a",
            thread_id="thread-1",
            schedule={"kind": "every", "every_seconds": 600, "anchor_at": 1000.0},
            now=1000.0,
            policy={
                "queue_timeout_seconds": 25,
                "failure_backoff_seconds": 30,
                "on_failure": {"investigate": False, "retry_once": True},
            },
        )
    )
    run_a1 = repository.reserve_manual_run(job_a["job_id"], now=1010.0)
    ca1 = _claim(repository, run_a1["run_id"], now=1011.0)
    service.finish(ca1, status="failed", error_code="SCHEDULER_EXECUTION_TARGET_FAILED", now=1015.0)

    state_a1 = repository.get_job(job_a["job_id"])
    assert state_a1 is not None
    retry_id = state_a1["failure_streak"]["retry_run_id"]
    assert retry_id
    retry_run = repository.get_active_run(retry_id)
    assert retry_run is not None
    assert retry_run["trigger"] == "failure_retry"
    assert retry_run["dispatch_after"] == pytest.approx(1075.0)
    assert retry_run["scheduled_for"] == pytest.approx(1075.0)
    assert retry_run["queue_deadline_at"] == pytest.approx(1100.0)
    assert repository.dispatchable_queued_runs(now=1050.0) == []
    assert [row["run_id"] for row in repository.dispatchable_queued_runs(now=1075.0)] == [retry_id]

    # Retry also fails -> count=2, NO second retry queued.
    ca_retry = _claim(repository, retry_id, now=1076.0)
    service.finish(ca_retry, status="failed", error_code="SCHEDULER_EXECUTION_TARGET_FAILED", now=1080.0)
    state_a2 = repository.get_job(job_a["job_id"])
    assert state_a2 is not None
    assert state_a2["failure_streak"]["count"] == 2
    assert state_a2["failure_streak"]["retry_run_id"] == retry_id
    _assert_no_pending_runs(repository)

    # Job B: first terminal status is "unknown" -> NEVER schedules failure_retry, sets effect_unknown=True.
    job_b, _ = repository.create_job(
        SchedulerJobCreateRequest(
            name="no-retry-on-unknown",
            prompt="run b",
            thread_id="thread-1",
            schedule={"kind": "every", "every_seconds": 600, "anchor_at": 2000.0},
            now=2000.0,
            policy={"on_failure": {"investigate": True, "after_failures": 1, "retry_once": True}},
        )
    )
    run_b1 = repository.reserve_manual_run(job_b["job_id"], now=2010.0)
    cb1 = _claim(repository, run_b1["run_id"], now=2011.0)
    service.finish(cb1, status="unknown", error_code="SCHEDULER_POSTCHECK_UNAVAILABLE", now=2015.0)
    state_b1 = repository.get_job(job_b["job_id"])
    assert state_b1 is not None
    assert state_b1["failure_streak"]["count"] == 1
    assert state_b1["failure_streak"]["retry_run_id"] == ""
    inv_b1_id = state_b1["failure_streak"]["investigation_run_id"]
    assert inv_b1_id
    inv_b1 = repository.get_active_run(inv_b1_id)
    assert inv_b1 is not None
    assert inv_b1["failure_context"]["effect_unknown"] is True


def test_with_scheduled_work_injects_bounded_context_and_target_exit_code(tmp_path, monkeypatch) -> None:
    _workspace, _store, repository, service, agent = _setup_service(tmp_path)
    job, _ = repository.create_job(
        SchedulerJobCreateRequest(
            name="ctx-job",
            prompt="run",
            thread_id="thread-1",
            schedule={"kind": "every", "every_seconds": 300, "anchor_at": 1000.0},
            now=1000.0,
            policy={"on_failure": {"investigate": True, "after_failures": 1}},
        )
    )
    fail_run = repository.reserve_manual_run(job["job_id"], now=1010.0)
    fail_claim = _claim(repository, fail_run["run_id"], now=1011.0)
    service.finish(
        fail_claim,
        status="failed",
        error_code="SCHEDULER_POSTCHECK_FAILED",
        failure_kind="business",
        postcheck_observation={
            "status": "failed",
            "failure_kind": "business",
            "checks": [{"path": f"file-{i}.txt", "code": "ARTIFACT_MISSING", "observed": {"bytes": 0}} for i in range(4)],
        },
        now=1015.0,
    )
    inv_id = repository.get_job(job["job_id"])["failure_streak"]["investigation_run_id"]
    inv_claim = _claim(repository, inv_id, now=1016.0)
    inv_claim.run["failure_context"]["error_code"] = "E" * 6000

    captured = {}

    def _run_once(kwargs):
        captured["kwargs"] = kwargs
        return SimpleNamespace(status="completed")

    deps = _ScheduledDeps(run_once=_run_once, terminal_task=lambda kwargs: False)
    wrapped = with_scheduled_work(deps, agent, service, inv_claim, None, now=1016.0)
    orig_signal = WakeSignal(
        wake_signal_id="wake_1",
        thread_id="thread-1",
        metadata={"scheduler_prompt": inv_claim.run["prompt"]},
    )
    orig_kwargs = {"wake_signal": orig_signal, "request": None}

    with _stub_required_notice_none():
        wrapped.run_once(orig_kwargs)

    sent_signal = captured["kwargs"]["wake_signal"]
    injected_prompt = sent_signal.metadata["scheduler_prompt"]
    assert "[failure_context]" in injected_prompt
    assert len(injected_prompt.encode("utf-8")) < 4600
    assert orig_signal.metadata["scheduler_prompt"] == inv_claim.run["prompt"]

    # Verify execute_scheduled_target propagates integer exit_code to target_outcome & tool history.
    real_ws = tmp_path / "exec_ws"
    real_ws.mkdir(parents=True, exist_ok=True)
    real_agent = SimpleAgent(
        AgentConfig(
            my_agent_home=str(tmp_path / "exec_home"),
            orphan_supervision_interval_seconds=0,
        ),
        real_ws,
    )
    thread = real_agent.conversation_store.threads.get_or_create(
        {
            "canonical_user_id": "user-exec",
            "channel": "chat",
            "channel_conversation_id": "conv-exec",
            "channel_user_id": "u-exec",
            "cwd": str(real_ws),
            "now": 900,
        }
    )
    real_agent.scheduler_repository.create_job(
        SchedulerJobCreateRequest(
            name="target-exit-job",
            prompt="",
            thread_id=thread.thread_id,
            schedule={"kind": "every", "every_seconds": 600, "anchor_at": 1000.0, "timezone": "UTC"},
            execution={"mode": "isolated"},
            execution_target={
                "kind": "script",
                "asset_id": "exit_script",
                "pinned_version": 1,
                "input_ref": {"inputs": {}},
            },
            now=900.0,
        )
    )
    real_agent.scheduler_service.monitor_jitter_window_seconds = 0
    real_agent.scheduler_service.enqueue_ready_runs(now=1000.0)
    wake_sig = real_agent.conversation_store.wakes.pending()[0]
    tclaim = real_agent.scheduler_service.claim_wake(wake_sig, now=1000.0).claim
    assert tclaim is not None
    real_agent.scheduler_repository.mark_run_running(tclaim.run_id, tclaim.claim_id, now=1000.0)
    monkeypatch.setattr(
        sched_exec,
        "_execute_target_tool",
        lambda *_args, **_kwargs: (
            SimpleNamespace(
                result=SimpleNamespace(
                    ok=False,
                    error_code="SCRIPT_EXIT_NONZERO",
                    call_id="c-exit",
                    tool_name="scripts",
                    status="failed",
                )
            ),
            json.dumps({"exit_code": 9, "error_message": "bad exit"}),
        ),
    )
    execute_scheduled_target(
        real_agent,
        real_agent.scheduler_service,
        tclaim,
        wake_sig,
        {"thread_id": tclaim.run["thread_id"], "task_id": tclaim.run_id, "reason": "scheduled_run", "now": 1000.0},
    )
    rows, _errs = real_agent.scheduler_repository.history(limit=5)
    assert rows[0]["target_outcome"]["exit_code"] == 9
    real_agent._current_run_params = RunParams(
        request_id="req-hist",
        task_id="task-hist",
        task_attributes={"conversation_thread_id": thread.thread_id, "conversation_task_id": "task-hist"},
    )
    hist_out = _payload(ScheduleTool(real_agent).execute({"action": "history", "job_id": rows[0]["job_id"], "limit": 5}))
    assert hist_out["ok"] is True
    assert hist_out["runs"][0]["target_exit_code"] == 9


def test_history_index_filters_explain_plan_and_window_200_coverage(tmp_path) -> None:
    _workspace, _store, repository, service, agent = _setup_service(tmp_path)
    tool = ScheduleTool(agent)
    job, _ = repository.create_job(
        SchedulerJobCreateRequest(
            name="history-filter-job",
            prompt="run",
            thread_id="thread-1",
            schedule={"kind": "every", "every_seconds": 300, "anchor_at": 100.0},
            now=100.0,
        )
    )
    # Seed 205 runs: oldest 3 failed (ended_at 101..103), newest 202 done.
    for idx in range(1, 206):
        ts = float(100 + idx)
        run = repository.reserve_manual_run(job["job_id"], now=ts)
        claim = _claim(repository, run["run_id"], now=ts)
        if idx <= 3:
            service.finish(claim, status="failed", error_code="E_OLD", now=ts)
        else:
            service.finish(claim, status="done", response=f"ok-{idx}", now=ts)

    # Directly verify EXPLAIN QUERY PLAN uses existing indexes with since/until.
    index = repository._history_index
    with closing(index._connect(read_only=True)) as conn:
        for query_job_id in (job["job_id"], ""):
            sql, args = index.page_queries(
                job_id=query_job_id,
                limit=10,
                generation=1,
                since=150.0,
                until=250.0,
            )[0]
            plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, args)]
            assert not any("SCAN " in part or "TEMP B-TREE" in part for part in plan)

    # Filter by time window via repository.history & tool.
    since_iso = "1970-01-01T00:01:41Z"  # 101.0
    until_iso = "1970-01-01T00:01:45Z"  # 105.0
    ranged_rows, ranged_errors = repository.history(job_id=job["job_id"], since=101.0, until=105.0, limit=10)
    assert ranged_errors == []
    assert [row["ended_at"] for row in ranged_rows] == [105.0, 104.0, 103.0, 102.0, 101.0]
    assert repository.last_history_coverage == ""

    tool_range = _payload(
        tool.execute({"action": "history", "job_id": job["job_id"], "since": since_iso, "until": until_iso, "limit": 10})
    )
    assert tool_range["ok"] is True
    assert len(tool_range["runs"]) == 5
    assert "coverage" not in tool_range

    # 三条失败运行都排在最新 200 条之外，索引应直接定位全部匹配。
    failed_resp = _payload(
        tool.execute({"action": "history", "job_id": job["job_id"], "status": "failed", "limit": 20})
    )
    assert failed_resp["ok"] is True
    assert [row["run_id"] for row in failed_resp["runs"]] == [row["run_id"] for row in tool_range["runs"][-3:]]
    assert failed_resp["load_error_codes"] == []
    assert "coverage" not in failed_resp

    # 触发类型筛选同样使用原历史索引。
    job_inv, _ = repository.create_job(
        SchedulerJobCreateRequest(
            name="inv-history-job",
            prompt="run",
            thread_id="thread-1",
            schedule={"kind": "every", "every_seconds": 300, "anchor_at": 500.0},
            now=500.0,
            policy={"on_failure": {"investigate": True, "after_failures": 1}},
        )
    )
    r_fail = repository.reserve_manual_run(job_inv["job_id"], now=501.0)
    c_fail = _claim(repository, r_fail["run_id"], now=502.0)
    service.finish(c_fail, status="failed", error_code="E_BOOM", now=505.0)
    inv_id = repository.get_job(job_inv["job_id"])["failure_streak"]["investigation_run_id"]
    c_inv = _claim(repository, inv_id, now=506.0)
    repository.mark_run_running(c_inv.run_id, c_inv.claim_id, now=506.0)
    service.finish(c_inv, status="done", response="investigated", now=508.0)

    inv_hist = _payload(
        tool.execute({"action": "history", "job_id": job_inv["job_id"], "trigger": "failure_investigation", "limit": 10})
    )
    assert inv_hist["ok"] is True
    assert [row["run_id"] for row in inv_hist["runs"]] == [inv_id]
    assert inv_hist["runs"][0]["duration_seconds"] == pytest.approx(2.0)


def test_schedule_tool_on_failure_postcheck_schema_and_projections(tmp_path) -> None:
    _workspace, _store, repository, service, agent = _setup_service(tmp_path)
    tool = ScheduleTool(agent)
    spec = tool.model_spec
    surface = tool_model_spec_to_anthropic_tool(spec)["input_schema"]

    # Every schema property and nested property must carry a non-empty description.
    def _walk_properties(node: dict[str, object]) -> None:
        props = node.get("properties")
        if isinstance(props, dict):
            for prop_name, prop_schema in props.items():
                assert isinstance(prop_schema, dict)
                assert str(prop_schema.get("description") or "").strip(), f"missing description on {prop_name}"
                _walk_properties(prop_schema)
        items = node.get("items")
        if isinstance(items, dict):
            _walk_properties(items)

    _walk_properties(surface)

    create_params = {
        "action": "create",
        "name": "schema-and-projection",
        "prompt": "check output",
        "schedule_kind": "every",
        "every_seconds": 300,
        "policy": {
            "on_failure": {
                "investigate": True,
                "after_failures": 2,
                "retry_once": True,
                "prompt": "inspect failures",
            }
        },
        "postcheck": {
            "artifacts": [
                {
                    "path": "out.json",
                    "min_bytes": 2,
                    "json_pointer": "/ok",
                    "equals": True,
                }
            ]
        },
    }
    validate_tool_input(surface, create_params)
    created = _payload(tool.execute(create_params))
    assert created["ok"] is True
    job_proj = created["job"]
    assert job_proj["postcheck_enabled"] is True
    assert job_proj["postcheck"]["artifacts"][0]["path"] == "out.json"
    assert job_proj["policy"]["on_failure"]["after_failures"] == 2

    # Finish one failing postcheck run and inspect job get + history projections.
    job_id = job_proj["job_id"]
    run = repository.reserve_manual_run(job_id, now=2006.0)
    claim = _claim(repository, run["run_id"], now=2006.0)
    repository.mark_run_running(claim.run_id, claim.claim_id, now=2006.0,
                                execution_root_ref=snapshot_execution_root(_workspace))
    repository.record_rule_result(
        claim.run_id,
        claim.claim_id,
        {"ok": True},
        target_outcome={
            "schema": "scheduler.target-outcome.v1",
            "run_id": claim.run_id,
            "status": "succeeded",
            "response": '{"ok": true, "exit_code": 7}',
            "error_code": "",
            "error_message": "",
            "exit_code": 7,
        },
    )
    term = service.finish(claim, status="done", response="done", now=2010.0)
    assert term is not None
    assert term["status"] == "failed"

    got = _payload(tool.execute({"action": "get", "job_id": job_id}))
    assert got["job"]["failure_streak"]["count"] == 1
    assert got["job"]["failure_streak"]["started_run_id"] == claim.run_id
    assert got["job"]["postcheck_enabled"] is True

    full_hist = _payload(tool.execute({"action": "history", "job_id": job_id, "limit": 5}))
    run_row = next(r for r in full_hist["runs"] if r["run_id"] == claim.run_id)
    assert run_row["postcheck_status"] == "failed"
    assert run_row["failure_kind"] == "business"
    assert run_row["duration_seconds"] == pytest.approx(4.0)
    assert run_row["target_exit_code"] == 7
    assert isinstance(run_row["postcheck_checks"], list) and run_row["postcheck_checks"]

    meta_hist = _payload(tool.execute({"action": "history", "job_id": job_id, "metadata_only": True, "limit": 5}))
    meta_row = next(r for r in meta_hist["runs"] if r["run_id"] == claim.run_id)
    assert meta_row["postcheck_status"] == "failed"
    assert meta_row["failure_kind"] == "business"
    assert meta_row["target_exit_code"] == 7
    assert "postcheck_checks" not in meta_row

    # Invalid on_failure and invalid history filters return concrete validation errors.
    with pytest.raises(ScheduleValidationError, match="policy\\.on_failure\\.after_failures"):
        normalize_policy({"on_failure": {"after_failures": 0}})
    bad_range = _payload(
        tool.execute(
            {
                "action": "history",
                "job_id": job_id,
                "since": "2026-11-18T12:00:00Z",
                "until": "2026-11-18T11:00:00Z",
            }
        )
    )
    assert bad_range["ok"] is False
    bad_status = _payload(tool.execute({"action": "history", "job_id": job_id, "status": "bogus"}))
    assert bad_status["ok"] is False
