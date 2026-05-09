# LLM: Hierarchy recovery builds refs-only handoff packets for nested subagent trees.
# 模块用途: 从 root run 汇总子/孙代理恢复线索，帮助父代理或接管代理快速判断哪里需要处理。

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from ..models import SubAgentTask
from ..policies import _is_active

RECOVERY_STATUSES = frozenset({"BLOCKED", "FAILED", "TIMEOUT", "ERROR", "CHANNEL_ERROR"})


# LLM: HierarchyRecoveryRequest is the stable bundle for recovery tree queries.
# 类用途: 描述要恢复哪棵任务树、是否隐藏健康节点，以及请求者身份。
@dataclass(frozen=True)
class HierarchyRecoveryRequest:
    root_run_id: str
    requested_by: str = "parent"
    include_healthy: bool = True
    max_nodes: int = 200
    heartbeat_timeout: float = 0.0
    run_timeout: float = 0.0
    now: float = 0.0


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
        scanned = _scan_tree(self.manager, request)
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
def _scan_tree(manager: Any, request: HierarchyRecoveryRequest) -> list[HierarchyRecoveryNode]:
    tasks = _load_tree_tasks(manager, request)
    task_index = {task.id: task for task in tasks}
    return [_node_from_task(task, request, task_index=task_index) for task in tasks]


# LLM: _load_tree_tasks collects task metadata before recovery classification so child checks can inspect parents.
# 函数用途: 按广度优先加载 root 子树任务对象；缺失节点跳过，不读取 artifact 正文。
def _load_tree_tasks(manager: Any, request: HierarchyRecoveryRequest) -> list[SubAgentTask]:
    queue = [request.root_run_id]
    seen: set[str] = set()
    tasks: list[SubAgentTask] = []
    limit = max(1, int(request.max_nodes or 1))
    while queue and len(tasks) < limit:
        run_id = queue.pop(0)
        if run_id in seen:
            continue
        seen.add(run_id)
        try:
            task = manager.load(run_id)
        except FileNotFoundError:
            continue
        tasks.append(task)
        queue.extend(child_id for child_id in task.child_ids if child_id not in seen)
    return tasks


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
# 函数用途: 提取 run 状态、refs、候选原因、过期运行线索和建议命令，不读取 refs 指向的正文。
def _node_from_task(
    task: SubAgentTask,
    request: HierarchyRecoveryRequest,
    *,
    task_index: dict[str, SubAgentTask] | None = None,
) -> HierarchyRecoveryNode:
    reason = _recovery_reason(task, request, task_index=task_index)
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
# 函数用途: 判断当前 run 是否需要恢复/接管；除终态失败外，也识别 RUNNING 心跳停滞或运行超时。
def _recovery_reason(
    task: SubAgentTask,
    request: HierarchyRecoveryRequest,
    *,
    task_index: dict[str, SubAgentTask] | None = None,
) -> str:
    status = str(task.status or "").upper()
    if status in RECOVERY_STATUSES:
        return f"status:{status}"
    if task.failure_type:
        return f"failure_type:{task.failure_type}"
    if task.blockers:
        return "blockers_present"
    parent_timeout = _parent_timeout_child_reason(task, task_index)
    if parent_timeout:
        return parent_timeout
    stale_reasons = _active_stale_reasons(task, request)
    if stale_reasons:
        return f"due:{','.join(stale_reasons)}"
    return ""


# LLM: _parent_timeout_child_reason exposes unfinished children when their parent runner timed out.
# 函数用途: 父节点已 TIMEOUT 且当前子任务未收口时，把子任务列为恢复候选。
def _parent_timeout_child_reason(
    task: SubAgentTask,
    task_index: dict[str, SubAgentTask] | None,
) -> str:
    if _child_is_closed_for_parent_timeout(task):
        return ""
    parent = (task_index or {}).get(str(task.parent_id or ""))
    if parent is None or str(parent.status or "").upper() != "TIMEOUT":
        return ""
    return f"parent_timeout_unfinished_child:{parent.id}"


# LLM: _child_is_closed_for_parent_timeout keeps verified completion and prior takeover out of recovery noise.
# 函数用途: 判断父超时后当前子任务是否已经安全收口，避免 hide-healthy 输出过多。
def _child_is_closed_for_parent_timeout(task: SubAgentTask) -> bool:
    status = str(task.status or "").upper()
    verification = str(task.verification_status or "").upper()
    if status == "DONE" and verification == "VERIFIED":
        return True
    return status in {"TAKEN_OVER", "ABANDONED"}


# LLM: _recommended_command points humans/parent automation to the existing controlled follow-up gate.
# 函数用途: 给需要恢复的节点生成下一步命令建议，不执行命令。
def _recommended_command(task: SubAgentTask, reason: str) -> str:
    if not reason:
        return ""
    if reason.startswith("parent_timeout_unfinished_child:"):
        root_id = task.root_id or task.id
        return f"my-agent subagents-recovery-tree {root_id} --hide-healthy"
    if reason.startswith("due:"):
        return (
            "my-agent subagents-apply-actions --apply --action takeover_or_reassign "
            f"--run-id {task.id} --take-over-by <agent>"
        )
    return f"my-agent subagents-acceptance-plan {task.id} --followup"


# LLM: _active_stale_reasons mirrors due-check timeout signals for recovery-tree visibility.
# 函数用途: 对仍处于活动状态的 run 计算 heartbeat/run timeout 原因，不修改任务状态。
def _active_stale_reasons(task: SubAgentTask, request: HierarchyRecoveryRequest) -> list[str]:
    status = str(task.status or "").upper()
    has_active_attempt = bool(str(task.runner_active_attempt_id or "").strip())
    if not (_is_active(status) and (status == "RUNNING" or has_active_attempt)):
        return []
    now = float(request.now or time.time())
    reasons: list[str] = []
    stale_seconds = max(0.0, now - float(task.heartbeat_at or task.updated_at or now))
    age_seconds = max(0.0, now - float(task.created_at or now))
    if request.heartbeat_timeout > 0 and stale_seconds > request.heartbeat_timeout:
        reasons.append("heartbeat_stale")
    if request.run_timeout > 0 and age_seconds > request.run_timeout:
        reasons.append("run_timeout")
    return reasons
