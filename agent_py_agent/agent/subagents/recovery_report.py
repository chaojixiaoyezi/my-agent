
from __future__ import annotations

"""Recovery report writer for subagent runner recovery."""

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class RecoveryReportRequest:
    task_dir: str | Path
    run_id: str
    title: str
    summary: str
    details: str = ""
    reason: str = ""
    artifact_refs: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)


@dataclass
class RecoveryReportResult:
    written: bool
    markdown_ref: str = ""
    json_ref: str = ""
    blockers: list[str] = field(default_factory=list)


def write_recovery_report(request: RecoveryReportRequest) -> RecoveryReportResult:
    task_dir = Path(request.task_dir).expanduser().resolve()
    if not str(request.run_id).strip():
        return RecoveryReportResult(False, blockers=["missing_run_id"])
    try:
        reports = task_dir / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        markdown = reports / "recovery_report.md"
        payload = _recovery_payload(request)
        markdown.write_text(_render_recovery_markdown(payload), encoding="utf-8")
        json_ref = reports / "recovery_report.json"
        json_ref.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        return RecoveryReportResult(False, blockers=[f"write_failed:{exc}"])
    return RecoveryReportResult(True, str(markdown), str(json_ref))


def _recovery_payload(request: RecoveryReportRequest) -> dict[str, object]:
    return {
        "run_id": request.run_id,
        "title": request.title,
        "summary": request.summary,
        "details": request.details,
        "reason": request.reason,
        "artifact_refs": list(request.artifact_refs),
        "evidence_refs": list(request.evidence_refs),
        "created_at": time.time(),
    }


def _render_recovery_markdown(payload: dict[str, object]) -> str:
    lines = [
        f"# {payload['title'] or 'Recovery Report'}",
        "",
        f"- run_id: {payload['run_id']}",
        f"- reason: {payload['reason'] or 'recovery'}",
        "",
        "## Summary",
        str(payload["summary"]),
    ]
    if payload.get("details"):
        lines.extend(["", "## Details", str(payload["details"])])
    lines.extend(["", "## Refs"])
    for ref in payload.get("artifact_refs", []):
        lines.append(f"- artifact: {ref}")
    for ref in payload.get("evidence_refs", []):
        lines.append(f"- evidence: {ref}")
    return "\n".join(lines).rstrip() + "\n"


def recovery_result_to_dict(result: RecoveryReportResult) -> dict[str, object]:
    return asdict(result)
