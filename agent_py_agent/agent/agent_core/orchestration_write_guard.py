from __future__ import annotations

"""LLM: preflight checks for write-capable orchestration tasks.

Subagent tool execution still owns the real write boundary. This module only catches
obvious outside-workspace write requests before a work order is created.
"""

import re
from pathlib import Path

WRITE_SUBAGENT_TOOLS = {"write_file", "append_file", "replace_in_file"}

_ABSOLUTE_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/][^\s\"'<>|]+|~[\\/][^\s\"'<>|]+|/[^\s\"'<>|]+)")
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_WRITE_INTENT_WORDS = (
    "写",
    "创建",
    "生成",
    "保存",
    "修改",
    "write",
    "create",
    "generate",
    "save",
    "modify",
)


def external_write_target_error(agent, goal: str, allowed_tools: list[str]) -> str:
    if not WRITE_SUBAGENT_TOOLS.intersection(allowed_tools):
        return ""
    if not _goal_has_write_intent(goal):
        return ""
    workspace_roots = getattr(agent.subagents, "workspace_roots", [agent.subagents.workspace_root])
    external_paths = _external_absolute_paths(goal, workspace_roots)
    if not external_paths:
        return ""
    preview = ", ".join(external_paths[:3])
    return (
        "子代理写入目标在当前工作区外，已拒绝创建任务，避免后续 dispatch 超时或越权写入。"
        f" target={preview} workspace_root={agent.subagents.workspace_root}。"
        " 请把目标目录放入 workspace_root，或先在工作区内生成文件后再人工移动。"
    )


def _goal_has_write_intent(goal: str) -> bool:
    lowered = goal.lower()
    return any(word in lowered for word in _WRITE_INTENT_WORDS)


def _external_absolute_paths(goal: str, workspace_roots: Path | list[Path]) -> list[str]:
    roots = _workspace_roots(workspace_roots)
    external: list[str] = []
    for match in _ABSOLUTE_PATH_RE.finditer(goal):
        raw = _trim_path_candidate(match.group())
        if raw and _is_external_absolute_path(raw, roots) and raw not in external:
            external.append(raw)
    return external


def _trim_path_candidate(raw: str) -> str:
    return raw.strip().rstrip(".,;:，。；：、)]}）】")


def _is_external_absolute_path(raw: str, workspace_roots: Path | list[Path]) -> bool:
    text = raw.replace("\\", "/")
    if _WINDOWS_ABSOLUTE_RE.match(raw) and not Path(text).is_absolute():
        return True
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        return False
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        return True
    return not any(_is_relative_to(resolved, root) for root in _workspace_roots(workspace_roots))


def _workspace_roots(workspace_roots: Path | list[Path]) -> list[Path]:
    raw_roots = workspace_roots if isinstance(workspace_roots, list) else [workspace_roots]
    return [Path(root).resolve(strict=False) for root in raw_roots]


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
