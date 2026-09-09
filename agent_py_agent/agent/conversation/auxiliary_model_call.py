"""统一记录 Compact 等非工具循环模型调用。"""

from __future__ import annotations

import inspect
import time
import uuid
from dataclasses import dataclass
from typing import Any

from ..agent_core.model.call_runtime import (
    max_output_tokens,
    model_call_ledger,
    model_name,
    record_model_call_failed,
    record_model_call_finished,
    record_model_provider_attempt,
)
from ..backends import ProviderRequestOptions
from ..backends.gateway_helpers import provider_attempt_observer
from ..backends.provider_headers import provider_runtime_scope
from ..contracts.model_call_ledger import (
    ModelCallActivityParams,
    ModelCallFirstTokenParams,
    ModelCallStartedParams,
)
from ..memory_archive import estimate_tokens
from ..tooling.runtime_contracts import ToolChoice

# LLM: This module is the one accounting/admission boundary for real model calls made outside the
# main tool loop. It must never own Compact state or infer request identity from prompt prose.
# 模块用途: 让 Compact 等辅助模型请求也进入统一调用账、供应商重试账、并发闸和成本统计。


# LLM: The host supplies exact request/run/task identity and a typed purpose; optional tools and
# system guidance preserve a parent request's provider cache surface but this one-shot wrapper never
# executes returned tool calls. Prompt/messages stay request-local and never enter ledger metadata.
# 类用途: 描述一次没有工具执行权的辅助模型调用；Compact 可复用主请求的缓存前缀，并绑定到真实任务用量作用域。
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


# LLM: Every physical auxiliary request is appended to the same ModelCallLedger as normal turns.
# Provider transport owns retry and timeout behavior; this wrapper never starts an orphan thread.
# 函数用途: 调用一次辅助模型并完整记录 token、缓存命中、HTTP 重试、耗时和费用。
def generate_auxiliary_model_response(request: AuxiliaryModelCallRequest) -> object:
    agent = request.agent
    backend = getattr(agent, "backend", None)
    generate = getattr(backend, "generate", None)
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
            from ..agent_core.model.llm_metrics import record_llm_call

            record_llm_call(label, time.monotonic() - started_at, None, ok=False)
        except Exception:
            pass
        raise
    try:
        record_llm_call(label, time.monotonic() - started_at, response, ok=True)
    except Exception:
        pass
    _record_auxiliary_cost(request, response)
    return response


# LLM: Ledger identity and estimated request size are fixed before any provider I/O. Cache-safe
# fields count toward input cost but their bodies never enter metadata.
# 函数用途: 为辅助请求建立唯一调用编号，并按真实 prompt/messages/tools/system 请求面登记预计输入。
def _start_auxiliary_call(
    request: AuxiliaryModelCallRequest,
    backend: object,
) -> tuple[object, str]:
    ledger = model_call_ledger(request.agent)
    identity = uuid.uuid4().hex
    logical_call_id = f"auxiliary:{_purpose(request.purpose)}:{identity[:24]}"
    call_id = f"{logical_call_id}:attempt-1:{identity[24:32]}"
    input_tokens = estimate_tokens(
        {
            "prompt": str(request.prompt or ""),
            "messages": list(request.messages or []),
            "tools": list(request.tools or []),
            "system_instruction": str(request.system_instruction or ""),
        }
    )
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
                "purpose": _purpose(request.purpose),
                "auxiliary": True,
            },
        )
    )
    return ledger, call_id


# LLM: The one-shot provider call shares global admission and typed transport observations with the
# main loop. Callbacks account only token counts/attempt facts and cannot expose summary bodies.
# 函数用途: 绑定原 owner/thread 会话头，在全局并发槽执行辅助请求，并把流式活动和 HTTP 重试写入同一本模型账。
def _invoke_auxiliary_generate(
    request: AuxiliaryModelCallRequest,
    generate: object,
    ledger: object,
    call_id: str,
) -> object:
    from ..llm_scale.hot_path import global_llm_admission_slot
    from ..observability.concurrency_metrics import llm_inflight

    on_chunk = _auxiliary_chunk_observer(ledger, call_id)

    def _observe_provider_attempt(event: dict[str, object]) -> None:
        record_model_provider_attempt(ledger, call_id, event)

    prompt = request.prompt if isinstance(request.prompt, str) else str(request.prompt or "")
    with provider_runtime_scope(request.agent, request), provider_attempt_observer(_observe_provider_attempt):
        with global_llm_admission_slot():
            llm_inflight(1)
            try:
                return _call_backend(
                    generate,
                    prompt,
                    request.messages,
                    on_chunk,
                    tools=request.tools,
                    tool_choice=request.tool_choice,
                    system_instruction=request.system_instruction,
                )
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


# LLM: Signature inspection avoids retrying a TypeError after provider I/O may already have begun.
# Cache-critical messages/tools/system are all-or-error when supplied; the wrapper deliberately has
# no tool execution loop, so an auxiliary response can never gain the parent turn's tool authority.
# 函数用途: 只传后端明确支持的参数；Compact 需要复用缓存面时强制保留原生历史、工具定义与 system 指令。
def _call_backend(
    generate: object,
    prompt: str,
    messages: list[dict[str, Any]] | None,
    on_chunk: object,
    *,
    tools: list[dict[str, Any]] | None,
    tool_choice: ToolChoice | None,
    system_instruction: str,
) -> object:
    kwargs: dict[str, object] = {}
    if _accepts_keyword(generate, "on_chunk"):
        kwargs["on_chunk"] = on_chunk
    if messages is not None:
        if not _accepts_keyword(generate, "messages"):
            raise TypeError("auxiliary model backend does not accept native messages")
        kwargs["messages"] = list(messages)
    if tools is not None:
        if not _accepts_keyword(generate, "tools"):
            raise TypeError("auxiliary model backend does not accept native tools")
        if not _accepts_keyword(generate, "tool_choice"):
            raise TypeError("auxiliary model backend does not accept tool_choice")
        kwargs["tools"] = list(tools)
        kwargs["tool_choice"] = tool_choice or ToolChoice.auto(
            "auxiliary_cache_surface"
        )
    if system_instruction:
        if not _accepts_keyword(generate, "request_options"):
            raise TypeError("auxiliary model backend does not accept request_options")
        kwargs["request_options"] = ProviderRequestOptions(
            system_instruction=str(system_instruction)
        )
    return generate(prompt, **kwargs)


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


# LLM: Purpose is a bounded diagnostic label, not a routing decision or prompt-derived status.
# 函数用途: 规范辅助调用用途标签，避免空值或超长内容污染模型账。
def _purpose(value: object) -> str:
    return str(value or "auxiliary").strip()[:80] or "auxiliary"


__all__ = ["AuxiliaryModelCallRequest", "generate_auxiliary_model_response"]
