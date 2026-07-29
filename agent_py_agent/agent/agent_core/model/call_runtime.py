
from __future__ import annotations

import hashlib
import uuid
from typing import Any

from ...contracts.model_call_ledger import (
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
from .usage import output_token_usage


def start_model_call_record(request: object) -> tuple[ModelCallLedger, str, object]:
    ledger = model_call_ledger(getattr(request, "agent", None))
    prompt = str(getattr(request, "prompt", "") or "")
    input_tokens = estimate_tokens(prompt)
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
    params = getattr(request, "params", None)
    backend = getattr(getattr(request, "agent", None), "backend", None)
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
    return ledger, call_id, estimate


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
    output_tokens = output_token_usage(response)
    if output_tokens is None:
        output_tokens = estimate_tokens(getattr(response, "text", ""))
    ledger.finished(ModelCallFinishParams(call_id=call_id, output_tokens=output_tokens))


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
) -> None:
    ledger.timeout(
        ModelCallTimeoutParams(
            call_id=call_id,
            timeout_seconds=timeout_seconds,
            timeout_stage=timeout_stage,
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


def first_token_timeout_options(agent: object) -> FirstTokenTimeoutOptions:
    config = getattr(agent, "config", None)
    return FirstTokenTimeoutOptions(
        safety_margin=float_config(config, "dynamic_timeout_safety_margin", 1.5),
        min_timeout_seconds=float_config(config, "dynamic_timeout_min", 5.0),
        max_timeout_seconds=float_config(config, "dynamic_timeout_max", 120.0),
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


def _output_generation_timeout_seconds(agent: object) -> float:
    tokens = max_output_tokens(agent)
    if tokens <= 0:
        return 0.0
    return tokens / 30.0


def _clamp_dynamic_request_timeout(value: float, options: FirstTokenTimeoutOptions) -> float:
    minimum = max(0.0, float(options.min_timeout_seconds))
    maximum = max(minimum, float(options.max_timeout_seconds))
    return max(minimum, min(maximum, float(value)))


def float_config(config: object, name: str, default: float) -> float:
    try:
        return float(getattr(config, name, default))
    except (TypeError, ValueError):
        return default


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
