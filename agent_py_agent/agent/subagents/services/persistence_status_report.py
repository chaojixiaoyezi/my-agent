# LLM: Status report construction is separated from persistence IO to keep save/load responsibilities thin.
# 模块用途: 根据 SubAgentTask 派生最新状态报告，供 persistence.save 写入 task.json。

from __future__ import annotations

from ..models import StatusReport, SubAgentTask


# LLM: build_status_report creates the next monotonically increasing subagent status report.
# 函数用途: 从任务当前状态、证据引用、blocker 和 checkpoint 构建 latest_status_report。
def build_status_report(task: SubAgentTask) -> StatusReport:
    previous = task.latest_status_report if isinstance(task.latest_status_report, StatusReport) else StatusReport()
    progress = max(0.0, min(1.0, _float_value(task.progress)))
    return StatusReport(
        run_id=task.id,
        version=max(0, int(previous.version or 0)) + 1,
        state=task.status,
        progress=progress,
        current_step=task.current_step or task.status,
        summary_delta=_summary_delta(task),
        budget_used=dict(task.budget_used or {}),
        artifact_refs=list(dict.fromkeys(task.artifact_refs)),
        evidence_refs=list(dict.fromkeys(task.evidence_refs)),
        blockers=list(dict.fromkeys(task.blockers)),
        checkpoint_ref=task.checkpoint_ref,
        next_recommended_action=(task.blockers[0] if task.blockers else ""),
        updated_at=task.updated_at or task.heartbeat_at or task.created_at,
    )


# LLM: _summary_delta keeps status reports concise and append-only.
# 函数用途: 把 latest_summary 和 blockers 转成事实增量，供父级 rollup 查询。
def _summary_delta(task: SubAgentTask) -> dict[str, list[str]]:
    return {
        "facts_added": [task.latest_summary] if task.latest_summary else [],
        "facts_invalidated": [],
        "decisions_changed": [],
        "open_questions": list(task.blockers),
    }


# LLM: _float_value normalizes progress values without raising during stale task loads.
# 函数用途: 将 progress 等可选字段转成 float，异常时使用默认值。
def _float_value(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
