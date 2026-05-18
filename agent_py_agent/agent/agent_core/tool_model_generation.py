# LLM: Tool-loop model generation tracing stays outside the loop service to keep orchestration flow small.
# 模块用途: 包装 backend.generate，并记录 runner 模型请求阶段 trace。

from __future__ import annotations

from dataclasses import dataclass
from queue import Empty, Queue
from threading import Thread

from ..backends.errors import ProviderTimeoutError
from ._runtime_params import ToolLoopExecuteParams
from .runner_stage_trace import (
    RunnerModelStageTraceRequest,
    trace_runner_model_request_failed,
    trace_runner_model_request_started,
    trace_runner_model_response_received,
)
from .tool_stream_boundary import (
    LongToolContentStreamAbort,
    ToolBoundaryChunkFilter,
    cut_response_after_first_complete_tool_call,
    long_write_abort_response,
)


# LLM: ModelGenerateParams bundles backend generation inputs for trace and bundle-interface guard.
# 类用途: 模型生成参数包，集中 agent、运行参数、prompt 和当前工具轮次。
@dataclass(frozen=True)
class ModelGenerateParams:
    agent: object
    params: ToolLoopExecuteParams
    prompt: str
    tool_rounds: int


# LLM: _BackendGenerateResult keeps the background timeout bridge typed without exposing thread details.
# 类用途: 保存 backend.generate 的返回或异常；公共模型调用边界用它把后台结果安全传回主流程。
@dataclass(frozen=True)
class _BackendGenerateResult:
    response: object | None = None
    exc: BaseException | None = None


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
    chunk_filter = ToolBoundaryChunkFilter(
        request.params.effective_on_chunk,
        max_inline_content_chars=_tool_write_inline_max_chars(request.agent),
    )
    try:
        response = _generate_with_wall_timeout(request, chunk_filter)
    except LongToolContentStreamAbort as exc:
        response = long_write_abort_response(
            exc,
            backend=str(getattr(request.agent.backend, "name", "") or ""),
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


# LLM: _generate_with_wall_timeout prevents a stuck provider call from trapping the whole runner.
# 函数用途: 给任意 backend.generate 增加 request_timeout 总时长保护；后端正常返回时保持原响应对象。
def _generate_with_wall_timeout(request: ModelGenerateParams, chunk_filter: ToolBoundaryChunkFilter):
    timeout = _model_request_timeout_seconds(request.agent)
    on_chunk = chunk_filter
    if timeout <= 0:
        return request.agent.backend.generate(request.prompt, on_chunk=on_chunk)

    results: Queue[_BackendGenerateResult] = Queue(maxsize=1)

    # LLM: _target runs the provider call in a daemon guard thread so the caller can regain control.
    # 函数用途: 执行真实模型请求并把返回值或异常放回队列；超时后线程不会阻塞当前 run 退出。
    def _target() -> None:
        try:
            results.put(
                _BackendGenerateResult(
                    response=request.agent.backend.generate(request.prompt, on_chunk=on_chunk)
                )
            )
        except BaseException as exc:  # pragma: no cover - exercised through queue result.
            results.put(_BackendGenerateResult(exc=exc))

    worker = Thread(target=_target, name="my-agent-model-generate-timeout-guard", daemon=True)
    worker.start()
    try:
        result = results.get(timeout=timeout)
    except Empty as exc:
        raise ProviderTimeoutError(
            f"模型接口请求超时: request_timeout={timeout:g}s"
        ) from exc
    if result.exc is not None:
        raise result.exc
    return result.response


# LLM: _model_request_timeout_seconds resolves the public request_timeout setting for the guard layer.
# 函数用途: 从 agent.config 或 backend 上读取 request_timeout；无效或关闭时返回 0 表示不启用总时长保护。
def _model_request_timeout_seconds(agent: object) -> float:
    config = getattr(agent, "config", None)
    raw = getattr(config, "request_timeout", None)
    if raw is None:
        raw = getattr(getattr(agent, "backend", None), "request_timeout", 0)
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return timeout if timeout > 0 else 0.0


# LLM: _tool_write_inline_max_chars keeps streaming guard aligned with write_file/append_file config.
# 函数用途: 从 config 读取单次 inline 写入上限；无效值由工具内容策略回退默认值。
def _tool_write_inline_max_chars(agent: object) -> int | None:
    return getattr(getattr(agent, "config", None), "tool_write_inline_max_chars", None)
