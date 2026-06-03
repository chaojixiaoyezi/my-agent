
"""Data structures for log-analysis subagent work-order planning."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class SubagentWorkOrder:
    """Work-order card passed to an analyst or reviewer subagent."""

    role: str
    case_id: str
    goal: str
    mode: str = "manual"
    dry_run: bool = True
    ready: bool = False
    allowed_tools: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    acceptance_checks: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a stable serializable work-order payload."""
        return asdict(self)


@dataclass
class LogAnalysisWorkOrderPlan:
    """Dry-run plan containing the case work orders and readiness state."""

    case_id: str
    ready: bool
    dry_run: bool = True
    mode: str = "manual"
    work_orders: list[SubagentWorkOrder] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return the complete serializable plan snapshot."""
        payload = asdict(self)
        payload["work_orders"] = [order.to_dict() for order in self.work_orders]
        return payload


@dataclass(frozen=True)
class CreateWorkOrdersParams:
    """Params bundle for building the analyst and reviewer work orders."""

    case_id: str
    common_context: dict[str, Any]
    refs: list[str]
    checks: list[str]
    issues: list[str]
    risks: list[str]
    mode: str
    dry_run: bool
    ready: bool


@dataclass(frozen=True)
class PlanInputs:
    """Normalized inputs used to build a work-order plan."""

    summary: dict[str, Any]
    case_id: str
    route: dict[str, Any]
    refs: list[str]
    checks: list[str]
    issues: list[str]
    risks: list[str]
    ready: bool


__all__ = [
    "CreateWorkOrdersParams",
    "LogAnalysisWorkOrderPlan",
    "PlanInputs",
    "SubagentWorkOrder",
]
