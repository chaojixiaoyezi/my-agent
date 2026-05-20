# LLM: Main-agent core entrypoint contracts keep single-agent execution facts in one structured place.
# 模块用途: 冻结主代理状态机、工具执行、验收、日志、审批和有效合同入口，防止绕过底层合同。

from __future__ import annotations

from typing import Any

from .offline_contract_report import OfflineContractValidation, finding, text, validation_report

REQUIRED_ENTRYPOINTS = (
    "state_machine",
    "tool_executor",
    "acceptance_gate",
    "runlog",
    "tooltrace",
    "approval_gate",
    "effective_contract",
)


# LLM: validate_main_agent_core_entrypoints checks machine-declared runtime invariants.
# 函数用途: 检查核心入口是否齐全，以及成功、工具调用、状态写入、机器事实来源是否走结构化合同。
def validate_main_agent_core_entrypoints(contract: dict[str, Any]) -> OfflineContractValidation:
    findings: list[dict[str, object]] = []
    entrypoints = contract.get("entrypoints") if isinstance(contract.get("entrypoints"), dict) else {}
    invariants = contract.get("invariants") if isinstance(contract.get("invariants"), dict) else {}
    _validate_required_entrypoints(entrypoints, findings)
    _validate_invariants(invariants, findings)
    return validation_report(findings)


# LLM: _validate_required_entrypoints rejects missing contract-bound runtime surfaces.
# 函数用途: 每个主代理核心入口都必须是显式非空结构字段。
def _validate_required_entrypoints(entrypoints: dict[str, Any], findings: list[dict[str, object]]) -> None:
    missing = tuple(name for name in REQUIRED_ENTRYPOINTS if not text(entrypoints.get(name)))
    if missing:
        findings.append(finding("MAIN_CORE_ENTRYPOINT_MISSING", {"entrypoints": missing}))


# LLM: _validate_invariants rejects runtime modes that can bypass verification or tool contracts.
# 函数用途: 校验主代理成功、工具执行、状态修改和机器事实来源的不变量。
def _validate_invariants(invariants: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if invariants.get("success_requires_verification") is not True:
        findings.append(finding("MAIN_CORE_SUCCESS_WITHOUT_VERIFICATION"))
    if invariants.get("tool_calls_require_executor") is not True:
        findings.append(finding("MAIN_CORE_TOOL_EXECUTOR_BYPASS"))
    if text(invariants.get("state_mutation_mode")) != "event_only":
        findings.append(finding("MAIN_CORE_STATE_MUTATION_NOT_EVENT_ONLY"))
    if text(invariants.get("machine_facts_source")) != "structured_fields":
        findings.append(finding("MAIN_CORE_NATURAL_LANGUAGE_FACT_SOURCE"))


__all__ = ["REQUIRED_ENTRYPOINTS", "validate_main_agent_core_entrypoints"]
