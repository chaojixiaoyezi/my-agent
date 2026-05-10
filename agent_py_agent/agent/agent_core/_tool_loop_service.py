# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。


from __future__ import annotations

from dataclasses import dataclass

from ..backends import ModelResponse
from ..memory_archive import ExternalizeToolOutputRequest, externalize_tool_output_record
from ..prompting_parts.builder import ToolSections
from ..tools import ToolExecutionResult
from ._runtime_params import ToolLoopExecuteParams
from .parameters import _one_shot_tool_call_key
from .runner_stage_trace import (
    RunnerModelStageTraceRequest,
    RunnerToolStageTraceRequest,
    trace_runner_model_request_failed,
    trace_runner_model_request_started,
    trace_runner_model_response_received,
    trace_runner_tool_call_finished,
    trace_runner_tool_call_started,
)
from .tool_call_context_reducer import (
    AssistantToolRoundContextRequest,
    render_assistant_tool_round_context,
    render_tool_payload_for_live_prompt,
)
from .tool_context_reducer import render_tool_result_for_live_prompt
from .tool_output_failsafe import write_tool_output_fail_safe_checkpoint


# LLM: ToolCallRecordParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存工具call记录参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class ToolCallRecordParams:
    params: ToolLoopExecuteParams
    tool_rounds: int
    idx: int
    payload: object
    result: ToolExecutionResult


# LLM: ToolCallExecuteParams keeps one tool execution request bundled before result recording.
# 类用途: 单次工具调用执行参数包，避免 runner trace 和执行入口继续增加散乱参数。
@dataclass(frozen=True)
class ToolCallExecuteParams:
    params: ToolLoopExecuteParams
    tool_rounds: int
    idx: int
    payload: object


# LLM: ModelGenerateParams bundles backend generation inputs for trace and bundle-interface guard.
# 类用途: 模型生成参数包，集中 agent、运行参数、prompt 和当前工具轮次。
@dataclass(frozen=True)
class ModelGenerateParams:
    agent: object
    params: ToolLoopExecuteParams
    prompt: str
    tool_rounds: int


# LLM: AssistantToolRoundAppendParams keeps live-context append inputs in one bundle.
# 类用途: 保存本轮工具调用摘要回写所需上下文，避免内部 helper 重新出现散乱参数。
@dataclass(frozen=True)
class AssistantToolRoundAppendParams:
    params: ToolLoopExecuteParams
    tool_rounds: int
    response: ModelResponse
    calls: list[dict[str, object]]


# LLM: _build_prompt 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 构建提示词所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _build_prompt(agent, params: ToolLoopExecuteParams) -> str:
    return agent.prompts.build(
        params.user_prompt,
        params.memories,
        inject=params.runtime_injections,
        prompt_files=params.prompt_files,
        tools=ToolSections(
            tool_catalog_section=params.tool_catalog_section,
            tool_recommendations_section=params.tool_recommendations_section,
            tool_context=params.tool_context,
        ),
    )


# LLM: _effective_max_tool_rounds 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理effectivemax工具轮数相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
def _effective_max_tool_rounds(agent, params: ToolLoopExecuteParams) -> int:
    effective = agent.config.max_tool_rounds
    attrs_to_check = params.task_attributes or getattr(agent, "_current_task_attributes", None)
    if attrs_to_check and "max_tool_rounds" in attrs_to_check:
        return int(attrs_to_check["max_tool_rounds"])
    return effective


# LLM: _duplicate_one_shot_result 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理duplicateoneshot结果相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
def _duplicate_one_shot_result(payload: dict[str, object]) -> ToolExecutionResult:
    tool_name = str(payload.get("tool") or "unknown")
    return ToolExecutionResult(
        tool_name,
        False,
        "本轮已经执行过相同的一次性编排工具调用，系统已阻止重复执行。"
        "请基于前面的工具结果直接给最终回答，不要再次调用同一个工具。",
    )


# LLM: _append_assistant_tool_round_context protects the next live prompt from large tool payloads.
# 函数用途: 把模型刚生成的工具调用摘要写回 tool_context；大正文只保留长度/hash/预览，不再反复塞进后续提示词。
def _append_assistant_tool_round_context(request: AssistantToolRoundAppendParams) -> None:
    rendered = render_assistant_tool_round_context(
        AssistantToolRoundContextRequest(request.response.text, request.calls)
    )
    request.params.tool_context.append(
        f"[assistant-tool-round-{request.tool_rounds}]\n{rendered}"
    )


# LLM: ToolLoopService 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 封装工具循环服务操作，把状态读写和错误处理收束在服务层；关键副作用: 方法可能触发运行循环、工具调用、调度记录和最终响应相关副作用，需保持公开契约稳定。
class ToolLoopService:

    # LLM: __init__ 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def __init__(self, agent):
        self._agent = agent

    # LLM: execute 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 推进execute的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
    def execute(self, params: ToolLoopExecuteParams):
        final_prompt = ""
        final_response = None
        tool_rounds = params.tool_rounds

        while True:
            final_prompt = _build_prompt(self._agent, params)
            response = _generate_model_response(
                ModelGenerateParams(
                    agent=self._agent,
                    params=params,
                    prompt=final_prompt,
                    tool_rounds=tool_rounds,
                )
            )
            final_response = response

            if not self._agent.config.enable_tools:
                break

            calls = self._agent.tools.parse_tool_calls(response.text)
            if not calls:
                break

            if self._tool_round_limit_reached(params, tool_rounds):
                final_prompt, final_response = self._final_response_after_tool_limit(
                    params, tool_rounds
                )
                break

            tool_rounds += 1
            _append_assistant_tool_round_context(
                AssistantToolRoundAppendParams(params, tool_rounds, response, calls)
            )
            for idx, payload in enumerate(calls, start=1):
                tool_request = ToolCallExecuteParams(
                    params=params,
                    tool_rounds=tool_rounds,
                    idx=idx,
                    payload=payload,
                )
                result = self._execute_one_tool_call(tool_request)
                self._record_tool_call(
                    ToolCallRecordParams(params, tool_rounds, idx, payload, result)
                )

        return final_prompt, final_response, tool_rounds

    # LLM: _tool_round_limit_reached 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理工具round限制reached相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def _tool_round_limit_reached(self, params: ToolLoopExecuteParams, tool_rounds: int) -> bool:
        return tool_rounds >= _effective_max_tool_rounds(self._agent, params)

    # LLM: _final_response_after_tool_limit 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理final响应after工具限制相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def _final_response_after_tool_limit(self, params: ToolLoopExecuteParams, tool_rounds: int):
        params.tool_context.append("[tool-system]\n已达到最大工具轮数限制，停止继续调用工具。")
        final_prompt = _build_prompt(self._agent, params)
        final_response = _generate_model_response(
            ModelGenerateParams(
                agent=self._agent,
                params=params,
                prompt=final_prompt,
                tool_rounds=tool_rounds,
            )
        )
        final_response = _without_tool_call_after_limit(self._agent, final_response)
        return final_prompt, final_response

    # LLM: _execute_one_tool_call 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 推进one工具call的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
    def _execute_one_tool_call(self, request: ToolCallExecuteParams):
        trace_request = RunnerToolStageTraceRequest(
            agent=self._agent,
            params=request.params,
            tool_rounds=request.tool_rounds,
            idx=request.idx,
            payload=request.payload,
        )
        trace_runner_tool_call_started(trace_request)
        one_shot_key = _one_shot_tool_call_key(request.payload)
        if one_shot_key and one_shot_key in request.params.one_shot_tool_calls:
            result = _duplicate_one_shot_result(request.payload)
            trace_runner_tool_call_finished(
                RunnerToolStageTraceRequest(
                    agent=self._agent,
                    params=request.params,
                    tool_rounds=request.tool_rounds,
                    idx=request.idx,
                    payload=request.payload,
                    result=result,
                )
            )
            return result
        result = self._agent.tools.execute_call(
            request.payload,
            allowed_tools=request.params.allowed_tools,
            granted_capabilities=request.params.granted_capabilities,
            write_boundary=request.params.write_boundary,
        )
        if one_shot_key and result.ok:
            request.params.one_shot_tool_calls.add(one_shot_key)
        trace_runner_tool_call_finished(
            RunnerToolStageTraceRequest(
                agent=self._agent,
                params=request.params,
                tool_rounds=request.tool_rounds,
                idx=request.idx,
                payload=request.payload,
                result=result,
            )
        )
        return result

    # LLM: _record_tool_call 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 写入工具call的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
    def _record_tool_call(self, record: ToolCallRecordParams) -> None:
        if record.result.ok and record.result.tool not in {"__parse_error__", "unknown"}:
            record.params.executed_tools.append(record.result.tool)
        archive_record = self._archive_tool_call_record(record)
        record.params.archive_tool_calls.append(archive_record)
        record.params.tool_context.append(
            f"[tool-call-{record.tool_rounds}-{record.idx}]\n"
            f"{render_tool_payload_for_live_prompt(record.payload)}\n"
            f"[tool-result-{record.tool_rounds}-{record.idx}]\n"
            f"{render_tool_result_for_live_prompt(record.result, archive_record)}"
        )

    # LLM: _archive_tool_call_record 属于 SimpleAgent 核心运行的函数边界；工具输出归档格式变化会影响 raw archive 和 compact。
    # 函数用途: 生成可归档的工具调用记录，大输出外置为 artifact，当前工具上下文仍保留完整结果。
    def _archive_tool_call_record(self, record: ToolCallRecordParams) -> dict[str, object]:
        call_id = f"{record.tool_rounds}-{record.idx}"
        request = ExternalizeToolOutputRequest(
            root=self._agent.root,
            tool=record.result.tool,
            call_id=call_id,
            output=record.result.output,
            ok=record.result.ok,
            request_id=record.params.request_id,
            run_id=record.params.run_id,
            task_id=record.params.task_id,
        )
        fail_safe = write_tool_output_fail_safe_checkpoint(request)
        output_record = externalize_tool_output_record(request)
        output_record.update(fail_safe)
        output_record["parameters"] = record.payload
        return output_record


# LLM: _without_tool_call_after_limit enforces max-tool-round boundaries even if the model ignores the stop hint.
# 函数用途: 工具轮数已到顶后，如果模型仍输出工具调用，改成确定性停止说明，避免上层把新工具请求当最终答复。
def _without_tool_call_after_limit(agent, response: ModelResponse) -> ModelResponse:
    if not agent.tools.parse_tool_calls(response.text):
        return response
    return ModelResponse(
        text=(
            "已达到最大工具轮数限制，系统已经停止执行新的工具调用。"
            "模型在收口阶段仍输出工具调用请求，后续工具请求不会被执行；"
            "请只基于已有工具结果总结，若已有证据足够则进入等待验收。"
        ),
        backend=response.backend,
    )


# LLM: _generate_model_response wraps backend calls with refs-only runner stage trace events.
# 函数用途: 在模型请求前后写 runner 阶段心跳；普通主代理没有 runner id 时不会写 trace。
def _generate_model_response(request: ModelGenerateParams):
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
