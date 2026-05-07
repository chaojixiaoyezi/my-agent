
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..settings.config import AgentConfig

from .logger import AuditAction, AuditEntry


@dataclass
class AuditQueryResult:
    """审计查询结果。"""

    entries: list[AuditEntry]
    total_count: int
    query_time_ms: float


@dataclass(frozen=True)
class AuditQueryParams:
    """Bundle of AuditQuery.query parameters."""

    user_id: str | None = None
    action: AuditAction | str | None = None
    target_id: str | None = None
    target_type: str | None = None
    status: str | None = None
    start_time: float | None = None
    end_time: float | None = None
    limit: int = 100
    offset: int = 0


def _normalize_query_params(
    params: AuditQueryParams | None,
    *,
    user_id: str | None = None,
    action: AuditAction | str | None = None,
    target_id: str | None = None,
    target_type: str | None = None,
    status: str | None = None,
    start_time: float | None = None,
    end_time: float | None = None,
    limit: int = 100,
    offset: int = 0,
) -> AuditQueryParams:
    if isinstance(params, AuditQueryParams):
        return params
    return AuditQueryParams(
        user_id=user_id,
        action=action,
        target_id=target_id,
        target_type=target_type,
        status=status,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
        offset=offset,
    )


def _iter_audit_dicts(path: Path):
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return
    for line in lines:
        data = _parse_audit_line(line)
        if data is not None:
            yield data


def _parse_audit_line(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line:
        return None
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _audit_entry_matches(data: dict[str, Any], params: AuditQueryParams, action_str: str | None) -> bool:
    if params.user_id and data.get("user_id") != params.user_id:
        return False
    if action_str and data.get("action") != action_str:
        return False
    if params.target_id and data.get("target_id") != params.target_id:
        return False
    if params.target_type and data.get("target_type") != params.target_type:
        return False
    if params.status and data.get("status") != params.status:
        return False
    return _timestamp_matches(data.get("timestamp", 0), params)


def _timestamp_matches(timestamp: float, params: AuditQueryParams) -> bool:
    if params.start_time and timestamp < params.start_time:
        return False
    if params.end_time and timestamp > params.end_time:
        return False
    return True


class AuditQuery:

    def __init__(self, config: AgentConfig):
        self.config = config
        self._audit_root = Path(getattr(config, "audit_log_path", "data/audit"))
        self._audit_file = self._audit_root / "audit.jsonl"

    def query(
        self,
        params: AuditQueryParams | None = None,
        *,
        user_id: str | None = None,
        action: AuditAction | str | None = None,
        target_id: str | None = None,
        target_type: str | None = None,
        status: str | None = None,
        start_time: float | None = None,
        end_time: float | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> AuditQueryResult:
        params = _normalize_query_params(
            params,
            user_id=user_id,
            action=action,
            target_id=target_id,
            target_type=target_type,
            status=status,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
            offset=offset,
        )
        start_query = time.time()
        if not self._audit_file.exists():
            return AuditQueryResult(entries=[], total_count=0, query_time_ms=0)

        action_str = (
            params.action.value if isinstance(params.action, AuditAction) else params.action
        )
        entries = [
            AuditEntry.from_dict(data)
            for data in _iter_audit_dicts(self._audit_file)
            if _audit_entry_matches(data, params, action_str)
        ]
        entries.sort(key=lambda e: e.timestamp, reverse=True)
        total = len(entries)
        entries = entries[params.offset : params.offset + params.limit]
        query_time_ms = (time.time() - start_query) * 1000
        return AuditQueryResult(
            entries=entries,
            total_count=total,
            query_time_ms=query_time_ms,
        )

    def summary(self, user_id: str | None = None) -> dict[str, Any]:
        if not self._audit_file.exists():
            return {
                "user_id": user_id,
                "total_actions": 0,
                "by_action": {},
                "by_status": {},
                "last_action_time": 0,
            }

        action_counts: dict[str, int] = {}
        status_counts: dict[str, int] = {}
        last_action_time = 0.0

        for data in _iter_audit_dicts(self._audit_file):
            if user_id and data.get("user_id") != user_id:
                continue
            action = data.get("action", "UNKNOWN")
            status = data.get("status", "UNKNOWN")
            timestamp = data.get("timestamp", 0)
            action_counts[action] = action_counts.get(action, 0) + 1
            status_counts[status] = status_counts.get(status, 0) + 1
            if timestamp > last_action_time:
                last_action_time = timestamp

        return {
            "user_id": user_id,
            "total_actions": sum(action_counts.values()),
            "by_action": action_counts,
            "by_status": status_counts,
            "last_action_time": last_action_time,
        }

    def recent_users(self, limit: int = 10) -> list[dict[str, Any]]:
        if not self._audit_file.exists():
            return []

        user_last_time: dict[str, float] = {}

        for data in _iter_audit_dicts(self._audit_file):
            user = data.get("user_id", "unknown")
            timestamp = data.get("timestamp", 0)
            if user not in user_last_time or timestamp > user_last_time[user]:
                user_last_time[user] = timestamp

        # 按最后活动时间排序
        sorted_users = sorted(user_last_time.items(), key=lambda x: x[1], reverse=True)

        return [
            {"user_id": user, "last_action_time": last_time}
            for user, last_time in sorted_users[:limit]
        ]

    def cleanup_old_entries(self, days: int = 90) -> int:
        """清理旧审计条目."""
        if not self._audit_file.exists():
            return 0
        cutoff_time = time.time() - (days * 24 * 60 * 60)
        temp_file = self._audit_file.with_suffix(".tmp")
        deleted_count = self._cleanup_entries(cutoff_time, temp_file)
        if deleted_count > 0:
            temp_file.replace(self._audit_file)
        return deleted_count

    def _cleanup_entries(self, cutoff_time: float, temp_file: Path) -> int:
        """Clean up entries older than cutoff_time; return deleted count."""
        deleted_count = 0
        try:
            lines = self._audit_file.read_text(encoding="utf-8").splitlines()
            kept_lines, deleted_count = _cleanup_audit_lines(lines, cutoff_time)
            temp_file.write_text("\n".join(kept_lines) + ("\n" if kept_lines else ""), encoding="utf-8")
        except OSError:
            if temp_file.exists():
                temp_file.unlink()
            return 0
        return deleted_count


def _cleanup_audit_lines(lines: list[str], cutoff_time: float) -> tuple[list[str], int]:
    kept_lines = []
    deleted_count = 0
    for line in lines:
        data = _parse_audit_line(line)
        if data is not None and data.get("timestamp", 0) >= cutoff_time:
            kept_lines.append(line.strip())
        else:
            deleted_count += 1
    return kept_lines, deleted_count


__all__ = ["AuditQuery", "AuditQueryResult"]
