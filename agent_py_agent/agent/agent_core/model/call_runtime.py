# LLM: 模型调用账本及同次采样CAS绑定在这里；输出上限复用 settings/defaults 的唯一公式（窗口没填按 128000 兜底），
#   不另写数值。记录函数会写调用账本；排队不计物理尝试，已结清的迟到完成抛原准入异常，不签发成功来源。
# 模块用途: 记录每次模型调用的开始、首包、结束、失败与超时，并给 compact 预算和超时估算提供输出上限。
from __future__ import annotations

import hashlib
import threading
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from weakref import ref

from ...contracts.model_call_ledger import (
    ModelCallActivityParams,
    ModelCallAdmissionClosedError,
    ModelCallAdmissionClosure,
    ModelCallFailureParams,
    ModelCallFinishParams,
    ModelCallFirstTokenParams,
    ModelCallLedger,
    ModelCallProviderAttemptParams,
    ModelCallStartedParams,
    ModelCallTimeoutParams,
    close_model_call_admission,
    model_call_admission_closure,
    model_call_purpose,
    summarize_model_call_records,
)
from ...conversation.context_usage import (
    ContextUsageBinding,
    capture_context_usage_binding,
    record_model_context_usage,
)
from ...conversation.model_metrics import publish_model_metrics
from ...memory_archive import estimate_tokens
from ...observability.task_call_inspector import (
    bind_model_call_telemetry,
    record_model_call_telemetry,
)
from ...settings.defaults import context_window_or_default, output_cap_for_window
from ..dynamic_timeout import DYNAMIC_TIMEOUT_SAFETY_MARGIN
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
# 仅记当前线程最近完成的作用域、账本弱引用及原账时钟，不跨worker、owner或run借用时间。
_LAST_MODEL_CALL_COMPLETED_AT = threading.local()
# LLM: 停机中断的唯一结构化原因码/类型；消费端按 error_code 识别"未结算"，不解析文案。
MODEL_CALL_INTERRUPTED_ERROR_CODE = "MODEL_CALL_INTERRUPTED_HOST_SHUTDOWN"
MODEL_CALL_INTERRUPTED_ERROR_TYPE = "HostShutdownInterrupted"
# Gateway 停机时关闭模型调用准入用的关门原因：在途调用按它记 failed，之后的新调用按它被拒绝。
_HOST_SHUTDOWN_CLOSURE = ModelCallAdmissionClosure(
    error_type=MODEL_CALL_INTERRUPTED_ERROR_TYPE,
    error_code=MODEL_CALL_INTERRUPTED_ERROR_CODE,
)


# LLM: 超时/账本/TUI共用预计算快照；估算单独保留，组装用单调时钟，轮间隔只借同线程同账本同run/request的终态点。
# 函数用途: 登记调用及组装/轮间耗时、绑定可选任务遥测，并按同一快照计算慢模型超时。
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
        binding = capture_context_usage_binding(agent, params)
        context_snapshot = model_visible_context_snapshot(agent, params, prompt, usage_binding=binding)
    input_tokens = context_snapshot.current_tokens
    estimate = estimate_first_token_timeout(FirstTokenTimeoutParams(
        input_tokens=input_tokens, ledger=ledger, options=first_token_timeout_options(agent)))
    logical_call_id = logical_model_call_id(request)
    physical_attempt = 1 + sum(
        str(record.metadata.get("logical_call_id") or "") == logical_call_id
        for record in ledger.records()
    )
    call_id = f"{logical_call_id}:attempt-{physical_attempt}:{uuid.uuid4().hex[:8]}"
    backend = getattr(agent, "backend", None)
    from ...settings.model_scope import model_selection_metadata
    from .response_provenance import response_call_purpose

    assembly_started = float(getattr(request, "context_assembly_started_at", 0.0) or 0.0)
    context_assembly_ms = (
        round(max(0.0, (time.monotonic() - assembly_started) * 1000.0), 3)
        if assembly_started > 0.0
        else 0.0
    )
    ledger.started(ModelCallStartedParams(
            call_id=call_id, backend=str(getattr(backend, "name", "") or ""), model=model_name(agent),
            input_tokens=input_tokens, output_tokens_estimate=max_output_tokens(agent),
            request_id=str(getattr(params, "request_id", "") or ""), run_id=str(getattr(params, "run_id", "") or ""),
            metadata={
                "purpose": response_call_purpose(params), "attempt_id": str(getattr(params, "attempt_id", "") or ""),
                "tool_rounds": getattr(request, "tool_rounds", 0),
                "task_id": str(getattr(params, "task_id", "") or ""),
                "thread_id": str((getattr(params, "task_attributes", None) or {}).get("conversation_thread_id") or ""),
                "logical_call_id": logical_call_id,
                "physical_attempt": physical_attempt, "model_selection": model_selection_metadata(agent),
                "first_token_timeout_estimate": estimate.to_dict(),
                "context_assembly_ms": context_assembly_ms,
                "inter_round_gap_ms": _model_call_inter_round_gap_ms(ledger, params),
                "estimated_input_tokens": input_tokens,
            },
        ))
    with suppress(Exception):
        bind_model_call_telemetry(ledger, call_id, agent, params)
    _publish_model_context_usage(request, context_snapshot.to_public_dict(),
                                 binding=getattr(context_snapshot, "usage_binding", None))
    publish_model_metrics(agent, params, pending=True)
    return ledger, call_id, estimate


# LLM: 用该账本的时钟与原终态点相减，兼容注入时钟；没有可靠前序或时钟倒退保持None，不伪造首轮零间隔。
# 函数用途: 计算同线程、账本和请求运行从上次完成到本次登记的宿主间隔。
def _model_call_inter_round_gap_ms(ledger: ModelCallLedger, params: object) -> float | None:
    previous = getattr(_LAST_MODEL_CALL_COMPLETED_AT, "last", None)
    scope = (str(getattr(params, "run_id", "") or ""), str(getattr(params, "request_id", "") or ""))
    if previous is None or previous[0]() is not ledger or previous[1] != scope or not any(scope):
        return None
    gap = float(ledger.context.now()) - previous[2]
    return round(gap * 1000.0, 3) if gap >= 0.0 else None


# LLM: 只观察首终态的固定时间，不以回调到达时间重签；迟到/重复回调不回退同账本时间，弱引用不延长账本寿命。
# 函数用途: 在线程内记住最近完成、失败或超时的模型调用边界，供下一轮计算宿主间隔。
def _remember_model_call_completion(ledger: ModelCallLedger, record: object) -> None:
    completed = record.finished_at if record.finished_at is not None else record.last_activity_at
    previous = getattr(_LAST_MODEL_CALL_COMPLETED_AT, "last", None)
    if previous is not None and previous[0]() is ledger and previous[2] > completed:
        return
    _LAST_MODEL_CALL_COMPLETED_AT.last = (ref(ledger), (record.run_id, record.request_id), float(completed))


# LLM: 数值与采样时 typed 版本成套保存；禁止用 publish 时最新线程重签旧数，原 live sink 仍只显示本请求实际计量。
# 函数用途: 在调用开始前交原CAS保存并向请求自己的出口展示数字，缺持久绑定不挡模型或临时展示。
def _publish_model_context_usage(
    request: object,
    usage: dict[str, object],
    *,
    binding: ContextUsageBinding | None = None,
) -> bool:
    params = getattr(request, "params", None)
    if not _context_usage_projection_enabled(params):
        return False
    record_model_context_usage(getattr(request, "agent", None), params, usage, binding=binding)
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

    # LLM: 仅观察结构化流活动；可选展示异常不得中断原chunk过滤器。
    # 函数用途: 给首事件和后续输出记时，并继续处理当前文本块。
    def _on_chunk(chunk: str) -> None:
        nonlocal seen_first_token
        if chunk and not seen_first_token:
            seen_first_token = True
            record_model_call_first_token(ledger, call_id, chunk, first_token_estimate)
        elif chunk:
            ledger.activity(ModelCallActivityParams(call_id=call_id, output_tokens_seen=estimate_tokens(chunk)))
        if chunk and callable(activity_callback):
            try:
                activity_callback(chunk)
            except Exception:
                pass
        chunk_filter(chunk)

    return _on_chunk


# LLM: 只更新原账本同call_id，不从流正文派生身份或新请求；联测model_call_ledger。
# 函数用途: 保存首个输出事件的延迟与缓存疑似事实。
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
            call_id=call_id, output_tokens_seen=estimate_tokens(chunk),
            cache_suspected=is_cache_suspected(input_tokens=record.input_tokens,
                                             first_token_latency_seconds=latency, estimate=first_token_estimate),
        )
    )


# LLM: 用量沿原归一读取器，首终态仍归原 ledger；准确调用已 failed 且原因与唯一准入 closure 完全匹配时抛原异常，
#   阻止生成出口签发来源或执行迟到工具。已成功、超时及其它失败不重新归类；不能按正文猜停机或重开终态。
#   线程内完成点取原账终态时间，不用遥测落盘结束或迟到通知时刻覆盖。
# 函数用途: 提交完成、记录原终态时间并投影任务轮次，停机后迟到响应仍走原准入异常出口。
def record_model_call_finished(ledger: ModelCallLedger, call_id: str, response: object) -> None:
    input_tokens = input_token_usage(response)
    output_tokens = output_token_usage(response)
    if output_tokens is None:
        output_tokens = estimate_tokens(response.text) if hasattr(response, "text") else 0
    record = ledger.finished(ModelCallFinishParams(
            call_id=call_id, input_tokens=input_tokens, output_tokens=output_tokens,
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
        ))
    _remember_model_call_completion(ledger, record)
    with suppress(Exception):
        record_model_call_telemetry(ledger, record)
    closure = model_call_admission_closure()
    if (closure is not None and record.status == "failed"
            and record.error_type == closure.error_type and record.error_code == closure.error_code):
        raise ModelCallAdmissionClosedError(closure)


# LLM: 首个失败仍归原ledger，原终态时间只记线程内；可选遥测失败不能改变账本或主链。
# 函数用途: 保存失败及完成边界，并自动投影当前任务轮次。
def record_model_call_failed(
    ledger: ModelCallLedger,
    call_id: str,
    exc: BaseException,
) -> None:
    record = ledger.failed(ModelCallFailureParams(
            call_id=call_id, error_type=type(exc).__name__,
            error_code=str(getattr(exc, "error_code", "") or getattr(exc, "code", "") or ""),
        ))
    _remember_model_call_completion(ledger, record)
    with suppress(Exception):
        record_model_call_telemetry(ledger, record)


# LLM: 排队只属展示、不得登记物理尝试；其余传输事件按结构化字段转成账本参数（含 progress 与分段计时），不读正文；
#   非 dict 的 transport 按未计时处理。事件形状改动须同步 gateway_helpers._gateway_request_attempt 与账本 provider_attempt。
# 函数用途: 把真实 HTTP 尝试记到对应模型调用记录，主请求、辅助、决策与探针共用过滤边界。
def record_model_provider_attempt(
    ledger: ModelCallLedger,
    call_id: str,
    event: dict[str, object],
) -> None:
    if event.get("status") == "queued":
        return
    ledger.provider_attempt(
        ModelCallProviderAttemptParams(
            call_id=call_id, attempt_id=str(event.get("attempt_id") or ""), status=str(event.get("status") or "unknown"),
            method=str(event.get("method") or ""), path=str(event.get("path") or ""),
            http_status=_nonnegative_int(event.get("http_status")), error_type=str(event.get("error_type") or ""),
            retry_scheduled=event.get("retry_scheduled") is True,
            request_surface=dict(event.get("request_surface") or {}),
            transport=event["transport"] if isinstance(event.get("transport"), dict) else {},
        )
    )


# LLM: 超时落账的参数组；收成一个冻结小数据类是为了让调用点按名字传结构化事实、
#   不再逐个堆关键字参数（同文件其它记录函数也走同一种「参数数据类」写法）。
# 类用途: 承载一次超时落账需要的全部结构化事实（含只用于诊断的等待位置）。
@dataclass(frozen=True)
class ModelCallTimeoutFacts:
    call_id: str
    timeout_seconds: float
    timeout_stage: str
    # 门槛2: 掐断时刻的真实墙钟经过（调用方从 record.started_at 计算）。
    elapsed_seconds: float = 0.0
    # 门槛2(终审补证 seq1613c): 最后活动到超时的静默时长; None=缺失/未计算，
    # 数值(含 0.0)=真实计算——「缺失/回退」与「真实零静默」可辨识。
    idle_silence_seconds: float | None = None
    # 只用于诊断的连接位置（TIMEOUT_WAIT_PHASES 之一）；不参与任何放行/退避判定。
    timeout_wait_phase: str = ""


# LLM: 超时阶段和用量仍归原账本，线程内只取原终态时间；遥测不改超时诊断和重试语义。
# 函数用途: 提交超时、保存完成边界并自动记录任务模型轮次。
def record_model_call_timeout(ledger: ModelCallLedger, facts: ModelCallTimeoutFacts) -> None:
    """落超时账。``facts.timeout_wait_phase`` 只承载诊断用的连接位置（默认空串），
    不参与任何放行/退避判定，只透传给账本；其余字段语义见 ``ModelCallTimeoutFacts``。"""
    record = ledger.timeout(
        ModelCallTimeoutParams(
            call_id=facts.call_id, timeout_seconds=facts.timeout_seconds, timeout_stage=facts.timeout_stage,
            elapsed_seconds=max(0.0, float(facts.elapsed_seconds)),
            idle_silence_seconds=max(0.0, float(facts.idle_silence_seconds)) if facts.idle_silence_seconds is not None else None,
            timeout_wait_phase=facts.timeout_wait_phase,
        )
    )
    _remember_model_call_completion(ledger, record)
    with suppress(Exception):
        record_model_call_telemetry(ledger, record)


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


# LLM: 停机准入栅栏（J17 必须修）：关闭本进程模型调用准入并结清在途调用，委托 contracts.close_model_call_admission。
#   本进程每本账本都在构造时登记（Gateway agent、进程内 runner worker、owner 池作用域 agent 无一例外），关门后新建的账本
#   和已有账本都不再接新调用（ModelCallAdmissionClosedError），所以结清之后不会再冒出新的在途调用；不新建账本。
#   每条未结清调用按同一原因码记 failed，用量仍按缺报处理（不补零）。返回的投影只含结构化身份与时长字段，不含提示词、
#   响应正文或密钥；写事件由调用方（Gateway 收尾）负责。local/main 的子进程 runner 不在本进程，各自收口。
# 函数用途: Gateway 停机排空后关闭模型调用准入，把本进程仍在途的模型调用统一记成"被停机中断、未结算"，并给出事实列表。
def settle_open_model_calls_for_shutdown() -> tuple[dict[str, object], ...]:
    interrupted = close_model_call_admission(_HOST_SHUTDOWN_CLOSURE)
    return tuple(_interrupted_call_projection(record) for record in interrupted)


# LLM: 投影字段固定：调用/请求/run 身份、后端与模型名、账本用途桶、是否已见首 token、耗时、估算输入、HTTP 尝试数、
#   是否探针、原因码与 settlement=unsettled；新增字段要同步 test_gateway_model_call_shutdown_settlement.py。
# 函数用途: 把一条被中断的调用记录压成可写进 Gateway 停机事件的小字典。
def _interrupted_call_projection(record: Any) -> dict[str, object]:
    return {
        "call_id": record.call_id, "request_id": record.request_id, "run_id": record.run_id,
        "backend": record.backend, "model": record.model, "purpose": model_call_purpose(record),
        "first_token_seen": record.first_token_at is not None,
        "elapsed_seconds": round(float(record.total_latency_seconds or 0.0), 3),
        "estimated_input_tokens": int(record.input_tokens),
        "provider_attempt_count": int(record.provider_attempt_count),
        "is_probe": bool(record.is_probe), "error_code": record.error_code, "settlement": "unsettled",
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
        return {"schema": "model_call_summary.v1", **cumulative}
    records = [
        record for record in ledger.records()
        if ((request_id and record.request_id == request_id)
            or (not request_id and run_id and record.run_id == run_id))
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


# LLM: 动态预算复用配置和唯一输出上限，不缩短已配置的请求超时；调用方沿用原超时语义。
# 函数用途: 合并固定请求时限与动态生成预算。
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


# LLM: 配置优先，缺失时读后端；非法值归零，不为遥测另设隐式时间限制。
# 函数用途: 读取当前请求有效的固定超时秒数。
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


# LLM: 只看仍是配置项的上下限两个字段（安全边际自参数减量第 3 批 D 组起是代码常量，不再作为判据）。
# 函数用途: 判断当前代理配置是否带动态超时上下限，决定请求超时走动态估算还是固定值。
def has_dynamic_timeout_config(agent: object) -> bool:
    config = getattr(agent, "config", None)
    return any(hasattr(config, name) for name in ("dynamic_timeout_min", "dynamic_timeout_max"))


# LLM: 上下限与排队预算来自当前工作片配置；预填充吞吐（200 token/s）与 probe 统计参数就是 FirstTokenTimeoutOptions 的字段默认值，
#   安全边际取 dynamic_timeout.DYNAMIC_TIMEOUT_SAFETY_MARGIN（参数减量第 3 批 D 组起都不再是配置项）；不修改共享 backend 的流式 idle。
# 函数用途: 从当前代理配置读取慢模型首事件预算的上下限与排队预算，其余用代码常量，供主代理和各级子代理共用。
def first_token_timeout_options(agent: object) -> FirstTokenTimeoutOptions:
    config = getattr(agent, "config", None)
    return FirstTokenTimeoutOptions(
        safety_margin=DYNAMIC_TIMEOUT_SAFETY_MARGIN,
        min_timeout_seconds=float_config(config, "dynamic_timeout_min", 5.0),
        max_timeout_seconds=float_config(config, "dynamic_timeout_max", 120.0),
        queue_wait_seconds=float_config(config, "model_queue_wait_seconds", 0.0),
    )


# LLM: 逻辑回合身份只由重试间稳定的事实决定：请求/运行/任务身份、工具轮次、渲染 prompt 指纹与
#   native 出站 provider messages 指纹。不能把 input_tokens 等会被响应 usage 校准改变的量放进种子
#   （断流响应带 usage 时会把一次重试拆成两个逻辑回合，model_retry_count 少算；2026-10-05 retrycount）。
#   native 下 tool_context/IR 变化不反映在渲染 prompt 上（工具内容单独投影成 messages），只认 prompt
#   指纹会把内容真变化的重跑误并进旧回合；provider_messages_fingerprint 与真实出站同源，且在同一
#   请求的断流重试之间稳定（未转发指引先经 tool_context、再经 IR 投影，两次输出一致），所以重试归并、
#   内容真变化（修复注入、压缩、插话）按新回合计。改动时同步核对 start_model_call_record 的调用点、
#   tool_ir_history.provider_messages_fingerprint 与 model_call_ledger._logical_call_id 的聚合口径。
# 函数用途: 计算一次模型调用的逻辑回合身份，让同一回合的物理重试在账本里归并计数。
def logical_model_call_id(request: object) -> str:
    params = getattr(request, "params", None)
    # 延迟 import：出站投影层只在身份计算时读取，避免记账模块与 IR 模块形成导入环。
    from ..tool_ir_history import provider_messages_fingerprint

    seed = "|".join(
        [
            str(getattr(params, "request_id", "") or ""),
            str(getattr(params, "run_id", "") or ""),
            str(getattr(params, "task_id", "") or ""),
            str(getattr(request, "tool_rounds", 0)),
            hashlib.sha256(str(getattr(request, "prompt", "") or "").encode("utf-8")).hexdigest()[:16],
            provider_messages_fingerprint(params) if params is not None else "",
        ]
    )
    return f"model-call:{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:24]}"


# LLM: 只归一非负整数，不能把格式错误升级为调用链异常。
# 函数用途: 安全读取传输事实里的计数字段。
def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


# LLM: 后端选定模型优先于配置回退，模型名只作账本身份而非授权判断。
# 函数用途: 读取当前实际后端的模型名称。
def model_name(agent: object) -> str:
    backend = getattr(agent, "backend", None)
    config = getattr(agent, "config", None)
    return str(getattr(backend, "model_name", "") or getattr(config, "model_name", "") or getattr(config, "model", "") or "")


# LLM: 与实际发送一致：后端上的 max_tokens 就是发送值（工厂已按窗口夹取），原样使用；没有这个属性的后端（测试替身、echo）
#   按同一公式用当前窗口夹取配置值。未配置（0 或无法解析）时返回 0 表示没有估算，调用方据此跳过；compact 预算用
#   “窗口×0.8−本值”，所以回退时不能返回未夹取的配置原值。
# 函数用途: 返回本次模型调用的输出 token 上限，供 compact 预算与超时估算共用。
def max_output_tokens(agent: object) -> int:
    backend = getattr(agent, "backend", None)
    config = getattr(agent, "config", None)
    raw = getattr(backend, "max_tokens", None)
    try:
        if raw is not None:
            return max(0, int(raw))
        configured = max(0, int(getattr(config, "max_tokens", 0) or 0))
        window = int(getattr(backend, "context_window_tokens", 0) or 0) or context_window_or_default(config)
    except (TypeError, ValueError):
        return 0
    return output_cap_for_window(configured, window) if configured else 0


# LLM: 压缩摘要请求专用的输出预留：读 memory_compact_summary_max_output_tokens（0 = 沿用主请求输出上限），与
#   max_output_tokens 取小、绝不放大主请求上限。compact 预算（conversation/compact_request_budget）和辅助调用发出的
#   request_options.max_output_tokens 都只读这里，保证"预算按它算、请求也按它发"。2026-10-08 之前预算按主请求的
#   32000 算，27.2 万窗口的 sol/astra 和 26.2 万窗口的 MiniMax 按 90% 触发时单次缓存面请求永远装不下，每次压缩都退到
#   不复用缓存、不带工具的分段路径（10～33 分钟）。改默认值须同步 AgentConfig、MemorySettings、随包 YAML 与
#   test_compact_request_budget。
#   2026-10-08 生产事故（step17x，mc3 DeepSeek v4-flash max 档）：16384 只对"压缩调用真的降到了低档"成立；DeepSeek/MiniMax
#   的压缩调用仍按线程档位推理，推理 token 算进输出上限，摘要正好在 16384 被截断（COMPACT_SUMMARY_TRUNCATED，回合失败）。
#   所以配置的预留只在后端能用 configuration_update 降档且配置了压缩档位时生效，其它后端沿用主请求上限（部署前口径）。
# 函数用途: 返回压缩摘要调用的输出 token 上限；配置为 0、主请求上限更小、或压缩调用降不了档时沿用主请求的上限。
def compact_summary_output_reserve_tokens(agent: object) -> int:
    normal = max_output_tokens(agent)
    configured = _nonnegative_int(getattr(getattr(agent, "config", None), "memory_compact_summary_max_output_tokens", 0))
    if configured <= 0:
        return normal
    if normal <= 0:
        return configured
    return min(configured, normal) if _compact_reasoning_downshift_available(agent) else normal


# LLM: 只读两个结构化事实：后端的 supports_reasoning_update_items()（GPT-6 Responses 的 configuration_update 项）和配置的
#   memory_compact_reasoning_level（空/auto/off 表示不降档）。两者都成立，压缩调用才会在低档下写摘要，输出预留才能收紧。
# 函数用途: 判断这个 agent 的压缩调用能不能被安全地降档（决定能不能用小的输出预留）。
def _compact_reasoning_downshift_available(agent: object) -> bool:
    supports = getattr(getattr(agent, "backend", None), "supports_reasoning_update_items", None)
    level = str(getattr(getattr(agent, "config", None), "memory_compact_reasoning_level", "") or "").strip().lower()
    return callable(supports) and bool(supports()) and level not in {"", "auto", "off"}


# LLM: Non-stream total budgeting may estimate full output time; stream liveness must not use this as a hidden wall clock.
# 函数用途: 按最大输出 token 与保守速度估算非流式生成时间。
def _output_generation_timeout_seconds(agent: object) -> float:
    tokens = max_output_tokens(agent)
    if tokens <= 0:
        return 0.0
    config = getattr(agent, "config", None)
    rate = max(1.0, float_config(config, "estimated_output_tokens_per_second", 20.0))
    return tokens / rate


# LLM: 上下限只读原首事件选项，非法倒置区间按原最小值夹取。
# 函数用途: 把动态预算约束在当前配置的时间范围。
def _clamp_dynamic_request_timeout(value: float, options: FirstTokenTimeoutOptions) -> float:
    minimum = max(0.0, float(options.min_timeout_seconds))
    maximum = max(minimum, float(options.max_timeout_seconds))
    return max(minimum, min(maximum, float(value)))


# LLM: 读取无副作用；仅数值转换失败时使用调用方指定默认值。
# 函数用途: 安全取得浮点配置项。
def float_config(config: object, name: str, default: float) -> float:
    try:
        return float(getattr(config, name, default))
    except (TypeError, ValueError):
        return default



__all__ = [
    "effective_model_request_timeout_seconds", "model_request_timeout_seconds", "model_call_summary",
    "observed_chunk_filter", "record_model_call_failed", "record_model_call_finished",
    "record_model_provider_attempt", "record_model_call_timeout", "ModelCallTimeoutFacts", "start_model_call_record",
]
