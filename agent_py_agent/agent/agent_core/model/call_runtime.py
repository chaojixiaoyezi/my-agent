
from __future__ import annotations

import hashlib
import uuid
from typing import Any

from ...contracts.model_call_ledger import (
    ModelCallActivityParams,
    ModelCallFailureParams,
    ModelCallFinishParams,
    ModelCallFirstTokenParams,
    ModelCallLedger,
    ModelCallProviderAttemptParams,
    ModelCallStartedParams,
    ModelCallTimeoutParams,
)
from ...memory_archive import estimate_tokens
from ..tool_stream import ToolBoundaryChunkFilter
from .call_monitor import (
    FirstTokenTimeoutOptions,
    FirstTokenTimeoutParams,
    estimate_first_token_timeout,
    is_cache_suspected,
)
from .context_pressure import model_visible_context_snapshot
from .usage import (
    cache_creation_input_token_usage,
    input_token_usage,
    output_token_usage,
    reported_cache_read_token_usage,
    response_usage,
)

# LLM: 本模块把 provider/model 生命周期写入唯一 ModelCallLedger，并只投影结构化计数与超时事实。
# 模块用途: 连接一次真实模型调用与账本、动态超时、流式活动观测和最终统计。


def start_model_call_record(request: object) -> tuple[ModelCallLedger, str, object]:
    agent = getattr(request, "agent", None)
    ledger = model_call_ledger(agent)
    prompt = str(getattr(request, "prompt", "") or "")
    params = getattr(request, "params", None)
    # 记账口径与统一可见口径对齐（门槛1）：text 协议恒等，native 协议计入
    # IR messages/pending guidance/tools —— 首 token 预算按出站可见量估计，
    # 避免多轮工具后 prefill 时间低估（真机 600s ProviderTimeout 根因之一）。
    context_snapshot = model_visible_context_snapshot(agent, params, prompt)
    input_tokens = context_snapshot.current_tokens
    estimate = estimate_first_token_timeout(
        FirstTokenTimeoutParams(
            input_tokens=input_tokens,
            ledger=ledger,
            options=first_token_timeout_options(getattr(request, "agent", None)),
        )
    )
    logical_call_id = logical_model_call_id(request, input_tokens)
    physical_attempt = 1 + sum(
        str(record.metadata.get("logical_call_id") or "") == logical_call_id
        for record in ledger.records()
    )
    call_id = (
        f"{logical_call_id}:attempt-{physical_attempt}:"
        f"{uuid.uuid4().hex[:8]}"
    )
    backend = getattr(agent, "backend", None)
    ledger.started(
        ModelCallStartedParams(
            call_id=call_id,
            backend=str(getattr(backend, "name", "") or ""),
            model=model_name(getattr(request, "agent", None)),
            input_tokens=input_tokens,
            output_tokens_estimate=max_output_tokens(getattr(request, "agent", None)),
            request_id=str(getattr(params, "request_id", "") or ""),
            run_id=str(getattr(params, "run_id", "") or ""),
            metadata={
                "tool_rounds": getattr(request, "tool_rounds", 0),
                "task_id": str(getattr(params, "task_id", "") or ""),
                "logical_call_id": logical_call_id,
                "physical_attempt": physical_attempt,
                "first_token_timeout_estimate": estimate.to_dict(),
            },
        )
    )
    _publish_model_context_usage(request, context_snapshot.to_public_dict())
    return ledger, call_id, estimate


# LLM: Live context display is an optional typed sink capability for durable
# task turns only. Missing/isolated capabilities stay silent; raw prompts,
# messages, guidance, and tool schemas must never be passed to the sink.
# 函数用途: 在真实任务调用开始前投影无正文 token 快照；临时表达轮只计费、不覆盖任务界面。
def _publish_model_context_usage(
    request: object,
    usage: dict[str, object],
) -> bool:
    params = getattr(request, "params", None)
    if not _context_usage_projection_enabled(params):
        return False
    _publish_subagent_context_usage(request, usage)
    sink = getattr(params, "effective_on_chunk", None)
    writer = getattr(sink, "write_context_usage", None)
    if not callable(writer):
        return False
    return writer(usage) is not False


# LLM: Isolated no-save model calls produce user-facing wording only. Their
# tiny prompt budget remains in cost/accounting ledgers but cannot replace the
# active task's context-usage projection in TUI/Web or child state.
# 函数用途: 阻止临时表达轮把真实主任务的上下文数字覆盖成一个很小的假读数。
def _context_usage_projection_enabled(params: object) -> bool:
    return str(getattr(params, "context_scope", "") or "").strip().lower() != "isolated"


# LLM: A task-local runner has no foreground TUI sink, so its provider-visible
# context snapshot must cross the process boundary through the canonical child
# run record. The runner trace accepts numbers only and never receives prompt,
# messages, tool schemas, or guidance text.
# 函数用途: 每次子代理真正调用模型前，把当前上下文总 token 写入该 run 的只读展示快照。
def _publish_subagent_context_usage(
    request: object,
    usage: dict[str, object],
) -> None:
    from ..runner.stage_trace import (
        RunnerModelContextUsageTraceRequest,
        trace_runner_model_context_usage,
    )

    trace_runner_model_context_usage(
        RunnerModelContextUsageTraceRequest(
            agent=getattr(request, "agent", None),
            params=getattr(request, "params", None),
            usage=usage,
        )
    )


def observed_chunk_filter(
    *,
    ledger: ModelCallLedger,
    call_id: str,
    chunk_filter: ToolBoundaryChunkFilter,
    first_token_estimate: object,
):
    seen_first_token = False

    def _on_chunk(chunk: str) -> None:
        nonlocal seen_first_token
        if chunk and not seen_first_token:
            seen_first_token = True
            record_model_call_first_token(ledger, call_id, chunk, first_token_estimate)
        elif chunk:
            ledger.activity(
                ModelCallActivityParams(
                    call_id=call_id,
                    output_tokens_seen=estimate_tokens(chunk),
                )
            )
        chunk_filter(chunk)

    return _on_chunk


def record_model_call_first_token(
    ledger: ModelCallLedger,
    call_id: str,
    chunk: str,
    first_token_estimate: object,
) -> None:
    record = next((item for item in ledger.records() if item.call_id == call_id), None)
    if record is None:
        return
    latency = max(0.0, float(ledger.context.now()) - record.started_at)
    ledger.first_token(
        ModelCallFirstTokenParams(
            call_id=call_id,
            output_tokens_seen=estimate_tokens(chunk),
            cache_suspected=is_cache_suspected(
                input_tokens=record.input_tokens,
                first_token_latency_seconds=latency,
                estimate=first_token_estimate,
            ),
        )
    )


def record_model_call_finished(ledger: ModelCallLedger, call_id: str, response: object) -> None:
    input_tokens = input_token_usage(response)
    output_tokens = output_token_usage(response)
    if output_tokens is None:
        output_tokens = estimate_tokens(getattr(response, "text", ""))
    ledger.finished(
        ModelCallFinishParams(
            call_id=call_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=reported_cache_read_token_usage(response),
            cache_creation_input_tokens=cache_creation_input_token_usage(response),
            provider_usage_reported=bool(response_usage(response)),
        )
    )


def record_model_call_failed(
    ledger: ModelCallLedger,
    call_id: str,
    exc: BaseException,
) -> None:
    ledger.failed(
        ModelCallFailureParams(
            call_id=call_id,
            error_type=type(exc).__name__,
            error_code=str(
                getattr(exc, "error_code", "")
                or getattr(exc, "code", "")
                or ""
            ),
        )
    )


def record_model_provider_attempt(
    ledger: ModelCallLedger,
    call_id: str,
    event: dict[str, object],
) -> None:
    ledger.provider_attempt(
        ModelCallProviderAttemptParams(
            call_id=call_id,
            attempt_id=str(event.get("attempt_id") or ""),
            status=str(event.get("status") or "unknown"),
            method=str(event.get("method") or ""),
            path=str(event.get("path") or ""),
            http_status=_nonnegative_int(event.get("http_status")),
            error_type=str(event.get("error_type") or ""),
            retry_scheduled=event.get("retry_scheduled") is True,
        )
    )


def record_model_call_timeout(
    *,
    ledger: ModelCallLedger,
    call_id: str,
    timeout_seconds: float,
    timeout_stage: str,
    elapsed_seconds: float = 0.0,
    idle_silence_seconds: float | None = None,
) -> None:
    """落超时账。``elapsed_seconds`` 是掐断时刻的真实墙钟经过
    （调用方从 record.started_at 计算；门槛2 前证据链断在调用点，
    参数层不暴露 elapsed）。``idle_silence_seconds`` 是最后活动到超时的
    静默时长（调用方从 record.last_activity_at 计算，只记秒数不混
    token 延迟）；None=缺失/未计算，数值(含 0.0)=真实计算——「缺失/回退」
    与「真实零静默」可辨识(seq1613c)。"""
    ledger.timeout(
        ModelCallTimeoutParams(
            call_id=call_id,
            timeout_seconds=timeout_seconds,
            timeout_stage=timeout_stage,
            elapsed_seconds=max(0.0, float(elapsed_seconds)),
            idle_silence_seconds=(
                max(0.0, float(idle_silence_seconds))
                if idle_silence_seconds is not None
                else None
            ),
        )
    )


def model_call_ledger(agent: object) -> ModelCallLedger:
    existing = getattr(agent, "_model_call_ledger", None)
    if isinstance(existing, ModelCallLedger):
        return existing
    ledger = ModelCallLedger()
    try:
        agent._model_call_ledger = ledger
    except Exception:
        return ledger
    return ledger


# LLM: 优先读取不受明细裁剪影响的累计 scope；仅为旧账本或测试私有注入保留 retained-record 回退。
# 函数用途: 汇总当前 request/run 的模型回合、重试、终态与供应商 token 消耗。
def model_call_summary(
    agent: object,
    *,
    request_id: str = "",
    run_id: str = "",
) -> dict[str, object]:
    ledger = getattr(agent, "_model_call_ledger", None)
    if not isinstance(ledger, ModelCallLedger):
        return _empty_model_call_summary()
    request_id = str(request_id or "").strip()
    run_id = str(run_id or "").strip()
    cumulative = ledger.cumulative_summary(request_id=request_id, run_id=run_id)
    if cumulative is not None:
        return {
            "schema": "model_call_summary.v1",
            **cumulative,
        }
    records = [
        record
        for record in ledger.records()
        if (
            (request_id and record.request_id == request_id)
            or (not request_id and run_id and record.run_id == run_id)
        )
    ]
    if not records:
        return _empty_model_call_summary()
    return _retained_model_call_summary(records)


# LLM: This path exists only for legacy/test ledgers without cumulative scope;
# it must preserve the public summary schema while staying independent of the
# bounded-record aggregate implementation.
# 函数用途: 从仍保留的模型调用明细计算兼容汇总，供旧账本和测试注入使用。
def _retained_model_call_summary(records: list[Any]) -> dict[str, object]:
    """Build a compatibility summary from retained model-call records."""
    logical_ids = {
        str(record.metadata.get("logical_call_id") or record.call_id)
        for record in records
    }
    statuses = {
        status: sum(record.status == status for record in records)
        for status in ("started", "first_token", "finished", "failed", "timed_out")
    }
    provider_attempts = sum(record.provider_attempt_count for record in records)
    provider_retries = sum(
        max(0, record.provider_attempt_count - 1)
        for record in records
    )
    accounted_input_tokens = sum(
        max(0, int(record.accounted_input_tokens)) for record in records
    )
    output_tokens = sum(max(0, int(record.output_tokens)) for record in records)
    provider_usage_call_count = sum(
        record.status == "finished" and record.provider_usage_reported
        for record in records
    )
    estimated_usage_call_count = sum(
        record.status == "finished" and not record.provider_usage_reported
        for record in records
    )
    return {
        "schema": "model_call_summary.v1",
        "logical_model_turn_count": len(logical_ids),
        "physical_model_attempt_count": len(records),
        "model_retry_count": max(0, len(records) - len(logical_ids)),
        "provider_http_attempt_count": provider_attempts,
        "provider_http_retry_count": provider_retries,
        "status_counts": statuses,
        "backends": sorted({record.backend for record in records if record.backend}),
        "models": sorted({record.model for record in records if record.model}),
        "accounted_input_tokens": accounted_input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": accounted_input_tokens + output_tokens,
        "cached_input_tokens": sum(
            max(0, int(record.cached_input_tokens)) for record in records
        ),
        "cache_creation_input_tokens": sum(
            max(0, int(record.cache_creation_input_tokens)) for record in records
        ),
        "provider_usage_call_count": provider_usage_call_count,
        "estimated_usage_call_count": estimated_usage_call_count,
        "usage_breakdown": _retained_usage_breakdown(records),
    }


# LLM: Provider-reported usage and fallback estimates are disjoint accounting
# partitions. Never fill a provider bucket from an estimate in this projection.
# 函数用途: 将保留明细按供应商真值和本地估算拆账，供兼容汇总展示成本口径。
def _retained_usage_breakdown(records: list[Any]) -> dict[str, object]:
    """Partition retained records into provider and estimated usage."""
    provider_records = [
        record
        for record in records
        if record.status == "finished" and record.provider_usage_reported
    ]
    estimated_records = [
        record
        for record in records
        if record.status == "finished" and not record.provider_usage_reported
    ]
    return {
        "schema": "model_usage_breakdown.v1",
        "provider": {
            "input_tokens": sum(
                max(0, int(record.accounted_input_tokens))
                for record in provider_records
            ),
            "output_tokens": sum(
                max(0, int(record.output_tokens)) for record in provider_records
            ),
            "cache_read_input_tokens": sum(
                max(0, int(record.cached_input_tokens)) for record in provider_records
            ),
            "cache_write_input_tokens": sum(
                max(0, int(record.cache_creation_input_tokens))
                for record in provider_records
            ),
            "call_count": len(provider_records),
        },
        "estimated": {
            "input_tokens": sum(
                max(0, int(record.accounted_input_tokens))
                for record in estimated_records
            ),
            "output_tokens": sum(
                max(0, int(record.output_tokens)) for record in estimated_records
            ),
            "call_count": len(estimated_records),
        },
    }


def _empty_model_call_summary() -> dict[str, object]:
    return {
        "schema": "model_call_summary.v1",
        "logical_model_turn_count": 0,
        "physical_model_attempt_count": 0,
        "model_retry_count": 0,
        "provider_http_attempt_count": 0,
        "provider_http_retry_count": 0,
        "status_counts": {},
        "backends": [],
        "models": [],
        "accounted_input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cached_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "provider_usage_call_count": 0,
        "estimated_usage_call_count": 0,
        "usage_breakdown": {
            "schema": "model_usage_breakdown.v1",
            "provider": {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_write_input_tokens": 0,
                "call_count": 0,
            },
            "estimated": {
                "input_tokens": 0,
                "output_tokens": 0,
                "call_count": 0,
            },
        },
    }


def effective_model_request_timeout_seconds(agent: object, first_token_timeout_seconds: float) -> float:
    base_timeout = model_request_timeout_seconds(agent)
    if base_timeout <= 0:
        return 0.0
    if not has_dynamic_timeout_config(agent):
        return base_timeout
    options = first_token_timeout_options(agent)
    dynamic_timeout = _clamp_dynamic_request_timeout(
        max(0.0, float(first_token_timeout_seconds)) + _output_generation_timeout_seconds(agent),
        options,
    )
    return max(base_timeout, dynamic_timeout)


def model_request_timeout_seconds(agent: object) -> float:
    config = getattr(agent, "config", None)
    raw = getattr(config, "request_timeout", None)
    if raw is None:
        raw = getattr(getattr(agent, "backend", None), "request_timeout", 0)
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return timeout if timeout > 0 else 0.0


def has_dynamic_timeout_config(agent: object) -> bool:
    config = getattr(agent, "config", None)
    return any(hasattr(config, name) for name in ("dynamic_timeout_min", "dynamic_timeout_max", "dynamic_timeout_safety_margin"))


# LLM: Runtime timeout options come only from normalized config and feed request-local first-event estimation.
# 函数用途: 从当前代理配置读取慢模型首事件预算参数，供主代理和各级子代理共用。
def first_token_timeout_options(agent: object) -> FirstTokenTimeoutOptions:
    config = getattr(agent, "config", None)
    return FirstTokenTimeoutOptions(
        estimated_prefill_tokens_per_second=float_config(
            config,
            "estimated_prefill_tokens_per_second",
            200.0,
        ),
        safety_margin=float_config(config, "dynamic_timeout_safety_margin", 1.5),
        min_timeout_seconds=float_config(config, "dynamic_timeout_min", 5.0),
        max_timeout_seconds=float_config(config, "dynamic_timeout_max", 120.0),
        # 门槛4: probe 统计参数(最小样本数/滑窗上限/去极值开关), 默认 2/5/True
        probe_min_samples=int(float_config(config, "probe_min_samples", 2)),
        probe_window_samples=int(float_config(config, "probe_window_samples", 5)),
        probe_outlier_trim=bool_config(config, "probe_outlier_trim", True),
    )


def logical_model_call_id(request: object, input_tokens: int) -> str:
    params = getattr(request, "params", None)
    seed = "|".join(
        [
            str(getattr(params, "request_id", "") or ""),
            str(getattr(params, "run_id", "") or ""),
            str(getattr(params, "task_id", "") or ""),
            str(getattr(request, "tool_rounds", 0)),
            str(input_tokens),
            hashlib.sha256(str(getattr(request, "prompt", "") or "").encode("utf-8")).hexdigest()[:16],
        ]
    )
    return f"model-call:{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:24]}"


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def model_name(agent: object) -> str:
    backend = getattr(agent, "backend", None)
    config = getattr(agent, "config", None)
    return str(getattr(backend, "model_name", "") or getattr(config, "model_name", "") or getattr(config, "model", "") or "")


def max_output_tokens(agent: object) -> int:
    raw = getattr(getattr(agent, "backend", None), "max_tokens", None)
    if raw is None:
        raw = getattr(getattr(agent, "config", None), "max_tokens", 0)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


# LLM: Non-stream total budgeting may estimate full output time; stream liveness must not use this as a hidden wall clock.
# 函数用途: 按最大输出 token 与保守速度估算非流式生成时间。
def _output_generation_timeout_seconds(agent: object) -> float:
    tokens = max_output_tokens(agent)
    if tokens <= 0:
        return 0.0
    config = getattr(agent, "config", None)
    rate = max(
        1.0,
        float_config(config, "estimated_output_tokens_per_second", 20.0),
    )
    return tokens / rate


def _clamp_dynamic_request_timeout(value: float, options: FirstTokenTimeoutOptions) -> float:
    minimum = max(0.0, float(options.min_timeout_seconds))
    maximum = max(minimum, float(options.max_timeout_seconds))
    return max(minimum, min(maximum, float(value)))


def float_config(config: object, name: str, default: float) -> float:
    try:
        return float(getattr(config, name, default))
    except (TypeError, ValueError):
        return default


def bool_config(config: object, name: str, default: bool) -> bool:
    raw = getattr(config, name, None)
    if raw is None:
        return default
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(raw)


__all__ = [
    "effective_model_request_timeout_seconds",
    "model_request_timeout_seconds",
    "model_call_summary",
    "observed_chunk_filter",
    "record_model_call_failed",
    "record_model_call_finished",
    "record_model_provider_attempt",
    "record_model_call_timeout",
    "start_model_call_record",
]
