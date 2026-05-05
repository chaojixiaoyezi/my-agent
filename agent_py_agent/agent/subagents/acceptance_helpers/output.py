from __future__ import annotations

"""LLM: build capability-gap, blocker, test, artifact, and patch findings.

新手说明:
检查是否还有未关闭的能力请求/缺口、output.json 里的 blocker 和测试失败、
artifact 路径是否真实存在、以及 patch 的状态和审核情况。
"""

from collections.abc import Callable

from ..models import SubAgentTask
from ..reports import AcceptanceReviewFinding
from .artifacts import _build_test_and_artifact_findings
from .capabilities import _build_capability_and_blocker_findings
from .patches import _build_patch_findings


def _build_output_and_capability_findings(
    task: SubAgentTask,
    output: dict[str, object],
    created_at: float,
    artifact_exists_fn: Callable[[str], bool],
) -> list[AcceptanceReviewFinding]:
    """LLM: build capability-gap, blocker, test, artifact, and patch findings.

    新手说明:
    检查是否还有未关闭的能力请求/缺口、output.json 里的 blocker 和测试失败、
    artifact 路径是否真实存在、以及 patch 的状态和审核情况。
    """

    findings: list[AcceptanceReviewFinding] = []
    findings.extend(_build_capability_and_blocker_findings(task, output, created_at))
    findings.extend(_build_test_and_artifact_findings(task, output, created_at, artifact_exists_fn))
    findings.extend(_build_patch_findings(task, output, created_at))
    return findings
