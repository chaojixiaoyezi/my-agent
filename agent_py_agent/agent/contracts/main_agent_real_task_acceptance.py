
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
