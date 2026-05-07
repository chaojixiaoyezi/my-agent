# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""Subagent task and learning-candidate dataclasses."""

from dataclasses import dataclass, field

from .model_capabilities import (
    CapabilityGap,
    CapabilityGrant,
    CapabilityRequest,
    VerificationEvidence,
)
from .model_records import ChannelProbeCheck, TakeoverRecord
from .quality_models import ContextManifest, QualityContract


# LLM: EvidencePacket 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存证据packet字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发任务状态、执行器结果、验收和报告展示相关副作用，需保持公开契约稳定。
@dataclass
class EvidencePacket:

    id: str = ""
    claim: str = ""
    checked_scope: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)
    counter_evidence_refs: list[str] = field(default_factory=list)
    confidence: float = 0.0
    unresolved_risks: list[str] = field(default_factory=list)
    created_at: float = 0.0


# LLM: Finding 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存finding字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发任务状态、执行器结果、验收和报告展示相关副作用，需保持公开契约稳定。
@dataclass
class Finding:

    id: str = ""
    claim: str = ""
    status: str = "OPEN"
    severity: str = ""
    confidence: float = 0.0
    evidence_packet_ids: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    counter_evidence_refs: list[str] = field(default_factory=list)
    created_at: float = 0.0


# LLM: StatusReport 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存状态报告字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass
class StatusReport:

    run_id: str = ""
    version: int = 0
    state: str = "PLANNING"
    progress: float = 0.0
    current_step: str = ""
    summary_delta: dict[str, list[str]] = field(default_factory=dict)
    budget_used: dict[str, object] = field(default_factory=dict)
    artifact_refs: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    checkpoint_ref: str = ""
    next_recommended_action: str = ""
    updated_at: float = 0.0


# LLM: SubAgentTask 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存subagent任务字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发任务状态、执行器结果、验收和报告展示相关副作用，需保持公开契约稳定。
@dataclass
class SubAgentTask:
    """Persistent run record for one subagent task."""

    id: str
    goal: str
    thought: str
    plan: list[str]
    agent_name: str = "general"
    role: str = "general"
    owner: str = ""
    supervisor: str = ""
    final_owner: str = ""
    parent_id: str = ""
    root_id: str = ""
    depth: int = 0
    allowed_skills: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    used_skills: list[str] = field(default_factory=list)
    used_tools: list[str] = field(default_factory=list)
    capability_requests: list[CapabilityRequest] = field(default_factory=list)
    capability_grants: list[CapabilityGrant] = field(default_factory=list)
    capability_gaps: list[CapabilityGap] = field(default_factory=list)
    acceptance_checks: list[str] = field(default_factory=list)
    evidence: list[VerificationEvidence] = field(default_factory=list)
    evidence_packets: list[EvidencePacket] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    quality_contract: QualityContract = field(default_factory=QualityContract)
    context_manifest: ContextManifest = field(default_factory=ContextManifest)
    context_packs: list[dict[str, object]] = field(default_factory=list)
    child_ids: list[str] = field(default_factory=list)
    status: str = "PLANNING"
    description: str = ""
    paused_at: float = 0.0
    abandoned_at: float = 0.0
    verification_status: str = "UNVERIFIED"
    failure_type: str = ""
    result: str = ""
    runner_attempts: int = 0
    runner_last_attempt_at: float = 0.0
    runner_last_error: str = ""
    runner_active_attempt_id: str = ""
    runner_abandoned_attempt_ids: list[str] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0
    heartbeat_at: float = 0.0
    ended_at: float = 0.0
    progress: float = 0.0
    current_step: str = ""
    latest_summary: str = ""
    blockers: list[str] = field(default_factory=list)
    budget_used: dict[str, object] = field(default_factory=dict)
    artifact_refs: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    checkpoint_ref: str = ""
    latest_status_report: StatusReport = field(default_factory=StatusReport)
    task_dir: str = ""
    data_dir: str = ""
    output_dir: str = ""
    tests_dir: str = ""
    reports_dir: str = ""
    logs_dir: str = ""
    scratch_dir: str = ""
    status_file: str = ""
    work_log_file: str = ""
    action_receipts_file: str = ""
    acceptance_file: str = ""
    test_checklist_file: str = ""
    bugs_file: str = ""
    skill_usage_file: str = ""
    skill_sparks_file: str = ""
    # LLM: 第 0 阶段运行记忆的任务工作区路径镜像旧工作单位置。
    task_workspace_dir: str = ""
    task_workspace_task_yaml: str = ""
    task_workspace_state_json: str = ""
    task_workspace_timeline_jsonl: str = ""
    task_workspace_summary_file: str = ""
    task_workspace_shared_dir: str = ""
    # LLM: 第 5 阶段共享工作区字段只暴露任务本地协作事实。
    task_workspace_shared_blackboard: str = ""
    task_workspace_shared_messages_jsonl: str = ""
    task_workspace_shared_findings_jsonl: str = ""
    task_workspace_shared_evidence_packets_dir: str = ""
    task_workspace_shared_evidence_index_jsonl: str = ""
    task_workspace_artifacts_dir: str = ""
    task_workspace_agents_dir: str = ""
    agent_run_workspace_dir: str = ""
    # LLM: 第 1 阶段代理运行工作区文件是旧运行目录的任务本地镜像。
    agent_run_agent_yaml: str = ""
    agent_run_state_json: str = ""
    agent_run_task_md: str = ""
    agent_run_timeline_jsonl: str = ""
    agent_run_checkpoint_json: str = ""
    agent_run_summary_md: str = ""
    agent_run_final_report_md: str = ""
    agent_run_findings_jsonl: str = ""
    agent_run_inbox_dir: str = ""
    agent_run_outbox_dir: str = ""
    agent_run_artifacts_dir: str = ""
    agent_run_compactions_dir: str = ""
    legacy_run_ref_json: str = ""
    # LLM: Phase 2 daily ledger stores compact task/run refs, not full subagent context.
    daily_ledger_file: str = ""
    daily_ledger_last_event_id: str = ""
    # LLM: Phase 3 artifact manifests normalize refs into summary/hash/path records.
    task_artifact_manifest_jsonl: str = ""
    agent_run_artifact_manifest_jsonl: str = ""
    # LLM: 第 4 阶段压缩链字段指向检查点快照，不表示上下文已删除。
    agent_run_compaction_ledger_jsonl: str = ""
    agent_run_latest_compaction_summary_md: str = ""
    agent_run_latest_compaction_metadata_json: str = ""
    # LLM: Phase 6 gate files hold review candidates only; they never auto-promote memory.
    agent_run_memory_gate_dir: str = ""
    agent_run_memory_candidates_jsonl: str = ""
    agent_run_memory_review_queue_jsonl: str = ""
    # LLM: decision logs are audit refs; they are not long-term memory exports.
    agent_run_memory_decisions_jsonl: str = ""
    # LLM: export logs are explicit promotion audit refs, never automatic writes.
    agent_run_memory_exports_jsonl: str = ""
    agent_run_skill_spark_gate_json: str = ""
    handoff_file: str = ""
    debrief_file: str = ""
    output_json: str = ""
    status_report_json: str = ""
    # LLM: compact/checkpoint recovery artifacts stay separate from full chat history.
    checkpoint_json: str = ""
    decision_ledger_json: str = ""
    progress_md: str = ""
    failing_tests_json: str = ""
    next_actions_json: str = ""
    dependencies_json: str = ""
    takeover_file: str = ""
    execution_context_file: str = ""
    execution_context_json: str = ""
    runner_result_file: str = ""
    runner_result_json: str = ""
    runner_prompt_file: str = ""
    runner_response_file: str = ""
    allowed_write_roots: list[str] = field(default_factory=list)
    forbidden_write_roots: list[str] = field(default_factory=list)
    workflow_mode: str = "off"
    workflow_template_id: str = ""
    workflow_plan: dict[str, object] = field(default_factory=dict)
    workflow_parent_run_id: str = ""
    workflow_phase_id: str = ""
    workflow_depends_on: list[str] = field(default_factory=list)
    workflow_child_run_ids: list[str] = field(default_factory=list)
    takeover_by: str = ""
    takeover_reason: str = ""
    locked_files: list[str] = field(default_factory=list)
    takeover_records: list[TakeoverRecord] = field(default_factory=list)
    channel_status: str = "UNKNOWN"
    last_probe_at: float = 0.0
    channel_checks: list[ChannelProbeCheck] = field(default_factory=list)
    channel_probe_file: str = ""
    attributes: dict[str, object] = field(default_factory=dict)


# LLM: LearningCandidate 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存learningcandidate字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发任务状态、执行器结果、验收和报告展示相关副作用，需保持公开契约稳定。
@dataclass
class LearningCandidate:
    """Candidate lesson that may later be accepted into learning memory."""

    id: str
    lesson: str
    normalized_key: str
    status: str = "draft"
    confidence: float = 0.5
    occurrence_count: int = 1
    evidence_count: int = 1
    source_runs: list[str] = field(default_factory=list)
    evidence: list[dict[str, object]] = field(default_factory=list)
    variants: list[str] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0
