# LLM: Hierarchy scheduler models keep the materializer thin as result fields grow.
# 模块用途: 放置层级调度的请求、结果和内部构建 bundle，避免 scheduler 主实现文件持续膨胀。

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import SubAgentTask
from .hierarchy_qa_scheduler import QaOrchestrationAdvice


# LLM: HierarchyChildSpec is the stable bundle for one planned descendant run.
# 类用途: 描述一个待创建的子/孙代理任务，避免用散乱 kwargs 扩展层级调度接口。
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


# LLM: HierarchyScheduleRequest is the only business entrypoint for hierarchy materialization.
# 类用途: 集中保存层级调度的 parent、候选子任务、限制和执行模式；0 表示不限制深度或直接孩子数量。
@dataclass(frozen=True)
class HierarchyScheduleRequest:
    parent_run_id: str
    child_specs: list[HierarchyChildSpec]
    apply: bool = False
    requested_by: str = "parent"
    max_children: int = 0
    max_depth: int = 0


# LLM: HierarchyScheduledItem reports either a planned or created child without loading large artifacts.
# 类用途: 返回单个调度条目摘要，给 CLI、测试和后续调度器读取。
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


# LLM: HierarchyScheduleResult keeps automatic behavior visible and conservative by default.
# 类用途: 返回层级调度结果、阻断原因和是否真正创建 run。
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
    # LLM: reused/dispatch ids make the next scheduling step machine-readable after idempotent reuse.
    # 参数说明: reused_run_ids 是本次复用的已有 child；dispatch_run_ids 是下一步仍适合启动 runner 的 child。
    reused_run_ids: list[str] = field(default_factory=list)
    dispatch_run_ids: list[str] = field(default_factory=list)
    manual_confirmation_required: bool = True
    automatic_execution_allowed: bool = False
    # LLM: quality_advice guides the model's next QA choice without creating a fixed workflow.
    quality_advice: QaOrchestrationAdvice | None = None
    scheduling_warnings: list[str] = field(default_factory=list)


# LLM: HierarchyCreateChildRequest separates child creation facts from the persistence call.
# 类用途: 保存创建一个 child run 所需的已推导字段，供参数组装 helper 使用。
@dataclass(frozen=True)
class HierarchyCreateChildRequest:
    parent: SubAgentTask
    spec: HierarchyChildSpec
    role: str
    goal: str
    extra_write_roots: list[str]
    sibling_index: int = 1


# LLM: HierarchyResultBuildRequest bundles shared result-rendering inputs to keep helpers small.
# 类用途: 组装 blocked/dry-run/apply 结果时复用 parent、request 和 LLM advice，避免 helper 参数继续膨胀。
@dataclass(frozen=True)
class HierarchyResultBuildRequest:
    parent: SubAgentTask
    request: HierarchyScheduleRequest
    quality_advice: QaOrchestrationAdvice | None = None
    scheduling_warnings: list[str] = field(default_factory=list)
