"""GWPERF-F3：被延后/退避/恢复阻断的待处理唤醒必须保留指数空闲退避，只有真正可运行的唤醒才清零退避。"""
from __future__ import annotations

import time as _stdlib_time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
    FakeDeliveryService,
)
from agent_py_agent.agent.conversation.input_dependencies import (
    arm_input_dependency,
    cancel_task_inputs,
    create_input_dependency,
)
from agent_py_agent.agent.conversation.observation_index import ObservationIndexIncomplete
from agent_py_agent.agent.conversation.tool_approval_scope import tool_approval_scope
from agent_py_agent.agent.conversation.wake_poison import (
    WAKE_VERDICT_FAILURE,
    WakeAttemptVerdict,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.owner_scoped_pool import ActiveOwnerRegistry
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.cli import gateway_loops as loops
from agent_py_agent.cli.gateway_lane_retry import BackgroundLaneRetry


# LLM: 使用真实 SimpleAgent、ConversationStore、BackgroundMainAgentScheduler 与 _BackgroundMainSupervisor；
#   只控制时钟并拦截实际线程池提交以精确验证规划节拍与准入。
# 函数用途: 构建带真实后台调度器与可控时钟的空闲退避测试环境。
@contextmanager
def _real_supervisor(tmp_path, monkeypatch, *, cap: float = 10.0):
    mono = [100.0]
    wall = [_stdlib_time.time()]
    monkeypatch.setattr(loops.time, "monotonic", lambda: mono[0])
    monkeypatch.setattr(loops.time, "time", lambda: wall[0])

    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            background_owner_workers=4,
            background_threads_per_owner=4,
            background_idle_owner_max_backoff_seconds=cap,
        ),
        tmp_path,
    )
    store = agent.conversation_store
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=store,
        channels=FakeDeliveryService(),
    )
    scheduler = BackgroundMainAgentScheduler(
        {"runtime": runtime, "store": store, "claim_ttl_seconds": 30}
    )

    plan_calls: list[float] = []
    submitted: list[list[str]] = []
    original_prepare = scheduler.prepare_tick

    def tracked_prepare_tick(**kwargs):
        plan_calls.append(mono[0])
        return original_prepare(**kwargs)

    monkeypatch.setattr(scheduler, "prepare_tick", tracked_prepare_tick)

    supervisor = object.__new__(loops._BackgroundMainSupervisor)
    supervisor._base_agent = agent
    supervisor._base_scheduler = scheduler
    supervisor._owner_schedulers = {}
    supervisor._inflight = {}
    supervisor._lane_retry = BackgroundLaneRetry()
    supervisor._registry = ActiveOwnerRegistry()
    supervisor._owner_pool = None
    supervisor._submit_thread_candidates = lambda rows, **kwargs: submitted.extend(
        row[2] for row in rows
    )
    try:
        yield SimpleNamespace(
            agent=agent,
            store=store,
            runtime=runtime,
            scheduler=scheduler,
            supervisor=supervisor,
            mono=mono,
            wall=wall,
            plan_calls=plan_calls,
            submitted=submitted,
        )
    finally:
        supervisor._retain_owner_planner_scopes(set())


# LLM: 沿真实线程/任务创建待处理唤醒，不造假队列对象。
# 函数用途: 创建线程与（可选）绑定任务，并发布一条 pending 唤醒信号。
def _raise_wake(
    case,
    *,
    conversation: str,
    reason: str = "subagent_runner_finished",
    task_id: str = "",
    urgency: str = "urgent",
    created_at: float | None = None,
):
    now = created_at if created_at is not None else case.wall[0]
    thread = case.store.threads.get_or_create(
        {
            "canonical_user_id": "owner-user",
            "channel": "internal",
            "channel_conversation_id": conversation,
            "channel_user_id": "owner-user",
            "now": now,
        }
    )
    if task_id:
        case.store.tasks.bind(
            {
                "thread_id": thread.thread_id,
                "task_id": task_id,
                "goal": "deferred-wake-test",
                "now": now,
            }
        )
    signal = case.store.wakes.raise_signal(
        {
            "thread_id": thread.thread_id,
            "root_task_id": task_id,
            "reason": reason,
            "urgency": urgency,
            "now": now,
        }
    )
    return thread, signal


# LLM: 首拍会建立或同步签名（首次打开规划 WAL 连接可能触发一次稳态刷新），随后连续空规划按 1/2/4/8/10 秒退避并在 cap 内重新检查。
# 函数用途: 驱动多拍规划并断言指数退避节拍确实生效且无误提交。
def _assert_exponential_backoff_ticks(case) -> None:
    supervisor = case.supervisor
    base_key = loops._BASE_SCHEDULER_KEY

    # 首次规划打开 WAL 或同步初始签名后进入稳态退避（最多 2 次预热拍后即应开始 1.0s 退避）。
    for _ in range(3):
        supervisor._submit_ready_thread_ticks()
        assert case.submitted == []
        next_at = getattr(supervisor, "_owner_next_check_at", {}).get(base_key, 0.0)
        if next_at > case.mono[0] and supervisor._owner_planner_signature(case.scheduler) == supervisor._owner_plan_signatures.get(base_key):
            break
    else:
        pytest.fail("未能在初始签名稳定后进入空闲退避")

    expected_delays = (1.0, 2.0, 4.0, 8.0, 10.0)
    first_delay = round(supervisor._owner_next_check_at[base_key] - case.mono[0], 6)
    assert first_delay == 1.0

    for delay in expected_delays:
        assert supervisor._owner_next_check_at[base_key] == pytest.approx(
            case.mono[0] + delay
        )
        before_calls = len(case.plan_calls)
        case.mono[0] += delay - 0.01
        case.wall[0] += delay - 0.01
        supervisor._submit_ready_thread_ticks()
        assert len(case.plan_calls) == before_calls
        assert case.submitted == []
        case.mono[0] += 0.01
        case.wall[0] += 0.01
        if delay != expected_delays[-1]:
            supervisor._submit_ready_thread_ticks()
            assert len(case.plan_calls) == before_calls + 1
            assert case.submitted == []


def test_wake_retry_after_retains_exponential_idle_backoff_until_due(
    tmp_path, monkeypatch
) -> None:
    with _real_supervisor(tmp_path, monkeypatch) as case:
        thread, signal = _raise_wake(case, conversation="conv-retry-after")
        case.scheduler._wake_retry_after[signal.wake_signal_id] = case.wall[0] + 3600.0

        assert case.scheduler.ready_thread_ids(now=case.wall[0]) == ()
        assert (
            case.supervisor._owner_plan_idle_delay(
                case.scheduler, 8.0, case.wall[0], frozenset()
            )
            == 8.0
        )

        _assert_exponential_backoff_ticks(case)

        # 到期后无需重开网关，最迟在 cap 内下一次检查即可提交该线程。
        case.scheduler._wake_retry_after[signal.wake_signal_id] = case.wall[0]
        case.supervisor._submit_ready_thread_ticks()
        assert case.submitted == [[thread.thread_id]]


def test_persisted_poison_backoff_retains_exponential_idle_backoff(
    tmp_path, monkeypatch
) -> None:
    with _real_supervisor(tmp_path, monkeypatch) as case:
        thread, signal = _raise_wake(case, conversation="conv-poison")
        outcome = case.store.wakes.attempts.record(
            signal,
            WakeAttemptVerdict(WAKE_VERDICT_FAILURE, "run:no_report"),
            now=case.wall[0],
        )
        assert outcome.state.next_attempt_at > case.wall[0]

        assert case.scheduler.ready_thread_ids(now=case.wall[0]) == ()
        assert (
            case.supervisor._owner_plan_idle_delay(
                case.scheduler, 8.0, case.wall[0], frozenset()
            )
            == 8.0
        )

        _assert_exponential_backoff_ticks(case)

        # 超过 30 秒持久毒丸退避后，在 cap(10s) 内恢复检查并提交。
        case.wall[0] = outcome.state.next_attempt_at + 0.1
        case.mono[0] += 10.0
        case.supervisor._submit_ready_thread_ticks()
        assert case.submitted == [[thread.thread_id]]


def test_waiting_input_dependency_retains_exponential_idle_backoff(
    tmp_path, monkeypatch
) -> None:
    with _real_supervisor(tmp_path, monkeypatch) as case:
        thread, _signal = _raise_wake(
            case,
            conversation="conv-waiting-input",
            reason="subagent_runner_finished",
            task_id="task-wait-input",
        )
        child = case.agent.subagents.create_run(
            goal="等待外部必要输入",
            root_id="task-wait-input",
            parent_id="task-wait-input",
        )
        child = case.agent.subagents.lifecycle.prepare_runner_attempt(child.id)
        create_input_dependency(
            case.agent,
            tool_approval_scope(case.agent, child.id),
            input_id="input-wait-1",
            provider="synthetic",
            expires_at=case.wall[0] + 3600.0,
        )
        arm_input_dependency(case.agent, "input-wait-1")

        assert case.scheduler.ready_thread_ids(now=case.wall[0]) == ()
        assert (
            case.supervisor._owner_plan_idle_delay(
                case.scheduler, 8.0, case.wall[0], frozenset()
            )
            == 8.0
        )

        _assert_exponential_backoff_ticks(case)

        # 输入依赖撤销/解除后，在 cap 内下一次检查立即恢复提交。
        cancel_task_inputs(case.agent, "task-wait-input")
        case.mono[0] += 10.0
        case.wall[0] += 10.0
        case.supervisor._submit_ready_thread_ticks()
        assert case.submitted == [[thread.thread_id]]


def test_supply_backoff_retains_exponential_idle_backoff_until_recovery(
    tmp_path, monkeypatch
) -> None:
    with _real_supervisor(tmp_path, monkeypatch) as case:
        thread, _signal = _raise_wake(case, conversation="conv-supply-backoff")
        failure = case.scheduler._supply_backoff.record_failure(
            thread.thread_id, case.wall[0]
        )

        assert case.scheduler.ready_thread_ids(now=case.wall[0]) == ()
        assert (
            case.supervisor._owner_plan_idle_delay(
                case.scheduler, 8.0, case.wall[0], frozenset()
            )
            == 8.0
        )

        _assert_exponential_backoff_ticks(case)

        # 30 秒供应冷却到期后，在 cap(10s) 窗口内自动恢复规划与提交。
        case.wall[0] = float(failure["next_attempt_at"]) + 0.1
        case.mono[0] += 10.0
        case.supervisor._submit_ready_thread_ticks()
        assert case.submitted == [[thread.thread_id]]


def test_recovery_guard_block_retains_exponential_idle_backoff(
    tmp_path, monkeypatch
) -> None:
    with _real_supervisor(tmp_path, monkeypatch) as case:
        thread, _signal = _raise_wake(
            case,
            conversation="conv-recovery-guard",
            reason="thread_goal_continue",
            task_id="task-recovery-blocked",
        )
        repo = case.agent.subagents.runtime_db
        rec = repo.record_run_creation(
            owner_id=case.agent.home_paths.owner_id,
            run_id="run-recovery-blocked",
            conversation_task_id="task-recovery-blocked",
            thread_id=thread.thread_id,
            role="main",
        )
        with repo.transaction() as conn:
            conn.execute(
                "UPDATE agent_attempts SET status='unknown' WHERE attempt_id=?",
                (rec["attempt_id"],),
            )
            conn.execute(
                "UPDATE agent_runs SET status='unknown' WHERE agent_run_id=?",
                (rec["agent_run_id"],),
            )

        assert (
            case.scheduler._recovery_guard.block_for_task("task-recovery-blocked")
            is not None
        )
        assert case.scheduler.ready_thread_ids(now=case.wall[0]) == ()
        assert (
            case.supervisor._owner_plan_idle_delay(
                case.scheduler, 8.0, case.wall[0], frozenset()
            )
            == 8.0
        )

        _assert_exponential_backoff_ticks(case)


def test_runnable_wake_still_returns_zero_delay_even_behind_deferred_wake(
    tmp_path, monkeypatch
) -> None:
    with _real_supervisor(tmp_path, monkeypatch) as case:
        # 1) 单条可运行唤醒：仍立即返回 0.0。
        thread_b, _signal_b = _raise_wake(
            case,
            conversation="conv-runnable",
            urgency="normal",
            created_at=case.wall[0],
        )
        assert (
            case.supervisor._owner_plan_idle_delay(
                case.scheduler, 8.0, case.wall[0], frozenset()
            )
            == 0.0
        )

        # 2) 前面排着一条 urgent 的延后唤醒（thread_a 处于供应退避中），后面排着可运行的 thread_b：
        #    不得因只读 limit=1 被延后唤醒挡住而误退避。
        thread_a, _signal_a = _raise_wake(
            case,
            conversation="conv-deferred-first",
            urgency="urgent",
            created_at=case.wall[0] - 10.0,
        )
        case.scheduler._supply_backoff.record_failure(thread_a.thread_id, case.wall[0])
        assert (
            case.supervisor._owner_plan_idle_delay(
                case.scheduler, 8.0, case.wall[0], frozenset()
            )
            == 0.0
        )

        # 3) 若 thread_a 在普通车道冷却中且 ready_thread_ids 因 ObservationIndexIncomplete
        #    回退成 limit=1 只拿到 cooling 线程，_owner_plan_idle_delay 仍必须发现后面的 thread_b 返回 0.0。
        case.scheduler._supply_backoff.record_success(thread_a.thread_id)
        case.supervisor._lane_retry.failed(
            str(loops._BASE_SCHEDULER_KEY),
            thread_a.thread_id,
            RuntimeError("lane failure"),
            delay=30.0,
        )
        original_ready = case.scheduler.ready_thread_ids
        calls = {"count": 0}

        def partial_then_limited(*, now=None, limit=0):
            calls["count"] += 1
            if calls["count"] == 1:
                raise ObservationIndexIncomplete("sync in progress")
            return original_ready(now=now, limit=limit)

        monkeypatch.setattr(case.scheduler, "ready_thread_ids", partial_then_limited)
        case.supervisor._submit_ready_thread_ticks()
        assert calls["count"] == 2
        assert case.supervisor._owner_next_check_at[loops._BASE_SCHEDULER_KEY] == 0.0
        assert case.submitted == []
        # 没有退避拦路，下一拍立即调度到可运行的 thread_b。
        monkeypatch.setattr(case.scheduler, "ready_thread_ids", original_ready)
        case.supervisor._submit_ready_thread_ticks()
        assert case.submitted == [[thread_b.thread_id]]


def test_minimal_stub_scheduler_without_runtime_or_attempts_stays_compatible() -> None:
    supervisor = object.__new__(loops._BackgroundMainSupervisor)
    scheduler = SimpleNamespace(
        store=SimpleNamespace(
            wakes=SimpleNamespace(
                pending=lambda **kwargs: [SimpleNamespace(thread_id="healthy")]
            )
        )
    )
    assert supervisor._owner_plan_idle_delay(scheduler, 8.0, 100.0, frozenset()) == 0.0
    assert (
        supervisor._owner_plan_idle_delay(
            scheduler, 8.0, 100.0, frozenset({"healthy"})
        )
        == 8.0
    )
