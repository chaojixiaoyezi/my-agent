
"""service objects composed by SubAgentManager.

这个包承接从 manager mixin 中抽出的稳定职责。外部仍使用 SubAgentManager，
service 只在内部收口实现细节。
"""

from .actions import SubAgentActionService
from .base import SubAgentBaseService
from .board import SubAgentBoardService
from .capabilities import SubAgentCapabilityService
from .dispatch import SubAgentDispatchService
from .indexing import SubAgentIndexingService
from .lifecycle import SubAgentLifecycleService
from .persistence import SubAgentPersistenceService
from .runner_context import SubAgentRunnerContextService
from .runner_result import SubAgentRunnerResultService
from .workflow import SubAgentWorkflowService

__all__ = [
    "SubAgentLifecycleService",
    "SubAgentPersistenceService",
    "SubAgentBaseService",
    "SubAgentBoardService",
    "SubAgentActionService",
    "SubAgentCapabilityService",
    "SubAgentDispatchService",
    "SubAgentIndexingService",
    "SubAgentRunnerContextService",
    "SubAgentRunnerResultService",
    "SubAgentWorkflowService",
]
