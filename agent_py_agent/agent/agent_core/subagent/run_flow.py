
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ...conversation.active_turn_input import (
    exclude_active_turn_user_input_ids,
    merge_active_turn_user_inputs,
)
from ...conversation.agent_thread import (
    append_subagent_thread_result,
    ensure_subagent_thread,
    prepare_subagent_thread_turn,
)
from ...conversation.authority import (
    AGENT_THREAD_ID_ATTR,
    CONVERSATION_REQUEST_ID_ATTR,
    CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR,
)
from ...subagents.context_bundle_refs import runtime_task_attributes
from ..runner.context import restore_current_subagent_context, set_current_subagent_context
from ..runtime.loop_models import RunParams
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
# transcript/carry state; authority ids and permissions remain explicit arguments.
# 类用途: 收拢一次子代理模型调用会变化的 Compact 续接材料，避免长参数列表和字段错位。
@dataclass(frozen=True)
class SubagentModelIteration:
    current: object
    carried_archive_tool_calls: list[dict[str, object]]
    carried_active_turn_user_inputs: list[dict[str, object]]
    transcript_sink: object


def run_subagent_flow(lifecycle, options: SubagentRunParams):
    """Run one subagent task from prompt construction through result persistence."""
    active_attempt_id = _prepare_subagent_attempt(lifecycle, options)
    if options.dry_run:
        _, prompt = _build_prompt(lifecycle, options)
        return lifecycle.record_dry_run(options.run_id, active_attempt_id, prompt)

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


# LLM: The delegated runner keeps task_local permission/memory isolation while the structured
# authoritative-thread flag delegates all durable Compact to ConversationStore. Overflow retries
# carry typed tool/input progress so completed work is not replayed.
# 函数用途: 使用子代理自己的历史执行一轮模型任务；超限时压缩旧轮次并在同一尝试内继续。
def _run_subagent_model_turn(
    lifecycle,
    prompt: str,
    context,
    task_attributes: dict[str, object],
    *,
    task: object,
    attempt_id: str,
):
    agent = lifecycle.agent
    carried_archive_tool_calls: list[dict[str, object]] = []
    carried_active_turn_user_inputs: list[dict[str, object]] = []
    thread = ensure_subagent_thread(getattr(agent, "subagents", None), task)
    if thread is None:
        raise RuntimeError("subagent ConversationStore is unavailable")
    transcript_sink = _open_subagent_transcript_sink(agent, task, thread, attempt_id)
    compact_progress = (
        transcript_sink.write_conversation_compact_progress
        if transcript_sink is not None
        else None
    )
    try:
        current = prepare_subagent_thread_turn(
            agent,
            task,
            prompt=prompt,
            attempt_id=attempt_id,
            progress_callback=compact_progress,
        )
        for _attempt in range(8):
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
                ),
            )
            result = agent.run(prompt, params=run_params)
            if str(getattr(result, "runtime_status", "") or "").strip().lower() != (
                "context_overflow"
            ):
                append_subagent_thread_result(
                    agent,
                    task,
                    attempt_id=attempt_id,
                    result=result,
                )
                if transcript_sink is not None:
                    transcript_sink.finish()
                return result
            (
                carried_archive_tool_calls,
                carried_active_turn_user_inputs,
                made_progress,
            ) = _next_subagent_overflow_carry(
                agent,
                run_params,
                result,
                carried_archive_tool_calls,
                carried_active_turn_user_inputs,
            )
            refreshed = prepare_subagent_thread_turn(
                agent,
                task,
                prompt=prompt,
                attempt_id=attempt_id,
                force=True,
                progress_callback=compact_progress,
            )
            if (
                refreshed.compact_generation <= current.compact_generation
                and not made_progress
            ):
                raise RuntimeError("subagent thread cannot compact the overflowing context")
            current = refreshed
        raise RuntimeError("subagent thread still exceeds the model context after Compact")
    except Exception:
        if transcript_sink is not None:
            transcript_sink.fail()
        raise
    finally:
        _trim_subagent_transcript(agent, task, enabled=transcript_sink is not None)


# LLM: Overflow retries may carry only typed tool progress and active-turn input records. Keep
# release filtering here so a Compact retry cannot replay completed tools or consumed guidance.
# 函数用途: 合并一次子代理超限轮次的结构化进度，并报告下一轮是否还有可继续的新增事实。
def _next_subagent_overflow_carry(
    agent,
    run_params: RunParams,
    result: object,
    carried_archive_tool_calls: list[dict[str, object]],
    carried_active_turn_user_inputs: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]], bool]:
    released_input_ids = release_active_turn_inputs_for_compact(agent, run_params)
    result_archive = [
        dict(item)
        for item in list(getattr(result, "archive_tool_calls", None) or [])
        if isinstance(item, dict)
    ]
    from ..runtime_mixin import _result_added_tool_progress

    made_tool_progress = _result_added_tool_progress(
        run_params,
        result,
        result_archive,
    )
    next_archive = result_archive or carried_archive_tool_calls
    next_inputs = exclude_active_turn_user_input_ids(
        merge_active_turn_user_inputs(
            carried_active_turn_user_inputs,
            getattr(result, "active_turn_user_inputs", None),
        ),
        released_input_ids,
    )
    made_guidance_progress = next_inputs != carried_active_turn_user_inputs
    return next_archive, next_inputs, made_tool_progress or made_guidance_progress


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
# display callbacks are observers and cannot alter ids, permissions, or carry state.
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
        conversation_history_seed=iteration.current.history_seed,
        on_chunk=iteration.transcript_sink,
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
