
from __future__ import annotations

from ...backends.errors import is_empty_provider_response_error
from .._runtime_params import ToolLoopExecuteParams


def should_retry_empty_model_response(
    params: ToolLoopExecuteParams,
    exc: Exception,
    empty_response_repairs: int,
) -> bool:
    return is_empty_model_response_error(exc) and bool(params.executed_tools) and empty_response_repairs < 1


def empty_model_response_retry_context(params: ToolLoopExecuteParams) -> str:
    tools = ", ".join(str(item) for item in params.executed_tools[-6:]) or "(none)"
    return "\n".join(
        [
            "[tool-system]",
            "上一轮模型接口返回了空文本；真实工具调用和工具结果已经保留在上方 tool-record/tool-output-record 中。",
            f"recent_executed_tools: {tools}",
            "请基于这些已完成结果继续：任务未完成就调用下一步工具，任务已完成才给最终回答。不要从头重复读取同一批材料。",
        ]
    )


def is_empty_model_response_error(exc: Exception) -> bool:
    return is_empty_provider_response_error(exc)
