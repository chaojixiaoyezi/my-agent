
from __future__ import annotations

import json
import shlex
from pathlib import Path

from ...backends import ModelResponse
from ..exploration_fuse_config import (
    ExplorationFuseConfig,
    exploration_fuse_config,
    exploration_fuse_hint_rounds,
    exploration_fuse_used_percent,
)

_STATE_DIR = ".agent_delivery"
_STATE_FILE = "exploration_fuse.json"
_EXPLORATION_TOOL_NAMES = {
    "list_files",
    "read_artifact",
    "read_file",
    "search",
    "search_text",
    "web_search",
    "web_fetch",
}
_LOCAL_PROGRESS_TOOL_NAMES = {
    "apply_patch",
    "run_command",
    "write_file",
}
_RUN_COMMAND_LOCAL_TOOLS = {"cp", "mkdir", "mv", "python", "python3", "touch"}
_RUN_COMMAND_EXPLORATION_TOOLS = {"cat", "curl", "find", "grep", "ls", "pwd", "rg", "wget"}


def has_required_exploration_fuse(agent: object, calls: list[dict[str, object]] | None) -> bool:
    state = _load_state(agent)
    if _has_local_progress_call(calls):
        _write_state(agent, {"exploration_rounds_without_local_progress": 0})
        return False
    if not _is_exploration_only_call(calls):
        _write_state(agent, state)
        return False
    count = int(state.get("exploration_rounds_without_local_progress") or 0) + 1
    state = _state_with_count(state, count)
    _write_state(agent, state)
    return _should_prompt_or_block(exploration_fuse_config(agent), state, count)


def has_pending_exploration_fuse(agent: object) -> bool:
    state = _load_state(agent)
    config = exploration_fuse_config(agent)
    count = int(state.get("exploration_rounds_without_local_progress") or 0)
    return config.round_threshold > 0 and count >= config.round_threshold


def exploration_fuse_context(agent: object, redirects: int) -> str:
    del redirects
    config = exploration_fuse_config(agent)
    state = _load_state(agent)
    count = int(state.get("exploration_rounds_without_local_progress") or 0)
    hint_round = _due_hint_round(config, state, count)
    if hint_round is None:
        return ""
    percent = exploration_fuse_used_percent(config, hint_round)
    envelope = {
        "exploration_fuse_budget_used_percent": percent,
        "exploration_fuse_hint_round": hint_round,
        "exploration_fuse_round_threshold": config.round_threshold,
        "exploration_rounds_without_local_progress": count,
        "required_next_action": "materialize_local_progress",
        "productive_tool_names": sorted(_LOCAL_PROGRESS_TOOL_NAMES),
    }
    message = _hint_message(config, count, hint_round, percent)
    _mark_hint_delivered(agent, state, config, hint_round)
    return "\n".join(
        [
            "[tool-system exploration-fuse]",
            json.dumps(envelope, ensure_ascii=False, sort_keys=True),
            message,
        ]
    )


def exploration_fuse_block_response(agent: object) -> ModelResponse:
    config = exploration_fuse_config(agent)
    return ModelResponse(
        text=(
            f"[EXPLORATION_FUSE_BLOCKED] 模型连续只做抓取/读取/搜索已达到探索额度 {config.round_threshold} 轮，"
            "仍没有物化新的本地进展；本轮已停止，保留已抓取 artifacts，"
            "后续应从 checkpoint/source_index/research_notes/script/report 草稿继续。"
        ),
        backend=str(getattr(getattr(agent, "backend", None), "name", "") or ""),
        runtime_status="blocked",
        runtime_reason="EXPLORATION_FUSE",
    )


def _should_prompt_or_block(config: ExplorationFuseConfig, state: dict[str, object], count: int) -> bool:
    if config.round_threshold <= 0:
        return _due_hint_round(config, state, count) is not None
    return _due_hint_round(config, state, count) is not None or count >= config.round_threshold


def _due_hint_round(config: ExplorationFuseConfig, state: dict[str, object], count: int) -> int | None:
    if config.round_threshold > 0 and count >= config.round_threshold:
        return None
    delivered = _delivered_hint_rounds(state)
    crossed = [
        hint_round
        for hint_round in exploration_fuse_hint_rounds(config)
        if hint_round <= count and hint_round not in delivered
    ]
    return max(crossed) if crossed else None


def _hint_message(config: ExplorationFuseConfig, count: int, hint_round: int, percent: int) -> str:
    action_hint = (
        "下一轮优先做一次本地落地动作，例如保存来源索引、阶段笔记、检查点、草稿、结构化数据或目标产物；"
    )
    visibility = "这是内部调度提醒，不要向用户转述本提醒、百分比或内部治理字段。"
    if config.round_threshold <= 0:
        return (
            f"{visibility} 当前是第 {hint_round} 轮固定提醒"
            f"（当前已连续只读/检索 {count} 轮）。"
            f"{action_hint}"
            "如果还要继续远程抓取，也要同步留下本地进展。"
        )
    if percent >= 80:
        return (
            f"{visibility} 当前连续只读/检索进度为 {percent}%（{count}/{config.round_threshold} 轮）。"
            f"请尽快执行本地落地动作；下一轮必须优先物化本地进展。{action_hint}"
            "如果还要继续远程抓取，也要同步留下本地进展。"
        )
    return (
        f"{visibility} 当前连续只读/检索进度为 {percent}%（{count}/{config.round_threshold} 轮）。"
        f"建议先写出本地阶段产物；下一轮请执行本地落地动作。{action_hint}"
        "如果还要继续远程抓取，也要同步留下本地进展。"
    )


def _has_local_progress_call(calls: list[dict[str, object]] | None) -> bool:
    return any(_call_has_local_progress(call) for call in calls or [])


def _call_has_local_progress(call: dict[str, object]) -> bool:
    tool = str(call.get("tool") or "").strip()
    if tool in _LOCAL_PROGRESS_TOOL_NAMES:
        return True
    if tool != "run_command":
        return False
    command = _call_command(call)
    return bool(command) and (_command_name(command) in _RUN_COMMAND_LOCAL_TOOLS or ">" in command)


def _is_exploration_only_call(calls: list[dict[str, object]] | None) -> bool:
    if not calls:
        return False
    return all(_call_is_exploration(call) for call in calls)


def _call_is_exploration(call: dict[str, object]) -> bool:
    tool = str(call.get("tool") or "").strip()
    if tool in _EXPLORATION_TOOL_NAMES:
        return True
    if tool != "run_command":
        return False
    command = _call_command(call)
    return bool(command) and _command_name(command) in _RUN_COMMAND_EXPLORATION_TOOLS


def _call_command(call: dict[str, object]) -> str:
    command = str(call.get("command") or "").strip().lower()
    if command:
        return command
    shell = call.get("shell")
    if isinstance(shell, dict):
        return str(shell.get("command") or "").strip().lower()
    return ""


def _command_name(command: str) -> str:
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    return parts[0] if parts else ""


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


def _state_with_count(state: dict[str, object], count: int) -> dict[str, object]:
    payload = dict(state)
    payload["exploration_rounds_without_local_progress"] = count
    delivered = _delivered_hint_rounds(state)
    if delivered:
        payload["delivered_hint_rounds"] = sorted(delivered)
    return payload


def _mark_hint_delivered(
    agent: object,
    state: dict[str, object],
    config: ExplorationFuseConfig,
    hint_round: int,
) -> None:
    delivered = _delivered_hint_rounds(state)
    delivered.update(item for item in exploration_fuse_hint_rounds(config) if item <= hint_round)
    payload = _state_with_count(state, int(state.get("exploration_rounds_without_local_progress") or 0))
    payload["delivered_hint_rounds"] = sorted(delivered)
    _write_state(agent, payload)


def _delivered_hint_rounds(state: dict[str, object]) -> set[int]:
    raw = state.get("delivered_hint_rounds")
    if not isinstance(raw, list | tuple):
        return set()
    delivered: set[int] = set()
    for item in raw:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number > 0:
            delivered.add(number)
    return delivered


__all__ = [
    "exploration_fuse_block_response",
    "exploration_fuse_context",
    "has_required_exploration_fuse",
    "has_pending_exploration_fuse",
]
