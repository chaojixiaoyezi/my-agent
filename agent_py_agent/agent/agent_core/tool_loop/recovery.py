
from __future__ import annotations

from typing import Any

from ...action_protocol import (
    RunScope,
    ToolCallEnvelopePayloadRequest,
    tool_call_envelope_from_payload,
)
from ...backends import ModelResponse
from ...common.value_parsing import text_value
from ...runtime_errors import runtime_error_report
from ...tooling.content_recovery_mode import (
    LongContentRecoveryRequest,
    long_content_recovery_context,
)
from .._runtime_params import ToolLoopExecuteParams
from ..runner.context import current_subagent_run_id
from .round_execution import ToolCallRecordParams


def without_tool_call_after_limit(agent, response: ModelResponse) -> ModelResponse:
    if not agent.tools.parse_tool_calls(response.text):
        return response
    return ModelResponse(
        text=(
            "已达到最大工具轮数限制，系统已经停止执行新的工具调用。"
            "模型在收口阶段仍输出工具调用请求，后续工具请求不会被执行；"
            "请只基于已有工具结果总结，若已有证据足够则进入等待收口。"
        ),
        backend=response.backend,
    )


def payload_with_runtime_scope(agent, params: ToolLoopExecuteParams, payload: object) -> object:
    if not isinstance(payload, dict):
        return payload
    tool = str(payload.get("tool") or "")
    if tool == "read_artifact":
        scoped = dict(payload)
        scoped.setdefault("run_id", runtime_run_id(agent, params))
        scoped.setdefault("task_id", params.task_id)
        scoped.setdefault("request_id", params.request_id)
        return scoped
    return payload


def runtime_run_id(agent, params: ToolLoopExecuteParams) -> str:
    scoped_run_id = params.run_scope.run_id if params.run_scope is not None else ""
    current = current_subagent_run_id(agent)
    return str(scoped_run_id or current or params.run_id or "")


def runtime_run_scope(agent, params: ToolLoopExecuteParams) -> RunScope:
    if params.run_scope is not None:
        return params.run_scope
    run_id = runtime_run_id(agent, params)
    attrs = params.task_attributes if isinstance(params.task_attributes, dict) else {}
    task, load_error = _load_runtime_task(agent, run_id)
    parent_run_id = text_value(getattr(task, "parent_id", "")) or text_value(attrs.get("parent_run_id"))
    root_run_id = text_value(getattr(task, "root_id", "")) or text_value(attrs.get("root_run_id")) or run_id
    root_task_id = text_value(getattr(task, "root_id", "")) or text_value(attrs.get("root_task_id")) or root_run_id
    depth = _int_value(getattr(task, "depth", attrs.get("depth", 0)))
    agent_kind = text_value(attrs.get("agent_kind")) or _agent_kind(parent_run_id=parent_run_id, depth=depth)
    return RunScope(
        request_id=params.request_id,
        task_id=params.task_id or run_id,
        run_id=run_id,
        parent_run_id=parent_run_id,
        root_task_id=root_task_id,
        root_run_id=root_run_id,
        depth=depth,
        agent_kind=agent_kind,
        task_load_error=_scope_task_load_error(load_error),
    )


def tool_payload_with_run_scope(
    agent,
    params: ToolLoopExecuteParams,
    payload: object,
    *,
    call_id: str,
    source: str = "model_tool_call",
) -> object:
    if not isinstance(payload, dict):
        return payload
    return tool_call_envelope_from_payload(
        ToolCallEnvelopePayloadRequest(
            payload=payload,
            call_id=call_id,
            source=source,
            scope=runtime_run_scope(agent, params),
        )
    )


def _load_runtime_task(agent, run_id: str) -> Any:
    if not run_id:
        return None, None
    try:
        return agent.subagents.load(run_id), None
    except Exception as exc:
        return None, exc


def _scope_task_load_error(load_error: BaseException | None) -> dict[str, object]:
    if load_error is None:
        return {}
    return runtime_error_report(load_error, context="tool_call_scope.subagents.load")


def _agent_kind(*, parent_run_id: str, depth: int) -> str:
    if not parent_run_id and depth <= 0:
        return "root_agent"
    if depth <= 1:
        return "child_agent"
    return "grandchild_agent"

def _int_value(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def append_long_content_recovery_context(record: ToolCallRecordParams) -> None:
    context = long_content_recovery_context(
        LongContentRecoveryRequest(
            payload=record.payload,
            result_tool=record.result.tool,
            result_ok=record.result.ok,
            output=record.result.output,
        )
    )
    if context:
        record.params.tool_context.append(context)
