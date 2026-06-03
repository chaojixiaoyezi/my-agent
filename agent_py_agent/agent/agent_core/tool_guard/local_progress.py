
from __future__ import annotations

import json
from pathlib import Path

from .._runtime_params import ToolLoopExecuteParams
from ..exploration_fuse_config import exploration_fuse_config
from .local_progress_hints import (
    HintDeliveryInput,
    due_hint_round,
    hint_message,
    mark_hint_delivered,
    recovery_signature,
    should_prompt,
)

_STATE_DIR = ".agent_delivery"
_STATE_FILE = "local_progress_guard.json"
_LOCAL_PROGRESS_TOOL_NAMES = {
    "apply_patch",
    "run_command",
    "write_file",
}
_EXPLORATION_TOOL_NAMES = {
    "list_files",
    "list_tools",
    "read_artifact",
    "read_file",
    "search",
    "web_fetch",
    "web_search",
}
_RUN_COMMAND_LOCAL_MUTATION_PREFIXES = ("mkdir ", "mkdir -p", "touch ", "cp ", "mv ", "tee ")
_RUN_COMMAND_EXPLORATION_PREFIXES = ("curl ", "find ", "ls", "pwd", "rg ", "cat ", "wget ")


def has_required_local_progress_guard(agent: object, params: ToolLoopExecuteParams, calls: list[dict[str, object]] | None) -> bool:
    payload = _guard_payload(agent)
    if not payload:
        return False
    state = _load_state(agent)
    recovery_sig = recovery_signature(params)
    fingerprint = str(payload.get("work_progress_fingerprint") or "")
    failure_fingerprint = str(payload.get("failure_fingerprint") or "")
    if (
        str(state.get("work_progress_fingerprint") or "") != fingerprint
        or str(state.get("failure_fingerprint") or "") != failure_fingerprint
        or str(state.get("recovery_signature") or "") != recovery_sig
    ):
        state = {
            "failure_fingerprint": failure_fingerprint,
            "exploration_rounds_without_local_progress": 0,
            "recovery_signature": recovery_sig,
            "work_progress_fingerprint": fingerprint,
        }
    if _is_local_progressive_call(payload, calls):
        state["exploration_rounds_without_local_progress"] = 0
        _write_state(agent, state)
        return False
    if not _is_exploration_only_call(calls):
        _write_state(agent, state)
        return False
    count = int(state.get("exploration_rounds_without_local_progress") or 0) + 1
    state["exploration_rounds_without_local_progress"] = count
    _write_state(agent, state)
    config = exploration_fuse_config(agent)
    return should_prompt(config, state, count)


def local_progress_guard_context(agent: object, redirects: int) -> str:
    del redirects
    payload = _guard_payload(agent)
    config = exploration_fuse_config(agent)
    if not payload:
        return ""
    state = _load_state(agent)
    count = int(state.get("exploration_rounds_without_local_progress") or 0)
    hint_round = due_hint_round(config, state, count)
    if hint_round is None:
        return ""
    envelope = {
        "exploration_rounds_without_local_progress": count,
        "failure_fingerprint": payload.get("failure_fingerprint", ""),
        "local_progress_hint_round": hint_round,
        "local_progress_hint_interval": config.local_progress_unlimited_hint_interval,
        "pending_materialization_targets": payload.get("pending_materialization_targets", []),
        "recovery_actions": payload.get("recovery_actions", []),
        "work_progress_fingerprint": payload.get("work_progress_fingerprint", ""),
    }
    mark_hint_delivered(HintDeliveryInput(agent, state, config, hint_round, _write_state))
    return "\n".join(
        [
            "[tool-system local-progress-guard]",
            json.dumps(envelope, ensure_ascii=False, sort_keys=True),
            hint_message(count, hint_round),
        ]
    )

def reset_local_progress_guard(agent: object, params: ToolLoopExecuteParams | None = None) -> None:
    recovery_sig = recovery_signature(params) if params is not None else ""
    _write_state(
        agent,
        {
            "failure_fingerprint": "",
            "exploration_rounds_without_local_progress": 0,
            "recovery_signature": recovery_sig,
            "work_progress_fingerprint": "",
        },
    )


def _guard_payload(agent: object) -> dict[str, object]:
    report = _closeout_report(agent)
    if not report or report.get("ok") is True:
        return {}
    progress = report.get("delivery_progress")
    if not isinstance(progress, dict):
        return {}
    fingerprint = str(progress.get("work_progress_fingerprint") or "")
    failure_fingerprint = str(progress.get("failure_fingerprint") or "")
    if not fingerprint or not failure_fingerprint:
        return {}
    recovery_actions = progress.get("recovery_actions")
    pending_targets = progress.get("pending_materialization_targets")
    if not isinstance(recovery_actions, list):
        recovery_actions = []
    if not isinstance(pending_targets, list):
        pending_targets = []
    if not recovery_actions and not pending_targets:
        return {}
    return {
        "failure_fingerprint": failure_fingerprint,
        "pending_materialization_targets": pending_targets,
        "recovery_actions": [item for item in recovery_actions if isinstance(item, dict)],
        "work_progress_fingerprint": fingerprint,
    }


def _closeout_report(agent: object) -> dict[str, object]:
    path = Path(getattr(agent, "root", ".")).resolve() / ".agent_delivery" / "closeout.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _state_path(agent: object) -> Path:
    return Path(getattr(agent, "root", ".")).resolve() / _STATE_DIR / _STATE_FILE


def _load_state(agent: object) -> dict[str, object]:
    path = _state_path(agent)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_state(agent: object, payload: dict[str, object]) -> None:
    path = _state_path(agent)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _is_local_progressive_call(payload: dict[str, object], calls: list[dict[str, object]] | None) -> bool:
    if not calls:
        return False
    action_tools = {
        str(item.get(key) or "").strip()
        for item in payload.get("recovery_actions", [])
        if isinstance(item, dict)
        for key in ("builder_tool", "writer_tool")
    }
    productive_tools = {tool for tool in action_tools if tool} | _LOCAL_PROGRESS_TOOL_NAMES
    return any(_call_is_local_progressive(call, productive_tools) for call in calls)


def _call_is_local_progressive(call: dict[str, object], productive_tools: set[str]) -> bool:
    tool = str(call.get("tool") or "").strip()
    if tool in productive_tools:
        return True
    if tool != "run_command":
        return False
    command = _call_command(call)
    if not command:
        return False
    return any(command.startswith(prefix) for prefix in _RUN_COMMAND_LOCAL_MUTATION_PREFIXES) or ">" in command


def _is_exploration_only_call(calls: list[dict[str, object]] | None) -> bool:
    if not calls:
        return True
    return all(_call_is_exploration_only(call) for call in calls)


def _call_is_exploration_only(call: dict[str, object]) -> bool:
    tool = str(call.get("tool") or "").strip()
    if tool in _EXPLORATION_TOOL_NAMES:
        return True
    if tool != "run_command":
        return False
    command = _call_command(call)
    if not command:
        return False
    return any(command.startswith(prefix) for prefix in _RUN_COMMAND_EXPLORATION_PREFIXES)


def _call_command(call: dict[str, object]) -> str:
    command = str(call.get("command") or "").strip().lower()
    if command:
        return command
    shell = call.get("shell")
    if isinstance(shell, dict):
        return str(shell.get("command") or "").strip().lower()
    return ""
