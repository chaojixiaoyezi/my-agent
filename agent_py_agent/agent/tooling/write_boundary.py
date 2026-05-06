from __future__ import annotations

"""LLM: enforces subagent write scopes before mutating filesystem tools run.

给人看的解释：
这个文件是一道真正的写入门禁。
prompt 里说'只能写这个目录'只是提醒，真正防止越界写文件的是这里的路径检查。
它会检查允许目录、禁止目录、锁定文件，确保子代理不能改不该改的地方。
"""

from pathlib import Path
from typing import Any

WRITE_TOOL_NAMES = {"write_file", "append_file", "replace_in_file"}
_MAX_BOUNDARY_PATH_CHARS = 4096


def _path_text(raw_path: object, *, label: str = "path") -> str:
    if raw_path is None:
        raise ValueError(f"{label} 参数缺失")
    if not isinstance(raw_path, (str, Path)):
        raise ValueError(f"{label} 参数必须是字符串路径")
    text = str(raw_path).strip()
    if not text:
        raise ValueError(f"{label} 不能为空")
    if len(text) > _MAX_BOUNDARY_PATH_CHARS:
        raise ValueError(f"{label} 过长，最多 {_MAX_BOUNDARY_PATH_CHARS} 个字符")
    if any(ord(char) < 32 for char in text):
        raise ValueError(f"{label} 包含不支持的控制字符")
    # Normalize path separators to forward slashes for cross-platform consistency
    text = text.replace("\\", "/")
    return text


def validate_write_boundary(
    tool_name: str,
    params: dict[str, Any],
    *,
    workspace_root: Path,
    workspace_roots: list[Path] | None = None,
    write_boundary: dict[str, object] | None,
) -> str:

    if tool_name not in WRITE_TOOL_NAMES or write_boundary is None:
        return ""

    if not isinstance(params, dict):
        return "写入被阻止: 工具参数必须是 JSON 对象。"

    raw_path = params.get("path")
    if raw_path is None:
        return ""

    roots = _normalized_workspace_roots(workspace_root, workspace_roots)
    try:
        target = _resolve_boundary_path(raw_path, workspace_root, roots)
    except ValueError as exc:
        return f"写入被阻止: {exc}"

    allowed_roots = _boundary_paths(write_boundary.get("allowed_write_roots"), workspace_root, roots)
    if not allowed_roots:
        return "写入被阻止: 当前 subagent 没有配置 allowed_write_roots，不能执行写文件工具。"
    if not any(_is_relative_to(target, root) for root in allowed_roots):
        roots = ", ".join(_display_path(root, workspace_root) for root in allowed_roots)
        return (
            "写入被阻止: 目标路径不在 allowed_write_roots 内。"
            f" target={_display_path(target, workspace_root)} allowed={roots}"
        )

    forbidden_error = _forbidden_boundary_error(target, allowed_roots, write_boundary, workspace_root)
    if forbidden_error:
        return forbidden_error
    return _locked_boundary_error(target, write_boundary, workspace_root)


def _forbidden_boundary_error(
    target: Path,
    allowed_roots: list[Path],
    write_boundary: dict[str, object],
    workspace_root: Path,
) -> str:
    forbidden_roots = _boundary_paths(write_boundary.get("forbidden_write_roots"), workspace_root)
    for root in forbidden_roots:
        if _forbidden_root_blocks_target(target, root, allowed_roots):
            return (
                "写入被阻止: 目标路径落在 forbidden_write_roots 内。"
                f" target={_display_path(target, workspace_root)} forbidden={_display_path(root, workspace_root)}"
            )
    return ""


def _forbidden_root_blocks_target(target: Path, root: Path, allowed_roots: list[Path]) -> bool:
    if not _is_relative_to(target, root):
        return False
    # LLM: general parent forbids (like ~) do not override a narrower explicit grant.
    return any(_is_relative_to(root, aroot) for aroot in allowed_roots)


def _locked_boundary_error(target: Path, write_boundary: dict[str, object], workspace_root: Path) -> str:
    locked_paths = _boundary_paths(write_boundary.get("locked_files"), workspace_root)
    for locked in locked_paths:
        if target == locked or _is_relative_to(target, locked):
            return (
                "写入被阻止: 目标路径已被 locked_files 锁定。"
                f" target={_display_path(target, workspace_root)} locked={_display_path(locked, workspace_root)}"
            )
    return ""


def _boundary_paths(
    raw_paths: object,
    workspace_root: Path,
    workspace_roots: list[Path] | None = None,
) -> list[Path]:
    if not isinstance(raw_paths, list):
        return []
    paths: list[Path] = []
    for raw in raw_paths:
        try:
            paths.append(_resolve_boundary_path(raw, workspace_root, workspace_roots))
        except ValueError:
            continue
    return paths


def _resolve_boundary_path(
    raw_path: object,
    workspace_root: Path,
    workspace_roots: list[Path] | None = None,
) -> Path:
    text = _path_text(raw_path)
    root = workspace_root.resolve(strict=False)
    roots = _normalized_workspace_roots(root, workspace_roots)
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError("路径解析失败，请检查路径是否有效。") from exc
    if any(_is_relative_to(resolved, item) for item in roots):
        return resolved
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("路径超出允许的工作区范围，请使用工作区内路径。") from exc
    return resolved


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _normalized_workspace_roots(primary: Path, roots: list[Path] | None) -> list[Path]:
    resolved: list[Path] = []
    for raw in [primary, *(roots or [])]:
        path = Path(raw).resolve(strict=False)
        if path not in resolved:
            resolved.append(path)
    return resolved


def _display_path(path: Path, workspace_root: Path) -> str:
    try:
        return str(path.relative_to(workspace_root)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")
