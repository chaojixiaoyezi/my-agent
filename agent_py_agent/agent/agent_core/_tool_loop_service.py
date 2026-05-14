# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。


from __future__ import annotations

from ..memory_archive import ExternalizeToolOutputRequest, externalize_tool_output_record
from ..prompting_parts.builder import ToolSections
from ._runtime_params import ToolLoopExecuteParams
from .runner_stage_trace import (
    RunnerToolStageTraceRequest,
    trace_runner_tool_call_started,
)
from .subagent_dispatch_closeout import subagent_dispatch_limit_response
from .tool_call_context_reducer import render_tool_payload_for_live_prompt
from .tool_call_runtime import (
    ToolCallRuntimeRequest,
    execute_traced_tool_call,
    guarded_tool_call_result,
)
from .tool_context_reducer import render_tool_result_for_live_prompt
from .tool_loop_completion import ToolRoundCompletionRequest, completion_response_after_tool_round
from .tool_loop_recovery import (
    append_long_content_recovery_context,
    payload_with_runtime_scope,
    runtime_run_id,
    without_tool_call_after_limit,
)
from .tool_loop_response_decision import (
    ToolLoopResponseDecisionRequest,
    tool_loop_response_decision,
)
from .tool_model_generation import ModelGenerateParams, generate_model_response
from .tool_output_failsafe import write_tool_output_fail_safe_checkpoint
from .tool_round_execution import (
    ToolCallExecuteParams,
    ToolCallRecordParams,
    ToolRoundExecutionRequest,
    execute_tool_round,
)


# LLM: _build_prompt 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 构建提示词所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _build_prompt(agent, params: ToolLoopExecuteParams) -> str:
    return agent.prompts.build(
        params.user_prompt,
        params.memories,
        inject=params.runtime_injections,
        prompt_files=params.prompt_files,
        system_prompt_override=params.system_prompt_override,
        tools=ToolSections(
            tool_catalog_section=params.tool_catalog_section,
            tool_recommendations_section=params.tool_recommendations_section,
            tool_context=params.tool_context,
        ),
    )


# LLM: _next_model_response keeps ToolLoopService.execute focused on control flow.
# 函数用途: 构建下一轮 prompt 并调用模型，返回 prompt 和 response 给工具循环使用。
def _next_model_response(agent, params: ToolLoopExecuteParams, tool_rounds: int):
    prompt = _build_prompt(agent, params)
    response = generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=params,
            prompt=prompt,
            tool_rounds=tool_rounds,
        )
    )
    return prompt, response


# LLM: _effective_max_tool_rounds 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理effectivemax工具轮数相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
def _effective_max_tool_rounds(agent, params: ToolLoopExecuteParams) -> int:
    effective = agent.config.max_tool_rounds
    attrs_to_check = params.task_attributes or getattr(agent, "_current_task_attributes", None)
    if attrs_to_check and "max_tool_rounds" in attrs_to_check:
        effective = attrs_to_check["max_tool_rounds"]
    try:
        return max(0, int(effective))
    except (TypeError, ValueError):
        return 0


# LLM: _executed_subagent_orchestration gates deterministic limit closeout to subagent workflows.
# 函数用途: 只有本轮实际碰过子代理编排工具时，工具上限才改用 subagent task.json 事实报告。
def _executed_subagent_orchestration(params: ToolLoopExecuteParams) -> bool:
    orchestration_tools = {
        "capability_config_patch",
        "create_subagents",
        "dispatch_subagents",
        "schedule_child_subagents",
        "subagent_board",
        "subagent_message",
    }
    return any(str(item or "") in orchestration_tools for item in params.executed_tools or [])


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
        reserved_record_repairs = 0

        while True:
            final_prompt, response = _next_model_response(self._agent, params, tool_rounds)
            final_response = response
            decision = tool_loop_response_decision(
                ToolLoopResponseDecisionRequest(
                    self._agent, params, response, reserved_record_repairs
                )
            )
            reserved_record_repairs = decision.reserved_record_repairs
            if decision.action == "continue":
                continue
            if decision.action == "break":
                final_response = decision.response
                break

            if self._tool_round_limit_reached(params, tool_rounds):
                final_prompt, final_response = self._final_response_after_tool_limit(
                    params, tool_rounds
                )
                break

            tool_rounds += 1
            tool_rounds, final_response = self._run_tool_round(
                ToolRoundExecutionRequest(
                    self._agent,
                    params,
                    tool_rounds,
                    decision.response,
                    decision.calls,
                    self._execute_one_tool_call,
                    self._record_tool_call,
                )
            )
            if final_response:
                break

        return final_prompt, final_response, tool_rounds

    # LLM: _run_tool_round executes one parsed tool round and returns any deterministic closeout.
    # 函数用途: 封装工具执行、output.json 收口和顶层 dispatch 收口，让 execute 保持短流程。
    def _run_tool_round(self, request: ToolRoundExecutionRequest):
        before_executed_count = len(request.params.executed_tools)
        subagent_output_written = execute_tool_round(request)
        final_response = completion_response_after_tool_round(
            ToolRoundCompletionRequest(
                self._agent,
                request.params,
                request.response,
                before_executed_count,
                subagent_output_written,
            )
        )
        return request.tool_rounds, final_response

    # LLM: _tool_round_limit_reached 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理工具round限制reached相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def _tool_round_limit_reached(self, params: ToolLoopExecuteParams, tool_rounds: int) -> bool:
        limit = _effective_max_tool_rounds(self._agent, params)
        return limit > 0 and tool_rounds >= limit

    # LLM: _final_response_after_tool_limit 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理final响应after工具限制相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def _final_response_after_tool_limit(self, params: ToolLoopExecuteParams, tool_rounds: int):
        params.tool_context.append("[tool-system]\n已达到最大工具轮数限制，停止继续调用工具。")
        if _executed_subagent_orchestration(params):
            backend = str(getattr(self._agent.backend, "name", "") or "")
            deterministic = subagent_dispatch_limit_response(self._agent, backend=backend)
            if deterministic is not None:
                return _build_prompt(self._agent, params), deterministic
        final_prompt = _build_prompt(self._agent, params)
        final_response = generate_model_response(
            ModelGenerateParams(
                agent=self._agent,
                params=params,
                prompt=final_prompt,
                tool_rounds=tool_rounds,
            )
        )
        final_response = without_tool_call_after_limit(self._agent, final_response)
        return final_prompt, final_response

    # LLM: _execute_one_tool_call 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 推进one工具call的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
    def _execute_one_tool_call(self, request: ToolCallExecuteParams):
        payload = payload_with_runtime_scope(self._agent, request.params, request.payload)
        trace_request = RunnerToolStageTraceRequest(
            agent=self._agent,
            params=request.params,
            tool_rounds=request.tool_rounds,
            idx=request.idx,
            payload=payload,
        )
        trace_runner_tool_call_started(trace_request)
        runtime_request = ToolCallRuntimeRequest(self._agent, request, payload, trace_request)
        guard_result = guarded_tool_call_result(runtime_request)
        if guard_result is not None:
            return guard_result
        return execute_traced_tool_call(runtime_request)

    # LLM: _record_tool_call 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 写入工具call的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
    def _record_tool_call(self, record: ToolCallRecordParams) -> None:
        if record.result.ok and record.result.tool not in {"__parse_error__", "unknown"}:
            record.params.executed_tools.append(record.result.tool)
        archive_record = self._archive_tool_call_record(record)
        record.params.archive_tool_calls.append(archive_record)
        record.params.tool_context.append(
            f"[tool-record round={record.tool_rounds} index={record.idx}]\n"
            f"{render_tool_payload_for_live_prompt(record.payload)}\n"
            f"[tool-output-record round={record.tool_rounds} index={record.idx}]\n"
            f"{render_tool_result_for_live_prompt(record.result, archive_record)}"
        )
        append_long_content_recovery_context(record)

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
            run_id=runtime_run_id(self._agent, record.params),
            task_id=record.params.task_id,
        )
        fail_safe = write_tool_output_fail_safe_checkpoint(request)
        output_record = externalize_tool_output_record(request)
        output_record.update(fail_safe)
        output_record["parameters"] = record.payload
        return output_record
