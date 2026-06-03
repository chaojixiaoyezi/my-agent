
from __future__ import annotations

"""Create idempotent takeover runs for dead subagent tasks."""

from dataclasses import dataclass, field
from typing import Any

from ....runtime_errors import runtime_error_report
from ...models import SubAgentTask
from .refs import (
    default_takeover_plan,
    source_refs,
    structured_payload,
    takeover_agent_name,
    takeover_attributes,
    takeover_chain_depth,
    takeover_chain_limit,
    takeover_lineage_root,
    takeover_write_roots,
    unique_strings,
    unique_structured,
)


@dataclass(frozen=True)
class TakeoverRunRequest:
    source_run_id: str
    reason: str
    agent_name: str = ""
    role: str = ""
    plan: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TakeoverRunResult:
    source_run_id: str
    takeover_run_id: str
    created: bool
    applied: bool
    message: str
    takeover_refs: dict[str, str] = field(default_factory=dict)
    load_error: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "source_run_id": self.source_run_id,
            "takeover_run_id": self.takeover_run_id,
            "created": self.created,
            "applied": self.applied,
            "message": self.message,
            "takeover_refs": dict(self.takeover_refs),
        }
        if self.load_error:
            payload["load_error"] = dict(self.load_error)
        return payload


class SubAgentTakeoverRunService:
    """Create one takeover run for a source task, or return the existing one."""

    def __init__(self, manager: Any) -> None:
        self.manager = manager

    def create(self, request: TakeoverRunRequest) -> TakeoverRunResult:
        source = self.manager.load(request.source_run_id)
        existing, lookup_error = _existing_takeover(self.manager, source)
        if lookup_error:
            return _lookup_failed_result(source, lookup_error)
        if existing:
            _ensure_existing_takeover_handoff(self.manager, source, existing)
            return _existing_result(source, existing)
        chain_limit = takeover_chain_limit(self.manager)
        if chain_limit > 0 and takeover_chain_depth(source) >= chain_limit:
            return _chain_exhausted_result(self.manager, source, chain_limit)
        takeover = _create_takeover_task(self.manager, source, request)
        self.manager.record_takeover(source.id, take_over_by=takeover.id, reason=request.reason, locked_files=[])
        return TakeoverRunResult(
            source_run_id=source.id,
            takeover_run_id=takeover.id,
            created=True,
            applied=True,
            message=f"created takeover run {takeover.id} for {source.id}",
            takeover_refs=source_refs(source),
        )


def _existing_takeover(manager: Any, source: SubAgentTask) -> tuple[SubAgentTask | None, dict[str, object]]:
    takeover_id = str(source.takeover_by or "").strip()
    if not takeover_id:
        return _existing_takeover_by_source_ref(manager, source.id)
    try:
        return manager.load(takeover_id), {}
    except FileNotFoundError:
        return _existing_takeover_by_source_ref(manager, source.id)
    except Exception as exc:
        return None, _takeover_load_error(exc, "subagent_takeover_run.load_existing_takeover", run_id=takeover_id)


def _existing_takeover_by_source_ref(manager: Any, source_run_id: str) -> tuple[SubAgentTask | None, dict[str, object]]:
    try:
        tasks = manager.list_runs()
    except Exception as exc:
        return None, _takeover_load_error(exc, "subagent_takeover_run.list_existing_takeovers", run_id=source_run_id)
    source_id = str(source_run_id or "").strip()
    candidates: list[SubAgentTask] = []
    for task in tasks:
        attrs = getattr(task, "attributes", {}) or {}
        if str(attrs.get("takeover_source_run_id") or "").strip() != source_id:
            continue
        if str(getattr(task, "status", "") or "").upper() in {"ABANDONED", "TAKEN_OVER"}:
            continue
        candidates.append(task)
    candidates.sort(key=lambda item: item.created_at or item.updated_at or 0.0)
    return (candidates[0] if candidates else None), {}


def _existing_result(source: SubAgentTask, existing: SubAgentTask) -> TakeoverRunResult:
    return TakeoverRunResult(
        source_run_id=source.id,
        takeover_run_id=existing.id,
        created=False,
        applied=False,
        message=f"source {source.id} already taken over by {existing.id}",
        takeover_refs=source_refs(source),
    )


def _lookup_failed_result(source: SubAgentTask, load_error: dict[str, object]) -> TakeoverRunResult:
    return TakeoverRunResult(
        source_run_id=source.id,
        takeover_run_id="",
        created=False,
        applied=False,
        message=f"takeover lookup failed for {source.id}; existing takeover state could not be verified",
        takeover_refs=source_refs(source),
        load_error=load_error,
    )


def _takeover_load_error(exc: BaseException, context: str, *, run_id: str) -> dict[str, object]:
    return {
        "context": context,
        "run_id": run_id,
        "error": runtime_error_report(exc, context=context),
    }


def _ensure_existing_takeover_handoff(manager: Any, source: SubAgentTask, existing: SubAgentTask) -> None:
    changed = False
    changed |= _merge_context_manifest(existing, source)
    changed |= _merge_context_packs(existing, source)
    changed |= _merge_attribute_handoff(existing, source)
    merged_artifacts = unique_strings([*source.artifact_refs, *existing.artifact_refs, source.agent_run_artifacts_dir])
    if merged_artifacts != existing.artifact_refs:
        existing.artifact_refs = merged_artifacts
        changed = True
    merged_evidence = unique_strings([*source.evidence_refs, *existing.evidence_refs])
    if merged_evidence != existing.evidence_refs:
        existing.evidence_refs = merged_evidence
        changed = True
    merged_roots = unique_strings([*existing.allowed_write_roots, *takeover_write_roots(source)])
    if merged_roots != existing.allowed_write_roots:
        existing.allowed_write_roots = merged_roots
        changed = True
    if changed:
        manager.save(existing)


def _merge_context_manifest(target: SubAgentTask, source: SubAgentTask) -> bool:
    changed = False
    for field_name in ("task_pack_refs", "required_read_paths", "hint_read_paths", "omitted_context"):
        source_values = list(getattr(source.context_manifest, field_name, []) or [])
        target_values = list(getattr(target.context_manifest, field_name, []) or [])
        merged = unique_strings([*source_values, *target_values])
        if merged != target_values:
            setattr(target.context_manifest, field_name, merged)
            changed = True
    for field_name in ("role_pack", "quality_contract_ref"):
        target_value = str(getattr(target.context_manifest, field_name, "") or "").strip()
        source_value = str(getattr(source.context_manifest, field_name, "") or "").strip()
        if not target_value and source_value:
            setattr(target.context_manifest, field_name, source_value)
            changed = True
    if not int(getattr(target.context_manifest, "token_budget", 0) or 0):
        source_budget = int(getattr(source.context_manifest, "token_budget", 0) or 0)
        if source_budget:
            target.context_manifest.token_budget = source_budget
            changed = True
    return changed


def _merge_context_packs(target: SubAgentTask, source: SubAgentTask) -> bool:
    merged = unique_structured([*source.context_packs, *target.context_packs])
    if merged == target.context_packs:
        return False
    target.context_packs = merged
    return True


def _merge_attribute_handoff(target: SubAgentTask, source: SubAgentTask) -> bool:
    source_attrs = takeover_attributes(source)
    target_attrs = takeover_attributes(target)
    merged = dict(source_attrs)
    merged.update(target_attrs)
    if merged == target.attributes:
        return False
    target.attributes = merged
    return True


def _chain_exhausted_result(manager: Any, source: SubAgentTask, chain_limit: int) -> TakeoverRunResult:
    source.status = "BLOCKED"
    source.failure_type = "takeover_chain_exhausted"
    source.blockers = unique_strings(
        [
            *source.blockers,
            f"takeover chain reached max depth {chain_limit}; escalate or adjust timeout/scope before retry",
        ]
    )
    source.current_step = "takeover chain exhausted; waiting for parent decision"
    source.result = source.result or "连续 takeover 仍无进展，已停止继续创建接管 run。"
    manager.save(source)
    return TakeoverRunResult(
        source_run_id=source.id,
        takeover_run_id="",
        created=False,
        applied=False,
        message=f"takeover chain exhausted for {source.id}; max_depth={chain_limit}",
        takeover_refs=source_refs(source),
    )


def _create_takeover_task(manager: Any, source: SubAgentTask, request: TakeoverRunRequest) -> SubAgentTask:
    takeover = manager.create_run(
        goal=f"接管 run {source.id}: {source.goal}",
        thought="旧 runner 已超时或断通道；从 task-local refs 接续，不重新理解任务。",
        plan=request.plan or default_takeover_plan(),
        agent_name=request.agent_name or takeover_agent_name(source),
        role=request.role or source.role or "worker",
        parent_id=source.parent_id,
        root_id=source.root_id or source.id,
        depth=source.depth,
        allowed_skills=list(source.allowed_skills),
        allowed_tools=list(source.allowed_tools),
        acceptance_checks=list(source.acceptance_checks),
        quality_contract=structured_payload(source.quality_contract),
        context_manifest=structured_payload(source.context_manifest),
        context_packs=structured_payload(source.context_packs),
        extra_write_roots=takeover_write_roots(source),
        workflow_mode="off",
        attributes=takeover_attributes(source),
    )
    takeover.attributes["takeover_source_run_id"] = source.id
    takeover.attributes["takeover_source_refs"] = source_refs(source)
    takeover.attributes["takeover_lineage_root_run_id"] = takeover_lineage_root(source)
    takeover.attributes["takeover_chain_depth"] = takeover_chain_depth(source) + 1
    takeover.current_step = "读取 takeover_source_refs.latest_continue_packet 或 checkpoint 后接续原任务。"
    takeover.latest_summary = source.latest_summary
    takeover.artifact_refs = unique_strings([*source.artifact_refs, source.agent_run_artifacts_dir])
    takeover.evidence_refs = list(source.evidence_refs)
    manager.save(takeover)
    return takeover

__all__ = [
    "SubAgentTakeoverRunService",
    "TakeoverRunRequest",
    "TakeoverRunResult",
]
