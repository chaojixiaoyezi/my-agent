
from __future__ import annotations

"""把内部 ToolSpec 弱推导成 Anthropic 原生 tool_use 的 input_schema。

内部 ToolSpec.parameters 只有 `{参数名: 中文描述}`，没有类型信息。Anthropic
`/v1/messages` 的 tools 数组要求每个工具带 `input_schema`（JSON Schema）。这里做
最保守的弱推导：每个参数声明成 `string` 并带上中文描述，required 一律留空，避免在
缺类型信息时误拦合法调用。下游工具执行仍按现有 dict 协议处理，类型由各工具自己校验。
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..tooling.models import ToolSpec


def tool_spec_to_input_schema(spec: ToolSpec) -> dict[str, Any]:
    """Derive a conservative JSON Schema for one tool's parameters.

    Every parameter becomes ``{"type": "string", "description": <中文描述>}`` and
    ``required`` is intentionally omitted (all optional) because the source specs
    carry no type or required-ness metadata.
    """

    properties: dict[str, Any] = {}
    for name, description in spec.parameters.items():
        properties[name] = {"type": "string", "description": str(description or "")}
    return {"type": "object", "properties": properties}


def tool_spec_to_anthropic_tool(spec: ToolSpec) -> dict[str, Any]:
    """Render one ToolSpec as an Anthropic ``tools`` array entry."""

    return {
        "name": spec.name,
        "description": str(spec.description or ""),
        "input_schema": tool_spec_to_input_schema(spec),
    }


def tool_specs_to_anthropic_tools(specs: list[ToolSpec]) -> list[dict[str, Any]]:
    """Render a list of ToolSpecs as the Anthropic ``tools`` payload array.

    Duplicate tool names are dropped (Anthropic rejects duplicates); the first
    occurrence wins so explicit ordering upstream is preserved.
    """

    tools: list[dict[str, Any]] = []
    seen: set[str] = set()
    for spec in specs:
        if spec.name in seen:
            continue
        seen.add(spec.name)
        tools.append(tool_spec_to_anthropic_tool(spec))
    return tools


__all__ = [
    "tool_spec_to_anthropic_tool",
    "tool_spec_to_input_schema",
    "tool_specs_to_anthropic_tools",
]
