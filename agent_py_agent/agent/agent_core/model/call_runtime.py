
from __future__ import annotations

import hashlib
import threading
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
    model_call_purpose,
    summarize_model_call_records,
)
from ...conversation.context_usage import record_model_context_usage
from ...conversation.model_metrics import publish_model_metrics
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
    provider_usage_fields,
    reported_cache_read_token_usage,
    response_usage,
)

# LLM: 本模块把响应和传输事实写入唯一 ModelCallLedger；按字段保留用量来源，终态裁决与用途统计仍归账本，不能创建生成旁路。
# 模块用途: 连接实际调用与账本、超时及统计，让生成和决策响应共用用量读取并保留缺报事实。

_LEDGER_CREATION_LOCK = threading.Lock()
# LLM: 停机中断的唯一结构化原因码/类型；消费端按 error_code 识别"未结算"，不解析文案。
MODEL_CALL_INTERRUPTED_ERROR_CODE = "MODEL_CALL_INTERRUPTED_HOST_SHUTDOWN"
MODEL_CALL_INTERRUPTED_ERROR_TYPE = "HostShutdownInterrupted"


# LLM: The caller may pass the exact precomputed context snapshot so timeout, ledger, TUI and
# provider-observation calibration all share one measurement; fallback construction preserves old
# auxiliary/test callers without creating a second accounting path.
# 函数用途: 建立一次模型调用账本，投影真实轮次，并复用同一份上下文快照计算慢模型超时。
def start_model_call_record(
    request: object,
    *,
    context_snapshot: object | None = None,
) -> tuple[ModelCallLedger, str, object]:
    agent = getattr(request, "agent", None)
    ledger = model_call_ledger(agent)
    prompt = getattr(request, "prompt", "") or ""
    params = getattr(request, "params", None)
    # 记账口径与统一可见口径对齐（门槛1）：text 协议恒等，native 协议计入
    # IR messages/pending guidance/tools —— 首 token 预算按出站可见量估计，
    # 避免多轮工具后 prefill 时间低估（真机 600s ProviderTimeout 根因之一）。
    if context_snapshot is None:
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
                "thread_id": str((getattr(params, "task_attributes", None) or {}).get("conversation_thread_id") or ""),
                "logical_call_id": logical_call_id,
                "physical_attempt": physical_attempt,
                "first_token_timeout_estimate": estimate.to_dict(),
            },
        )
    )
    _publish_model_context_usage(request, context_snapshot.to_public_dict())
    publish_model_metrics(agent, params, pending=True)
    return ledger, call_id, estimate


# LLM: 真实工作片先持久化同一 numeric preflight 再投影，主/子都绑定 exact thread；隔离表达轮不覆盖。
# 函数用途: 在调用开始前保存并展示上下文数字，让空闲恢复不依赖仍在 Working 或内存里的旧帧。
def _publish_model_context_usage(
    request: object,
    usage: dict[str, object],
) -> bool:
    params = getattr(request, "params", None)
    if not _context_usage_projection_enabled(params):
        return False
    record_model_context_usage(getattr(request, "agent", None), params, usage)
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


# LLM: The model ledger owns token/first-event accounting. An optional status callback receives
# the same non-empty delta but its failure is isolated and cannot interrupt provider streaming.
# 函数用途: 统一记录模型流活动、过滤工具边界，并可通知慢模型状态投影。
def observed_chunk_filter(
    *,
    ledger: ModelCallLedger,
    call_id: str,
    chunk_filter: ToolBoundaryChunkFilter,
    first_token_estimate: object,
    activity_callback: object = None,
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
        if chunk and callable(activity_callback):
            try:
                activity_callback(chunk)
            except Exception:
                pass
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


# LLM: 已报字段由同一归一读取器提供；仅真实 text 可走原正文估算，无 text 的决策响应不得套空正文估算；缺报仍由字段来源表达。
# 函数用途: 把响应的用量和结束事实提交原账本，逐字段保留真值或估算，不为决策响应伪造生成内容。
def record_model_call_finished(ledger: ModelCallLedger, call_id: str, response: object) -> None:
    input_tokens = input_token_usage(response)
    output_tokens = output_token_usage(response)
    if output_tokens is None:
        output_tokens = estimate_tokens(response.text) if hasattr(response, "text") else 0
    ledger.finished(
        ModelCallFinishParams(
            call_id=call_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=reported_cache_read_token_usage(response),
            cache_creation_input_tokens=cache_creation_input_token_usage(response),
            provider_usage_reported=bool(response_usage(response)),
            provider_usage_fields=provider_usage_fields(response),
            # 流末事实必须随调用落账：真实事故（provider 断流 → 空正文 → USER_REPLY_UNAVAILABLE）
            # 事后只能从 attempt 事件侧推，因为 finish_reason/stop_reason/turn_end 当时没有落盘。
            stop_reason=str(getattr(response, "stop_reason", "") or ""),
            runtime_reason=str(getattr(response, "runtime_reason", "") or ""),
            turn_end_reason=str(getattr(response, "turn_end_reason", "") or ""),
            truncated=bool(getattr(response, "truncated", False)),
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
            request_surface=dict(event.get("request_surface") or {}),
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


# LLM: 同一 agent 的首次并发调用必须取得同一本账；只锁初始化，不串行模型调用，不能建立第二工厂或状态源。
# 函数用途: 原子取得或建立代理的模型调用账本，保留不能附加属性对象的旧临时账本行为。
def model_call_ledger(agent: object) -> ModelCallLedger:
    with _LEDGER_CREATION_LOCK:
        existing = getattr(agent, "_model_call_ledger", None)
        if isinstance(existing, ModelCallLedger):
            return existing
        ledger = ModelCallLedger()
        try:
            agent._model_call_ledger = ledger
        except Exception:
            return ledger
        return ledger


# LLM: 只读取 agent 上已存在的账本，停机时不新建账本；每条未结清调用按同一原因码记 failed，用量仍按缺报处理（不补零）。
#   返回的投影只含结构化身份与时长字段，不含提示词、响应正文或密钥；写事件由调用方（Gateway 收尾）负责。
# 函数用途: Gateway 停机排空后，把仍在途的模型调用统一记成"被停机中断、未结算"，并给出可写进停机事件的事实列表。
def settle_open_model_calls_for_shutdown(agent: object) -> tuple[dict[str, object], ...]:
    ledger = getattr(agent, "_model_call_ledger", None)
    if not isinstance(ledger, ModelCallLedger):
        return ()
    interrupted = ledger.fail_open_calls(
        error_type=MODEL_CALL_INTERRUPTED_ERROR_TYPE,
        error_code=MODEL_CALL_INTERRUPTED_ERROR_CODE,
    )
    return tuple(_interrupted_call_projection(record) for record in interrupted)


# LLM: 投影字段固定：调用/请求/run 身份、后端与模型名、账本用途桶、是否已见首 token、耗时、估算输入、HTTP 尝试数、
#   是否探针、原因码与 settlement=unsettled；新增字段要同步 test_gateway_model_call_shutdown_settlement.py。
# 函数用途: 把一条被中断的调用记录压成可写进 Gateway 停机事件的小字典。
def _interrupted_call_projection(record: Any) -> dict[str, object]:
    return {
        "call_id": record.call_id,
        "request_id": record.request_id,
        "run_id": record.run_id,
        "backend": record.backend,
        "model": record.model,
        "purpose": model_call_purpose(record),
        "first_token_seen": record.first_token_at is not None,
        "elapsed_seconds": round(float(record.total_latency_seconds or 0.0), 3),
        "estimated_input_tokens": int(record.input_tokens),
        "provider_attempt_count": int(record.provider_attempt_count),
        "is_probe": bool(record.is_probe),
        "error_code": record.error_code,
        "settlement": "unsettled",
    }


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


# LLM: 兼容入口只用于没有增量 scope 的保留明细；用量及用途规则复用原统计器，不维护第二份来源判断。
# 函数用途: 为旧账本或测试注入生成兼容摘要，正常运行仍读取原累计容器。
def _retained_model_call_summary(records: list[Any]) -> dict[str, object]:
    return {"schema": "model_call_summary.v1", **summarize_model_call_records(records)}


# LLM: 空摘要同样包含用途分区和字段计次；沿用旧空 status_counts，不把无记录解释成失败或已报零。
# 函数用途: 在尚无模型调用时返回完整统计形状，供最终响应与消费端统一读取。
def _empty_model_call_summary() -> dict[str, object]:
    return {"schema": "model_call_summary.v1", **summarize_model_call_records(()), "status_counts": {}}


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


# LLM: 超时参数来自当前工作片，包括该模型显式排队预算；不修改共享 backend 的流式 idle。
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
        queue_wait_seconds=float_config(config, "model_queue_wait_seconds", 0.0),
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
