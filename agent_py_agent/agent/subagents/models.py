from __future__ import annotations

"""LLM contract: core subagent state and execution dataclasses.

Human version:
这里放“子代理运行本身”需要的数据结构，比如任务、证据、能力请求、执行上下文。
这些类只描述数据，不做调度、不写文件。
"""

from dataclasses import dataclass, field

@dataclass
class QualityContract:
    """Structured quality bar passed from the parent session to a worker."""

    user_visible_goal: str = ""
    benchmark_sample: str = ""
    quality_bar: str = ""
    failure_conditions: list[str] = field(default_factory=list)
    forbidden_delivery: list[str] = field(default_factory=list)
    must_check: list[str] = field(default_factory=list)
    sampling_plan: list[str] = field(default_factory=list)
    evidence_required: list[str] = field(default_factory=list)
    risk_report_required: str = ""
    allowed_degradation: list[str] = field(default_factory=list)
    final_judge: str = "parent_final_gate"
    cannot_self_accept: bool = True
    parent_final_gate: bool = True


@dataclass
class ContextManifest:
    """Manifest describing the focused context packs given to a worker."""

    core_pack_version: str = "subagent-quality-contract-v1"
    task_pack_refs: list[str] = field(default_factory=list)
    role_pack: str = ""
    required_read_paths: list[str] = field(default_factory=list)
    quality_contract_ref: str = ""
    omitted_context: list[str] = field(default_factory=list)
    token_budget: int = 0


@dataclass
class SubAgentCard:
    """子代理角色卡。

    它描述的是“这个子代理适合干什么，以及默认有哪些边界”。
    后续可以从文件加载，也可以由父代理临时生成。
    """

    name: str
    description: str
    role: str = "general"
    default_model: str = "inherit"
    allowed_skills: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    can_write: bool = False
    can_spawn_children: bool = False
    can_request_capability: bool = True
    max_depth: int = 0
    result_contract: list[str] = field(default_factory=list)


@dataclass
class CapabilityRequest:
    """子代理向父代理上抛的能力请求。"""

    id: str
    from_run_id: str
    problem: str
    needed_capability: str
    expected_output: str = ""
    tried: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    constraints: dict[str, str] = field(default_factory=dict)
    status: str = "OPEN"
    created_at: float = 0.0


@dataclass
class CapabilityGrant:
    """上级代理下发给子代理的能力授权。"""

    id: str
    request_id: str
    grant_to_run_id: str
    skills: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    capability_cards: list[dict[str, str]] = field(default_factory=list)
    reason: str = ""
    constraints: dict[str, str] = field(default_factory=dict)
    expires_after_task: bool = True
    created_at: float = 0.0


@dataclass
class CapabilityGap:
    """最终没找到 skill/tool 时留下的能力缺口。"""

    id: str
    run_id: str
    missing_capability: str
    source_task: str
    why_failed: str
    attempted_skills: list[str] = field(default_factory=list)
    attempted_tools: list[str] = field(default_factory=list)
    needed_outputs: list[str] = field(default_factory=list)
    suggested_skill: str = ""
    suggested_tool: str = ""
    memory_routes: list[dict[str, str]] = field(default_factory=list)
    injected_rule_paths: list[str] = field(default_factory=list)
    status: str = "OPEN"
    created_at: float = 0.0


@dataclass
class VerificationEvidence:
    """子代理交付物的验收证据。"""

    kind: str
    summary: str
    command: str = ""
    path: str = ""
    url: str = ""
    ok: bool = True
    created_at: float = 0.0


@dataclass
class WorkOrderValidation:
    """工单目录校验结果。"""

    run_id: str
    ok: bool
    missing: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class TakeoverRecord:
    """父代理或上级代理接管子代理任务的记录。"""

    id: str
    run_id: str
    take_over_by: str
    reason: str
    locked_files: list[str] = field(default_factory=list)
    previous_owner: str = ""
    created_at: float = 0.0


@dataclass
class ChannelProbeCheck:
    """通道健康检查中的单项结果。"""

    name: str
    ok: bool
    summary: str
    severity: str = "P1"
    evidence_path: str = ""
    error: str = ""
    created_at: float = 0.0


@dataclass
class ChannelProbeResult:
    """单个子代理运行的通道健康检查结果。"""

    run_id: str
    channel_status: str
    checks: list[ChannelProbeCheck]
    task_dir: str = ""
    goal: str = ""
    created_at: float = 0.0


@dataclass
class ChannelProbeReport:
    """批量通道健康检查报告。"""

    generated_at: float
    summary: dict[str, int]
    results: list[ChannelProbeResult]


@dataclass
class SubAgentExecutionContext:
    """下发给子代理执行器的瘦身上下文。

    它不是全局 skill/tool 清单，而是父代理确认过的最小授权包。
    未来真正启动子代理时，runner 应优先读取这份上下文。
    """

    run_id: str
    generated_at: float
    goal: str
    thought: str
    plan: list[str]
    agent_name: str = "general"
    role: str = "general"
    status: str = "PLANNING"
    verification_status: str = "UNVERIFIED"
    channel_status: str = "UNKNOWN"
    runner_attempts: int = 0
    runner_last_error: str = ""
    owner: str = ""
    supervisor: str = ""
    final_owner: str = ""
    parent_id: str = ""
    root_id: str = ""
    depth: int = 0
    task_dir: str = ""
    execution_context_file: str = ""
    execution_context_json: str = ""
    allowed_skills: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    granted_cards: list[dict[str, str]] = field(default_factory=list)
    grants: list[dict[str, object]] = field(default_factory=list)
    acceptance_checks: list[str] = field(default_factory=list)
    evidence: list[dict[str, object]] = field(default_factory=list)
    quality_contract: QualityContract = field(default_factory=QualityContract)
    context_manifest: ContextManifest = field(default_factory=ContextManifest)
    context_packs: list[dict[str, object]] = field(default_factory=list)
    write_boundary: dict[str, object] = field(default_factory=dict)
    pending_requests: list[dict[str, object]] = field(default_factory=list)
    open_gaps: list[dict[str, object]] = field(default_factory=list)
    instructions: list[str] = field(default_factory=list)


@dataclass
class SubAgentRunnerResult:
    """一次子代理 runner 入口调用的结果。"""

    run_id: str
    dry_run: bool
    ok: bool
    status: str
    verification_status: str
    message: str
    backend: str = ""
    tool_rounds: int = 0
    runner_attempts: int = 0
    runner_last_error: str = ""
    execution_context_json: str = ""
    execution_context_file: str = ""
    prompt_file: str = ""
    response_file: str = ""
    result_file: str = ""
    result_json: str = ""
    output_json: str = ""
    structured_output_found: bool = False
    structured_output_ok: bool = False
    structured_parse_error: str = ""
    structured_repair_attempted: bool = False
    structured_repair_ok: bool = False
    structured_repair_error: str = ""
    structured_summary: str = ""
    evidence_count: int = 0
    capability_request_count: int = 0
    artifact_count: int = 0
    test_count: int = 0
    patch_count: int = 0
    lesson_count: int = 0
    blocked_reason: str = ""
    created_at: float = 0.0


@dataclass
class SubAgentParsedOutput:
    """从 runner 模型回复里解析出来的机器结果。"""

    found: bool = False
    ok: bool = False
    parse_error: str = ""
    status: str = ""
    summary: str = ""
    blocked_reason: str = ""
    failure_type: str = ""
    used_skills: list[str] = field(default_factory=list)
    used_tools: list[str] = field(default_factory=list)
    evidence: list[dict[str, object]] = field(default_factory=list)
    capability_requests: list[dict[str, object]] = field(default_factory=list)
    artifacts: list[dict[str, object]] = field(default_factory=list)
    tests: list[dict[str, object]] = field(default_factory=list)
    patches: list[dict[str, object]] = field(default_factory=list)
    lessons: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    raw_json: dict[str, object] = field(default_factory=dict)


@dataclass
class SubAgentTask:
    """子代理运行记录。

    名字继续叫 `SubAgentTask` 是为了兼容现有代码和测试；
    实际上它已经是一个轻量 SubAgentRun。
    """

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
    quality_contract: QualityContract = field(default_factory=QualityContract)
    context_manifest: ContextManifest = field(default_factory=ContextManifest)
    context_packs: list[dict[str, object]] = field(default_factory=list)
    child_ids: list[str] = field(default_factory=list)
    status: str = "PLANNING"
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
    handoff_file: str = ""
    debrief_file: str = ""
    output_json: str = ""
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


@dataclass
class LearningCandidate:
    """自学习候选草稿。

    这是“可能值得沉淀”的 lesson 草稿，不代表已经自动升级成正式 skill。
    只有在用户显式 accept 后，状态才会从 draft 变成 accepted。
    """

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
