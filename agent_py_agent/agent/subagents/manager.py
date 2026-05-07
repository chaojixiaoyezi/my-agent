from __future__ import annotations

"""LLM contract: compose focused subagent manager mixins into the public manager class.

Human version:
SubAgentManager 仍然是外部代码使用的入口，但具体能力已经分散到按职责命名的 mixin。
它本身不写业务逻辑，只组合那些已经按职责拆开的能力。
"""

from .manager_acceptance import SubAgentAcceptanceMixin
from .manager_acceptance_findings import SubAgentAcceptanceFindingMixin
from .manager_actions import SubAgentActionMixin
from .manager_base import SubAgentBaseMixin
from .manager_board import SubAgentBoardMixin
from .manager_capabilities import SubAgentCapabilityMixin
from .manager_channel_probe import SubAgentChannelProbeMixin
from .manager_dispatch import SubAgentDispatchMixin
from .manager_indexing import SubAgentIndexingMixin
from .manager_learning import SubAgentLearningMixin
from .manager_lifecycle import SubAgentLifecycleMixin
from .manager_memory_gate import SubAgentMemoryGateMixin
from .manager_patch import SubAgentPatchMixin
from .manager_runner_context import SubAgentRunnerContextMixin
from .manager_runner_results import SubAgentRunnerResultMixin
from .manager_workflow import SubAgentWorkflowMixin

# LLM: memory gate review is separate from learning drafts and never promotes by itself.


class SubAgentManager(
    SubAgentBaseMixin,
    SubAgentLifecycleMixin,
    SubAgentBoardMixin,
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
    SubAgentWorkflowMixin,
):
    """LLM contract: public subagent orchestration facade composed from focused mixins.

    Human version:
    外部还是用 `SubAgentManager`，不用知道底下拆成多少文件。
    它本身不写业务逻辑，只组合那些已经按职责拆开的能力。
    """

    def __init__(
        self,
        workspace,
        local_store=None,
        workspace_root=None,
        workspace_roots=None,
        enable_self_learning=False,
    ):
        super().__init__(
            workspace,
            local_store=local_store,
            workspace_root=workspace_root,
            workspace_roots=workspace_roots,
            enable_self_learning=enable_self_learning,
        )
        self._init_patch_services()
