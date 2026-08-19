from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model.call_monitor import (
    FirstTokenTimeoutContext,
    FirstTokenTimeoutOptions,
    FirstTokenTimeoutParams,
    estimate_first_token_timeout,
    is_cache_suspected,
)
from agent_py_agent.agent.agent_core.model.call_runtime import (
    model_call_summary,
    start_model_call_record,
)
from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallActivityParams,
    ModelCallFailureParams,
    ModelCallFinishParams,
    ModelCallFirstTokenParams,
    ModelCallLedger,
    ModelCallLedgerContext,
    ModelCallLedgerOptions,
    ModelCallProviderAttemptParams,
    ModelCallStartedParams,
    ModelCallTimeoutParams,
)
from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot


@dataclass
class _FakeClock:
    now_seconds: float = 0.0

    def now(self) -> float:
        return self.now_seconds

    def advance(self, seconds: float) -> None:
        self.now_seconds += seconds


@dataclass(frozen=True)
class _ProbeSpec:
    call_id: str
    input_tokens: int
    latency_seconds: float


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


def test_ledger_stream_activity_refreshes_timestamp_without_growing_events() -> None:
    clock = _FakeClock(100.0)
    ledger = _ledger(clock)
    ledger.started(
        ModelCallStartedParams(
            call_id="call-streaming",
            backend="test-backend",
            model="test-model",
            input_tokens=1200,
            run_id="run-streaming",
        )
    )
    clock.advance(1.0)
    ledger.first_token(
        ModelCallFirstTokenParams(
            call_id="call-streaming",
            output_tokens_seen=2,
        )
    )
    clock.advance(3.0)
    ledger.activity(
        ModelCallActivityParams(
            call_id="call-streaming",
            output_tokens_seen=4,
        )
    )

    (streaming,) = ledger.records()

    assert streaming.events == ("started", "first_token")
    assert streaming.last_activity_at == 104.0
    assert streaming.output_tokens_seen == 6
    assert streaming.to_dict()["last_activity_at"] == 104.0


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
            # 门槛2 终审边界②: ledger 写入层 stage 合同封闭后, 只能写四值
            # (stream_idle/wall_clock/provider_declared/provider_wall);
            # "first_token" 不是超时 stage, 由新的 fail-closed 校验拒绝。
            timeout_stage="wall_clock",
        )
    )

    (timed_out,) = ledger.records()

    assert timed_out.status == "timed_out"
    assert timed_out.events == ("started", "timeout")
    assert timed_out.timeout_seconds == 25.0
    assert timed_out.timeout_stage == "wall_clock"
    assert timed_out.total_latency_seconds == 30.0


def test_failed_model_call_is_not_overwritten_by_late_finish() -> None:
    clock = _FakeClock(100.0)
    ledger = _ledger(clock)
    ledger.started(
        ModelCallStartedParams(
            call_id="call-failed",
            backend="test-backend",
            model="test-model",
            input_tokens=500,
        )
    )
    clock.advance(3.0)
    ledger.failed(
        ModelCallFailureParams(
            call_id="call-failed",
            error_type="ProviderTransientError",
            error_code="PROVIDER_TRANSIENT",
        )
    )
    clock.advance(2.0)
    ledger.finished(ModelCallFinishParams(call_id="call-failed", output_tokens=20))

    (failed,) = ledger.records()

    assert failed.status == "failed"
    assert failed.events == ("started", "failed")
    assert failed.error_type == "ProviderTransientError"
    assert failed.error_code == "PROVIDER_TRANSIENT"
    assert failed.total_latency_seconds == 3.0


def test_ledger_records_each_physical_provider_attempt() -> None:
    clock = _FakeClock(100.0)
    ledger = _ledger(clock)
    ledger.started(
        ModelCallStartedParams(
            call_id="call-provider-retry",
            backend="test-backend",
            model="test-model",
            input_tokens=500,
        )
    )
    ledger.provider_attempt(
        ModelCallProviderAttemptParams(
            call_id="call-provider-retry",
            attempt_id="http-1",
            status="started",
            method="POST",
            path="/v1/messages",
        )
    )
    clock.advance(1.0)
    ledger.provider_attempt(
        ModelCallProviderAttemptParams(
            call_id="call-provider-retry",
            attempt_id="http-1",
            status="failed",
            method="POST",
            path="/v1/messages",
            http_status=529,
            error_type="HTTPError",
            retry_scheduled=True,
        )
    )
    ledger.provider_attempt(
        ModelCallProviderAttemptParams(
            call_id="call-provider-retry",
            attempt_id="http-2",
            status="started",
            method="POST",
            path="/v1/messages",
        )
    )
    clock.advance(1.0)
    ledger.provider_attempt(
        ModelCallProviderAttemptParams(
            call_id="call-provider-retry",
            attempt_id="http-2",
            status="response_opened",
            method="POST",
            path="/v1/messages",
            http_status=200,
        )
    )

    (record,) = ledger.records()

    assert record.provider_attempt_count == 2
    assert record.provider_attempts[0]["status"] == "failed"
    assert record.provider_attempts[0]["retry_scheduled"] is True
    assert record.provider_attempts[1]["status"] == "response_opened"
    assert record.provider_attempts[1]["http_status"] == 200


def test_same_logical_model_turn_preserves_distinct_physical_attempts() -> None:
    class _ContextSink:
        def __init__(self) -> None:
            self.rows: list[dict[str, object]] = []

        def write_context_usage(self, usage: dict[str, object]) -> bool:
            self.rows.append(dict(usage))
            return True

    sink = _ContextSink()
    agent = SimpleNamespace(
        backend=SimpleNamespace(
            name="test-backend",
            model_name="test-model",
            max_tokens=128,
        ),
        config=SimpleNamespace(request_timeout=10),
    )
    request = SimpleNamespace(
        agent=agent,
        prompt="same prompt",
        tool_rounds=2,
        params=SimpleNamespace(
            request_id="request-1",
            run_id="run-1",
            task_id="task-1",
            # 门槛1 后记账走 model_visible_context_tokens, 需 run snapshot;
            # text 协议下口径退化为 estimate_tokens(prompt), 不影响本测试意图
            # (逻辑 turn 去重 + 物理 attempt 计数)。
            tool_protocol_snapshot=make_test_protocol_snapshot(
                run_id="run-1", source_protocol="native"
            ),
            effective_on_chunk=sink,
        ),
    )

    first_ledger, first_call_id, _ = start_model_call_record(request)
    second_ledger, second_call_id, _ = start_model_call_record(request)
    records = first_ledger.records()

    assert first_ledger is second_ledger
    assert first_call_id != second_call_id
    assert len(records) == 2
    assert records[0].metadata["logical_call_id"] == records[1].metadata["logical_call_id"]
    assert [record.metadata["physical_attempt"] for record in records] == [1, 2]
    assert len(sink.rows) == 2
    assert all(row["schema"] == "model_visible_context_usage.v1" for row in sink.rows)
    assert [row["current_tokens"] for row in sink.rows] == [
        records[0].input_tokens,
        records[1].input_tokens,
    ]
    assert model_call_summary(agent, request_id="request-1") == {
        "schema": "model_call_summary.v1",
        "logical_model_turn_count": 1,
        "physical_model_attempt_count": 2,
        "model_retry_count": 1,
        "provider_http_attempt_count": 0,
        "provider_http_retry_count": 0,
        "status_counts": {
            "started": 2,
            "first_token": 0,
            "finished": 0,
            "failed": 0,
            "timed_out": 0,
        },
        "backends": ["test-backend"],
        "models": ["test-model"],
    }


def test_summary_counts_all_calls_after_detail_retention_limit() -> None:
    agent = SimpleNamespace()
    ledger = ModelCallLedger(options=ModelCallLedgerOptions(max_records=4))
    agent._model_call_ledger = ledger

    for index in range(7):
        call_id = f"call-{index}"
        ledger.started(
            ModelCallStartedParams(
                call_id=call_id,
                backend="test-backend",
                model="test-model",
                input_tokens=10,
                request_id="request-over-limit",
                run_id="run-over-limit",
                metadata={"logical_call_id": f"logical-{index // 2}"},
            )
        )
        ledger.provider_attempt(
            ModelCallProviderAttemptParams(
                call_id=call_id,
                attempt_id=f"http-{index}-1",
                status="failed" if index == 3 else "response_opened",
                retry_scheduled=index == 3,
            )
        )
        if index == 3:
            ledger.provider_attempt(
                ModelCallProviderAttemptParams(
                    call_id=call_id,
                    attempt_id=f"http-{index}-2",
                    status="response_opened",
                )
            )
        ledger.finished(ModelCallFinishParams(call_id=call_id, output_tokens=1))

    assert len(ledger.records()) == 4
    assert model_call_summary(agent, request_id="request-over-limit") == {
        "schema": "model_call_summary.v1",
        "logical_model_turn_count": 4,
        "physical_model_attempt_count": 7,
        "model_retry_count": 3,
        "provider_http_attempt_count": 8,
        "provider_http_retry_count": 1,
        "status_counts": {
            "started": 0,
            "first_token": 0,
            "finished": 7,
            "failed": 0,
            "timed_out": 0,
        },
        "backends": ["test-backend"],
        "models": ["test-model"],
    }


def test_estimates_prefill_and_first_token_timeout_without_probe_samples() -> None:
    ledger = ModelCallLedger()

    estimate = estimate_first_token_timeout(
        FirstTokenTimeoutParams(
            input_tokens=2000,
            ledger=ledger,
            options=FirstTokenTimeoutOptions(
                estimated_prefill_tokens_per_second=500.0,
                base_first_token_seconds=3.0,
                safety_margin=2.0,
                min_timeout_seconds=1.0,
                max_timeout_seconds=60.0,
            ),
        )
    )

    assert estimate.source == "estimated_rate"
    assert estimate.prefill_seconds == 4.0
    assert estimate.first_token_seconds == 3.0
    assert estimate.timeout_seconds == 14.0


def test_estimates_first_token_timeout_from_5k_and_10k_probe_samples() -> None:
    """门槛4 语义: 每点 2 条样本(>= min_samples) -> probe 估计(窗口均值)。

    门槛4 前锁定为单样本即用; 「先固化再改」升级为最小样本数 2,
    source 带窗口样本数(_n2)。单样本回退语义由 gate4 测试覆盖。
    """
    clock = _FakeClock(0.0)
    ledger = ModelCallLedger(context=ModelCallLedgerContext(now=clock.now))
    _record_first_token_probe(ledger, clock, _ProbeSpec("probe-5k", 5000, 13.0))
    _record_first_token_probe(ledger, clock, _ProbeSpec("probe-5k-b", 5000, 13.5))
    _record_first_token_probe(ledger, clock, _ProbeSpec("probe-10k", 10000, 23.0))
    _record_first_token_probe(ledger, clock, _ProbeSpec("probe-10k-b", 10000, 23.5))

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

    # 窗口均值 13.25/23.25: slope=0.002, prefill(15000)=30, first_token=3.25
    assert estimate.source == "probe_5k_10k_n2"
    assert estimate.prefill_seconds == 30.0
    assert estimate.first_token_seconds == pytest.approx(3.25)
    assert estimate.timeout_seconds == pytest.approx(49.875)


def test_first_token_timeout_estimate_clamps_to_bounds() -> None:
    low = estimate_first_token_timeout(
        FirstTokenTimeoutParams(
            input_tokens=1,
            ledger=ModelCallLedger(),
            options=FirstTokenTimeoutOptions(
                estimated_prefill_tokens_per_second=10000.0,
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
                estimated_prefill_tokens_per_second=100.0,
                base_first_token_seconds=10.0,
                safety_margin=2.0,
                min_timeout_seconds=5.0,
                max_timeout_seconds=50.0,
            ),
        )
    )

    assert low.timeout_seconds == 5.0
    assert high.timeout_seconds == 50.0


def test_cache_suspected_mark_for_large_fast_first_token_record() -> None:
    estimate = estimate_first_token_timeout(
        FirstTokenTimeoutParams(
            input_tokens=12000,
            ledger=ModelCallLedger(),
            options=FirstTokenTimeoutOptions(
                estimated_prefill_tokens_per_second=400.0,
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


def _ledger(clock: _FakeClock) -> ModelCallLedger:
    return ModelCallLedger(
        options=ModelCallLedgerOptions(max_records=4),
        context=ModelCallLedgerContext(now=clock.now),
    )
