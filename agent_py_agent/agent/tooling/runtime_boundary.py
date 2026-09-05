from __future__ import annotations

"""LLM: 仅还原宿主展示的 owner-home 别名；普通相对路径始终相对 cwd，绝不按 tasks/output/work 名字重定向。

模块用途: 在统一执行入口还原脱敏地址并核对精确只读范围，不改变用户业务目录语义。
"""

from pathlib import Path
from typing import Any

_OWNER_HOME_ALIAS_TOOL_NAMES = {
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


# LLM: 只把宿主展示别名还原为同 owner 地址；哈希、权限和 handler 继续共享同一参数，不改普通路径。
# 函数用途: 还原脱敏家目录，包括补丁文件头；补丁正文、绝对路径和业务目录名保持原样。
def canonicalize_owner_home_arguments(
    tool_name: str,
    arguments: dict[str, Any],
    write_boundary: dict[str, object] | None,
) -> dict[str, Any]:
    """Resolve only the display home alias before policy, hashing, and execution."""

    params = dict(arguments)
    if (
        tool_name not in _OWNER_HOME_ALIAS_TOOL_NAMES
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


# LLM: 此入口只恢复补丁头里的 owner 展示别名，不解释业务目录，也不改补丁正文中的路径。
# 函数用途: 遍历补丁文件头；没有需要还原的地址时返回空值，调用方保留原始补丁。
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


# LLM: 只有结构化补丁头可还原 owner 别名；正文的相同文字不能成为文件路由依据。
# 函数用途: 识别新增、修改、删除和移动文件头，保留其他每一行的原文。
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


# LLM: 普通相对地址、绝对地址及 tasks/output/work 名称不作映射；仅尝试还原宿主 owner 展示别名。
# 函数用途: 规范化待识别的展示地址，再交给唯一 owner 别名解析器；不会选择任务或授予权限。
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
    owner_root = _canonical_owner_address_root(boundary)
    if owner_root is None:
        return ""
    try:
        candidate = owner_root.joinpath(*suffix_parts).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return ""
    if not _is_relative_to(candidate, owner_root):
        return ""
    return str(candidate)


# LLM: Address resolution prefers the explicit canonical owner home and only falls back to the
# security wall for older host callers. This helper never grants access; normal read/write policy
# still evaluates the canonical result after rewriting.
# 函数用途: 取得 owner 路径别名的宿主地址基准，并兼容尚未补新字段的内部调用。
def _canonical_owner_address_root(
    boundary: dict[str, object],
) -> Path | None:
    root_text = str(
        boundary.get("canonical_owner_home_root")
        or boundary.get("effective_owner_scope_root")
        or ""
    ).strip()
    if not root_text:
        return None
    try:
        return Path(root_text).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
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


__all__ = [
    "canonicalize_owner_home_arguments",
    "exact_read_boundary_error",
]
