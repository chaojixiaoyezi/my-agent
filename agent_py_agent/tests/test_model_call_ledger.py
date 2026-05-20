from __future__ import annotations

from dataclasses import dataclass

from agent_py_agent.agent.agent_core.model_call_monitor import (
    FirstTokenTimeoutContext,
    FirstTokenTimeoutOptions,
    FirstTokenTimeoutParams,
    estimate_first_token_timeout,
    is_cache_suspected,
)
from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallFinishParams,
    ModelCallFirstTokenParams,
    ModelCallLedger,
    ModelCallLedgerContext,
    ModelCallLedgerOptions,
    ModelCallStartedParams,
    ModelCallTimeoutParams,
)


# LLM: _FakeClock lets ledger tests assert exact event timing without sleeping.
# 类用途: 测试专用时钟；通过显式 advance 生成可预测的 started/first_token/finished 时间戳。
@dataclass
class _FakeClock:
    now_seconds: float = 0.0

    # LLM: now mirrors time.monotonic so production code can accept this injected clock.
    # 函数用途: 返回当前测试时间，不读取真实系统时钟。
    def now(self) -> float:
        return self.now_seconds

    # LLM: advance moves the deterministic test clock forward.
    # 函数用途: 调整当前测试时间，让测试能验证耗时字段。
    def advance(self, seconds: float) -> None:
        self.now_seconds += seconds


# LLM: _ProbeSpec keeps probe helper arguments structured and readable.
# 类用途: 测试用 probe 样本参数包，避免 helper 用散乱业务参数。
@dataclass(frozen=True)
class _ProbeSpec:
    call_id: str
    input_tokens: int
    latency_seconds: float


# LLM: ledger records must expose finished model-call timing as structured facts.
# 函数用途: 验证 started、first_token、finished 的状态、事件顺序和耗时字段。
def test_ledger_records_finished_model_call_timing() -> None:
    clock = _FakeClock(100.0)
    ledger = _ledger(clock)
    ledger.started(
        ModelCallStartedParams(
            call_id="call-finished",
            backend="test-backend",
            model="test-model",
            input_tokens=1200,
            output_tokens_estimate=300,
            request_id="req-1",
            run_id="run-1",
        )
    )
    clock.advance(2.5)
    ledger.first_token(ModelCallFirstTokenParams(call_id="call-finished"))
    clock.advance(7.5)
    ledger.finished(ModelCallFinishParams(call_id="call-finished", output_tokens=240))

    (finished,) = ledger.records()

    assert finished.status == "finished"
    assert finished.events == ("started", "first_token", "finished")
    assert finished.first_token_latency_seconds == 2.5
    assert finished.total_latency_seconds == 10.0
    assert finished.output_tokens == 240
    assert finished.to_dict()["first_token_at"] == 102.5


# LLM: timeout records must keep stage and duration as machine-readable recovery facts.
# 函数用途: 验证模型调用 timeout 状态、timeout_stage 和 total_latency_seconds。
def test_ledger_records_timeout_stage_and_duration() -> None:
    clock = _FakeClock(100.0)
    ledger = _ledger(clock)
    ledger.started(
        ModelCallStartedParams(
            call_id="call-timeout",
            backend="test-backend",
            model="test-model",
            input_tokens=5000,
            output_tokens_estimate=500,
            request_id="req-2",
            run_id="run-2",
        )
    )
    clock.advance(30.0)
    ledger.timeout(
        ModelCallTimeoutParams(
            call_id="call-timeout",
            timeout_seconds=25.0,
            timeout_stage="first_token",
        )
    )

    (timed_out,) = ledger.records()

    assert timed_out.status == "timed_out"
    assert timed_out.events == ("started", "timeout")
    assert timed_out.timeout_seconds == 25.0
    assert timed_out.timeout_stage == "first_token"
    assert timed_out.total_latency_seconds == 30.0


# LLM: timeout estimation should separate prefill cost from first-token fixed overhead.
# 函数用途: 验证无 probe 样本时，按结构化输入 token 和 fallback prefill 速率估算并应用安全边际。
def test_estimates_prefill_and_first_token_timeout_without_probe_samples() -> None:
    ledger = ModelCallLedger()

    estimate = estimate_first_token_timeout(
        FirstTokenTimeoutParams(
            input_tokens=2000,
            ledger=ledger,
            options=FirstTokenTimeoutOptions(
                fallback_prefill_tokens_per_second=500.0,
                base_first_token_seconds=3.0,
                safety_margin=2.0,
                min_timeout_seconds=1.0,
                max_timeout_seconds=60.0,
            ),
        )
    )

    assert estimate.source == "fallback"
    assert estimate.prefill_seconds == 4.0
    assert estimate.first_token_seconds == 3.0
    assert estimate.timeout_seconds == 14.0


# LLM: probe samples at 5K and 10K must drive extrapolation for larger prompts.
# 函数用途: 验证 monitor 从账本中的 5K/10K first_token 结构化记录推算目标输入大小。
def test_estimates_first_token_timeout_from_5k_and_10k_probe_samples() -> None:
    clock = _FakeClock(0.0)
    ledger = ModelCallLedger(context=ModelCallLedgerContext(now=clock.now))
    _record_first_token_probe(ledger, clock, _ProbeSpec("probe-5k", 5000, 13.0))
    _record_first_token_probe(ledger, clock, _ProbeSpec("probe-10k", 10000, 23.0))

    estimate = estimate_first_token_timeout(
        FirstTokenTimeoutParams(
            input_tokens=15000,
            ledger=ledger,
            options=FirstTokenTimeoutOptions(
                safety_margin=1.5,
                min_timeout_seconds=1.0,
                max_timeout_seconds=120.0,
            ),
            context=FirstTokenTimeoutContext(required_probe_tokens=(5000, 10000)),
        )
    )

    assert estimate.source == "probe_5k_10k"
    assert estimate.prefill_seconds == 30.0
    assert estimate.first_token_seconds == 3.0
    assert estimate.timeout_seconds == 49.5


# LLM: timeout budgets must stay inside operator-configured bounds.
# 函数用途: 覆盖首 token 超时估算的 clamp 边界，避免异常样本给出过小或过大预算。
def test_first_token_timeout_estimate_clamps_to_bounds() -> None:
    low = estimate_first_token_timeout(
        FirstTokenTimeoutParams(
            input_tokens=1,
            ledger=ModelCallLedger(),
            options=FirstTokenTimeoutOptions(
                fallback_prefill_tokens_per_second=10000.0,
                base_first_token_seconds=0.1,
                safety_margin=1.0,
                min_timeout_seconds=5.0,
                max_timeout_seconds=50.0,
            ),
        )
    )
    high = estimate_first_token_timeout(
        FirstTokenTimeoutParams(
            input_tokens=1_000_000,
            ledger=ModelCallLedger(),
            options=FirstTokenTimeoutOptions(
                fallback_prefill_tokens_per_second=100.0,
                base_first_token_seconds=10.0,
                safety_margin=2.0,
                min_timeout_seconds=5.0,
                max_timeout_seconds=50.0,
            ),
        )
    )

    assert low.timeout_seconds == 5.0
    assert high.timeout_seconds == 50.0


# LLM: very fast first-token samples for large prompts are cache evidence, not normal speed facts.
# 函数用途: 验证 cache_suspected 标记来自结构化 token/latency，不依赖模型输出文本。
def test_cache_suspected_mark_for_large_fast_first_token_record() -> None:
    estimate = estimate_first_token_timeout(
        FirstTokenTimeoutParams(
            input_tokens=12000,
            ledger=ModelCallLedger(),
            options=FirstTokenTimeoutOptions(
                fallback_prefill_tokens_per_second=400.0,
                base_first_token_seconds=3.0,
                safety_margin=1.0,
                min_timeout_seconds=1.0,
                max_timeout_seconds=120.0,
            ),
        )
    )

    assert is_cache_suspected(
        input_tokens=12000,
        first_token_latency_seconds=1.2,
        estimate=estimate,
    )
    assert not is_cache_suspected(
        input_tokens=12000,
        first_token_latency_seconds=26.0,
        estimate=estimate,
    )


# LLM: _record_first_token_probe creates finished ledger entries for probe extrapolation tests.
# 函数用途: 用 started/first_token/finished 公共 API 写入一个指定首 token 延迟的 probe 样本。
def _record_first_token_probe(
    ledger: ModelCallLedger,
    clock: _FakeClock,
    spec: _ProbeSpec,
) -> None:
    ledger.started(
        ModelCallStartedParams(
            call_id=spec.call_id,
            backend="test-backend",
            model="test-model",
            input_tokens=spec.input_tokens,
            output_tokens_estimate=100,
            request_id=spec.call_id,
            run_id="probe-run",
            is_probe=True,
        )
    )
    clock.advance(spec.latency_seconds)
    ledger.first_token(ModelCallFirstTokenParams(call_id=spec.call_id))
    ledger.finished(ModelCallFinishParams(call_id=spec.call_id, output_tokens=1))


# LLM: _ledger gives tests the same small retention setup without repeating constructor details.
# 函数用途: 创建带假时钟和固定 max_records 的 ModelCallLedger。
def _ledger(clock: _FakeClock) -> ModelCallLedger:
    return ModelCallLedger(
        options=ModelCallLedgerOptions(max_records=4),
        context=ModelCallLedgerContext(now=clock.now),
    )
