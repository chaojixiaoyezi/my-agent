
from __future__ import annotations

from ._runtime_params import ToolLoopExecuteParams
from .tool_loop.prompting import build_tool_loop_prompt
from .tool_loop.recovery import without_tool_call_after_limit
from .tool_model_generation import ModelGenerateParams, generate_model_response

_ORCHESTRATION_TOOLS = {
    "create_subagents",
    "dispatch_subagents",
    "schedule_child_subagents",
}


def final_response_after_tool_limit(agent, params: ToolLoopExecuteParams, tool_rounds: int):
    params.tool_context.append("[tool-system]\n已达到最大工具轮数限制，停止继续调用工具。")
    if _executed_subagent_orchestration(params):
        params.tool_context.append("[tool-system]\n子代理调度状态请通过 dispatch_subagents/tree 状态结果继续查看；系统不再替主代理生成最终结论。")
    final_prompt = build_tool_loop_prompt(agent, params)
    final_response = generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=params,
            prompt=final_prompt,
            tool_rounds=tool_rounds,
        )
    )
    return final_prompt, without_tool_call_after_limit(agent, final_response)


def _executed_subagent_orchestration(params: ToolLoopExecuteParams) -> bool:
    return any(str(item or "") in _ORCHESTRATION_TOOLS for item in params.executed_tools or [])
