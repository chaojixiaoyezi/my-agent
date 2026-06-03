
from __future__ import annotations

from dataclasses import dataclass

from ..models import SubAgentTask


@dataclass(frozen=True)
class OutputRefRebinding:
    field: str
    from_ref: str
    to_ref: str

    def to_dict(self) -> dict[str, str]:
        return {"field": self.field, "from": self.from_ref, "to": self.to_ref}


def rebind_task_output_refs_to_run(task: SubAgentTask) -> list[OutputRefRebinding]:
    rewrites: list[OutputRefRebinding] = []
    task.attributes = _rewrite_attribute_output_refs(task.attributes, task.id, rewrites)
    if rewrites:
        _store_rebindings(task, rewrites)
    return rewrites


def _rewrite_attribute_output_refs(
    attributes: dict[str, object],
    run_id: str,
    rewrites: list[OutputRefRebinding],
) -> dict[str, object]:
    attrs = dict(attributes or {})
    for field in ("output_refs", "output_files", "artifact_refs"):
        if field in attrs:
            attrs[field] = _rewrite_attribute_value(field, attrs.get(field), run_id, rewrites)
    return attrs


def _rewrite_attribute_value(
    field: str,
    value: object,
    run_id: str,
    rewrites: list[OutputRefRebinding],
) -> object:
    if isinstance(value, list):
        return [_rewrite_attribute_value(field, item, run_id, rewrites) for item in value]
    if not isinstance(value, str):
        return value
    rebound = _rebound_subagent_ref(value, run_id)
    if rebound and rebound != value:
        rewrites.append(OutputRefRebinding(field, value, rebound))
        return rebound
    return value


def _rebound_subagent_ref(ref: str, run_id: str) -> str:
    parts = str(ref or "").split("/")
    for index, part in enumerate(parts[:-1]):
        if part != "subagents":
            continue
        next_index = index + 1
        if next_index >= len(parts) or not parts[next_index].startswith("subagent-"):
            continue
        if parts[next_index] == run_id:
            return ""
        parts[next_index] = run_id
        return "/".join(parts)
    return ""


def _store_rebindings(task: SubAgentTask, rewrites: list[OutputRefRebinding]) -> None:
    attrs = dict(getattr(task, "attributes", {}) or {})
    existing = attrs.get("output_ref_rebindings")
    records = list(existing) if isinstance(existing, list) else []
    records.extend(item.to_dict() for item in rewrites)
    attrs["output_ref_rebindings"] = records
    task.attributes = attrs
