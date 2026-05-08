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
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

from ..models import (
    CapabilityGap,
    CapabilityGrant,
    CapabilityRequest,
    ChannelProbeCheck,
    ContextManifest,
    EvidencePacket,
    Finding,
    QualityContract,
    StatusReport,
    SubAgentTask,
    TakeoverRecord,
    VerificationEvidence,
)
from ..utils import _apply_missing_paths, _read_json_object
from .checkpoint_artifacts import build_checkpoint_artifact_payloads
from .control_plane_projection import sync_subagent_control_plane_projection
from .failure_handoff import refresh_failure_handoff
from .persistence_failure_handoff import normalize_failure_handoff, write_failure_handoff
from .persistence_identity import normalize_runtime_identity
from .persistence_inheritance import normalize_inheritance_manifest, write_inheritance_manifest
from .persistence_recovery_outputs import write_recovery_output_files
from .persistence_security import normalize_security_signal
from .task_workspace_adapter import sync_task_workspace_fields


# LLM: _field_names 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理字段names相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
def _field_names(model: type) -> set[str]:
    return {item.name for item in fields(model)}


# LLM: _list_value 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 读取或查询value需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _list_value(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


# LLM: _string_list_value 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理stringlistvalue相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _string_list_value(value: object) -> list[str]:
    return [str(item) for item in _list_value(value) if item not in (None, "")]


# LLM: _normalize_quality_contract 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 解析并归一化qualitycontract的输入形态，让下游只处理稳定结构；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _normalize_quality_contract(value: object) -> QualityContract:
    if isinstance(value, QualityContract):
        return value
    if not isinstance(value, dict):
        return QualityContract()
    payload = {key: value[key] for key in _field_names(QualityContract) if key in value}
    for key in [
        "failure_conditions",
        "forbidden_delivery",
        "must_check",
        "sampling_plan",
        "evidence_required",
        "allowed_degradation",
    ]:
        payload[key] = _string_list_value(payload.get(key))
    payload["cannot_self_accept"] = bool(payload.get("cannot_self_accept", True))
    payload["parent_final_gate"] = bool(payload.get("parent_final_gate", True))
    return QualityContract(**payload)


# LLM: _normalize_context_manifest 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 解析并归一化上下文manifest的输入形态，让下游只处理稳定结构；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _normalize_context_manifest(value: object) -> ContextManifest:
    if isinstance(value, ContextManifest):
        return value
    if not isinstance(value, dict):
        return ContextManifest()
    payload = {key: value[key] for key in _field_names(ContextManifest) if key in value}
    for key in ["task_pack_refs", "required_read_paths", "omitted_context"]:
        payload[key] = _string_list_value(payload.get(key))
    try:
        payload["token_budget"] = int(payload.get("token_budget") or 0)
    except (TypeError, ValueError):
        payload["token_budget"] = 0
    return ContextManifest(**payload)


# LLM: _normalize_context_packs 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 解析并归一化上下文packs的输入形态，让下游只处理稳定结构；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _normalize_context_packs(value: object) -> list[dict[str, object]]:
    if isinstance(value, dict):
        return [value]
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


# LLM: _float_value 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理floatvalue相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
def _float_value(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# LLM: _dict_value 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理dictvalue相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
def _dict_value(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


# LLM: _normalize_evidence_packet 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 解析并归一化证据packet的输入形态，让下游只处理稳定结构；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _normalize_evidence_packet(value: object) -> EvidencePacket:
    if isinstance(value, EvidencePacket):
        return value
    if not isinstance(value, dict):
        return EvidencePacket()
    payload = {key: value[key] for key in _field_names(EvidencePacket) if key in value}
    for key in ["evidence_refs", "artifact_refs", "counter_evidence_refs", "unresolved_risks"]:
        payload[key] = _string_list_value(payload.get(key))
    payload["confidence"] = _float_value(payload.get("confidence"))
    payload["created_at"] = _float_value(payload.get("created_at"))
    return EvidencePacket(**payload)


# LLM: _normalize_finding 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 解析并归一化finding的输入形态，让下游只处理稳定结构；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _normalize_finding(value: object) -> Finding:
    if isinstance(value, Finding):
        return value
    if not isinstance(value, dict):
        return Finding()
    payload = {key: value[key] for key in _field_names(Finding) if key in value}
    for key in ["evidence_packet_ids", "evidence_refs", "counter_evidence_refs"]:
        payload[key] = _string_list_value(payload.get(key))
    payload["confidence"] = _float_value(payload.get("confidence"))
    payload["created_at"] = _float_value(payload.get("created_at"))
    return Finding(**payload)


# LLM: _normalize_status_report 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 解析并归一化状态报告的输入形态，让下游只处理稳定结构；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _normalize_status_report(value: object) -> StatusReport:
    if isinstance(value, StatusReport):
        return value
    if not isinstance(value, dict):
        return StatusReport()
    payload = {key: value[key] for key in _field_names(StatusReport) if key in value}
    payload["version"] = int(_float_value(payload.get("version"), 0.0))
    payload["progress"] = _float_value(payload.get("progress"))
    payload["summary_delta"] = _dict_value(payload.get("summary_delta"))
    payload["budget_used"] = _dict_value(payload.get("budget_used"))
    for key in ["artifact_refs", "evidence_refs", "blockers"]:
        payload[key] = _string_list_value(payload.get(key))
    payload["updated_at"] = _float_value(payload.get("updated_at"))
    return StatusReport(**payload)


# LLM: _summary_delta 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 渲染或汇总delta的展示文本，保持命令行、日志和审计输出一致；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
def _summary_delta(task: SubAgentTask) -> dict[str, list[str]]:
    return {
        "facts_added": [task.latest_summary] if task.latest_summary else [],
        "facts_invalidated": [],
        "decisions_changed": [],
        "open_questions": list(task.blockers),
    }


# LLM: build_status_report 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 构建状态报告所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def build_status_report(task: SubAgentTask) -> StatusReport:
    previous = task.latest_status_report if isinstance(task.latest_status_report, StatusReport) else StatusReport()
    progress = max(0.0, min(1.0, _float_value(task.progress)))
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
            CapabilityRequest(**item) for item in data.get("capability_requests", []) if isinstance(item, dict)
        ]
        data["capability_grants"] = [
            CapabilityGrant(**item) for item in data.get("capability_grants", []) if isinstance(item, dict)
        ]
        data["capability_gaps"] = [
            CapabilityGap(**item) for item in data.get("capability_gaps", []) if isinstance(item, dict)
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

    # LLM: save 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 写入save的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
    def save(self, task: SubAgentTask) -> None:
        """Persist a task as JSON plus human-readable Markdown."""

        _apply_missing_paths(task, self.manager._build_work_order_paths(task.id, task.task_dir or None))
        task_dir = Path(task.task_dir)
        task_dir.mkdir(parents=True, exist_ok=True)
        self.manager._ensure_work_order_files(task)
        task.updated_at = task.updated_at or time.time()
        if task.checkpoint_json:
            task.checkpoint_ref = task.checkpoint_json
        task.latest_status_report = build_status_report(task)
        refresh_failure_handoff(task)
        output_payload = _read_json_object(Path(task.output_json)) if task.output_json else {}
        checkpoint_artifacts = build_checkpoint_artifact_payloads(task, output_payload)
        # LLM: 任务工作区是增量运行记忆适配层，旧路径暂时仍是权威来源。
        sync_task_workspace_fields(self.workspace, task)
        payload = json.dumps(asdict(task), ensure_ascii=False, indent=2)
        (task_dir / "task.json").write_text(payload, encoding="utf-8")
        (task_dir / "run.json").write_text(payload, encoding="utf-8")
        if task.status_report_json:
            Path(task.status_report_json).write_text(
                json.dumps(asdict(task.latest_status_report), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        write_inheritance_manifest(task)
        write_failure_handoff(task)
        write_recovery_output_files(task, checkpoint_artifacts)
        (task_dir / "thought.md").write_text(self._render_thought_markdown(task), encoding="utf-8")
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

    # LLM: _render_thought_markdown 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 渲染或汇总thoughtmarkdown的展示文本，保持命令行、日志和审计输出一致；关键副作用: 会更新任务状态、报告记录和持久化副作用，需避免破坏既有状态机约定。
    @staticmethod
    def _render_thought_markdown(task: SubAgentTask) -> str:
        return (
            "# Thought\n\n"
            f"{task.thought}\n\n"
            "## Plan\n"
            + "\n".join(f"- {item}" for item in task.plan)
            + "\n\n"
            "## Capability Boundary\n"
            f"- Agent: {task.agent_name}\n"
            f"- Role: {task.role}\n"
            f"- Owner: {task.owner or 'none'}\n"
            f"- Supervisor: {task.supervisor or 'none'}\n"
            f"- Final owner: {task.final_owner or 'none'}\n"
            f"- Parent: {task.parent_id or 'none'}\n"
            f"- Depth: {task.depth}\n"
            f"- Allowed skills: {', '.join(task.allowed_skills) or 'none'}\n"
            f"- Allowed tools: {', '.join(task.allowed_tools) or 'none'}\n\n"
            "## Write Boundary\n"
            f"- Task dir: {task.task_dir}\n"
            f"- Allowed write roots: {', '.join(task.allowed_write_roots) or 'none'}\n"
            f"- Forbidden write roots: {', '.join(task.forbidden_write_roots) or 'none'}\n\n"
            "## Acceptance Checks\n"
            + "\n".join(f"- {item}" for item in task.acceptance_checks or ["未设置"])
            + "\n\n"
            "## Evidence\n"
            + "\n".join(f"- [{item.kind}] {item.summary}" for item in task.evidence or [])
            + ("\n" if task.evidence else "- 暂无\n")
        )
