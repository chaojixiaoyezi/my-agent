# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。


from __future__ import annotations

import json
from dataclasses import dataclass

from ..backends import ModelResponse
from ..contracts.delivery_contract import repair_delivery_contract, run_delivery_contract
from ..memory_archive import ExternalizeToolOutputRequest, externalize_tool_output_record
from ..prompting_parts.builder import ToolSections
from ..subagents.services.session_progress import record_runtime_subagent_tool_progress
from ._runtime_params import ToolLoopExecuteParams
from .runner_stage_trace import (
    RunnerToolStageTraceRequest,
    trace_runner_tool_call_started,
)
from .subagent_attempt_guard import stale_subagent_attempt_message
from .subagent_dispatch_closeout import (
    subagent_dispatch_final_response_guard,
    subagent_dispatch_limit_response,
)
from .tool_call_context_reducer import render_tool_payload_for_live_prompt
from .tool_call_runtime import (
    ToolCallRuntimeRequest,
    execute_traced_tool_call,
    guarded_tool_call_result,
)
from .tool_context_reducer import render_tool_result_for_live_prompt
from .tool_context_window import window_tool_context_params
from .tool_loop_completion import ToolRoundCompletionRequest, completion_response_after_tool_round
from .tool_loop_empty_response import (
    empty_model_response_fallback,
    retry_context_for_model_response_error,
    should_retry_model_response_error,
)
from .tool_loop_orchestration_scope import executed_subagent_orchestration
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


# LLM: _ToolStepRequest bundles one tool-step transition for the loop service.
# 类用途: 保存进入工具执行/工具上限判断所需的上下文，避免 helper 参数继续扩散。
@dataclass(frozen=True)
class _ToolStepRequest:
    params: ToolLoopExecuteParams
    tool_rounds: int
    action: object
    current_prompt: str


# LLM: _build_prompt 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 构建提示词所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _build_prompt(agent, params: ToolLoopExecuteParams) -> str:
    window_tool_context_params(params)
    return agent.prompts.build(
        params.user_prompt,
        params.memories,
        inject=params.runtime_injections,
        prompt_files=params.prompt_files,
        system_prompt_override=params.system_prompt_override,
        context_scope=params.context_scope,
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
        empty_response_repairs = 0

        while True:
            (
                final_prompt,
                final_response,
                should_stop,
                retry_after_empty,
                empty_response_repairs,
            ) = self._model_turn_or_fallback(params, tool_rounds, empty_response_repairs)
            if retry_after_empty:
                continue
            if should_stop:
                break
            reserved_record_repairs, action = self._response_action(
                params,
                final_response,
                reserved_record_repairs,
            )
            if action.action == "continue":
                continue
            if action.action == "break":
                contract_break = self._delivery_contract_break_response(params, action.response)
                if contract_break is None:
                    continue
                final_response = contract_break
                break
            final_prompt, final_response, tool_rounds = self._tool_step_or_limit(
                _ToolStepRequest(
                    params=params,
                    tool_rounds=tool_rounds,
                    action=action,
                    current_prompt=final_prompt,
                )
            )
            if final_response:
                break

        final_response = subagent_dispatch_final_response_guard(
            self._agent,
            final_response,
            executed_tools=params.executed_tools,
        )
        return final_prompt, final_response, tool_rounds

    # LLM: _model_turn_or_fallback keeps model errors and fallback response generation isolated.
    # 函数用途: 执行一轮模型调用；过期 runner attempt 或空响应可恢复时返回 fallback 并要求主循环停止。
    def _model_turn_or_fallback(
        self,
        params: ToolLoopExecuteParams,
        tool_rounds: int,
        empty_response_repairs: int,
    ):
        stale_message = stale_subagent_attempt_message(self._agent)
        if stale_message is not None:
            backend = str(getattr(getattr(self._agent, "backend", None), "name", "") or "")
            return "", ModelResponse(text=stale_message, backend=backend), True, False, empty_response_repairs
        try:
            prompt, response = _next_model_response(self._agent, params, tool_rounds)
            return prompt, response, False, False, empty_response_repairs
        except Exception as exc:
            if should_retry_model_response_error(params, exc, empty_response_repairs):
                params.tool_context.append(retry_context_for_model_response_error(params, exc))
                return _build_prompt(self._agent, params), None, False, True, empty_response_repairs + 1
            fallback = empty_model_response_fallback(
                self._agent,
                params,
                exc,
                executed_subagent_orchestration=executed_subagent_orchestration,
            )
            if fallback is None:
                raise
            return _build_prompt(self._agent, params), fallback, True, False, empty_response_repairs

    # LLM: _response_action owns response decision bookkeeping for one model turn.
    # 函数用途: 根据模型输出判断继续生成、停止、或进入工具执行，并同步 reserved repair 次数。
    def _response_action(self, params: ToolLoopExecuteParams, response, reserved_record_repairs: int):
        decision = tool_loop_response_decision(
            ToolLoopResponseDecisionRequest(
                self._agent,
                params,
                response,
                reserved_record_repairs,
            )
        )
        return decision.reserved_record_repairs, decision

    # LLM: _tool_step_or_limit keeps tool-limit closeout separate from normal tool execution.
    # 函数用途: 达到工具轮数上限时生成收口回复，否则执行一轮工具并返回新状态。
    def _tool_step_or_limit(self, request: _ToolStepRequest):
        if self._tool_round_limit_reached(request.params, request.tool_rounds):
            final_prompt, final_response = self._final_response_after_tool_limit(
                request.params,
                request.tool_rounds,
            )
            return final_prompt, final_response, request.tool_rounds
        next_round = request.tool_rounds + 1
        next_round, final_response = self._run_tool_round(
            ToolRoundExecutionRequest(
                self._agent,
                request.params,
                next_round,
                request.action.response,
                request.action.calls,
                self._execute_one_tool_call,
                self._record_tool_call,
            )
        )
        return request.current_prompt, final_response, next_round

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
        if final_response is None:
            final_response = self._delivery_contract_response(request.params)
        return request.tool_rounds, final_response

    # LLM: _delivery_contract_response lets explicit root-agent delivery contracts stop runaway tool loops.
    # 函数用途: 每轮工具后按 task_attributes.delivery_contract_file 验收；通过即确定性收口，失败只回填 JSON findings。
    def _delivery_contract_response(self, params: ToolLoopExecuteParams):
        attrs = params.task_attributes if isinstance(params.task_attributes, dict) else {}
        contract_file = str(attrs.get("delivery_contract_file") or "").strip()
        if not contract_file:
            return None
        workspace_root = _agent_workspace_root(self._agent)
        report = run_delivery_contract(contract_file, workspace_root=workspace_root)
        if attrs.get("_delivery_contract_auto_repair_tried") is not True and _delivery_contract_has_checked_files(report):
            attrs["_delivery_contract_auto_repair_tried"] = True
            report = repair_delivery_contract(contract_file, workspace_root=workspace_root)
        payload = json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True)
        if attrs.get("_delivery_contract_last_report") != payload:
            attrs["_delivery_contract_last_report"] = payload
            params.tool_context.append("[delivery-contract-report]\n" + payload)
        if not report.ok:
            return None
        backend = str(getattr(getattr(self._agent, "backend", None), "name", "") or "")
        return ModelResponse(
            text=(
                "delivery_contract=pass\n"
                "结构化产物合同已通过，停止继续工具循环。"
            ),
            backend=backend,
        )

    # LLM: _delivery_contract_break_response is part of this module's structured runtime path; keep callers and tests aligned before changing it.
    # 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
    def _delivery_contract_break_response(self, params: ToolLoopExecuteParams, response):
        attrs = params.task_attributes if isinstance(params.task_attributes, dict) else {}
        contract_file = str(attrs.get("delivery_contract_file") or "").strip()
        if not contract_file:
            return response
        workspace_root = _agent_workspace_root(self._agent)
        report = repair_delivery_contract(contract_file, workspace_root=workspace_root)
        payload = json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True)
        if report.ok:
            backend = str(getattr(getattr(self._agent, "backend", None), "name", "") or "")
            return ModelResponse(
                text=(
                    "delivery_contract=pass\n"
                    "结构化产物合同已通过，停止继续工具循环。"
                ),
                backend=backend,
            )
        attempts = int(attrs.get("_delivery_contract_final_repair_count") or 0)
        if attempts < 1:
            attrs["_delivery_contract_final_repair_count"] = attempts + 1
            params.tool_context.append("[delivery-contract-report]\n" + payload)
            return None
        backend = str(getattr(getattr(self._agent, "backend", None), "name", "") or "")
        return ModelResponse(text="delivery_contract=fail\n" + payload, backend=backend)

    # LLM: _tool_round_limit_reached 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理工具round限制reached相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def _tool_round_limit_reached(self, params: ToolLoopExecuteParams, tool_rounds: int) -> bool:
        limit = _effective_max_tool_rounds(self._agent, params)
        return limit > 0 and tool_rounds >= limit

    # LLM: _final_response_after_tool_limit 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理final响应after工具限制相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def _final_response_after_tool_limit(self, params: ToolLoopExecuteParams, tool_rounds: int):
        params.tool_context.append("[tool-system]\n已达到最大工具轮数限制，停止继续调用工具。")
        contract_response = self._delivery_contract_limit_response(params)
        if contract_response is not None:
            return _build_prompt(self._agent, params), contract_response
        if executed_subagent_orchestration(params):
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

    # LLM: _delivery_contract_limit_response is part of this module's structured runtime path; keep callers and tests aligned before changing it.
    # 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
    def _delivery_contract_limit_response(self, params: ToolLoopExecuteParams):
        attrs = params.task_attributes if isinstance(params.task_attributes, dict) else {}
        contract_file = str(attrs.get("delivery_contract_file") or "").strip()
        if not contract_file:
            return None
        report = repair_delivery_contract(contract_file, workspace_root=_agent_workspace_root(self._agent))
        payload = json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True)
        backend = str(getattr(getattr(self._agent, "backend", None), "name", "") or "")
        if report.ok:
            return ModelResponse(
                text=(
                    "delivery_contract=pass\n"
                    "结构化产物合同已通过，停止继续工具循环。"
                ),
                backend=backend,
            )
        return ModelResponse(
            text=(
                "delivery_contract=fail\n"
                f"{payload}"
            ),
            backend=backend,
        )

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
        previous_background_intake = getattr(self._agent, "_current_background_intake", False)
        self._agent._current_background_intake = bool(request.params.background_intake)
        try:
            guard_result = guarded_tool_call_result(runtime_request)
            if guard_result is not None:
                return guard_result
            return execute_traced_tool_call(runtime_request)
        finally:
            self._agent._current_background_intake = previous_background_intake

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
        progress = record_runtime_subagent_tool_progress(self._agent, record)
        if progress:
            record.params.tool_context.append(_task_local_progress_context(progress))

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


# LLM: _task_local_progress_context gives the next runner turn a tiny progress anchor after writes.
# 函数用途: 将 latest_tool_progress 摘要放入 live prompt，提醒子代理 compact/续跑后对照已完成章节。
def _task_local_progress_context(progress: dict[str, object]) -> str:
    return "\n".join(
        [
            "[task-local-progress]",
            f"summary: {progress.get('summary', '')}",
            f"latest_written_path: {progress.get('latest_written_path', '')}",
            f"headings: {progress.get('headings', [])}",
            f"next_action: {progress.get('next_action', '')}",
            f"latest_tool_progress_ref: {progress.get('latest_tool_progress_ref', '')}",
            "policy: follow next_action; avoid duplicating recorded headings. "
            "If next_action mentions output.json, stop product-body writes and close out with structured refs.",
        ]
    )


# LLM: _agent_workspace_root is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _agent_workspace_root(agent) -> object:
    value = getattr(agent, "workspace_root", None) or getattr(agent, "root", None)
    if value:
        return value
    roots = getattr(agent, "workspace_roots", []) or []
    return roots[0] if roots else "."


# LLM: _delivery_contract_has_checked_files is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _delivery_contract_has_checked_files(report) -> bool:
    for check in getattr(report, "checks", []) or []:
        details = getattr(check, "details", {}) or {}
        validation = details.get("validation_result") if isinstance(details, dict) else {}
        if isinstance(validation, dict) and validation.get("checked_files"):
            return True
    return False
