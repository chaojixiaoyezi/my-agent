# LLM: Tool-loop model generation tracing stays outside the loop service to keep orchestration flow small.
# 模块用途: 包装 backend.generate，并记录 runner 模型请求阶段 trace。

from __future__ import annotations

from dataclasses import dataclass

from ._runtime_params import ToolLoopExecuteParams
from .runner_stage_trace import (
    RunnerModelStageTraceRequest,
    trace_runner_model_request_failed,
    trace_runner_model_request_started,
    trace_runner_model_response_received,
)
from .tool_stream_boundary import (
    ToolBoundaryChunkFilter,
    cut_response_after_first_complete_tool_call,
)


# LLM: ModelGenerateParams bundles backend generation inputs for trace and bundle-interface guard.
# 类用途: 模型生成参数包，集中 agent、运行参数、prompt 和当前工具轮次。
@dataclass(frozen=True)
class ModelGenerateParams:
    agent: object
    params: ToolLoopExecuteParams
    prompt: str
    tool_rounds: int


# LLM: generate_model_response wraps backend calls with refs-only runner stage trace events.
# 函数用途: 在模型请求前后写 runner 阶段心跳；普通主代理没有 runner id 时不会写 trace。
def generate_model_response(request: ModelGenerateParams):
    trace_runner_model_request_started(
        RunnerModelStageTraceRequest(
            agent=request.agent,
            params=request.params,
            tool_rounds=request.tool_rounds,
            prompt=request.prompt,
        )
    )
    chunk_filter = ToolBoundaryChunkFilter(request.params.effective_on_chunk)
    try:
        response = request.agent.backend.generate(
            request.prompt,
            on_chunk=chunk_filter if request.params.effective_on_chunk is not None else None,
        )
    except Exception as exc:
        trace_runner_model_request_failed(
            RunnerModelStageTraceRequest(
                agent=request.agent,
                params=request.params,
                tool_rounds=request.tool_rounds,
                exc=exc,
            )
        )
        raise
    chunk_filter.finish()
    response, cut = cut_response_after_first_complete_tool_call(response)
    if cut:
        request.params.tool_context.append(
            "[tool-system]\n"
            "模型回复在第一个完整工具调用后仍继续输出内容；系统已只保留第一个工具调用，"
            "后续正文不会作为工具结果、事实或下一轮上下文。"
        )
    trace_runner_model_response_received(
        RunnerModelStageTraceRequest(
            agent=request.agent,
            params=request.params,
            tool_rounds=request.tool_rounds,
            response=response,
        )
    )
    return response
