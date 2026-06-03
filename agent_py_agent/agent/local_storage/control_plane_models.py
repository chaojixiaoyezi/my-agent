
from __future__ import annotations

"""data models for LocalStore agent runtime control-plane projections.

这些模型不是新的事实源，而是把 task/run workspace 的关键信息投影进 SQLite，
方便上级代理或接管代理快速查看 agent tree、阻塞状态和任务汇总。
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AgentRunRecord:
    run_id: str
    root_task_id: str
    parent_run_id: str = ""
    depth: int = 0
    role: str = ""
    agent_name: str = ""
    status: str = ""
    progress: float = 0.0
    current_step: str = ""
    latest_summary: str = ""
    workspace_path: str = ""
    checkpoint_ref: str = ""
    latest_compact_ref: str = ""
    compact_count: int = 0
    heartbeat_at: float = 0.0
    created_at: float = 0.0
    updated_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    reserved: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentEventInput:
    root_task_id: str
    run_id: str
    parent_run_id: str = ""
    event_type: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    event_id: str = ""
    reserved: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentEventRecord:
    event_id: str
    root_task_id: str
    run_id: str
    parent_run_id: str
    event_type: str
    payload: dict[str, Any]
    created_at: float
    reserved: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskRollupRecord:
    task_id: str
    status: str = ""
    progress: float = 0.0
    running_agents: int = 0
    blocked_agents: int = 0
    completed_agents: int = 0
    failed_agents: int = 0
    latest_summary: str = ""
    updated_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    reserved: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentTreeReport:
    task_id: str
    runs: list[AgentRunRecord]
    rollup: TaskRollupRecord | None = None


@dataclass(frozen=True)
class AgentRuntimeQueryContext:
    requester_run_id: str = ""
    service_owner_id: str = ""
    requester_id: str = ""
    effective_principal_id: str = ""
    conversation_id: str = ""
    memory_namespace: str = ""
    config_scope: str = ""
    root_task_id: str = ""
    target_run_id: str = ""
    scope: str = "root_tree"
    purpose: str = ""
    requester_role: str = ""
    authorized_scope: str = ""
    visibility: str = ""
    reserved: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentRuntimeQueryResult:
    context: AgentRuntimeQueryContext
    report: AgentTreeReport
    warnings: list[str] = field(default_factory=list)
    reserved: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SharedProgressPanel:
    context: AgentRuntimeQueryContext
    rollup: TaskRollupRecord | None
    runs: list[AgentRunRecord]
    blocked_runs: list[AgentRunRecord]
    inheritance_manifest_refs: list[str] = field(default_factory=list)
    failure_handoff_refs: list[str] = field(default_factory=list)
    takeover_readiness_refs: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    reserved: dict[str, Any] = field(default_factory=dict)
