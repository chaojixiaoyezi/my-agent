from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..common.json_io import locked_json_path, read_json_object_report, write_json_file_atomic
from ..io import append_jsonl
from .home_layout import MyAgentHomePaths

_TASK_TERMINAL_STATUSES = frozenset(
    {
        "ABANDONED",
        "CANCELLED",
        "CHANNEL_ERROR",
        "DONE",
        "FAILED",
        "TAKEN_OVER",
        "TIMEOUT",
    }
)
_SUBAGENT_SCRATCH_PATHS = (
    Path("inbox"),
    Path("outbox"),
    Path("compactions"),
    Path("artifacts") / "tool_outputs",
)


@dataclass(frozen=True)
class RetentionAction:
    category: str
    path: Path
    reason: str
    status: str = "planned"
    operation: str = "delete_file"
    destination: Path | None = None
    authority_path: Path | None = None
    authority_status: str = ""
    cutoff_timestamp: float | None = None

    def to_dict(self) -> dict[str, str]:
        payload = {
            "category": self.category,
            "path": str(self.path),
            "reason": self.reason,
            "status": self.status,
            "operation": self.operation,
        }
        if self.destination is not None:
            payload["destination"] = str(self.destination)
        if self.authority_path is not None:
            payload["authority_path"] = str(self.authority_path)
        if self.authority_status:
            payload["authority_status"] = self.authority_status
        return payload


@dataclass(frozen=True)
class OwnerRetentionPlan:
    applied: bool
    actions: tuple[RetentionAction, ...]
    load_errors: tuple[dict[str, object], ...] = field(default_factory=tuple)
    legal_hold: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "legal_hold": self.legal_hold,
            "actions": [action.to_dict() for action in self.actions],
            "load_errors": list(self.load_errors),
        }


@dataclass(frozen=True)
class _RetentionSpec:
    category: str
    root: Path
    days_key: str
    default_days: int
    pattern: str


def plan_owner_retention(home: MyAgentHomePaths, *, now: datetime | None = None) -> OwnerRetentionPlan:
    current = _normalize_now(now)
    report = read_json_object_report(
        home.owner_retention_json,
        context="owner_retention.policy",
    )
    if report.load_error is not None:
        return OwnerRetentionPlan(
            applied=False,
            actions=(),
            load_errors=(report.load_error,),
        )
    retention = report.payload
    if bool(retention.get("legal_hold", False)):
        return OwnerRetentionPlan(applied=False, actions=(), legal_hold=True)

    held_task_ids = frozenset(_string_list(retention.get("legal_hold_task_ids")))
    errors: list[dict[str, object]] = []
    task_actions, task_roots = _expired_task_actions(
        home,
        retention=retention,
        held_task_ids=held_task_ids,
        now=current,
        errors=errors,
    )
    actions: list[RetentionAction] = list(task_actions)
    actions.extend(
        _expired_subagent_scratch_actions(
            home,
            retention=retention,
            held_task_ids=held_task_ids,
            excluded_task_roots=task_roots,
            now=current,
            errors=errors,
        )
    )
    for spec in _retention_specs(home):
        days = _retention_days(retention.get(spec.days_key), spec.default_days)
        if days > 0:
            actions.extend(_expired_file_actions(spec, days=days, now=current))
    actions.extend(
        _expired_trash_actions(
            home,
            days=_retention_days(retention.get("trash_days"), 30),
            now=current,
            errors=errors,
        )
    )
    actions.sort(key=lambda action: (action.category, str(action.path)))
    return OwnerRetentionPlan(
        applied=False,
        actions=tuple(actions),
        load_errors=tuple(errors),
    )


def apply_owner_retention(home: MyAgentHomePaths, *, now: datetime | None = None) -> OwnerRetentionPlan:
    current = _normalize_now(now)
    lock_anchor = home.owner_trash_dir / ".retention"
    with locked_json_path(lock_anchor):
        plan = plan_owner_retention(home, now=current)
        if plan.load_errors or plan.legal_hold:
            _append_retention_audit(home, [], now=current, plan=plan)
            return OwnerRetentionPlan(
                applied=False,
                actions=(),
                load_errors=plan.load_errors,
                legal_hold=plan.legal_hold,
            )
        applied = [_apply_retention_action(action, now=current) for action in plan.actions]
        _append_retention_audit(home, applied, now=current, plan=plan)
        return OwnerRetentionPlan(applied=True, actions=tuple(applied))


def _append_retention_audit(
    home: MyAgentHomePaths,
    actions: list[RetentionAction],
    *,
    now: datetime,
    plan: OwnerRetentionPlan,
) -> None:
    append_jsonl(
        home.owner_audit_log_jsonl,
        {
            "schema_version": "owner-audit.v1",
            "event_type": "owner_retention_applied",
            "owner_id": str(getattr(home, "owner_id", "") or ""),
            "applied": not bool(plan.load_errors or plan.legal_hold),
            "legal_hold": plan.legal_hold,
            "load_errors": list(plan.load_errors),
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
    )


def _expired_task_actions(
    home: MyAgentHomePaths,
    *,
    retention: dict[str, Any],
    held_task_ids: frozenset[str],
    now: datetime,
    errors: list[dict[str, object]],
) -> tuple[list[RetentionAction], frozenset[Path]]:
    days = _retention_days(retention.get("task_completed_days"), 365)
    if days <= 0 or not home.owner_tasks_dir.exists():
        return [], frozenset()
    cutoff = now.timestamp() - days * 86400
    actions: list[RetentionAction] = []
    selected_roots: set[Path] = set()
    for state_path in sorted(home.owner_tasks_dir.rglob("work/state.json")):
        task_root = state_path.parent.parent
        if not _safe_child(task_root, home.owner_tasks_dir) or task_root.is_symlink():
            continue
        state = _read_retention_state(state_path, errors, context="owner_retention.task_state")
        if state is None:
            continue
        task_id = str(state.get("task_id") or "").strip()
        status = str(state.get("status") or "").strip().upper()
        updated_at = _state_timestamp(state.get("updated_at"))
        if task_id in held_task_ids or status not in _TASK_TERMINAL_STATUSES:
            continue
        if updated_at is None:
            errors.append(_state_timestamp_error(state_path, status))
            continue
        if updated_at >= cutoff:
            continue
        action = _trash_action(
            home,
            category="task_completed",
            path=task_root,
            reason=f"terminal_{status}_older_than_{days}_days",
            authority_path=state_path,
            authority_status=status,
            cutoff_timestamp=cutoff,
            now=now,
        )
        actions.append(action)
        selected_roots.add(task_root.resolve(strict=False))
    return actions, frozenset(selected_roots)


def _expired_subagent_scratch_actions(
    home: MyAgentHomePaths,
    *,
    retention: dict[str, Any],
    held_task_ids: frozenset[str],
    excluded_task_roots: frozenset[Path],
    now: datetime,
    errors: list[dict[str, object]],
) -> list[RetentionAction]:
    days = _retention_days(retention.get("subagent_scratch_days"), 30)
    if days <= 0 or not home.owner_tasks_dir.exists():
        return []
    cutoff = now.timestamp() - days * 86400
    actions: list[RetentionAction] = []
    for agents_dir in sorted(home.owner_tasks_dir.rglob("work/agents")):
        task_root = agents_dir.parent.parent
        resolved_task_root = task_root.resolve(strict=False)
        if resolved_task_root in excluded_task_roots or not _safe_child(task_root, home.owner_tasks_dir):
            continue
        task_state = _read_retention_state(
            task_root / "work" / "state.json",
            errors,
            context="owner_retention.task_state",
        )
        if task_state is None or str(task_state.get("task_id") or "").strip() in held_task_ids:
            continue
        for run_root in sorted(path for path in agents_dir.iterdir() if path.is_dir() and not path.is_symlink()):
            authority_path = _subagent_authority_path(run_root)
            if authority_path is None:
                continue
            state = _read_retention_state(
                authority_path,
                errors,
                context="owner_retention.subagent_state",
            )
            if state is None:
                continue
            status = str(state.get("status") or "").strip().upper()
            updated_at = _state_timestamp(state.get("updated_at"))
            if status not in _TASK_TERMINAL_STATUSES:
                continue
            if updated_at is None:
                errors.append(_state_timestamp_error(authority_path, status))
                continue
            if updated_at >= cutoff:
                continue
            for relative in _SUBAGENT_SCRATCH_PATHS:
                scratch = run_root / relative
                if not scratch.exists() or scratch.is_symlink():
                    continue
                actions.append(
                    _trash_action(
                        home,
                        category="subagent_scratch",
                        path=scratch,
                        reason=f"terminal_{status}_scratch_older_than_{days}_days",
                        authority_path=authority_path,
                        authority_status=status,
                        cutoff_timestamp=cutoff,
                        now=now,
                    )
                )
    return actions


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
        if path.is_symlink() or not path.is_file() or _mtime(path) >= cutoff:
            continue
        actions.append(
            RetentionAction(
                category=spec.category,
                path=path,
                reason=f"older_than_{days}_days",
            )
        )
    return actions


def _expired_trash_actions(
    home: MyAgentHomePaths,
    *,
    days: int,
    now: datetime,
    errors: list[dict[str, object]],
) -> list[RetentionAction]:
    if days <= 0 or not home.owner_trash_dir.exists():
        return []
    cutoff = now.timestamp() - days * 86400
    actions: list[RetentionAction] = []
    retention_root = home.owner_trash_dir / "retention"
    if retention_root.exists():
        for tombstone_path in sorted(retention_root.glob("*/*/tombstone.json")):
            container = tombstone_path.parent
            report = read_json_object_report(
                tombstone_path,
                context="owner_retention.tombstone",
            )
            if report.load_error is not None:
                errors.append(report.load_error)
                continue
            moved_at = _state_timestamp(report.payload.get("moved_at"))
            if moved_at is None:
                errors.append(_state_timestamp_error(tombstone_path, "TRASHED"))
                continue
            if moved_at < cutoff:
                actions.append(
                    RetentionAction(
                        category="trash",
                        path=container,
                        reason=f"trashed_older_than_{days}_days",
                        operation="delete_tree",
                    )
                )
    for path in sorted(home.owner_trash_dir.rglob("*")):
        if path == home.owner_trash_dir / ".retention.lock" or _safe_child(path, retention_root):
            continue
        if path.is_symlink() or not path.is_file() or _mtime(path) >= cutoff:
            continue
        actions.append(
            RetentionAction(
                category="trash",
                path=path,
                reason=f"older_than_{days}_days",
            )
        )
    return actions


def _apply_retention_action(action: RetentionAction, *, now: datetime) -> RetentionAction:
    if action.authority_path is not None and not _authority_still_expired(action):
        return _action_result(action, status="state_changed")
    if action.operation == "trash_tree":
        return _move_to_retention_trash(action, now=now)
    if action.operation == "delete_tree":
        try:
            shutil.rmtree(action.path)
        except FileNotFoundError:
            return _action_result(action, status="missing")
        except OSError as exc:
            return _action_failure(action, exc)
        return _action_result(action, status="deleted")
    try:
        action.path.unlink()
    except FileNotFoundError:
        return _action_result(action, status="missing")
    except OSError as exc:
        return _action_failure(action, exc)
    return _action_result(action, status="deleted")


def _authority_still_expired(action: RetentionAction) -> bool:
    assert action.authority_path is not None
    report = read_json_object_report(
        action.authority_path,
        context="owner_retention.revalidate_state",
    )
    if report.load_error is not None:
        return False
    status = str(report.payload.get("status") or "").strip().upper()
    timestamp = _state_timestamp(report.payload.get("updated_at"))
    return bool(
        status == action.authority_status
        and timestamp is not None
        and action.cutoff_timestamp is not None
        and timestamp < action.cutoff_timestamp
    )


def _move_to_retention_trash(action: RetentionAction, *, now: datetime) -> RetentionAction:
    destination = action.destination
    if destination is None:
        return _action_failure(action, ValueError("retention trash destination is missing"))
    try:
        if not action.path.exists():
            return _action_result(action, status="missing")
        destination.mkdir(parents=True, exist_ok=False)
        payload_path = destination / "payload"
        write_json_file_atomic(
            destination / "tombstone.json",
            {
                "schema_version": "owner-retention-tombstone.v1",
                "category": action.category,
                "source_path": str(action.path),
                "payload_path": str(payload_path),
                "reason": action.reason,
                "authority_path": str(action.authority_path or ""),
                "authority_status": action.authority_status,
                "moved_at": now.isoformat(),
                "status": "moving",
            },
        )
        shutil.move(str(action.path), str(payload_path))
        write_json_file_atomic(
            destination / "tombstone.json",
            {
                "schema_version": "owner-retention-tombstone.v1",
                "category": action.category,
                "source_path": str(action.path),
                "payload_path": str(payload_path),
                "reason": action.reason,
                "authority_path": str(action.authority_path or ""),
                "authority_status": action.authority_status,
                "moved_at": now.isoformat(),
                "status": "trashed",
            },
        )
    except FileExistsError:
        return _action_result(action, status="collision")
    except OSError as exc:
        return _action_failure(action, exc)
    return _action_result(action, status="trashed")


def _trash_action(
    home: MyAgentHomePaths,
    *,
    category: str,
    path: Path,
    reason: str,
    authority_path: Path,
    authority_status: str,
    cutoff_timestamp: float,
    now: datetime,
) -> RetentionAction:
    digest = hashlib.sha256(str(path.resolve(strict=False)).encode("utf-8")).hexdigest()[:12]
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    destination = home.owner_trash_dir / "retention" / category / f"{stamp}-{path.name}-{digest}"
    return RetentionAction(
        category=category,
        path=path,
        reason=reason,
        operation="trash_tree",
        destination=destination,
        authority_path=authority_path,
        authority_status=authority_status,
        cutoff_timestamp=cutoff_timestamp,
    )


def _subagent_authority_path(run_root: Path) -> Path | None:
    canonical = run_root / "canonical_state.json"
    if canonical.is_file() and not canonical.is_symlink():
        return canonical
    state = run_root / "state.json"
    if state.is_file() and not state.is_symlink():
        return state
    return None


def _read_retention_state(
    path: Path,
    errors: list[dict[str, object]],
    *,
    context: str,
) -> dict[str, Any] | None:
    report = read_json_object_report(path, context=context)
    if report.load_error is not None:
        errors.append(report.load_error)
        return None
    return report.payload or None


def _state_timestamp(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (OverflowError, ValueError):
        return None


def _state_timestamp_error(path: Path, status: str) -> dict[str, object]:
    return {
        "error_code": "RETENTION_STATE_TIMESTAMP_UNAVAILABLE",
        "error_type": "RetentionStateTimestampUnavailable",
        "message": "terminal state has no valid updated_at; cleanup skipped",
        "path": str(path),
        "status": status,
    }


def _safe_child(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _action_result(action: RetentionAction, *, status: str) -> RetentionAction:
    return RetentionAction(
        category=action.category,
        path=action.path,
        reason=action.reason,
        status=status,
        operation=action.operation,
        destination=action.destination,
        authority_path=action.authority_path,
        authority_status=action.authority_status,
        cutoff_timestamp=action.cutoff_timestamp,
    )


def _action_failure(action: RetentionAction, exc: BaseException) -> RetentionAction:
    return RetentionAction(
        category=action.category,
        path=action.path,
        reason=f"{action.reason}; error={type(exc).__name__}: {exc}",
        status="failed",
        operation=action.operation,
        destination=action.destination,
        authority_path=action.authority_path,
        authority_status=action.authority_status,
        cutoff_timestamp=action.cutoff_timestamp,
    )


def _retention_days(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else 0


def _string_list(value: object) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [text for item in value if (text := str(item or "").strip())]


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
