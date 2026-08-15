
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ....conversation.models import new_id
from ....runtime_errors import runtime_error_report
from ....tooling.models import (
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ResourceScopePolicy,
    ToolHandlerOutcome,
    ToolRuntimePolicy,
)
from ..tool_specs import build_raise_event_model_spec

if TYPE_CHECKING:
    from ....core import SimpleAgent


@dataclass(frozen=True)
class _EventResultRefs:
    thread_id: str
    task_id: str
    observation_id: str
    wake_signal_id: str
    wake_signal_error: dict[str, object] | None = None


class RaiseEventTool(BaseTool):
    model_spec = build_raise_event_model_spec()
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("mutating"),
        idempotency_policy=IdempotencyPolicy("operation"),
        # seq 253 闭合：thread_id/task_id/dedupe_key 是逻辑 ID 不是路径。
        resource_scopes=ResourceScopePolicy(
            parameter_names=("thread_id", "task_id", "dedupe_key"),
            parameter_kinds={
                "thread_id": "logical",
                "task_id": "logical",
                "dedupe_key": "logical",
            },
        ),
    )

    def __init__(self, agent: SimpleAgent):
        self.agent = agent

    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        resolved = _resolve_event_thread(self.agent, params, tool_name="raise_event")
        if isinstance(resolved, ToolHandlerOutcome):
            return resolved
        thread_id, task_id = resolved
        lineage_task_id = str(params.get("source_agent_id") or task_id).strip()
        lineage = _event_lineage_defaults(self.agent, lineage_task_id)
        lineage_load_error = lineage.pop("lineage_load_error", None)
        metadata = _metadata_values(params.get("metadata"))
        if lineage_load_error:
            metadata["lineage_load_error"] = lineage_load_error
        urgency = str(params.get("urgency") or "normal")
        requires_main_agent = (
            _boolish(params.get("requires_main_agent"))
            or urgency.strip().lower() == "urgent"
        )
        source_agent_id = str(
            params.get("source_agent_id") or lineage["source_agent_id"]
        )
        root_task_id = str(params.get("root_task_id") or lineage["root_task_id"])
        observation_id = new_id("obs")
        observation_request = {
            "thread_id": thread_id,
            "event_type": str(params.get("event_type") or "observation"),
            "summary": str(params.get("summary") or ""),
            "urgency": urgency,
            "severity": str(params.get("severity") or ""),
            "source_agent_id": source_agent_id,
            "parent_agent_id": str(
                params.get("parent_agent_id") or lineage["parent_agent_id"]
            ),
            "root_task_id": root_task_id,
            "evidence_refs": _string_values(params.get("evidence_refs")),
            "requires_main_agent": requires_main_agent,
            "requires_llm_report": _boolish(params.get("requires_llm_report")),
            "metadata": metadata,
            # Private preallocation lets the tool identify the durable observation
            # even when the paired wake queue write fails after fallback persistence.
            "_observation_id": observation_id,
        }
        wake_required = _event_needs_wake(
            params,
            source_agent_id=source_agent_id,
            root_task_id=root_task_id,
        )
        try:
            if wake_required:
                observation, signal = (
                    self.agent.conversation_store.append_observation_with_wake(
                        observation_request,
                        {
                            "thread_id": thread_id,
                            "reason": str(
                                params.get("event_type") or "urgent_observation"
                            ),
                            "urgency": "urgent",
                            "dedupe_key": str(params.get("dedupe_key") or ""),
                        },
                    )
                )
                wake_signal_id = signal.wake_signal_id
            else:
                observation = self.agent.conversation_store.append_observation(
                    observation_request
                )
                wake_signal_id = ""
        except Exception as exc:
            if wake_required:
                # append_observation_with_wake preserves the observation as its
                # documented fallback when the wake queue is unavailable.
                return _event_tool_result(
                    "raise_event",
                    _EventResultRefs(
                        thread_id=thread_id,
                        task_id=task_id,
                        observation_id=observation_id,
                        wake_signal_id="",
                        wake_signal_error=_load_error(
                            exc, "raise_event.append_observation_with_wake"
                        ),
                    ),
                )
            return _event_error(
                "raise_event",
                "observation_write_failed",
                "观察事件写入失败；这不是没有事件，而是会话事件账本写入失败。",
                load_error=_load_error(exc, "raise_event.append_observation"),
            )
        return _event_tool_result(
            "raise_event",
            _EventResultRefs(
                thread_id=thread_id,
                task_id=task_id,
                observation_id=observation.observation_id,
                wake_signal_id=wake_signal_id,
            ),
        )


def _event_needs_wake(
    params: dict[str, object],
    *,
    source_agent_id: str,
    root_task_id: str,
) -> bool:
    if (
        str(params.get("urgency") or "").strip().lower() != "urgent"
        and not _boolish(params.get("requires_main_agent"))
    ):
        return False
    requested_task_id = str(
        params.get("task_id")
        or params.get("root_task_id")
        or ""
    ).strip()
    if (
        requested_task_id
        and source_agent_id == requested_task_id
        and root_task_id == requested_task_id
    ):
        # The root Agent is already executing the exact task it would wake.
        # All three structured identities must agree. A missing child lineage
        # can temporarily make observation.source == observation.root; that is
        # not enough to suppress a real child event. Keep the observation for
        # auditability, but do not enqueue a second owner turn for a proven
        # root self-event that can duplicate the finding or narrate stale work.
        return False
    return True


def _resolve_event_thread(
    agent: SimpleAgent,
    params: dict[str, object],
    *,
    tool_name: str,
) -> tuple[str, str] | ToolHandlerOutcome:
    thread_id = str(params.get("thread_id") or "").strip()
    task_id = str(params.get("task_id") or params.get("root_task_id") or "").strip()
    if thread_id:
        thread = _load_event_thread(agent, thread_id, context="raise_event.load_thread", tool_name=tool_name)
        if isinstance(thread, ToolHandlerOutcome):
            return thread
        if thread is None:
            return _event_error(tool_name, "unknown_thread", f"unknown conversation thread: {thread_id}")
        return thread_id, task_id
    if task_id:
        try:
            thread = agent.conversation_store.thread_for_task(task_id)
        except Exception as exc:
            return _event_error(
                tool_name,
                "task_thread_lookup_failed",
                f"conversation task binding lookup failed: {task_id}",
                load_error=_load_error(exc, "raise_event.thread_for_task"),
            )
        if thread is not None:
            return thread.thread_id, task_id
        linked = _thread_from_subagent_task(agent, task_id, tool_name=tool_name)
        if isinstance(linked, ToolHandlerOutcome):
            return linked
        if linked:
            return linked, task_id
    return _event_error(
        tool_name,
        "thread_required",
        "thread_id is required unless task_id is bound to a conversation thread",
    )


def _thread_from_subagent_task(agent: SimpleAgent, task_id: str, *, tool_name: str) -> str | ToolHandlerOutcome:
    try:
        task = agent.subagents.load(task_id)
    except Exception as exc:
        return _event_error(
            tool_name,
            "subagent_task_load_failed",
            f"subagent task lookup failed while resolving event thread: {task_id}",
            load_error=_load_error(exc, "raise_event.subagents.load"),
        )
    attrs = getattr(task, "attributes", {}) or {}
    if not isinstance(attrs, dict):
        return ""
    thread_id = str(attrs.get("conversation_thread_id") or "").strip()
    if thread_id:
        thread = _load_event_thread(agent, thread_id, context="raise_event.load_linked_thread", tool_name=tool_name)
        if isinstance(thread, ToolHandlerOutcome):
            return thread
        if thread is not None:
            return thread_id
    return ""


def _load_event_thread(
    agent: SimpleAgent,
    thread_id: str,
    *,
    context: str,
    tool_name: str,
):
    try:
        thread, load_error = _load_thread_with_report(agent, thread_id)
    except Exception as exc:
        return _event_error(
            tool_name,
            "thread_lookup_failed",
            f"conversation thread lookup failed: {thread_id}",
            load_error=_load_error(exc, context),
        )
    if load_error is not None:
        return _event_error(
            tool_name,
            "thread_lookup_failed",
            f"conversation thread lookup failed: {thread_id}",
            load_error=_report_load_error(load_error, context),
        )
    return thread


def _load_thread_with_report(agent: SimpleAgent, thread_id: str):
    if callable(getattr(agent.conversation_store, "load_thread_report", None)):
        return agent.conversation_store.load_thread_report(thread_id)
    return agent.conversation_store.load_thread(thread_id), None


def _event_lineage_defaults(agent: SimpleAgent, task_id: str) -> dict[str, object]:
    task_id = str(task_id or "").strip()
    if not task_id:
        return {"source_agent_id": "", "parent_agent_id": "", "root_task_id": ""}
    try:
        task = agent.subagents.load(task_id)
    except Exception as exc:
        return {
            "source_agent_id": task_id,
            "parent_agent_id": "",
            "root_task_id": task_id,
            "lineage_load_error": runtime_error_report(exc, context="raise_event.lineage.subagents.load"),
        }
    return {
        "source_agent_id": str(getattr(task, "id", "") or task_id),
        "parent_agent_id": str(getattr(task, "parent_id", "") or ""),
        "root_task_id": str(getattr(task, "root_id", "") or getattr(task, "id", "") or task_id),
    }


# 内部失败 code（如 thread_lookup_failed）→ 错误分类码的分流：硬编码 TOOL_INVALID_ARGUMENTS
# 会把运行时 lookup/load 异常说成"参数不合法"，把模型引去反复改参数而非重试/补必填参数。
# 缺省回落 TOOL_INVALID_ARGUMENTS（真参数无效语义，retryable + 修参数）。
_EVENT_ERROR_CODE_BY_INTERNAL: dict[str, str] = {
    # 运行时写入/查找/加载异常（账本写失败、thread_for_task/subagents.load/load_thread 抛错）→
    # 可恢复执行异常，应重试，而非纠结参数格式。
    "observation_write_failed": "TOOL_EXECUTION_FAILED",
    "task_thread_lookup_failed": "TOOL_EXECUTION_FAILED",
    "thread_lookup_failed": "TOOL_EXECUTION_FAILED",
    "subagent_task_load_failed": "TOOL_EXECUTION_FAILED",
    # 既没有 thread_id 也没有可绑定的 task_id → 缺必填参数，补 thread_id/task_id 后重试。
    "thread_required": "TOOL_PARAMETER_REQUIRED",
    # 提供了 thread_id 但解析不到对应会话线程 → 改用正确 thread_id 可修（真参数无效语义）。
    "unknown_thread": "TOOL_INVALID_ARGUMENTS",
}


def _event_error(
    tool: str,
    code: str,
    message: str,
    *,
    load_error: dict[str, object] | None = None,
) -> ToolHandlerOutcome:
    payload = {"ok": False, "error": code, "message": message}
    if load_error:
        payload["load_error"] = load_error
    error_code = _EVENT_ERROR_CODE_BY_INTERNAL.get(code, "TOOL_INVALID_ARGUMENTS")
    return ToolHandlerOutcome(tool, False, json.dumps(payload, ensure_ascii=False, indent=2), error_code=error_code)


def _load_error(exc: BaseException, context: str) -> dict[str, object]:
    return runtime_error_report(exc, context=context)


def _report_load_error(report: dict[str, object], context: str) -> dict[str, object]:
    payload = dict(report)
    payload["read_context"] = payload.get("context", "")
    payload["context"] = context
    return payload


def _event_tool_result(tool: str, refs: _EventResultRefs) -> ToolHandlerOutcome:
    payload = {
        "ok": True,
        "thread_id": refs.thread_id,
        "task_id": refs.task_id,
        "observation_id": refs.observation_id,
        "wake_signal_id": refs.wake_signal_id,
    }
    if refs.wake_signal_error:
        payload["wake_signal_error"] = refs.wake_signal_error
    return ToolHandlerOutcome(tool, True, json.dumps(payload, ensure_ascii=False, indent=2))


def _string_values(value: object) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item or "").strip()]
    if value is None:
        return []
    text = str(value).strip()
    return [text] if text else []


def _metadata_values(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _boolish(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true"}
