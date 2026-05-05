from __future__ import annotations

"""LLM: build evidence presence and tool-requirement findings.

新手说明:
检查是否有可用验收证据、是否有失败证据，以及 acceptance_checks 要求的
read_file / write_file 工具是否已有对应证据。
"""

from ..models import SubAgentTask
from ..reports import AcceptanceReviewFinding


def _build_evidence_findings(
    task: SubAgentTask,
    created_at: float,
) -> list[AcceptanceReviewFinding]:
    """LLM: build evidence presence and tool-requirement findings.

    新手说明:
    检查是否有可用验收证据、是否有失败证据，以及 acceptance_checks 要求的
    read_file / write_file 工具是否已有对应证据。
    """

    findings: list[AcceptanceReviewFinding] = []

    ok_evidence = [item for item in task.evidence if item.ok]
    bad_evidence = [item for item in task.evidence if not item.ok]
    findings.append(
        AcceptanceReviewFinding(
            name="evidence_present",
            ok=bool(ok_evidence),
            severity="P0",
            message=(
                f"已有 {len(ok_evidence)} 条可用验收证据。"
                if ok_evidence
                else "缺少可用验收证据。"
            ),
            evidence_path=task.acceptance_file,
            created_at=created_at,
        )
    )
    findings.append(
        AcceptanceReviewFinding(
            name="evidence_not_failed",
            ok=not bad_evidence,
            severity="P1",
            message=(
                "没有失败验收证据。"
                if not bad_evidence
                else f"存在 {len(bad_evidence)} 条失败证据。"
            ),
            evidence_path=task.acceptance_file,
            created_at=created_at,
        )
    )

    acceptance_text = "；".join(task.acceptance_checks).lower()
    if "read_file" in acceptance_text:
        has_read = "read_file" in task.used_tools and any(
            item.ok and (
                item.kind in {"read_file", "file_read", "file_content"}
                or "read_file" in item.command.lower()
                or "read_file" in item.summary.lower()
            )
            for item in task.evidence
        )
        findings.append(
            AcceptanceReviewFinding(
                name="acceptance_requires_read_file",
                ok=has_read,
                severity="P0",
                message=(
                    "acceptance_checks 要求 read_file，且已有对应工具和证据。"
                    if has_read
                    else "acceptance_checks 要求 read_file，但缺少对应工具执行或证据。"
                ),
                evidence_path=task.acceptance_file,
                created_at=created_at,
            )
        )
    if "write_file" in acceptance_text:
        has_write = "write_file" in task.used_tools and any(
            item.ok and (
                item.kind in {"write_file", "file_write", "file_written"}
                or "write_file" in item.command.lower()
                or "write_file" in item.summary.lower()
                or "写入" in item.summary
            )
            for item in task.evidence
        )
        findings.append(
            AcceptanceReviewFinding(
                name="acceptance_requires_write_file",
                ok=has_write,
                severity="P0",
                message=(
                    "acceptance_checks 要求 write_file，且已有对应工具和证据。"
                    if has_write
                    else "acceptance_checks 要求 write_file，但缺少对应工具执行或证据。"
                ),
                evidence_path=task.acceptance_file,
                created_at=created_at,
            )
        )
    return findings
