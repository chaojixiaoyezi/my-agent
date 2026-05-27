# LLM: Tool-limit closeout isolates max-round finalization from the main tool-loop service.
# 模块用途: 达到工具轮数上限时生成确定性或模型最终回复。

from __future__ import annotations

from ._runtime_params import ToolLoopExecuteParams
from .tool_loop_orchestration_scope import executed_subagent_orchestration
from .tool_loop_prompting import build_tool_loop_prompt
from .tool_loop_recovery import without_tool_call_after_limit
from .tool_model_generation import ModelGenerateParams, generate_model_response


# LLM: final_response_after_tool_limit is the single path for max-tool-round closeout.
# 函数用途: 工具轮数到顶时追加系统提示，子代理调度场景优先确定性收口，否则调用模型总结。
def final_response_after_tool_limit(agent, params: ToolLoopExecuteParams, tool_rounds: int):
    params.tool_context.append("[tool-system]\n已达到最大工具轮数限制，停止继续调用工具。")
    if executed_subagent_orchestration(params):
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
