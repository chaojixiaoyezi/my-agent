
from __future__ import annotations

"""Task-local auto-continuation for subagent runner model turns."""

from dataclasses import dataclass

from ...subagents import parse_subagent_runner_output
from ...subagents.context_bundle_refs import runtime_task_attributes
from ...subagents.services.subagent_session_compact import (
    SubagentSessionCompactRequest,
    subagent_session_compact_payload_from_result,
    write_subagent_session_compact,
)
from ..runner.prompts import subagent_runner_system_prompt

_SUMMARY_PREVIEW_CHARS = 800

# fix#2(长期助手 外部完成信号):判读子代理 agent.run 结束时,若自己那路 spool 还有未逐条判读的
#   候选,就【立即原地重跑续判】(无重派空档),直到队列判空(backlog<=0/watch close)才收尾。
#   进展守卫看【已判(acked)数在不在涨】(入流可能比判快、积压照涨但没卡死):连续
#   _WATCH_STALL_CAP 轮已判数没涨=判读卡死/模型拒判 → 停,交补岗兜底。硬深度上限兜底防失控。
_WATCH_STALL_CAP = 3
_WATCH_CONTINUE_DEPTH_CAP = 60


@dataclass
class _WatchBacklogContinue:
    last_judged: int = -1
    stall: int = 0
    backlog: int = 0


@dataclass(frozen=True)
class SubagentSessionContinuationResult:
    result: object
    bundle: object
    continuation_depth: int = 0


def continue_subagent_session_if_needed(
    agent,
    bundle: object,
    first_result: object,
) -> SubagentSessionContinuationResult:
    result = first_result
    active_bundle = bundle
    depth = 0
    executed_tools = _result_executed_tools(first_result)
    watch = _WatchBacklogContinue()
    while True:
        if _should_continue(result):
            depth += 1
            _write_intermediate_package(agent, active_bundle, result, depth)
            active_bundle = _rebuild_bundle(agent, active_bundle, depth)
        elif _watch_backlog_wants_continue(agent, active_bundle, watch, depth):
            depth += 1
            active_bundle = _rebuild_watch_backlog_bundle(agent, active_bundle, watch.backlog)
        else:
            break
        result = _run_model_turn(agent, active_bundle.prompt, active_bundle.context)
        executed_tools = _merge_strings(executed_tools, _result_executed_tools(result))
    _attach_continued_tool_facts(result, executed_tools)
    return SubagentSessionContinuationResult(result, active_bundle, depth)


def _watch_backlog_wants_continue(agent, bundle: object, watch: _WatchBacklogContinue, depth: int) -> bool:
    """判读子代理还有未判积压且判读在推进 → 续判(True);判空/卡死/超硬上限 → 停(False)。
    非判读子代理没 watch 路 → backlog=0 自然 False。全结构化计数,失败保守 False(不续,走原收尾)。"""
    if depth >= _WATCH_CONTINUE_DEPTH_CAP:
        return False
    run_id = str(getattr(getattr(bundle, "options", None), "run_id", "") or "").strip()
    if not run_id:
        return False
    try:
        from ...ingestion.wake_backstop import run_judged_watch_count, run_unjudged_watch_backlog

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


def _rebuild_watch_backlog_bundle(agent, bundle: object, backlog: int) -> object:
    context, prompt = agent._build_subagent_prompt(
        bundle.options.run_id,
        bundle.options.max_cards,
        _watch_backlog_instruction(bundle.options.instruction, backlog),
    )
    return bundle.__class__(bundle.options, bundle.active_attempt_id, context, prompt)


def _watch_backlog_instruction(existing: str, backlog: int) -> str:
    lines = [
        str(existing or "").strip(),
        f"【/audit 未判完·继续】你负责的盯守 spool 里还有 {backlog} 条已抬升、未逐条判读的候选。"
        "/audit 契约=一条不漏、判完才算完,现在【不能收尾】。继续 watch_stream(action=pull)拉这批,"
        "命中的先 record_finding、再用 action=verdict 逐条交结论销账,直到待判归零或 watch 被 close 再交结果。",
    ]
    return "\n".join(line for line in lines if line).strip()


def _should_continue(result: object) -> bool:
    if not subagent_session_compact_payload_from_result(result):
        return False
    parsed = parse_subagent_runner_output(str(getattr(result, "response", "") or ""))
    return not parsed.found


def _write_intermediate_package(agent, bundle: SubagentModelTurnBundle, result: object, depth: int) -> None:
    task = agent.subagents.load(bundle.options.run_id)
    output_payload = _intermediate_output_payload(result, depth)
    task.current_step = str(output_payload["next_action"])
    task.latest_summary = str(output_payload["summary"])
    write_subagent_session_compact(
        SubagentSessionCompactRequest(
            task,
            subagent_session_compact_payload_from_result(result),
            output_payload,
        )
    )
    agent.subagents.save(task)


def _intermediate_output_payload(result: object, depth: int) -> dict[str, object]:
    return {
        "summary": _response_preview(result),
        "next_action": f"continue subagent task after local compact cycle {depth}",
        "next_actions": [f"continue from task-local session compact package cycle {depth}"],
        "blockers": [],
    }


def _rebuild_bundle(agent, bundle: object, depth: int) -> object:
    context, prompt = agent._build_subagent_prompt(
        bundle.options.run_id,
        bundle.options.max_cards,
        _continuation_instruction(bundle.options.instruction, depth),
    )
    return bundle.__class__(bundle.options, bundle.active_attempt_id, context, prompt)


def _run_model_turn(agent, prompt: str, context):
    task = agent.subagents.load(context.run_id)
    return agent.run(
        prompt,
        save=False,
        allowed_tools=context.allowed_tools,
        write_boundary=context.write_boundary,
        run_id=context.run_id,
        task_id=context.root_id or context.run_id,
        task_attributes=runtime_task_attributes(task),
        system_prompt_override=subagent_runner_system_prompt(context),
        source="subagent_run_model_turn",
        context_scope="task_local",
    )


def _attach_continued_tool_facts(result: object, executed_tools: list[str]) -> None:
    if not executed_tools:
        return
    try:
        result.executed_tools = _merge_strings(_result_executed_tools(result), executed_tools)
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


def _continuation_instruction(existing: str, depth: int) -> str:
    lines = [
        str(existing or "").strip(),
        f"Local session compact cycle {depth}: continue the same subagent run from Task-Local Compact Continuation.",
        "Do not restart the task; read latest_continue_packet/checkpoint/summary refs only as needed.",
    ]
    return "\n".join(line for line in lines if line).strip()


def _response_preview(result: object) -> str:
    text = str(getattr(result, "response", "") or "").strip()
    if len(text) <= _SUMMARY_PREVIEW_CHARS:
        return text
    return text[:_SUMMARY_PREVIEW_CHARS].rstrip() + "...<truncated>"


__all__ = ["SubagentSessionContinuationResult", "continue_subagent_session_if_needed"]
