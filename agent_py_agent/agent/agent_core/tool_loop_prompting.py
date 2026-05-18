# LLM: Tool-loop prompt/model helpers stay outside ToolLoopService to keep the loop thin.
# 模块用途: 构建工具循环提示词，并执行下一轮模型调用。

from __future__ import annotations

from ..prompting_parts.builder import ToolSections
from ._runtime_params import ToolLoopExecuteParams
from .tool_context_window import window_tool_context_params
from .tool_model_generation import ModelGenerateParams, generate_model_response


# LLM: build_tool_loop_prompt applies context-window trimming before rendering the prompt.
# 函数用途: 构建工具循环下一轮 prompt，包含工具目录、推荐工具和工具上下文。
def build_tool_loop_prompt(agent, params: ToolLoopExecuteParams) -> str:
    window_tool_context_params(params)
    return agent.prompts.build(
        params.user_prompt,
        params.memories,
        inject=params.runtime_injections,
        prompt_files=params.prompt_files,
        system_prompt_override=params.system_prompt_override,
        context_scope=params.context_scope,
        tools=ToolSections(
            tool_catalog_section=params.tool_catalog_section,
            tool_recommendations_section=params.tool_recommendations_section,
            tool_context=params.tool_context,
        ),
    )


# LLM: next_tool_loop_model_response returns both rendered prompt and provider response.
# 函数用途: 调用模型生成下一轮工具循环响应。
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
