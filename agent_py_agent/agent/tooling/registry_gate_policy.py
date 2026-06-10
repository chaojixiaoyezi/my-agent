
from __future__ import annotations

from ..contracts.gates.models import GateDecision
from ..contracts.gates.tool_effects import ToolGatePolicy
from ..contracts.gates.tool_manifest import evaluate_tool_manifest_gate, tool_manifest_from_spec
from .models import BaseTool


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
    "tool_gate_policy",
    "tool_manifest_decision",
]
