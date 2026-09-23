# LLM: 后台单片不拥有 wake/Goal/租约或投递；同 conversation_turn_id 的 Compact 保留展示与已评估事实，新片清零，历史/CAS/取消仍沿原合同。
# 模块用途: 执行后台模型工作片并压缩重试；复用本轮展示，失效清除后不重发推荐，也不保存第二份状态。
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, replace
from functools import partial
from typing import Protocol

from ..agent_core.runtime.loop_models import RunParams
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
from .compact_carry import compact_overflow_carry
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


# LLM: 仅在同片八次 Compact 都推进 canonical 代次后让出；调度器必须单独处理，不将其记为失败。
# 类用途: 表示健康长任务用完本片压缩配额，等待下一片从原检查点继续。
class BackgroundCompactSliceYield(RuntimeError):
    pass


# LLM: 复用本地调用的原身份发布句柄，先尊重已有宿主回调；不按 task 查询猜 run，不从展示载体恢复执行权。
# 类用途: 接收后台实际执行身份，让 Compact 使用 core 已绑定的 run/attempt，同时保留原任务确认回调。
class _BackgroundRunControl(LocalRunControl):
    # LLM: 每次 agent.run 创建独立句柄，只保存本调用的发布投影；不跨片持久化或变更任务状态。
    # 函数用途: 组合后台原回调与现有本地身份校验。
    def __init__(self, request_id: str, previous: object) -> None:
        super().__init__(request_id)
        self._previous = previous

    # LLM: 原回调拒绝或抛异常时不发布本地副本，由 core 沿原失败合同收口 attempt。
    # 函数用途: 在模型启动前收取已经由 RuntimeDB 决定的执行身份。
    def bind_runtime_authority(self, binding: dict[str, str]) -> bool:
        self.check_admission()
        publish = getattr(self._previous, "bind_runtime_authority", None)
        if callable(publish) and publish(binding) is not True:
            return False
        return super().bind_runtime_authority(binding)

    # LLM: 任务晋升仍交原宿主确认；无原回调时只确认本调用尚未关闭，不新增持久任务或权限。
    # 函数用途: 保持后台已有任务链接回调的确认结果。
    def __call__(self, link: object) -> bool:
        if not super().__call__(link):
            return False
        if self._previous is None:
            return True
        return callable(self._previous) and self._previous(link) is True


# LLM: 原请求默认解析和身份发布共用生产入口；只回填 core 已发布的 run/attempt，展示回调仍更新原参数，异常不吞。
# 函数用途: 执行后台模型尝试并将准确身份交给后续 Compact，解决恢复既有主 run 时 task_id 与 run_id 不同的问题。
def _run_background_model_attempt(agent: object, prompt: str, params: RunParams) -> tuple[object, RunParams]:
    from ..agent_core.runtime.run_params import run_params_with_request_id

    resolved = run_params_with_request_id(params)
    control = _BackgroundRunControl(resolved.request_id, params.conversation_task_binding_callback)
    try:
        result = agent.run(prompt, params=replace(resolved, conversation_task_binding_callback=control))
    finally:
        control.finish()
    binding = control.runtime_authority()
    return result, replace(params, request_id=resolved.request_id,
        run_id=binding.get("run_id", resolved.run_id), task_id=binding.get("task_id", resolved.task_id),
        attempt_id=binding.get("attempt_id", resolved.attempt_id))


# LLM: 调用方已冻结模型作用域与提示；执行保持同一请求和活动 sink，异常/取消不提交伪造 final。
# 函数用途: 运行一片后台模型并整理显示快照，Compact 公平让出与真实错误分别收尾。
def invoke_background_turn(
    execution: BackgroundExecutionDependencies,
    thread: ConversationThread,
    request: BackgroundExecutionRequest,
    *,
    user_prompt: str,
    continuation_injection: list[str],
    proactive_delivery_available: bool | None,
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
        )
    except BackgroundCompactSliceYield:
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


# LLM: 本片冻结摘要范围及覆盖，与原历史种子和工具参数一同传递；展示绑定宿主turn，局部Compact不发布全线程摘要。
# 函数用途: 用同一适用摘要准备后台模型片，压缩续跑沿用展示和拒绝记录，失败或取消沿原路径退出。
def run_background_turn_with_compact(
    execution: BackgroundExecutionDependencies,
    thread: ConversationThread,
    request: BackgroundExecutionRequest,
    *,
    user_prompt: str,
    continuation_injection: list[str],
    proactive_delivery_available: bool | None,
    activity_sink: BackgroundMainActivitySink,
) -> object:
    current = thread
    carried_archive_tool_calls: list[dict[str, object]] | None = None
    carried_active_turn_user_inputs: list[dict[str, object]] = []
    runtime_rejected_actions: list[dict[str, str]] | None = None
    capability_presentation = None
    presentation_evaluated = False
    recovering = False

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
    # 首次业务尝试不消耗Compact配额，之后每次必须真实提交才可继续。
    for _attempt in range(9):
        history_seed = history.seed
        run_params = execution.prepare_run(
            current.thread_id,
            thread=current,
            history_seed=history_seed,
        )
        run_params.compact_context = history.compact_context
        if carried_archive_tool_calls is None:
            carried_archive_tool_calls = list(run_params.carried_archive_tool_calls or [])
        else:
            run_params.carried_archive_tool_calls = list(carried_archive_tool_calls)
        run_params.carried_active_turn_user_inputs = list(carried_active_turn_user_inputs)
        if runtime_rejected_actions is None:
            runtime_rejected_actions = run_params.runtime_rejected_actions
        else:
            run_params.runtime_rejected_actions = runtime_rejected_actions
        prepared_context = prepare_background_context(
            agent=execution.agent, store=execution.store, thread=current, request=request,
            proactive_delivery_available=proactive_delivery_available,
            include_recent_messages=history_seed is None, context_bundle=history.context_bundle,
        )
        prepared_context = apply_background_compact_context(prepared_context, history.compact_context, current.compact_generation)
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
            recovering=recovering, activity_sink=activity_sink,
        )
        if recovering and (recovery is None or not recovery.committed):
            raise RuntimeError("background compact recovery did not commit")
        if recovery is not None and recovery.committed:
            current = recovery.committed_thread
        if str(getattr(result, "runtime_status", "") or "").strip().lower() != ("context_overflow"):
            _persist_background_native_turn(execution.store, request, result)
            return result
        from ..agent_core.runtime_mixin import release_active_turn_inputs_for_compact

        released_input_ids = release_active_turn_inputs_for_compact(execution.agent, run_params)
        carried_archive_tool_calls, carried_active_turn_user_inputs = compact_overflow_carry(
            carried_archive_tool_calls=carried_archive_tool_calls,
            carried_active_turn_user_inputs=carried_active_turn_user_inputs,
            result_archive_tool_calls=getattr(result, "archive_tool_calls", None),
            result_active_turn_user_inputs=getattr(result, "active_turn_user_inputs", None),
            released_input_ids=released_input_ids,
        )
        if _attempt == 8:
            raise BackgroundCompactSliceYield(
                "background main compact slice advanced eight generations and will resume"
            )
        current, history = _refresh_background_compact_source(execution, current, history)
        run_params.compact_context = history.compact_context
        if history.compact_source is None or (not history.compact_source.messages and not carried_archive_tool_calls):
            raise RuntimeError("background compact has no recoverable source")
        recovering = True


# LLM: 原生信封只进入当前 store/thread 的同一 transcript；与公开 final 按宿主回合编号去重，不发消息或修改 wake 生命周期。
# 函数用途: 保存后台正常、静默让出或异常前的工具历史，外发被抑制时也不丢记录；写盘失败不能冒充保存成功。
def _persist_background_native_turn(store: ConversationStore, request: BackgroundExecutionRequest, result: object) -> None:
    from .native_history import (
        CANONICAL_NATIVE_MESSAGES_METADATA_KEY,
        canonical_native_messages_envelope,
    )

    # 窄范围审计事件不是普通会话续接，沿原隔离合同保存，不把内部事件写入用户模型历史。
    if is_narrow_audit_event(request.reason):
        return
    native = canonical_native_messages_envelope(getattr(result, "canonical_native_messages", None))
    if not native:
        return
    store.messages.append_once({
        "thread_id": request.thread_id, "role": "assistant", "content": "", "channel": request.route_channel,
        "metadata": {
            "conversation_request_id": request.conversation_turn_id,
            "assistant_part_id": "native", "task_id": request.task_id,
            "turn_end_reason": result_turn_end_reason(result),
            CANONICAL_NATIVE_MESSAGES_METADATA_KEY: native,
        },
    }, dedupe_key=f"background-native:{request.conversation_turn_id}")


# LLM: 只刷新原CAS thread及已冻结范围的来源；不在真实恢复准备前摘要，narrow仍为已知空transcript来源。
# 函数用途: 溢出后读取下一轮来源，完整请求准备后再由公共恢复器压缩。
def _refresh_background_compact_source(execution, current, history):
    latest, load_error = execution.store.threads.load_report(current.thread_id)
    if load_error is not None or latest is None:
        raise RuntimeError("background main conversation thread could not be reloaded")
    return latest, refresh_background_history(execution.agent, execution.store, latest, history)


# LLM: 只有真实溢出后且来源已知才装恢复宿主；finally清ContextVar，transcript或活动归档都先完整计量再同次发送。
# 函数用途: 执行后台一次原模型尝试，恢复时同时带回获胜CAS的线程与参数事实。
def _run_background_recovery_attempt(execution, prompt, params, history, context, *, recovering, activity_sink):
    from ..agent_core.runtime.run_params import run_params_with_request_id
    from ..model_request_selection import model_request_selection_scope
    from .background_compact_recovery import prepare_background_compact_recovery

    params = run_params_with_request_id(params)
    recovery = None
    if recovering and history.compact_source is not None:
        recovery = prepare_background_compact_recovery(
            execution.agent, history, context, request_id=params.request_id,
            progress_callback=activity_sink.write_conversation_compact_progress, interrupt_check=is_interrupted,
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
    )
