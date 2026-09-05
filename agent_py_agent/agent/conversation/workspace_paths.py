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


__all__ = [
    "audit_workspace_path",
    "durable_work_root",
    "validated_durable_work_path",
]
