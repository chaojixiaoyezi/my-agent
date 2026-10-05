# LLM: 普通与结构化辅助生成共享原模型账；schema 分派不执行工具，观察回调只读原记录、不复制正文或另建账。
# 模块用途: 记录 Compact 和结构化辅助调用，保留原载荷及统计，并返回无正文的调用观察。
"""统一记录 Compact 等非工具循环模型调用。"""

from __future__ import annotations

import inspect
import logging
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from ..agent_core.model.call_runtime import (
    max_output_tokens,
    model_call_ledger,
    model_name,
    record_model_call_failed,
    record_model_call_finished,
    record_model_provider_attempt,
)
from ..agent_core.model.llm_metrics import record_llm_call
from ..backends import ProviderRequestOptions
from ..backends.gateway_helpers import provider_attempt_observer
from ..backends.provider_headers import provider_runtime_scope
from ..backends.response_completion import has_reasoning_content
from ..contracts.model_call_ledger import (
    ModelCallActivityParams,
    ModelCallFirstTokenParams,
    ModelCallStartedParams,
)
from ..memory_archive import estimate_tokens
from ..tooling.runtime_contracts import ToolChoice

# LLM: 辅助模型共享调用账与并发入口；精确 thread 写入 metadata，独立快照身份归原 store，不解析正文身份。
# 模块用途: 让压缩等辅助请求进入统一消耗与成本统计，原快照去重允许迟到物理事实补记。


# LLM: 这里只投影精确 call_id 的原账；LRU 已移除该记录时次数为 None，不能编造零 HTTP。
# 类用途: 向调用者提供不含提示词、响应或凭据的辅助调用定位和传输次数。
@dataclass(frozen=True)
class AuxiliaryModelCallObservation:
    call_id: str
    provider_http_attempt_count: int | None


# LLM: 宿主提供真实身份；schema 交原 generate_structured，普通分支保留缓存面，二者都没有工具执行权。
# 类用途: 描述一次辅助调用及可选只读观察者；正文留在请求内，原账按真实任务统计。
@dataclass(frozen=True)
class AuxiliaryModelCallRequest:
    agent: object
    prompt: str
    messages: list[dict[str, Any]] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: ToolChoice | None = None
    system_instruction: str = ""
    request_id: str = ""
    run_id: str = ""
    task_id: str = ""
    thread_id: str = ""
    purpose: str = "auxiliary"
    response_schema: dict[str, Any] | None = None
    on_observation: Callable[[AuxiliaryModelCallObservation], None] | None = field(default=None, repr=False, compare=False)


# LLM: 只进行一次逻辑分派，后端仍拥有内部 schema/传输重试；不把一次分派宣称成一次 HTTP，旧 generate 不改。
# 函数用途: 调用普通或结构化辅助模型并沿原账统计；失败与取消仍传播，观察者不能改变结果。
def generate_auxiliary_model_response(request: AuxiliaryModelCallRequest) -> object:
    agent = request.agent
    backend = getattr(agent, "backend", None)
    method = "generate_structured" if request.response_schema is not None else "generate"
    generate = getattr(backend, method, None)
    if not callable(generate):
        raise RuntimeError("auxiliary model backend is unavailable")

    ledger, call_id = _start_auxiliary_call(request, backend)
    started_at = time.monotonic()
    label = type(backend).__name__
    response: object | None = None
    try:
        response = _invoke_auxiliary_generate(
            request,
            generate,
            ledger,
            call_id,
        )
        record_model_call_finished(ledger, call_id, response)
    except Exception as exc:
        record_model_call_failed(ledger, call_id, exc)
        try:
            record_llm_call(label, time.monotonic() - started_at, None, ok=False)
        except Exception:
            pass
        raise
    finally:
        if request.on_observation is not None:
            _report_auxiliary_observation(request, ledger, call_id)
    try:
        record_llm_call(label, time.monotonic() - started_at, response, ok=True)
    except Exception:
        pass
    _record_auxiliary_cost(request, response)
    return response


# LLM: I/O 前冻结真实身份和输入估算；仅结构化请求增加 schema，普通请求的估算对象保持原样，正文不进 metadata。
# 函数用途: 为辅助请求登记调用及归属，把实际声明的 schema 成本纳入同一本账。
def _start_auxiliary_call(
    request: AuxiliaryModelCallRequest,
    backend: object,
) -> tuple[object, str]:
    ledger = model_call_ledger(request.agent)
    identity = uuid.uuid4().hex
    logical_call_id = f"auxiliary:{_purpose(request.purpose)}:{identity[:24]}"
    call_id = f"{logical_call_id}:attempt-1:{identity[24:32]}"
    material = {
        "prompt": str(request.prompt or ""), "messages": list(request.messages or []),
        "tools": list(request.tools or []), "system_instruction": str(request.system_instruction or ""),
    }
    if request.response_schema is not None:
        material["response_schema"] = request.response_schema
    input_tokens = estimate_tokens(material)
    ledger.started(
        ModelCallStartedParams(
            call_id=call_id,
            backend=str(getattr(backend, "name", "") or ""),
            model=model_name(request.agent),
            input_tokens=input_tokens,
            output_tokens_estimate=max_output_tokens(request.agent),
            request_id=str(request.request_id or ""),
            run_id=str(request.run_id or ""),
            metadata={
                "logical_call_id": logical_call_id,
                "physical_attempt": 1,
                "task_id": str(request.task_id or ""),
                "thread_id": str(request.thread_id or ""),
                "purpose": _purpose(request.purpose),
                "auxiliary": True,
            },
        )
    )
    return ledger, call_id


# LLM: 两种生成共享原 admission、provider scope 和观察者；schema 不启动工具循环，取消与后端异常原样退出。
# 函数用途: 在原并发槽调用已选方法，把实际流事件和 HTTP 尝试记入原模型账。
def _invoke_auxiliary_generate(
    request: AuxiliaryModelCallRequest,
    generate: object,
    ledger: object,
    call_id: str,
) -> object:
    from ..llm_scale.hot_path import global_llm_admission_slot
    from ..observability.concurrency_metrics import llm_inflight

    on_chunk = _auxiliary_chunk_observer(ledger, call_id)

    # LLM: 只向当前 call_id 追加传输事实，不改请求或用量；消费者不得用此计数补猜 token。
    # 函数用途: 把后端每次 HTTP 观察记入当前辅助调用。
    def _observe_provider_attempt(event: dict[str, object]) -> None:
        record_model_provider_attempt(ledger, call_id, event)

    with provider_runtime_scope(request.agent, request), provider_attempt_observer(_observe_provider_attempt):
        with global_llm_admission_slot():
            llm_inflight(1)
            try:
                return _call_backend(generate, request, on_chunk)
            finally:
                llm_inflight(-1)


# LLM: Delta accounting stores only token counts. It cannot influence provider liveness; a backend
# without an on_chunk keyword simply returns one terminal result.
# 函数用途: 创建辅助调用的流式计数回调，首段和后续活动分别写入统一模型账。
def _auxiliary_chunk_observer(ledger: object, call_id: str) -> object:
    saw_first_event = False

    def _on_chunk(chunk: str) -> None:
        nonlocal saw_first_event
        tokens = estimate_tokens(str(chunk or ""))
        if tokens <= 0:
            return
        if not saw_first_event:
            saw_first_event = True
            ledger.first_token(
                ModelCallFirstTokenParams(
                    call_id=call_id,
                    output_tokens_seen=tokens,
                )
            )
            return
        ledger.activity(
            ModelCallActivityParams(
                call_id=call_id,
                output_tokens_seen=tokens,
            )
        )

    return _on_chunk


# LLM: 仅压缩类同线程辅助请求沿主请求读取同一档位；普通辅助任务不得因共享 thread_id 改变采样策略。
#   none 是摘要拒绝工具的选项，不代表主回合 forced；缓存面的 system 仍逐请求原样传递。
# 函数用途: 为明确标记的压缩请求构造与主生成相同的思考开关和档位。
def _auxiliary_provider_options(request: AuxiliaryModelCallRequest) -> ProviderRequestOptions | None:
    backend = getattr(request.agent, "backend", None)
    system_instruction = str(request.system_instruction or "")
    if not bool(getattr(backend, "supports_provider_request_options", False)):
        return ProviderRequestOptions(system_instruction=system_instruction) if system_instruction else None

    thinking_disabled = False
    reasoning_effort = ""
    if _is_compact_purpose(request.purpose) and str(request.thread_id or "").strip():
        from ..settings.reasoning_effort import request_reasoning_options

        params = SimpleNamespace(
            task_attributes={"conversation_thread_id": str(request.thread_id).strip()}
        )
        thinking_disabled, reasoning_effort = request_reasoning_options(
            request.agent, params, backend, forced=False,
        )
    if not (system_instruction or thinking_disabled or reasoning_effort):
        return None
    return ProviderRequestOptions(
        system_instruction=system_instruction,
        thinking_disabled=thinking_disabled,
        reasoning_effort=reasoning_effort,
    )


# LLM: Purpose 是宿主给定的结构化路由标签；仅压缩目的共用缓存分区，其它辅助请求保持旧默认。
# 函数用途: 判断辅助目的是否属于对话压缩，不从提示正文推断行为。
def _is_compact_purpose(purpose: str) -> bool:
    return str(purpose or "") in {
        "conversation_compact_summary",
        "conversation_compact_media_digest",
        "compact_live_tool_summary",
        "compact_carried_summary",
    }


# LLM: 发送前检查签名，不用 TypeError 重试；显式 schema/历史/工具/system/options 不支持就失败，不能静默丢掉声明。
# 函数用途: 将通用辅助输入交给原后端方法，保留同线程选项并维持普通调用的原参数形态。
def _call_backend(generate: object, request: AuxiliaryModelCallRequest, on_chunk: object) -> object:
    kwargs: dict[str, object] = {}
    if request.response_schema is not None:
        if not _accepts_keyword(generate, "response_schema"):
            raise TypeError("auxiliary model backend does not accept response_schema")
        kwargs["response_schema"] = request.response_schema
    if _accepts_keyword(generate, "on_chunk"):
        kwargs["on_chunk"] = on_chunk
    if request.messages is not None:
        if not _accepts_keyword(generate, "messages"):
            raise TypeError("auxiliary model backend does not accept native messages")
        kwargs["messages"] = list(request.messages)
    if request.tools is not None:
        if not _accepts_keyword(generate, "tools"):
            raise TypeError("auxiliary model backend does not accept native tools")
        if not _accepts_keyword(generate, "tool_choice"):
            raise TypeError("auxiliary model backend does not accept tool_choice")
        kwargs["tools"] = list(request.tools)
        kwargs["tool_choice"] = request.tool_choice or ToolChoice.auto(
            "auxiliary_cache_surface"
        )
    options = _auxiliary_provider_options(request)
    if options is not None:
        if not _accepts_keyword(generate, "request_options"):
            raise TypeError("auxiliary model backend does not accept request_options")
        kwargs["request_options"] = options
    prompt = request.prompt if isinstance(request.prompt, str) else str(request.prompt or "")
    return generate(prompt, **kwargs)


# LLM: 只读精确调用记录，观察者异常只留类型无关警告、不影响原模型结果；缺记录保持未知而非零请求。
# 函数用途: 把原 HTTP 次数与调用编号交给宿主，用于诚实报告内部重试的统计边界。
def _report_auxiliary_observation(request: AuxiliaryModelCallRequest, ledger: object, call_id: str) -> None:
    try:
        record = next((item for item in ledger.records() if item.call_id == call_id), None)
        request.on_observation(AuxiliaryModelCallObservation(
            call_id, record.provider_attempt_count if record is not None else None,
        ))
    except Exception:
        logging.getLogger(__name__).warning("辅助模型调用观察暂不可用；原结果保持不变")


# LLM: Callable signatures are inspected before network submission. Unknown built-in signatures
# use the project backend contract, whose generate method accepts all declared keywords.
# 函数用途: 判断模型后端是否接收某个关键字，防止用异常重试造成重复请求。
def _accepts_keyword(callable_obj: object, keyword: str) -> bool:
    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return True
    return keyword in signature.parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


# LLM: Cost metrics consume only normalized model identity and provider usage. Failures here are
# observability failures and must not change the Compact result.
# 函数用途: 将辅助调用费用同时计入全局和 owner/run 成本指标。
def _record_auxiliary_cost(
    request: AuxiliaryModelCallRequest,
    response: object,
) -> None:
    try:
        from ..agent_core.model.llm_metrics import record_llm_cost, record_run_cost

        model = model_name(request.agent)
        record_llm_cost(model, response)
        config = getattr(request.agent, "config", None)
        record_run_cost(
            str(getattr(config, "my_agent_owner_id", "") or ""),
            str(request.run_id or ""),
            model,
            response,
        )
    except Exception:
        pass


# LLM: 独立 request_id 的辅助调用提交原累计快照；store 按范围/摘要生成幂等身份，不用物理调用数猜快照版本。
# 函数用途: 保存独立压缩或显式探测的模型消耗；只更新用量时保留当前生成状态，保存失败不改变调用结果。
def settle_standalone_model_usage(request: object, *, source: str = "conversation_compact", usage_only: bool = False) -> None:
    from ..agent_core.model.call_runtime import model_call_summary
    from .model_metrics import publish_model_metrics

    try:
        store, agent = request.store, request.agent
        append = getattr(getattr(store, "model_usage", None), "append_snapshot_once", None)
        if not callable(append):
            return
        summary = model_call_summary(agent, request_id=request.request_id, run_id=request.run_id)
        count = int(summary.get("physical_model_attempt_count") or 0)
        if not count:
            return
        thread_id = request.thread.thread_id
        append({
            "thread_id": thread_id, "request_id": request.request_id,
            "run_id": request.run_id, "task_id": request.task_id,
            "source": source, "model_calls": summary, "now": time.time(),
        })
        params = SimpleNamespace(request_id=request.request_id, run_id=request.run_id,
            live_archive_state={}, task_attributes={"conversation_thread_id": thread_id})
        # 已结束的独立压缩没有工具执行权；没有原始响应就不伪造最近缓存与速度。
        publish_model_metrics(agent, params, pending=False, tool_count=0, usage_only=usage_only)
    except Exception:
        logging.getLogger(__name__).warning("独立模型调用用量保存失败；调用结果不受影响", exc_info=False)


# LLM: Purpose is a bounded diagnostic label, not a routing decision or prompt-derived status.
# 函数用途: 规范辅助调用用途标签，避免空值或超长内容污染模型账。
def _purpose(value: object) -> str:
    return str(value or "auxiliary").strip()[:80] or "auxiliary"


# LLM: 只读 ModelResponse 的形状和 typed 终态；未知标签有界保留，正文、思考、参数、签名和凭据不得进入返回值。
# 函数用途: 为摘要失败日志生成固定字段诊断；不推断 token 上限、不发请求、不改变原响应或恢复分类。
def auxiliary_response_shape(response: object) -> dict[str, str | int | bool]:
    text_chars = len(str(getattr(response, "text", "") or "").strip())
    tool_use_count = len(getattr(response, "tool_use_blocks", None) or [])
    blocks = getattr(response, "assistant_content_blocks", None) or []
    return {
        "text_chars": text_chars,
        "tool_use_count": tool_use_count,
        "thinking_only": not text_chars and not tool_use_count and has_reasoning_content(blocks),
        **{name: _diagnostic_label(getattr(response, name, "")) for name in (
            "stop_reason", "runtime_status", "runtime_reason", "runtime_source", "turn_end_reason",
        )},
        "truncated": bool(getattr(response, "truncated", False)),
    }


# LLM: 终态字段允许供应商扩展，但不得把多行内容或异常长值带入日志；不把未知值映射成已知状态。
# 函数用途: 保留最多 80 字符的机器标签，非法形状只显示固定标记。
def _diagnostic_label(value: object) -> str:
    label = str(value or "")
    return label if not label or re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", label) else "invalid_label"


__all__ = ["AuxiliaryModelCallObservation", "AuxiliaryModelCallRequest", "auxiliary_response_shape", "generate_auxiliary_model_response", "settle_standalone_model_usage"]
