from __future__ import annotations

"""Business-level continuation for audit watch backlogs.

This is intentionally unrelated to context compaction.  Every model turn here
still runs through the shared runtime Compact chain.
"""

from dataclasses import dataclass

from ...subagents.context_bundle_refs import runtime_task_attributes
from ..runner.prompts import subagent_runner_system_prompt

_WATCH_STALL_CAP = 3
_WATCH_CONTINUE_DEPTH_CAP = 60


@dataclass
class _WatchBacklogContinue:
    last_judged: int = -1
    stall: int = 0
    backlog: int = 0


@dataclass(frozen=True)
class SubagentWatchContinuationResult:
    result: object
    bundle: object
    continuation_depth: int = 0


def continue_subagent_watch_if_needed(
    agent: object,
    bundle: object,
    first_result: object,
) -> SubagentWatchContinuationResult:
    result = first_result
    active_bundle = bundle
    depth = 0
    executed_tools = _result_executed_tools(first_result)
    watch = _WatchBacklogContinue()
    while _watch_backlog_wants_continue(agent, active_bundle, watch, depth):
        depth += 1
        active_bundle = _rebuild_watch_backlog_bundle(
            agent,
            active_bundle,
            watch.backlog,
        )
        result = _run_model_turn(agent, active_bundle.prompt, active_bundle.context)
        executed_tools = _merge_strings(executed_tools, _result_executed_tools(result))
    _attach_continued_tool_facts(result, executed_tools)
    return SubagentWatchContinuationResult(result, active_bundle, depth)


def _watch_backlog_wants_continue(
    agent: object,
    bundle: object,
    watch: _WatchBacklogContinue,
    depth: int,
) -> bool:
    if depth >= _WATCH_CONTINUE_DEPTH_CAP:
        return False
    run_id = str(getattr(getattr(bundle, "options", None), "run_id", "") or "").strip()
    if not run_id:
        return False
    try:
        from ...ingestion.wake_backstop import (
            run_judged_watch_count,
            run_unjudged_watch_backlog,
        )

        backlog = run_unjudged_watch_backlog(agent, run_id)
    except Exception:
        return False
    watch.backlog = backlog
    if backlog <= 0:
        return False
    try:
        judged = run_judged_watch_count(agent, run_id)
    except Exception:
        judged = watch.last_judged
    if watch.last_judged < 0 or judged > watch.last_judged:
        watch.stall = 0
    else:
        watch.stall += 1
    watch.last_judged = judged
    return watch.stall < _WATCH_STALL_CAP


def _rebuild_watch_backlog_bundle(
    agent: object,
    bundle: object,
    backlog: int,
) -> object:
    context, prompt = agent._build_subagent_prompt(
        bundle.options.run_id,
        bundle.options.max_cards,
        _watch_backlog_instruction(bundle.options.instruction, backlog),
    )
    return bundle.__class__(
        bundle.options,
        bundle.active_attempt_id,
        context,
        prompt,
    )


def _watch_backlog_instruction(existing: str, backlog: int) -> str:
    lines = [
        str(existing or "").strip(),
        f"【/audit 未判完·继续】你负责的盯守 spool 里还有 {backlog} 条已抬升、未逐条判读的候选。"
        "/audit 契约=一条不漏、判完才算完,现在【不能收尾】。继续 watch_stream(action=pull)拉这批,"
        "命中的先 record_finding、再用 action=verdict 逐条交结论销账,直到待判归零或 watch 被 close 再交结果。",
    ]
    return "\n".join(line for line in lines if line).strip()


def _run_model_turn(agent: object, prompt: str, context: object):
    task = agent.subagents.load(context.run_id)
    return agent.run(
        prompt,
        save=True,
        allowed_tools=context.allowed_tools,
        write_boundary=context.write_boundary,
        run_id=context.run_id,
        task_id=context.root_id or context.run_id,
        task_attributes=runtime_task_attributes(task),
        system_prompt_override=subagent_runner_system_prompt(context),
        source="subagent_run_model_turn",
        context_scope="task_local",
    )


def _attach_continued_tool_facts(
    result: object,
    executed_tools: list[str],
) -> None:
    if not executed_tools:
        return
    try:
        result.executed_tools = _merge_strings(
            _result_executed_tools(result),
            executed_tools,
        )
    except Exception:
        return


def _result_executed_tools(result: object) -> list[str]:
    return [
        str(item).strip()
        for item in (getattr(result, "executed_tools", None) or [])
        if str(item or "").strip()
    ]


def _merge_strings(first: list[str], second: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for item in [*first, *second]:
        if item in seen:
            continue
        seen.add(item)
        merged.append(item)
    return merged


__all__ = [
    "SubagentWatchContinuationResult",
    "continue_subagent_watch_if_needed",
]
