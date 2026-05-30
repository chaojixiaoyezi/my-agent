# LLM: Conversation event tools for descendant agents; keep these as ledger writes, not dispatch gates.
# 模块用途: 让子/孙代理写 observation 或紧急唤醒主代理，同时保留 refs-first 事件结果。

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..tools import BaseTool, ToolExecutionResult
from .orchestration_tool_specs import build_raise_event_spec

if TYPE_CHECKING:
    from ..core import SimpleAgent


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


class RaiseEventTool(BaseTool):
    # LLM: RaiseEventTool merges ordinary observations and urgent wake events into one model action.
    # 类用途: 记录子/孙代理事件；urgent 或 requires_main_agent 时同步写唤醒信号。
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_raise_event_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        resolved = _resolve_event_thread(self.agent, params, tool_name="raise_event")
        if isinstance(resolved, ToolExecutionResult):
            return resolved
        thread_id, task_id = resolved
        lineage = _event_lineage_defaults(self.agent, task_id)
        urgency = str(params.get("urgency") or "normal")
        requires_main_agent = _boolish(params.get("requires_main_agent")) or urgency.strip().lower() == "urgent"
        observation = self.agent.conversation_store.append_observation({'thread_id': thread_id, 'event_type': str(params.get("event_type") or "observation"), 'summary': str(params.get("summary") or ""), 'urgency': urgency, 'severity': str(params.get("severity") or ""), 'source_agent_id': str(params.get("source_agent_id") or lineage["source_agent_id"]), 'parent_agent_id': str(params.get("parent_agent_id") or lineage["parent_agent_id"]), 'root_task_id': str(params.get("root_task_id") or lineage["root_task_id"]), 'evidence_refs': _string_values(params.get("evidence_refs")), 'requires_main_agent': requires_main_agent, 'requires_llm_report': _boolish(params.get("requires_llm_report")), 'metadata': _metadata_values(params.get("metadata"))})
        wake_signal_id = _maybe_raise_event_wake(
            _ObservationWakeRequest(self.agent, params, thread_id, observation)
        )
        return _event_tool_result("raise_event", _EventResultRefs(
            thread_id=thread_id,
            task_id=task_id,
            observation_id=observation.observation_id,
            wake_signal_id=wake_signal_id,
        ))


def _maybe_raise_event_wake(request: _ObservationWakeRequest) -> str:
    if str(request.params.get("urgency") or "").strip().lower() != "urgent" and not _boolish(request.params.get("requires_main_agent")):
        return ""
    signal = request.agent.conversation_store.raise_wake_signal({'thread_id': request.thread_id, 'observation': request.observation, 'reason': str(request.params.get("event_type") or "urgent_observation"), 'dedupe_key': str(request.params.get("dedupe_key") or "")})
    return signal.wake_signal_id


def _resolve_event_thread(
    agent: SimpleAgent,
    params: dict[str, object],
    *,
    tool_name: str,
) -> tuple[str, str] | ToolExecutionResult:
    thread_id = str(params.get("thread_id") or "").strip()
    task_id = str(params.get("task_id") or params.get("root_task_id") or "").strip()
    if thread_id:
        if agent.conversation_store.load_thread(thread_id) is None:
            return _event_error(tool_name, "unknown_thread", f"unknown conversation thread: {thread_id}")
        return thread_id, task_id
    if task_id:
        thread = agent.conversation_store.thread_for_task(task_id)
        if thread is not None:
            return thread.thread_id, task_id
        linked = _thread_from_subagent_task(agent, task_id)
        if linked:
            return linked, task_id
    return _event_error(
        tool_name,
        "thread_required",
        "thread_id is required unless task_id is bound to a conversation thread",
    )


def _thread_from_subagent_task(agent: SimpleAgent, task_id: str) -> str:
    try:
        task = agent.subagents.load(task_id)
    except Exception:
        return ""
    attrs = getattr(task, "attributes", {}) or {}
    if not isinstance(attrs, dict):
        return ""
    thread_id = str(attrs.get("conversation_thread_id") or "").strip()
    if thread_id and agent.conversation_store.load_thread(thread_id) is not None:
        return thread_id
    return ""


def _event_lineage_defaults(agent: SimpleAgent, task_id: str) -> dict[str, str]:
    task_id = str(task_id or "").strip()
    if not task_id:
        return {"source_agent_id": "", "parent_agent_id": "", "root_task_id": ""}
    try:
        task = agent.subagents.load(task_id)
    except Exception:
        return {"source_agent_id": task_id, "parent_agent_id": "", "root_task_id": task_id}
    return {
        "source_agent_id": str(getattr(task, "id", "") or task_id),
        "parent_agent_id": str(getattr(task, "parent_id", "") or ""),
        "root_task_id": str(getattr(task, "root_id", "") or getattr(task, "id", "") or task_id),
    }


def _event_error(tool: str, code: str, message: str) -> ToolExecutionResult:
    payload = {"ok": False, "error": code, "message": message}
    return ToolExecutionResult(tool, False, json.dumps(payload, ensure_ascii=False, indent=2))


def _event_tool_result(tool: str, refs: _EventResultRefs) -> ToolExecutionResult:
    payload = {
        "ok": True,
        "thread_id": refs.thread_id,
        "task_id": refs.task_id,
        "observation_id": refs.observation_id,
        "wake_signal_id": refs.wake_signal_id,
    }
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
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}
