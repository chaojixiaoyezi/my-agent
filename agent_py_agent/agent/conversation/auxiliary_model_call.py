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
    compact_summary_output_reserve_tokens,
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
    ModelCallTimeoutParams,
)
from ..memory_archive import estimate_tokens
from ..tooling.runtime_contracts import ToolChoice

# LLM: 辅助模型共享调用账与并发入口；精确 thread 写入 metadata，独立快照身份归原 store，不解析正文身份。
# 模块用途: 让压缩等辅助请求进入统一消耗与成本统计，原快照去重允许迟到物理事实补记。

# LLM: cabfix（2026-10-04）：辅助（压缩）调用此前没有任何可靠的调用级时限——
#   ①首事件：主模型按出站可见输入量估算动态首包预算并传给传输层，压缩没传，于是
#   大上下文的压缩会被基础读超时过早判成 first_event 超时（真机 ~1s）；
#   ②非流式：socket 读超时是"两次读之间的间隔"，不是调用总期限，慢滴流可以
#   无限期拖住一次压缩（真机可远超 request_timeout）；
#   ③流式：只要一直有有效事件就无限续期，没有总时限。
#   本模块统一给辅助调用补齐这三样，且不改变主模型/子代理路径的任何行为。
#
#   绝对期限的存在理由：压缩是在一个正在进行的用户回合里跑的辅助工作，它必须
#   有上界——否则一次卡住的压缩会把这个回合永久占住。上界取"比主模型对应总时限
#   更宽"，因为压缩通常是整段历史里最大的一次预填充，本该得到更多耐心。
#
# 字段用途: 辅助调用总时限的兜底窗口（秒），与 agent.request_timeout 取较大值。
AUXILIARY_CALL_FLOOR_SECONDS = 240.0
# LLM: 流式辅助调用的总时长上限。默认 1800 秒（30 分钟）：
#   - 比主模型对应总时限宽——主模型的 wall guard 用 max(request_timeout, 动态估算)，
#     压缩是历史里最大的一次预填充，理应有更大的预算；
#   - 但必须有上限：只要一直有有效事件就无限续期的流，会把一个用户回合永久占住，
#     这是 cabfix 要修的缺陷本身。30 分钟足够任何真实压缩完成，又不会无限。
#   - cabfix2：硬期限不能早于首包预算，否则"首包阶段"的耐心会被总时限反过来削掉；
#     实际取 max(本常量, 首包预算 + request_timeout)——这让 dynamic_timeout_max 这个
#     用户配置真的生效，而不是被一个写死的窗口静默覆盖。
#   不新增配置项：与 request_timeout / dynamic_timeout_max 同源推导，避免又多一个
#   用户需要理解、却又几乎不会调的旋钮。
AUXILIARY_STREAM_TOTAL_TIMEOUT_SECONDS = 1800.0


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

    ledger, call_id, input_tokens = _start_auxiliary_call(request, backend)
    started_at = time.monotonic()
    label = type(backend).__name__
    response: object | None = None
    try:
        response = _invoke_auxiliary_generate(request, generate, _AuxiliaryCallRef(
            ledger=ledger, call_id=call_id, timeouts=_auxiliary_timeout_plan(request, input_tokens),
        ))
        record_model_call_finished(ledger, call_id, response)
    except Exception as exc:
        _record_auxiliary_failure(ledger, _AuxiliaryFailureCall(
            call_id=call_id, exc=exc, started_at=started_at, label=label,
        ))
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


# LLM: cabfix 修法②的收口要求：超时必须落成结构化的"时间到"，而不是只留一个异常类型。
#   账本层已有 timed_out 这个终态（contracts/model_call_ledger），这里按超时阶段区分——
#   ProviderTimeoutError 用它的 stage，其余异常仍走原来的 failed，不夸大也不掩盖。
#   任何记账失败都不得顶掉原始异常：调用方要拿到真实原因。
# 类用途: 一次辅助调用失败要写账的最小事实（身份、异常、起止与后端标签）。
@dataclass(frozen=True)
class _AuxiliaryFailureCall:
    call_id: str
    exc: BaseException
    started_at: float
    label: str


# 函数用途: 把辅助调用的失败写进模型账，超时记 timed_out、其它记 failed。
def _record_auxiliary_failure(ledger: object, call: _AuxiliaryFailureCall) -> None:
    call_id, exc = call.call_id, call.exc
    started_at, label = call.started_at, call.label
    try:
        from ..backends.errors import ProviderTimeoutError

        if isinstance(exc, ProviderTimeoutError):
            ledger.timeout(
                ModelCallTimeoutParams(
                    call_id=call_id,
                    timeout_stage=str(getattr(exc, "stage", "") or "provider_wall"),
                    timeout_seconds=float(getattr(exc, "timeout_seconds", 0.0) or 0.0),
                    elapsed_seconds=max(0.0, time.monotonic() - started_at),
                )
            )
        else:
            record_model_call_failed(ledger, call_id, exc)
    except Exception:  # noqa: BLE001 - 记账失败不能顶掉原始异常
        pass
    try:
        record_llm_call(label, time.monotonic() - started_at, None, ok=False)
    except Exception:
        pass


# LLM: 一次辅助调用只估算一次输入：_start_auxiliary_call 调这里记账，并把同一个数交给时限计划，
#   账本和传输层的耐心必须出自同一份估算。大压缩请求的材料有几十万 token，重复估算是纯 CPU 浪费
#   （Gateway 进程本就 CPU 吃紧）；不要在别处再调本函数复算。估算失败照记账原口径抛出。
# 函数用途: 按实际发出的材料（普通请求不含 schema，结构化请求加 schema）估算本次辅助调用的输入 token。
def _auxiliary_input_tokens(request: AuxiliaryModelCallRequest) -> int:
    material = {
        "prompt": str(request.prompt or ""), "messages": list(request.messages or []),
        "tools": list(request.tools or []), "system_instruction": str(request.system_instruction or ""),
    }
    if request.response_schema is not None:
        material["response_schema"] = request.response_schema
    return int(estimate_tokens(material))


# LLM: I/O 前冻结真实身份和输入估算；仅结构化请求增加 schema，普通请求的估算对象保持原样，正文不进 metadata。
#   返回的 input_tokens 就是记账用的那一份，调用方拿它算时限计划，不得再复算。
# 函数用途: 为辅助请求登记调用及归属，把实际声明的 schema 成本纳入同一本账；返回账本、调用号和输入估算。
def _start_auxiliary_call(
    request: AuxiliaryModelCallRequest,
    backend: object,
) -> tuple[object, str, int]:
    ledger = model_call_ledger(request.agent)
    identity = uuid.uuid4().hex
    logical_call_id = f"auxiliary:{_purpose(request.purpose)}:{identity[:24]}"
    call_id = f"{logical_call_id}:attempt-1:{identity[24:32]}"
    input_tokens = _auxiliary_input_tokens(request)
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
    return ledger, call_id, input_tokens


# LLM: cabfix 修法①：首事件预算复用主模型的同一个估算函数（estimate_first_token_timeout +
#   first_token_timeout_options），不另写一套——两处口径必须一致，否则压缩与主模型会对
#   同样大的输入给出不同耐心。输入量用上面已算好的 input_tokens（与账本同一份估算），
#   估算失败或无动态超时配置时返回 None，让传输层保持原有基础读超时（不引入新行为）。
# 函数用途: 按辅助调用自己的输入估算，给出应当传给传输层的首事件预算秒数。
def _auxiliary_first_event_budget(request: AuxiliaryModelCallRequest, input_tokens: int) -> float | None:
    try:
        from ..agent_core.model.call_monitor import (
            FirstTokenTimeoutParams,
            estimate_first_token_timeout,
        )
        from ..agent_core.model.call_runtime import (
            first_token_timeout_options,
            has_dynamic_timeout_config,
        )

        if not has_dynamic_timeout_config(request.agent):
            return None
        estimate = estimate_first_token_timeout(
            FirstTokenTimeoutParams(
                input_tokens=max(0, int(input_tokens)),
                ledger=model_call_ledger(request.agent),
                options=first_token_timeout_options(request.agent),
            )
        )
        return max(0.0, float(estimate.timeout_seconds)) or None
    except Exception:  # noqa: BLE001 - 估算是增强；失败退回基础读超时，不改变调用成败
        return None


# LLM: cabfix 修法②③：辅助调用的绝对期限。非流式取 request_timeout 与兜底窗口的较大值——
#   请求基础超时是运维已经调好的刻度，不该被辅助调用缩得更短；兜底窗口保证即使
#   配置成很小的值，辅助调用也不会完全没有上界。
#   流式不叠兜底窗口，而是取 max(流式总时限, 首包预算 + request_timeout)（cabfix2）：
#   ① 叠兜底会让 240 顶掉总时限，"宽但有限"失真；
#   ② 硬期限若短于首包预算，首包阶段的耐心会被总时限静默削掉（dynamic_timeout_max
#      配到 10800 时尤其明显）——加一个 request_timeout 的余量，让首包预算真正可用。
#   first_event 是 _auxiliary_timeout_plan 已算好的首包预算（None 表示没有），这里不复算。
# 函数用途: 算出一次辅助调用的绝对秒数上界；非流式与流式各用一档。
def _auxiliary_absolute_timeout(
    request: AuxiliaryModelCallRequest,
    *,
    streaming: bool,
    first_event: float | None,
) -> float:
    base = _auxiliary_base_request_timeout(request)
    if streaming:
        return max(base, AUXILIARY_STREAM_TOTAL_TIMEOUT_SECONDS, (first_event or 0.0) + base)
    return max(base, AUXILIARY_CALL_FLOOR_SECONDS)


# LLM: 时限计划只在发起前算一次：首包预算按记账那份输入估算推出，绝对期限再复用这个首包预算；
#   流式与否看后端 stream_enabled（和传输层实际走的分支一致）。
# 函数用途: 由本次辅助调用的输入估算，一次算出首事件预算和绝对期限。
def _auxiliary_timeout_plan(request: AuxiliaryModelCallRequest, input_tokens: int) -> AuxiliaryTimeoutPlan:
    backend = getattr(getattr(request, "agent", None), "backend", None)
    streaming = bool(backend is not None and getattr(backend, "stream_enabled", False))
    first_event = _auxiliary_first_event_budget(request, input_tokens)
    return AuxiliaryTimeoutPlan(
        first_event_budget_seconds=first_event,
        total_deadline_seconds=_auxiliary_absolute_timeout(request, streaming=streaming, first_event=first_event),
    )


# LLM: 读不到配置时退回 0，让两个分支各自的兜底常量决定下限（不引入新行为）。
# 函数用途: 取本次辅助调用对应 agent 的请求基础超时秒数，读不到返回 0。
def _auxiliary_base_request_timeout(request: AuxiliaryModelCallRequest) -> float:
    try:
        from ..agent_core.model.call_runtime import model_request_timeout_seconds

        return float(model_request_timeout_seconds(request.agent))
    except Exception:  # noqa: BLE001 - 读不到配置就用兜底常量
        return 0.0


# LLM: 两种生成共享原 admission、provider scope 和观察者；schema 不启动工具循环，取消与后端异常原样退出。
#   cabfix：这里是所有辅助调用的唯一咽喉，首事件预算和绝对期限都从这一处注入，不散落到各调用方。
# 函数用途: 在原并发槽调用已选方法，把实际流事件和 HTTP 尝试记入原模型账。
def _invoke_auxiliary_generate(
    request: AuxiliaryModelCallRequest,
    generate: object,
    call: _AuxiliaryCallRef,
) -> object:
    from ..llm_scale.hot_path import global_llm_admission_slot
    from ..observability.concurrency_metrics import llm_inflight

    ledger, call_id = call.ledger, call.call_id
    on_chunk = _auxiliary_chunk_observer(ledger, call_id)

    # LLM: 只向当前 call_id 追加传输事实，不改请求或用量；消费者不得用此计数补猜 token。
    # 函数用途: 把后端每次 HTTP 观察记入当前辅助调用。
    def _observe_provider_attempt(event: dict[str, object]) -> None:
        record_model_provider_attempt(ledger, call_id, event)

    # 压缩类调用也算请求前缀诊断（backends/cache_diagnostics）：和上一次主请求比 system/工具/历史链，账本里留变化码，
    # 用来回答"压缩请求有没有复用主请求前缀"；不记正文，不改请求。
    with provider_runtime_scope(request.agent, request), provider_attempt_observer(
        _observe_provider_attempt, cache_diagnostics=_is_compact_purpose(request.purpose),
    ):
        with global_llm_admission_slot():
            llm_inflight(1)
            try:
                return _call_backend(generate, request, on_chunk, timeouts=call.timeouts)
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


# LLM: cabfix 的两个超时事实合成一个小数据类；两个字段都可能为 None，表示沿用原行为（不设该预算）。
# 类用途: 携带一次辅助调用的首事件预算与绝对期限。
@dataclass(frozen=True)
class AuxiliaryTimeoutPlan:
    first_event_budget_seconds: float | None = None
    total_deadline_seconds: float | None = None


# LLM: 一次辅助调用发起时的全部句柄：账本与调用号用于流事件和 HTTP 尝试记账，timeouts 是按记账那份
#   输入估算算好的时限计划；分派函数只读这三项，不再自己估算。
# 类用途: 打包一次辅助调用在账本里的身份和它的时限计划，传给分派函数。
@dataclass(frozen=True)
class _AuxiliaryCallRef:
    ledger: object
    call_id: str
    timeouts: AuxiliaryTimeoutPlan


# LLM: 一次辅助调用的请求选项唯一组装点（cachecompact + cabfix 合并）：
#   - 压缩类同线程请求沿主请求读取同一档思考和档位（普通辅助任务不因共享 thread_id 改变采样策略）；
#     none 是摘要拒绝工具的选项，不代表主回合 forced；
#   - cabfix 的首包预算与绝对期限走同一个 ProviderRequestOptions 通道。
#   后端不接收 request_options 时：带了 system、思考或档位这类声明的必须失败，不能静默丢掉；
#   只有超时增强时静默缺席（测试替身或旧实现，没有真实传输需要保护）。
# 函数用途: 为辅助调用组装思考档位、系统指令和期限选项；没有可传项时返回 None。
def _auxiliary_provider_options(
    generate: object,
    request: AuxiliaryModelCallRequest,
    timeouts: AuxiliaryTimeoutPlan | None,
) -> ProviderRequestOptions | None:
    backend = getattr(request.agent, "backend", None)
    system_instruction = str(request.system_instruction or "")
    timeouts = timeouts or AuxiliaryTimeoutPlan()
    thinking_disabled, reasoning_effort = _compact_reasoning_options(request, backend)
    reasoning_update_effort = _compact_reasoning_update_effort(request, backend)
    output_cap = _compact_output_cap(request)
    declared = bool(system_instruction or thinking_disabled or reasoning_effort or reasoning_update_effort)
    has_timeouts = timeouts.first_event_budget_seconds is not None or timeouts.total_deadline_seconds is not None
    if not (declared or has_timeouts or output_cap is not None):
        return None
    if not _accepts_keyword(generate, "request_options"):
        if declared:
            raise TypeError("auxiliary model backend does not accept request_options")
        return None
    return ProviderRequestOptions(
        system_instruction=system_instruction,
        thinking_disabled=thinking_disabled,
        reasoning_effort=reasoning_effort,
        reasoning_update_effort=reasoning_update_effort,
        max_output_tokens=output_cap,
        first_event_timeout_seconds=timeouts.first_event_budget_seconds,
        total_deadline_seconds=timeouts.total_deadline_seconds,
    )


# LLM: 只给压缩类目的加本次输出封顶：读 call_runtime.compact_summary_output_reserve_tokens（与 compact 预算同源），
#   只在它小于主请求上限时才带（否则 None 沿后端配置）；后端不收 request_options 时静默不带（与期限同一口径），
#   供应商若因此判超窗仍走原分段链。
# 函数用途: 返回压缩辅助调用要发的 max_output_tokens；不适用时返回 None。
def _compact_output_cap(request: AuxiliaryModelCallRequest) -> int | None:
    if not _is_compact_purpose(request.purpose):
        return None
    reserve = compact_summary_output_reserve_tokens(request.agent)
    normal = max_output_tokens(request.agent)
    return reserve if reserve > 0 and (normal <= 0 or reserve < normal) else None


# LLM: 压缩降档只走"追加 configuration_update 项"这一条缓存安全的路（OpenAI 缓存文档：改请求级 reasoning.effort 会让
#   前缀失配，GPT-6 系列应改用 configuration_update 项）：请求级档位仍沿线程（见 _compact_reasoning_options），由后端
#   在 input 末尾插入档位更新项。条件：压缩目的、配置了 memory_compact_reasoning_level、后端声明
#   supports_reasoning_update_items()；其它后端一律返回空串，继续沿线程档位，不冒险改请求级参数。
# 函数用途: 返回压缩请求要通过 configuration_update 项申请的档位；不适用时返回空串。
def _compact_reasoning_update_effort(request: AuxiliaryModelCallRequest, backend: object) -> str:
    if not _is_compact_purpose(request.purpose):
        return ""
    supports = getattr(backend, "supports_reasoning_update_items", None)
    if not callable(supports) or not supports():
        return ""
    from ..backends.reasoning_control import normalize_reasoning_level

    configured = getattr(getattr(request.agent, "config", None), "memory_compact_reasoning_level", "") or ""
    level = normalize_reasoning_level(configured)
    return "" if level in {"", "auto", "off"} else level


# LLM: 只有后端声明支持 provider 请求选项、且是明确标记的压缩目的、带线程身份时，才按线程读取思考档位。
# 函数用途: 返回压缩请求应沿用的 (thinking_disabled, reasoning_effort)；不适用时返回 (False, "")。
def _compact_reasoning_options(request: AuxiliaryModelCallRequest, backend: object) -> tuple[bool, str]:
    if not bool(getattr(backend, "supports_provider_request_options", False)):
        return False, ""
    if not (_is_compact_purpose(request.purpose) and str(request.thread_id or "").strip()):
        return False, ""
    from ..settings.reasoning_effort import request_reasoning_options

    params = SimpleNamespace(task_attributes={"conversation_thread_id": str(request.thread_id).strip()})
    return request_reasoning_options(request.agent, params, backend, forced=False)


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
#   cabfix 的期限计划随 timeouts 传入，并入同一份 request_options。
# 函数用途: 将通用辅助输入交给原后端方法，保留同线程选项与期限，并维持普通调用的原参数形态。
def _call_backend(
    generate: object,
    request: AuxiliaryModelCallRequest,
    on_chunk: object,
    *,
    timeouts: AuxiliaryTimeoutPlan | None = None,
) -> object:
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
    options = _auxiliary_provider_options(generate, request, timeouts)
    if options is not None:
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
