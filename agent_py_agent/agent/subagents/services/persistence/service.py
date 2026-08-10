
"""Persistence service for SubAgentManager task records.

Saves are split into two phases. The canonical task-local state is written first
and remains the source of truth; owner indexes, tree projections, status reports
and LocalStore mirrors are derived projections. Projection failures are recorded
as warnings instead of corrupting or blocking the canonical save.
"""

from __future__ import annotations

import copy
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ....common.json_io import locked_json_path, read_json_object_report, write_json_file_atomic
from ....common.opaque_id import validate_opaque_id
from ....runtime_db.operations import directory_id_for_opaque
from ....common.value_parsing import sequence_strings
from ....runtime_errors import runtime_error_report
from ...models import (
    SUBAGENT_FAILED_RESULT_STATUSES,
    CapabilityGap,
    CapabilityGrant,
    CapabilityRequest,
    ChannelProbeCheck,
    FailureHandoff,
    FailureType,
    InheritanceManifest,
    RuntimeIdentity,
    SecuritySignal,
    StatusReport,
    SubAgentTask,
    TakeoverRecord,
    TaskStatus,
    VerificationEvidence,
    VerificationStatus,
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
    canonical_state_path_from_payload,
    read_agent_state_payload,
    write_agent_run_state,
)
from ..checkpoint_artifacts import build_checkpoint_artifact_payloads
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
        # mtime 缓存:子代理 task.json 未变时复用已解析对象,避免每轮 dispatch 全量重读+重解析。
        # 大量历史 DONE 子代理累积时(实测 600 个全量 745ms)命中后只 stat。返回 deepcopy 副本,
        # 调用方改了也不污染缓存(无需逐一审 33 处调用方是否只读)。
        self._run_cache: dict[str, tuple[float, SubAgentTask]] = {}

    @property
    def workspace(self) -> Path:
        return self.manager.workspace

    def load(self, run_id: str) -> SubAgentTask:
        """Load one subagent task from disk."""

        # 3.txt B.2/B.3：run_id 是 opaque identifier，拼进路径前必须先过拒绝式
        # 校验（拦 ../、绝对路径、控制字符等注入），再由框架按已验证 ID 计算
        # 路径。load 是 cancel/dispatch/inspect/resume/takeover 等入口的共同
        # 读路径，此门挡住全部 ID 路径注入。
        run_id = validate_opaque_id(run_id, kind="run_id")
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
        """Scan task records and preserve per-record load failures.

        mtime 缓存:task.json 未变(mtime 相同)就复用上次解析的对象、只 stat 不重读重解析。
        大量历史 DONE 子代理累积时(实测 600 个全量 read+parse 745ms),命中后降到 stat(17ms)+
        deepcopy(82ms)。返回 deepcopy 副本,保证缓存对象不被任何调用方修改污染。
        """

        runs: list[SubAgentTask] = []
        load_errors: list[dict[str, Any]] = []
        seen: set[str] = set()
        for task_file in sorted(self.workspace.glob("*/task.json")):
            run_id = task_file.parent.name
            seen.add(run_id)
            try:
                runs.append(self._cached_run_copy(run_id, task_file))
            except Exception as exc:
                report = runtime_error_report(exc, context="subagents.load")
                report["run_id"] = run_id
                report["path"] = str(task_file)
                load_errors.append(report)
        self._evict_run_cache(seen)
        runs.sort(key=lambda item: item.updated_at or item.created_at, reverse=True)
        return SubAgentListRunsReport(runs=runs, load_errors=load_errors)

    def _cached_run_copy(self, run_id: str, task_file: Path) -> SubAgentTask:
        """命中(mtime 未变)→复用缓存解析结果;未命中→读盘并缓存。一律返回 deepcopy 副本。"""
        mtime = task_file.stat().st_mtime
        cached = self._run_cache.get(run_id)
        if cached is None or cached[0] != mtime:
            self._run_cache[run_id] = (mtime, self.load(run_id))
        return copy.deepcopy(self._run_cache[run_id][1])

    def _evict_run_cache(self, seen: set[str]) -> None:
        """清理已从磁盘消失的 run 的缓存项,防内存随历史增长泄漏。"""
        if len(self._run_cache) > len(seen):
            for stale in [rid for rid in self._run_cache if rid not in seen]:
                self._run_cache.pop(stale, None)

    def save(self, task: SubAgentTask, *, preserve_child_links: bool = True) -> None:
        """Persist a task as JSON plus human-readable Markdown."""

        _apply_missing_paths(task, self.manager._build_work_order_paths(task.id, task.task_dir or None))
        # canonical state 有两类写者:业务状态保存与 runner-session 窄心跳。
        # 用独立 guard 串行化；不能直接锁 canonical_state.json，因为原子写入
        # 自己还会取该路径的非可重入锁。
        with locked_json_path(_canonical_state_guard_path(self.workspace, task)):
            previous_locked = _existing_locked_files(self, task)
            if preserve_child_links:
                _merge_existing_child_links(self, task)
                _merge_existing_takeover_state(self, task)
            _project_closed_audit_source_state(task)
            # P3-1 锁生命周期(R5a 实锤):save 是落盘唯一权威口——无论锁来自模型参数、
            # takeover 透传还是合并,这里统一剔除"锁住自己交付目标"的派工矛盾并记账。
            now = time.time()
            sanitize_self_locked_delivery_targets(task, now)
            record_locked_files_change(task, previous_locked, now)
            task_dir, owner_projection = _prepare_and_write_state(self, task)
            sync_derived_projections(self.manager, task, task_dir, owner_projection)

    def save_runner_session(
        self,
        run_id: str,
        session: dict[str, object],
        *,
        now: float,
    ) -> None:
        """Persist runner liveness without rebuilding task projections."""

        run_id = validate_opaque_id(run_id, kind="run_id")
        locator_path = self.workspace / run_id / "task.json"
        if not locator_path.exists():
            raise FileNotFoundError(f"子代理记录不存在: {run_id}")
        initial_task = self.task_from_payload(_read_state_payload(locator_path))
        with locked_json_path(_canonical_state_guard_path(self.workspace, initial_task)):
            payload = _read_state_payload(locator_path)
            _merge_runner_session_payload(payload, session, now=now)
            canonical_ref = _canonical_state_ref_from_payload(payload)
            if not canonical_ref:
                raise FileNotFoundError(f"canonical subagent state missing for runner session: {run_id}")
            write_json_file_atomic(Path(canonical_ref), payload)
            # list_runs 的缓存签名来自 locator mtime；只刷新这个轻量 locator，
            # 让列表入口及时看到新 heartbeat，不重建其余派生投影。
            locator = read_json_object_report(locator_path).payload
            if locator:
                locator["updated_at"] = now
                write_json_file_atomic(locator_path, locator)


def _canonical_state_guard_path(workspace: Path, task: SubAgentTask) -> Path:
    # Lock metadata belongs to the system locator plane, not the model-visible
    # task workspace where artifact scans or the runner could mistake it for work.
    # task.id 是 run_id，拼进路径前必须过拒绝式校验（fail-closed：数据损坏
    # 时拒绝写盘而不是把锁文件写到任意目录）。
    run_id = str(getattr(task, "id", "") or "").strip()
    if run_id:
        run_id = validate_opaque_id(run_id, kind="run_id")
        return workspace / run_id / ".canonical_state.guard"
    task_dir = str(getattr(task, "task_dir", "") or "").strip()
    return workspace / (Path(task_dir).name if task_dir else "unknown-run") / ".canonical_state.guard"


def _canonical_state_ref_from_payload(payload: dict[str, Any]) -> str:
    # 3.txt B.6：canonical_ref 由框架重算（agent_run_workspace_dir + 域内检查，
    # 锚 = 同 payload 的 task_workspace_dir），不信任 payload 自报的
    # canonical_state_ref 字符串。
    canonical = canonical_state_path_from_payload(payload)
    return str(canonical) if canonical else ""


def _merge_runner_session_payload(
    payload: dict[str, Any],
    session: dict[str, object],
    *,
    now: float,
) -> None:
    attrs_value = payload.get("attributes")
    attrs = dict(attrs_value) if isinstance(attrs_value, dict) else {}
    history_value = attrs.get("runner_session_history")
    history = list(history_value) if isinstance(history_value, list) else []
    previous = attrs.get("runner_session")
    if (
        isinstance(previous, dict)
        and previous.get("session_id") != session.get("session_id")
        and not any(
            isinstance(item, dict) and item.get("session_id") == previous.get("session_id")
            for item in history
        )
    ):
        history.append(previous)
    attrs["runner_session"] = dict(session)
    attrs["runner_session_history"] = history[-10:]
    payload["attributes"] = attrs
    payload["heartbeat_at"] = now
    payload["updated_at"] = now


def _directory_id_for_task(service: SubAgentPersistenceService, task: SubAgentTask) -> str:
    """G1（3.txt B.3）：task.id（opaque）→ 框架目录 ID。

    有权威库（manager.runtime_db）→ 走 id_path_mapping 映射（查表→无则
    登记，首次登记即固定、重复幂等）；无库（纯投影环境）→ 退化本地校验，
    结果与 DB 登记值一致（directory_id 即 validate 后的安全路径段）。
    """
    db = getattr(getattr(service, "manager", None), "runtime_db", None)
    if db is not None:
        return db.directory_id_for(task.id, kind="run_id")
    return directory_id_for_opaque(task.id, kind="run_id")


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
    if output_report and output_report.load_error:
        append_agent_run_checkpoint_load_error(task, output_report.load_error)
    _set_canonical_state_ref(task)
    write_inheritance_manifest(task)
    write_failure_handoff(task)
    write_checkpoint_output_files(task, checkpoint_artifacts)
    state = build_agent_run_state(task)
    write_agent_run_state(state)
    locator_payload = build_agent_state_locator(task, state)
    locator_dir = service.workspace / _directory_id_for_task(service, task)
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


def _read_state_payload(path: Path, workspace: Path | None = None) -> dict[str, Any]:
    # workspace 提供时 canonical ref 跳转强制域内（本 service 读写路径）；缺省
    # 宽松模式留给只读投影调用方。
    return read_agent_state_payload(path, workspace=workspace)


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


def write_checkpoint_output_files(task: SubAgentTask, checkpoint_artifacts: dict[str, object]) -> None:
    for field_name, artifact_payload in checkpoint_artifacts.items():
        _write_checkpoint_artifact(getattr(task, field_name, ""), artifact_payload)
    write_takeover_readiness_files(task)


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


# LLM: canonical task persistence is the last shared write boundary for every
# runner, cancellation path and narrow lifecycle repair.  A named Audit watch
# already marked closed is a durable control fact; no stale runner snapshot may
# persist RUNNING/PENDING/BLOCKED over it.
# 函数用途：统一保存子代理状态前读取来源 watch 的结构化生命周期；已关闭就单调
# 投影为 CANCELLED，并废弃仍挂在旧快照上的 attempt。
def _project_closed_audit_source_state(task: SubAgentTask) -> None:
    attrs = getattr(task, "attributes", {}) or {}
    from ....common.audit_activation import (
        structured_audit_source_worker_attributes,
    )

    if not structured_audit_source_worker_attributes(attrs):
        return
    try:
        from ....ingestion.source_worker import source_worker_lifecycle_state

        closed = source_worker_lifecycle_state(task) == "closed"
    except Exception:
        closed = False
    if not closed:
        return
    attempt_id = str(getattr(task, "runner_active_attempt_id", "") or "").strip()
    if attempt_id and attempt_id not in task.runner_abandoned_attempt_ids:
        task.runner_abandoned_attempt_ids.append(attempt_id)
    task.runner_active_attempt_id = ""
    task.status = TaskStatus.CANCELLED.value
    task.verification_status = VerificationStatus.UNVERIFIED.value
    task.failure_type = FailureType.CANCELLED.value
    task.blockers = []
    task.ended_at = task.ended_at or time.time()


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
