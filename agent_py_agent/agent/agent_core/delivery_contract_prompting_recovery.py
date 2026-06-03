
from .delivery_contract_prompting_staged import (
    _staged_json_invalid_lines,
    _staged_json_no_rows_lines,
)


def render_recovery_guidance_lines(
    contract: dict[str, object], artifact_items: list[dict[str, object]]
) -> list[str]:
    findings = _runtime_findings(contract)
    if not findings:
        return []
    lines = ["恢复要求："]
    for finding in findings:
        lines.extend(_one_runtime_finding_lines(finding, artifact_items))
    return lines


def _runtime_findings(contract: dict[str, object]) -> list[dict[str, object]]:
    recovery = contract.get("recovery")
    acceptance = recovery.get("acceptance") if isinstance(recovery, dict) else None
    findings = acceptance.get("runtime_findings") if isinstance(acceptance, dict) else None
    if not isinstance(findings, list):
        return []
    return [dict(item) for item in findings if isinstance(item, dict)]


def _one_runtime_finding_lines(
    finding: dict[str, object], artifact_items: list[dict[str, object]]
) -> list[str]:
    code = str(finding.get("code") or "").strip()
    if code == "STAGED_JSON_INVALID":
        return _staged_json_invalid_lines(finding, artifact_items)
    if code == "STAGED_JSON_NO_ROWS":
        return _staged_json_no_rows_lines(finding, artifact_items)
    location = str(finding.get("location") or finding.get("stage_ref") or "").strip()
    return [f"- runtime_finding={code or '<unknown>'} location={location or '<none>'}"]


__all__ = ["render_recovery_guidance_lines"]
