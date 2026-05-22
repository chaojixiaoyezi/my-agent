# LLM: Run contract gate makes the effective contract and run scope mandatory facts.
# 模块用途: 在执行/收口前校验 request/run/task/workspace 和有效合同，并生成稳定 hash 供 replay 对齐。

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from ..contract_doctor import lint_contract
from .models import GateDecision, GateFinding


# LLM: evaluate_run_contract_gate validates scope and fingerprints the effective contract.
# 函数用途: 要求运行标识和 workspace 这些机器字段存在，并输出 effective_contract_hash。
def evaluate_run_contract_gate(
    contract: Mapping[str, Any] | None,
    *,
    scope: Mapping[str, Any] | None = None,
) -> GateDecision:
    findings: list[GateFinding] = []
    scope = scope or {}
    request_id = str(scope.get("request_id") or "")
    run_id = str(scope.get("run_id") or "")
    task_id = str(scope.get("task_id") or "")
    workspace_root = str(scope.get("workspace_root") or "")
    if not isinstance(contract, Mapping) or not contract:
        findings.append(GateFinding("EFFECTIVE_CONTRACT_MISSING"))
    elif _should_lint_contract(contract):
        findings.extend(_doctor_findings(dict(contract)))
    _require_text("REQUEST_ID_MISSING", request_id, findings)
    _require_text("RUN_ID_MISSING", run_id, findings)
    _require_text("TASK_ID_MISSING", task_id, findings)
    _require_text("WORKSPACE_ROOT_MISSING", workspace_root, findings)
    artifacts = _artifact_items(contract)
    raw_artifacts = contract.get("artifacts") if isinstance(contract, Mapping) else None
    if raw_artifacts is not None and not isinstance(raw_artifacts, (list, dict)):
        findings.append(GateFinding("EFFECTIVE_CONTRACT_ARTIFACTS_INVALID"))
    if findings:
        return GateDecision.recovering("run_contract", findings, evidence={"missing_count": len(findings)})
    return GateDecision.allow(
        "run_contract",
        evidence={
            "request_id": request_id,
            "run_id": run_id,
            "task_id": task_id,
            "workspace_root": workspace_root,
            "effective_contract_hash": _contract_hash(dict(contract or {})),
            "artifact_count": len(artifacts),
        },
    )


# LLM: _should_lint_contract detects full structured contracts without rejecting legacy gate fixtures.
# 函数用途: 只有带 version/rules/tool policy 或 artifacts 对象的合同才跑 Contract Doctor。
def _should_lint_contract(contract: Mapping[str, Any]) -> bool:
    return any(key in contract for key in ("version", "rules", "required_tools", "forbidden_tools", "artifact_path")) or isinstance(
        contract.get("artifacts"), dict
    )


# LLM: _doctor_findings converts Contract Doctor codes into gate findings.
# 函数用途: 把合同预检错误接入 run_contract gate，不让坏合同继续进入 hash/replay。
def _doctor_findings(contract: dict[str, Any]) -> list[GateFinding]:
    report = lint_contract(contract)
    return [GateFinding(code) for code in report.error_codes]


# LLM: _artifact_items accepts the legacy list shape and current artifacts.required shape.
# 函数用途: 计算产物数量时只读取结构字段，兼容旧 gate 调用方。
def _artifact_items(contract: Mapping[str, Any] | None) -> list[object]:
    if not isinstance(contract, Mapping):
        return []
    artifacts = contract.get("artifacts")
    if isinstance(artifacts, list):
        return artifacts
    if isinstance(artifacts, dict):
        required = artifacts.get("required")
        return list(required) if isinstance(required, list) else []
    return []


# LLM: _require_text appends one stable missing-field finding.
# 函数用途: 对 run scope 字段做非空校验，保持错误码稳定。
def _require_text(code: str, value: object, findings: list[GateFinding]) -> None:
    if not str(value or "").strip():
        findings.append(GateFinding(code))


# LLM: _contract_hash fingerprints canonical JSON rather than prompt text.
# 函数用途: 用排序 JSON 计算有效合同 hash，供 closeout/replay 对照同一份机器合同。
def _contract_hash(contract: dict[str, Any]) -> str:
    encoded = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


__all__ = ["evaluate_run_contract_gate"]
