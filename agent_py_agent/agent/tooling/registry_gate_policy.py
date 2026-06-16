
from __future__ import annotations

from typing import Any

from ..contracts.gates.models import GateDecision
from ..contracts.gates.tool_effects import ToolGatePolicy
from ..contracts.gates.tool_manifest import evaluate_tool_manifest_gate, tool_manifest_from_spec
from ..contracts.tool_call_policy import ToolCallPolicy
from .models import BaseTool


def tool_call_policy_for_spec(tool: BaseTool | None) -> ToolCallPolicy | None:
    """从工具 ToolSpec 现场构造参数校验 policy（required + 顶层 type）。

    灰度只接 required_parameters 与 parameter_schema 顶层 type 字符串：
    - required_parameters 是 execute 的实际必填（Step0a 已对齐），不照搬 prose。
    - parameter_types 把 parameter_schema[name]["type"] 拍平成 {name: "string"|...}，
      policy 的 _matches_type 只认顶层 type 字符串，不认 items/enum/minimum，正好兼容。
    仅声明了 type 的参数才参与类型校验；未声明的参数完全不约束，
    保证不会比 native API 自身的 input_schema 更严而误拒。
    无任何 required/type 声明时返回 None（gate 直接跳过，零开销零风险）。
    """
    spec = getattr(tool, "spec", None)
    if spec is None:
        return None
    name = str(getattr(spec, "name", "") or "")
    if not name:
        return None
    required = tuple(str(item) for item in getattr(spec, "required_parameters", []) or [])
    parameter_types = _flatten_parameter_types(getattr(spec, "parameter_schema", {}) or {})
    if not required and not parameter_types:
        return None
    return ToolCallPolicy(
        required_parameters={name: required} if required else {},
        parameter_types={name: parameter_types} if parameter_types else {},
    )


def _flatten_parameter_types(parameter_schema: dict[str, Any]) -> dict[str, str]:
    flattened: dict[str, str] = {}
    for param_name, schema in parameter_schema.items():
        if not isinstance(schema, dict):
            continue
        kind = schema.get("type")
        if isinstance(kind, str) and kind.strip():
            flattened[str(param_name)] = kind.strip()
    return flattened


def tool_manifest_decision(tool_name: str, tools: dict[str, BaseTool]) -> GateDecision:
    tool = tools.get(tool_name)
    if tool is None:
        return GateDecision.deny("tool_manifest", "TOOL_NOT_REGISTERED", evidence={"tool_name": tool_name})
    return evaluate_tool_manifest_gate(tool_manifest_from_spec(getattr(tool, "spec", None)))


def tool_gate_policy(boundary: dict[str, object] | None, tool: BaseTool | None = None) -> ToolGatePolicy | None:
    effects = dict(boundary_mapping(boundary, "tool_effects") or {})
    if tool is not None:
        _merge_tool_spec_effect(effects, tool)
    modes = boundary_mapping(boundary, "tool_modes")
    approvals = boundary_list(boundary, "approved_actions")
    ledger = boundary_list(boundary, "idempotency_ledger")
    run_id = boundary_text(boundary, "run_id")
    if not effects and not modes and not approvals and not ledger:
        return None
    return ToolGatePolicy(
        tool_effects=effects,
        tool_modes=modes or {},
        approved_actions=tuple(approvals),
        idempotency_ledger=tuple(ledger),
        run_id=run_id,
    )


def _merge_tool_spec_effect(effects: dict[str, object], tool: BaseTool) -> None:
    spec = getattr(tool, "spec", None)
    name = str(getattr(spec, "name", "") or "")
    effect = str(getattr(spec, "effect", "") or "")
    if name and effect and name not in effects:
        effects[name] = effect


def boundary_mapping(boundary: dict[str, object] | None, key: str) -> dict[str, object] | None:
    if not isinstance(boundary, dict):
        return None
    value = boundary.get(key)
    return value if isinstance(value, dict) else None


def boundary_list(boundary: dict[str, object] | None, key: str) -> list[object]:
    if not isinstance(boundary, dict):
        return []
    value = boundary.get(key)
    return list(value) if isinstance(value, (list, tuple)) else []


def boundary_strings(boundary: dict[str, object] | None, key: str) -> list[str]:
    return [str(item) for item in boundary_list(boundary, key) if str(item).strip()]


def boundary_path_roots(boundary: dict[str, object] | None) -> list[str]:
    roots: list[str] = []
    for key in ("allowed_write_roots", "allowed_read_roots", "product_write_roots", "path_scope"):
        roots.extend(boundary_strings(boundary, key))
    return _dedupe_strings(roots)


def boundary_bool(boundary: dict[str, object] | None, key: str) -> bool:
    if not isinstance(boundary, dict):
        return False
    return boundary.get(key) is True


def boundary_text(boundary: dict[str, object] | None, key: str) -> str:
    if not isinstance(boundary, dict):
        return ""
    return str(boundary.get(key) or "").strip()


def _dedupe_strings(items: list[str]) -> list[str]:
    result: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


__all__ = [
    "boundary_bool",
    "boundary_path_roots",
    "boundary_strings",
    "tool_call_policy_for_spec",
    "tool_gate_policy",
    "tool_manifest_decision",
]
