from __future__ import annotations

"""Structured contracts for reusable subagent workflow templates."""

from dataclasses import dataclass, field


@dataclass
class WorkflowPhase:
    """One executable phase in a workflow template."""

    id: str
    kind: str
    task: str
    acceptance: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)


@dataclass
class WorkflowTemplate:
    """A reusable workflow shape that can be selected for a parent task."""

    id: str
    name: str
    solves: list[str]
    fit_for: list[str]
    phases: list[WorkflowPhase]
    parent_acceptance: list[str]
    source: str = ""
    source_path: str = ""


@dataclass
class WorkflowLoadIssue:
    """A non-fatal template loading or validation problem."""

    message: str
    source_path: str = ""
    template_id: str = ""
    field: str = ""
    severity: str = "error"
