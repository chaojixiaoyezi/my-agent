# LLM: Delivery materializer orchestration helpers; keep orchestration fields generic and prompt-derived.
# 模块用途: 从物化 JSON 中保留通用 orchestration_contract，不写任何专项任务模板。

from __future__ import annotations

from typing import Any


def _preserve_orchestration_contract(contract: dict[str, Any], source: dict[str, Any]) -> None:
    orchestration = source.get("orchestration_contract")
    if not isinstance(orchestration, dict):
        return
    normalized = _orchestration_contract(orchestration)
    if normalized:
        contract["orchestration_contract"] = normalized


def _orchestration_contract(value: dict[str, Any]) -> dict[str, Any]:
    required_tools = _string_list(value.get("required_tools") or value.get("required_actions"))
    requires = _boolish(value.get("requires_orchestration")) or bool(required_tools)
    minimum = _non_negative_int(value.get("minimum_subagent_count"), default=1 if requires else 0)
    if not requires and minimum <= 0:
        return {}
    execution_required = not _explicit_false(value.get("execution_required"))
    required_tools = _orchestration_required_tools(required_tools)
    result: dict[str, Any] = {
        "schema_version": "orchestration_contract.v1",
        "requires_orchestration": True,
        "execution_required": execution_required,
        "required_tools": required_tools,
        "minimum_subagent_count": minimum,
        "rework_budget": _non_negative_int(value.get("rework_budget"), default=2),
    }
    for key in ("source", "reason"):
        text = str(value.get(key) or "").strip()
        if text:
            result[key] = text
    return result


# LLM: _orchestration_required_tools keeps explicit contract fields stable while ensuring a creation tool exists.
# 函数用途: 保留外部显式 required_tools；运行时根据 execution_required 推导是否还必须 dispatch。
def _orchestration_required_tools(required_tools: list[str]) -> list[str]:
    tools = list(required_tools or ["create_subagents"])
    if "create_subagents" not in tools:
        tools.insert(0, "create_subagents")
    return tools


def _derived_evidence_contract(quality: dict[str, Any]) -> dict[str, Any]:
    evidence = dict(quality.get("evidence_contract")) if isinstance(quality.get("evidence_contract"), dict) else {}
    required_fields = _merged_required_fields(evidence.get("required_fields"), quality.get("metric_contracts"))
    if not required_fields:
        return {}
    evidence["required_fields"] = required_fields
    evidence.setdefault("require_verified", True)
    if "allowed_value_types" not in evidence:
        evidence["allowed_value_types"] = ["exact"]
    return evidence


def _merged_required_fields(raw_fields: object, metric_contracts: object) -> list[str]:
    fields = [str(item).strip() for item in raw_fields if str(item).strip()] if isinstance(raw_fields, list) else []
    if isinstance(metric_contracts, list):
        fields.extend(str(item.get("field") or "").strip() for item in metric_contracts if isinstance(item, dict))
    return sorted({item for item in fields if item})


def _string_list(value: object) -> list[str]:
    if isinstance(value, list | tuple):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _non_negative_int(value: object, *, default: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return max(0, parsed)


def _boolish(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    return str(value or "").strip().casefold() in {"1", "true", "yes", "y", "on", "required"}


def _explicit_false(value: object) -> bool:
    if isinstance(value, bool):
        return value is False
    if isinstance(value, int | float):
        return value == 0
    return str(value or "").strip().casefold() in {"0", "false", "no", "n", "off", "disabled"}
