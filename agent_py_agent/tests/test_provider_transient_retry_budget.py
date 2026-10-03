"""模型回合瞬时错误重试的总时长上限（修法 B）：上限内照常重试、到限保留原异常收口、0 不限、/stop 优先。"""

from __future__ import annotations

import pytest

from agent_py_agent.agent.agent_core import provider_transient_auto_resume as resume
from agent_py_agent.agent.backends import (
    ProviderTimeoutError,
    ProviderTransientError,
    ProviderUsageLimitError,
)
from agent_py_agent.agent.contracts.error_taxonomy import error_contract
from agent_py_agent.agent.settings.runtime_guard_config import RuntimeGuardPolicy


# LLM: 假时钟只替换模块自己读的 time.monotonic；配合被替换的 wait_interruptibly（推进假时钟）
#   就能在毫秒内断言“计时包含退避等待”，不需要真睡。
class _FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _policy(**values: object) -> RuntimeGuardPolicy:
    return RuntimeGuardPolicy(values=values)


def _prepare(monkeypatch, clock: _FakeClock, *, delays: tuple[float, ...], budget: float) -> list[float]:
    waits: list[float] = []

    monkeypatch.setattr(resume, "time", type("T", (), {"monotonic": staticmethod(clock)})())
    monkeypatch.setattr(resume, "apply_retry_jitter", lambda value: value)
    monkeypatch.setattr(resume, "provider_transient_retry_delays", lambda _policy=None: delays)
    monkeypatch.setattr(resume, "provider_transient_total_budget_seconds", lambda _policy=None: budget)

    def wait(delay: float) -> None:
        waits.append(delay)
        clock.advance(delay)  # 退避等待真实占用预算，用假时钟推进表示

    monkeypatch.setattr(resume, "wait_interruptibly", wait)
    return waits


def _flaky(failures: int, error: Exception | None = None):
    calls: list[int] = []

    def operation() -> str:
        calls.append(1)
        if len(calls) <= failures:
            raise error or ProviderTransientError("HTTP 429: rate limited")
        return "ok"

    return operation, calls


def test_retries_within_total_budget(monkeypatch) -> None:
    """上限内照常按阶梯重试；退避等待计入预算，最终成功。"""
    clock = _FakeClock()
    waits = _prepare(monkeypatch, clock, delays=(10.0, 25.0, 45.0), budget=100.0)
    operation, calls = _flaky(2)

    assert resume.run_with_provider_transient_auto_resume(operation) == "ok"
    assert len(calls) == 3 and waits == [10.0, 25.0]
    assert clock.now == 1035.0, "退避等待经过假时钟，证明计时包含等待"


def test_next_retry_exceeding_budget_raises_original_error_with_structured_facts(monkeypatch) -> None:
    """再退避一次就会超过上限时不再重试：原样抛出触发判断的原异常（类型与 stage 不变），
    只补结构化码与上限秒数——上层收尾与跑满阶梯完全同口径。"""
    clock = _FakeClock()
    waits = _prepare(monkeypatch, clock, delays=(10.0, 25.0, 45.0), budget=30.0)
    original = ProviderTimeoutError("模型接口等待首个流式事件超时", stage="first_event")
    operation, calls = _flaky(9, original)

    with pytest.raises(ProviderTimeoutError) as exc_info:
        resume.run_with_provider_transient_auto_resume(operation)

    error = exc_info.value
    assert error is original, "必须抛原对象：换类型会让上层（claim 结算/失败分类）走另一条路收尾"
    assert error.stage == "first_event"
    assert error.error_code == resume.PROVIDER_TRANSIENT_RETRY_TIME_BUDGET_EXCEEDED
    assert error.retry_budget_seconds == 30.0
    assert len(calls) == 2 and waits == [10.0], "第二次等待会越界，所以只等了一次"


def test_usage_limit_error_keeps_type_at_budget_limit(monkeypatch) -> None:
    """429 的 ProviderUsageLimitError 撞上限同样保留类型与 stage 口径，只补结构化事实。"""
    clock = _FakeClock()
    waits = _prepare(monkeypatch, clock, delays=(10.0,), budget=1.0)
    original = ProviderUsageLimitError("HTTP 429: usage limit")
    operation, calls = _flaky(9, original)

    with pytest.raises(ProviderUsageLimitError) as exc_info:
        resume.run_with_provider_transient_auto_resume(operation)

    assert exc_info.value is original
    assert exc_info.value.error_code == resume.PROVIDER_TRANSIENT_RETRY_TIME_BUDGET_EXCEEDED
    assert exc_info.value.retry_budget_seconds == 1.0
    assert len(calls) == 1 and waits == [], "第一次等待就会越界"


def test_budget_counts_slow_attempts_not_only_waits(monkeypatch) -> None:
    """预算也吃尝试本身耗时：第一次尝试耗时 25 秒时，10 秒的退避已越界。"""
    clock = _FakeClock()
    waits = _prepare(monkeypatch, clock, delays=(10.0, 25.0), budget=30.0)
    original = ProviderTransientError("HTTP 503: overloaded")

    def operation() -> str:
        clock.advance(25.0)  # 这次调用自己耗掉 25 秒，超时前的模型等待也算
        raise original

    with pytest.raises(ProviderTransientError) as exc_info:
        resume.run_with_provider_transient_auto_resume(operation)

    assert exc_info.value is original
    assert exc_info.value.error_code == resume.PROVIDER_TRANSIENT_RETRY_TIME_BUDGET_EXCEEDED
    assert waits == []


def test_zero_budget_means_unlimited(monkeypatch) -> None:
    """0 表示不限：走完整条阶梯，不因总时长收口。"""
    clock = _FakeClock()
    waits = _prepare(monkeypatch, clock, delays=(10.0, 25.0, 45.0), budget=0.0)
    operation, calls = _flaky(3)

    assert resume.run_with_provider_transient_auto_resume(operation) == "ok"
    assert len(calls) == 4 and waits == [10.0, 25.0, 45.0]
    assert clock.now == 1080.0, "累计 80 秒等待远超默认上限，证明 0 确实不限"


def test_stop_takes_priority_over_budget(monkeypatch) -> None:
    """用户停止优先：中断在预算判断之前消费标记，一次尝试都不发。"""
    clock = _FakeClock()
    waits = _prepare(monkeypatch, clock, delays=(10.0,), budget=1.0)
    from agent_py_agent.agent.concurrency.interrupt import set_interrupt

    calls: list[int] = []

    def operation() -> str:
        calls.append(1)
        return "ok"

    set_interrupt(True)
    try:
        with pytest.raises(InterruptedError):
            resume.run_with_provider_transient_auto_resume(operation)
    finally:
        set_interrupt(False)

    assert calls == [] and waits == [], "中断先于预算与尝试"


def test_interrupt_during_wait_still_wins_over_budget(monkeypatch) -> None:
    """退避等待里收到停止：等待抛中断，不因预算宽裕而继续重试。"""
    clock = _FakeClock()
    waits: list[float] = []
    monkeypatch.setattr(resume, "time", type("T", (), {"monotonic": staticmethod(clock)})())
    monkeypatch.setattr(resume, "apply_retry_jitter", lambda value: value)
    monkeypatch.setattr(resume, "provider_transient_retry_delays", lambda _policy=None: (10.0, 25.0))
    monkeypatch.setattr(resume, "provider_transient_total_budget_seconds", lambda _policy=None: 100.0)

    def wait(delay: float) -> None:
        waits.append(delay)
        raise InterruptedError("用户停止")

    monkeypatch.setattr(resume, "wait_interruptibly", wait)
    operation, calls = _flaky(2)

    with pytest.raises(InterruptedError):
        resume.run_with_provider_transient_auto_resume(operation)
    assert len(calls) == 1 and waits == [10.0]


def test_budget_reads_runtime_guard_policy_and_defaults() -> None:
    """上限只从运行护栏配置读：缺键回落 1800 秒，坏值、NaN、负数都回落默认；只有 0 表示不限。"""
    assert resume.provider_transient_total_budget_seconds(_policy()) == 1800.0
    assert resume.provider_transient_total_budget_seconds(_policy(
        provider_transient_auto_resume_total_budget_seconds=600)) == 600.0
    assert resume.provider_transient_total_budget_seconds(_policy(
        provider_transient_auto_resume_total_budget_seconds="bad")) == 1800.0
    assert resume.provider_transient_total_budget_seconds(_policy(
        provider_transient_auto_resume_total_budget_seconds=float("nan"))) == 1800.0
    assert resume.provider_transient_total_budget_seconds(_policy(
        provider_transient_auto_resume_total_budget_seconds=-5)) == 1800.0
    # 9b 终审补（变异 F2）：inf 不能当成“不限”，否则会静默关掉护栏。
    assert resume.provider_transient_total_budget_seconds(_policy(
        provider_transient_auto_resume_total_budget_seconds=float("inf"))) == 1800.0
    assert resume.provider_transient_total_budget_seconds(_policy(
        provider_transient_auto_resume_total_budget_seconds=0)) == 0.0


# 9b 终审补（变异 F7）：新码必须登记在错误契约表里，否则会落到 UNKNOWN_ERROR 的兜底契约。
def test_budget_code_is_registered_in_the_error_taxonomy() -> None:
    contract = error_contract(resume.PROVIDER_TRANSIENT_RETRY_TIME_BUDGET_EXCEEDED)
    assert (contract.code, contract.recommended_action) == (
        "PROVIDER_TRANSIENT_RETRY_TIME_BUDGET_EXCEEDED", "retry_after_backoff")


def test_interrupt_mark_set_during_wait_stops_before_next_attempt(monkeypatch) -> None:
    """退避等待正常返回、但等待期间置上了中断标记：不发起下一次尝试，抛中断。

    钉住主循环等待后的那次 _raise_if_interrupted()（初审变异 5：删掉它后原用例全绿——
    原用例让 wait_interruptibly 自己抛中断，永远走不到等待后的兜底检查）。
    """
    clock = _FakeClock()
    waits: list[float] = []
    monkeypatch.setattr(resume, "time", type("T", (), {"monotonic": staticmethod(clock)})())
    monkeypatch.setattr(resume, "apply_retry_jitter", lambda value: value)
    monkeypatch.setattr(resume, "provider_transient_retry_delays", lambda _policy=None: (10.0, 25.0))
    monkeypatch.setattr(resume, "provider_transient_total_budget_seconds", lambda _policy=None: 100.0)
    from agent_py_agent.agent.concurrency.interrupt import set_interrupt

    def wait(delay: float) -> None:
        waits.append(delay)
        clock.advance(delay)
        set_interrupt(True)  # 等待窗口内标记被置上，而 wait_interruptibly 因时序没有抛

    monkeypatch.setattr(resume, "wait_interruptibly", wait)
    operation, calls = _flaky(9)

    try:
        with pytest.raises(InterruptedError):
            resume.run_with_provider_transient_auto_resume(operation)
    finally:
        set_interrupt(False)

    assert len(calls) == 1 and waits == [10.0], "等待后必须再查一次中断，不能进入第二次尝试"


def test_budget_exhausted_emits_final_notice_before_raising(monkeypatch) -> None:
    """到上限时先在 on_chunk 通道发一条「本轮不再重试」的收口提示，再抛出异常。

    第一次退避（10 秒）在预算内、发的是普通重试提示；第二次等待（25 秒）越界，
    收口提示写明已到总时长上限。同步顺序：提示能被记录到，就证明它在 raise 之前执行。
    """
    clock = _FakeClock()
    waits = _prepare(monkeypatch, clock, delays=(10.0, 25.0, 45.0), budget=30.0)
    chunks: list[str] = []
    order: list[str] = []
    original = ProviderTimeoutError("模型接口等待首个流式事件超时", stage="first_event")

    def operation() -> str:
        order.append("attempt")
        raise original

    def on_chunk(text: str) -> None:
        chunks.append(text)
        order.append("notice")

    with pytest.raises(ProviderTimeoutError):
        resume.run_with_provider_transient_auto_resume(operation, on_chunk=on_chunk)
    order.append("raised")

    assert waits == [10.0], "第二次等待会越界，收口时不再等待"
    assert len(chunks) == 2, "第一条是普通重试提示，第二条是收口提示"
    assert "wait_seconds=10" in chunks[0]
    assert "budget_exhausted" in chunks[1] and "已到自动重试的总时长上限" in chunks[1]
    assert "本轮不再重试" in chunks[1]
    assert order == ["attempt", "notice", "attempt", "notice", "raised"], (
        "收口提示必须在异常抛出之前发出"
    )


def test_subagent_failure_type_stays_provider_timeout_at_budget_limit(monkeypatch) -> None:
    """到上限抛出的原异常在子代理失败类型上仍是 provider_timeout：与跑满阶梯同口径。

    子代理重派与后台 claim 结算都按异常类型分路（is_provider_transient_error 等只看
    类型），类型保持后两者与跑满阶梯走同一分支；这里钉住子代理侧的直接判定。
    """
    from agent_py_agent.agent.agent_core.subagent_mixin import _subagent_run_failure_type

    clock = _FakeClock()
    waits = _prepare(monkeypatch, clock, delays=(10.0,), budget=1.0)
    original = ProviderTimeoutError("模型接口等待首个流式事件超时", stage="first_event")
    operation, calls = _flaky(9, original)

    with pytest.raises(ProviderTimeoutError) as exc_info:
        resume.run_with_provider_transient_auto_resume(operation)

    assert exc_info.value is original
    plain = ProviderTimeoutError("普通超时（没到上限）", stage="first_event")
    assert _subagent_run_failure_type(plain) == _subagent_run_failure_type(exc_info.value)
    assert _subagent_run_failure_type(exc_info.value) == "provider_timeout"


def test_runtime_error_report_swaps_message_only_for_budget_code() -> None:
    """到上限时运行时错误报告的文案不再说"系统会自动退避重试"，分类与可恢复性不变；
    其它供应错误的文案逐字不变（只按结构化错误码分流，不做文本匹配）。"""
    from agent_py_agent.agent.runtime_errors import runtime_error_report

    plain = ProviderTimeoutError("模型接口等待首个流式事件超时", stage="first_event")
    baseline = runtime_error_report(plain)
    assert baseline["category"] == "provider_timeout" and baseline["recoverable"] is True
    assert "系统会自动退避重试" in baseline["model_message"]

    capped = ProviderTimeoutError("模型接口等待首个流式事件超时", stage="first_event")
    capped.error_code = resume.PROVIDER_TRANSIENT_RETRY_TIME_BUDGET_EXCEEDED
    capped.retry_budget_seconds = 1800.0
    report = runtime_error_report(capped)
    assert report["category"] == baseline["category"], "分类不变"
    assert report["recoverable"] is baseline["recoverable"], "可恢复性不变"
    assert "系统会自动退避重试" not in report["model_message"]
    assert "总时长上限" in report["model_message"] and "已停止自动重试" in report["model_message"]
