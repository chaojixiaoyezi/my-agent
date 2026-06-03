
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..io import append_jsonl
from .home_layout import MyAgentHomePaths
from .owner_policy import read_owner_policy_bundle


@dataclass(frozen=True)
class RetentionAction:
    category: str
    path: Path
    reason: str
    status: str = "planned"

    def to_dict(self) -> dict[str, str]:
        return {
            "category": self.category,
            "path": str(self.path),
            "reason": self.reason,
            "status": self.status,
        }


@dataclass(frozen=True)
class OwnerRetentionPlan:
    applied: bool
    actions: tuple[RetentionAction, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"applied": self.applied, "actions": [action.to_dict() for action in self.actions]}


@dataclass(frozen=True)
class _RetentionSpec:
    category: str
    root: Path
    days_key: str
    default_days: int
    pattern: str


def plan_owner_retention(home: MyAgentHomePaths, *, now: datetime | None = None) -> OwnerRetentionPlan:
    current = _normalize_now(now)
    retention = read_owner_policy_bundle(home).retention
    actions: list[RetentionAction] = []
    for spec in _retention_specs(home):
        days = _retention_days(retention.get(spec.days_key), spec.default_days)
        if days <= 0:
            continue
        actions.extend(_expired_file_actions(spec, days=days, now=current))
    actions.sort(key=lambda action: (action.category, str(action.path)))
    return OwnerRetentionPlan(applied=False, actions=tuple(actions))


def apply_owner_retention(home: MyAgentHomePaths, *, now: datetime | None = None) -> OwnerRetentionPlan:
    applied: list[RetentionAction] = []
    for action in plan_owner_retention(home, now=now).actions:
        applied.append(_apply_retention_action(action))
    _append_retention_audit(home, applied, now=_normalize_now(now))
    return OwnerRetentionPlan(applied=True, actions=tuple(applied))


def _append_retention_audit(home: MyAgentHomePaths, actions: list[RetentionAction], *, now: datetime) -> None:
    append_jsonl(
        home.owner_audit_log_jsonl,
        {
            "schema_version": "owner-audit.v1",
            "event_type": "owner_retention_applied",
            "owner_id": str(getattr(home, "owner_id", "") or ""),
            "actions": [action.to_dict() for action in actions],
            "updated_at": now.isoformat(),
        },
        sort_keys=True,
    )


def _retention_specs(home: MyAgentHomePaths) -> tuple[_RetentionSpec, ...]:
    return (
        _RetentionSpec("audit", home.owner_audit_dir, "raw_days", 90, "*.jsonl"),
        _RetentionSpec("daily", home.owner_memory_daily_dir, "daily_days", 365, "*.jsonl"),
        _RetentionSpec("hooks", home.owner_memory_hooks_dir, "hooks_days", 180, "*.jsonl"),
        _RetentionSpec("compact", home.owner_compact_dir, "compact_days", 365, "*"),
        _RetentionSpec("cache", home.owner_cache_dir, "cache_days", 30, "*"),
        _RetentionSpec("tmp", home.owner_tmp_dir, "tmp_days", 7, "*"),
        _RetentionSpec("trash", home.owner_trash_dir, "trash_days", 30, "*"),
    )


def _expired_file_actions(
    spec: _RetentionSpec,
    *,
    days: int,
    now: datetime,
) -> list[RetentionAction]:
    root = spec.root
    if not root.exists():
        return []
    cutoff = now.timestamp() - days * 86400
    actions: list[RetentionAction] = []
    for path in sorted(root.rglob(spec.pattern)):
        if not path.is_file() or _mtime(path) >= cutoff:
            continue
        actions.append(
            RetentionAction(
                category=spec.category,
                path=path,
                reason=f"older_than_{days}_days",
            )
        )
    return actions


def _apply_retention_action(action: RetentionAction) -> RetentionAction:
    try:
        action.path.unlink()
    except FileNotFoundError:
        return RetentionAction(action.category, action.path, action.reason, status="missing")
    except OSError as exc:
        return RetentionAction(action.category, action.path, f"{action.reason}; error={type(exc).__name__}: {exc}", status="failed")
    return RetentionAction(action.category, action.path, action.reason, status="deleted")


def _retention_days(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else 0


def _normalize_now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


__all__ = ["OwnerRetentionPlan", "RetentionAction", "apply_owner_retention", "plan_owner_retention"]
