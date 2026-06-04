
"""Persistence service for SubAgentManager task records.

Saves are split into two phases. The canonical task-local state is written first
and remains the source of truth; owner indexes, tree projections, status reports
and LocalStore mirrors are derived projections. Projection failures are recorded
as warnings instead of corrupting or blocking the canonical save.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ....common.json_io import read_json_object_report, write_json_file_atomic
from ....runtime_errors import runtime_error_report
from ....user_space.task_compact_rollup import sync_task_compact_rollup
from ...models import (
    CapabilityGap,
    CapabilityGrant,
    CapabilityRequest,
    ChannelProbeCheck,
    SubAgentTask,
    TakeoverRecord,
    VerificationEvidence,
)
from ...utils import _apply_missing_paths
from ..agent_run_state import (
    build_agent_run_state,
    build_agent_state_locator,
    build_owner_agent_projection,
    read_agent_state_payload,
    write_agent_run_state,
)
from ..checkpoint_artifacts import build_checkpoint_artifact_payloads
from ..failure_handoff import refresh_failure_handoff
from ..task_workspace_adapter import sync_task_workspace_fields
from .failure_handoff import normalize_failure_handoff, write_failure_handoff
from .identity import normalize_runtime_identity
from .inheritance import normalize_inheritance_manifest, write_inheritance_manifest
from .model_normalizers import (
    _field_names,
    _normalize_context_manifest,
    _normalize_context_packs,
    _normalize_evidence_packet,
    _normalize_finding,
    _normalize_nested_model,
    _normalize_quality_contract,
    _normalize_status_report,
)
from .output_load_errors import append_agent_run_checkpoint_load_error, append_checkpoint_load_error
from .projections import sync_derived_projections
from .recovery_outputs import write_recovery_output_files
from .security import normalize_security_signal
from .status_report import build_status_report


@dataclass(frozen=True)
class SubAgentListRunsReport:
    """Result of scanning persisted subagent runs.

    The run list stays best-effort, but load failures are kept as structured
    facts so tree/board callers do not confuse broken bookkeeping with "no
    subagent work exists".
    """

    runs: list[SubAgentTask] = field(default_factory=list)
    load_errors: list[dict[str, Any]] = field(default_factory=list)


class SubAgentPersistenceService:
    """Read and write SubAgentTask records for SubAgentManager."""

    def __init__(self, manager: Any):
        self.manager = manager

    @property
    def workspace(self) -> Path:
        return self.manager.workspace

    def load(self, run_id: str) -> SubAgentTask:
        """Load one subagent task from disk."""

        path = self.workspace / run_id / "task.json"
        if not path.exists():
            raise FileNotFoundError(f"子代理记录不存在: {run_id}")
        data = _read_state_payload(path)
        return self.task_from_payload(data)

    def task_from_payload(self, data: dict[str, Any]) -> SubAgentTask:
        """Build a normalized task from a canonical state payload."""

        data = {key: value for key, value in data.items() if key in _field_names(SubAgentTask)}
        data["capability_requests"] = [
            _normalize_nested_model(CapabilityRequest, item)
            for item in data.get("capability_requests", [])
            if isinstance(item, dict)
        ]
        data["capability_grants"] = [
            _normalize_nested_model(CapabilityGrant, item)
            for item in data.get("capability_grants", [])
            if isinstance(item, dict)
        ]
        data["capability_gaps"] = [
            _normalize_nested_model(CapabilityGap, item)
            for item in data.get("capability_gaps", [])
            if isinstance(item, dict)
        ]
        data["evidence"] = [VerificationEvidence(**item) for item in data.get("evidence", []) if isinstance(item, dict)]
        data["evidence_packets"] = [
            _normalize_evidence_packet(item) for item in data.get("evidence_packets", []) if isinstance(item, dict)
        ]
        data["findings"] = [
            _normalize_finding(item) for item in data.get("findings", []) if isinstance(item, dict)
        ]
        data["takeover_records"] = [
            TakeoverRecord(**item) for item in data.get("takeover_records", []) if isinstance(item, dict)
        ]
        data["channel_checks"] = [
            ChannelProbeCheck(**item) for item in data.get("channel_checks", []) if isinstance(item, dict)
        ]
        data["quality_contract"] = _normalize_quality_contract(data.get("quality_contract"))
        data["context_manifest"] = _normalize_context_manifest(data.get("context_manifest"))
        data["context_packs"] = _normalize_context_packs(data.get("context_packs"))
        data["latest_status_report"] = _normalize_status_report(data.get("latest_status_report"))
        data["inheritance_manifest"] = normalize_inheritance_manifest(data.get("inheritance_manifest"))
        data["failure_handoff"] = normalize_failure_handoff(data.get("failure_handoff"))
        data["security_signals"] = [
            normalize_security_signal(item) for item in data.get("security_signals", []) if isinstance(item, dict)
        ]
        data["runtime_identity"] = normalize_runtime_identity(data.get("runtime_identity"))
        return SubAgentTask(**data)

    def list_runs(self) -> list[SubAgentTask]:
        """Scan the workspace for subagent task records."""

        return self.list_runs_report().runs

    def list_runs_report(self) -> SubAgentListRunsReport:
        """Scan task records and preserve per-record load failures."""

        runs: list[SubAgentTask] = []
        load_errors: list[dict[str, Any]] = []
        for task_file in sorted(self.workspace.glob("*/task.json")):
            try:
                runs.append(self.load(task_file.parent.name))
            except Exception as exc:
                report = runtime_error_report(exc, context="subagents.load")
                report["run_id"] = task_file.parent.name
                report["path"] = str(task_file)
                load_errors.append(report)
        runs.sort(key=lambda item: item.updated_at or item.created_at, reverse=True)
        return SubAgentListRunsReport(runs=runs, load_errors=load_errors)

    def save(self, task: SubAgentTask, *, preserve_child_links: bool = True) -> None:
        """Persist a task as JSON plus human-readable Markdown."""

        _apply_missing_paths(task, self.manager._build_work_order_paths(task.id, task.task_dir or None))
        if preserve_child_links:
            _merge_existing_child_links(self, task)
            _merge_existing_takeover_state(self, task)
        task_dir, owner_projection = _prepare_and_write_state(self, task)
        sync_derived_projections(self.manager, task, task_dir, owner_projection)


def _prepare_and_write_state(
    service: SubAgentPersistenceService,
    task: SubAgentTask,
) -> tuple[Path, dict[str, Any]]:
    task_dir = Path(task.task_dir)
    task_dir.mkdir(parents=True, exist_ok=True)
    service.manager._ensure_work_order_files(task)
    task.updated_at = task.updated_at or time.time()
    if task.checkpoint_json:
        task.checkpoint_ref = task.checkpoint_json
    _refresh_system_tree_snapshot(task)
    task.latest_status_report = build_status_report(task)
    refresh_failure_handoff(task)
    output_report = (
        read_json_object_report(
            Path(task.output_json),
            parse_nested_string=True,
            context="subagent.persistence.output_json",
        )
        if task.output_json
        else None
    )
    output_payload = output_report.payload if output_report else {}
    checkpoint_artifacts = build_checkpoint_artifact_payloads(task, output_payload)
    if output_report and output_report.load_error:
        append_checkpoint_load_error(checkpoint_artifacts, output_report.load_error)
    sync_task_workspace_fields(service.workspace, task)
    _sync_task_rollup_if_possible(task)
    if output_report and output_report.load_error:
        append_agent_run_checkpoint_load_error(task, output_report.load_error)
    _set_canonical_state_ref(task)
    write_inheritance_manifest(task)
    write_failure_handoff(task)
    write_recovery_output_files(task, checkpoint_artifacts)
    state = build_agent_run_state(task)
    write_agent_run_state(state)
    locator_payload = build_agent_state_locator(task, state)
    write_json_file_atomic(task_dir / "task.json", locator_payload)
    write_json_file_atomic(task_dir / "run.json", locator_payload)
    return task_dir, build_owner_agent_projection(task, state)


def _set_canonical_state_ref(task: SubAgentTask) -> None:
    task.attributes = dict(getattr(task, "attributes", {}) or {})
    if task.agent_run_workspace_dir:
        task.attributes["canonical_state_ref"] = str(
            Path(task.agent_run_workspace_dir) / "canonical_state.json"
        )


def _read_state_payload(path: Path) -> dict[str, Any]:
    return read_agent_state_payload(path)


def _sync_task_rollup_if_possible(task: SubAgentTask) -> None:
    task_workspace = str(getattr(task, "task_workspace_dir", "") or "").strip()
    if not task_workspace:
        return
    result = sync_task_compact_rollup(task_workspace)
    task.attributes = dict(getattr(task, "attributes", {}) or {})
    task.attributes["task_compact_rollup"] = {
        "rollup_json": str(result.rollup_json),
        "rollup_markdown": str(result.rollup_markdown),
        "compact_package_dir": str(result.compact_package_dir),
        "child_count": result.child_count,
    }


def _merge_existing_child_links(service: SubAgentPersistenceService, task: SubAgentTask) -> None:
    try:
        existing = service.load(task.id)
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return
    task.child_ids = _unique_strings([*existing.child_ids, *task.child_ids])


def _merge_existing_takeover_state(service: SubAgentPersistenceService, task: SubAgentTask) -> None:
    try:
        existing = service.load(task.id)
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return
    existing_taken = str(existing.status or "").upper() == "TAKEN_OVER" or bool(existing.takeover_by)
    incoming_taken = str(task.status or "").upper() == "TAKEN_OVER" or bool(task.takeover_by)
    if not existing_taken or incoming_taken:
        return
    task.status = existing.status
    task.takeover_by = existing.takeover_by
    task.takeover_reason = existing.takeover_reason
    task.takeover_records = list(existing.takeover_records)
    task.final_owner = existing.final_owner
    task.locked_files = _unique_strings([*existing.locked_files, *task.locked_files])


def _refresh_system_tree_snapshot(task: SubAgentTask) -> None:
    attrs = dict(getattr(task, "attributes", {}) or {})
    attrs["system_tree"] = {
        "schema_version": "subagent_system_tree.v1",
        "updated_by": "system",
        "source": "persistence.save",
        "run_id": task.id,
        "owner_id": task.owner,
        "root_id": task.root_id or task.id,
        "parent_id": task.parent_id,
        "depth": _safe_int(task.depth),
        "status": task.status,
        "verification_status": task.verification_status,
        "failure_type": task.failure_type,
        "progress": _safe_float(task.progress),
        "current_step": task.current_step,
        "latest_summary": task.latest_summary,
        "child_ids": _unique_strings(list(task.child_ids)),
        "artifact_refs": _unique_strings(list(task.artifact_refs)),
        "artifact_registry_refs": _registry_records(attrs.get("artifact_registry_refs")),
        "evidence_refs": _unique_strings(list(task.evidence_refs)),
        "blockers": _unique_strings(list(task.blockers)),
        "updated_at": _safe_float(task.updated_at or task.heartbeat_at or task.created_at),
    }
    task.attributes = attrs


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return default


def _unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in values if item))


def _registry_records(value: object, *, limit: int = 12) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        artifact_id = str(row.get("artifact_id") or "").strip()
        path = str(row.get("path") or "").strip()
        key = artifact_id or path
        if not key or key in seen:
            continue
        seen.add(key)
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows
