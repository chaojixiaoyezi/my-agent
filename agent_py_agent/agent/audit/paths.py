
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AuditPaths:
    root: Path
    log_file: Path


# LLM: 相对路径必须相对调用方给出的 canonical 目录解析；当前进程 cwd 不是所有者事实，
#   也不能作为兜底（否则 `audit-log --cleanup` 会随启动目录清到不同文件，甚至清掉工作区内的同名目录）。
#   不传 root 时保持旧的“相对 cwd”语义，供既有单元测试与旧调用方使用。
# 函数用途: 解析审计日志根目录与日志文件，相对路径按给定基准目录展开。
def resolve_audit_paths(config: Any, *, root: Path | str | None = None) -> AuditPaths:
    raw_value = getattr(config, "audit_log_path", "data/audit") or "data/audit"
    path = Path(str(raw_value)).expanduser()
    if root is not None and not path.is_absolute():
        path = Path(root).expanduser() / path
    if path.suffix:
        return AuditPaths(root=path.parent, log_file=path)
    return AuditPaths(root=path, log_file=path / "audit.jsonl")
