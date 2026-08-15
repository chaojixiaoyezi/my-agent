
from __future__ import annotations

from dataclasses import dataclass, field

from ...models import SubAgentTask
from .qa_scheduler import QaOrchestrationAdvice


@dataclass(frozen=True)
class HierarchyChildSpec:
    goal: str
    agent_name: str = "worker"
    role: str = "worker"
    thought: str = ""
    plan: list[str] = field(default_factory=list)
    allowed_skills: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    acceptance_checks: list[str] = field(default_factory=list)
    extra_write_roots: list[str] = field(default_factory=list)
    # context_manifest/context_packs 直接写入 child task，用于 repair/execute/verify 同 run 闭环。
    context_manifest: dict[str, object] = field(default_factory=dict)
    context_packs: list[dict[str, object]] = field(default_factory=list)
    # 运行期事实放在 attributes；goal 只给模型阅读，不作为代码层判断依据。
    attributes: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class HierarchyScheduleRequest:
    parent_run_id: str
    child_specs: list[HierarchyChildSpec]
    apply: bool = False
    requested_by: str = "parent"
    max_children: int = 0
    max_depth: int = 0


@dataclass(frozen=True)
class HierarchyScheduledItem:
    run_id: str
    parent_id: str
    root_id: str
    depth: int
    role: str
    agent_name: str
    goal: str
    created: bool
    reason: str = ""


@dataclass(frozen=True)
class HierarchyScheduleResult:
    generated_at: float
    parent_run_id: str
    root_id: str
    dry_run: bool
    blocked: bool
    reason: str
    requested_by: str
    planned_count: int
    created_run_ids: list[str]
    items: list[HierarchyScheduledItem]
    # reused_run_ids 是本次复用的已有 child；dispatch_run_ids 是下一步仍适合启动 runner 的 child。
    reused_run_ids: list[str] = field(default_factory=list)
    dispatch_run_ids: list[str] = field(default_factory=list)
    manual_confirmation_required: bool = True
    automatic_execution_allowed: bool = False
    quality_advice: QaOrchestrationAdvice | None = None


@dataclass(frozen=True)
class HierarchyCreateChildRequest:
    parent: SubAgentTask
    spec: HierarchyChildSpec
    role: str
    goal: str
    extra_write_roots: list[str]
    sibling_index: int = 1


@dataclass(frozen=True)
class HierarchyResultBuildRequest:
    parent: SubAgentTask
    request: HierarchyScheduleRequest
    quality_advice: QaOrchestrationAdvice | None = None
