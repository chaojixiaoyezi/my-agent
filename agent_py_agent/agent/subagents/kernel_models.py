# LLM: Subagent kernel models stay separate from snapshot logic to keep the read model small.
# 模块用途: 定义子代理 kernel 只读快照的数据结构。

from __future__ import annotations

from dataclasses import dataclass, field


# LLM: SubagentKernelQuery is the bundle-shaped read contract for kernel snapshots.
# 类用途: 描述调用方要读取 root tree、某个子树，还是全部可见 run；默认只读，不产生副作用。
@dataclass(frozen=True)
class SubagentKernelQuery:
    root_id: str = ""
    run_id: str = ""
    scope: str = "root_tree"
    include_refs: bool = True
    reserved: dict[str, object] = field(default_factory=dict)

# LLM: SubagentKernelRun is a compact refs-first status row for one subagent run.
# 类用途: 保存单个 run 的身份、状态、父子关系、工作区引用和恢复引用。
@dataclass(frozen=True)
class SubagentKernelRun:
    run_id: str = ""
    task_id: str = ""
    session_id: str = ""
    thread_id: str = ""
    root_id: str = ""
    root_run_id: str = ""
    parent_task_id: str = ""
    parent_id: str = ""
    parent_run_id: str = ""
    depth: int = 0
    agent_kind: str = ""
    role: str = ""
    agent_name: str = ""
    status: str = ""
    verification_status: str = ""
    failure_type: str = ""
    progress: float = 0.0
    current_step: str = ""
    current_tool: str = ""
    heartbeat_at: float = 0.0
    updated_at: float = 0.0
    last_progress_at: float = 0.0
    last_progress_summary: str = ""
    latest_summary: str = ""
    child_ids: list[str] = field(default_factory=list)
    address: dict[str, object] = field(default_factory=dict)
    task_envelope: dict[str, object] = field(default_factory=dict)
    workspace_refs: dict[str, str] = field(default_factory=dict)
    recovery_refs: dict[str, str] = field(default_factory=dict)
    tool_contract: dict[str, object] = field(default_factory=dict)
    artifact_refs: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    reserved: dict[str, object] = field(default_factory=dict)

# LLM: SubagentKernelSnapshot is the stable boundary between orchestration and status consumers.
# 类用途: 汇总一棵任务树或子树的状态桶和 run 列表。
@dataclass(frozen=True)
class SubagentKernelSnapshot:
    schema_version: str
    root_id: str = ""
    scope: str = ""
    runs: list[SubagentKernelRun] = field(default_factory=list)
    running_run_ids: list[str] = field(default_factory=list)
    blocked_run_ids: list[str] = field(default_factory=list)
    completed_run_ids: list[str] = field(default_factory=list)
    failed_run_ids: list[str] = field(default_factory=list)
    takeover_candidate_run_ids: list[str] = field(default_factory=list)
    source_refs: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    reserved: dict[str, object] = field(default_factory=dict)


__all__ = ["SubagentKernelQuery", "SubagentKernelRun", "SubagentKernelSnapshot"]
