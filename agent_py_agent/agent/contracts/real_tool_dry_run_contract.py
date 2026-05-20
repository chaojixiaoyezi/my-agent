# LLM: Real tool dry-run contracts prove live wrappers ran through the trusted executor before shadow mode.
# 模块用途: 校验真实只读工具和真实 dry-run 工具的探测记录，防止只看 adapter 声明就进入影子模式。

from __future__ import annotations

from typing import Any

from .offline_contract_report import (
    OfflineContractValidation,
    finding,
    string_tuple,
    text,
    validation_report,
)

TRUSTED_TOOL_EXECUTOR_REFS = {
    "ToolRegistry.execute_call",
    "registry.execute_call",
    "tool_executor.execute",
    "tool_registry.execute_call",
}
VALID_EFFECTS = {"read_only", "mutating", "dangerous"}
SIDE_EFFECT_EFFECTS = {"mutating", "dangerous"}


# LLM: validate_real_tool_dry_run_probes checks live wrapper probes, not adapter metadata.
# 函数用途: 对真实工具 probe 的执行入口、结果形状、只读边界、dry-run 边界和幂等字段做通用验收。
def validate_real_tool_dry_run_probes(probes: tuple[dict[str, Any], ...]) -> OfflineContractValidation:
    findings: list[dict[str, object]] = []
    if not probes:
        findings.append(finding("REAL_TOOL_PROBES_MISSING"))
        return validation_report(findings)
    for probe in probes:
        _validate_one_probe(probe, findings)
    return validation_report(findings)


# LLM: _validate_one_probe keeps probe checks ordered so reports are stable across CI runs.
# 函数用途: 校验单个真实工具 probe 的必填机器字段、可信执行器、结果 payload 和 effect 专属规则。
def _validate_one_probe(probe: dict[str, Any], findings: list[dict[str, object]]) -> None:
    _validate_probe_identity(probe, findings)
    effect = _validate_effect(probe, findings)
    _validate_executor_ref(probe, findings)
    _validate_result_schema(probe, findings)
    _validate_result_shape(probe, findings)
    if effect == "read_only":
        _validate_read_only_probe(probe, findings)
    if effect in SIDE_EFFECT_EFFECTS:
        _validate_side_effect_dry_run_probe(probe, findings)


# LLM: _validate_probe_identity requires traceable operation ids without parsing prompt text.
# 函数用途: 确认 probe 具备定位真实工具调用所需的结构化 id 和工具名。
def _validate_probe_identity(probe: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if not text(probe.get("probe_id")):
        findings.append(finding("REAL_TOOL_PROBE_ID_MISSING", _tool_extra(probe)))
    if not text(probe.get("operation_id")):
        findings.append(finding("REAL_TOOL_OPERATION_ID_MISSING", _tool_extra(probe)))
    if not text(probe.get("tool")):
        findings.append(finding("REAL_TOOL_NAME_MISSING", _tool_extra(probe)))


# LLM: _validate_effect returns a canonical effect only when it is an explicit supported value.
# 函数用途: 校验 effect 字段，不从工具名或业务描述推断工具副作用等级。
def _validate_effect(probe: dict[str, Any], findings: list[dict[str, object]]) -> str:
    effect = text(probe.get("effect"))
    if effect not in VALID_EFFECTS:
        findings.append(finding("REAL_TOOL_EFFECT_INVALID", _tool_extra(probe)))
        return ""
    return effect


# LLM: _validate_executor_ref ensures phase 5 probes used our wrapper layer.
# 函数用途: 只认可工具执行器或 ToolRegistry 的结构化引用，拒绝 direct SDK / direct shell 类绕行记录。
def _validate_executor_ref(probe: dict[str, Any], findings: list[dict[str, object]]) -> None:
    executor_ref = text(probe.get("tool_executor_ref"))
    if not executor_ref:
        findings.append(finding("REAL_TOOL_EXECUTOR_REF_MISSING", _tool_extra(probe)))
        return
    if executor_ref not in TRUSTED_TOOL_EXECUTOR_REFS:
        findings.append(finding("REAL_TOOL_EXECUTOR_REF_UNTRUSTED", _tool_extra(probe)))


# LLM: _validate_result_schema keeps tool outputs tied to machine schemas.
# 函数用途: 要求真实 probe 记录 result_schema_ref，避免后续 replay 只能看自然语言说明。
def _validate_result_schema(probe: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if not text(probe.get("result_schema_ref")):
        findings.append(finding("REAL_TOOL_RESULT_SCHEMA_MISSING", _tool_extra(probe)))


# LLM: _validate_result_shape rejects raw strings and missing ok flags from live wrapper probes.
# 函数用途: 确保 result 是结构化对象，且 ok 字段是明确布尔值。
def _validate_result_shape(probe: dict[str, Any], findings: list[dict[str, object]]) -> None:
    result = probe.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
        findings.append(finding("REAL_TOOL_RESULT_SHAPE_INVALID", _tool_extra(probe)))


# LLM: _validate_read_only_probe blocks read-only probes that report any side-effect refs.
# 函数用途: 只读真实工具必须以 read_only 模式记录，且不能有已执行动作或副作用引用。
def _validate_read_only_probe(probe: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if text(probe.get("mode")) != "read_only":
        findings.append(finding("REAL_TOOL_READ_ONLY_MODE_MISMATCH", _tool_extra(probe)))
    if _executed_actions(probe) or string_tuple(probe.get("side_effect_refs")) or _result_mode(probe) in {
        "apply",
        "execute",
        "real_run",
    }:
        findings.append(finding("REAL_TOOL_READ_ONLY_HAS_SIDE_EFFECTS", _tool_extra(probe)))


# LLM: _validate_side_effect_dry_run_probe requires dry-run evidence and replay-safe idempotency facts.
# 函数用途: 可变更/高危工具在阶段 5 只能记录 dry_run，不能真实执行，并必须保留幂等键和参数 hash。
def _validate_side_effect_dry_run_probe(probe: dict[str, Any], findings: list[dict[str, object]]) -> None:
    result_mode = _result_mode(probe)
    if text(probe.get("mode")) != "dry_run" or result_mode != "dry_run":
        findings.append(finding("REAL_TOOL_DRY_RUN_MODE_MISMATCH", _tool_extra(probe)))
    if _executed_actions(probe) or _result_execution_flag(probe):
        findings.append(finding("REAL_TOOL_SIDE_EFFECT_EXECUTED", _tool_extra(probe)))
    if not text(probe.get("idempotency_key")):
        findings.append(finding("REAL_TOOL_IDEMPOTENCY_KEY_MISSING", _tool_extra(probe)))
    if not text(probe.get("args_hash")):
        findings.append(finding("REAL_TOOL_ARGS_HASH_MISSING", _tool_extra(probe)))


# LLM: _result_mode reads explicit result mode fields only.
# 函数用途: 从 result 或 result.payload 中读取 mode，不解析 output 文本含义。
def _result_mode(probe: dict[str, Any]) -> str:
    result = probe.get("result")
    if not isinstance(result, dict):
        return ""
    payload = _payload(result)
    return text(payload.get("mode") or result.get("mode"))


# LLM: _result_execution_flag detects structured proof of real execution.
# 函数用途: 识别 result/payload 中的执行布尔标记，供 dry-run 边界拒绝真实副作用。
def _result_execution_flag(probe: dict[str, Any]) -> bool:
    result = probe.get("result")
    if not isinstance(result, dict):
        return False
    payload = _payload(result)
    return any(
        value is True
        for value in (
            result.get("executed"),
            result.get("real_execution"),
            payload.get("executed"),
            payload.get("real_execution"),
        )
    )


# LLM: _payload handles both nested payload records and flat result objects.
# 函数用途: 兼容 result.payload 和 result 本身两种结构化结果形状。
def _payload(result: dict[str, Any]) -> dict[str, Any]:
    payload = result.get("payload")
    return payload if isinstance(payload, dict) else result


# LLM: _executed_actions returns explicit action refs only.
# 函数用途: 规整 executed_actions 数组；不会从输出文本里猜测是否发生副作用。
def _executed_actions(probe: dict[str, Any]) -> tuple[str, ...]:
    return string_tuple(probe.get("executed_actions"))


# LLM: _tool_extra keeps findings compact but traceable.
# 函数用途: 给 finding 附加工具、probe 和 operation id，方便定位失败 probe。
def _tool_extra(probe: dict[str, Any]) -> dict[str, object]:
    return {
        "tool": text(probe.get("tool")),
        "probe_id": text(probe.get("probe_id")),
        "operation_id": text(probe.get("operation_id")),
    }


__all__ = [
    "TRUSTED_TOOL_EXECUTOR_REFS",
    "validate_real_tool_dry_run_probes",
]
