# LLM: Report debug trace helpers keep observability summaries out of core service files.
# 模块用途: 为 due-check、action-plan、recovery-tree 和 dispatch 报告写 refs-only 调试摘要。

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .debug_trace import SubAgentDebugTraceRequest, write_subagent_debug_trace


# LLM: SubAgentReportTraceRequest bundles report trace fields so helpers stay extensible.
# 类用途: 保存报告类 trace 的 manager、事件名、等级、摘要 payload 和可选任务引用。
@dataclass(frozen=True)
class SubAgentReportTraceRequest:
    manager: Any
    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    task: Any = None
    level: int = 3


# LLM: trace_due_check_report records issue counts and top kinds without reading task bodies.
# 函数用途: 写 due-check 报告摘要 trace，并返回原 report 方便服务入口保持直通。
def trace_due_check_report(manager: Any, report: Any) -> Any:
    return _trace_report(
        SubAgentReportTraceRequest(
            manager=manager,
            event_type="due_check_report",
            payload={
                "issue_count": len(getattr(report, "issues", []) or []),
                "summary": dict(getattr(report, "summary", {}) or {}),
                "issue_kinds": _issue_kinds(getattr(report, "issues", []) or []),
            },
        ),
        report,
    )


# LLM: trace_action_plan_report records dry-run action decisions without applying them.
# 函数用途: 写 action-plan 报告摘要 trace，帮助定位下一步建议和恢复动作。
def trace_action_plan_report(manager: Any, report: Any) -> Any:
    return _trace_report(
        SubAgentReportTraceRequest(
            manager=manager,
            event_type="action_plan_report",
            payload={
                "action_count": len(getattr(report, "actions", []) or []),
                "summary": dict(getattr(report, "summary", {}) or {}),
                "actions": _action_names(getattr(report, "actions", []) or []),
            },
        ),
        report,
    )


# LLM: trace_hierarchy_recovery_result records recovery-tree candidates as ids and counts only.
# 函数用途: 写 recovery-tree 摘要 trace，不读取 takeover refs、artifact refs 或任务正文。
def trace_hierarchy_recovery_result(manager: Any, result: Any) -> Any:
    return _trace_report(
        SubAgentReportTraceRequest(
            manager=manager,
            event_type="hierarchy_recovery_packet",
            payload={
                "root_run_id": str(getattr(result, "root_run_id", "") or ""),
                "node_count": int(getattr(result, "node_count", 0) or 0),
                "candidate_count": int(getattr(result, "recovery_candidate_count", 0) or 0),
                "omitted_healthy_count": int(getattr(result, "omitted_healthy_count", 0) or 0),
                "candidate_run_ids": _run_ids(getattr(result, "recovery_candidates", []) or []),
            },
        ),
        result,
    )


# LLM: trace_dispatch_report records dispatch report counts after the report is written.
# 函数用途: 写 dispatch 报告摘要 trace，帮助测试长循环是否继续推进任务。
def trace_dispatch_report(manager: Any, report: Any) -> Any:
    return _trace_report(
        SubAgentReportTraceRequest(
            manager=manager,
            event_type="dispatch_report",
            payload=_dispatch_payload(report),
        ),
        report,
    )


# LLM: trace_dispatch_watch_report records watch-loop report counts without copying cycle logs.
# 函数用途: 写 dispatch-watch 报告摘要 trace，帮助定位 watch 是否空转或持续有变化。
def trace_dispatch_watch_report(manager: Any, report: Any) -> Any:
    return _trace_report(
        SubAgentReportTraceRequest(
            manager=manager,
            event_type="dispatch_watch_report",
            payload=_dispatch_payload(report),
        ),
        report,
    )


# LLM: _trace_report is the common bridge to the bounded JSONL writer.
# 函数用途: 写一条报告类 trace，并始终返回原对象，避免调用方行为变化。
def _trace_report(request: SubAgentReportTraceRequest, original: Any) -> Any:
    write_subagent_debug_trace(
        SubAgentDebugTraceRequest(
            manager=request.manager,
            level=request.level,
            event_type=request.event_type,
            task=request.task,
            payload=request.payload,
        )
    )
    return original


# LLM: _dispatch_payload keeps dispatch and watch summaries aligned.
# 函数用途: 从 dispatch/watch 报告中提取通用 summary、record_count 和 dry_run 字段。
def _dispatch_payload(report: Any) -> dict[str, Any]:
    return {
        "record_count": len(getattr(report, "records", []) or []),
        "dry_run": bool(getattr(report, "dry_run", True)),
        "summary": dict(getattr(report, "summary", {}) or {}),
    }


# LLM: _issue_kinds keeps due-check trace searchable without copying full messages.
# 函数用途: 提取最多 32 个 issue kind，避免长报告膨胀。
def _issue_kinds(issues: list[Any]) -> list[str]:
    return [str(getattr(issue, "kind", "") or "") for issue in issues[:32]]


# LLM: _action_names keeps action-plan trace searchable without copying reasons.
# 函数用途: 提取最多 32 个 action 名称，避免把 reason/message 正文写入 trace。
def _action_names(actions: list[Any]) -> list[str]:
    return [str(getattr(action, "action", "") or "") for action in actions[:32]]


# LLM: _run_ids keeps recovery trace focused on refs instead of node bodies.
# 函数用途: 提取最多 32 个恢复候选 run_id，方便 E2E 快速定位。
def _run_ids(nodes: list[Any]) -> list[str]:
    return [str(getattr(node, "run_id", "") or "") for node in nodes[:32]]
