# LLM: Activity timeout contracts separate idle failure from healthy long-running work.
# 模块用途: 为长任务、真实模型 E2E 和恢复包提供机器可读的超时判断；只因无活动暂停，不因总耗时长直接杀任务。

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# LLM: ActivityTimeoutPolicy bundles timeout thresholds without adding workflow-specific rules.
# 类用途: 描述空闲超时和可选总时长提示；wall_timeout 不覆盖最近活动事实。
@dataclass(frozen=True)
class ActivityTimeoutPolicy:
    idle_timeout_seconds: int
    wall_timeout_seconds: int = 0
    reserved: dict[str, Any] = field(default_factory=dict)


# LLM: ActivitySnapshot is the structured state needed for one timeout decision.
# 类用途: 保存任务开始时间、当前时间、最后活动时间、活动工具数和恢复引用。
@dataclass(frozen=True)
class ActivitySnapshot:
    started_at: float
    now: float
    last_activity_at: float = 0
    latest_checkpoint_ref: str = ""
    latest_recovery_snapshot_ref: str = ""
    active_tool_count: int = 0
    reserved: dict[str, Any] = field(default_factory=dict)


# LLM: ActivityTimeoutDecision records whether to continue or write a recovery packet.
# 类用途: 输出机器动作、原因和恢复引用；调用方不用解析错误文本决定下一步。
@dataclass(frozen=True)
class ActivityTimeoutDecision:
    action: str
    timed_out: bool
    reason: str = ""
    idle_seconds: float = 0
    wall_seconds: float = 0
    recovery_refs: dict[str, str] = field(default_factory=dict)

    # LLM: to_dict gives logs and tests a stable timeout payload.
    # 函数用途: 转成普通 dict，方便写入 ledger、checkpoint 或恢复包。
    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "timed_out": self.timed_out,
            "reason": self.reason,
            "idle_seconds": self.idle_seconds,
            "wall_seconds": self.wall_seconds,
            "recovery_refs": dict(self.recovery_refs),
        }


# LLM: decide_activity_timeout implements 长期助手 activity-aware timeout behavior.
# 函数用途: 最近仍有工具/模型活动时继续运行；真正空闲超过阈值时要求写恢复包并暂停。
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


# LLM: _recovery_refs preserves continuation anchors without reading task prose.
# 函数用途: 从 snapshot 提取 checkpoint/recovery 引用，空值不写入。
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
