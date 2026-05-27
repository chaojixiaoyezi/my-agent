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


# LLM: RuntimeIdentity is audit-only scope metadata for future tenant/conversation isolation.
# 类用途: 记录服务所有者、请求者、会话、记忆命名空间和配置覆盖边界；当前只用于追踪，不授予额外权限。
@dataclass
class RuntimeIdentity:

    service_owner_id: str = ""
    requester_id: str = ""
    effective_principal_id: str = ""
    conversation_id: str = ""
    root_run_id: str = ""
    memory_namespace: str = ""
    conversation_memory_policy: str = "not_enabled"
    promotion_policy: str = "explicit_review"
    config_scope: str = "run_override"
    config_overlay_ref: str = ""
    config_promotion_policy: str = "admin_approval_required"
    reserved: dict[str, object] = field(default_factory=dict)


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


# LLM: InheritanceManifest records parent-to-child projection decisions without expanding parent context.
# 类用途: 记录子代理从上级继承、覆盖和裁剪了哪些上下文/能力字段。
@dataclass
class InheritanceManifest:

    source_run_id: str = ""
    target_run_id: str = ""
    root_task_id: str = ""
    inherited: dict[str, object] = field(default_factory=dict)
    overridden: dict[str, object] = field(default_factory=dict)
    dropped: dict[str, object] = field(default_factory=dict)
    policy: dict[str, object] = field(default_factory=dict)
    created_at: float = 0.0
    reserved: dict[str, object] = field(default_factory=dict)


# LLM: FailureHandoff records warnings and recovery advice before/after a run fails.
# 类用途: 保存失败交接事实，帮助后续接管代理避开同一个坑。
@dataclass
class FailureHandoff:

    run_id: str = ""
    status: str = ""
    failure_type: str = ""
    risk_level: str = ""
    warning: str = ""
    last_safe_checkpoint_ref: str = ""
    artifact_refs: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    avoid_next_time: list[str] = field(default_factory=list)
    recommended_next_action: str = ""
    created_at: float = 0.0
    reserved: dict[str, object] = field(default_factory=dict)


# LLM: SecuritySignal is an audit-only reserve for future hijack/deception defenses.
# 类用途: 记录可疑安全信号和证据引用，先给后续安全模块预留读取入口。
@dataclass
class SecuritySignal:

    signal_type: str = ""
    severity: str = ""
    summary: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)
    created_at: float = 0.0
    reserved: dict[str, object] = field(default_factory=dict)


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
    # LLM: Session identity sits above run_id so retries/takeovers/compact continuation can share an owner.
    # 字段用途: subagent_session_id 表示一个独立子代理会话，agent_thread_id 表示当前模型对话线程；parent/root 字段用于跨层恢复。
    subagent_session_id: str = ""
    agent_thread_id: str = ""
    parent_subagent_session_id: str = ""
    root_subagent_session_id: str = ""
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
    # LLM: effective_permissions is a system-derived snapshot, not a model-editable grant.
    # 字段用途: 记录子代理最终 shell/工具边界；父级 full-access 默认降级为 workspace-write。
    effective_permissions: dict[str, object] = field(default_factory=dict)
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
    # LLM: current_tool and last_progress_* are read-only observability facts for parent/status tools.
    # 字段用途: 记录最近观测到的工具和最近真实推进摘要；它们不授予权限，也不触发调度。
    current_tool: str = ""
    last_progress_at: float = 0.0
    last_progress_summary: str = ""
    latest_summary: str = ""
    blockers: list[str] = field(default_factory=list)
    budget_used: dict[str, object] = field(default_factory=dict)
    artifact_refs: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    checkpoint_ref: str = ""
    latest_status_report: StatusReport = field(default_factory=StatusReport)
    inheritance_manifest: InheritanceManifest = field(default_factory=InheritanceManifest)
    failure_handoff: FailureHandoff = field(default_factory=FailureHandoff)
    security_signals: list[SecuritySignal] = field(default_factory=list)
    security_review_required: bool = False
    runtime_identity: RuntimeIdentity = field(default_factory=RuntimeIdentity)
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
    # LLM: session compact refs are separate from checkpoint compact refs so runner resume never overwrites checkpoints.
    # 字段用途: 指向子代理会话续接包的最新 metadata/summary/packet；只用于 task-local 自动续跑和接管。
    agent_run_session_compaction_ledger_jsonl: str = ""
    agent_run_latest_session_compaction_summary_md: str = ""
    agent_run_latest_session_compaction_metadata_json: str = ""
    agent_run_latest_session_continue_packet_json: str = ""
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
    inheritance_manifest_json: str = ""
    failure_handoff_json: str = ""
    # LLM: takeover readiness files list recovery refs; they must not embed large artifact bodies.
    takeover_readiness_json: str = ""
    takeover_readiness_md: str = ""
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
