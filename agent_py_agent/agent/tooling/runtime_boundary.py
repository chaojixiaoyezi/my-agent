from __future__ import annotations

"""Pure normalization and exact task-boundary checks used before execution."""

import re
from pathlib import Path
from typing import Any

_TASK_WORKSPACE_RELATIVE_PATH_TOOL_NAMES = {
    "apply_patch",
    "edit_file",
    "find_files",
    "list_files",
    "read_file",
    "search_text",
    "write_file",
}
_READ_BOUNDARY_TOOL_NAMES = {
    "find_files",
    "list_files",
    "read_file",
    "search_text",
}


def canonicalize_task_workspace_arguments(
    tool_name: str,
    arguments: dict[str, Any],
    write_boundary: dict[str, object] | None,
) -> dict[str, Any]:
    """Resolve host-declared task aliases before policy, hashing, and execution."""

    params = dict(arguments)
    if (
        tool_name not in _TASK_WORKSPACE_RELATIVE_PATH_TOOL_NAMES
        or not isinstance(write_boundary, dict)
    ):
        return params
    if tool_name == "apply_patch":
        rewritten = _task_workspace_relative_patch(
            params.get("patch"),
            write_boundary,
        )
        return {**params, "patch": rewritten} if rewritten else params
    rewritten = _task_workspace_relative_path(
        params.get("path"),
        write_boundary,
    )
    return {**params, "path": rewritten} if rewritten else params


def exact_read_boundary_error(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    workspace_root: Path,
    write_boundary: dict[str, object] | None,
) -> str:
    """Return an error when an exact read scope excludes the requested path."""

    if (
        tool_name not in _READ_BOUNDARY_TOOL_NAMES
        or not isinstance(write_boundary, dict)
        or str(write_boundary.get("read_scope_mode") or "").strip().lower()
        != "exact"
    ):
        return ""
    target = _resolved_path(arguments.get("path", "."), workspace_root)
    allowed = _resolved_boundary_paths(
        write_boundary.get("allowed_read_roots"),
        workspace_root,
    )
    if target is not None and any(_is_relative_to(target, root) for root in allowed):
        return ""
    return "只能读取当前任务结构化授权的输入或本 run 工作目录。"


def _task_workspace_relative_patch(
    raw_patch: object,
    boundary: dict[str, object],
) -> str:
    if not isinstance(raw_patch, str) or not raw_patch:
        return ""
    normalized = raw_patch.replace("\r\n", "\n").replace("\r", "\n")
    rewritten_lines: list[str] = []
    changed = False
    for line in normalized.split("\n"):
        rewritten = _task_workspace_patch_header(line, boundary)
        changed |= rewritten != line
        rewritten_lines.append(rewritten)
    return "\n".join(rewritten_lines) if changed else ""


def _task_workspace_patch_header(
    line: str,
    boundary: dict[str, object],
) -> str:
    for prefix in (
        "*** Add File: ",
        "*** Update File: ",
        "*** Delete File: ",
        "*** Move to: ",
    ):
        if line.startswith(prefix):
            rewritten = _task_workspace_relative_path(
                line.removeprefix(prefix).strip(),
                boundary,
            )
            return f"{prefix}{rewritten}" if rewritten else line
    return line


def _task_workspace_relative_path(
    raw: object,
    boundary: dict[str, object],
) -> str:
    text = str(raw or "").strip()
    if not text or _is_absolute_or_home_path(text):
        return ""
    normalized = text.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    for prefix, root_key in (
        ("output", "task_output_dir"),
        ("work", "task_work_dir"),
        ("workspace", "owner_workspace_dir"),
    ):
        rewritten = _task_workspace_prefixed_path(
            normalized,
            prefix,
            boundary.get(root_key),
        )
        if rewritten:
            return rewritten
    return ""


def _task_workspace_prefixed_path(
    normalized: str,
    prefix: str,
    raw_root: object,
) -> str:
    suffix = _task_workspace_path_suffix(normalized, prefix)
    if suffix is None:
        return ""
    root = str(raw_root or "").strip()
    if not root:
        return ""
    try:
        base = Path(root).expanduser().resolve(strict=False)
    except OSError:
        return ""
    return str((base / suffix).resolve(strict=False)) if suffix else str(base)


def _task_workspace_path_suffix(normalized: str, prefix: str) -> str | None:
    if normalized == prefix:
        return ""
    if normalized.startswith(prefix + "/"):
        return normalized[len(prefix) + 1 :]
    return None


def _resolved_boundary_paths(
    value: object,
    workspace_root: Path,
) -> tuple[Path, ...]:
    values = value if isinstance(value, (list, tuple)) else (value,)
    resolved: list[Path] = []
    for raw in values:
        path = _resolved_path(raw, workspace_root)
        if path is not None and path not in resolved:
            resolved.append(path)
    return tuple(resolved)


def _resolved_path(raw: object, workspace_root: Path) -> Path | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = workspace_root / path
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_absolute_or_home_path(text: str) -> bool:
    return text.startswith("/") or text.startswith("~") or bool(
        re.match(r"^[A-Za-z]:[\\/]", text)
        or re.match(r"^\\\\[^\\/]+[\\/][^\\/]+", text)
    )


__all__ = [
    "canonicalize_task_workspace_arguments",
    "exact_read_boundary_error",
]
