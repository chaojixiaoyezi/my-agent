from __future__ import annotations

"""Helpers for evidence-related acceptance review findings."""

from ..models import SubAgentTask
from ..reports import AcceptanceReviewFinding


def _make_finding(
    name: str,
    ok: bool,
    severity: str,
    message: str,
    evidence_path: str,
    created_at: float,
) -> AcceptanceReviewFinding:
    """Create a single AcceptanceReviewFinding."""
    return AcceptanceReviewFinding(
        name=name,
        ok=ok,
        severity=severity,
        message=message,
        evidence_path=evidence_path,
        created_at=created_at,
    )


def _has_tool_evidence(
    tool_name: str,
    kind_aliases: set[str],
    extra_summary_keywords: list[str],
    used_tools: list[str],
    evidence: list,
) -> bool:
    """Check if tool has evidence in the task evidence list."""
    if tool_name not in used_tools:
        return False
    return any(
        item.ok and (
            item.kind in kind_aliases
            or tool_name in item.command.lower()
            or tool_name in item.summary.lower()
            or any(kw in item.summary for kw in extra_summary_keywords)
        )
        for item in evidence
    )


def build_evidence_findings(
    task: SubAgentTask,
    created_at: float,
) -> list[AcceptanceReviewFinding]:
    """Build all evidence acceptance findings for a task."""
    findings = _build_presence_findings(task, created_at)
    findings.extend(_build_tool_requirement_findings(task, created_at))
    return findings


def _build_presence_findings(
    task: SubAgentTask,
    created_at: float,
) -> list[AcceptanceReviewFinding]:
    ok_evidence = [item for item in task.evidence if item.ok]
    bad_evidence = [item for item in task.evidence if not item.ok]

    return [
        _make_finding(
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
        ),
        _make_finding(
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
        ),
    ]


def _build_tool_requirement_findings(
    task: SubAgentTask,
    created_at: float,
) -> list[AcceptanceReviewFinding]:
    findings: list[AcceptanceReviewFinding] = []
    acceptance_text = "；".join(task.acceptance_checks).lower()

    if "read_file" in acceptance_text:
        findings.append(
            _build_read_file_requirement_finding(task, created_at)
        )
    if "write_file" in acceptance_text:
        findings.append(
            _build_write_file_requirement_finding(task, created_at)
        )

    return findings


def _build_read_file_requirement_finding(
    task: SubAgentTask,
    created_at: float,
) -> AcceptanceReviewFinding:
    has_read = _has_tool_evidence(
        "read_file",
        {"read_file", "file_read", "file_content"},
        [],
        task.used_tools,
        task.evidence,
    )
    return _make_finding(
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


def _build_write_file_requirement_finding(
    task: SubAgentTask,
    created_at: float,
) -> AcceptanceReviewFinding:
    has_write = _has_tool_evidence(
        "write_file",
        {"write_file", "file_write", "file_written"},
        ["写入"],
        task.used_tools,
        task.evidence,
    )
    return _make_finding(
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
