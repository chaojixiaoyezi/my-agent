# LLM: Runtime report gates validate acceptance, recovery snapshots, and audit records.
# 模块用途: 校验完成收口、恢复回放和工具审计的结构化字段，让 replay 不依赖自然语言摘要。

from __future__ import annotations

from typing import Any

from .models import GateDecision, GateFinding


# LLM: evaluate_acceptance_closeout_gate requires verified runtime gate facts before DONE.
# 函数用途: 完成入口必须带 verification_status 和 runtime_gate，不能只凭 final_status 成功。
def evaluate_acceptance_closeout_gate(report: dict[str, Any]) -> GateDecision:
    status = str(report.get("final_status") or report.get("status") or "").strip().upper()
    verification = str(report.get("verification_status") or "").strip().upper()
    if status in {"DONE", "SUCCEEDED", "VERIFIED"} and verification not in {"PASSED", "VERIFIED"}:
        return GateDecision.deny(
            "acceptance_closeout",
            "ACCEPTANCE_VERIFICATION_MISSING",
            evidence={"final_status": status, "verification_status": verification},
        )
    runtime_gate = report.get("runtime_gate")
    if not isinstance(runtime_gate, dict):
        return GateDecision.deny("acceptance_closeout", "ACCEPTANCE_RUNTIME_GATE_MISSING")
    if runtime_gate.get("allowed") is not True:
        findings = gate_payload_findings(runtime_gate, fallback="ACCEPTANCE_RUNTIME_GATE_FAILED")
        return GateDecision.repair("acceptance_closeout", findings, evidence={"final_status": status})
    return GateDecision.allow("acceptance_closeout", evidence={"final_status": status, "verification_status": verification})


# LLM: evaluate_runtime_audit_gate confirms replay records include gate and parameters.
# 函数用途: 工具审计记录必须带 runtime_gate 和 parameters，否则恢复链路进入 RECOVERING。
def evaluate_runtime_audit_gate(records: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> GateDecision:
    findings: list[GateFinding] = []
    if not isinstance(records, (list, tuple)):
        return GateDecision.deny("runtime_audit", "AUDIT_RECORDS_MISSING")
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            findings.append(GateFinding("AUDIT_RECORD_INVALID", evidence={"index": index}))
            continue
        if is_tool_audit_record(record):
            validate_tool_audit_record(index, record, findings)
    if findings:
        return GateDecision.recovering("runtime_audit", findings, evidence={"record_count": len(records)})
    return GateDecision.allow("runtime_audit", evidence={"record_count": len(records)})


# LLM: evaluate_recovery_replay_gate checks snapshot refs before recovery trusts state.
# 函数用途: 恢复/回放必须有 run/task/scope/effective_contract/tool_manifest/runlog 这些机器事实。
def evaluate_recovery_replay_gate(snapshot: dict[str, Any]) -> GateDecision:
    findings: list[GateFinding] = []
    _require_text(snapshot, "run_id", findings)
    _require_text(snapshot, "task_id", findings)
    _require_mapping(snapshot, "scope", findings)
    _require_text(snapshot, "effective_contract_ref", findings)
    _require_text(snapshot, "effective_contract_hash", findings)
    _require_text(snapshot, "tool_manifest_ref", findings)
    _require_text(snapshot, "runlog_ref", findings)
    if findings:
        return GateDecision.recovering("recovery_replay", findings, evidence={"missing_count": len(findings)})
    return GateDecision.allow(
        "recovery_replay",
        evidence={
            "run_id": str(snapshot.get("run_id") or ""),
            "task_id": str(snapshot.get("task_id") or ""),
            "effective_contract_hash": str(snapshot.get("effective_contract_hash") or ""),
        },
    )


# LLM: gate_payload_findings copies child gate findings without parsing human text.
# 函数用途: 从 runtime_gate.findings 结构字段恢复统一 GateFinding，缺失时给 fallback code。
def gate_payload_findings(payload: dict[str, Any], *, fallback: str) -> list[GateFinding]:
    raw = payload.get("findings")
    if not isinstance(raw, list):
        return [GateFinding(fallback)]
    findings = [_gate_payload_finding(item, fallback=fallback) for item in raw if isinstance(item, dict)]
    return findings or [GateFinding(fallback)]


# LLM: is_tool_audit_record identifies tool rows by stable type/tool fields.
# 函数用途: 区分工具审计记录和其他记录，避免按自然语言标签判断。
def is_tool_audit_record(record: dict[str, Any]) -> bool:
    return str(record.get("type") or "tool_result") == "tool_result" or str(record.get("tool") or "").strip() != ""


# LLM: validate_tool_audit_record appends machine findings for missing audit fields.
# 函数用途: 检查单条工具审计记录是否包含 runtime_gate 和 parameters。
def validate_tool_audit_record(index: int, record: dict[str, Any], findings: list[GateFinding]) -> None:
    if not isinstance(record.get("runtime_gate"), dict):
        findings.append(GateFinding("AUDIT_RUNTIME_GATE_MISSING", evidence={"index": index}))
    if not isinstance(record.get("parameters"), dict):
        findings.append(GateFinding("AUDIT_PARAMETERS_MISSING", evidence={"index": index}))


# LLM: _gate_payload_finding maps one child gate finding into this gate's finding format.
# 函数用途: 保留 code/severity/message/evidence 字段，让父级 gate 可追溯子 gate 失败原因。
def _gate_payload_finding(item: dict[str, Any], *, fallback: str) -> GateFinding:
    evidence = item.get("evidence")
    return GateFinding(
        str(item.get("code") or fallback),
        severity=str(item.get("severity") or "P1"),
        message=str(item.get("message") or ""),
        evidence=dict(evidence) if isinstance(evidence, dict) else {},
    )


# LLM: _require_text records a missing non-empty snapshot field.
# 函数用途: 对恢复快照的字符串字段做必填检查，并输出稳定 code。
def _require_text(snapshot: dict[str, Any], key: str, findings: list[GateFinding]) -> None:
    if not str(snapshot.get(key) or "").strip():
        findings.append(GateFinding(f"{key.upper()}_MISSING"))


# LLM: _require_mapping records a missing object snapshot field.
# 函数用途: 对恢复快照的 mapping 字段做必填检查，并输出稳定 code。
def _require_mapping(snapshot: dict[str, Any], key: str, findings: list[GateFinding]) -> None:
    value = snapshot.get(key)
    if not isinstance(value, dict) or not value:
        findings.append(GateFinding(f"{key.upper()}_MISSING"))


__all__ = [
    "evaluate_acceptance_closeout_gate",
    "evaluate_recovery_replay_gate",
    "evaluate_runtime_audit_gate",
    "gate_payload_findings",
    "is_tool_audit_record",
    "validate_tool_audit_record",
]
