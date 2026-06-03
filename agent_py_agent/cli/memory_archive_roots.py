
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..agent.memory_archive.query import collect_archive_records


@dataclass(frozen=True)
class ArchiveCollectRequest:
    layer: str = "all"
    date_key: str | None = None
    limit: int = 0
    level: int | None = None
    file_limit: int = 30


def archive_roots(agent) -> list[Path]:
    roots: list[Path] = []
    home_paths = getattr(agent, "home_paths", None)
    owner_home = _path_or_none(getattr(home_paths, "owner_home_dir", None))
    if owner_home is not None:
        roots.append(owner_home)
    return _dedupe_paths(roots)


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


def _archive_record_identity(record: dict[str, Any]) -> tuple[str, str]:
    ident = str(record.get("id") or "").strip()
    if ident:
        return str(record.get("layer") or ""), ident
    return str(record.get("file_path") or ""), str(record.get("line_no") or "")


def _path_or_none(value: Any) -> Path | None:
    if value in (None, ""):
        return None
    return Path(value).resolve()


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


__all__ = ["ArchiveCollectRequest", "archive_roots", "collect_agent_archive_records"]
