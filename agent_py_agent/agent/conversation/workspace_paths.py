from __future__ import annotations

"""LLM: 运行路径验证与业务目录授权分离；旧 tasks 运行链接保持可恢复，新归档位于 runs。

模块用途: 验证当前用户的宿主运行引用，Audit 继续使用单独的精确目录。
"""

import re
from pathlib import Path

_STABLE_TASK_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


# LLM: 此根只供宿主运行身份与恢复使用，不能作为普通文件操作的权限白名单。
# 函数用途: 返回内部运行或 Audit 的默认归档根。
def durable_work_root(owner_home: str | Path, work_kind: str) -> Path:
    owner = Path(owner_home).expanduser().resolve(strict=False)
    return owner / ("audits" if str(work_kind or "").strip().lower() == "audit" else "runs")


def audit_workspace_path(owner_home: str | Path, audit_id: str) -> Path:
    stable_id = str(audit_id or "").strip()
    if not stable_id or _STABLE_TASK_ID.fullmatch(stable_id) is None:
        raise ValueError("audit id is not a stable path segment")
    return durable_work_root(owner_home, "audit") / stable_id


# LLM: 验证真实路径归属；包含旧归档根只为结构化历史兼容，不从目录正文或命名推断权限。
# 函数用途: 检查运行记录仍属于当前用户，防止恢复时串到其他用户。
def validated_durable_work_path(
    owner_home: str | Path,
    task_path: str | Path,
    work_kind: str,
    *,
    require_directory: bool = False,
) -> Path:
    # 存量 task link 仍指向 tasks；保留其读取/恢复，不创建新的用户业务目录。
    owner = Path(owner_home).expanduser().resolve(strict=False)
    normalized_kind = str(work_kind or "").strip().lower()
    roots = (
        (durable_work_root(owner, "audit"),)
        if normalized_kind == "audit"
        else (durable_work_root(owner, "task"), owner / "tasks")
        if normalized_kind
        else (durable_work_root(owner, "task"), owner / "tasks", durable_work_root(owner, "audit"))
    )
    path = Path(task_path).expanduser().resolve(strict=False)
    root = next((candidate for candidate in roots if path.is_relative_to(candidate)), None)
    if root is None:
        raise ValueError("durable work path is outside its owner run and audit roots")
    if path == root:
        raise ValueError("durable work path must be below its owner root")
    if require_directory and not path.is_dir():
        raise ValueError("durable work path is not a directory")
    return path


# LLM: 恢复材料只认这三类规范任务根（新版 runs 第二层、旧版 tasks 第二层、audits 第一层）；深度差异在这里
#   一次说清，调用方不得自己数层数或从目录名推断。返回 None 表示"不是规范任务根"。
# 函数用途: 给定路径，判断它是不是某个规范任务根，并返回该根与任务目录。
def canonical_task_root(owner_home: str | Path, task_path: str | Path) -> tuple[Path, Path] | None:
    owner = Path(owner_home).expanduser().resolve(strict=False)
    path = Path(task_path).expanduser().resolve(strict=False)
    candidates = (
        # 新版运行工作区：runs/<date>/<key>
        (durable_work_root(owner, "task"), 2),
        # 旧版任务工作区：tasks/<date>/<slug>
        (owner / "tasks", 2),
        # Audit 工作区：audits/<audit_id>
        (durable_work_root(owner, "audit"), 1),
    )
    for root, depth in candidates:
        if not path.is_relative_to(root) or path == root:
            continue
        parts = path.relative_to(root).parts
        if len(parts) != depth:
            continue
        return root, path
    return None


__all__ = [
    "audit_workspace_path",
    "canonical_task_root",
    "durable_work_root",
    "validated_durable_work_path",
]
