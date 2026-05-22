# LLM: Shared tool-manifest contracts keep runtime visibility and recovery facts aligned across bundle, list_tools, and replay.
# 模块用途: 统一生成工具清单、失败分类和恢复合同，避免 context bundle、list_tools 和恢复链各自拼一份工具事实。

from __future__ import annotations

from typing import Any

from .error_taxonomy import error_contract, tool_failure_taxonomy


# LLM: tool_manifest_payload builds one machine-readable tool surface from specs and runtime grants.
# 函数用途: 统一返回 visible/executable tools、权限模式、失败分类和恢复合同，供主代理上下文包与 list_tools 共用。
def tool_manifest_payload(
    tool_specs: list[object],
    *,
    allowed_tools: list[str] | None = None,
    granted_capabilities: list[str] | None = None,
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
        "granted_capabilities": _string_list(granted_capabilities),
        "failure_taxonomy": tool_failure_taxonomy(),
        "failure_contracts": _failure_contracts(),
        "tools": tools,
    }


# LLM: _tool_item trims tool specs down to stable runtime facts.
# 函数用途: 从 ToolSpec 或 dict 中提取名字、类别、参数和示例，避免把整份工具手册塞进上下文。
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
        requires_idempotency = value.get("requires_idempotency") is True
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
        requires_idempotency = getattr(value, "requires_idempotency", False) is True
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
        "requires_idempotency": requires_idempotency,
        "requires_approval": requires_approval,
        "timeout_seconds": timeout_seconds,
        "output_refs": _string_list(output_refs),
        "parameters": sorted(str(key) for key in param_map),
        "parameter_details": {str(key): str(val) for key, val in detail_map.items()},
        "examples": [str(item) for item in list(examples or ())[:2]],
        "orchestration_tool": category == "orchestration",
    }


# LLM: _visible_tools treats explicit allowed_tools as runtime visibility hints, not permanent hard-coded contracts.
# 函数用途: allowed_tools 存在时按它决定当前可见工具；否则回退到 specs 里的全部工具名。
def _visible_tools(spec_names: list[str], allowed_tools: list[str] | None) -> list[str]:
    allowed = _string_list(allowed_tools)
    return allowed or spec_names


# LLM: _permission_mode keeps owner-scoped bundle readers from inferring policy from prose.
# 函数用途: 根据 owner_type 返回稳定权限模式名。
def _permission_mode(owner_type: str) -> str:
    return "same_as_root_agent" if str(owner_type or "").strip() == "main_agent" else "owner_scoped"


# LLM: _failure_contracts exposes structured recovery facts instead of only raw error-code strings.
# 函数用途: 把工具失败分类扩展成 code/category/retryable/recommended_action/recovery_hint，方便恢复链直接消费。
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


# LLM: _string_list normalizes optional list inputs without importing larger utility modules.
# 函数用途: 将 granted_capabilities / allowed_tools 这类可空列表规整成去重字符串数组。
def _string_list(items: object) -> list[str]:
    result: list[str] = []
    if not isinstance(items, (list, tuple, set)):
        return result
    for item in items:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


# LLM: _int_or_zero keeps manifest rendering deterministic for optional numeric fields.
# 函数用途: 将 timeout_seconds 这类可选数字字段规整成 int，非法值暴露为 0 供执行门再拒绝。
def _int_or_zero(value: object) -> int:
    if value in (None, ""):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


__all__ = ["tool_manifest_payload"]
