
from __future__ import annotations

"""LLM: provider 适配只渲染 run 快照里的 canonical ToolModelSpec。

模块用途: 把统一工具输入结构包装成 Anthropic/OpenAI 原生工具定义，保持模型与运行时一致。
"""

from copy import deepcopy
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..tooling.models import ToolModelSpec


def tool_model_spec_to_input_schema(spec: ToolModelSpec) -> dict[str, Any]:
    """Return the exact snapshotted schema after verifying its integrity hash."""

    spec.assert_schema_hash()
    return deepcopy(spec.input_schema)


def tool_model_spec_to_anthropic_tool(spec: ToolModelSpec) -> dict[str, Any]:
    """Render one provider definition without deriving or weakening its schema."""

    return {
        "name": spec.name,
        "description": spec.description,
        "input_schema": tool_model_spec_to_input_schema(spec),
    }


def tool_model_specs_to_anthropic_tools(
    specs: list[ToolModelSpec],
) -> list[dict[str, Any]]:
    """Render the ordered, duplicate-free model surface fixed by the run snapshot."""

    tools: list[dict[str, Any]] = []
    seen: set[str] = set()
    for spec in specs:
        if spec.name in seen:
            raise ValueError(f"duplicate tool in provider surface: {spec.name}")
        seen.add(spec.name)
        tools.append(tool_model_spec_to_anthropic_tool(spec))
    return tools


__all__ = [
    "tool_model_spec_to_anthropic_tool",
    "tool_model_spec_to_input_schema",
    "tool_model_specs_to_anthropic_tools",
]
