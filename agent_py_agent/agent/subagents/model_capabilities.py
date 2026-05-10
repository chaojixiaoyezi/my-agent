# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""Capability and evidence dataclasses used by subagent runs."""

from dataclasses import dataclass, field


# LLM: CapabilityRequest 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存能力请求字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发任务状态、执行器结果、验收和报告展示相关副作用，需保持公开契约稳定。
@dataclass
class CapabilityRequest:
    """Request from a subagent for a missing capability."""

    id: str
    from_run_id: str
    problem: str
    needed_capability: str
    expected_output: str = ""
    # LLM: 以下 scoped request 字段是后续 shell/MCP/tool/skill 网关的稳定申请口，旧记录缺省为 generic。
    capability_type: str = "generic"
    tried: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    constraints: dict[str, str] = field(default_factory=dict)
    requested_tools: list[str] = field(default_factory=list)
    requested_skills: list[str] = field(default_factory=list)
    requested_mcp_tools: list[str] = field(default_factory=list)
    requested_commands: list[str] = field(default_factory=list)
    cwd_scope: list[str] = field(default_factory=list)
    path_scope: list[str] = field(default_factory=list)
    network_scope: list[str] = field(default_factory=list)
    output_budget: dict[str, object] = field(default_factory=dict)
    risk_level: str = ""
    fallback_attempted: list[str] = field(default_factory=list)
    escalation_target: str = ""
    status: str = "OPEN"
    created_at: float = 0.0
    reserved: dict[str, object] = field(default_factory=dict)


# LLM: CapabilityGrant 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存能力grant字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发任务状态、执行器结果、验收和报告展示相关副作用，需保持公开契约稳定。
@dataclass
class CapabilityGrant:
    """Capability granted by a parent/supervisor to a subagent."""

    id: str
    request_id: str
    grant_to_run_id: str
    # LLM: grant 的 scope 字段只表示父级授权边界，不等于立即执行任何工具。
    grant_type: str = "generic"
    skills: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    mcp_tools: list[str] = field(default_factory=list)
    command_allowlist: list[str] = field(default_factory=list)
    capability_cards: list[dict[str, str]] = field(default_factory=list)
    reason: str = ""
    constraints: dict[str, str] = field(default_factory=dict)
    path_scope: list[str] = field(default_factory=list)
    network_scope: list[str] = field(default_factory=list)
    output_budget: dict[str, object] = field(default_factory=dict)
    risk_level: str = ""
    expires_after_task: bool = True
    expires_at: float = 0.0
    created_at: float = 0.0
    reserved: dict[str, object] = field(default_factory=dict)


# LLM: CapabilityGap 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存能力缺口字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发任务状态、执行器结果、验收和报告展示相关副作用，需保持公开契约稳定。
@dataclass
class CapabilityGap:
    """A capability gap left after routing/search could not find a match."""

    id: str
    run_id: str
    missing_capability: str
    source_task: str
    why_failed: str
    # LLM: gap 保留原始申请范围，方便后续升级给父级、skill_spark 或人工处理。
    gap_type: str = "generic"
    attempted_skills: list[str] = field(default_factory=list)
    attempted_tools: list[str] = field(default_factory=list)
    needed_outputs: list[str] = field(default_factory=list)
    suggested_skill: str = ""
    suggested_tool: str = ""
    requested_scope: dict[str, object] = field(default_factory=dict)
    escalation_chain: list[str] = field(default_factory=list)
    next_record_refs: list[str] = field(default_factory=list)
    memory_routes: list[dict[str, str]] = field(default_factory=list)
    injected_rule_paths: list[str] = field(default_factory=list)
    status: str = "OPEN"
    created_at: float = 0.0
    reserved: dict[str, object] = field(default_factory=dict)


# LLM: VerificationEvidence 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存verification证据字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发任务状态、执行器结果、验收和报告展示相关副作用，需保持公开契约稳定。
@dataclass
class VerificationEvidence:
    """Evidence produced by a subagent for verification/acceptance."""

    kind: str
    summary: str
    command: str = ""
    path: str = ""
    url: str = ""
    ok: bool = True
    created_at: float = 0.0
