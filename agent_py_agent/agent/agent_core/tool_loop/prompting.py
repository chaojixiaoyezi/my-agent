
from __future__ import annotations

from ...prompting_parts.builder import ToolSections
from .._runtime_params import ToolLoopExecuteParams
from ..delivery_contract_prompting import render_delivery_contract_section
from ..runtime.guidance import inject_pending_guidance
from ..tool_context.window import window_tool_context_params
from ..tool_model_generation import ModelGenerateParams, generate_model_response


def build_tool_loop_prompt(agent, params: ToolLoopExecuteParams) -> str:
    inject_pending_guidance(agent, params)
    window_tool_context_params(agent, params)
    return agent.prompts.build(
        params.user_prompt,
        params.memories,
        inject=_runtime_injections_with_delivery_contract(params),
        prompt_files=params.prompt_files,
        system_prompt_override=params.system_prompt_override,
        context_scope=params.context_scope,
        tools=ToolSections(
            tool_catalog_section=params.tool_catalog_section,
            tool_recommendations_section=params.tool_recommendations_section,
            tool_context=params.tool_context,
        ),
    )


def _runtime_injections_with_delivery_contract(params: ToolLoopExecuteParams) -> list:
    if not isinstance(params.delivery_contract, dict):
        return params.runtime_injections
    return [*params.runtime_injections, render_delivery_contract_section(params.delivery_contract)]


def next_tool_loop_model_response(agent, params: ToolLoopExecuteParams, tool_rounds: int):
    prompt = build_tool_loop_prompt(agent, params)
    response = generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=params,
            prompt=prompt,
            tool_rounds=tool_rounds,
        )
    )
    return prompt, response
