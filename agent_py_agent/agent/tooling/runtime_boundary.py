from __future__ import annotations

"""Pure normalization and exact task-boundary checks used before execution."""

import re
from pathlib import Path
from typing import Any

# LLM: This module normalizes only host-declared workspace aliases before policy and execution.
# It must never infer paths from prose or widen the boundary; absolute canonical paths remain
# idempotent and every rewritten path is still checked by the normal read/write policy.
# 模块用途: 把 output/work/workspace 和当前 tasks/... 地址转成唯一物理路径，避免重复拼接任务目录。

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
_MODEL_OWNER_HOME_ALIAS = "~/.my-agent/owner"


# LLM: This is the sole argument canonicalizer for task workspace aliases. Keep it pure and
# ensure callers still run authorization, hashing, sandboxing, and handlers after normalization.
# 函数用途: 在工具执行前统一路径别名，包括补丁头里的文件路径。
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
    if not text:
        return ""
    normalized = text.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    owner_alias_path = _model_owner_home_alias_path(normalized, boundary)
    if owner_alias_path:
        return owner_alias_path
    if _is_absolute_or_home_path(normalized):
        return ""
    current_task_path = _current_task_alias_path(normalized, boundary)
    if current_task_path:
        return current_task_path
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


# LLM: The public owner-home token is only a reversible display alias. Resolve it from the
# host-authored effective owner wall and leave the resulting path to the normal read/write
# policy; never derive owner authority from the alias text itself.
# 函数用途: 将界面脱敏后的 ~/.my-agent/owner 地址还原到当前用户家目录，避免模型复用展示路径时找错目录。
def _model_owner_home_alias_path(
    normalized: str,
    boundary: dict[str, object],
) -> str:
    if normalized == _MODEL_OWNER_HOME_ALIAS:
        suffix_parts: tuple[str, ...] = ()
    elif normalized.startswith(_MODEL_OWNER_HOME_ALIAS + "/"):
        suffix_parts = tuple(normalized[len(_MODEL_OWNER_HOME_ALIAS) + 1 :].split("/"))
    else:
        return ""
    if any(not part or part in {".", ".."} for part in suffix_parts):
        return ""
    root_text = str(boundary.get("effective_owner_scope_root") or "").strip()
    if not root_text:
        return ""
    try:
        owner_root = Path(root_text).expanduser().resolve(strict=False)
        candidate = owner_root.joinpath(*suffix_parts).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return ""
    if not _is_relative_to(candidate, owner_root):
        return ""
    return str(candidate)


# LLM: Model-visible owner-relative task addresses are aliases only when their exact tasks/...
# prefix matches the host-authored current task root. This prevents cwd/tasks/... duplication
# without treating arbitrary relative paths as owner-home authority.
# 函数用途: 将当前任务的 tasks/日期/任务名/... 地址幂等地还原为真实路径。
def _current_task_alias_path(
    normalized: str,
    boundary: dict[str, object],
) -> str:
    parts = tuple(part for part in normalized.split("/") if part)
    if not parts or parts[0] != "tasks" or any(part in {".", ".."} for part in parts):
        return ""
    root_text = str(boundary.get("task_root") or "").strip()
    if not root_text:
        return ""
    try:
        task_root = Path(root_text).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return ""
    root_parts = task_root.parts
    task_indexes = [index for index, part in enumerate(root_parts) if part == "tasks"]
    if not task_indexes:
        return ""
    for index in reversed(task_indexes):
        alias_parts = root_parts[index:]
        if tuple(parts[: len(alias_parts)]) != alias_parts:
            continue
        suffix = parts[len(alias_parts) :]
        return str((task_root.joinpath(*suffix)).resolve(strict=False))
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
