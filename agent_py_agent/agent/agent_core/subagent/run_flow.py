
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
            "conversation_thread_id": thread.thread_id,
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
    from ..runner.prompts import subagent_runner_system_prompt

    agent = lifecycle.agent
    current = prepare_subagent_thread_turn(
        agent,
        task,
        prompt=prompt,
        attempt_id=attempt_id,
    )
    carried_archive_tool_calls: list[dict[str, object]] = []
    carried_active_turn_user_inputs: list[dict[str, object]] = []
    for _attempt in range(8):
        run_params = RunParams(
            inject=[current.injection] if current.injection else [],
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
            carried_archive_tool_calls=list(carried_archive_tool_calls),
            carried_active_turn_user_inputs=list(carried_active_turn_user_inputs),
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
            return result
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
        if result_archive:
            carried_archive_tool_calls = result_archive
        prior_active_turn_inputs = list(carried_active_turn_user_inputs)
        carried_active_turn_user_inputs = exclude_active_turn_user_input_ids(
            merge_active_turn_user_inputs(
                carried_active_turn_user_inputs,
                getattr(result, "active_turn_user_inputs", None),
            ),
            released_input_ids,
        )
        made_guidance_progress = (
            carried_active_turn_user_inputs != prior_active_turn_inputs
        )
        refreshed = prepare_subagent_thread_turn(
            agent,
            task,
            prompt=prompt,
            attempt_id=attempt_id,
            force=True,
        )
        if (
            refreshed.compact_generation <= current.compact_generation
            and not (made_tool_progress or made_guidance_progress)
        ):
            raise RuntimeError("subagent thread cannot compact the overflowing context")
        current = refreshed
    raise RuntimeError("subagent thread still exceeds the model context after Compact")
