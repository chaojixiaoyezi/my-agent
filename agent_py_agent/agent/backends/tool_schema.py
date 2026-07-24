
from __future__ import annotations

"""LLM: provider 适配只渲染 canonical ToolSpec Schema，不再自行推导另一套参数结构。

模块用途: 把统一工具输入结构包装成 Anthropic/OpenAI 原生工具定义，保持模型与运行时一致。
"""

from typing import TYPE_CHECKING, Any

from ..tooling.tool_spec_schema import tool_spec_input_schema

if TYPE_CHECKING:
    from ..tooling.models import ToolSpec


def tool_spec_to_input_schema(spec: ToolSpec) -> dict[str, Any]:
    """LLM: 该入口只能委托 canonical builder，不能加入 provider 专属的弱校验旁路。

    函数用途: 返回一个工具公开给模型的 JSON Schema。
    """
    return tool_spec_input_schema(spec)


def tool_spec_to_anthropic_tool(spec: ToolSpec) -> dict[str, Any]:
    """LLM: 工具名、描述和输入结构来自同一个 ToolSpec，不执行可用性或授权判断。

    函数用途: 生成 Anthropic tools 数组中的一个元素。
    """

    return {
        "name": spec.name,
        "description": str(spec.description or ""),
        "input_schema": tool_spec_to_input_schema(spec),
    }


def tool_specs_to_anthropic_tools(specs: list[ToolSpec]) -> list[dict[str, Any]]:
    """LLM: 重名工具只保留上游快照中的第一项；函数不得重新扩展工具权限范围。

    函数用途: 把当前运行快照的工具列表转换成 provider 可接收的定义数组。
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
