# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""LLM contract: public dataclass exports for subagent state.

This module is intentionally kept as the compatibility surface. Concrete model
families live in narrower modules so new state can grow without turning this
file back into a catch-all.
LLM: keep external imports pointed here while moving concrete dataclasses out.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .execution_executor import TestExecutor

# LLM: TestExecutionRecord/TestExecutor stay exported from this facade so acceptance code can use the stable model path.
from .execution_records import TestExecutionRecord
from .model_capabilities import (
    CapabilityGap,
    CapabilityGrant,
    CapabilityRequest,
    VerificationEvidence,
)
from .model_records import (
    ChannelProbeCheck,
    ChannelProbeReport,
    ChannelProbeResult,
    TakeoverRecord,
    WorkOrderValidation,
)
from .model_runtime import (
    SubAgentExecutionContext,
    SubAgentParsedOutput,
    SubAgentRunnerResult,
)

# LLM: 通过稳定门面导出任务树控制面数据类，避免调用方绑定内部文件。
# LLM: RuntimeIdentity is re-exported here so callers keep using the stable subagent model facade.
from .model_task import (
    EvidencePacket,
    FailureHandoff,
    Finding,
    InheritanceManifest,
    LearningCandidate,
    RuntimeIdentity,
    SecuritySignal,
    StatusReport,
    SubAgentTask,
)
from .quality_models import ContextManifest, QualityContract


# LLM: TaskStatus 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 封装任务状态相关状态和行为，维持当前模块的职责边界；关键副作用: 方法可能触发任务状态、执行器结果、验收和报告展示相关副作用，需保持公开契约稳定。
class TaskStatus(str, Enum):
    """Subagent lifecycle status values."""

    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    BLOCKED = "BLOCKED"
    PAUSED = "PAUSED"
    ABANDONED = "ABANDONED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


DISPATCH_INELIGIBLE_STATUSES = frozenset({
    TaskStatus.PAUSED.value,
    TaskStatus.ABANDONED.value,
    TaskStatus.COMPLETED.value,
    TaskStatus.FAILED.value,
})


# LLM: SubAgentCard 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存subagentcard字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发任务状态、执行器结果、验收和报告展示相关副作用，需保持公开契约稳定。
@dataclass
class SubAgentCard:
    """Role/capability card describing what a subagent is allowed to do."""

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


# LLM: SubAgentCapabilityRouteOptions 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存subagent能力route选项字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class SubAgentCapabilityRouteOptions:
    """Bundle for capability-request routing options."""

    apply: bool = False
    run_ids: list[str] | None = None
    limit: int = 0


# LLM: SubAgentChannelProbeOptions 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存subagent通道probe选项字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class SubAgentChannelProbeOptions:
    """Bundle for channel probe selection options."""

    run_ids: list[str] | None = None
    limit: int = 0


# LLM: SubAgentDueCheckOptions carries report scope and capability thresholds without widening CLI signatures.
# 类用途: 集中保存 subagent 到期检查选项字段，可按 root_id 限定一棵任务树，避免多场景共用 workspace 时互相污染。
@dataclass(frozen=True)
class SubAgentDueCheckOptions:
    """Bundle for due-check report options."""

    config: Any | None = None
    write_report: bool = False
    root_id: str = ""
    include_run_ids: list[str] = field(default_factory=list)
    exclude_run_ids: list[str] = field(default_factory=list)


# LLM: SubAgentPlanActionsOptions keeps action-plan scope explicit without widening CLI/service calls.
# 类用途: 集中保存 subagent 动作计划选项；root_id 用来只根据一棵任务树的问题生成建议动作。
@dataclass(frozen=True)
class SubAgentPlanActionsOptions:
    """Bundle for dry-run action-plan options."""

    config: Any | None = None
    write_report: bool = False
    root_id: str = ""
    include_run_ids: list[str] = field(default_factory=list)
    exclude_run_ids: list[str] = field(default_factory=list)


# LLM: SubAgentLeadershipRecoveryPlanOptions keeps batch leader handoff planning read-only and scoped.
# 类用途: 集中保存批量领导权恢复计划参数；只生成 dry-run 分配建议，不修改任务树。
@dataclass(frozen=True)
class SubAgentLeadershipRecoveryPlanOptions:
    """Bundle for dry-run batch coordinator leadership recovery planning."""

    config: Any | None = None
    write_report: bool = False
    root_id: str = ""
    leader_ids: list[str] = field(default_factory=list)
    max_children_per_leader: int = 3


# LLM: SubAgentLeadershipRecoveryApplyOptions keeps subset handoff apply explicit and auditable.
# 类用途: 集中保存分批 leadership recovery apply 参数；只有 apply=True 才真正重挂指定 child 子集。
@dataclass(frozen=True)
class SubAgentLeadershipRecoveryApplyOptions:
    """Bundle for controlled subset coordinator leadership recovery apply."""

    root_id: str = ""
    coordinator_id: str = ""
    leader_id: str = ""
    child_ids: list[str] = field(default_factory=list)
    apply: bool = False
    max_children_per_leader: int = 0


# LLM: SubAgentBoardOptions 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存subagent看板选项字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class SubAgentBoardOptions:
    """Bundle for board rendering and recent-list selection."""

    recent_limit: int = 20
    # LLM: Lightweight status/startup paths can skip tree expansion while full boards keep child rollups.
    include_child_status_counts: bool = True


__all__ = [
    "CapabilityGap",
    "CapabilityGrant",
    "CapabilityRequest",
    "ChannelProbeCheck",
    "ChannelProbeReport",
    "ChannelProbeResult",
    "ContextManifest",
    "DISPATCH_INELIGIBLE_STATUSES",
    "EvidencePacket",
    "FailureHandoff",
    "Finding",
    "InheritanceManifest",
    "LearningCandidate",
    "QualityContract",
    "RuntimeIdentity",
    "SecuritySignal",
    "SubAgentCard",
    "SubAgentBoardOptions",
    "SubAgentCapabilityRouteOptions",
    "SubAgentChannelProbeOptions",
    "SubAgentDueCheckOptions",
    "SubAgentLeadershipRecoveryApplyOptions",
    "SubAgentLeadershipRecoveryPlanOptions",
    "SubAgentPlanActionsOptions",
    "SubAgentExecutionContext",
    "SubAgentParsedOutput",
    "SubAgentRunnerResult",
    "SubAgentTask",
    "StatusReport",
    "TakeoverRecord",
    "TestExecutor",
    "TestExecutionRecord",
    "TaskStatus",
    "VerificationEvidence",
    "WorkOrderValidation",
]
