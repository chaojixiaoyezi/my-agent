
from __future__ import annotations

from collections.abc import Callable

from ...backends import ModelResponse
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


def empty_model_response_fallback(
    agent: object,
    params: ToolLoopExecuteParams,
    exc: Exception,
    *,
    executed_subagent_orchestration: Callable[[ToolLoopExecuteParams], bool],
) -> ModelResponse | None:
    if not is_empty_model_response_error(exc) or not params.executed_tools:
        return None
    backend = str(getattr(getattr(agent, "backend", None), "name", "") or "")
    tools = ", ".join(str(item) for item in params.executed_tools[-6:])
    text = (
        "模型接口最终总结返回空文本；本轮真实工具调用已经完成，系统没有丢弃工具结果。\n\n"
        f"- executed_tools: {tools or '(none)'}\n"
        "- 请根据上方工具记录继续，或重试生成最终总结。"
    )
    return ModelResponse(text=text, backend=backend)


def is_empty_model_response_error(exc: Exception) -> bool:
    return "没有文本内容" in str(exc)
