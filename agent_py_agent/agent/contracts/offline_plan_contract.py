# LLM: Offline plan contracts validate structured execution plans before tools run.
# 模块用途: 检查计划产物、工具要求、禁用工具、执行漂移和失败后重规划是否合规。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .contract_validation_recovery import recovery_for_findings


# LLM: OfflinePlanValidation reports plan/contract mismatches.
# 类用途: 返回计划合同是否通过、错误码和结构化 finding。
@dataclass(frozen=True)
class OfflinePlanValidation:
    ok: bool
    error_codes: tuple[str, ...]
    findings: tuple[dict[str, object], ...]
    recovery: dict[str, object] | None = None


# LLM: validate_plan_contract checks explicit plan fields against explicit contract fields.
# 函数用途: 用 output_paths、steps.tool、args_hash 和 execution_events 校验计划，不解析 prompt 文本。
def validate_plan_contract(
    *,
    plan: dict[str, Any],
    contract: dict[str, Any],
    execution_events: tuple[dict[str, Any], ...] = (),
) -> OfflinePlanValidation:
    findings: list[dict[str, object]] = []
    _validate_output_paths(plan, contract, findings)
    _validate_required_tools(plan, contract, findings)
    _validate_forbidden_tools(plan, contract, findings)
    _validate_artifact_drift(plan, execution_events, findings)
    _validate_replan_changes_strategy(plan, execution_events, findings)
    return OfflinePlanValidation(
        ok=not findings,
        error_codes=tuple(dict.fromkeys(_text(item.get("code")) for item in findings)),
        findings=tuple(findings),
        recovery=recovery_for_findings("offline_plan", findings),
    )


# LLM: _validate_output_paths requires planned output paths for required artifacts.
# 函数用途: 合同声明 required artifact 时，计划必须显式列出 output_paths。
def _validate_output_paths(
    plan: dict[str, Any],
    contract: dict[str, Any],
    findings: list[dict[str, object]],
) -> None:
    if _required_artifact_paths(contract) and not _string_tuple(plan.get("output_paths")):
        findings.append(_finding("PLAN_OUTPUT_PATH_MISSING"))


# LLM: _validate_required_tools requires every contract-required tool in plan steps.
# 函数用途: required_tools 不在 steps.tool 中时返回 PLAN_REQUIRED_TOOL_MISSING。
def _validate_required_tools(
    plan: dict[str, Any],
    contract: dict[str, Any],
    findings: list[dict[str, object]],
) -> None:
    missing = sorted(set(_string_tuple(contract.get("required_tools"))) - set(_plan_tools(plan)))
    if missing:
        findings.append(_finding("PLAN_REQUIRED_TOOL_MISSING", {"tools": tuple(missing)}))


# LLM: _validate_forbidden_tools rejects plans containing contract-forbidden tools.
# 函数用途: forbidden_tools 与 steps.tool 有交集时返回 PLAN_FORBIDDEN_TOOL。
def _validate_forbidden_tools(
    plan: dict[str, Any],
    contract: dict[str, Any],
    findings: list[dict[str, object]],
) -> None:
    used = set(_plan_tools(plan))
    forbidden_used = sorted(used & set(_string_tuple(contract.get("forbidden_tools"))))
    if forbidden_used:
        findings.append(_finding("PLAN_FORBIDDEN_TOOL", {"tools": tuple(forbidden_used)}))


# LLM: _validate_artifact_drift ensures writes stay within planned output paths.
# 函数用途: artifact_write.path 不在 output_paths 中时返回 PLAN_ARTIFACT_DRIFT。
def _validate_artifact_drift(
    plan: dict[str, Any],
    events: tuple[dict[str, Any], ...],
    findings: list[dict[str, object]],
) -> None:
    allowed = set(_string_tuple(plan.get("output_paths")))
    if not allowed:
        return
    for event in events:
        if _text(event.get("type")) != "artifact_write":
            continue
        if _text(event.get("path")) not in allowed:
            findings.append(_finding("PLAN_ARTIFACT_DRIFT", {"path": _text(event.get("path"))}))
            return


# LLM: _validate_replan_changes_strategy blocks retrying the exact failed tool parameters.
# 函数用途: 新计划 step 与已有失败 tool_result 的 tool/args_hash 相同时返回 REPLAN_REPEATS_FAILED_PARAMS。
def _validate_replan_changes_strategy(
    plan: dict[str, Any],
    events: tuple[dict[str, Any], ...],
    findings: list[dict[str, object]],
) -> None:
    failed = {_tool_args_key(event) for event in events if _failed_tool_result(event)}
    planned = {_tool_args_key(step) for step in _plan_steps(plan)}
    repeated = sorted(key for key in planned & failed if key[0] and key[1])
    if repeated:
        findings.append(_finding("REPLAN_REPEATS_FAILED_PARAMS", {"tool": repeated[0][0]}))


# LLM: _required_artifact_paths reads artifacts.required.path fields.
# 函数用途: 返回合同中显式要求的产物路径。
def _required_artifact_paths(contract: dict[str, Any]) -> tuple[str, ...]:
    artifacts = contract.get("artifacts")
    if not isinstance(artifacts, dict) or not isinstance(artifacts.get("required"), (list, tuple)):
        return ()
    return tuple(_text(item.get("path")) for item in artifacts["required"] if isinstance(item, dict) and item.get("path"))


# LLM: _plan_tools extracts step tool names from structured plan steps.
# 函数用途: 从 plan.steps[].tool 读取工具名。
def _plan_tools(plan: dict[str, Any]) -> tuple[str, ...]:
    return tuple(_text(step.get("tool")) for step in _plan_steps(plan) if _text(step.get("tool")))


# LLM: _plan_steps returns dict steps only.
# 函数用途: 忽略非 dict step，避免从文本计划中猜机器事实。
def _plan_steps(plan: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    steps = plan.get("steps")
    if not isinstance(steps, (list, tuple)):
        return ()
    return tuple(step for step in steps if isinstance(step, dict))


# LLM: _failed_tool_result detects structured failed tool result events.
# 函数用途: tool_result 且 ok=false 时视为失败事实。
def _failed_tool_result(event: dict[str, Any]) -> bool:
    return _text(event.get("type")) == "tool_result" and event.get("ok") is False


# LLM: _tool_args_key returns exact tool and args_hash identity.
# 函数用途: 用于比较失败参数和重规划参数是否完全相同。
def _tool_args_key(value: dict[str, Any]) -> tuple[str, str]:
    return (_text(value.get("tool")), _text(value.get("args_hash")))


# LLM: _string_tuple normalizes explicit string collections.
# 函数用途: 把结构化数组规整成去空字符串元组。
def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set)):
        return ()
    return tuple(text for item in value for text in (_text(item),) if text)


# LLM: _finding creates compact plan findings.
# 函数用途: 统一生成 code 和可选结构字段。
def _finding(code: str, extra: dict[str, object] | None = None) -> dict[str, object]:
    return {"code": code, **(extra or {})}


# LLM: _text normalizes scalar fields for exact comparisons only.
# 函数用途: 将 None 或标量转成去空白字符串，不解析自然语言语义。
def _text(value: object) -> str:
    return str(value or "").strip()


__all__ = ["OfflinePlanValidation", "validate_plan_contract"]
