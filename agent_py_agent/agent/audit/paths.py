# LLM: Centralizes audit path interpretation so config values work as either a directory or a log file path.
# 模块用途: 统一解析 audit_log_path，避免 logger/query 对同一个配置字段产生不同理解。

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


# LLM: AuditPaths is the resolved filesystem contract shared by audit readers and writers.
# 类用途: 保存审计目录和具体日志文件路径，调用方不用再重复判断配置是目录还是文件。
@dataclass(frozen=True)
class AuditPaths:
    root: Path
    log_file: Path


# LLM: resolve_audit_paths accepts legacy file-style values and current directory-style values without migration.
# 函数用途: 把 audit_log_path 解析成稳定路径；有后缀时当作文件，没有后缀时当作目录并使用 audit.jsonl。
def resolve_audit_paths(config: Any) -> AuditPaths:
    raw_value = getattr(config, "audit_log_path", "data/audit") or "data/audit"
    path = Path(str(raw_value)).expanduser()
    if path.suffix:
        return AuditPaths(root=path.parent, log_file=path)
    return AuditPaths(root=path, log_file=path / "audit.jsonl")
