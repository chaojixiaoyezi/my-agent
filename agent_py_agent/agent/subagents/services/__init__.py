# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪和汇总子代理任务。

"""service objects behind the public SubAgentManager facade.

给人看的解释：
这个包承接从 manager mixin 中抽出的稳定职责。外部仍使用 SubAgentManager，
service 只在内部收口实现细节。
"""

from .actions import SubAgentActionService
from .base import SubAgentBaseService
from .board import SubAgentBoardService
from .dispatch import SubAgentDispatchService
from .indexing import SubAgentIndexingService
from .lifecycle import SubAgentLifecycleService
from .persistence import SubAgentPersistenceService
from .workflow import SubAgentWorkflowService

__all__ = [
    "SubAgentLifecycleService",
    "SubAgentPersistenceService",
    "SubAgentBaseService",
    "SubAgentBoardService",
    "SubAgentActionService",
    "SubAgentDispatchService",
    "SubAgentIndexingService",
    "SubAgentWorkflowService",
]
