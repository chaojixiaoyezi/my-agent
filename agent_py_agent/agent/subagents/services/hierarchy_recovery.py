# LLM: Hierarchy recovery builds refs-only handoff packets for nested subagent trees.
# 模块用途: 从 root run 汇总子/孙代理恢复线索，帮助父代理或接管代理快速判断哪里需要处理。

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from ..models import SubAgentTask

RECOVERY_STATUSES = frozenset({"BLOCKED", "FAILED", "TIMEOUT", "ERROR", "CHANNEL_ERROR"})


# LLM: HierarchyRecoveryRequest is the stable bundle for recovery tree queries.
# 类用途: 描述要恢复哪棵任务树、是否隐藏健康节点，以及请求者身份。
@dataclass(frozen=True)
class HierarchyRecoveryRequest:
    root_run_id: str
    requested_by: str = "parent"
    include_healthy: bool = True
    max_nodes: int = 200


# LLM: HierarchyRecoveryNode is a refs-only summary for one task in the tree.
# 类用途: 保存单个 run 的恢复摘要、父子关系、接管入口和推荐命令。
@dataclass(frozen=True)
class HierarchyRecoveryNode:
    run_id: str
    parent_id: str
    root_id: str
    depth: int
    role: str
    agent_name: str
    status: str
    current_step: str
    latest_summary: str
    child_ids: list[str]
    needs_recovery: bool
    recovery_reason: str
    takeover_readiness_ref: str = ""
    failure_handoff_ref: str = ""
    checkpoint_ref: str = ""
    recommended_command: str = ""
    artifact_refs: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)


# LLM: HierarchyRecoveryResult keeps complete tree metrics separate from returned nodes.
# 类用途: 返回恢复树摘要、候选节点和 omitted 健康节点数量。
@dataclass(frozen=True)
class HierarchyRecoveryResult:
    generated_at: float
    root_run_id: str
    requested_by: str
    node_count: int
    recovery_candidate_count: int
    omitted_healthy_count: int
    truncated: bool
    nodes: list[HierarchyRecoveryNode]
    recovery_candidates: list[HierarchyRecoveryNode]
    reserved: dict[str, object] = field(default_factory=dict)

    # LLM: to_dict gives CLI/tests a stable serialization hook.
    # 函数用途: 把恢复结果转换成 JSON 友好的 dict。
    def to_dict(self) -> dict[str, object]:
        return asdict(self)


# LLM: SubAgentHierarchyRecoveryService scans task metadata only and never expands artifacts.
# 类用途: 根据 parent/child refs 构造多层恢复包，不执行接管、不读正文。
class SubAgentHierarchyRecoveryService:
    """Build refs-only recovery packets for a nested subagent tree."""

    # LLM: __init__ stores the manager facade used for task loading.
    # 函数用途: 保存 manager 依赖；初始化不读写文件。
    def __init__(self, manager: Any) -> None:
        self.manager = manager

    # LLM: build_packet walks child_ids breadth-first and returns recovery candidates.
    # 函数用途: 生成 root 子树恢复包，支持隐藏健康节点以降低上下文体积。
    def build_packet(self, request: HierarchyRecoveryRequest) -> HierarchyRecoveryResult:
        scanned = _scan_tree(self.manager, request.root_run_id, max_nodes=request.max_nodes)
        candidates = [node for node in scanned if node.needs_recovery]
        visible = _visible_nodes(scanned, candidates, request)
        return HierarchyRecoveryResult(
            generated_at=time.time(),
            root_run_id=request.root_run_id,
            requested_by=request.requested_by,
            node_count=len(scanned),
            recovery_candidate_count=len(candidates),
            omitted_healthy_count=max(0, len(scanned) - len(visible)),
            truncated=len(scanned) >= max(1, request.max_nodes),
            nodes=visible,
            recovery_candidates=candidates,
            reserved={"reads_artifact_bodies": False, "auto_takeover": False, "schema_version": 1},
        )


# LLM: _scan_tree walks persisted child_ids in deterministic breadth-first order.
# 函数用途: 从 root 出发加载任务树摘要；节点缺失时跳过，不中断整个恢复包。
def _scan_tree(manager: Any, root_run_id: str, *, max_nodes: int) -> list[HierarchyRecoveryNode]:
    queue = [root_run_id]
    seen: set[str] = set()
    nodes: list[HierarchyRecoveryNode] = []
    limit = max(1, int(max_nodes or 1))
    while queue and len(nodes) < limit:
        run_id = queue.pop(0)
        if run_id in seen:
            continue
        seen.add(run_id)
        try:
            task = manager.load(run_id)
        except FileNotFoundError:
            continue
        nodes.append(_node_from_task(task))
        queue.extend(child_id for child_id in task.child_ids if child_id not in seen)
    return nodes


# LLM: _visible_nodes keeps root and candidates when healthy nodes are hidden.
# 函数用途: 控制恢复包体积，默认全量展示；隐藏健康节点时保留 root 和所有候选。
def _visible_nodes(
    scanned: list[HierarchyRecoveryNode],
    candidates: list[HierarchyRecoveryNode],
    request: HierarchyRecoveryRequest,
) -> list[HierarchyRecoveryNode]:
    if request.include_healthy:
        return scanned
    candidate_ids = {node.run_id for node in candidates}
    return [node for node in scanned if node.run_id == request.root_run_id or node.run_id in candidate_ids]


# LLM: _node_from_task converts one persisted task into a recovery summary.
# 函数用途: 提取 run 状态、refs、候选原因和建议命令，不读取 refs 指向的正文。
def _node_from_task(task: SubAgentTask) -> HierarchyRecoveryNode:
    reason = _recovery_reason(task)
    return HierarchyRecoveryNode(
        run_id=task.id,
        parent_id=task.parent_id,
        root_id=task.root_id or task.id,
        depth=int(task.depth or 0),
        role=task.role,
        agent_name=task.agent_name,
        status=task.status,
        current_step=task.current_step or task.status,
        latest_summary=task.latest_summary,
        child_ids=list(task.child_ids),
        needs_recovery=bool(reason),
        recovery_reason=reason,
        takeover_readiness_ref=task.takeover_readiness_json,
        failure_handoff_ref=task.failure_handoff_json,
        checkpoint_ref=task.agent_run_checkpoint_json or task.checkpoint_json or task.checkpoint_ref,
        recommended_command=_recommended_command(task, reason),
        artifact_refs=list(dict.fromkeys(task.artifact_refs)),
        evidence_refs=list(dict.fromkeys(task.evidence_refs)),
    )


# LLM: _recovery_reason classifies candidate runs without changing task state.
# 函数用途: 判断当前 run 是否需要恢复/接管，并返回机器可读原因。
def _recovery_reason(task: SubAgentTask) -> str:
    status = str(task.status or "").upper()
    if status in RECOVERY_STATUSES:
        return f"status:{status}"
    if task.failure_type:
        return f"failure_type:{task.failure_type}"
    if task.blockers:
        return "blockers_present"
    return ""


# LLM: _recommended_command points humans/parent automation to the existing controlled follow-up gate.
# 函数用途: 给需要恢复的节点生成下一步命令建议，不执行命令。
def _recommended_command(task: SubAgentTask, reason: str) -> str:
    if not reason:
        return ""
    return f"my-agent subagents-acceptance-plan {task.id} --followup"
