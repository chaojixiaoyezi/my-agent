from __future__ import annotations

"""LLM: enforces subagent write scopes before mutating filesystem tools run.

给人看的解释：
这个文件是一道真正的写入门禁。
prompt 里说“只能写这个目录”只是提醒，真正防止越界写文件的是这里的路径检查。
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
    return text


def validate_write_boundary(
    tool_name: str,
    params: dict[str, Any],
    *,
    workspace_root: Path,
    write_boundary: dict[str, object] | None,
) -> str:
    """Enforce subagent write boundaries before filesystem write tools run.

    In plain terms: prompts can tell a subagent "only write here", but prompts
    are not a lock. This check is the real lock at the tool layer: a write must
    stay inside allowed roots and avoid forbidden or locked paths.
    """

    if tool_name not in WRITE_TOOL_NAMES or write_boundary is None:
        return ""

    if not isinstance(params, dict):
        return "写入被阻止: 工具参数必须是 JSON 对象。"

    raw_path = params.get("path")
    if raw_path is None:
        return ""

    try:
        target = _resolve_boundary_path(raw_path, workspace_root)
    except ValueError as exc:
        return f"写入被阻止: {exc}"

    allowed_roots = _boundary_paths(write_boundary.get("allowed_write_roots"), workspace_root)
    if not allowed_roots:
        return "写入被阻止: 当前 subagent 没有配置 allowed_write_roots，不能执行写文件工具。"
    if not any(_is_relative_to(target, root) for root in allowed_roots):
        roots = ", ".join(_display_path(root, workspace_root) for root in allowed_roots)
        return (
            "写入被阻止: 目标路径不在 allowed_write_roots 内。"
            f" target={_display_path(target, workspace_root)} allowed={roots}"
        )

    forbidden_roots = _boundary_paths(write_boundary.get("forbidden_write_roots"), workspace_root)
    for root in forbidden_roots:
        if _is_relative_to(target, root):
            return (
                "写入被阻止: 目标路径落在 forbidden_write_roots 内。"
                f" target={_display_path(target, workspace_root)} forbidden={_display_path(root, workspace_root)}"
            )

    locked_paths = _boundary_paths(write_boundary.get("locked_files"), workspace_root)
    for locked in locked_paths:
        if target == locked or _is_relative_to(target, locked):
            return (
                "写入被阻止: 目标路径已被 locked_files 锁定。"
                f" target={_display_path(target, workspace_root)} locked={_display_path(locked, workspace_root)}"
            )

    return ""


def _boundary_paths(raw_paths: object, workspace_root: Path) -> list[Path]:
    if not isinstance(raw_paths, list):
        return []
    paths: list[Path] = []
    for raw in raw_paths:
        try:
            paths.append(_resolve_boundary_path(raw, workspace_root))
        except ValueError:
            continue
    return paths


def _resolve_boundary_path(raw_path: object, workspace_root: Path) -> Path:
    text = _path_text(raw_path)
    root = workspace_root.resolve(strict=False)
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError("路径解析失败，请检查路径是否有效。") from exc
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


def _display_path(path: Path, workspace_root: Path) -> str:
    try:
        return str(path.relative_to(workspace_root))
    except ValueError:
        return str(path)
