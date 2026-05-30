# LLM: task_progress is one generic model-facing ledger tool for long work.
# 模块用途: 让模型记录/读取自己的任务进度清单；不触发验收、不调度子代理、不改变任务状态机。

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from ..task_progress import read_task_progress, write_task_progress
from ..tools import BaseTool, ToolExecutionResult
from .orchestration_tool_specs import build_task_progress_spec
from .runner_context import current_subagent_run_id

if TYPE_CHECKING:
    from ..core import SimpleAgent


class TaskProgressTool(BaseTool):
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_task_progress_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        action = _normalized_action(params.get("action"))
        run_id = _target_run_id(self.agent, params, allow_explicit=action == "read")
        if not run_id:
            run_id = "main"
        root = Path(getattr(self.agent, "root", "."))
        if action == "update":
            payload = write_task_progress(root, run_id, params)
            payload = _with_write_feedback(payload)
        else:
            payload = read_task_progress(root, run_id)
        return ToolExecutionResult("task_progress", True, json.dumps(payload, ensure_ascii=False, indent=2))


def _normalized_action(value: object) -> str:
    action = str(value or "read").strip().lower()
    if action in {
        "update",
        "create",
        "init",
        "initialize",
        "start",
        "begin",
        "set",
        "save",
        "record",
        "write",
    }:
        return "update"
    return "read"


def _target_run_id(agent: object, params: dict[str, object], *, allow_explicit: bool) -> str:
    explicit = str(params.get("run_id") or "").strip()
    if explicit and allow_explicit:
        return explicit
    scoped = _scope_run_id(params.get("__run_scope"))
    if scoped:
        return scoped
    return str(
        current_subagent_run_id(agent)
        or getattr(agent, "_main_agent_run_id", "")
        or getattr(agent, "_current_request_id", "")
        or "main"
    ).strip()


def _scope_run_id(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    return str(value.get("run_id") or value.get("task_id") or value.get("request_id") or "").strip()


def _with_write_feedback(payload: dict[str, object]) -> dict[str, object]:
    hints = payload.get("quality_hints")
    if not isinstance(hints, dict) or not hints.get("messages"):
        return payload
    feedback = {
        "severity": "soft",
        "blocking": False,
        "message": str(hints.get("soft_prompt") or "软提醒：有些进度项缺少证据，建议补上文件、来源或产物引用。"),
    }
    return {**payload, "soft_feedback": feedback}


__all__ = ["TaskProgressTool"]
