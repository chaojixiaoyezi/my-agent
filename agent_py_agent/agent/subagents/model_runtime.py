# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""Runtime dataclasses for subagent execution and runner output."""

from dataclasses import dataclass, field

from .quality_models import ContextManifest, QualityContract


# LLM: SubAgentExecutionContext 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存subagentexecution上下文字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass
class SubAgentExecutionContext:
    """Minimum authorized context passed to a subagent runner."""

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


# LLM: SubAgentRunnerResult 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存subagent执行器结果字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass
class SubAgentRunnerResult:
    """Result of one subagent runner invocation."""

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


# LLM: SubAgentParsedOutput 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存subagentparsedoutput字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发任务状态、执行器结果、验收和报告展示相关副作用，需保持公开契约稳定。
@dataclass
class SubAgentParsedOutput:
    """Machine-readable output parsed from a runner model response."""

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
    # LLM: evidence_packets/findings keep claims traceable for parent acceptance.
    evidence_packets: list[dict[str, object]] = field(default_factory=list)
    findings: list[dict[str, object]] = field(default_factory=list)
    capability_requests: list[dict[str, object]] = field(default_factory=list)
    artifacts: list[dict[str, object]] = field(default_factory=list)
    tests: list[dict[str, object]] = field(default_factory=list)
    patches: list[dict[str, object]] = field(default_factory=list)
    lessons: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    raw_json: dict[str, object] = field(default_factory=dict)
