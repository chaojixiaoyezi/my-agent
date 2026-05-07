from __future__ import annotations

from .subagent_params import (
    SubagentFinalizeParams,
    SubagentProbeParams,
    SubagentRunFailureParams,
    SubagentRunParams,
)


def run_subagent_flow(agent, options: SubagentRunParams):
    """Run one subagent task from prompt construction through result persistence."""
    active_attempt_id = str(options.attempt_id or "").strip()
    active_attempt_id = agent._prepare_subagent_attempt(
        options.run_id,
        dry_run=options.dry_run,
        active_attempt_id=active_attempt_id,
        retry_reason=options.retry_reason,
    )
    context, prompt = agent._build_subagent_prompt(
        options.run_id,
        options.max_cards,
        options.instruction,
    )
    if options.dry_run:
        return agent._record_subagent_dry_run(options.run_id, active_attempt_id, prompt)

    probe_blocked = agent._probe_subagent_channel(
        SubagentProbeParams(
            options.run_id,
            active_attempt_id,
            options.max_cards,
            options.instruction,
            options.probe,
        )
    )
    if probe_blocked is not None:
        return probe_blocked

    context, prompt = agent._build_subagent_prompt(
        options.run_id,
        options.max_cards,
        options.instruction,
    )
    task_for_attrs = agent.subagents.load(options.run_id)
    agent._current_task_attributes = task_for_attrs.attributes

    try:
        result = _run_subagent_model_turn(agent, prompt, context)
    except Exception as exc:
        return agent._handle_subagent_run_failure(
            SubagentRunFailureParams(options.run_id, active_attempt_id, exc, context, prompt)
        )

    return agent._finalize_subagent_run(
        SubagentFinalizeParams(options.run_id, active_attempt_id, result, context, prompt)
    )


def _run_subagent_model_turn(agent, prompt: str, context):
    # LLM: one model turn stays separate from attempt/probe bookkeeping.
    return agent.run(
        prompt,
        save=False,
        allowed_tools=context.allowed_tools,
        write_boundary=context.write_boundary,
        source="subagent_run_model_turn",
        recovery_snapshot=False,
    )
