# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""LLM contract: compose focused subagent manager mixins into the public manager class.

Human version:
SubAgentManager 仍然是外部代码使用的入口，但具体能力已经分散到按职责命名的 mixin。
它本身不写业务逻辑，只组合那些已经按职责拆开的能力。
"""

from .manager_acceptance import SubAgentAcceptanceMixin
from .manager_acceptance_findings import SubAgentAcceptanceFindingMixin
from .manager_actions import SubAgentActionMixin
from .manager_base import SubAgentBaseMixin, SubAgentManagerInitParams
from .manager_board import SubAgentBoardMixin
from .manager_budget import SubAgentBudgetMixin
from .manager_capabilities import SubAgentCapabilityMixin
from .manager_channel_probe import SubAgentChannelProbeMixin
from .manager_dispatch import SubAgentDispatchMixin

# LLM: hierarchy mixin owns explicit child/grandchild scheduling and refs-only recovery queries.
from .manager_hierarchy import SubAgentHierarchyMixin
from .manager_indexing import SubAgentIndexingMixin
from .manager_learning import SubAgentLearningMixin
from .manager_lifecycle import SubAgentLifecycleMixin
from .manager_memory_gate import SubAgentMemoryGateMixin
from .manager_patch import SubAgentPatchMixin
from .manager_runner_context import SubAgentRunnerContextMixin
from .manager_runner_results import SubAgentRunnerResultMixin
from .manager_workflow import SubAgentWorkflowMixin

# LLM: memory gate review is separate from learning drafts and never promotes by itself.


# LLM: SubAgentManager 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 协调subagent管理器的下游服务和持久化入口，对外维持稳定管理接口；关键副作用: 方法可能触发任务状态、执行器结果、验收和报告展示相关副作用，需保持公开契约稳定。
class SubAgentManager(
    SubAgentBaseMixin,
    SubAgentLifecycleMixin,
    SubAgentBoardMixin,
    SubAgentBudgetMixin,
    SubAgentActionMixin,
    SubAgentCapabilityMixin,
    SubAgentAcceptanceMixin,
    SubAgentPatchMixin,
    SubAgentDispatchMixin,
    SubAgentAcceptanceFindingMixin,
    SubAgentRunnerContextMixin,
    SubAgentRunnerResultMixin,
    SubAgentChannelProbeMixin,
    SubAgentLearningMixin,
    SubAgentMemoryGateMixin,
    SubAgentIndexingMixin,
    SubAgentHierarchyMixin,
    SubAgentWorkflowMixin,
):
    """LLM contract: public subagent orchestration facade composed from focused mixins.

    Human version:
    外部还是用 `SubAgentManager`，不用知道底下拆成多少文件。
    它本身不写业务逻辑，只组合那些已经按职责拆开的能力。
    """

    # LLM: __init__ 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def __init__(
        self,
        workspace,
        *,
        params: SubAgentManagerInitParams | None = None,
        local_store=None,
        workspace_root=None,
        workspace_roots=None,
        enable_self_learning=False,
        debug_trace_level=0,
    ):
        # LLM: debug_trace_level is an internal observability switch; level 0 must keep persistence silent.
        params = params or SubAgentManagerInitParams(
            local_store=local_store,
            workspace_root=workspace_root,
            workspace_roots=workspace_roots,
            enable_self_learning=enable_self_learning,
            debug_trace_level=debug_trace_level,
        )
        super().__init__(
            workspace,
            params=params,
        )
        self._init_patch_services()
