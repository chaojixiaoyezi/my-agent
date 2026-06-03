
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ActivityTimeoutPolicy:
    idle_timeout_seconds: int
    wall_timeout_seconds: int = 0
    reserved: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ActivitySnapshot:
    started_at: float
    now: float
    last_activity_at: float = 0
    latest_checkpoint_ref: str = ""
    latest_recovery_snapshot_ref: str = ""
    active_tool_count: int = 0
    reserved: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ActivityTimeoutDecision:
    action: str
    timed_out: bool
    reason: str = ""
    idle_seconds: float = 0
    wall_seconds: float = 0
    recovery_refs: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "timed_out": self.timed_out,
            "reason": self.reason,
            "idle_seconds": self.idle_seconds,
            "wall_seconds": self.wall_seconds,
            "recovery_refs": dict(self.recovery_refs),
        }


def decide_activity_timeout(policy: ActivityTimeoutPolicy, snapshot: ActivitySnapshot) -> ActivityTimeoutDecision:
    last_activity_at = snapshot.last_activity_at or snapshot.started_at
    idle_seconds = max(0.0, float(snapshot.now) - float(last_activity_at))
    wall_seconds = max(0.0, float(snapshot.now) - float(snapshot.started_at))
    recovery_refs = _recovery_refs(snapshot)
    if policy.idle_timeout_seconds <= 0:
        return ActivityTimeoutDecision(
            action="keep_running",
            timed_out=False,
            reason="idle_timeout_disabled",
            idle_seconds=idle_seconds,
            wall_seconds=wall_seconds,
            recovery_refs=recovery_refs,
        )
    if snapshot.active_tool_count > 0 or idle_seconds <= policy.idle_timeout_seconds:
        reason = "active_tool" if snapshot.active_tool_count > 0 else "recent_activity"
        return ActivityTimeoutDecision(
            action="keep_running",
            timed_out=False,
            reason=reason,
            idle_seconds=idle_seconds,
            wall_seconds=wall_seconds,
            recovery_refs=recovery_refs,
        )
    return ActivityTimeoutDecision(
        action="write_recovery_and_pause",
        timed_out=True,
        reason="idle_timeout",
        idle_seconds=idle_seconds,
        wall_seconds=wall_seconds,
        recovery_refs=recovery_refs,
    )


def _recovery_refs(snapshot: ActivitySnapshot) -> dict[str, str]:
    refs: dict[str, str] = {}
    if snapshot.latest_checkpoint_ref:
        refs["latest_checkpoint_ref"] = snapshot.latest_checkpoint_ref
    if snapshot.latest_recovery_snapshot_ref:
        refs["latest_recovery_snapshot_ref"] = snapshot.latest_recovery_snapshot_ref
    return refs


__all__ = [
    "ActivitySnapshot",
    "ActivityTimeoutDecision",
    "ActivityTimeoutPolicy",
    "decide_activity_timeout",
]
