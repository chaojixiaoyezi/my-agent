"""审计日志查询。

提供审计日志的查询和统计功能。
"""

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


class AuditQuery:
    """审计日志查询器。

    查询审计日志：
    - 支持按用户、动作、目标、时间范围过滤
    - 支持分页
    - 支持统计摘要
    """

    def __init__(self, config: AgentConfig):
        """初始化审计查询器。

        Args:
            config: 智能体配置对象
        """
        self.config = config
        self._audit_root = Path(getattr(config, "audit_log_path", "data/audit"))
        self._audit_file = self._audit_root / "audit.jsonl"

    def query(self, params: AuditQueryParams | None = None, **kwargs) -> AuditQueryResult:
        """查询审计日志。

        Args:
            params: AuditQueryParams对象，或None（向后兼容）
            **kwargs: 向后兼容的关键字参数

        Returns:
            AuditQueryResult 包含条目列表和总数
        """
        if params is None and not kwargs:
            params = AuditQueryParams()
        elif isinstance(params, AuditQueryParams):
            pass
        else:
            # Backward compatibility: convert kwargs to AuditQueryParams
            params = AuditQueryParams(
                user_id=kwargs.pop("user_id", None),
                action=kwargs.pop("action", None),
                target_id=kwargs.pop("target_id", None),
                target_type=kwargs.pop("target_type", None),
                status=kwargs.pop("status", None),
                start_time=kwargs.pop("start_time", None),
                end_time=kwargs.pop("end_time", None),
                limit=kwargs.pop("limit", 100),
                offset=kwargs.pop("offset", 0),
            )

        start_query = time.time()

        if not self._audit_file.exists():
            return AuditQueryResult(
                entries=[],
                total_count=0,
                query_time_ms=0,
            )

        entries: list[AuditEntry] = []
        total_count = 0

        action_str = (
            params.action.value if isinstance(params.action, AuditAction) else params.action
        )

        try:
            with open(self._audit_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    total_count += 1

                    # 过滤条件
                    if params.user_id and data.get("user_id") != params.user_id:
                        continue
                    if action_str and data.get("action") != action_str:
                        continue
                    if params.target_id and data.get("target_id") != params.target_id:
                        continue
                    if params.target_type and data.get("target_type") != params.target_type:
                        continue
                    if params.status and data.get("status") != params.status:
                        continue

                    timestamp = data.get("timestamp", 0)
                    if params.start_time and timestamp < params.start_time:
                        continue
                    if params.end_time and timestamp > params.end_time:
                        continue

                    entries.append(AuditEntry.from_dict(data))

        except (OSError, UnicodeDecodeError):
            pass

        # 按时间倒序
        entries.sort(key=lambda e: e.timestamp, reverse=True)

        # 应用分页
        total = len(entries)
        entries = entries[params.offset : params.offset + params.limit]

        query_time_ms = (time.time() - start_query) * 1000

        return AuditQueryResult(
            entries=entries,
            total_count=total,
            query_time_ms=query_time_ms,
        )

    def summary(self, user_id: str | None = None) -> dict[str, Any]:
        """获取用户操作摘要。

        Args:
            user_id: 用户 ID，为空时查询所有用户

        Returns:
            摘要统计
        """
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

        try:
            with open(self._audit_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if user_id and data.get("user_id") != user_id:
                        continue

                    action = data.get("action", "UNKNOWN")
                    status = data.get("status", "UNKNOWN")
                    timestamp = data.get("timestamp", 0)

                    action_counts[action] = action_counts.get(action, 0) + 1
                    status_counts[status] = status_counts.get(status, 0) + 1

                    if timestamp > last_action_time:
                        last_action_time = timestamp

        except (OSError, UnicodeDecodeError):
            pass

        return {
            "user_id": user_id,
            "total_actions": sum(action_counts.values()),
            "by_action": action_counts,
            "by_status": status_counts,
            "last_action_time": last_action_time,
        }

    def recent_users(self, limit: int = 10) -> list[dict[str, Any]]:
        """获取最近活跃的用户。

        Args:
            limit: 最多返回多少用户

        Returns:
            用户列表，每个用户包含 user_id 和最后活动时间
        """
        if not self._audit_file.exists():
            return []

        user_last_time: dict[str, float] = {}

        try:
            with open(self._audit_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    user = data.get("user_id", "unknown")
                    timestamp = data.get("timestamp", 0)

                    if user not in user_last_time or timestamp > user_last_time[user]:
                        user_last_time[user] = timestamp

        except (OSError, UnicodeDecodeError):
            pass

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
            with (
                open(self._audit_file, encoding="utf-8") as f_in,
                open(temp_file, "w", encoding="utf-8") as f_out,
            ):
                for line in f_in:
                    line = line.strip()
                    if not line:
                        continue
                    deleted = self._process_cleanup_line(line, cutoff_time)
                    if deleted:
                        deleted_count += 1
                    else:
                        f_out.write(line + "\n")
        except OSError:
            if temp_file.exists():
                temp_file.unlink()
            return 0
        return deleted_count

    def _process_cleanup_line(self, line: str, cutoff_time: float) -> bool:
        """Return True if line should be deleted, False to keep."""
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return True
        timestamp = data.get("timestamp", 0)
        return timestamp < cutoff_time


__all__ = ["AuditQuery", "AuditQueryResult"]
