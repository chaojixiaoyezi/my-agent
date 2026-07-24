
from __future__ import annotations

from typing import Any

from ..common.value_parsing import dedupe_strings
from .error_taxonomy import error_contract, tool_failure_taxonomy


# LLM: manifest 只陈述实际工具范围和副作用合同；无消费者的 capability 标签不得冒充执行权限。
# 函数用途: 把当前工具快照投影成机器可读的可见、可执行和失败合同清单。
def tool_manifest_payload(
    tool_specs: list[object],
    *,
    allowed_tools: list[str] | None = None,
    owner_type: str = "main_agent",
) -> dict[str, object]:
    specs = [_tool_item(spec) for spec in tool_specs]
    spec_names = [str(item["name"]) for item in specs if str(item["name"]).strip()]
    visible = _visible_tools(spec_names, allowed_tools)
    executable = [name for name in visible if not spec_names or name in spec_names]
    indexed = {str(item["name"]): item for item in specs}
    tools = [
        {
            **indexed[name],
            "visible_in_context": True,
            "executable_in_context": name in executable,
            "permission_mode": _permission_mode(owner_type),
        }
        for name in visible
        if name in indexed
    ]
    return {
        "visible_tools": visible,
        "executable_tools": executable,
        "permission_mode": _permission_mode(owner_type),
        "failure_taxonomy": tool_failure_taxonomy(),
        "failure_contracts": _failure_contracts(),
        "tools": tools,
    }


def _tool_item(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        name = str(value.get("name") or "")
        category = str(value.get("category") or "")
        description = str(value.get("description") or "")
        parameters = value.get("parameters")
        details = value.get("parameter_details")
        examples = value.get("examples")
        effect = str(value.get("effect") or "")
        default_mode = str(value.get("default_mode") or "")
        idempotency_scope = str(value.get("idempotency_scope") or "")
        requires_approval = value.get("requires_approval") is True
        timeout_seconds = _int_or_zero(value.get("timeout_seconds"))
        output_refs = value.get("output_refs")
    else:
        name = str(getattr(value, "name", "") or "")
        category = str(getattr(value, "category", "") or "")
        description = str(getattr(value, "description", "") or "")
        parameters = getattr(value, "parameters", {})
        details = getattr(value, "parameter_details", {})
        examples = getattr(value, "examples", ())
        effect = str(getattr(value, "effect", "") or "")
        default_mode = str(getattr(value, "default_mode", "") or "")
        idempotency_scope = str(getattr(value, "idempotency_scope", "") or "")
        requires_approval = getattr(value, "requires_approval", False) is True
        timeout_seconds = _int_or_zero(getattr(value, "timeout_seconds", 0))
        output_refs = getattr(value, "output_refs", ())
    param_map = parameters if isinstance(parameters, dict) else {}
    detail_map = details if isinstance(details, dict) else {}
    return {
        "name": name,
        "category": category,
        "description": description,
        "effect": effect,
        "default_mode": default_mode,
        "idempotency_scope": idempotency_scope,
        "requires_approval": requires_approval,
        "timeout_seconds": timeout_seconds,
        "output_refs": dedupe_strings(output_refs),
        "parameters": sorted(str(key) for key in param_map),
        "parameter_details": {str(key): str(val) for key, val in detail_map.items()},
        "examples": [str(item) for item in list(examples or ())[:2]],
        "orchestration_tool": category in {"orchestration", "collaboration"},
    }


def _visible_tools(spec_names: list[str], allowed_tools: list[str] | None) -> list[str]:
    allowed = dedupe_strings(allowed_tools or [])
    return allowed or spec_names


def _permission_mode(owner_type: str) -> str:
    return "same_as_root_agent" if str(owner_type or "").strip() == "main_agent" else "owner_scoped"


def _failure_contracts() -> list[dict[str, object]]:
    return [
        {
            "code": code,
            "category": contract.category,
            "retryable": contract.retryable,
            "recommended_action": contract.recommended_action,
            "recovery_hint": contract.recovery_hint,
        }
        for code in tool_failure_taxonomy()
        for contract in [error_contract(code)]
    ]


def _int_or_zero(value: object) -> int:
    if value in (None, ""):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


__all__ = ["tool_manifest_payload"]
