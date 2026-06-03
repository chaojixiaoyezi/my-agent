
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .debug_trace import SubAgentDebugTraceRequest, write_subagent_debug_trace


@dataclass(frozen=True)
class SubAgentReportTraceRequest:
    manager: Any
    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    task: Any = None
    level: int = 3


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


def trace_dispatch_report(manager: Any, report: Any) -> Any:
    return _trace_report(
        SubAgentReportTraceRequest(
            manager=manager,
            event_type="dispatch_report",
            payload=_dispatch_payload(report),
        ),
        report,
    )


def trace_dispatch_watch_report(manager: Any, report: Any) -> Any:
    return _trace_report(
        SubAgentReportTraceRequest(
            manager=manager,
            event_type="dispatch_watch_report",
            payload=_dispatch_payload(report),
        ),
        report,
    )


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


def _dispatch_payload(report: Any) -> dict[str, Any]:
    return {
        "record_count": len(getattr(report, "records", []) or []),
        "dry_run": bool(getattr(report, "dry_run", True)),
        "summary": dict(getattr(report, "summary", {}) or {}),
    }


def _issue_kinds(issues: list[Any]) -> list[str]:
    return [str(getattr(issue, "kind", "") or "") for issue in issues[:32]]


def _action_names(actions: list[Any]) -> list[str]:
    return [str(getattr(action, "action", "") or "") for action in actions[:32]]


def _run_ids(nodes: list[Any]) -> list[str]:
    return [str(getattr(node, "run_id", "") or "") for node in nodes[:32]]
