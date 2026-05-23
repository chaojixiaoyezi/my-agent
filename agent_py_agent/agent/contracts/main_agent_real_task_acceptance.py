# LLM: Real-task acceptance is a compatibility facade over unified task acceptance.
# 模块用途: 保留 real_task 导入名；产物验收、runtime findings 和恢复建议只维护一套实现。

from __future__ import annotations

from .main_agent_task_acceptance import (
    TaskRunAcceptanceReport as RealTaskAcceptanceReport,
)
from .main_agent_task_acceptance import (
    TaskRunAcceptanceRequest as RealTaskAcceptanceRequest,
)
from .main_agent_task_acceptance import (
    TaskRunArtifactAcceptance as RealTaskArtifactAcceptance,
)
from .main_agent_task_acceptance import (
    validate_task_artifacts,
)


# LLM: validate_real_task_artifacts delegates to the unified acceptance engine.
# 函数用途: 让 real_task 与 task 轨道经过同一产物和运行时合同验收。
def validate_real_task_artifacts(
    request: RealTaskAcceptanceRequest,
) -> RealTaskAcceptanceReport:
    return validate_task_artifacts(request)


__all__ = [
    "RealTaskAcceptanceReport",
    "RealTaskAcceptanceRequest",
    "RealTaskArtifactAcceptance",
    "validate_real_task_artifacts",
]
