
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ....runtime_errors import runtime_error_report
from ....tooling.models import BaseTool, ToolExecutionResult
from ..tool_specs import build_raise_event_spec

if TYPE_CHECKING:
    from ....core import SimpleAgent


@dataclass(frozen=True)
class _ObservationWakeRequest:
    agent: SimpleAgent
    params: dict[str, object]
    thread_id: str
    observation: object


@dataclass(frozen=True)
class _EventResultRefs:
    thread_id: str
    task_id: str
    observation_id: str
    wake_signal_id: str
    wake_signal_error: dict[str, object] | None = None


class RaiseEventTool(BaseTool):
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_raise_event_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        resolved = _resolve_event_thread(self.agent, params, tool_name="raise_event")
        if isinstance(resolved, ToolExecutionResult):
            return resolved
        thread_id, task_id = resolved
        lineage = _event_lineage_defaults(self.agent, task_id)
        lineage_load_error = lineage.pop("lineage_load_error", None)
        metadata = _metadata_values(params.get("metadata"))
        if lineage_load_error:
            metadata["lineage_load_error"] = lineage_load_error
        urgency = str(params.get("urgency") or "normal")
        requires_main_agent = _boolish(params.get("requires_main_agent")) or urgency.strip().lower() == "urgent"
        try:
            observation = self.agent.conversation_store.append_observation({
                'thread_id': thread_id,
                'event_type': str(params.get("event_type") or "observation"),
                'summary': str(params.get("summary") or ""),
                'urgency': urgency,
                'severity': str(params.get("severity") or ""),
                'source_agent_id': str(params.get("source_agent_id") or lineage["source_agent_id"]),
                'parent_agent_id': str(params.get("parent_agent_id") or lineage["parent_agent_id"]),
                'root_task_id': str(params.get("root_task_id") or lineage["root_task_id"]),
                'evidence_refs': _string_values(params.get("evidence_refs")),
                'requires_main_agent': requires_main_agent,
                'requires_llm_report': _boolish(params.get("requires_llm_report")),
                'metadata': metadata,
            })
        except Exception as exc:
            return _event_error(
                "raise_event",
                "observation_write_failed",
                "观察事件写入失败；这不是没有事件，而是会话事件账本写入失败。",
                load_error=_load_error(exc, "raise_event.append_observation"),
            )
        wake_signal_id, wake_signal_error = _maybe_raise_event_wake(
            _ObservationWakeRequest(self.agent, params, thread_id, observation)
        )
        return _event_tool_result("raise_event", _EventResultRefs(
            thread_id=thread_id,
            task_id=task_id,
            observation_id=observation.observation_id,
            wake_signal_id=wake_signal_id,
            wake_signal_error=wake_signal_error,
        ))


def _maybe_raise_event_wake(request: _ObservationWakeRequest) -> tuple[str, dict[str, object] | None]:
    if str(request.params.get("urgency") or "").strip().lower() != "urgent" and not _boolish(request.params.get("requires_main_agent")):
        return "", None
    try:
        signal = request.agent.conversation_store.raise_wake_signal({'thread_id': request.thread_id, 'observation': request.observation, 'reason': str(request.params.get("event_type") or "urgent_observation"), 'dedupe_key': str(request.params.get("dedupe_key") or "")})
    except Exception as exc:
        return "", runtime_error_report(exc, context="raise_event.raise_wake_signal")
    return signal.wake_signal_id, None


def _resolve_event_thread(
    agent: SimpleAgent,
    params: dict[str, object],
    *,
    tool_name: str,
) -> tuple[str, str] | ToolExecutionResult:
    thread_id = str(params.get("thread_id") or "").strip()
    task_id = str(params.get("task_id") or params.get("root_task_id") or "").strip()
    if thread_id:
        thread = _load_event_thread(agent, thread_id, context="raise_event.load_thread", tool_name=tool_name)
        if isinstance(thread, ToolExecutionResult):
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
        if isinstance(linked, ToolExecutionResult):
            return linked
        if linked:
            return linked, task_id
    return _event_error(
        tool_name,
        "thread_required",
        "thread_id is required unless task_id is bound to a conversation thread",
    )


def _thread_from_subagent_task(agent: SimpleAgent, task_id: str, *, tool_name: str) -> str | ToolExecutionResult:
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
        if isinstance(thread, ToolExecutionResult):
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


def _event_error(
    tool: str,
    code: str,
    message: str,
    *,
    load_error: dict[str, object] | None = None,
) -> ToolExecutionResult:
    payload = {"ok": False, "error": code, "message": message}
    if load_error:
        payload["load_error"] = load_error
    return ToolExecutionResult(tool, False, json.dumps(payload, ensure_ascii=False, indent=2))


def _load_error(exc: BaseException, context: str) -> dict[str, object]:
    return runtime_error_report(exc, context=context)


def _report_load_error(report: dict[str, object], context: str) -> dict[str, object]:
    payload = dict(report)
    payload["read_context"] = payload.get("context", "")
    payload["context"] = context
    return payload


def _event_tool_result(tool: str, refs: _EventResultRefs) -> ToolExecutionResult:
    payload = {
        "ok": True,
        "thread_id": refs.thread_id,
        "task_id": refs.task_id,
        "observation_id": refs.observation_id,
        "wake_signal_id": refs.wake_signal_id,
    }
    if refs.wake_signal_error:
        payload["wake_signal_error"] = refs.wake_signal_error
    return ToolExecutionResult(tool, True, json.dumps(payload, ensure_ascii=False, indent=2))


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
