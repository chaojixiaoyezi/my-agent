# LLM: 后台单片不拥有 wake/Goal/租约或外部投递；同片 Compact 保留拒绝记忆，历史、CAS 和取消仍沿原合同。
# 模块用途: 独立后台模型工作片、压缩重试和具名执行结果，修改时同步后台历史、模型选择、取消和投递回归。
from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import Protocol

from ..agent_core.runtime.loop_models import RunParams
from ..concurrency.interrupt import is_interrupted
from ..turn_end import result_turn_end_reason, should_continue_task
from .agent_activity import BackgroundMainActivitySink
from .background_context import context_markdown, is_narrow_audit_event
from .background_history_seed import background_history_seed_or_raise
from .compact_carry import compact_overflow_carry
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


# LLM: 后台生命周期唤醒继续同一个权威活动轮；超窗在同片压缩并携带已执行工具与插话，
# 携带快照与前台共用纯 reducer，同片拒绝记忆保持原对象；mailbox 释放和历史写回顺序不变，同步 Compact 回归。
# 函数用途: 后台主代理在同片压缩并续跑；正常/异常原生历史写回原会话，工具显示批次不改变执行身份。
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
    for _attempt in range(8):
        history_seed = background_history_seed_or_raise(
            execution.agent,
            execution.store,
            current,
            request,
            proactive_delivery_available=proactive_delivery_available,
        )
        run_params = execution.prepare_run(
            current.thread_id,
            thread=current,
            history_seed=history_seed,
        )
        if carried_archive_tool_calls is None:
            carried_archive_tool_calls = list(run_params.carried_archive_tool_calls or [])
        else:
            run_params.carried_archive_tool_calls = list(carried_archive_tool_calls)
        run_params.carried_active_turn_user_inputs = list(carried_active_turn_user_inputs)
        if runtime_rejected_actions is None:
            runtime_rejected_actions = run_params.runtime_rejected_actions
        else:
            run_params.runtime_rejected_actions = runtime_rejected_actions
        run_params.inject = [
            context_markdown(
                agent=execution.agent,
                store=execution.store,
                thread=current,
                request=request,
                proactive_delivery_available=proactive_delivery_available,
                # 已经带上 canonical 历史时不再重复注入最近消息副本：
                # 两份历史会重复计费、并且摘要副本没有工具细节。
                include_recent_messages=history_seed is None,
            ),
            *continuation_injection,
        ]
        run_params.on_chunk = activity_sink
        run_params.partial_turn_callback = partial(_persist_background_native_turn, execution.store, request)
        activity_sink.begin_model_attempt(_attempt + 1)
        result = execution.agent.run(user_prompt, params=run_params)
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
        refreshed = _compact_background_main_thread(
            execution,
            current,
            current_prompt=user_prompt,
            activity_sink=activity_sink,
            run_params=run_params,
            carried_archive_tool_calls=carried_archive_tool_calls,
        )
        if refreshed.compact_generation <= current.compact_generation:
            refreshed = _compact_background_active_turn(
                execution, refreshed, run_params, carried_archive_tool_calls,
                task_id=request.task_id, user_prompt=user_prompt, activity_sink=activity_sink,
            )
        current = refreshed
    raise BackgroundCompactSliceYield(
        "background main compact slice advanced eight generations and will resume"
    )


# LLM: 已结束历史无压缩进展时仅压缩本活动轮归档；沿原 CAS 与取消合同提交，不凭输出正文判定可继续。
# 函数用途: 缩减后台当前轮的工具上下文并确认真正推进，失败时保留原错误而不空转重试。
def _compact_background_active_turn(
    execution: BackgroundExecutionDependencies,
    thread: ConversationThread,
    run_params: RunParams,
    archive: list[dict[str, object]],
    *,
    task_id: str,
    user_prompt: str,
    activity_sink: BackgroundMainActivitySink,
) -> ConversationThread:
    from .active_turn_compact import (
        ActiveTurnArchiveCompactRequest,
        compact_carried_active_turn_archive,
    )

    result = compact_carried_active_turn_archive(
        execution.agent, execution.store, thread, archive,
        ActiveTurnArchiveCompactRequest(
            task_attributes=run_params.task_attributes,
            request_id=str(run_params.request_id or task_id or ""),
            attempt_id=str(run_params.attempt_id or run_params.request_id or ""),
            task_prompt=user_prompt,
            progress_callback=activity_sink.write_conversation_compact_progress,
            interrupt_check=is_interrupted,
        ),
    )
    if not result.compacted:
        raise RuntimeError("background main thread cannot compact the overflowing active turn")
    return result.thread


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


# LLM: 最新线程与 canonical Compact CAS 是重试权威；临时工具从归档状态恢复，取消贯穿摘要和提交。
# 显示快照不能推进压缩代次，修改时同步同片重试与取消回归。
# 函数用途: 以后台主代理已完成历史和当前工具归档可中断地压缩，并返回同一轮继续用的最新线程。
def _compact_background_main_thread(
    execution: BackgroundExecutionDependencies,
    current: ConversationThread,
    *,
    current_prompt: str,
    activity_sink: BackgroundMainActivitySink,
    run_params: RunParams,
    carried_archive_tool_calls: list[dict[str, object]],
) -> ConversationThread:
    from ..tooling.tool_search_state import pending_carried_loaded_tool_names
    from .compact import ConversationCompactOptions, prepare_conversation_context
    from .compact_provider_surface import ConversationCompactModelSurface

    latest, load_error = execution.store.threads.load_report(current.thread_id)
    if load_error is not None or latest is None:
        raise RuntimeError("background main conversation thread could not be reloaded")
    compact = prepare_conversation_context(
        execution.agent,
        execution.store,
        latest,
        options=ConversationCompactOptions(
            current_prompt=str(current_prompt or ""),
            force=True,
            progress_callback=activity_sink.write_conversation_compact_progress,
            interrupt_check=is_interrupted,
            model_surface=ConversationCompactModelSurface(
                allowed_tools=(
                    tuple(run_params.allowed_tools)
                    if run_params.allowed_tools is not None
                    else None
                ),
                prompt_files=tuple(run_params.prompt_files or ()),
                system_prompt_override=run_params.system_prompt_override,
                context_scope=str(run_params.context_scope or "conversation"),
                loaded_tool_names=tuple(
                    sorted(
                        pending_carried_loaded_tool_names(carried_archive_tool_calls)
                    )
                ),
            ),
        ),
    )
    return compact.thread


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
