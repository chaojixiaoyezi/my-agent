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


@dataclass
class EvidencePacket:
    """LLM: Evidence unit that backs a claim instead of trusting runner prose."""

    id: str = ""
    claim: str = ""
    checked_scope: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)
    counter_evidence_refs: list[str] = field(default_factory=list)
    confidence: float = 0.0
    unresolved_risks: list[str] = field(default_factory=list)
    created_at: float = 0.0


@dataclass
class Finding:
    """LLM: Parent-readable conclusion that must cite evidence packets."""

    id: str = ""
    claim: str = ""
    status: str = "OPEN"
    severity: str = ""
    confidence: float = 0.0
    evidence_packet_ids: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    counter_evidence_refs: list[str] = field(default_factory=list)
    created_at: float = 0.0


@dataclass
class StatusReport:
    """LLM: Latest observable progress snapshot for task-tree control plane."""

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
    # LLM: Phase 0 runtime memory task workspace paths mirror this legacy work order.
    task_workspace_dir: str = ""
    task_workspace_task_yaml: str = ""
    task_workspace_state_json: str = ""
    task_workspace_timeline_jsonl: str = ""
    task_workspace_summary_file: str = ""
    task_workspace_shared_dir: str = ""
    # LLM: Phase 5 shared workspace fields expose task-local collaboration facts only.
    task_workspace_shared_blackboard: str = ""
    task_workspace_shared_messages_jsonl: str = ""
    task_workspace_shared_findings_jsonl: str = ""
    task_workspace_shared_evidence_packets_dir: str = ""
    task_workspace_shared_evidence_index_jsonl: str = ""
    task_workspace_artifacts_dir: str = ""
    task_workspace_agents_dir: str = ""
    agent_run_workspace_dir: str = ""
    # LLM: Phase 1 agent-run workspace files are task-local mirrors of the legacy run.
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
    # LLM: Phase 4 compact chain fields point to checkpoint snapshots, not deleted context.
    agent_run_compaction_ledger_jsonl: str = ""
    agent_run_latest_compaction_summary_md: str = ""
    agent_run_latest_compaction_metadata_json: str = ""
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
