# LLM: 同一 child turn 的 Compact 重试保留展示与已评估事实，下一 Goal turn 清零；拒绝记忆仍属原 attempt，失败沿原生命周期。
# 模块用途: 执行子代理并保存结果；overflow来源延迟到完整请求准备后压缩，同轮展示失效后不复活。

from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path

from ...concurrency.interrupt import is_interrupted
from ...conversation.active_turn_input import (
    exclude_active_turn_user_input_ids,
    merge_active_turn_user_inputs,
)
from ...conversation.agent_thread import (
    AgentThreadTurnInput,
    append_subagent_thread_result,
    ensure_subagent_thread,
    prepare_subagent_thread_turn,
)
from ...conversation.authority import (
    AGENT_THREAD_ID_ATTR,
    CONVERSATION_REQUEST_ID_ATTR,
    CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR,
)
from ...runtime_context import restore_current_subagent_context, set_current_subagent_context
from ...subagents.context_bundle_refs import runtime_task_attributes
from ...turn_end import result_turn_end_reason
from ..runtime.loop_models import RunParams, RuntimeContextRequest
from ..runtime_mixin import release_active_turn_inputs_for_compact
from .params import (
    SubagentFinalizeParams,
    SubagentProbeParams,
    SubagentRunFailureParams,
    SubagentRunParams,
)


@dataclass(frozen=True)
class SubagentModelTurnBundle:
    options: SubagentRunParams
    active_attempt_id: str
    context: object
    prompt: str


# LLM: This immutable carrier groups only one provider iteration's changing
# transcript/carry state and the exact attempt's rejection memory; it never carries approval grants.
# 类用途: 收拢一次子代理模型调用会变化的 Compact 续接材料，避免长参数列表和字段错位。
@dataclass(frozen=True)
class SubagentModelIteration:
    current: object
    carried_archive_tool_calls: list[dict[str, object]]
    carried_active_turn_user_inputs: list[dict[str, object]]
    transcript_sink: object
    runtime_rejected_actions: list[dict[str, str]]


# LLM: 原溢出请求不产生身份或权限；run_params 仅指向同轮宿主参数，二次准备须读取失效回调清过的当前展示，不复活 frozen surface。
# 类用途: 收拢子代理压缩的代次、轨迹和缓存面；内部defer供真实恢复轮完整准备，缺原参数不补造事实。
@dataclass(frozen=True)
class SubagentOverflowCompactRequest:
    prompt: str
    attempt_id: str
    task_attributes: dict[str, object]
    current: object
    carried_archive_tool_calls: list[dict[str, object]]
    progress_callback: object
    model_surface: object
    conversation_turn_id: str = ""
    run_params: RunParams | None = None
    defer_compact: bool = False


# LLM: exact attempt 先登记真实执行器，再开启可选首次请求准备；退出同时清理范围并记录执行器退出，不新增 task/attempt。
# 函数用途: 执行并记录同一子代理工作片，干跑不登记执行器，不改变原取消和结果收口语义。
def run_subagent_flow(lifecycle, options: SubagentRunParams):
    """Run one subagent task from prompt construction through result persistence."""
    active_attempt_id = _prepare_subagent_attempt(lifecycle, options)
    if options.dry_run:
        _, prompt = _build_prompt(lifecycle, options)
        return lifecycle.record_dry_run(options.run_id, active_attempt_id, prompt)

    from ...runtime_db.executor_liveness import attempt_executor
    from .model_selection import subagent_first_request_scope

    with attempt_executor(lifecycle.agent.subagents.runtime_db, options.run_id, active_attempt_id), subagent_first_request_scope(
        lifecycle.agent, options.run_id, active_attempt_id,
    ):
        return _run_prepared_subagent(lifecycle, options, active_attempt_id)


# LLM: 本函数运行在已登记的执行器内部；结果与异常沿原生命周期入口处理，不新增业务完成裁决。
# 函数用途: 依次探测、构造提示并执行当前子代理工作片。
def _run_prepared_subagent(lifecycle, options: SubagentRunParams, active_attempt_id: str):

    probe_blocked = _probe_subagent_channel(lifecycle, options, active_attempt_id)
    if probe_blocked is not None:
        return probe_blocked

    # _build_prompt 只在确定要真跑(非 dry_run、未被 probe 拦)后构造一次。原先在函数开头
    # 无条件先调一次,非 dry_run 路径里那次结果会被这里覆盖、probe 也不用它,纯属重复构造
    # (白做一次 skill 检索 + prompt 拼装)。task1 自动审计发现,已核实首次调用结果在非
    # dry_run 路径下未被使用;dry_run 分支自带一次,语义不变。
    context, prompt = _build_prompt(lifecycle, options)
    _persist_runner_prompt_before_model(lifecycle.agent, options.run_id, prompt)
    return _run_and_finalize_subagent(
        lifecycle,
        SubagentModelTurnBundle(options, active_attempt_id, context, prompt),
    )


def _prepare_subagent_attempt(lifecycle, options: SubagentRunParams) -> str:
    active_attempt_id = str(options.attempt_id or "").strip()
    return lifecycle.prepare_attempt(
        options.run_id,
        dry_run=options.dry_run,
        active_attempt_id=active_attempt_id,
        retry_reason=options.retry_reason,
    )


def _build_prompt(lifecycle, options: SubagentRunParams):
    return lifecycle.build_prompt(options.run_id, options.max_cards, options.instruction)


def _probe_subagent_channel(lifecycle, options: SubagentRunParams, active_attempt_id: str):
    return lifecycle.probe_channel(
        SubagentProbeParams(
            options.run_id,
            active_attempt_id,
            options.max_cards,
            options.instruction,
            options.probe,
        )
    )


def _persist_runner_prompt_before_model(agent, run_id: str, prompt: str) -> None:
    task = agent.subagents.load(run_id)
    prompt_file = str(getattr(task, "runner_prompt_file", "") or "")
    if not prompt_file:
        return
    path = Path(prompt_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(prompt, encoding="utf-8")


# LLM: One attempt binds its exact child thread before entering runner-local context. Any
# transcript/Compact failure is recorded through the same lifecycle failure path as model errors.
# 函数用途: 在子代理运行上下文中执行模型轮，并把成功或失败交给统一生命周期收口。
def _run_and_finalize_subagent(lifecycle, bundle: SubagentModelTurnBundle):
    agent = lifecycle.agent
    options = bundle.options
    task_for_attrs = agent.subagents.load(options.run_id)
    task_attributes = runtime_task_attributes(task_for_attrs)
    try:
        thread = ensure_subagent_thread(agent.subagents, task_for_attrs)
        if thread is None:
            raise RuntimeError("subagent ConversationStore is unavailable")
    except Exception as exc:
        return lifecycle.handle_run_failure(
            SubagentRunFailureParams(
                options.run_id,
                bundle.active_attempt_id,
                exc,
                bundle.context,
                bundle.prompt,
            )
        )
    task_attributes.update(
        {
            # 会话运行时 会给每个 child 新建 thread，同时单独保留 parent_thread_id 谱系。
            # 本项目继承的 conversation_thread_id 继续拥有父 workspace/task link，
            # 只有 agent_thread_id 才拥有 child 自己的模型 transcript。
            AGENT_THREAD_ID_ATTR: thread.thread_id,
            CONVERSATION_REQUEST_ID_ATTR: bundle.active_attempt_id,
            CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True,
        }
    )
    previous_context = set_current_subagent_context(
        agent,
        run_id=options.run_id,
        attempt_id=bundle.active_attempt_id,
        task_attributes=task_attributes,
    )

    try:
        result = _run_subagent_model_turn(
            lifecycle,
            bundle.prompt,
            bundle.context,
            task_attributes,
            task=task_for_attrs,
            attempt_id=bundle.active_attempt_id,
        )
    except Exception as exc:
        return lifecycle.handle_run_failure(
            SubagentRunFailureParams(
                options.run_id,
                bundle.active_attempt_id,
                exc,
                bundle.context,
                bundle.prompt,
            )
        )
    finally:
        restore_current_subagent_context(agent, previous_context)

    return lifecycle.finalize_run(
        SubagentFinalizeParams(
            options.run_id,
            bundle.active_attempt_id,
            result,
            bundle.context,
            bundle.prompt,
        )
    )


# LLM: Goal persistence reuses the exact runner, permission scope and rejection list; fresh attempts allocate their own list.
# 函数用途: 子代理目标未完成时接着执行下一对话轮，普通 prompt-only 派工仍只执行原有模型循环。
def _run_subagent_model_turn(lifecycle, prompt, context, task_attributes, *, task, attempt_id):
    from ...conversation.goal_delegation import active_delegated_goal_after_turn
    from ...conversation.goal_prompting import continuation_prompt

    turn_number = 0
    runtime_rejected_actions: list[dict[str, str]] = []
    while True:
        turn_id = f"{attempt_id}-goal-{turn_number}" if turn_number else attempt_id
        result = _run_subagent_conversation_turn(
            lifecycle, AgentThreadTurnInput(prompt, attempt_id, turn_id), context, task_attributes, task=task,
            runtime_rejected_actions=runtime_rejected_actions,
        )
        goal = active_delegated_goal_after_turn(lifecycle.agent, task, result)
        if goal is None:
            return result
        turn_number += 1
        prompt = continuation_prompt(goal)


# LLM: 展示及已评估事实仅属于 AgentThreadTurnInput.turn_id；同 attempt 的下一 Goal turn 清零，回调只改本地参数，Compact/CAS/取消仍沿原合同。
# 函数用途: 使用child独立历史执行一轮，完整恢复请求与摘要同次提交和发送，同轮压缩不重复推荐。
def _run_subagent_conversation_turn(
    lifecycle,
    turn: AgentThreadTurnInput,
    context,
    task_attributes: dict[str, object],
    *,
    task: object,
    runtime_rejected_actions: list[dict[str, str]],
):
    agent = lifecycle.agent
    prompt, attempt_id, conversation_turn_id = turn.prompt, turn.attempt_id, turn.turn_id
    carried_archive_tool_calls: list[dict[str, object]] = []
    carried_active_turn_user_inputs: list[dict[str, object]] = []
    run_params = None
    thread = ensure_subagent_thread(getattr(agent, "subagents", None), task)
    if thread is None:
        raise RuntimeError("subagent ConversationStore is unavailable")
    transcript_sink = _open_subagent_transcript_sink(agent, task, thread, conversation_turn_id)
    compact_progress = (
        transcript_sink.write_conversation_compact_progress
        if transcript_sink is not None
        else None
    )
    model_surface = _subagent_compact_model_surface(context)
    try:
        current = prepare_subagent_thread_turn(
            agent,
            task,
            turn=turn,
            progress_callback=compact_progress,
            interrupt_check=is_interrupted,
            model_surface=model_surface,
        )
        model_iteration = 0
        while True:
            model_iteration += 1
            if transcript_sink is not None:
                transcript_sink.begin_model_attempt(model_iteration)
            previous_params = run_params
            run_params = _subagent_model_run_params(
                context=context,
                prompt=prompt,
                attempt_id=attempt_id,
                task_attributes=task_attributes,
                iteration=SubagentModelIteration(
                    current=current,
                    carried_archive_tool_calls=carried_archive_tool_calls,
                    carried_active_turn_user_inputs=carried_active_turn_user_inputs,
                    transcript_sink=transcript_sink,
                    runtime_rejected_actions=runtime_rejected_actions,
                ),
            )
            run_params.partial_turn_callback = partial(
                _persist_subagent_partial_result, agent, task, turn,
            )
            _bind_subagent_presentation(run_params, previous_params, conversation_turn_id)
            result, current = _run_subagent_recovery_attempt(
                agent, run_params, current, task=task, turn=turn, progress_callback=compact_progress,
            )
            if str(getattr(result, "runtime_status", "") or "").strip().lower() != (
                "context_overflow"
            ):
                _complete_subagent_conversation_turn(agent, task, turn, result, transcript_sink)
                return result
            (
                carried_archive_tool_calls,
                carried_active_turn_user_inputs,
            ) = _next_subagent_overflow_carry(
                agent,
                run_params,
                result,
                carried_archive_tool_calls,
                carried_active_turn_user_inputs,
            )
            refreshed = _compact_subagent_overflowing_turn(
                agent,
                task,
                SubagentOverflowCompactRequest(
                    prompt=prompt,
                    attempt_id=attempt_id,
                    conversation_turn_id=conversation_turn_id,
                    task_attributes=task_attributes,
                    current=current,
                    carried_archive_tool_calls=carried_archive_tool_calls,
                    progress_callback=compact_progress,
                    model_surface=_pending_subagent_compact_model_surface(
                        context,
                        carried_archive_tool_calls,
                        run_params=run_params,
                    ),
                    run_params=run_params, defer_compact=True,
                ),
            )
            current = refreshed
    except Exception:
        if transcript_sink is not None:
            transcript_sink.fail()
        raise
    finally:
        _trim_subagent_transcript(agent, task, enabled=transcript_sink is not None)


# LLM: scope仅覆盖一次原agent.run，异常必清理；成功CAS后的host_state回到原循环，其余路径不创建恢复器。
# 函数用途: 用当前子代理完整准备执行一次模型尝试，并带回实际已提交的新历史。
def _run_subagent_recovery_attempt(agent, params, current, *, task, turn, progress_callback):
    from ...model_request_selection import model_request_selection_scope
    from .compact_recovery import prepare_subagent_compact_recovery

    recovery = None
    if current.compact_source is not None and current.compact_source.messages:
        recovery = prepare_subagent_compact_recovery(
            agent, current, task, turn, progress_callback=progress_callback, interrupt_check=is_interrupted,
        )
    with model_request_selection_scope(recovery) if recovery is not None else nullcontext():
        result = agent.run(turn.prompt, params=params)
    return result, recovery.host_state if recovery is not None and recovery.committed else current


# LLM: 只在宿主明确相同 turn_id 时读取上一实际 RunParams；不从 attempt、历史或 carrier.binding 推断同轮，不持久化。
# 函数用途: 给 child 当次运行绑定原展示回传；无上次参数就未评估，失效 None 则保持已评估，避免压缩后重发推荐。
def _bind_subagent_presentation(params: RunParams, previous: RunParams | None, turn_id: str) -> None:
    same_turn = bool(turn_id) and previous is not None and previous.capability_presentation_turn_id == turn_id
    params.capability_presentation = previous.capability_presentation if same_turn else None
    params.capability_presentation_evaluated = previous.capability_presentation_evaluated if same_turn else False
    params.capability_presentation_turn_id = turn_id

    # LLM: 回调仅修改本轮原参数；清选择不清已评估事实，不改变身份、授权、工具记录或取消合同。
    # 函数用途: 让后续 Compact 和重试读取本次模型或压缩复核后的当前展示。
    def retain_presentation(value) -> None:
        params.capability_presentation = value
        params.capability_presentation_evaluated = True

    params.capability_presentation_callback = retain_presentation


# LLM: 异常与正常结果共用 child 的 canonical 出口；不能用父 thread 或 attempt 代替本次独立 turn。
# 函数用途: 在子代理异常退出前幂等保留原生历史，不投递成功正文或启动下一轮。
def _persist_subagent_partial_result(agent, task, turn, result) -> None:
    append_subagent_thread_result(agent, task, attempt_id=turn.attempt_id, turn_id=turn.turn_id, result=result)


# LLM: Normalize typed Goal stop before canonical history/display publication; completing a logical turn does not settle its runner.
# 函数用途: 保存子代理某一对话轮的完整回复并同步界面，下一 Goal 轮使用新轮次编号，不覆盖本轮。
def _complete_subagent_conversation_turn(agent, task, turn, result, transcript_sink):
    from ...conversation.goal_delegation import active_delegated_goal_after_turn

    active_delegated_goal_after_turn(agent, task, result)
    append_subagent_thread_result(agent, task, attempt_id=turn.attempt_id, result=result, turn_id=turn.turn_id)
    if transcript_sink is not None:
        transcript_sink.finish(final_text=str(getattr(result, "response", "") or ""), publish_final=True,
                               end_reason=result_turn_end_reason(result))


# LLM: Overflow retries may carry only typed tool progress and active-turn input records. These
# values prevent replay but never authorize a retry without a committed Compact generation.
# 函数用途: 合并子代理超限轮次的工具与插话事实，过滤已消费输入后交给正式 Compact 续接。
def _next_subagent_overflow_carry(
    agent,
    run_params: RunParams,
    result: object,
    carried_archive_tool_calls: list[dict[str, object]],
    carried_active_turn_user_inputs: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    released_input_ids = release_active_turn_inputs_for_compact(agent, run_params)
    result_archive = [
        dict(item)
        for item in list(getattr(result, "archive_tool_calls", None) or [])
        if isinstance(item, dict)
    ]
    next_archive = result_archive or carried_archive_tool_calls
    next_inputs = exclude_active_turn_user_input_ids(
        merge_active_turn_user_inputs(
            carried_active_turn_user_inputs,
            getattr(result, "active_turn_user_inputs", None),
        ),
        released_input_ids,
    )
    return next_archive, next_inputs


# LLM: A child provider-overflow retry must advance the same ConversationThread generation before
# rebuilding RunParams. Transcript Compact gets first claim; if the unfinished turn is the pressure
# source, the canonical active-turn archive checkpoint/CAS is the only valid fallback. Never treat a
# freshly rendered handoff or newly archived tool record as an uncounted Compact.
# 完整恢复模式只加载transcript来源，提交延迟到真实请求select；无来源仍先走原active-turn提交。
# 函数用途: 子代理上下文溢出时先压已结束历史，再正式压当前工具轨迹；成功推进代次后才继续原尝试。
def _compact_subagent_overflowing_turn(
    agent: object,
    task: object,
    request: SubagentOverflowCompactRequest,
) -> object:
    from .model_selection import canonical_subagent_model_scope

    with canonical_subagent_model_scope(agent, task.id):
        refreshed = prepare_subagent_thread_turn(
            agent,
            task,
            turn=AgentThreadTurnInput(request.prompt, request.attempt_id, request.conversation_turn_id),
            force=not request.defer_compact, defer_compact=request.defer_compact,
            progress_callback=request.progress_callback,
            interrupt_check=is_interrupted,
            model_surface=request.model_surface,
        )
        if request.defer_compact and refreshed.compact_source is not None and refreshed.compact_source.messages:
            return refreshed
        if refreshed.compact_generation > request.current.compact_generation:
            return refreshed
        return _compact_subagent_active_turn_archive(
            agent,
            task,
            request,
            refreshed,
        )


# LLM: active-turn archive 仍须原 child/store/CAS；二次加载只读回调更新后的展示，缺原 RunParams 时保留旧调用合同而不伪造已评估值。
# 函数用途: 先提交活动工具轨迹再加载新历史；defer模式不在二次加载时重做摘要，已清除推荐不复活。
def _compact_subagent_active_turn_archive(
    agent: object,
    task: object,
    request: SubagentOverflowCompactRequest,
    refreshed: object,
) -> object:
    store, latest = _load_subagent_compact_thread(agent, refreshed)

    from ...conversation.active_turn_compact import (
        ActiveTurnArchiveCompactRequest,
        compact_carried_active_turn_archive,
    )

    compacted = compact_carried_active_turn_archive(
        agent,
        store,
        latest,
        request.carried_archive_tool_calls,
        ActiveTurnArchiveCompactRequest(
            task_attributes=request.task_attributes,
            request_id=str(request.conversation_turn_id or request.attempt_id or ""),
            attempt_id=str(request.attempt_id or ""),
            task_prompt=request.prompt,
            progress_callback=request.progress_callback,
            interrupt_check=is_interrupted,
        ),
    )
    if not compacted.compacted:
        raise RuntimeError("subagent thread cannot compact the overflowing active turn")
    model_surface = request.model_surface
    if request.run_params is not None:
        model_surface = replace(model_surface, capability_presentation=request.run_params.capability_presentation)
    refreshed = prepare_subagent_thread_turn(
        agent,
        task,
        turn=AgentThreadTurnInput(request.prompt, request.attempt_id, request.conversation_turn_id),
        progress_callback=request.progress_callback,
        interrupt_check=is_interrupted,
        model_surface=model_surface, defer_compact=request.defer_compact,
    )
    if refreshed.compact_generation <= request.current.compact_generation:
        raise RuntimeError("subagent Compact generation did not advance")
    return refreshed


# LLM: Store/thread loading is fail-closed because a child may never compact against its parent or
# a guessed fallback thread. The returned pair has already passed exact readable authority checks.
# 函数用途: 读取子代理当前唯一会话线程；存储缺失或记录损坏时直接停止本次恢复。
def _load_subagent_compact_thread(agent: object, refreshed: object) -> tuple[object, object]:
    store = getattr(agent, "conversation_store", None)
    latest, load_error = (
        store.threads.load_report(refreshed.thread_id)
        if store is not None and refreshed.thread_id
        else (None, {"code": "CONVERSATION_STORE_UNAVAILABLE"})
    )
    if load_error is not None or latest is None:
        raise RuntimeError("subagent conversation thread is unavailable during Compact")
    return store, latest


# LLM: Public display setup is explicitly best effort and must remain separate from
# child authority. Keep the stable request identity tied to the exact run/attempt.
# 函数用途: 为本次子代理尝试建立公开过程流；展示存储不可用时安静降级，不中断真实任务。
def _open_subagent_transcript_sink(agent, task: object, current, attempt_id: str):
    from ...conversation.agent_transcript import (
        append_agent_transcript_event,
        begin_agent_transcript_turn,
    )
    from ...conversation.background_transcript import BackgroundTranscriptSink

    run_id = str(getattr(task, "id", "") or "")
    try:
        return BackgroundTranscriptSink(
            agent,
            thread_id=current.thread_id,
            task_id=run_id,
            request_id=begin_agent_transcript_turn(
                agent,
                run_id=run_id,
                attempt_id=attempt_id,
            ),
            event_writer=append_agent_transcript_event,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


# LLM: Every compact generation reuses this exact task-local RunParams contract;
# display callbacks are observers and cannot alter ids, permissions, or carry state. Rejections
# share the outer attempt's list; approvals are never promoted into this carrier.
# 函数用途: 组装一次子代理模型调用参数，让初轮和 Compact 后续轮保持同一身份与权限。
def _subagent_model_run_params(
    *,
    context,
    prompt: str,
    attempt_id: str,
    task_attributes: dict[str, object],
    iteration: SubagentModelIteration,
) -> RunParams:
    from ..runner.prompts import subagent_runner_system_prompt

    return RunParams(
        inject=[iteration.current.injection] if iteration.current.injection else [],
        save=True,
        allowed_tools=context.allowed_tools,
        write_boundary=context.write_boundary,
        request_id=attempt_id,
        attempt_id=attempt_id,
        run_id=context.run_id,
        task_id=context.root_id or context.run_id,
        task_attributes=task_attributes,
        system_prompt_override=subagent_runner_system_prompt(context),
        source="subagent_run_model_turn",
        resume_context=False,
        context_scope="task_local",
        root_user_prompt=prompt,
        carried_archive_tool_calls=list(iteration.carried_archive_tool_calls),
        carried_active_turn_user_inputs=list(iteration.carried_active_turn_user_inputs),
        runtime_rejected_actions=iteration.runtime_rejected_actions,
        conversation_history_seed=iteration.current.history_seed,
        on_chunk=iteration.transcript_sink,
    )


# LLM: 身份及当前输入只复制原 RunParams，不取 carrier.binding；没有实际参数的预运行准备不回传已评估，当前授权和原快照仍唯一。
# 函数用途: 为 child/grandchild 压缩构造与实际模型轮一致的缓存面，同步携带本轮展示的原清除回调。
def _subagent_compact_model_surface(
    context: object,
    *,
    loaded_tool_names: object = (),
    run_params: RunParams | None = None,
) -> object:
    from ...conversation.compact_provider_surface import ConversationCompactModelSurface
    from ..runner.prompts import subagent_runner_system_prompt

    allowed = getattr(context, "allowed_tools", None)
    return ConversationCompactModelSurface(
        allowed_tools=tuple(allowed) if allowed is not None else None,
        system_prompt_override=subagent_runner_system_prompt(context),
        context_scope="task_local",
        loaded_tool_names=tuple(
            sorted(
                {
                    str(item).strip()
                    for item in (loaded_tool_names or ())
                    if str(item).strip()
                }
            )
        ),
        presentation_context=(RuntimeContextRequest(
            user_prompt=run_params.root_user_prompt, inject=[], resume_context=False,
            context_scope=run_params.context_scope, allowed_tools=deepcopy(run_params.allowed_tools),
            write_boundary=deepcopy(run_params.write_boundary), request_id=run_params.request_id,
            run_id=run_params.run_id, task_id=run_params.task_id,
            task_attributes=deepcopy(run_params.task_attributes), source="conversation_compact_summary", save=False,
        ) if run_params is not None else None),
        capability_presentation=run_params.capability_presentation if run_params is not None else None,
        capability_presentation_turn_id=run_params.capability_presentation_turn_id if run_params is not None else "",
        capability_presentation_callback=run_params.capability_presentation_callback if run_params is not None else None,
    )


# LLM: 工具发现只经原 typed archive reducer 恢复；可选展示只读当前 RunParams，不从归档正文推测选择或执行权。
# 函数用途: 恢复本轮一次性 schema，并把原宿主展示交给 transcript Compact 复核。
def _pending_subagent_compact_model_surface(
    context: object,
    records: list[dict[str, object]],
    *,
    run_params: RunParams | None = None,
) -> object:
    from ...tooling.tool_search_state import pending_carried_loaded_tool_names

    return _subagent_compact_model_surface(
        context,
        loaded_tool_names=pending_carried_loaded_tool_names(records),
        run_params=run_params,
    )


# LLM: Trimming applies only to the lossy public event projection and is best
# effort. Never translate a trim failure into a child lifecycle failure.
# 函数用途: 子代理一轮结束后收缩公开展示文件，失败时不影响任务收口。
def _trim_subagent_transcript(agent, task: object, *, enabled: bool) -> None:
    if not enabled:
        return
    from ...conversation.agent_transcript import compact_agent_transcript_events

    try:
        compact_agent_transcript_events(
            agent,
            run_id=str(getattr(task, "id", "") or ""),
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        pass
