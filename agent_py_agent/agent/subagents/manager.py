from __future__ import annotations

"""LLM contract: compose focused subagent manager mixins into the public manager class.

Human version:
SubAgentManager 仍然是外部代码使用的入口，但具体能力已经分散到按职责命名的 mixin。
这不是为了凑行数，而是让看板、验收、runner、能力路由、索引这些变化能各自维护。
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
from .manager_patch import SubAgentPatchMixin
from .manager_runner_context import SubAgentRunnerContextMixin
from .manager_runner_results import SubAgentRunnerResultMixin


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
    SubAgentIndexingMixin,
):
    """LLM contract: public subagent orchestration facade composed from focused mixins.

    Human version:
    外部还是用 `SubAgentManager`，不用知道底下拆成多少文件。
    它本身不写业务逻辑，只组合那些已经按职责拆开的能力。
    """

    pass
