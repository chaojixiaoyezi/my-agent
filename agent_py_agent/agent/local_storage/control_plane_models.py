# LLM: Control-plane records are query projections; files remain the authoritative task/run source.
# 模块用途: 定义 Agent Runtime 控制面查询投影的数据结构。

from __future__ import annotations

"""data models for LocalStore agent runtime control-plane projections.

给人看的解释：
这些模型不是新的事实源，而是把 task/run workspace 的关键信息投影进 SQLite，
方便上级代理或接管代理快速查看 agent tree、阻塞状态和任务汇总。
"""

from dataclasses import dataclass, field
from typing import Any


# LLM: AgentRunRecord mirrors one run row; reserved keeps future schema changes contained.
# 类用途: 保存单个 agent run 在控制面里的可查询状态。
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


# LLM: AgentEventInput is the append-only event write contract for run lifecycle events.
# 类用途: 写入 agent_events 表时使用的输入包。
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


# LLM: AgentEventRecord is the hydrated event row returned by LocalStore control-plane APIs.
# 类用途: 表示一条 agent 生命周期事件，供查询和调试展示。
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


# LLM: TaskRollupRecord is a fast status projection, not a replacement for task workspace files.
# 类用途: 保存一个 root task 的聚合状态，供 status/board 快速读取。
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


# LLM: AgentTreeReport groups control-plane rows into a stable tree query result.
# 类用途: 返回上级代理或接管代理查看任务树或子树时需要的记录集合和汇总。
@dataclass(frozen=True)
class AgentTreeReport:
    task_id: str
    runs: list[AgentRunRecord]
    rollup: TaskRollupRecord | None = None


# LLM: AgentRuntimeQueryContext is the bundle-first query contract for hierarchical runtime views.
# 类用途: 描述哪个代理为了什么目的查询哪一段 agent runtime 投影。
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


# LLM: AgentRuntimeQueryResult keeps the query context attached to returned rows for audit/debug.
# 类用途: 返回层级查询结果，同时保留归一化后的查询上下文和扩展提示。
@dataclass(frozen=True)
class AgentRuntimeQueryResult:
    context: AgentRuntimeQueryContext
    report: AgentTreeReport
    warnings: list[str] = field(default_factory=list)
    reserved: dict[str, Any] = field(default_factory=dict)


# LLM: SharedProgressPanel is a read model for upper-agent and takeover progress views.
# 类用途: 汇总 runtime query、rollup、阻塞任务和继承清单引用，供共享进度面板读取。
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
