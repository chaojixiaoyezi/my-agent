
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ProviderTrashResult:
    original: Path
    trashed: Path
    moved: bool


@dataclass(frozen=True)
class ProviderTrashRequest:
    paths: Any
    target: Path
    actor_id: str = ""
    reason: str = ""
    date: str | None = None


def trash_target_for(paths: Any, target: str | Path, *, date: str | None = None) -> Path:
    target_path = Path(target)
    name = _safe_trash_name(target_path.name)
    trash_date = date or date_type.today().isoformat()
    candidate = paths.trash_dir / trash_date / name
    counter = 1
    while candidate.exists():
        candidate = paths.trash_dir / trash_date / f"{target_path.stem}-{counter}{target_path.suffix}"
        counter += 1
    return candidate


def move_to_space_trash(request: ProviderTrashRequest) -> ProviderTrashResult:
    source = Path(request.target)
    if not _path_is_within_space(source, request.paths):
        raise ValueError(f"target is outside provider space: {source}")
    destination = trash_target_for(request.paths, source, date=request.date)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))
    result = ProviderTrashResult(original=source, trashed=destination, moved=True)
    record_provider_space_event(
        request.paths,
        event_type="move_to_trash",
        actor_id=request.actor_id,
        payload={"original": str(source), "trashed": str(destination), "reason": request.reason},
    )
    return result


def provider_trash_retention_days_from_agent_config(config: object) -> int:
    return max(0, int(getattr(config, "provider_space_trash_retention_days", 30) or 0))


def provider_destructive_actions_use_trash_from_agent_config(config: object) -> bool:
    return bool(getattr(config, "provider_space_destructive_actions_use_trash", True))


def purge_provider_trash(paths: Any, *, retention_days: int, today: str | date_type | None = None) -> list[Path]:
    days = max(0, int(retention_days or 0))
    if days == 0 or not paths.trash_dir.exists():
        return []
    cutoff = (_coerce_today(today) or date_type.today()) - timedelta(days=days - 1)
    deleted: list[Path] = []
    for child in sorted(paths.trash_dir.iterdir()):
        if _should_purge_trash_folder(child, paths, cutoff):
            shutil.rmtree(child)
            deleted.append(child)
    return deleted


def provider_audit_log_path(paths: Any) -> Path:
    return paths.data_dir / "audit.jsonl"


def record_provider_space_event(
    paths: Any,
    *,
    event_type: str,
    actor_id: str = "",
    payload: dict[str, object] | None = None,
) -> None:
    provider_audit_log_path(paths).parent.mkdir(parents=True, exist_ok=True)
    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "provider": paths.identity.provider,
        "space_type": paths.identity.space_type,
        "space_id": paths.identity.space_id,
        "actor_id": actor_id,
        "payload": payload or {},
    }
    with provider_audit_log_path(paths).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def _should_purge_trash_folder(path: Path, paths: Any, cutoff: date_type) -> bool:
    if not path.is_dir() or not _path_is_within_space(path, paths):
        return False
    folder_date = _date_from_folder(path)
    return bool(folder_date is not None and folder_date < cutoff)


def _path_is_within_space(path: str | Path, paths: Any) -> bool:
    try:
        Path(path).resolve().relative_to(paths.root_dir.resolve())
        return True
    except ValueError:
        return False


def _safe_trash_name(value: str) -> str:
    result = "".join(char if char.isalnum() or char in {"_", "-", "."} else "-" for char in str(value or ""))
    result = result.strip(".-_/")
    return result or "item"


def _coerce_today(value: str | date_type | None) -> date_type | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date_type):
        return value
    if isinstance(value, str):
        try:
            return date_type.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _date_from_folder(path: Path) -> date_type | None:
    try:
        return date_type.fromisoformat(path.name)
    except ValueError:
        return None
