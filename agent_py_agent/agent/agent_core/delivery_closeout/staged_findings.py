"""Merge staged-checkpoint diagnostics into artifact closeout reports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...contracts.staged_checkpoint_acceptance import staged_checkpoint_findings


def with_staged_checkpoint_findings(
    report: dict[str, Any],
    item: dict[str, Any],
    workspace_root: Path,
) -> dict[str, Any]:
    """Attach staged-checkpoint diagnostics as warnings."""

    staged_findings = staged_checkpoint_findings([item], workspace_root)
    if not staged_findings:
        return report
    findings = report.get("findings")
    merged_findings = list(findings) if isinstance(findings, list) else []
    merged_findings.extend(_public_staged_finding(finding) for finding in staged_findings)
    updated = dict(report)
    updated["findings"] = merged_findings
    updated["ok"] = bool(report.get("ok"))
    return updated


def _public_staged_finding(finding: dict[str, object]) -> dict[str, str]:
    public_keys = {"code", "severity", "message", "location", "value"}
    details = {key: value for key, value in finding.items() if key not in public_keys}
    value = finding.get("value")
    if value is None and details:
        value = json.dumps(details, ensure_ascii=False, sort_keys=True)
    return {
        "code": str(finding.get("code") or ""),
        "severity": "warning",
        "message": str(finding.get("message") or ""),
        "location": str(finding.get("location") or ""),
        "value": str(value or ""),
    }
