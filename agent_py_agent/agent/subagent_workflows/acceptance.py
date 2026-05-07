from __future__ import annotations

"""LLM: parent-side acceptance planning keeps final verification outside worker self-report."""

from dataclasses import dataclass, field, is_dataclass
from typing import Any

from .models import WorkflowTemplate


@dataclass(frozen=True)
class ParentAcceptanceItem:
    """One check the parent session must perform before accepting delivery."""

    id: str
    text: str
    source: str
    category: str = "acceptance"
    required: bool = True


@dataclass(frozen=True)
class ParentAcceptancePlan:
    """Structured final-gate checklist assembled for a workflow template."""

    template_id: str
    goal: str
    items: list[ParentAcceptanceItem] = field(default_factory=list)
    final_gate: str = "parent_final_gate"

    @property
    def checklist(self) -> list[str]:
        """Return human-readable check text for renderers that need strings."""

        return [item.text for item in self.items]


@dataclass(frozen=True)
class _AcceptanceItemSource:
    """Bundle shared labels for parent acceptance item expansion."""

    source: str
    category: str
    id_prefix: str
    transform: Any | None = None


_DEFAULT_ANTI_ACCEPTANCE_ITEMS = (
    (
        "anti_worker_pass",
        "Do not accept worker self-reported PASS as proof; parent must verify independently.",
    ),
    (
        "anti_real_artifacts",
        "Inspect the real changed artifacts, files, outputs, or behavior before acceptance.",
    ),
    (
        "anti_evidence_paths",
        "Check that cited evidence paths, commands, logs, or URLs exist and support the claim.",
    ),
    (
        "anti_residual_risk",
        "Review residual risks, deferred work, and allowed degradation before deciding.",
    ),
    (
        "anti_parent_final_gate",
        "Keep final acceptance in the parent session final gate; workers cannot self-accept.",
    ),
)


def plan_parent_acceptance(
    template: WorkflowTemplate,
    *,
    goal: str,
    quality_contract: object | None = None,
) -> ParentAcceptancePlan:
    """Build the parent final-gate checklist for a selected workflow template."""

    items: list[ParentAcceptanceItem] = []
    contract = _contract_mapping(quality_contract)

    _extend_items(
        items,
        template.parent_acceptance,
        _AcceptanceItemSource("template.parent_acceptance", "template", "template"),
    )
    _extend_contract_items(items, contract)
    _add_default_anti_acceptance_items(items)

    if _requires_reviewer_result(template, contract):
        items.append(
            ParentAcceptanceItem(
                id="critic_or_reviewer_result",
                text="Require an existing critic or reviewer result and confirm repair responded to accepted findings.",
                source="workflow.critic_gate",
                category="critic_gate",
            )
        )

    return ParentAcceptancePlan(template_id=template.id, goal=goal, items=_dedupe_items(items))


def _add_default_anti_acceptance_items(items: list[ParentAcceptanceItem]) -> None:
    """Append the default anti-acceptance items that prevent self-acceptance."""
    for item_id, text in _DEFAULT_ANTI_ACCEPTANCE_ITEMS:
        items.append(
            ParentAcceptanceItem(
                id=item_id,
                text=text,
                source="default.anti_acceptance",
                category="anti_acceptance",
            )
        )


def _extend_contract_items(
    items: list[ParentAcceptanceItem],
    contract: dict[str, object],
) -> None:
    """Extend items from quality contract fields."""
    field_configs = (
        ("must_check", "quality_contract.must_check", "quality_contract", "must_check"),
        ("sampling_plan", "quality_contract.sampling_plan", "quality_contract", "sampling_plan"),
        ("evidence_required", "quality_contract.evidence_required", "evidence", "evidence_required"),
        (
            "forbidden_delivery",
            "quality_contract.forbidden_delivery",
            "forbidden_delivery",
            "forbidden_delivery",
        ),
    )
    for key, source, category, id_prefix in field_configs:
        transform = (
            (lambda text: f"Reject delivery if it includes forbidden condition: {text}")
            if key == "forbidden_delivery"
            else None
        )
        _extend_items(
            items,
            _as_list(contract.get(key)),
            _AcceptanceItemSource(source, category, id_prefix, transform),
        )


def _extend_items(
    items: list[ParentAcceptanceItem],
    values: list[str],
    item_source: _AcceptanceItemSource,
) -> None:
    for index, value in enumerate(values, start=1):
        text = item_source.transform(value) if item_source.transform else value
        items.append(
            ParentAcceptanceItem(
                id=f"{item_source.id_prefix}_{index}",
                text=text,
                source=item_source.source,
                category=item_source.category,
            )
        )


def _contract_mapping(value: object | None) -> dict[str, object]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if is_dataclass(value):
        return {
            name: getattr(value, name)
            for name in (
                "must_check",
                "sampling_plan",
                "evidence_required",
                "forbidden_delivery",
                "quality_bar",
            )
            if hasattr(value, name)
        }
    return {
        name: getattr(value, name)
        for name in (
            "must_check",
            "sampling_plan",
            "evidence_required",
            "forbidden_delivery",
            "quality_bar",
        )
        if hasattr(value, name)
    }


def _as_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item)]
    return [str(value)] if str(value) else []


def _requires_reviewer_result(template: WorkflowTemplate, contract: dict[str, object]) -> bool:
    if template.id == "producer_critic_repair":
        return True
    if any(phase.kind in {"review", "critic", "reviewer"} for phase in template.phases):
        return True
    quality_bar = str(contract.get("quality_bar") or "").lower()
    return "high quality" in quality_bar or "critic" in quality_bar or "reviewer" in quality_bar


def _dedupe_items(items: list[ParentAcceptanceItem]) -> list[ParentAcceptanceItem]:
    seen: set[tuple[str, str]] = set()
    deduped: list[ParentAcceptanceItem] = []
    for item in items:
        key = (item.source or "", (item.text or "").strip().lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped
