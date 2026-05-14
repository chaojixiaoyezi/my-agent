# LLM: Subagent session continuation loops compacted save=False runners through task-local refs only.
# 模块用途: 当子代理模型回合触发 compact 但还没输出最终结果时，写本地包并继续同一个 run。

from __future__ import annotations

"""Task-local auto-continuation for subagent runner model turns."""

from dataclasses import dataclass

from ..subagent import parse_subagent_runner_output
from ..subagents.services.subagent_session_compact import (
    SubagentSessionCompactRequest,
    write_subagent_session_compact,
)
from .runner_identity_prompt import subagent_runner_system_prompt
from .subagent_session_compact_payload import subagent_session_compact_payload_from_result

_SUMMARY_PREVIEW_CHARS = 800


# LLM: SubagentSessionContinuationResult returns the latest model result and prompt bundle together.
# 类用途: 记录子代理本地 compact 自动续跑后的最终 AgentRunResult、最后 prompt 上下文和续跑次数。
@dataclass(frozen=True)
class SubagentSessionContinuationResult:
    result: object
    bundle: object
    continuation_depth: int = 0


# LLM: continue_subagent_session_if_needed is the controller for task-local subagent compact loops.
# 函数用途: 在未形成结构化最终结果且 compact 建议触发时，写本地包、重建 prompt 并继续同一 run。
def continue_subagent_session_if_needed(
    agent,
    bundle: object,
    first_result: object,
) -> SubagentSessionContinuationResult:
    result = first_result
    active_bundle = bundle
    max_depth = _max_depth(agent)
    depth = 0
    executed_tools = _result_executed_tools(first_result)
    while depth < max_depth and _should_continue(result):
        depth += 1
        _write_intermediate_package(agent, active_bundle, result, depth)
        active_bundle = _rebuild_bundle(agent, active_bundle, depth)
        result = _run_model_turn(agent, active_bundle.prompt, active_bundle.context)
        executed_tools = _merge_strings(executed_tools, _result_executed_tools(result))
    _attach_continued_tool_facts(result, executed_tools)
    return SubagentSessionContinuationResult(result, active_bundle, depth)


# LLM: _should_continue requires compact pressure and absence of a final SUBAGENT_RESULT.
# 函数用途: 只有模型尚未提交结构化结果时才自动续跑；BLOCKED/DONE 等结构化输出交给正常 finalizer。
def _should_continue(result: object) -> bool:
    if not subagent_session_compact_payload_from_result(result):
        return False
    parsed = parse_subagent_runner_output(str(getattr(result, "response", "") or ""))
    return not parsed.found


# LLM: _write_intermediate_package persists compact refs before the next runner prompt is rebuilt.
# 函数用途: 将中间模型回合的状态写进当前 run workspace，让下一轮 prompt 从 task-local refs 接续。
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


# LLM: _intermediate_output_payload keeps continuation facts small and reusable for packet generation.
# 函数用途: 从中间 response 生成 summary/next_action，不写完整正文，避免本地 compact 包膨胀。
def _intermediate_output_payload(result: object, depth: int) -> dict[str, object]:
    return {
        "summary": _response_preview(result),
        "next_action": f"continue subagent task after local compact cycle {depth}",
        "next_actions": [f"continue from task-local session compact package cycle {depth}"],
        "blockers": [],
    }


# LLM: _rebuild_bundle makes the next model turn read the freshly written task-local compact refs.
# 函数用途: 重新构建 execution context 和 runner prompt，保持 attempt/run_id 不变但 prompt 读取最新 compact 包。
def _rebuild_bundle(agent, bundle: object, depth: int) -> object:
    context, prompt = agent._build_subagent_prompt(
        bundle.options.run_id,
        bundle.options.max_cards,
        _continuation_instruction(bundle.options.instruction, depth),
    )
    return bundle.__class__(bundle.options, bundle.active_attempt_id, context, prompt)


# LLM: _run_model_turn mirrors the normal subagent runner call path without importing the flow module.
# 函数用途: 用同一个 agent.run/save=False/identity prompt 边界执行续跑轮，避免循环导入。
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
        recovery_snapshot=False,
        context_scope="task_local",
    )


# LLM: _attach_continued_tool_facts keeps actual tool evidence across task-local compact turns.
# 函数用途: 子代理本地 compact 后会重新进一次 agent.run；最终结果要携带续跑前真实执行过的工具。
def _attach_continued_tool_facts(result: object, executed_tools: list[str]) -> None:
    if not executed_tools:
        return
    try:
        result.executed_tools = _merge_strings(_result_executed_tools(result), executed_tools)
    except Exception:
        return


# LLM: _result_executed_tools reads AgentRunResult.executed_tools defensively.
# 函数用途: 从单段模型循环结果中取真实执行过的工具名；坏形态返回空列表。
def _result_executed_tools(result: object) -> list[str]:
    return [
        str(item).strip()
        for item in (getattr(result, "executed_tools", None) or [])
        if str(item or "").strip()
    ]


# LLM: _merge_strings preserves first-seen order while de-duplicating tool names.
# 函数用途: 合并 compact 续跑前后工具列表，避免重复工具名污染 runner 结果。
def _merge_strings(first: list[str], second: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for item in [*first, *second]:
        if item in seen:
            continue
        seen.add(item)
        merged.append(item)
    return merged


# LLM: _continuation_instruction keeps the model from restarting the original task after local compact.
# 函数用途: 给下一轮 runner 一个轻量指令，要求先读 Session Compact Package / Continue Packet 后继续。
def _continuation_instruction(existing: str, depth: int) -> str:
    lines = [
        str(existing or "").strip(),
        f"Local session compact cycle {depth}: continue the same subagent run from Task-Local Compact Continuation.",
        "Do not restart the task; read latest_continue_packet/checkpoint/summary refs only as needed.",
    ]
    return "\n".join(line for line in lines if line).strip()


# LLM: _max_depth reuses the compact continuation depth config for subagent-local compact loops.
# 函数用途: 控制单个子代理 run 内最多自动续跑几次，默认继承 memory_compact_auto_continue_max_depth。
def _max_depth(agent) -> int:
    try:
        return max(0, int(getattr(agent.config, "memory_compact_auto_continue_max_depth", 1) or 0))
    except (TypeError, ValueError):
        return 1


# LLM: _response_preview trims intermediate model text before storing it in task-local compact metadata.
# 函数用途: 为 latest_summary 提供短摘要；不把大模型回复完整写进恢复包。
def _response_preview(result: object) -> str:
    text = str(getattr(result, "response", "") or "").strip()
    if len(text) <= _SUMMARY_PREVIEW_CHARS:
        return text
    return text[:_SUMMARY_PREVIEW_CHARS].rstrip() + "...<truncated>"


__all__ = ["SubagentSessionContinuationResult", "continue_subagent_session_if_needed"]
