# LLM: Empty-response recovery keeps provider blank-text handling out of the core tool loop.
# 模块用途: 处理模型工具调用后返回空文本的重试提示和保守收口，不吞掉普通错误。

from __future__ import annotations

from collections.abc import Callable

from ..backends import ModelResponse
from ._runtime_params import ToolLoopExecuteParams


# LLM: should_retry_empty_model_response gives provider blank text one structured continuation attempt.
# 函数用途: 模型在工具调用后返回空文本时，先追加恢复提示再重试一次；重复空响应才走保守兜底。
def should_retry_empty_model_response(
    params: ToolLoopExecuteParams,
    exc: Exception,
    empty_response_repairs: int,
) -> bool:
    return is_empty_model_response_error(exc) and bool(params.executed_tools) and empty_response_repairs < 1


# LLM: empty_model_response_retry_context keeps real tool outputs as the source of truth after blank text.
# 函数用途: 告诉下一轮模型接着已完成的工具结果推进，不要因为供应商空响应从头重读或直接收口。
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


# LLM: empty_model_response_fallback prevents successful tool work from crashing on blank final text.
# 函数用途: 模型最终总结返回空文本时，若已有真实工具结果，就用本地事实生成保守收口，不让 CLI 异常退出。
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


# LLM: is_empty_model_response_error matches provider adapters that signal blank assistant text.
# 函数用途: 只兜底空文本响应，不吞掉普通 HTTP、权限、解析或业务异常。
def is_empty_model_response_error(exc: Exception) -> bool:
    return "没有文本内容" in str(exc)
