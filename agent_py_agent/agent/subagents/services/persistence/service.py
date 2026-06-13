
"""Persistence service for SubAgentManager task records.

Saves are split into two phases. The canonical task-local state is written first
and remains the source of truth; owner indexes, tree projections, status reports
and LocalStore mirrors are derived projections. Projection failures are recorded
as warnings instead of corrupting or blocking the canonical save.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ....common.json_io import read_json_object_report, write_json_file_atomic
from ....common.value_parsing import sequence_strings
from ....runtime_errors import runtime_error_report
from ....user_space.task_compact_rollup import sync_task_compact_rollup
from ...models import (
    SUBAGENT_FAILED_RESULT_STATUSES,
    CapabilityGap,
    CapabilityGrant,
    CapabilityRequest,
    ChannelProbeCheck,
    FailureHandoff,
    InheritanceManifest,
    RuntimeIdentity,
    SecuritySignal,
    StatusReport,
    SubAgentTask,
    TakeoverRecord,
    TaskStatus,
    VerificationEvidence,
    failure_type_from_task_status,
    task_has_failure_status,
    task_has_status,
    task_status_in,
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
from ..compact_continue_packet import SubagentContinuePacketRequest, write_subagent_continue_packet
from ..output_alignment import (
    record_locked_files_change,
    sanitize_self_locked_delivery_targets,
)
from ..takeover.readiness import write_takeover_readiness_files
from ..task_workspace_adapter import sync_task_workspace_fields
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
from .projections import sync_derived_projections

_HIGH_RISK_FAILURE_TYPES = {"tool_output_context_overflow", "context_overflow", "blackbox_output_overflow"}


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
        previous_locked = _existing_locked_files(self, task)
        if preserve_child_links:
            _merge_existing_child_links(self, task)
            _merge_existing_takeover_state(self, task)
        # P3-1 锁生命周期(R5a 实锤):save 是落盘唯一权威口——无论锁来自模型参数、
        # takeover 透传还是合并,这里统一剔除"锁住自己交付目标"的派工矛盾并记账。
        now = time.time()
        sanitize_self_locked_delivery_targets(task, now)
        record_locked_files_change(task, previous_locked, now)
        task_dir, owner_projection = _prepare_and_write_state(self, task)
        sync_derived_projections(self.manager, task, task_dir, owner_projection)


def _prepare_and_write_state(
    service: SubAgentPersistenceService,
    task: SubAgentTask,
) -> tuple[Path, dict[str, Any]]:
    sync_task_workspace_fields(service.workspace, task)
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
    locator_dir = service.workspace / task.id
    locator_dir.mkdir(parents=True, exist_ok=True)
    write_json_file_atomic(locator_dir / "task.json", locator_payload)
    write_json_file_atomic(locator_dir / "run.json", locator_payload)
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


def build_status_report(task: SubAgentTask) -> StatusReport:
    previous = task.latest_status_report if isinstance(task.latest_status_report, StatusReport) else StatusReport()
    progress = max(0.0, min(1.0, _safe_float(task.progress)))
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


def normalize_runtime_identity(value: object) -> RuntimeIdentity:
    if isinstance(value, RuntimeIdentity):
        return value
    if not isinstance(value, dict):
        return RuntimeIdentity()
    payload = {key: value[key] for key in _field_names(RuntimeIdentity) if key in value}
    return RuntimeIdentity(**payload)


def normalize_security_signal(value: object) -> SecuritySignal:
    if isinstance(value, SecuritySignal):
        return value
    if not isinstance(value, dict):
        return SecuritySignal()
    payload = {key: value[key] for key in _field_names(SecuritySignal) if key in value}
    for key in ("evidence_refs", "artifact_refs"):
        payload[key] = sequence_strings(payload.get(key))
    payload["created_at"] = _safe_float(payload.get("created_at"))
    return SecuritySignal(**payload)


def normalize_inheritance_manifest(value: object) -> InheritanceManifest:
    if isinstance(value, InheritanceManifest):
        return value
    if not isinstance(value, dict):
        return InheritanceManifest()
    payload = {key: value[key] for key in _field_names(InheritanceManifest) if key in value}
    for key in ("inherited", "overridden", "dropped", "policy"):
        payload[key] = _dict_value(payload.get(key))
    payload["created_at"] = _safe_float(payload.get("created_at"))
    return InheritanceManifest(**payload)


def write_inheritance_manifest(task: SubAgentTask) -> None:
    if not task.inheritance_manifest_json:
        return
    Path(task.inheritance_manifest_json).write_text(
        json.dumps(asdict(task.inheritance_manifest), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def normalize_failure_handoff(value: object) -> FailureHandoff:
    if isinstance(value, FailureHandoff):
        return value
    if not isinstance(value, dict):
        return FailureHandoff()
    payload = {key: value[key] for key in _field_names(FailureHandoff) if key in value}
    for key in ("artifact_refs", "evidence_refs", "avoid_next_time"):
        payload[key] = sequence_strings(payload.get(key), allow_scalar=True)
    payload["created_at"] = _safe_float(payload.get("created_at"))
    return FailureHandoff(**payload)


def refresh_failure_handoff(task: SubAgentTask) -> FailureHandoff:
    if not should_write_failure_handoff(task):
        task.failure_handoff = FailureHandoff()
        return task.failure_handoff
    task.failure_handoff = FailureHandoff(
        run_id=task.id,
        status=task.status,
        failure_type=task.failure_type or failure_type_from_task_status(task.status),
        risk_level=_failure_handoff_risk_level(task),
        warning=task.latest_summary or _default_failure_warning(task),
        last_safe_checkpoint_ref=task.checkpoint_json or task.checkpoint_ref,
        artifact_refs=list(dict.fromkeys(task.artifact_refs)),
        evidence_refs=list(dict.fromkeys(task.evidence_refs)),
        avoid_next_time=_avoid_next_time(task),
        recommended_next_action=_recommended_next_action(task),
        auto_rescue=False,
        created_at=task.updated_at or task.heartbeat_at or task.created_at or time.time(),
    )
    return task.failure_handoff


def should_write_failure_handoff(task: SubAgentTask) -> bool:
    return task_has_failure_status(task) or bool(task.failure_type)


def _failure_handoff_risk_level(task: SubAgentTask) -> str:
    if task.failure_type in _HIGH_RISK_FAILURE_TYPES:
        return "high"
    if task_status_in(task.status, SUBAGENT_FAILED_RESULT_STATUSES):
        return "high"
    if task_has_status(task, TaskStatus.BLOCKED):
        return "medium"
    return "low"


def _default_failure_warning(task: SubAgentTask) -> str:
    if task.failure_type in _HIGH_RISK_FAILURE_TYPES:
        return "黑盒或工具输出存在撑爆上下文风险，已停止继续展开。"
    return "子代理未能正常完成，后续接管前请先读取 checkpoint 和 evidence refs。"


def _avoid_next_time(task: SubAgentTask) -> list[str]:
    avoid = ["不要机械重试同一工具调用；先读取 checkpoint、artifact manifest 和失败交接记录。"]
    if task.failure_type in _HIGH_RISK_FAILURE_TYPES:
        avoid.append("不要把黑盒大输出直接塞回 prompt；先外置文件，再读取摘要或切片。")
    avoid.extend(f"先处理 blocker: {item}" for item in task.blockers)
    return list(dict.fromkeys(avoid))


def _recommended_next_action(task: SubAgentTask) -> str:
    if task.failure_type in _HIGH_RISK_FAILURE_TYPES:
        return "先外置黑盒输出，再让接管代理读取摘要和 checkpoint。"
    if task.blockers:
        return f"先解决阻塞项：{task.blockers[0]}"
    return "读取 checkpoint、failure_handoff 和 evidence refs 后再决定是否接管。"


def write_failure_handoff(task: SubAgentTask) -> None:
    if not task.failure_handoff_json:
        return
    path = Path(task.failure_handoff_json)
    if not task.failure_handoff.run_id:
        path.unlink(missing_ok=True)
        return
    path.write_text(
        json.dumps(asdict(task.failure_handoff), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def append_checkpoint_load_error(
    checkpoint_artifacts: dict[str, object],
    error: dict[str, object],
) -> None:
    checkpoint = checkpoint_artifacts.get("checkpoint_json")
    if isinstance(checkpoint, dict):
        _append_load_error(checkpoint, error)


def append_agent_run_checkpoint_load_error(task: SubAgentTask, error: dict[str, object]) -> None:
    path_text = str(getattr(task, "agent_run_checkpoint_json", "") or "").strip()
    if not path_text:
        return
    path = Path(path_text)
    report = read_json_object_report(
        path,
        context="subagent.persistence.agent_run_checkpoint",
    )
    checkpoint = dict(report.payload)
    if report.load_error:
        _append_load_error(checkpoint, report.load_error)
    _append_load_error(checkpoint, error)
    path.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_recovery_output_files(task: SubAgentTask, checkpoint_artifacts: dict[str, object]) -> None:
    for field_name, artifact_payload in checkpoint_artifacts.items():
        _write_checkpoint_artifact(getattr(task, field_name, ""), artifact_payload)
    write_takeover_readiness_files(task)
    output_payload, output_load_error = _output_payload_report(task)
    write_subagent_continue_packet(
        SubagentContinuePacketRequest(
            task,
            output_payload,
            load_errors=tuple(error for error in (output_load_error,) if error),
        )
    )


def _summary_delta(task: SubAgentTask) -> dict[str, list[str]]:
    return {
        "facts_added": [task.latest_summary] if task.latest_summary else [],
        "facts_invalidated": [],
        "decisions_changed": [],
        "open_questions": list(task.blockers),
    }


def _dict_value(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _append_load_error(checkpoint: dict[str, Any], error: dict[str, object]) -> None:
    load_errors = checkpoint.get("load_errors")
    items = list(load_errors) if isinstance(load_errors, list) else []
    items.append(error)
    checkpoint["load_errors"] = items


def _write_checkpoint_artifact(path_text: str, artifact_payload: object) -> None:
    if not path_text:
        return
    path = Path(path_text)
    if isinstance(artifact_payload, str):
        path.write_text(artifact_payload, encoding="utf-8")
        return
    path.write_text(json.dumps(artifact_payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _output_payload_report(task: SubAgentTask) -> tuple[dict[str, object], dict[str, object] | None]:
    if not task.output_json:
        return {}, None
    path = Path(task.output_json)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return {}, _output_load_error(path, exc)
    if not isinstance(payload, dict):
        return {}, _output_load_error(path, ValueError(f"output_json is {type(payload).__name__}, expected object"))
    return payload, None


def _output_load_error(path: Path, exc: BaseException) -> dict[str, object]:
    report = runtime_error_report(exc, context="subagent.continue_packet.output_json")
    report["path"] = str(path)
    return report


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


# 函数用途: 读上一份落盘状态的 locked_files(锁变更账本的对比基准;首存返回 None)。
def _existing_locked_files(service: SubAgentPersistenceService, task: SubAgentTask) -> list[str] | None:
    try:
        existing = service.load(task.id)
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return None
    return list(existing.locked_files or [])


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
