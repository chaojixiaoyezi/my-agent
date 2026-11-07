
from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AuditPaths:
    root: Path
    log_file: Path


def audit_segment_dir(log_file: Path) -> Path:
    """封存段与活动账本同目录，不另建可漂移的归档根。"""
    return log_file.parent


def _audit_segment_key(path: Path):
    match = re.fullmatch(r".+-(\d{4}-\d{2}-\d{2})(?:\.([1-9]\d*))?" + re.escape(path.suffix), path.name)
    if match is None:
        return None
    try:
        day = date.fromisoformat(match[1]).isoformat()
        sequence = int(match[2] or 0)
    except ValueError:
        return None
    return day, sequence


def audit_segment_day(path: Path) -> str | None:
    """只接受完整日期段名，损坏日期不能成为整段删除证据。"""
    key = _audit_segment_key(path)
    return key[0] if key is not None else None


def audit_segment_matches(log_file: Path, path: Path) -> bool:
    """段名及所在目录必须精确归属一个活动账本，不能只凭日期扩大删除范围。"""
    key = _audit_segment_key(path)
    if key is None or path.parent != audit_segment_dir(log_file):
        return False
    day, sequence = key
    expected = f"{log_file.stem}-{day}" + (f".{sequence}" if sequence else "") + log_file.suffix
    return path.name == expected


def audit_segment_paths(log_file: Path) -> list[Path]:
    """非递归列出本账本的普通封存文件，按日期与数字序号排序。"""
    found = []
    try:
        with os.scandir(audit_segment_dir(log_file)) as entries:
            for entry in entries:
                path = log_file.parent / entry.name
                key = _audit_segment_key(path)
                if key is None:
                    continue
                if not audit_segment_matches(log_file, path):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(info.st_mode):
                    found.append((key, path))
    except FileNotFoundError:
        return []
    return [path for _key, path in sorted(found)]


def audit_uses_daily_segments(log_file: Path, trusted_root: Path | None) -> bool:
    """仅规范 owner 安全审计开启轮转，自定义路径保留原行为。"""
    if trusted_root is None:
        return False
    source = Path(os.path.abspath(log_file))
    root = Path(os.path.abspath(trusted_root))
    return source in (root / "logs" / "audit" / "audit.jsonl", root / "audit_log.jsonl")


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
