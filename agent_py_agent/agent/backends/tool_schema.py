
from __future__ import annotations

"""LLM: provider 适配只渲染 run 快照里的 canonical ToolModelSpec。

模块用途: 把统一工具输入结构包装成 Anthropic/OpenAI 原生工具定义，保持模型与运行时一致。
"""

from copy import deepcopy
from typing import TYPE_CHECKING, Any

from ..contracts.tool_input_schema import EXCLUSIVE_ARGUMENT_GROUPS_KEY

if TYPE_CHECKING:
    from ..tooling.models import ToolModelSpec


# LLM: canonical schema 里的宿主扩展参与哈希与运行时校验，但第三方 provider 不承诺接受未知关键字；这里只剥离精确注册的宿主键，不弱化标准 Schema。
# 函数用途: 校验快照完整性并生成可安全发送给模型服务商的隔离 Schema 副本。
def tool_model_spec_to_input_schema(spec: ToolModelSpec) -> dict[str, Any]:
    """Return the provider-safe snapshotted schema after verifying its hash."""

    spec.assert_schema_hash()
    schema = deepcopy(spec.input_schema)
    _strip_host_schema_extensions(schema)
    return schema


# LLM: 递归遍历是为了支持嵌套对象声明；仅移除当前宿主拥有的互斥扩展，其它 x-* annotation 保持原行为。
# 函数用途: 从 provider 副本递归删除运行时专用的互斥参数组声明。
def _strip_host_schema_extensions(value: Any) -> None:
    if isinstance(value, dict):
        value.pop(EXCLUSIVE_ARGUMENT_GROUPS_KEY, None)
        for child in value.values():
            _strip_host_schema_extensions(child)
    elif isinstance(value, list):
        for child in value:
            _strip_host_schema_extensions(child)


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
