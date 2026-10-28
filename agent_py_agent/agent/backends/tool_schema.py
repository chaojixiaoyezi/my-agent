# LLM: 进程缓存只复用内容hash对应的provider投影；每次先验canonical完整性，返回值不共享可变引用。
# 模块用途: 有界缓存宿主扩展剥离结果，按调用生成隔离的模型工具Schema。
from __future__ import annotations

"""LLM: provider 适配只渲染 run 快照里的 canonical ToolModelSpec。

模块用途: 把统一工具输入结构包装成 Anthropic/OpenAI 原生工具定义，保持模型与运行时一致。
"""

import threading
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from ..contracts.tool_input_schema import EXCLUSIVE_ARGUMENT_GROUPS_KEY

if TYPE_CHECKING:
    from ..tooling.models import ToolModelSpec

_STRIPPED_SCHEMA_CACHE: dict[str, dict[str, Any]] = {}
_STRIPPED_SCHEMA_CACHE_LOCK = threading.Lock()
_STRIPPED_SCHEMA_CACHE_MAX_ENTRIES = 1024


# LLM: 先assert再查hash缓存；miss还须核副本hash，防校验后并发篡改污染旧hash槽，返回再深拷贝。
# 函数用途: 复用宿主扩展剥离结果，保持每次返回可独立修改且不污染canonical或进程缓存。
def tool_model_spec_to_input_schema(spec: ToolModelSpec) -> dict[str, Any]:
    """Return the provider-safe snapshotted schema after verifying its hash."""

    spec.assert_schema_hash()
    with _STRIPPED_SCHEMA_CACHE_LOCK:
        cached = _STRIPPED_SCHEMA_CACHE.get(spec.schema_hash)
        if cached is None:
            cached = deepcopy(spec.input_schema)
            from ..tooling.models import tool_schema_hash

            if tool_schema_hash(cached) != spec.schema_hash:
                raise ValueError(f"tool input_schema mutated during provider snapshot: {spec.name}")
            _strip_host_schema_extensions(cached)
            if len(_STRIPPED_SCHEMA_CACHE) >= _STRIPPED_SCHEMA_CACHE_MAX_ENTRIES:
                _STRIPPED_SCHEMA_CACHE.pop(next(iter(_STRIPPED_SCHEMA_CACHE)))
            _STRIPPED_SCHEMA_CACHE[spec.schema_hash] = cached
    return deepcopy(cached)


# LLM: 用显式栈遍历整棵 schema 以支持嵌套对象声明；仅移除当前宿主拥有的互斥扩展，其它 x-* annotation 保持原行为。
#   只改 provider 副本（调用方已深拷贝），canonical schema 与其 hash 不受影响。
# 函数用途: 从 provider 副本中删除所有层级的运行时专用互斥参数组声明。
def _strip_host_schema_extensions(value: Any) -> None:
    pending = [value]
    while pending:
        pending.extend(_stripped_schema_children(pending.pop()))


# LLM: 单层处理：字典先删宿主扩展键再返回子值，列表返回元素，其它返回空；供 _strip_host_schema_extensions 逐层展开。
# 函数用途: 删掉一个 schema 节点上的宿主扩展，并返回它的下一层子节点。
def _stripped_schema_children(value: Any) -> list[Any]:
    if isinstance(value, dict):
        value.pop(EXCLUSIVE_ARGUMENT_GROUPS_KEY, None)
        return list(value.values())
    return list(value) if isinstance(value, list) else []


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
