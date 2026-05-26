# LLM: Delivery recovery prompting keeps continuation hints derived from structured recovery findings only.
# 模块用途: 渲染 delivery_contract.recovery / recovery_reconciliation 里的机器恢复事实，不让系统回读 stdout 或自然语言摘要。

from .delivery_contract_prompting_staged import (
    _staged_json_invalid_lines,
    _staged_json_no_rows_lines,
)


# LLM: render_recovery_guidance_lines renders machine recovery findings without parsing prior stdout prose.
# 函数用途: 将 recovery.acceptance.runtime_findings 里的结构化恢复事实展示给模型；不再渲染旧的分块写入 session。
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


# LLM: _runtime_findings extracts only structured recovery findings from the delivery contract.
# 函数用途: 读取 recovery.acceptance.runtime_findings；不读取 stdout、stderr 或自然语言摘要。
def _runtime_findings(contract: dict[str, object]) -> list[dict[str, object]]:
    recovery = contract.get("recovery")
    acceptance = recovery.get("acceptance") if isinstance(recovery, dict) else None
    findings = acceptance.get("runtime_findings") if isinstance(acceptance, dict) else None
    if not isinstance(findings, list):
        return []
    return [dict(item) for item in findings if isinstance(item, dict)]


# LLM: _reconciliation_guidance_lines renders system-side duplicate-session cleanup facts.
# 函数用途: 展示恢复前已保留/已 abort 的 session，不让模型继续使用旧恢复包里的过期 session。
# LLM: _one_runtime_finding_lines keeps each runtime finding compact and action-oriented.
# 函数用途: 根据结构化 code 渲染单条恢复提示；未知 code 只展示 code/location，不做自然语言推断。
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
