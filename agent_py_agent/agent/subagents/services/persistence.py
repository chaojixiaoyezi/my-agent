# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""persistence service for SubAgentManager task records.

给人看的解释：
子代理工单的读取、扫描和保存集中在这里。SubAgentManager 继续提供原方法名，
但具体持久化逻辑不再塞在 base mixin 里。
"""

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ...user_space.task_compact_rollup import sync_task_compact_rollup
from ..models import (
    CapabilityGap,
    CapabilityGrant,
    CapabilityRequest,
    ChannelProbeCheck,
    SubAgentTask,
    TakeoverRecord,
    VerificationEvidence,
)
from ..utils import _apply_missing_paths, _read_json_object
from .checkpoint_artifacts import build_checkpoint_artifact_payloads
from .control_plane_projection import sync_subagent_control_plane_projection
from .failure_handoff import refresh_failure_handoff
from .owner_indexes import register_owner_runtime_indexes
from .persistence_failure_handoff import normalize_failure_handoff, write_failure_handoff
from .persistence_identity import normalize_runtime_identity
from .persistence_inheritance import normalize_inheritance_manifest, write_inheritance_manifest
from .persistence_model_normalizers import (
    _field_names,
    _normalize_context_manifest,
    _normalize_context_packs,
    _normalize_evidence_packet,
    _normalize_finding,
    _normalize_nested_model,
    _normalize_quality_contract,
    _normalize_status_report,
)
from .persistence_recovery_outputs import write_recovery_output_files
from .persistence_rendering import render_thought_markdown
from .persistence_security import normalize_security_signal
from .persistence_status_report import build_status_report
from .task_workspace_adapter import sync_task_workspace_fields


# LLM: SubAgentPersistenceService 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 封装subagent持久化服务操作，把状态读写和错误处理收束在服务层；关键副作用: 方法可能触发任务状态、报告记录和持久化副作用相关副作用，需保持公开契约稳定。
class SubAgentPersistenceService:
    """Read and write SubAgentTask records for the manager facade."""

    # LLM: __init__ 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
    def __init__(self, manager: Any):
        self.manager = manager

    # LLM: workspace 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 处理workspace相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
    @property
    def workspace(self) -> Path:
        return self.manager.workspace

    # LLM: load 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 读取或查询load需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    def load(self, run_id: str) -> SubAgentTask:
        """Load one subagent task from disk."""

        path = self.workspace / run_id / "task.json"
        if not path.exists():
            raise FileNotFoundError(f"子代理记录不存在: {run_id}")
        data = json.loads(path.read_text(encoding="utf-8"))
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
        # LLM: Runtime identity normalization stays in a helper so this load path only wires audit scope metadata.
        data["runtime_identity"] = normalize_runtime_identity(data.get("runtime_identity"))
        return SubAgentTask(**data)

    # LLM: list_runs 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 读取或查询runs需要的状态，返回调用方可继续处理的快照；关键副作用: 会影响任务状态、报告记录和持久化副作用，需保持重试、超时和状态迁移语义。
    def list_runs(self) -> list[SubAgentTask]:
        """Scan the workspace for subagent task records."""

        runs: list[SubAgentTask] = []
        for task_file in sorted(self.workspace.glob("*/task.json")):
            try:
                runs.append(self.load(task_file.parent.name))
            except (FileNotFoundError, json.JSONDecodeError, TypeError):
                continue
        runs.sort(key=lambda item: item.updated_at or item.created_at, reverse=True)
        return runs

    # LLM: save preserves child links by default; hierarchy rewrites must explicitly opt out.
    # 函数用途: 写入任务状态和工单文件；默认合并已有 child_ids，受控重挂时可关闭合并以精确保存父子关系。
    def save(self, task: SubAgentTask, *, preserve_child_links: bool = True) -> None:
        """Persist a task as JSON plus human-readable Markdown."""

        _apply_missing_paths(task, self.manager._build_work_order_paths(task.id, task.task_dir or None))
        if preserve_child_links:
            _merge_existing_child_links(self, task)
            _merge_existing_takeover_state(self, task)
        task_dir = Path(task.task_dir)
        task_dir.mkdir(parents=True, exist_ok=True)
        self.manager._ensure_work_order_files(task)
        task.updated_at = task.updated_at or time.time()
        if task.checkpoint_json:
            task.checkpoint_ref = task.checkpoint_json
        _refresh_system_tree_snapshot(task)
        task.latest_status_report = build_status_report(task)
        refresh_failure_handoff(task)
        output_payload = _read_json_object(Path(task.output_json)) if task.output_json else {}
        checkpoint_artifacts = build_checkpoint_artifact_payloads(task, output_payload)
        # LLM: 任务工作区是增量运行记忆适配层，旧路径暂时仍是权威来源。
        sync_task_workspace_fields(self.workspace, task)
        _sync_task_rollup_if_possible(task)
        write_inheritance_manifest(task)
        write_failure_handoff(task)
        write_recovery_output_files(task, checkpoint_artifacts)
        payload = json.dumps(asdict(task), ensure_ascii=False, indent=2)
        (task_dir / "task.json").write_text(payload, encoding="utf-8")
        (task_dir / "run.json").write_text(payload, encoding="utf-8")
        if task.status_report_json:
            Path(task.status_report_json).write_text(
                json.dumps(asdict(task.latest_status_report), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        (task_dir / "thought.md").write_text(render_thought_markdown(task), encoding="utf-8")
        _write_owner_agent_projection(self.manager, task, payload)
        register_owner_runtime_indexes(self.manager, task)
        self.manager._index_task(task)
        if self.manager.local_store:
            # LLM: 控制面投影只给父级查询和 rollup 用，旧工单目录与 runtime workspace 仍是事实源。
            sync_subagent_control_plane_projection(self.manager.local_store, task)
            self.manager.local_store.task_registry.register_task(
                task_id=task.id,
                session_id=task.root_id,
                user_id=task.owner or "",
                status=task.status,
                goal=task.goal,
            )


def _write_owner_agent_projection(manager: Any, task: SubAgentTask, payload: str) -> None:
    # LLM: owner projection is a refs-only lookup mirror; task.json remains the detailed run record.
    owner_home = str(getattr(manager, "owner_home_dir", "") or "").strip()
    if not owner_home:
        return
    root = Path(owner_home) / "agents" / task.id
    root.mkdir(parents=True, exist_ok=True)
    (root / "state.json").write_text(payload, encoding="utf-8")
    refs = {
        "schema_version": "owner-agent-projection.v1",
        "run_id": task.id,
        "owner_id": task.owner,
        "task_workspace_dir": task.task_workspace_dir,
        "agent_run_workspace_dir": task.agent_run_workspace_dir,
        "compact_dir": task.agent_run_compactions_dir,
        "final_report": task.agent_run_final_report_md,
        "updated_at": task.updated_at,
    }
    (root / "refs.json").write_text(json.dumps(refs, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _sync_task_rollup_if_possible(task: SubAgentTask) -> None:
    # LLM: task compact rollup summarizes child run refs so parents do not scan every child compact package first.
    # 函数用途: 子代理保存时同步任务级 compact rollup，给父代理恢复和汇总提供入口摘要。
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


# LLM: _merge_existing_child_links protects hierarchy edges from stale full-object saves.
# 函数用途: 保存任务前合并磁盘上已有 child_ids，避免父任务旧快照覆盖新创建的子任务链接。
def _merge_existing_child_links(service: SubAgentPersistenceService, task: SubAgentTask) -> None:
    try:
        existing = service.load(task.id)
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return
    task.child_ids = _unique_strings([*existing.child_ids, *task.child_ids])


# LLM: stale runner snapshots must not undo a takeover that was already recorded by the parent.
# 函数用途: 保存旧 task 对象时保留磁盘上的 TAKEN_OVER/takeover_by，避免超时线程或旧父级快照把接管状态写回 TIMEOUT。
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


# LLM: system_tree is a system-owned read snapshot, never model-provided task content.
# 函数用途: 每次保存都从 SubAgentTask 字段重建树节点摘要，覆盖子代理输出里可能夹带的伪造 system_tree。
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


# LLM: _unique_strings keeps append-only refs stable while removing duplicates.
# 函数用途: 合并 child_ids 时保留首次出现顺序，避免重复链接污染 board 和控制面展示。
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
