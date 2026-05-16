# LLM: Artifact path resolution needs one bounded root policy shared by evidence and integrity.
# 模块用途: 统一产物引用的候选根目录，避免 normalize 与 missing-check 使用两套不一致规则。

from __future__ import annotations

from pathlib import Path
from typing import Any

from .workspace_roots import derived_workspace_roots_from_subagent_path

_ROOT_ATTRIBUTES = (
    "output_dir",
    "reports_dir",
    "agent_run_workspace_dir",
    "task_workspace_artifacts_dir",
    "agent_run_artifacts_dir",
    "task_workspace_shared_dir",
    "task_workspace_dir",
    "task_dir",
    "data_dir",
    "scratch_dir",
)


# LLM: artifact_candidate_roots keeps local artifact lookup broad enough for task products but bounded to known workspaces.
# 函数用途: 返回子代理产物可能出现的目录；include_workspace_roots 只补由 data/subagents 推导出的任务工作区根，不扫描整机。
def artifact_candidate_roots(task: Any, *, include_workspace_roots: bool = True) -> list[Path]:
    roots: list[Path] = []
    for value in _root_values(task):
        _append_existing_root(roots, value)
    if include_workspace_roots:
        _append_derived_workspace_roots(roots)
    return roots


# LLM: artifact_suffix_roots avoids expensive broad workspace scans for bare filenames.
# 函数用途: 给短文件名后缀恢复使用较窄候选根；不包含任务工作区大根目录，避免 rglob 扫描过宽。
def artifact_suffix_roots(task: Any) -> list[Path]:
    return artifact_candidate_roots(task, include_workspace_roots=False)


# LLM: _root_values mirrors the runtime task fields that can legitimately point at product files.
# 函数用途: 收集 task 上已有的目录字段和 allowed_write_roots；字段不存在时按空值处理。
def _root_values(task: Any) -> list[object]:
    values = [getattr(task, name, "") for name in _ROOT_ATTRIBUTES]
    values.extend(getattr(task, "allowed_write_roots", []) or [])
    return values


# LLM: _append_existing_root is defensive because tests may pass mocks or future fields may be malformed.
# 函数用途: 把存在的目录加入候选根；如果传入的是文件路径或尚未创建的目录，则用父目录参与解析。
def _append_existing_root(roots: list[Path], value: object) -> None:
    text = str(value or "").strip()
    if not text:
        return
    try:
        path = Path(text).expanduser()
    except OSError:
        return
    root = path if path.is_dir() else path.parent
    if root.exists() and root.is_dir() and root not in roots:
        roots.append(root)


# LLM: _append_derived_workspace_roots recovers refs like data/subagents/<alias>/file from the product workspace.
# 函数用途: 从 run-local 路径 `/workspace/data/subagents/<run>` 推导 `/workspace`，让相对业务产物 ref 可验收。
def _append_derived_workspace_roots(roots: list[Path]) -> None:
    for workspace in _derived_workspace_roots(roots):
        _append_existing_workspace_root(roots, workspace)


# LLM: _derived_workspace_roots flattens per-root workspace derivation for small helpers and strict size gates.
# 函数用途: 从现有候选根批量推导任务工作区根；只返回候选，不负责存在性和去重。
def _derived_workspace_roots(roots: list[Path]) -> list[Path]:
    derived: list[Path] = []
    for root in list(roots):
        derived.extend(derived_workspace_roots_from_subagent_path(root))
    return derived


# LLM: _append_existing_workspace_root keeps derived broad roots explicit and deduplicated.
# 函数用途: 只把真实存在、未重复的工作区根加入候选列表。
def _append_existing_workspace_root(roots: list[Path], workspace: Path) -> None:
    if workspace.exists() and workspace.is_dir() and workspace not in roots:
        roots.append(workspace)
