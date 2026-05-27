# LLM: Tool-agent budget stage keeps ToolLoopService thin while preserving runner trace contracts.
# 模块用途: 在工具真正执行前检查单代理预算；触发时写完 runner trace 并返回自检提示。

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ..tools import ToolExecutionResult
from .runner_context import current_subagent_run_id
from .runner_stage_trace import RunnerToolStageTraceRequest, trace_runner_tool_call_finished
from .tool_agent_budget import ToolAgentBudgetRequest, check_tool_agent_budget
from .tool_round_execution import ToolCallExecuteParams


# LLM: ToolAgentBudgetStageRequest bundles tool-loop stage data without exposing loose kwargs.
# 类用途: 保存当前 agent、工具执行请求和作用域 payload，用于执行预算检查并补 trace。
@dataclass(frozen=True)
class ToolAgentBudgetStageRequest:
    __test__: ClassVar[bool] = False

    agent: object
    execute_request: ToolCallExecuteParams
    payload: object


# LLM: maybe_block_tool_agent_budget is the tool-loop integration point for per-run budgets.
# 函数用途: 如果当前 run 超过单代理工具预算，返回工具结果并记录完成 trace；未超预算返回 None。
def maybe_block_tool_agent_budget(request: ToolAgentBudgetStageRequest) -> ToolExecutionResult | None:
    execute_request = request.execute_request
    result = check_tool_agent_budget(
        ToolAgentBudgetRequest(
            agent=request.agent,
            run_id=_runtime_run_id(request.agent, execute_request),
            tool_name=_tool_name(request.payload),
        )
    )
    if result is None:
        return None
    trace_runner_tool_call_finished(
        RunnerToolStageTraceRequest(
            agent=request.agent,
            params=execute_request.params,
            tool_rounds=execute_request.tool_rounds,
            idx=execute_request.idx,
            payload=request.payload,
            result=result,
        )
    )
    return result


# LLM: _runtime_run_id matches ToolLoopService runtime scoping without importing the service module.
# 函数用途: 从工具循环参数或当前子代理上下文解析 run_id；为空时表示主代理普通聊天不走预算限制。
def _runtime_run_id(agent: object, request: ToolCallExecuteParams) -> str:
    return str(current_subagent_run_id(agent) or request.params.run_id or "")


# LLM: _tool_name renders stable budget output for dict payloads and malformed payloads.
# 函数用途: 从工具 payload 中提取工具名，预算拦截提示会使用这个名称。
def _tool_name(payload: object) -> str:
    if isinstance(payload, dict):
        return str(payload.get("tool") or "unknown")
    return "unknown"
