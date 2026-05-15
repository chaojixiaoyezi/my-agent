# LLM: Tool-loop orchestration scope helpers stay separate from the main loop service.
# 模块用途: 判断本轮是否实际碰过子代理编排工具，供工具上限和空响应兜底选择收口方式。

from __future__ import annotations

from ._runtime_params import ToolLoopExecuteParams

_ORCHESTRATION_TOOLS = {
    "capability_config_patch",
    "create_subagents",
    "dispatch_subagents",
    "schedule_child_subagents",
    "subagent_board",
    "subagent_message",
}


# LLM: executed_subagent_orchestration gates deterministic closeout to subagent workflows.
# 函数用途: 只有本轮实际碰过子代理编排工具时，工具上限或空响应才改用 task.json 事实报告。
def executed_subagent_orchestration(params: ToolLoopExecuteParams) -> bool:
    return any(str(item or "") in _ORCHESTRATION_TOOLS for item in params.executed_tools or [])
