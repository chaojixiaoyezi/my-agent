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
    try:
        response = request.agent.backend.generate(
            request.prompt,
            on_chunk=request.params.effective_on_chunk,
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
    trace_runner_model_response_received(
        RunnerModelStageTraceRequest(
            agent=request.agent,
            params=request.params,
            tool_rounds=request.tool_rounds,
            response=response,
        )
    )
    return response
