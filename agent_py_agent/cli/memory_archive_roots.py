# LLM: Owner archive roots keep memory archive CLI scoped without bloating command handlers.
# 模块用途: 解析当前 agent 可读取的 archive 根目录，并收集 owner-scoped archive records。

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..agent.memory_archive.query import collect_archive_records


# LLM: ArchiveCollectRequest keeps archive query knobs grouped for CLI helpers.
# 类用途: 保存一次 archive 收集请求的 layer/date/limit/level/file_limit 参数。
@dataclass(frozen=True)
class ArchiveCollectRequest:
    layer: str = "all"
    date_key: str | None = None
    limit: int = 0
    level: int | None = None
    file_limit: int = 30


# LLM: archive_roots returns owner home first; local/main may also read legacy workspace archives.
# 函数用途: 返回当前 agent 允许查询的 memory archive 根目录，避免 provider 用户串读 local/main。
def archive_roots(agent) -> list[Path]:
    roots: list[Path] = []
    home_paths = getattr(agent, "home_paths", None)
    owner_home = _path_or_none(getattr(home_paths, "owner_home_dir", None))
    if owner_home is not None:
        roots.append(owner_home)
    if _is_local_main_owner(home_paths):
        legacy_root = _path_or_none(getattr(agent, "root", None))
        if legacy_root is not None:
            roots.append(legacy_root)
    return _dedupe_paths(roots)


# LLM: collect_agent_archive_records merges allowed roots while preserving original archive record shape.
# 函数用途: 从允许的 archive 根目录收集记录，按原有时间排序与 limit 语义返回。
def collect_agent_archive_records(agent, request: ArchiveCollectRequest) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for root in archive_roots(agent):
        root_records = collect_archive_records(
            root,
            layer=request.layer,
            date_key=request.date_key,
            limit=0,
            level=request.level,
            file_limit=request.file_limit,
        )
        _extend_unique_archive_records(records, seen, root_records)
    records.sort(key=lambda item: (item["created_at_sort"], item["file_path"], item["line_no"]), reverse=True)
    return records[: request.limit] if request.limit > 0 else records


# LLM: _extend_unique_archive_records merges records without duplicating the same archive event.
# 函数用途: 将一个 root 的 archive 记录合并到结果列表，并按记录身份去重。
def _extend_unique_archive_records(
    records: list[dict[str, Any]],
    seen: set[tuple[str, str]],
    root_records: list[dict[str, Any]],
) -> None:
    for record in root_records:
        key = _archive_record_identity(record)
        if key in seen:
            continue
        seen.add(key)
        records.append(record)


# LLM: _archive_record_identity prefers stable archive ids and falls back to file coordinates.
# 函数用途: 计算 archive 记录去重键。
def _archive_record_identity(record: dict[str, Any]) -> tuple[str, str]:
    ident = str(record.get("id") or "").strip()
    if ident:
        return str(record.get("layer") or ""), ident
    return str(record.get("file_path") or ""), str(record.get("line_no") or "")


# LLM: _path_or_none normalizes optional path-like config values.
# 函数用途: 把非空路径值转成绝对 Path；空值返回 None。
def _path_or_none(value: Any) -> Path | None:
    if value in (None, ""):
        return None
    return Path(value).resolve()


# LLM: _dedupe_paths preserves root priority while removing duplicates.
# 函数用途: 按顺序去重 archive root 路径。
def _dedupe_paths(paths: list[Path]) -> list[Path]:
    deduped: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped


# LLM: _is_local_main_owner decides whether legacy workspace archives are still readable.
# 函数用途: 判断当前 owner 是否为 local/main，只有它能兼容读取旧 archive root。
def _is_local_main_owner(home_paths: Any) -> bool:
    return (
        str(getattr(home_paths, "owner_provider", "") or "local") == "local"
        and str(getattr(home_paths, "owner_kind", "") or "main") == "main"
        and str(getattr(home_paths, "owner_id", "") or "local/main") == "local/main"
    )


__all__ = ["ArchiveCollectRequest", "archive_roots", "collect_agent_archive_records"]
