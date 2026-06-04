
from __future__ import annotations

"""Task-local auto-continuation for subagent runner model turns."""

from dataclasses import dataclass

from ...subagents import parse_subagent_runner_output
from ...subagents.services.subagent_session_compact import (
    SubagentSessionCompactRequest,
    write_subagent_session_compact,
)
from ..runner.identity_prompt import subagent_runner_system_prompt
from .session_compact_payload import subagent_session_compact_payload_from_result

_SUMMARY_PREVIEW_CHARS = 800


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
    while _should_continue(result):
        depth += 1
        _write_intermediate_package(agent, active_bundle, result, depth)
        active_bundle = _rebuild_bundle(agent, active_bundle, depth)
        result = _run_model_turn(agent, active_bundle.prompt, active_bundle.context)
        executed_tools = _merge_strings(executed_tools, _result_executed_tools(result))
    _attach_continued_tool_facts(result, executed_tools)
    return SubagentSessionContinuationResult(result, active_bundle, depth)


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
    return agent.run(
        prompt,
        save=False,
        allowed_tools=context.allowed_tools,
        write_boundary=context.write_boundary,
        run_id=context.run_id,
        task_id=context.root_id or context.run_id,
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
