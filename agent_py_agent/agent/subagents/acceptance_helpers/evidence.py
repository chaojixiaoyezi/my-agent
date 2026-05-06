from __future__ import annotations

"""LLM: build evidence presence and tool-requirement findings.

新手说明:
检查是否有可用验收证据、是否有失败证据，以及 acceptance_checks 要求的
read_file / write_file 工具是否已有对应证据。
"""

from ..models import SubAgentTask
from ..reports import AcceptanceReviewFinding
from .evidence_acceptance_findings import (
    _has_tool_evidence,
    _make_finding,
    build_evidence_findings,
)

__all__ = ["_build_evidence_findings", "_has_tool_evidence", "_make_finding"]


def _build_evidence_findings(
    task: SubAgentTask,
    created_at: float,
) -> list[AcceptanceReviewFinding]:
    """LLM: build evidence presence and tool-requirement findings.

    新手说明:
    检查是否有可用验收证据、是否有失败证据，以及 acceptance_checks 要求的
    read_file / write_file 工具是否已有对应证据。
    """
    # LLM: keep the historical import path while focused helpers own the real logic.
    return build_evidence_findings(task, created_at)
