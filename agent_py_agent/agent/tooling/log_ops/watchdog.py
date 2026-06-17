
from __future__ import annotations

"""值守看门狗 —— 确定性扫职责台账:挂了的(心跳停)标记 stalled + 产出重派动作,漏检的产出补派动作。

不靠 LLM 盯,系统级 cron/gateway 周期调 scan() 即可(LLM 会困会退,看门狗不会)。复用 DutyRegistry 的
stalled/coverage_gaps:超时代理标记 stalled 并产出"重派接管(带断点 resume_from,不重不漏)"动作,没人盯的源
产出"补派"动作。告警(挂了/漏检 → 推送用户)由调用方(log_watchdog_scan 工具 / cron)据 result 发,逻辑解耦。
"""

from dataclasses import dataclass, field
from typing import Any

from .duty_registry import DutyRegistry
from .store import LogOpsStore

_DEFAULT_STALL_SECONDS = 180.0


@dataclass
class WatchdogResult:
    """一次扫描的产出:谁挂了、哪漏了、该做什么、整体健康否。"""

    stalled: list[str] = field(default_factory=list)  # 心跳停的 assignment_id
    coverage_gaps: list[str] = field(default_factory=list)  # 没人盯的 source_id
    actions: list[dict[str, Any]] = field(default_factory=list)  # 重派/补派建议
    healthy: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "healthy": self.healthy,
            "stalled": self.stalled,
            "coverage_gaps": self.coverage_gaps,
            "actions": self.actions,
        }


def scan(store: LogOpsStore, *, now: float | None = None, stall_seconds: float = _DEFAULT_STALL_SECONDS) -> WatchdogResult:
    """扫一遍台账:超时代理标记 stalled + 产出重派动作(带断点续接依据),漏检源产出补派动作。"""
    registry = DutyRegistry(store.root)
    source_ids = [spec.source_id for spec in store.source_specs()]
    stalled = registry.stalled(now=now, stall_seconds=stall_seconds)
    gaps = registry.coverage_gaps(source_ids)
    actions: list[dict[str, Any]] = []
    for assignment in stalled:
        registry.mark_stalled(assignment.assignment_id)
        actions.append({
            "action": "reassign",
            "assignment_id": assignment.assignment_id,
            "agent_id": assignment.agent_id,
            "targets": assignment.targets,
            "owner": assignment.owner,
            "reason": "heartbeat_stalled",
            "resume_from": assignment.progress,  # 重派的代理从这个断点续接,不重不漏
        })
    actions.extend({"action": "assign_new", "target": source_id, "reason": "no_coverage"} for source_id in gaps)
    return WatchdogResult(
        stalled=[a.assignment_id for a in stalled],
        coverage_gaps=gaps,
        actions=actions,
        healthy=not (stalled or gaps),
    )


__all__ = ["WatchdogResult", "scan"]
