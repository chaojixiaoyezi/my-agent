

from __future__ import annotations

from dataclasses import dataclass

from ..backends import ModelResponse
from ..backends.errors import is_empty_provider_response_error
from ..prompting_parts.builder import ToolSections
from ..settings.runtime_guard_config import runtime_guard_int
from ..subagents.services.session_progress import record_runtime_subagent_tool_progress
from ._runtime_params import ToolLoopExecuteParams
from .delivery_contract_prompting import render_delivery_contract_section
from .delivery_completion_soft_hint import maybe_append_delivery_completion_soft_hint
from .orchestration.shared_context import (
    refresh_parent_shared_context_cache,
    refresh_parent_shared_context_from_tool_record,
)
from .provider_transient_auto_resume import run_with_provider_transient_auto_resume
from .runner.context import current_task_attributes
from .runner.stage_trace import RunnerToolStageTraceRequest, trace_runner_tool_call_started
from .runtime.live_archive import (
    archive_tool_call_if_enabled,
    update_runtime_fact_progress_if_enabled,
)
from .runtime.guidance import inject_pending_guidance
from .subagent.attempt_guard import stale_subagent_attempt_message
from .tool_call_archive_record import archive_tool_call_record
from .tool_call_runtime import (
    ToolCallRuntimeRequest,
    execute_traced_tool_call,
    guarded_tool_call_result,
)
from .tool_context.call_reducer import render_tool_payload_for_live_prompt
from .tool_context.reducer import render_tool_result_for_live_prompt
from .tool_guard.call_guardrail import record_tool_guard_observation
from .tool_guard.loop_hints import append_tool_guardrail_action_block_hint
from .tool_context.window import window_tool_context_params
from .tool_loop.completion import ToolRoundCompletionRequest, completion_response_after_tool_round
from .tool_loop.recovery import append_long_content_recovery_context, without_tool_call_after_limit
from .tool_loop.response_decision import (
    ToolLoopRepairCounters,
    ToolLoopResponseDecisionRequest,
    tool_loop_response_decision,
)
from .tool_loop.round_execution import (
    ToolCallExecuteParams,
    ToolCallRecordParams,
    ToolRoundExecutionRequest,
    execute_tool_round,
)
from .tool_loop.recovery import payload_with_runtime_scope
from .tool_model_generation import ModelGenerateParams, generate_model_response
from .tool_runtime_ledger import persist_tool_runtime_ledger

_ORCHESTRATION_TOOLS = {
    "create_subagents",
    "dispatch_subagents",
    "schedule_child_subagents",
}


@dataclass(frozen=True)
class _ToolStepRequest:
    params: ToolLoopExecuteParams
    tool_rounds: int
    action: object
    current_prompt: str


@dataclass(frozen=True)
class _PendingDeferredToolDrainResult:
    tool_rounds: int
    final_response: ModelResponse | None = None


def _effective_max_tool_rounds(agent, params: ToolLoopExecuteParams) -> int:
    config = getattr(agent, "config", None)
    if hasattr(config, "max_tool_rounds"):
        effective = getattr(config, "max_tool_rounds", None)
    else:
        effective = runtime_guard_int(
            "max_tool_rounds",
            0,
            policy=getattr(agent, "runtime_guard_policy", None),
        )
    attrs_to_check = params.task_attributes or current_task_attributes(agent)
    if attrs_to_check and "max_tool_rounds" in attrs_to_check:
        effective = attrs_to_check["max_tool_rounds"]
    try:
        return max(0, int(effective))
    except (TypeError, ValueError):
        return 0


def _should_retry_empty_model_response(
    params: ToolLoopExecuteParams,
    exc: Exception,
    empty_response_repairs: int,
) -> bool:
    return is_empty_provider_response_error(exc) and bool(params.executed_tools) and empty_response_repairs < 1


def _empty_model_response_retry_context(params: ToolLoopExecuteParams) -> str:
    tools = ", ".join(str(item) for item in params.executed_tools[-6:]) or "(none)"
    return "\n".join(
        [
            "[tool-system]",
            "上一轮模型接口返回了空文本；真实工具调用和工具结果已经保留在上方 tool-record/tool-output-record 中。",
            f"recent_executed_tools: {tools}",
            "请基于这些已完成结果继续：任务未完成就调用下一步工具，任务已完成才给最终回答。不要从头重复读取同一批材料。",
        ]
    )


def build_tool_loop_prompt(agent, params: ToolLoopExecuteParams) -> str:
    inject_pending_guidance(agent, params)
    window_tool_context_params(agent, params)
    return agent.prompts.build(
        params.user_prompt,
        params.memories,
        inject=_runtime_injections_with_delivery_contract(params),
        prompt_files=params.prompt_files,
        system_prompt_override=params.system_prompt_override,
        context_scope=params.context_scope,
        tools=ToolSections(
            tool_catalog_section=params.tool_catalog_section,
            tool_recommendations_section=params.tool_recommendations_section,
            tool_context=params.tool_context,
        ),
    )


def _runtime_injections_with_delivery_contract(params: ToolLoopExecuteParams) -> list:
    if not isinstance(params.delivery_contract, dict):
        return params.runtime_injections
    return [*params.runtime_injections, render_delivery_contract_section(params.delivery_contract)]


def next_tool_loop_model_response(agent, params: ToolLoopExecuteParams, tool_rounds: int):
    prompt = build_tool_loop_prompt(agent, params)
    response = generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=params,
            prompt=prompt,
            tool_rounds=tool_rounds,
        )
    )
    return prompt, response


def execute_one_tool_call(agent, request: ToolCallExecuteParams):
    sentinel = object()
    previous = getattr(agent, "_current_tool_loop_params", sentinel)
    agent._current_tool_loop_params = request.params
    try:
        return _execute_scoped_tool_call(agent, request)
    finally:
        _restore_tool_loop_params(agent, previous, sentinel)


def _execute_scoped_tool_call(agent, request: ToolCallExecuteParams):
    payload = payload_with_runtime_scope(agent, request.params, request.payload)
    trace_request = RunnerToolStageTraceRequest(
        agent=agent,
        params=request.params,
        tool_rounds=request.tool_rounds,
        idx=request.idx,
        payload=payload,
    )
    trace_runner_tool_call_started(trace_request)
    runtime_request = ToolCallRuntimeRequest(agent, request, payload, trace_request)
    guard_result = guarded_tool_call_result(runtime_request)
    if guard_result is not None:
        return guard_result
    return execute_traced_tool_call(runtime_request)


def _restore_tool_loop_params(agent, previous: object, sentinel: object) -> None:
    if previous is sentinel:
        try:
            delattr(agent, "_current_tool_loop_params")
        except AttributeError:
            pass
        return
    agent._current_tool_loop_params = previous


class ToolLoopService:

    def __init__(self, agent):
        self._agent = agent

    def execute(self, params: ToolLoopExecuteParams):
        final_prompt = ""
        final_response = None
        tool_rounds = params.tool_rounds
        repair_counters = ToolLoopRepairCounters()
        empty_response_repairs = 0

        while True:
            pending_result = self._drain_pending_deferred_tool_calls(params, tool_rounds)
            if pending_result is not None:
                tool_rounds = pending_result.tool_rounds
                if pending_result.final_response is not None:
                    final_response = pending_result.final_response
                    break
                continue
            (
                final_prompt,
                final_response,
                should_stop,
                retry_after_empty,
                empty_response_repairs,
            ) = self._model_turn_or_retry(params, tool_rounds, empty_response_repairs)
            if retry_after_empty:
                continue
            if should_stop:
                break
            repair_counters, action = self._response_action(
                params,
                final_response,
                repair_counters,
            )
            if action.action == "continue":
                continue
            if action.action == "break":
                final_response = action.response
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

        return final_prompt, final_response, tool_rounds

    def _drain_pending_deferred_tool_calls(
        self,
        params: ToolLoopExecuteParams,
        tool_rounds: int,
    ) -> _PendingDeferredToolDrainResult | None:
        pending_calls = _pop_pending_deferred_tool_calls(params)
        if not pending_calls:
            return None
        if self._tool_round_limit_reached(params, tool_rounds):
            final_prompt, final_response = self._final_response_after_tool_limit(params, tool_rounds)
            del final_prompt
            return _PendingDeferredToolDrainResult(tool_rounds, final_response)
        next_round = tool_rounds + 1
        next_round, final_response = self._run_tool_round(
            ToolRoundExecutionRequest(
                self._agent,
                params,
                next_round,
                ModelResponse(text="[PENDING_DEFERRED_TOOL_CALLS]", backend="tool_loop"),
                pending_calls,
                self._execute_one_tool_call,
                self._record_tool_call,
                _deferred_drain_prompt(self._agent, params),
            )
        )
        return _PendingDeferredToolDrainResult(next_round, final_response)

    def _model_turn_or_retry(
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
            prompt, response = run_with_provider_transient_auto_resume(
                lambda: next_tool_loop_model_response(self._agent, params, tool_rounds),
                on_chunk=params.effective_on_chunk,
                policy=getattr(self._agent, "runtime_guard_policy", None),
            )
            return prompt, response, False, False, empty_response_repairs
        except Exception as exc:
            if _should_retry_empty_model_response(params, exc, empty_response_repairs):
                params.tool_context.append(_empty_model_response_retry_context(params))
                return (
                    build_tool_loop_prompt(self._agent, params),
                    None,
                    False,
                    True,
                    empty_response_repairs + 1,
                )
            raise

    def _response_action(
        self,
        params: ToolLoopExecuteParams,
        response,
        repair_counters: ToolLoopRepairCounters,
    ):
        decision = tool_loop_response_decision(
            ToolLoopResponseDecisionRequest(
                self._agent,
                params,
                response,
                repair_counters,
            )
        )
        return decision.counters, decision

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
                request.current_prompt,
            )
        )
        return request.current_prompt, final_response, next_round

    def _run_tool_round(self, request: ToolRoundExecutionRequest):
        before_executed_count = len(request.params.executed_tools)
        subagent_output_written = execute_tool_round(request)
        update_runtime_fact_progress_if_enabled(self._agent, request.params, tool_round=request.tool_rounds)
        append_tool_guardrail_action_block_hint(request)
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

    def _tool_round_limit_reached(self, params: ToolLoopExecuteParams, tool_rounds: int) -> bool:
        limit = _effective_max_tool_rounds(self._agent, params)
        return limit > 0 and tool_rounds >= limit

    def _final_response_after_tool_limit(self, params: ToolLoopExecuteParams, tool_rounds: int):
        params.tool_context.append("[tool-system]\n已达到最大工具轮数限制，停止继续调用工具。")
        if _executed_subagent_orchestration(params):
            params.tool_context.append("[tool-system]\n子代理调度状态请通过 dispatch_subagents/tree 状态结果继续查看；系统不再替主代理生成最终结论。")
        final_prompt = build_tool_loop_prompt(self._agent, params)
        final_response = generate_model_response(
            ModelGenerateParams(
                agent=self._agent,
                params=params,
                prompt=final_prompt,
                tool_rounds=tool_rounds,
            )
        )
        return final_prompt, without_tool_call_after_limit(self._agent, final_response)

    def _execute_one_tool_call(self, request: ToolCallExecuteParams):
        return execute_one_tool_call(self._agent, request)

    def _record_tool_call(self, record: ToolCallRecordParams) -> None:
        guardrail_hint = record_tool_guard_observation(self._agent, record.params, record.payload, record.result)
        if record.result.ok and record.result.tool not in {"__parse_error__", "unknown"}:
            record.params.executed_tools.append(record.result.tool)
        archive_record = self._archive_tool_call_record(record)
        archive_tool_call_if_enabled(
            self._agent,
            record.params,
            archive_record,
            tool_round=record.tool_rounds,
            tool_index=record.idx,
        )
        persist_tool_runtime_ledger(self._agent, archive_record)
        record.params.archive_tool_calls.append(archive_record)
        update_runtime_fact_progress_if_enabled(self._agent, record.params, tool_round=record.tool_rounds)
        refresh_parent_shared_context_cache(self._agent, record.params.archive_tool_calls)
        refresh_parent_shared_context_from_tool_record(self._agent, record)
        maybe_append_delivery_completion_soft_hint(
            self._agent,
            record.params,
            archive_record,
            tool_ok=bool(record.result.ok),
        )
        record.params.tool_context.append(
            f"[tool-record round={record.tool_rounds} index={record.idx}]\n"
            f"{render_tool_payload_for_live_prompt(record.payload)}\n"
            f"[tool-output-record round={record.tool_rounds} index={record.idx}]\n"
            f"{render_tool_result_for_live_prompt(record.result, archive_record)}"
        )
        if guardrail_hint:
            record.params.tool_context.append(f"[tool-loop-guardrail-hint]\n{guardrail_hint}")
        append_long_content_recovery_context(record)
        progress = record_runtime_subagent_tool_progress(self._agent, record)
        if progress:
            record.params.tool_context.append(_task_local_progress_context(progress))

    def _archive_tool_call_record(self, record: ToolCallRecordParams) -> dict[str, object]:
        return archive_tool_call_record(self._agent, record)


def _task_local_progress_context(progress: dict[str, object]) -> str:
    load_error = progress.get("load_error")
    status_save_error = progress.get("status_save_error")
    return "\n".join(
        [
            "[task-local-progress]",
            f"summary: {progress.get('summary', '')}",
            f"latest_written_path: {progress.get('latest_written_path', '')}",
            f"headings: {progress.get('headings', [])}",
            f"next_action: {progress.get('next_action', '')}",
            f"latest_tool_progress_ref: {progress.get('latest_tool_progress_ref', '')}",
            f"load_error: {load_error}" if isinstance(load_error, dict) else "",
            f"status_save_error: {status_save_error}" if isinstance(status_save_error, dict) else "",
            "policy: follow next_action; avoid duplicating recorded headings. "
            "If next_action mentions output.json, stop product-body writes and close out with structured refs.",
        ]
    )


def _executed_subagent_orchestration(params: ToolLoopExecuteParams) -> bool:
    return any(str(item or "") in _ORCHESTRATION_TOOLS for item in params.executed_tools or [])


def _pop_pending_deferred_tool_calls(params: ToolLoopExecuteParams) -> list[dict[str, object]]:
    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict):
        return []
    value = state.pop("pending_deferred_tool_calls", [])
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict) and str(item.get("tool") or "").strip()]


def _deferred_drain_prompt(agent: object, params: ToolLoopExecuteParams) -> str:
    try:
        return build_tool_loop_prompt(agent, params)
    except Exception:
        return ""
