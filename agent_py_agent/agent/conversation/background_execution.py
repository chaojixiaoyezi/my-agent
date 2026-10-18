# LLM: 原BG binder在同T内发布claim运输定位，Count0仍原publisher；后台单片不拥有wake/Goal/租约，Compact/历史/CAS/取消沿原合同。
# 模块用途: 执行后台模型与Compact；仅组合已认领原RunParams短门，actual未结不释放输入或伪报失败。
from __future__ import annotations

import logging
from contextlib import nullcontext
from dataclasses import dataclass, replace
from functools import partial
from typing import Protocol

from ..agent_core.runtime.loop_models import RunParams
from ..backends.bounded_call import BoundedCallExitUnconfirmedError
from ..common.request_execution_ports import (
    RequestExecutionContextUnavailable,
    RequestExecutionOutcomeUncertainError,
)
from ..common.tool_write_context import ToolWriteContextUnavailable
from ..concurrency.interrupt import is_interrupted
from ..turn_end import result_turn_end_reason, should_continue_task
from .agent_activity import BackgroundMainActivitySink
from .background_compact_context import apply_background_compact_context
from .background_context import (
    is_narrow_audit_event,
    prepare_background_context,
    render_background_context,
)
from .background_history_seed import prepare_background_history_or_raise, refresh_background_history
from .claim_execution_binding import publish_claim_runtime_authority
from .compact_carry import compact_overflow_carry
from .compact_progress import COMPACT_TRIGGER_PREFLIGHT
from .local_run_control import LocalRunControl
from .models import ConversationThread
from .store import ConversationStore


# LLM: 字段来自已冻结后台请求；执行不修改请求、不解析正文身份，也不需要调度器或投递服务。
# 类用途: 声明执行片实际消费的宿主请求事实，便于准备层与执行层独立维护。
class BackgroundExecutionRequest(Protocol):
    thread_id: str
    task_id: str
    reason: str
    route_channel: str
    conversation_turn_id: str
    wake_signal: dict | None


# LLM: 每次模型尝试用最新线程和原生历史构造新参数；调用方捕获同一份 Goal/权限上下文，不发模型请求。
# 类用途: 声明后台准备层交给执行器的唯一回调，避免执行器依赖调度器内部方法。
class BackgroundRunPreparation(Protocol):
    # LLM: 必须返回当前代次的新 RunParams；同片已执行工具和插话由执行器随后接续，不由准备器重建。
    # 函数用途: 按最新线程、当前原生历史准备一轮执行参数。
    def __call__(self, thread_id: str, *, thread: ConversationThread, history_seed: object | None) -> RunParams: ...


# LLM: 依赖只有执行能力、唯一持久存储和参数准备；不得把 channels/scheduler 或另一份状态装入这里。
# 类用途: 显式连接后台单片执行需要的三项能力，不创建任何新持久状态。
@dataclass(frozen=True)
class BackgroundExecutionDependencies:
    agent: object
    store: ConversationStore
    prepare_run: BackgroundRunPreparation


# LLM: 字段均由同一片 AgentRunResult 和显示快照投影；结果不代表 Goal 完成或投递成功。
# 类用途: 用具名字段交接执行产出，防止计数、附件和显示历史因元组位置错位。
@dataclass(frozen=True)
class BackgroundExecutionResult:
    response: str
    tool_call_count: int
    tool_success_count: int
    material_progress_count: int
    delivery_artifacts: tuple[dict[str, object], ...]
    message_tool_deliveries: tuple[dict[str, object], ...]
    operation_verification: dict[str, object]
    assistant_commentaries: tuple[str, ...]
    display_snapshot: dict[str, object]
    runtime_status: str = ""
    runtime_reason: str = ""
    internal_response: str = ""
    public_receipt: dict[str, object] | None = None


# LLM: 仅在同片八次 Compact 都推进 canonical 代次后让出；调度器必须单独处理，不将其记为失败。
# 类用途: 表示健康长任务用完本片压缩配额，等待下一片从原检查点继续。
class BackgroundCompactSliceYield(RuntimeError):
    pass


# LLM: 子代理容量在原创建事务复核后暂不可用；后台宿主保留原 wake/正文，沿原不执行退避，不能记失败或 DONE。
# 类用途: 表示后台任务仍在等待容量，没有进入模型或工作执行。
class BackgroundCapacityDeferred(RuntimeError):
    pass


# LLM: 先原callback再本地身份，确切BG producer在原T内发布同claim定位；不按task猜run，不从投影恢复执行权。
# 类用途: 接收后台实际执行身份，让 Compact 使用 core 已绑定的 run/attempt，同时保留原任务确认回调。
class _BackgroundRunControl(LocalRunControl):
    # LLM: 每次 agent.run 创建独立句柄，只保存本调用的发布投影；不跨片持久化或变更任务状态。
    # 函数用途: 组合后台原回调与现有本地身份校验。
    def __init__(self, request_id: str, previous: object) -> None:
        super().__init__(request_id)
        self._previous = previous

    # LLM: 原回调和本地身份先核；只有确切BG binding在原T内发布同claim运输定位，普通路径保原publisher与零新增I/O。
    # 函数用途: 在模型前发布 RuntimeDB 已决定的身份，供后台控制准确定位 Compact 后的新尝试。
    def bind_runtime_authority(self, binding: dict[str, str]) -> bool:
        self.check_admission()
        publish = getattr(self._previous, "bind_runtime_authority", None)
        if callable(publish) and publish(binding) is not True:
            return False
        accepted = super().bind_runtime_authority(binding)
        from ..gateway_parts.multiprocess_background_execution import BackgroundRuntimeBinding

        if type(self._previous) is BackgroundRuntimeBinding:
            self._previous.publish_claim_runtime_authority(binding)
        else:
            publish_claim_runtime_authority(binding)
        return accepted

    # LLM: 任务晋升仍交原宿主确认；无原回调时只确认本调用尚未关闭，不新增持久任务或权限。
    # 函数用途: 保持后台已有任务链接回调的确认结果。
    def __call__(self, link: object) -> bool:
        if not super().__call__(link):
            return False
        if self._previous is None:
            return True
        return callable(self._previous) and self._previous(link) is True


# LLM: 只回填 core 已发布的身份；异常退出沿原账释放该 attempt 的提交前预留，submitted 未知仍保留，正常 Compact 保持原恢复路径。
# LLM: bind_background_run_params只组合原live来源；无scope同对象，control保resolved原回调；actual未结不释放预留。
# 函数用途: 执行原后台尝试并发布真实身份；保完成协议内部结果、Compact与原宿主权限。
def _run_background_model_attempt(agent: object, prompt: str, params: RunParams) -> tuple[object, RunParams]:
    from ..agent_core.runtime.run_params import run_params_with_request_id
    from ..gateway_parts.multiprocess_background_execution import bind_background_run_params

    resolved = bind_background_run_params(agent, run_params_with_request_id(params))
    control = _BackgroundRunControl(resolved.request_id, resolved.conversation_task_binding_callback)
    try:
        result = agent.run(prompt, params=replace(resolved, conversation_task_binding_callback=control))
    except (RequestExecutionOutcomeUncertainError, BoundedCallExitUnconfirmedError,
            RequestExecutionContextUnavailable, ToolWriteContextUnavailable):
        raise
    except Exception:
        _release_failed_attempt_inputs(agent, resolved.request_id, control.runtime_authority())
        raise
    finally:
        control.finish()
    binding = control.runtime_authority()
    from .background_receipt_contract import raise_background_receipt_failure

    raise_background_receipt_failure(result, persist_result=params.partial_turn_callback)
    return result, replace(params, request_id=resolved.request_id,
        run_id=binding.get("run_id", resolved.run_id), task_id=binding.get("task_id", resolved.task_id),
        attempt_id=binding.get("attempt_id", resolved.attempt_id))


# LLM: agent.run 已退出，只有 core 正式发布的精确 attempt 能证明其预留可释放；原提交批次先修复，不释放 submitted。
#   恢复失败记录结构化身份，不输出消息正文，也不能遮盖原异常；后续恢复器仍能读取原账。
# LLM: 辅助恢复抛actual来源未结必须原对象交回，普通恢复错误仍只记录；不能伪造已释放。
# 函数用途: 仅在普通异常或Esc后恢复未提交插话，actual未知不提前释放原责任。
def _release_failed_attempt_inputs(agent: object, turn_id: str, binding: dict[str, str]) -> None:
    attempt_id = binding.get("attempt_id", "")
    if not attempt_id:
        return
    try:
        summary = agent.conversation_store.guidance.recovery.release_reserved(turn_id, dead_attempt_id=attempt_id)
        if summary.get("errors"):
            raise RuntimeError("guidance reservation recovery incomplete")
    except (RequestExecutionOutcomeUncertainError, BoundedCallExitUnconfirmedError,
            RequestExecutionContextUnavailable, ToolWriteContextUnavailable):
        raise
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "后台插话预留恢复未完成：turn_id=%s attempt_id=%s error_type=%s",
            turn_id, attempt_id, type(exc).__name__,
        )


# LLM: 调用方已冻结模型作用域与提示；执行保持同一请求和活动 sink，异常/取消不提交伪造 final。
#   turn_trigger 是调用方按唤醒信封构造的回合触发类型（None 为普通回合），原样交给每次模型尝试。
# LLM: actual未结不调用sink.fail伪报普通失败；不包住模型等待，原F6/CAS/媒体与静默通知保持。
# 函数用途: 运行原后台片并整理显示，未结原样退出，Compact/容量/普通错误保既有边界。
def invoke_background_turn(
    execution: BackgroundExecutionDependencies,
    thread: ConversationThread,
    request: BackgroundExecutionRequest,
    *,
    user_prompt: str,
    continuation_injection: list[str],
    proactive_delivery_available: bool | None,
    turn_trigger: object | None = None,
) -> tuple[object, dict[str, object]]:
    activity_sink = BackgroundMainActivitySink(
        execution.agent,
        thread_id=thread.thread_id,
        task_id=request.task_id,
    )
    try:
        result = run_background_turn_with_compact(
            execution,
            thread,
            request,
            user_prompt=user_prompt,
            continuation_injection=continuation_injection,
            proactive_delivery_available=proactive_delivery_available,
            activity_sink=activity_sink,
            turn_trigger=turn_trigger,
        )
    except (RequestExecutionOutcomeUncertainError, BoundedCallExitUnconfirmedError,
            RequestExecutionContextUnavailable, ToolWriteContextUnavailable):
        raise
    except (BackgroundCompactSliceYield, BackgroundCapacityDeferred):
        activity_sink.finish()
        raise
    except Exception:
        activity_sink.fail()
        raise
    activity_sink.finish()
    snapshot = activity_sink.display_history_snapshot()
    snapshot["turn_end_reason"] = result_turn_end_reason(result)
    snapshot["goal_continuation_allowed"] = (
        snapshot["turn_end_reason"] == "completed" or should_continue_task(result)[0]
    )
    return result, snapshot


# LLM: 本片冻结摘要范围、覆盖和首次解析的请求身份；后续准备复用该 request_id，run/attempt 仍由原绑定。
#   回合触发类型每次尝试都写进新准备的 RunParams，压缩续跑与首次尝试用同一个；携带记录及其是否读全按首次尝试冻结。
# 函数用途: 用同一适用摘要准备后台模型片，压缩续跑沿用展示和拒绝记录，成功提交计入公平让出预算。
def run_background_turn_with_compact(
    execution: BackgroundExecutionDependencies,
    thread: ConversationThread,
    request: BackgroundExecutionRequest,
    *,
    user_prompt: str,
    continuation_injection: list[str],
    proactive_delivery_available: bool | None,
    activity_sink: BackgroundMainActivitySink,
    turn_trigger: object | None = None,
) -> object:
    current = thread
    carried_archive_tool_calls: list[dict[str, object]] | None = None
    carry_incomplete: list[dict[str, str]] | None = None
    carried_active_turn_user_inputs: list[dict[str, object]] = []
    native_compact_carry = None
    runtime_rejected_actions: list[dict[str, str]] | None = None
    capability_presentation = None
    presentation_evaluated = False
    # 首次尝试是发送前按完整请求的自动预检；之后由溢出结果的 runtime_source 说明每次恢复的触发来源。
    recovering, compact_trigger_source = False, COMPACT_TRIGGER_PREFLIGHT
    committed_generations = 0
    slice_request_id = ""

    # LLM: 同步回调只写当前调用的局部状态及原参数，失效 None 不清已评估事实，绝不落盘或回写请求。
    # 函数用途: 让 Compact 和后续模型尝试都读取本后台 turn 的最新展示。
    def retain_presentation(value) -> None:
        nonlocal capability_presentation, presentation_evaluated
        capability_presentation, presentation_evaluated = value, True
        run_params.capability_presentation = value
        run_params.capability_presentation_evaluated = True

    history = prepare_background_history_or_raise(
        execution.agent, execution.store, current, request,
        proactive_delivery_available=proactive_delivery_available,
    )
    # 首次业务尝试只有真实提交才消耗Compact配额，之后每次恢复必须真实提交才可继续。
    for _attempt in range(9):
        history_seed = history.seed
        run_params = execution.prepare_run(
            current.thread_id,
            thread=current,
            history_seed=history_seed,
        )
        from .background_receipt_contract import configure_background_public_receipt

        configure_background_public_receipt(execution.agent, request, run_params)
        run_params.request_id = slice_request_id or run_params.request_id
        run_params.compact_context = history.compact_context
        if carried_archive_tool_calls is None:
            carried_archive_tool_calls = list(run_params.carried_archive_tool_calls or [])
        else:
            run_params.carried_archive_tool_calls = list(carried_archive_tool_calls)
        carry_incomplete = _freeze_carry_completeness(run_params, carry_incomplete)
        run_params.carried_active_turn_user_inputs = list(carried_active_turn_user_inputs)
        run_params.native_compact_carry = native_compact_carry
        run_params.conversation_turn_id = request.conversation_turn_id
        run_params.turn_trigger = turn_trigger
        if runtime_rejected_actions is None:
            runtime_rejected_actions = run_params.runtime_rejected_actions
        else:
            run_params.runtime_rejected_actions = runtime_rejected_actions
        prepared_context = _prepared_slice_context(
            execution, request, thread=current, history=history, proactive_delivery_available=proactive_delivery_available,
        )
        run_params.inject = [render_background_context(prepared_context), *continuation_injection]
        run_params.on_chunk = activity_sink
        run_params.partial_turn_callback = partial(_persist_background_native_turn, execution.store, request)
        run_params.capability_presentation = capability_presentation
        run_params.capability_presentation_evaluated = presentation_evaluated
        run_params.capability_presentation_turn_id = request.conversation_turn_id
        run_params.capability_presentation_callback = retain_presentation
        activity_sink.begin_model_attempt(_attempt + 1)
        result, run_params, recovery = _run_background_recovery_attempt(
            execution, user_prompt, run_params, history, prepared_context,
            recovering=recovering, activity_sink=activity_sink, trigger_source=compact_trigger_source,
        )
        slice_request_id = slice_request_id or run_params.request_id
        if recovering and (recovery is None or not recovery.committed):
            raise RuntimeError("background compact recovery did not commit")
        if recovery is not None and recovery.committed:
            current = recovery.committed_thread
            committed_generations += 1
        if str(getattr(result, "runtime_status", "") or "").strip().lower() != ("context_overflow"):
            _persist_background_native_turn(execution.store, request, result)
            return result
        native_compact_carry, carried_archive_tool_calls, carried_active_turn_user_inputs = _next_overflow_carry(
            result, carried_archive_tool_calls, carried_active_turn_user_inputs,
        )
        if committed_generations >= 8:
            raise BackgroundCompactSliceYield(
                "background main compact slice advanced eight generations and will resume"
            )
        current, history = _refresh_background_compact_source(execution, current, history)
        run_params.compact_context = history.compact_context
        if history.compact_source is None or (not history.compact_source.messages and not carried_archive_tool_calls
                                             and native_compact_carry is None):
            raise RuntimeError("background compact has no recoverable source")
        recovering, compact_trigger_source = True, str(getattr(result, "runtime_source", "") or "")


# LLM: 每次尝试都按本次线程和已冻结的历史重新准备后台上下文（含本次适用的压缩视图），不跨尝试复用渲染结果；
#   有历史种子时不渲染 Recent Messages。会读取 store 与任务事实，不写状态。
# 函数用途: 准备一次后台模型尝试要用的上下文，供注入渲染与压缩恢复共用。
def _prepared_slice_context(
    execution: BackgroundExecutionDependencies,
    request: BackgroundExecutionRequest,
    *,
    thread: ConversationThread,
    history: object,
    proactive_delivery_available: bool | None,
) -> object:
    prepared = prepare_background_context(
        agent=execution.agent, store=execution.store, thread=thread, request=request,
        proactive_delivery_available=proactive_delivery_available,
        include_recent_messages=history.seed is None, context_bundle=history.context_bundle,
    )
    return apply_background_compact_context(prepared, history.compact_context, thread.compact_generation)


# LLM: 溢出结果只交回同宿主的原生携带与已释放插话编号；工具携带记录与插话按 compact_overflow_carry 的唯一规则合并，
#   不读正文、不写状态。
# 函数用途: 从一次上下文溢出结果算出下一次尝试的原生携带、工具携带记录和插话携带。
def _next_overflow_carry(
    result: object,
    carried_archive_tool_calls: list[dict[str, object]],
    carried_active_turn_user_inputs: list[dict[str, object]],
) -> tuple[object, list[dict[str, object]], list[dict[str, object]]]:
    from .compact_carry import native_compact_carry_from_result

    native_carry = native_compact_carry_from_result(result)
    released_input_ids = native_carry.released_input_ids if native_carry is not None else ()
    archive, inputs = compact_overflow_carry(
        carried_archive_tool_calls=carried_archive_tool_calls,
        carried_active_turn_user_inputs=carried_active_turn_user_inputs,
        result_archive_tool_calls=getattr(result, "archive_tool_calls", None),
        result_active_turn_user_inputs=getattr(result, "active_turn_user_inputs", None),
        released_input_ids=released_input_ids,
    )
    return native_carry, archive, inputs


# LLM: 携带记录只在首次尝试读取并冻结，是否读全也按首次结论冻结：首次没读全时，之后每次尝试都写回同一条不完整事实，
#   即使重读成功也不能把首次的半份携带当成完整；首次读全时保留后续尝试自己的事实（后续读错只会更保守）。只改本次
#   RunParams 的 task_attributes。
# 函数用途: 在压缩重试之间保持“本片携带记录是否读全”的首次结论，返回冻结值供下一次尝试使用。
def _freeze_carry_completeness(run_params: RunParams, frozen: list[dict[str, str]] | None) -> list[dict[str, str]]:
    from .authority import CONVERSATION_ACTIVE_TURN_CARRY_INCOMPLETE_ATTR as carry_key

    attributes = run_params.task_attributes if isinstance(run_params.task_attributes, dict) else {}
    if frozen is None:
        return [dict(item) for item in attributes.get(carry_key) or []]
    if frozen:
        attributes[carry_key] = [dict(item) for item in frozen]
    return frozen


# LLM: 原生信封只进入当前 store/thread 的同一 transcript；与公开 final 按宿主回合编号去重，不发消息或修改 wake 生命周期。
# 函数用途: 保存原生历史与explicit内部总结，静默或公开修复失败也保留原产出；写盘失败不冒充保存成功。
def _persist_background_native_turn(store: ConversationStore, request: BackgroundExecutionRequest, result: object) -> None:
    from .native_history import (
        CANONICAL_NATIVE_MESSAGES_METADATA_KEY,
        canonical_native_messages_envelope,
    )

    # 窄范围审计事件不是普通会话续接，沿原隔离合同保存，不把内部事件写入用户模型历史。
    if is_narrow_audit_event(request.reason):
        return
    native = canonical_native_messages_envelope(getattr(result, "canonical_native_messages", None))
    internal_result = str(getattr(result, "internal_response", "") or "")
    if not native and not internal_result:
        return
    store.messages.append_once({
        "thread_id": request.thread_id, "role": "assistant", "content": "", "channel": request.route_channel,
        "metadata": {
            "conversation_request_id": request.conversation_turn_id,
            "assistant_part_id": "native", "task_id": request.task_id,
            "turn_end_reason": result_turn_end_reason(result),
            CANONICAL_NATIVE_MESSAGES_METADATA_KEY: native,
            "internal_execution_result": internal_result,
            "public_receipt": getattr(result, "public_receipt", None),
        },
    }, dedupe_key=f"background-native:{request.conversation_turn_id}")


# LLM: 只刷新原CAS thread及已冻结范围的来源；不在真实恢复准备前摘要，narrow仍为已知空transcript来源。
# 函数用途: 溢出后读取下一轮来源，完整请求准备后再由公共恢复器压缩。
def _refresh_background_compact_source(execution, current, history):
    latest, load_error = execution.store.threads.load_report(current.thread_id)
    if load_error is not None or latest is None:
        raise RuntimeError("background main conversation thread could not be reloaded")
    return latest, refresh_background_history(execution.agent, execution.store, latest, history)


# LLM: 首次自动和溢出恢复均绑定同次已冻结来源；force只区分阈值与已报溢出，完整准备前不得另路粗估提交。
# 函数用途: 执行后台一次原模型尝试，并将可能获胜的Compact线程及原运行身份交回宿主。
def _run_background_recovery_attempt(execution, prompt, params, history, context, *, recovering, activity_sink,
                                     trigger_source=""):
    from ..agent_core.runtime.run_params import run_params_with_request_id
    from ..model_request_selection import model_request_selection_scope
    from .background_compact_recovery import prepare_background_compact_recovery

    params = run_params_with_request_id(params)
    recovery = None
    if history.compact_source is not None:
        recovery = prepare_background_compact_recovery(
            execution.agent, history, context, request_id=params.request_id,
            progress_callback=activity_sink.write_conversation_compact_progress, interrupt_check=is_interrupted,
            force=recovering, trigger_source=trigger_source,
        )
    with model_request_selection_scope(recovery) if recovery is not None else nullcontext():
        result, resolved = _run_background_model_attempt(execution.agent, prompt, params)
    return result, resolved, recovery


# LLM: 工具成功与有副作用成功按运行时 policy 统计；不能从正文或工具名猜测，不修改工具账。
# 函数用途: 为执行结果统计当前片真正成功的状态变更工具。
def material_tool_success_count(
    agent: object,
    calls: list[dict[str, object]],
) -> int:
    """Count successful state-changing calls from the run-time policy, never prose."""
    from ..tooling.models import tool_effect_for_runtime_policy

    registry = getattr(agent, "tools", None)
    if registry is None or not hasattr(registry, "runtime_snapshot"):
        return 0
    snapshot = registry.runtime_snapshot()
    count = 0
    for record in calls:
        if record.get("ok") is not True:
            continue
        tool_name = str(record.get("tool") or "").strip()
        runtime = snapshot.runtime(tool_name)
        if runtime is None:
            continue
        raw = record.get("parameters")
        arguments = dict(raw) if isinstance(raw, dict) else {}
        arguments.pop("tool", None)
        if tool_effect_for_runtime_policy(runtime.runtime_policy, arguments) in {
            "mutating",
            "dangerous",
        }:
            count += 1
    return count


# LLM: 后台轮与前台轮必须复用同一个 public operation projection；不得在会话层重算工具终态。
# 函数用途: 从 AgentRunResult 提取已由 canonical operation ledger 生成的安全公开核验摘要。
def public_result_operation_verification(result: object) -> dict[str, object]:
    from ..tooling.operation_verification import public_operation_verification

    projected = public_operation_verification(getattr(result, "operation_verification", None))
    return projected


# LLM: 在模型作用域退出后按原顺序投影结果；工具事实来自原始执行账，公开核验沿唯一投影入口。
# 函数用途: 把执行回执与显示快照归为具名结果，供原收口与投递事务消费。
def collect_background_execution_result(
    agent: object, result: object, display_snapshot: dict[str, object],
) -> BackgroundExecutionResult:
    calls = [item for item in (getattr(result, "archive_tool_calls", None) or []) if isinstance(item, dict)]
    successes = sum(1 for item in calls if item.get("ok") is True)
    material_progress = material_tool_success_count(agent, calls)
    artifacts = tuple(dict(item) for item in (getattr(result, "delivery_artifacts", None) or []) if isinstance(item, dict))
    deliveries = tuple(dict(item) for item in (getattr(result, "message_tool_deliveries", None) or []) if isinstance(item, dict))
    operation_verification = public_result_operation_verification(result)
    assistant_commentaries = tuple(
        text for value in (getattr(result, "assistant_commentary_messages", None) or ())
        if (text := str(value or "").strip())
    )
    return BackgroundExecutionResult(
        response=str(getattr(result, "response", "") or ""),
        tool_call_count=len(calls), tool_success_count=successes, material_progress_count=material_progress,
        delivery_artifacts=artifacts, message_tool_deliveries=deliveries,
        operation_verification=operation_verification, assistant_commentaries=assistant_commentaries,
        display_snapshot=display_snapshot,
        runtime_status=str(getattr(result, "runtime_status", "") or ""),
        runtime_reason=str(getattr(result, "runtime_reason", "") or ""),
        internal_response=str(getattr(result, "internal_response", "") or ""),
        public_receipt=getattr(result, "public_receipt", None),
    )
