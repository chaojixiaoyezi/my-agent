
from __future__ import annotations

"""Runtime handoff facts for compact/resume.

The handoff may point to recent guidance and active child agents, but its
suggestion must not teach the parent to poll subagents every model turn.
Only current protocol states decide whether a child is active; terminal
failure/cancel states stay visible as recent evidence, not runnable work.
"""

import json
from pathlib import Path
from typing import Any

from ..subagents.models import TaskStatus, task_status_in, task_status_reason_code


def build_runtime_handoff(workspace: Path, ids: list[str]) -> dict[str, Any]:
    guidance = _recent_guidance(workspace, ids)
    tree = _agent_tree_handoff(workspace, ids)
    if not guidance and not tree["active_agents"] and tree["counts"]["total"] == 0:
        return {}
    return {
        "schema_version": "runtime_handoff.v1",
        "recent_guidance": guidance,
        "agent_tree": tree,
        "next_suggestion": (
            "继续推进当前任务；只有缺少最新事实、到验收/接管节点或已有状态明显变化时，才查看进度账本和下级状态，避免高频轮询。"
        ),
        "soft_only": True,
    }


def runtime_handoff_payload(value: Any) -> dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    guidance = payload.get("recent_guidance") if isinstance(payload.get("recent_guidance"), list) else []
    tree = payload.get("agent_tree") if isinstance(payload.get("agent_tree"), dict) else {}
    active = tree.get("active_agents") if isinstance(tree.get("active_agents"), list) else []
    recent = tree.get("recent_agents") if isinstance(tree.get("recent_agents"), list) else []
    return {
        "schema_version": str(payload.get("schema_version") or ""),
        "soft_only": bool(payload.get("soft_only", True)),
        "next_suggestion": str(payload.get("next_suggestion") or ""),
        "recent_guidance_count": len(guidance),
        "recent_guidance": [_short_guidance(row) for row in guidance[:5] if isinstance(row, dict)],
        "agent_tree": {
            "counts": dict(tree.get("counts", {}) if isinstance(tree.get("counts"), dict) else {}),
            "active_agents": [_short_agent(row) for row in active[:8] if isinstance(row, dict)],
            "recent_agents": [_short_agent(row) for row in recent[:12] if isinstance(row, dict)],
        },
    }


def render_runtime_handoff_lines(value: Any, *, title: str = "Runtime Handoff") -> list[str]:
    payload = value if isinstance(value, dict) else {}
    guidance = payload.get("recent_guidance") if isinstance(payload.get("recent_guidance"), list) else []
    tree = payload.get("agent_tree") if isinstance(payload.get("agent_tree"), dict) else {}
    active = tree.get("active_agents") if isinstance(tree.get("active_agents"), list) else []
    recent = tree.get("recent_agents") if isinstance(tree.get("recent_agents"), list) else []
    if not guidance and not active and not recent:
        return []
    lines = [f"## {title}", ""]
    suggestion = str(payload.get("next_suggestion") or "")
    if suggestion:
        lines.append(f"- next_suggestion: {suggestion}")
    lines.extend(_guidance_lines(guidance))
    lines.extend(_active_agent_lines(active))
    if not active:
        lines.extend(_recent_agent_lines(recent))
    lines.append("")
    return lines


def _recent_guidance(workspace: Path, ids: list[str]) -> list[dict[str, Any]]:
    guidance_dirs = _guidance_dirs(workspace)
    if not guidance_dirs:
        return []
    id_set = set(ids)
    rows = [
        row
        for path in _guidance_candidate_files(guidance_dirs, ids)
        for row in _read_jsonl_dicts(path)
        if str(row.get("target_id") or "").strip() in id_set
    ]
    rows.sort(key=lambda row: _safe_float(row.get("created_at")), reverse=True)
    return [_guidance_row(row) for row in rows[:8]]


def _guidance_candidate_files(guidance_dirs: list[Path], ids: list[str]) -> list[Path]:
    exact = [
        path
        for guidance_dir in guidance_dirs
        for item_id in ids
        for path in _guidance_exact_paths(guidance_dir, item_id)
        if path.exists()
    ]
    recent = [
        path
        for guidance_dir in guidance_dirs
        for path in sorted(guidance_dir.glob("*.jsonl"))[:32]
    ]
    return _dedupe_paths([*exact, *recent])


def _guidance_dirs(workspace: Path) -> list[Path]:
    candidates = [
        workspace / "guidance",
        workspace / "conversations" / "guidance",
        workspace / "work" / "guidance",
        *sorted((workspace / "workspace" / "runtime" / "workspaces").glob("*/conversations/guidance")),
        *sorted((workspace / "tasks").glob("*/work/guidance")),
        *sorted((workspace / "tasks").glob("*/*/work/guidance")),
    ]
    return _dedupe_paths([path for path in candidates if path.exists() and _inside_workspace(path, workspace)])


def _guidance_exact_paths(guidance_dir: Path, item_id: str) -> list[Path]:
    safe = _safe_file_stem(item_id)
    return [guidance_dir / f"{target_type}.{safe}.jsonl" for target_type in ("thread", "agent_run", "task", "case")]


def _guidance_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "guidance_id": str(row.get("guidance_id") or ""),
        "target_type": str(row.get("target_type") or ""),
        "target_id": str(row.get("target_id") or ""),
        "message": str(row.get("message") or ""),
        "sender": str(row.get("sender") or ""),
        "priority": str(row.get("priority") or ""),
        "created_at": _safe_float(row.get("created_at")),
    }


def _agent_tree_handoff(workspace: Path, ids: list[str]) -> dict[str, Any]:
    id_set = {item for item in ids if item}
    rows = [
        row
        for path in _agent_state_files(workspace, ids)
        if (row := _agent_state_payload(path)) and _agent_related_to_scope(row, id_set)
    ]
    return {
        "source": "workspace_task_agents",
        "counts": _agent_counts(rows),
        "active_agents": [row for row in rows if _active_status(row)][:12],
        "recent_agents": rows[:20],
    }


def _agent_state_files(workspace: Path, ids: list[str]) -> list[Path]:
    paths: list[Path] = []
    for item_id in ids:
        if item_id:
            paths.extend(_task_agent_state_files_for_task(workspace, item_id))
            paths.extend(_task_agent_state_files_for_id(workspace, item_id))
    paths.extend(_recent_task_agent_state_files(workspace))
    return _dedupe_paths([path for path in paths if path.exists() and _inside_workspace(path, workspace)])


def _task_agent_state_files_for_task(workspace: Path, item_id: str) -> list[Path]:
    task_dirs = [
        workspace / "tasks" / item_id,
        *sorted((workspace / "tasks").glob(f"*/{_safe_file_stem(item_id)}")),
    ]
    return [
        state_path
        for task_dir in task_dirs
        for state_path in sorted((task_dir / "work" / "agents").glob("*/canonical_state.json"))
    ]


def _task_agent_state_files_for_id(workspace: Path, item_id: str) -> list[Path]:
    return [
        path / "canonical_state.json"
        for pattern in ("*/work/agents/*", "*/*/work/agents/*")
        for path in sorted((workspace / "tasks").glob(pattern))
        if path.is_dir() and path.name == item_id and (path / "canonical_state.json").exists()
    ]


def _recent_task_agent_state_files(workspace: Path) -> list[Path]:
    tasks_dir = workspace / "tasks"
    if not tasks_dir.exists():
        return []
    paths = [
        path
        for pattern in ("*/work/agents/*/canonical_state.json", "*/*/work/agents/*/canonical_state.json")
        for path in tasks_dir.glob(pattern)
        if path.is_file()
    ]
    return sorted(paths, key=_path_mtime, reverse=True)[:512]


def _agent_state_payload(path: Path) -> dict[str, Any]:
    payload = _read_json_dict(path)
    if not payload:
        return {}
    return {
        "run_id": str(payload.get("run_id") or payload.get("id") or path.parent.name),
        "parent_run_id": str(payload.get("parent_run_id") or payload.get("parent_id") or ""),
        "root_run_id": str(payload.get("root_run_id") or payload.get("root_id") or ""),
        "task_id": str(payload.get("task_id") or ""),
        "status": str(payload.get("status") or ""),
        "current_tool": str(payload.get("current_tool") or ""),
        "last_progress_summary": str(payload.get("last_progress_summary") or payload.get("latest_summary") or ""),
        "state_ref": str(path),
        "updated_at": _safe_float(payload.get("updated_at")),
    }


def _agent_related_to_scope(row: dict[str, Any], id_set: set[str]) -> bool:
    if not id_set:
        return False
    return any(
        str(row.get(field) or "").strip() in id_set
        for field in ("run_id", "parent_run_id", "root_run_id", "task_id")
    )


def _agent_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {"total": len(rows)}
    for row in rows:
        status = _agent_status_bucket(row.get("status"))
        counts[status] = counts.get(status, 0) + 1
    return counts


def _agent_status_bucket(value: object) -> str:
    status = str(value or "").strip()
    bucket_statuses = frozenset({
        TaskStatus.DONE.value,
        TaskStatus.RUNNING.value,
        TaskStatus.PLANNING.value,
        TaskStatus.PENDING.value,
        TaskStatus.BLOCKED.value,
        TaskStatus.FAILED.value,
        TaskStatus.TIMEOUT.value,
        TaskStatus.CHANNEL_ERROR.value,
        TaskStatus.ABANDONED.value,
        TaskStatus.TAKEN_OVER.value,
    })
    if task_status_in(status, bucket_statuses):
        return task_status_reason_code(status)
    return "unknown"


def _active_status(row: dict[str, Any]) -> bool:
    # Terminal child states stay in recent evidence but must not be resumed as active work.
    inactive_statuses = frozenset({
        TaskStatus.DONE.value,
        TaskStatus.FAILED.value,
        TaskStatus.TIMEOUT.value,
        TaskStatus.CHANNEL_ERROR.value,
        TaskStatus.CANCELLED.value,
        TaskStatus.ABANDONED.value,
        TaskStatus.TAKEN_OVER.value,
    })
    return not task_status_in(row.get("status"), inactive_statuses)


def _short_guidance(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "target_type": str(row.get("target_type") or ""),
        "target_id": str(row.get("target_id") or ""),
        "message": str(row.get("message") or ""),
        "sender": str(row.get("sender") or ""),
    }


def _short_agent(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": str(row.get("run_id") or ""),
        "parent_run_id": str(row.get("parent_run_id") or ""),
        "root_run_id": str(row.get("root_run_id") or ""),
        "status": str(row.get("status") or ""),
        "current_tool": str(row.get("current_tool") or ""),
        "last_progress_summary": str(row.get("last_progress_summary") or ""),
        "state_ref": str(row.get("state_ref") or ""),
    }


def _guidance_lines(rows: list[Any]) -> list[str]:
    return [f"- guidance: {row.get('message')}" for row in rows[:5] if isinstance(row, dict) and row.get("message")]


def _active_agent_lines(rows: list[Any]) -> list[str]:
    return [
        f"- active_agent: {row.get('run_id', '')} status={row.get('status', '')} "
        f"tool={row.get('current_tool', '')} note={row.get('last_progress_summary', '')}"
        for row in rows[:5]
        if isinstance(row, dict)
    ]


def _recent_agent_lines(rows: list[Any]) -> list[str]:
    return [
        f"- recent_agent: {row.get('run_id', '')} status={row.get('status', '')} "
        f"tool={row.get('current_tool', '')} note={row.get('last_progress_summary', '')}"
        for row in rows[:8]
        if isinstance(row, dict)
    ]


def _read_json_dict(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_jsonl_dicts(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [payload for line in lines if isinstance((payload := _json_line(line)), dict)]


def _json_line(line: str) -> object:
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def _inside_workspace(path: Path, workspace: Path) -> bool:
    try:
        resolved = path.resolve()
        root = workspace.resolve()
    except OSError:
        return False
    return root in (resolved, *resolved.parents)


def _dedupe_paths(values: list[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for value in values:
        key = str(value)
        if key not in seen:
            result.append(value)
            seen.add(key)
    return result


def _safe_file_stem(value: str) -> str:
    return "".join(char if char.isalnum() or char in "._-" else "-" for char in str(value or "")).strip("-") or "run"


def _safe_float(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _path_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


__all__ = ["build_runtime_handoff", "render_runtime_handoff_lines", "runtime_handoff_payload"]
