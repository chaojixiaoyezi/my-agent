"""网关后台消费循环遇临时供应断供与额度墙的分流单测。

真机实锤场景:短期限流可退避自愈；套餐额度耗尽原地重试无效，必须只通知一次并等待人工恢复。

新行为(撤修复即 FAIL):
- 临时供应错按会话吸收，其他会话照常消费；信号留 pending 并指数退避；
- 套餐额度墙不进入自动退避，而由同一主代理通道发一次系统兜底并停止重打模型；
- 临时供应恢复后自动续跑；额度墙等待显式人工恢复；
- 非供应错(真程序 bug)照旧上抛,不掩盖。
"""

from __future__ import annotations

import pytest

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.backends.errors import (
    ProviderQuotaExhaustedError,
    ProviderTransientError,
)
from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
    ConversationStore,
    FakeDeliveryService,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


class _RateLimitedThenHealthyBackend:
    """前 fail_times 次调用抛 429(ProviderTransientError),之后返回正常响应。"""

    name = "rate-limited"

    def __init__(self, fail_times: int = 10**9):
        self.fail_times = fail_times
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None, **_kwargs: object) -> ModelResponse:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ProviderTransientError('HTTP 429: rate_limit_error "已达到 Token Plan 用量上限"')
        return ModelResponse(text="供应已恢复,后台轮续跑并汇报了进展。", backend=self.name)


class _CrashingBackend:
    name = "crashing"

    def generate(self, prompt: str, on_chunk=None, **_kwargs: object) -> ModelResponse:
        raise RuntimeError("real programmer bug")


class _QuotaExhaustedThenHealthyBackend:
    """若系统错误地自动重试，第二次会成功，从而让测试立即暴露。"""

    name = "quota-exhausted-then-healthy"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None, **_kwargs: object) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            raise ProviderQuotaExhaustedError("HTTP 429: insufficient_quota")
        return ModelResponse(text="切换供应后已继续。", backend=self.name)


@pytest.fixture(autouse=True)
def _no_auto_resume_delays(monkeypatch):
    """turn 内 auto_resume 短链不是本测试对象:清空延迟表让 429 直接上抛到消费循环。"""
    from agent_py_agent.agent.agent_core import provider_transient_auto_resume

    monkeypatch.setattr(
        provider_transient_auto_resume, "provider_transient_retry_delays", lambda _policy=None: ()
    )


def _scheduler(tmp_path, backend):
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    agent.backend = backend
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store, "claim_ttl_seconds": 30})
    return store, scheduler


def _thread_with_wake_signal(store, *, user: str, conversation: str, now: float) -> str:
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": user,
            "channel": "internal",
            "channel_conversation_id": conversation,
            "channel_user_id": user,
            "now": now,
        }
    )
    observation = store.append_observation(
        {
            "thread_id": thread.thread_id,
            "event_type": "subagent_runner_finished",
            "summary": "子代理已完成,请整合。",
            "urgency": "normal",
            "requires_main_agent": False,
            "now": now,
        }
    )
    store.raise_wake_signal(
        {
            "thread_id": thread.thread_id,
            "observation": observation,
            "reason": "subagent_runner_finished",
            "now": now,
        }
    )
    return thread.thread_id


def test_supply_outage_does_not_break_tick_and_other_threads_still_consume(tmp_path, capsys) -> None:
    """撤修复即 FAIL:429 中断整个 tick,B 会话被队头阻塞、tick 直接抛异常。"""
    backend = _RateLimitedThenHealthyBackend(fail_times=1)
    store, scheduler = _scheduler(tmp_path, backend)
    thread_a = _thread_with_wake_signal(store, user="user-a", conversation="conv-a", now=10.0)
    thread_b = _thread_with_wake_signal(store, user="user-b", conversation="conv-b", now=11.0)

    reports = scheduler.tick(now=20.0)  # A 撞 429 被吸收;B 照常消费

    assert backend.calls == 2
    assert [report.thread_id for report in reports] == [thread_b]
    pending = {signal.thread_id for signal in store.pending_wake_signals()}
    assert thread_a in pending  # A 的信号留 pending,不被标记消费、不丢
    assert thread_b not in pending
    printed = capsys.readouterr().out
    assert "[gateway-supply-backoff]" in printed
    assert "provider_supply_backoff" in printed
    assert "ProviderTransientError" in printed


def test_supply_backoff_throttles_retry_then_auto_resumes_after_recovery(tmp_path, capsys) -> None:
    """断供期指数退避(不秒级猛打),供应恢复后到点自动续跑,全程无人肉干预。"""
    backend = _RateLimitedThenHealthyBackend(fail_times=1)
    store, scheduler = _scheduler(tmp_path, backend)
    thread_id = _thread_with_wake_signal(store, user="user-a", conversation="conv-a", now=10.0)

    assert scheduler.tick(now=100.0) == []  # 撞 429,进退避
    assert backend.calls == 1
    assert scheduler.tick(now=101.0) == []  # 退避窗内(30s):不打模型
    assert backend.calls == 1
    assert store.pending_wake_signals()  # 信号还在等

    reports = scheduler.tick(now=131.0)  # 到点自动重试;供应已恢复 → 续跑

    assert backend.calls == 2
    assert [report.thread_id for report in reports] == [thread_id]
    assert store.pending_wake_signals() == []  # 消费完成
    printed = capsys.readouterr().out
    assert "provider_supply_resumed" in printed


def test_quota_exhaustion_sends_one_system_fallback_and_does_not_retry_model(
    tmp_path,
    capsys,
) -> None:
    backend = _QuotaExhaustedThenHealthyBackend()
    store, scheduler = _scheduler(tmp_path, backend)
    thread_id = _thread_with_wake_signal(
        store,
        user="quota-user",
        conversation="quota-conversation",
        now=10.0,
    )

    reports = scheduler.tick(now=100.0)
    assert backend.calls == 1
    assert len(reports) == 1
    assert reports[0].reason == "provider_quota_exhausted"
    assert reports[0].wake_handled is True
    assert "额度耗尽" in reports[0].response
    assert store.pending_wake_signals() == []

    assert scheduler.tick(now=101.0) == []
    assert backend.calls == 1
    assert scheduler.tick(now=131.0) == []
    assert backend.calls == 1
    printed = capsys.readouterr().out
    assert "provider_supply_backoff" not in printed
    assert reports[0].thread_id == thread_id


def test_due_progress_policy_survives_outage_and_resumes(tmp_path) -> None:
    """盯守判读走 progress policy 路(真机 1.9 冻死的主场景):断供期 policy 留 due,恢复后自动续跑。"""
    backend = _RateLimitedThenHealthyBackend(fail_times=1)
    store, scheduler = _scheduler(tmp_path, backend)
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "conv-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.bind_task({"thread_id": thread.thread_id, "task_id": "task-1", "goal": "盯守判读", "now": 11.0})
    store.set_progress_policy(
        {"thread_id": thread.thread_id, "task_id": "task-1", "interval_seconds": 60, "now": 12.0}
    )

    assert scheduler.tick(now=80.0) == []  # 到点判读撞 429:吸收进退避,policy 不推进
    assert backend.calls == 1

    reports = scheduler.tick(now=111.0)  # 退避到点重试,供应已恢复

    assert backend.calls == 2
    assert len(reports) == 1
    assert reports[0].thread_id == thread.thread_id


def test_non_supply_error_still_raises_out_of_tick(tmp_path) -> None:
    """不回归:真程序 bug 不吸收、照旧上抛走 [gateway-loop-error] 兜底,不掩盖。"""
    store, scheduler = _scheduler(tmp_path, _CrashingBackend())
    _thread_with_wake_signal(store, user="user-a", conversation="conv-a", now=10.0)

    with pytest.raises(RuntimeError, match="real programmer bug"):
        scheduler.tick(now=20.0)


def test_backoff_anchors_at_failure_time_not_tick_start(monkeypatch) -> None:
    """真机演练实锤回归锁:turn 内 auto_resume 短链自己要跑几分钟,退避若锚在 tick 起点,
    next_attempt_at 在 turn 结束时早已过期 → 退避形同虚设、下一 poll 立刻猛打。
    锚点必须=tick 时刻+turn 实际耗时。"""
    import agent_py_agent.agent.conversation.runtime as runtime_module
    from agent_py_agent.agent.conversation.runtime import (
        _consume_with_supply_guard,
        _ProviderSupplyBackoff,
    )

    clock = {"v": 1000.0}
    monkeypatch.setattr(runtime_module.time, "monotonic", lambda: clock["v"])
    backoff = _ProviderSupplyBackoff(base_seconds=30.0, max_seconds=900.0)

    def slow_failing_turn():
        clock["v"] += 100.0  # turn 实际耗时 100s(约等于 auto_resume 链长)
        raise ProviderTransientError("HTTP 429: rate_limit_error")

    assert _consume_with_supply_guard(backoff, "t", 50.0, slow_failing_turn) is None

    # 失败锚点 = 50(tick 时刻)+100(turn 耗时)=150,next_attempt=180。
    assert backoff.should_attempt("t", now=179.0) is False  # 锚在 tick 起点的话这里已放行(50+30=80)
    assert backoff.should_attempt("t", now=180.0) is True


def test_provider_supply_backoff_delays_are_exponential_and_capped() -> None:
    """退避可核算:30→60→120→240→480→900 封顶;成功清零后从头开始。"""
    from agent_py_agent.agent.conversation.runtime import _ProviderSupplyBackoff

    backoff = _ProviderSupplyBackoff(base_seconds=30.0, max_seconds=900.0)

    delays = [backoff.record_failure("t", now=0.0)["retry_delay_seconds"] for _ in range(7)]
    assert delays == [30.0, 60.0, 120.0, 240.0, 480.0, 900.0, 900.0]
    assert backoff.should_attempt("t", now=899.0) is False
    assert backoff.should_attempt("t", now=900.0) is True
    assert backoff.should_attempt("other-thread", now=0.0) is True  # 按会话隔离

    assert backoff.record_success("t") == 7
    assert backoff.record_failure("t", now=0.0)["retry_delay_seconds"] == 30.0
