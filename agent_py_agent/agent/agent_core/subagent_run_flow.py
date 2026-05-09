# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

from dataclasses import dataclass

from .subagent_params import (
    SubagentFinalizeParams,
    SubagentProbeParams,
    SubagentRunFailureParams,
    SubagentRunParams,
)


# LLM: SubagentModelTurnBundle keeps runner finalization inputs together across helper calls.
# 类用途: 汇总一次 subagent 模型回合所需的 options、attempt、context 和 prompt，避免函数参数膨胀。
@dataclass(frozen=True)
class SubagentModelTurnBundle:
    options: SubagentRunParams
    active_attempt_id: str
    context: object
    prompt: str


# LLM: run_subagent_flow 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进子代理flow的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def run_subagent_flow(agent, options: SubagentRunParams):
    """Run one subagent task from prompt construction through result persistence."""
    active_attempt_id = _prepare_subagent_attempt(agent, options)
    context, prompt = _build_prompt(agent, options)
    if options.dry_run:
        return agent._record_subagent_dry_run(options.run_id, active_attempt_id, prompt)

    probe_blocked = _probe_subagent_channel(agent, options, active_attempt_id)
    if probe_blocked is not None:
        return probe_blocked

    context, prompt = _build_prompt(agent, options)
    return _run_and_finalize_subagent(
        agent,
        SubagentModelTurnBundle(options, active_attempt_id, context, prompt),
    )


# LLM: _prepare_subagent_attempt keeps attempt bookkeeping isolated from runner execution.
# 函数用途: 标记本次 runner 尝试，保存 dry-run、attempt id 和重试原因。
def _prepare_subagent_attempt(agent, options: SubagentRunParams) -> str:
    active_attempt_id = str(options.attempt_id or "").strip()
    return agent._prepare_subagent_attempt(
        options.run_id,
        dry_run=options.dry_run,
        active_attempt_id=active_attempt_id,
        retry_reason=options.retry_reason,
    )


# LLM: _build_prompt centralizes runner prompt construction arguments.
# 函数用途: 根据 run_id、max_cards 和额外 instruction 构建 subagent runner 上下文与 prompt。
def _build_prompt(agent, options: SubagentRunParams):
    return agent._build_subagent_prompt(options.run_id, options.max_cards, options.instruction)


# LLM: _probe_subagent_channel checks repair/blocked state before spending a model call.
# 函数用途: 运行子代理通道探测；如果需要阻断则直接返回记录结果。
def _probe_subagent_channel(agent, options: SubagentRunParams, active_attempt_id: str):
    return agent._probe_subagent_channel(
        SubagentProbeParams(
            options.run_id,
            active_attempt_id,
            options.max_cards,
            options.instruction,
            options.probe,
        )
    )


# LLM: _run_and_finalize_subagent scopes active runner context around the model turn.
# 函数用途: 设置当前 runner id，执行模型回合，失败则记录，成功则走统一 finalization。
def _run_and_finalize_subagent(agent, bundle: SubagentModelTurnBundle):
    options = bundle.options
    task_for_attrs = agent.subagents.load(options.run_id)
    previous_task_attributes = getattr(agent, "_current_task_attributes", None)
    previous_subagent_run_id = getattr(agent, "_current_subagent_run_id", "")
    agent._current_task_attributes = task_for_attrs.attributes
    agent._current_subagent_run_id = options.run_id

    try:
        result = _run_subagent_model_turn(agent, bundle.prompt, bundle.context)
    except Exception as exc:
        return agent._handle_subagent_run_failure(
            SubagentRunFailureParams(
                options.run_id,
                bundle.active_attempt_id,
                exc,
                bundle.context,
                bundle.prompt,
            )
        )
    finally:
        agent._current_task_attributes = previous_task_attributes
        agent._current_subagent_run_id = previous_subagent_run_id

    return agent._finalize_subagent_run(
        SubagentFinalizeParams(
            options.run_id,
            bundle.active_attempt_id,
            result,
            bundle.context,
            bundle.prompt,
        )
    )


# LLM: _run_subagent_model_turn 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进子代理模型turn的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _run_subagent_model_turn(agent, prompt: str, context):
    # LLM: 单次模型调用与尝试次数、通道探测记账分离，便于独立重试。
    return agent.run(
        prompt,
        save=False,
        allowed_tools=context.allowed_tools,
        write_boundary=context.write_boundary,
        source="subagent_run_model_turn",
        recovery_snapshot=False,
    )
