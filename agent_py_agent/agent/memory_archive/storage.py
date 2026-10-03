
from __future__ import annotations

from ..common.json_io import append_private_jsonl_records, jsonl_lines, write_private_json_file_atomic

"""JSONL storage primitives for memory hook snapshots and raw archive events.

新手说明:
这个文件只管把快照和归档事件写到固定目录。
它不决定什么时候压缩、不调用模型，也不读取配置；以后接入真实流程时，外层负责判断时机，这里负责可靠落盘。
"""

import json
import os
import stat
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ._storage_dates import (
    _coerce_retention_days,
    _coerce_today,
    _date_from_filename,
    _date_key,
)
from .models import CompressionSnapshot, RawMemoryEvent


class MemoryArchiveError(RuntimeError):
    """Raised when archive writes cannot be verified after append."""


def snapshot_path_for(root: str | Path, created_at: str | int | float | None = None) -> Path:

    return Path(root) / "memory" / "hooks" / f"{_date_key(created_at)}.jsonl"


def compression_snapshot_dir(root: str | Path) -> Path:

    return Path(root) / "memory_archive" / "snapshots"


def compression_snapshot_file_for(root: str | Path, snapshot: CompressionSnapshot) -> Path:

    safe_id = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in snapshot.snapshot_id)
    return compression_snapshot_dir(root) / f"{_date_key(snapshot.created_at)}--{safe_id}.json"


def raw_event_path_for(root: str | Path, created_at: str | int | float | None = None) -> Path:

    return Path(root) / "audit" / f"{_date_key(created_at)}.jsonl"


def filter_snapshot_for_level(payload: dict[str, Any], level: int) -> dict[str, Any]:

    level = max(0, min(3, level))
    if level == 0:
        return dict(payload)
    result = dict(payload)
    if level >= 1:
        _clear_tool_parameter_previews(result)
    if level >= 2:
        result["user_intents"] = result.get("user_intents", [])[:1]
        result["assistant_actions"] = result.get("assistant_actions", [])[:1]
        result["decisions"] = []
        result["open_questions"] = []
    if level >= 3:
        result["user_intents"] = []
        result["assistant_actions"] = []
        result["tool_calls"] = []
        result["content"] = ""
    return result


def _clear_tool_parameter_previews(payload: dict[str, Any]) -> None:
    for tc in payload.get("tool_calls", []):
        if isinstance(tc, dict) and "parameters_preview" in tc:
            tc["parameters_preview"] = ""


def filter_raw_event_for_level(payload: dict[str, Any], level: int) -> dict[str, Any]:

    level = max(0, min(3, level))
    if level == 0:
        return dict(payload)
    result = dict(payload)
    if level >= 2:
        result["content_path"] = ""
    if level >= 3:
        result["content_path"] = ""
    return result


def append_snapshot(root: str | Path, snapshot: CompressionSnapshot) -> Path:

    path = snapshot_path_for(root, snapshot.created_at)
    payload = snapshot.to_dict()
    append_private_jsonl_records(path, [payload], sort_keys=True)
    _verify_record_exists(path, key="snapshot_id", value=snapshot.snapshot_id, expected=payload)
    return path


def write_compression_snapshot_file(root: str | Path, snapshot: CompressionSnapshot) -> Path:

    path = compression_snapshot_file_for(root, snapshot)
    payload = snapshot.to_dict()
    write_private_json_file_atomic(path, payload, sort_keys=True)
    _verify_json_file_payload(path, expected=payload)
    return path


def append_raw_event(root: str | Path, event: RawMemoryEvent) -> Path:

    path = raw_event_path_for(root, event.created_at)
    payload = filter_raw_event_for_level(event.to_dict(), event.archive_level)
    append_private_jsonl_records(path, [payload], sort_keys=True)
    _verify_record_exists(path, key="event_id", value=event.event_id, expected=payload)
    return path


def _verify_record_exists(path: Path, *, key: str, value: str, expected: dict[str, Any]) -> None:
    """Read a JSONL file backwards and confirm the just-written record is present.

    顺序必须是"先按 LF 切记录、再倒序"：把 reversed 结果交给 jsonl_lines 会得到
    'reversed' object has no attribute 'split'，真机上表现为子代理 runner 直接 FAILED。
    """

    normalized_expected = _normalized_json(expected)
    # JSONL 记录边界只能是物理 LF：splitlines() 会在 U+0085/U+2028/U+2029 等合法正文字符处切开记录。
    for line in reversed(jsonl_lines(path.read_text(encoding="utf-8"))):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get(key) != value:
            continue
        if _normalized_json(payload) != normalized_expected:
            raise MemoryArchiveError(f"readback payload mismatch for {key}={value}")
        return
    raise MemoryArchiveError(f"readback failed for {key}={value} in {path}")


def _verify_json_file_payload(path: Path, *, expected: dict[str, Any]) -> None:
    """Ensure an authoritative JSON snapshot file can be read back exactly."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MemoryArchiveError(f"readback failed for snapshot file {path}: {exc}") from exc
    if _normalized_json(payload) != _normalized_json(expected):
        raise MemoryArchiveError(f"readback payload mismatch for snapshot file {path}")


def _normalized_json(payload: dict[str, Any]) -> str:
    """Serialize JSON payloads into a canonical string for equality checks."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def enforce_retention(root: str | Path, retention_days: Any, today: date | str | None = None) -> list[Path]:

    days = _coerce_retention_days(retention_days)
    if days is None or days == 0:
        return []

    hook_dir = Path(root) / "memory" / "hooks"
    if not hook_dir.exists():
        return []

    today_date = _coerce_today(today) or date.today()
    cutoff = today_date - timedelta(days=days - 1)
    deleted: list[Path] = []

    for path in sorted(hook_dir.glob("*.jsonl")):
        file_date = _date_from_filename(path)
        if file_date is None or file_date >= cutoff:
            continue
        path.unlink()
        deleted.append(path)

    return deleted


# LLM: 只收紧不放松：宽于 0700/0600（group/other 任一位）才 chmod；符号链接既不收紧也不进入目标；
#   chmod 失败按稳定原因码计数并继续，绝不抛给调用方。返回结构化事实：收紧数（文件/目录分开）、
#   失败数与原因码计数、跳过的符号链接数；调用方（owner 维护）原样写进维护状态。
# 函数用途: 把 owner memory_archive 目录树里已有文件的权限收紧到 0600、目录收紧到 0700（只改权限，不改内容）。
def tighten_memory_archive_permissions(archive: str | Path) -> dict[str, Any]:
    root = Path(archive)
    result: dict[str, Any] = {
        "tightened_count": 0,
        "tightened_files": 0,
        "tightened_directories": 0,
        "failed_count": 0,
        "failure_codes": {},
        "symlink_skipped_count": 0,
    }
    if root.is_symlink() or not root.is_dir():
        return result
    _tighten_tree(root, result)
    return result


# LLM: 先收紧当前目录，再逐项处理子项；scandir 的 follow_symlinks=False 保证不进入符号链接目录、
#   也不 chmod 链接目标。计数与失败码都写进同一个 result，调用方不需要第二份账。
# 函数用途: 递归收紧一个目录树的权限（副作用：chmod 目录与文件、累加 result 计数）。
def _tighten_tree(directory: Path, result: dict[str, Any]) -> None:
    _tighten_entry(directory, result, is_directory=True)
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return
    for entry in entries:
        if entry.is_symlink():
            result["symlink_skipped_count"] += 1
            continue
        if entry.is_dir(follow_symlinks=False):
            _tighten_tree(Path(entry.path), result)
            continue
        if entry.is_file(follow_symlinks=False):
            _tighten_entry(Path(entry.path), result, is_directory=False)


# LLM: 目标权限目录 0700、文件 0600；已经不比目标宽就不动（不放松更严的既有权限）。成功才计入
#   tightened_*；PermissionError 与其它 OSError 分开记原因码，单点失败不影响后续条目。
# 函数用途: 按目标权限收紧单个路径，成功或失败都记进 result。
def _tighten_entry(path: Path, result: dict[str, Any], *, is_directory: bool) -> None:
    try:
        if stat.S_IMODE(path.stat().st_mode) & 0o077 == 0:
            return
        os.chmod(path, 0o700 if is_directory else 0o600)
    except PermissionError:
        _record_tighten_failure(result, "permission_denied")
        return
    except OSError:
        _record_tighten_failure(result, "os_error")
        return
    result["tightened_count"] += 1
    result["tightened_directories" if is_directory else "tightened_files"] += 1


# LLM: failure_codes 是原因码到条数的映射，稳定可断言；计数只在这里累加。
# 函数用途: 给一次收紧失败记账（失败总数 + 原因码条数）。
def _record_tighten_failure(result: dict[str, Any], code: str) -> None:
    result["failed_count"] += 1
    codes = result["failure_codes"]
    codes[code] = int(codes.get(code, 0)) + 1
