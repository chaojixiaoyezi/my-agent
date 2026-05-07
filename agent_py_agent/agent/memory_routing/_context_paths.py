# LLM: Memory routing module; keep context selection and read-receipt records stable.
# 模块用途: 根据任务上下文选择可注入记忆，并记录读取路径。

"""Path safety and match-to-read-target helpers for routed memory context."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .models import MemoryRouteMatch


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 _ReadTarget 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 _ReadTarget 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass
class _ReadTarget:
    """Internal representation of one safe authority file read target."""
    route_id: str
    path: str
    absolute_path: Path
    reasons: list[str] = field(default_factory=list)


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 _resolve_root 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 resolve root 在当前模块中的核心转换或协调步骤，衔接 memory routing 读取项目规则、路径和上下文片段来决定注入范围。
def _resolve_root(root: str | Path) -> tuple[Path | None, str]:
    """Resolve the configured project root before any index or rule reads."""
    root_path = Path(root)
    try:
        resolved = root_path.resolve()
    except OSError as exc:
        return None, f"root path cannot be resolved: {root_path} ({exc})"
    if not resolved.exists():
        return None, f"root path does not exist: {root_path}"
    if not resolved.is_dir():
        return None, f"root path is not a directory: {root_path}"
    return resolved, ""


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 _resolve_relative_path 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 resolve relative path 在当前模块中的核心转换或协调步骤，衔接 memory routing 读取项目规则、路径和上下文片段来决定注入范围。
def _resolve_relative_path(root: Path, raw_path: str, *, label: str) -> tuple[Path | None, str, str]:
    """Resolve a caller-provided relative path without escaping root."""
    cleaned = raw_path.strip()
    if not cleaned:
        return None, "", f"{label} is empty"
    candidate = Path(cleaned)
    if candidate.is_absolute():
        return None, "", f"{label} must be relative, got {cleaned}"
    try:
        resolved = (root / candidate).resolve()
    except OSError as exc:
        return None, "", f"{label} cannot be resolved: {cleaned} ({exc})"
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        return None, "", f"{label} escapes root: {cleaned}"
    return resolved, relative.as_posix(), ""


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 _read_targets_from_matches 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 读取 read targets from matches 需要的文件、记录或配置，并整理成调用方可直接使用的结果。
def _read_targets_from_matches(
    matches: list[MemoryRouteMatch],
    root: Path,
    findings: list[str],
) -> list[_ReadTarget]:
    """Convert matched routes into unique safe read targets."""
    targets: list[_ReadTarget] = []
    seen_paths: set[str] = set()
    for match in matches:
        raw_path = match.route.authority_file()
        if not raw_path:
            continue
        absolute_path, normalized_path, error = _resolve_relative_path(root, raw_path, label="source_file")
        if error:
            _append_finding(findings, _authority_path_finding(match, error, raw_path))
            continue
        if absolute_path is None or normalized_path in seen_paths:
            continue
        seen_paths.add(normalized_path)
        targets.append(
            _ReadTarget(
                route_id=match.route.route_id,
                path=normalized_path,
                absolute_path=absolute_path,
                reasons=list(match.reasons),
            )
        )
    return targets


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 _authority_path_finding 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 authority path finding 在当前模块中的核心转换或协调步骤，衔接 memory routing 读取项目规则、路径和上下文片段来决定注入范围。
def _authority_path_finding(match: MemoryRouteMatch, error: str, raw_path: str) -> str:
    """Format authority path safety findings like the route validator."""
    label = match.route.route_id or "<empty route_id>"
    if "must be relative" in error:
        return f"route '{label}': authority_path must be relative, got {raw_path}"
    if "escapes root" in error:
        return f"route '{label}': authority_path escapes root: {raw_path}"
    if "cannot be resolved" in error:
        return f"route '{label}': authority_path cannot be resolved ({error})"
    return f"route '{label}': {error}"


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 _append_finding 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 append finding 相关记录，集中处理目标路径、格式化和状态更新。
def _append_finding(findings: list[str], finding: str) -> None:
    """Append a diagnostic once while preserving first-seen order."""
    if finding and finding not in findings:
        findings.append(finding)
