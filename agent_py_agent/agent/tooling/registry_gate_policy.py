# LLM: Registry gate policy helpers keep execute_registry_call thin.
# 模块用途: 从工具 spec 和 write_boundary 构造运行时 gate policy，不执行真实工具。

from __future__ import annotations

from ..contracts.gates import (
    GateDecision,
    ToolGatePolicy,
    evaluate_tool_manifest_gate,
    tool_manifest_from_spec,
)
from .models import BaseTool


# LLM: tool_manifest_decision validates the selected tool spec before any side effect can run.
# 函数用途: 从 registry tool.spec 生成 manifest facts，确保缺 effect/schema 的工具不能执行。
def tool_manifest_decision(tool_name: str, tools: dict[str, BaseTool]) -> GateDecision:
    tool = tools.get(tool_name)
    if tool is None:
        return GateDecision.deny("tool_manifest", "TOOL_NOT_REGISTERED", evidence={"tool_name": tool_name})
    return evaluate_tool_manifest_gate(tool_manifest_from_spec(getattr(tool, "spec", None)))


# LLM: tool_gate_policy builds a trusted side-effect policy from write_boundary.
# 函数用途: 把 write_boundary 里的 tool_effects/tool_modes/approved_actions 收成 ToolGatePolicy。
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


# LLM: _merge_tool_spec_effect copies the registered ToolSpec effect into the runtime policy.
# 函数用途: 没有 write_boundary 覆盖时，使用工具注册声明作为副作用 gate 事实来源。
def _merge_tool_spec_effect(effects: dict[str, object], tool: BaseTool) -> None:
    spec = getattr(tool, "spec", None)
    name = str(getattr(spec, "name", "") or "")
    effect = str(getattr(spec, "effect", "") or "")
    if name and effect and name not in effects:
        effects[name] = effect


# LLM: boundary_mapping reads a mapping-valued write_boundary field.
# 函数用途: 安全读取工具 gate 的结构化映射配置，字段不是 dict 时返回 None。
def boundary_mapping(boundary: dict[str, object] | None, key: str) -> dict[str, object] | None:
    if not isinstance(boundary, dict):
        return None
    value = boundary.get(key)
    return value if isinstance(value, dict) else None


# LLM: boundary_list reads a list-valued write_boundary field.
# 函数用途: 安全读取 approved_actions 等结构化列表，字段不是 list 时返回空列表。
def boundary_list(boundary: dict[str, object] | None, key: str) -> list[object]:
    if not isinstance(boundary, dict):
        return []
    value = boundary.get(key)
    return value if isinstance(value, list) else []


# LLM: boundary_strings reads list-valued gate policy fields as compact strings.
# 函数用途: 给 URL/path/command gate 提供私网 allowlist 等结构配置。
def boundary_strings(boundary: dict[str, object] | None, key: str) -> list[str]:
    return [str(item) for item in boundary_list(boundary, key) if str(item).strip()]


# LLM: boundary_path_roots merges all trusted path-root grants for pre-tool path checks.
# 函数用途: 让 path gate 使用父级结构化 allowed/product/read roots，而不是只看主 workspace。
def boundary_path_roots(boundary: dict[str, object] | None) -> list[str]:
    roots: list[str] = []
    for key in ("allowed_write_roots", "allowed_read_roots", "product_write_roots", "path_scope"):
        roots.extend(boundary_strings(boundary, key))
    return _dedupe_strings(roots)


# LLM: boundary_bool reads boolean gate policy switches without accepting string prose.
# 函数用途: 只接受真正 bool 字段，避免普通文本绕过 shell 操作符门。
def boundary_bool(boundary: dict[str, object] | None, key: str) -> bool:
    if not isinstance(boundary, dict):
        return False
    return boundary.get(key) is True


# LLM: boundary_text returns a scalar scope field from trusted write_boundary data.
# 函数用途: 读取 run_id 等机器字段，供审批绑定和幂等账本使用。
def boundary_text(boundary: dict[str, object] | None, key: str) -> str:
    if not isinstance(boundary, dict):
        return ""
    return str(boundary.get(key) or "").strip()


# LLM: _dedupe_strings preserves order while dropping empty duplicate roots.
# 函数用途: 合并多种 root grant 时保持稳定输出，避免重复路径膨胀。
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
