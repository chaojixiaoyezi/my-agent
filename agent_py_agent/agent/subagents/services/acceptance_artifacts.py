# LLM: Artifact acceptance helpers stay separate from generic finding composition.
# 模块用途: 校验 runner 上报的产物路径是否能在任务目录、workspace 或授权输出目录中找到。

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..models import SubAgentTask


# LLM: artifact_exists checks runner-reported local artifacts without trusting arbitrary external paths.
# 函数用途: 核对产物路径是否存在；相对路径会按任务目录、workspace 和 allowed_write_roots 恢复。
def artifact_exists(manager: Any, task: SubAgentTask, raw_path: str) -> bool:
    text = raw_path.strip()
    if not text or "://" in text:
        return True
    path = Path(text).expanduser()
    roots = artifact_roots(manager, task)
    candidates = [path] if path.is_absolute() else [root / path for root in roots]
    if any(candidate.exists() for candidate in candidates):
        return True
    if not path.is_absolute():
        return path_suffix_exists(path, roots)
    if absolute_path_inside_roots(path, roots):
        return absolute_path_suffix_exists(path, roots)
    return False


# LLM: artifact_roots includes runtime, manager workspace, project root, and explicit write grants.
# 函数用途: 返回产物恢复的候选根目录；只使用当前任务和 manager 已知边界。
def artifact_roots(manager: Any, task: SubAgentTask) -> list[Path]:
    roots = [Path(task.task_dir), Path(manager.workspace), Path(manager.workspace).parent]
    workspace_root = getattr(manager, "workspace_root", None)
    if workspace_root:
        roots.append(Path(workspace_root))
    roots.extend(Path(item) for item in (getattr(manager, "workspace_roots", None) or []))
    roots.extend(Path(item) for item in (getattr(task, "allowed_write_roots", None) or []))
    runtime_root = runtime_project_root(Path(manager.workspace))
    if runtime_root is not None:
        roots.append(runtime_root)
    return list(dict.fromkeys(root.resolve() for root in roots))


# LLM: runtime_project_root recovers the user project root from hidden runtime subagent workspaces.
# 函数用途: 从 <project>/.my_agent_runtime/<case>/subagents 或 <project>/.my_agent/subagents 推回 project。
def runtime_project_root(workspace: Path) -> Path | None:
    parts = workspace.resolve().parts
    index = runtime_marker_index(parts)
    return Path(*parts[:index]) if index > 0 else None


# LLM: runtime_marker_index keeps runtime root detection shallow and deterministic.
# 函数用途: 返回隐藏 runtime 标记所在下标；找不到时返回 -1。
def runtime_marker_index(parts: tuple[str, ...]) -> int:
    for marker in (".my_agent_runtime", ".my_agent"):
        try:
            return parts.index(marker)
        except ValueError:
            continue
    return -1


# LLM: path_suffix_exists repairs nested artifact paths without broad text scanning.
# 函数用途: 当 runner 少写外层目录时，在允许根目录内按文件名和路径后缀找真实文件。
def path_suffix_exists(relative_path: Path, roots: list[Path]) -> bool:
    parts = relative_path.parts
    if not parts:
        return False
    return any(
        path_has_suffix(item, parts)
        for root in roots
        if root.exists() and root.is_dir()
        for item in root.rglob(parts[-1])
    )


# LLM: absolute_path_inside_roots avoids repairing arbitrary outside-system paths.
# 函数用途: 只有模型报告的绝对路径位于允许根目录下，才尝试后缀恢复。
def absolute_path_inside_roots(path: Path, roots: list[Path]) -> bool:
    resolved = path.resolve()
    for root in roots:
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        return True
    return False


# LLM: absolute_path_suffix_exists repairs case-id typos while staying inside approved roots.
# 函数用途: 绝对路径不存在但尾部路径足够具体时，在允许根目录内按尾部片段查找。
def absolute_path_suffix_exists(path: Path, roots: list[Path]) -> bool:
    parts = path.parts
    max_depth = min(6, len(parts))
    return any(path_suffix_exists(Path(*parts[-depth:]), roots) for depth in range(max_depth, 2, -1))


# LLM: path_has_suffix keeps nested artifact recovery small and predictable.
# 函数用途: 判断真实文件路径是否以 runner 报告的相对路径片段结尾。
def path_has_suffix(path: Path, parts: tuple[str, ...]) -> bool:
    return len(path.parts) >= len(parts) and path.parts[-len(parts):] == parts
