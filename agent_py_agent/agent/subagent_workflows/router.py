
from __future__ import annotations

"""route parent goals to reusable workflow templates without mutating task state."""

from dataclasses import dataclass, field
from typing import Any

from .store import WorkflowTemplateStore, load_template_store

SINGLE_WORKER_TEMPLATE_ID = "single_worker_verified"
CODE_FEATURE_TEMPLATE_ID = "code_feature_split"
PRODUCER_CRITIC_TEMPLATE_ID = "producer_critic_repair"
DEFAULT_MODE = "auto"
VALID_MODES = {"off", "manual", "auto"}


@dataclass
class WorkflowRouteDecision:
    """Structured decision returned by the workflow router."""

    mode: str
    selected_template_id: str
    reason: str
    task_type: str
    risk_tags: list[str] = field(default_factory=list)
    needs_confirmation: bool = False
    available_template_ids: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _RouteDecisionFields:
    mode: str
    selected_template_id: str
    reason: str
    task_type: str
    risk_tags: list[str]
    needs_confirmation: bool
    available_template_ids: list[str]
    issues: list[str]


@dataclass(frozen=True)
class WorkflowRouteRequest:
    goal: str
    config: Any = None
    template_store: WorkflowTemplateStore | None = None
    explicit_template_id: str = ""
    workflow_task_type: str = ""
    workflow_risk_tags: object = None


@dataclass(frozen=True)
class _TemplateSelectionRequest:
    explicit_template_id: str
    preferred_template_id: str
    store: WorkflowTemplateStore
    available_template_ids: list[str]
    issues: list[str]


@dataclass(frozen=True)
class _RouteFieldsRequest:
    mode: str
    selected_template_id: str
    task_type: str
    risk_tags: list[str]
    available_template_ids: list[str]
    issues: list[str]
    reason: str = "Subagent workflow routing is disabled by config."


_TASK_TYPE_TEMPLATE_MAP = {
    "quality_deliverable": PRODUCER_CRITIC_TEMPLATE_ID,
    "code_or_bugfix": CODE_FEATURE_TEMPLATE_ID,
    "simple": SINGLE_WORKER_TEMPLATE_ID,
}


def route_workflow(
    request: WorkflowRouteRequest,
) -> WorkflowRouteDecision:
    """Choose a workflow template for a parent goal."""

    store, available_template_ids, issues, mode = _workflow_route_inputs(request.config, request.template_store)
    task_type = _workflow_task_type(request.workflow_task_type)
    preferred_template_id = _TASK_TYPE_TEMPLATE_MAP.get(task_type, SINGLE_WORKER_TEMPLATE_ID)
    risk_tags = _workflow_risk_tags(
        request.workflow_risk_tags,
        task_type=task_type,
        explicit_template_id=request.explicit_template_id,
    )

    if mode == "off":
        return _disabled_route_decision(_route_fields(_RouteFieldsRequest(mode, "", task_type, risk_tags, available_template_ids, issues)))

    selected_template_id, reason = _select_template(
        _TemplateSelectionRequest(
            explicit_template_id=request.explicit_template_id,
            preferred_template_id=preferred_template_id,
            store=store,
            available_template_ids=available_template_ids,
            issues=issues,
        )
    )

    if selected_template_id and mode == "manual":
        reason = f"{reason} Manual mode requires parent confirmation before use."

    return _make_route_decision(
        _route_fields(
            _RouteFieldsRequest(mode, selected_template_id, task_type, risk_tags, available_template_ids, issues, reason)
        )
    )


def _disabled_route_decision(fields: _RouteDecisionFields) -> WorkflowRouteDecision:
    """Build the off-mode route result without lengthening the public API."""
    return _make_route_decision(fields)


def _route_fields(request: _RouteFieldsRequest) -> _RouteDecisionFields:
    return _RouteDecisionFields(
        mode=request.mode,
        selected_template_id=request.selected_template_id,
        reason=request.reason,
        task_type=request.task_type,
        risk_tags=request.risk_tags,
        needs_confirmation=request.mode == "manual",
        available_template_ids=request.available_template_ids,
        issues=request.issues,
    )


def _workflow_route_inputs(
    config: Any,
    template_store: WorkflowTemplateStore | None,
) -> tuple[WorkflowTemplateStore, list[str], list[str], str]:
    store = template_store or load_template_store()
    available_template_ids = [template.id for template in store.all()]
    issues = [_format_store_issue(issue) for issue in store.issues]
    mode = _workflow_mode(config, issues)
    return store, available_template_ids, issues, mode


def _make_route_decision(fields: _RouteDecisionFields) -> WorkflowRouteDecision:
    return WorkflowRouteDecision(
        mode=fields.mode,
        selected_template_id=fields.selected_template_id,
        reason=fields.reason,
        task_type=fields.task_type,
        risk_tags=fields.risk_tags,
        needs_confirmation=fields.needs_confirmation,
        available_template_ids=fields.available_template_ids,
        issues=fields.issues,
    )


def _workflow_mode(config: Any, issues: list[str]) -> str:
    raw_mode = getattr(config, "subagent_workflow_mode", DEFAULT_MODE)
    if isinstance(raw_mode, str):
        mode = raw_mode.strip().lower()
        if mode in VALID_MODES:
            return mode

    issues.append(f"invalid subagent_workflow_mode {raw_mode!r}; using default auto")
    return DEFAULT_MODE


def _select_template(request: _TemplateSelectionRequest) -> tuple[str, str]:
    explicit_template_id = request.explicit_template_id.strip()
    if explicit_template_id:
        if request.store.get(explicit_template_id) is not None:
            return explicit_template_id, f"Explicit workflow template requested: {explicit_template_id}."
        request.issues.append(f"explicit workflow template not found: {explicit_template_id}")

    if request.store.get(request.preferred_template_id) is not None:
        return request.preferred_template_id, f"Selected {request.preferred_template_id} for the classified task type."

    request.issues.append(f"preferred workflow template not available: {request.preferred_template_id}")
    if not request.available_template_ids:
        request.issues.append("no workflow templates are available")
    return "", "No workflow template could be selected."


def _workflow_task_type(value: object) -> str:
    task_type = str(value or "").strip().lower().replace("-", "_")
    return task_type if task_type in _TASK_TYPE_TEMPLATE_MAP else "simple"


def _workflow_risk_tags(value: object, *, task_type: str, explicit_template_id: str) -> list[str]:
    tags = _token_list(value)
    if tags:
        return tags
    return ["explicit_workflow"] if task_type != "simple" or str(explicit_template_id or "").strip() else ["low_scope"]


def _token_list(value: object) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        values = [str(item or "").strip().lower().replace("-", "_") for item in value]
    else:
        values = [str(value or "").strip().lower().replace("-", "_")]
    return [item for item in values if item and all(ch.isalnum() or ch == "_" for ch in item)]


def _format_store_issue(issue: Any) -> str:
    parts = [str(getattr(issue, "message", issue))]
    template_id = getattr(issue, "template_id", "")
    source_path = getattr(issue, "source_path", "")
    field_name = getattr(issue, "field", "")
    if template_id:
        parts.append(f"template={template_id}")
    if field_name:
        parts.append(f"field={field_name}")
    if source_path:
        parts.append(f"source={source_path}")
    return " | ".join(parts)
