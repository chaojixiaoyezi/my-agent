# LLM: 进程缓存只复用内容hash对应的provider投影；每次先验canonical完整性，返回值不共享可变引用。
# 模块用途: 有界缓存宿主扩展剥离结果，按调用生成隔离的模型工具Schema。
from __future__ import annotations

"""LLM: provider 适配只渲染 run 快照里的 canonical ToolModelSpec。

模块用途: 把统一工具输入结构包装成 Anthropic/OpenAI 原生工具定义，保持模型与运行时一致。
"""

import threading
from collections import deque
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from ..contracts.tool_input_schema import EXCLUSIVE_ARGUMENT_GROUPS_KEY

if TYPE_CHECKING:
    from ..tooling.models import ToolModelSpec

_STRIPPED_SCHEMA_CACHE: dict[Any, dict[str, Any]] = {}
_STRIPPED_SCHEMA_CACHE_LOCK = threading.Lock()
_STRIPPED_SCHEMA_CACHE_MAX_ENTRIES = 1024


# LLM: 递归提取剥离宿主扩展后的字典键顺序指纹；同内容不同键序不能共用同一缓存槽，否则破坏工具参数顺序确定性。
# 函数用途: 计算工具输入 Schema 的各级字典键顺序签名，与内容 hash 一起作为缓存键。
def _schema_key_order(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(
            (key, _schema_key_order(child))
            for key, child in value.items()
            if key != EXCLUSIVE_ARGUMENT_GROUPS_KEY
        )
    if isinstance(value, list):
        return tuple(_schema_key_order(item) for item in value)
    return None


# LLM: 先assert再查(hash,键序)LRU缓存；miss还须核副本hash与键序，防校验后并发篡改污染旧槽，返回再深拷贝。
# 函数用途: 复用宿主扩展剥离结果并保持原始属性键顺序，每次返回可独立修改且不污染canonical或进程缓存。
def tool_model_spec_to_input_schema(spec: ToolModelSpec) -> dict[str, Any]:
    """Return the provider-safe snapshotted schema after verifying its hash."""

    spec.assert_schema_hash()
    cache_key = (spec.schema_hash, _schema_key_order(spec.input_schema))
    with _STRIPPED_SCHEMA_CACHE_LOCK:
        cached = _STRIPPED_SCHEMA_CACHE.pop(cache_key, None)
        if cached is not None:
            _STRIPPED_SCHEMA_CACHE[cache_key] = cached
        else:
            cached = deepcopy(spec.input_schema)
            from ..tooling.models import tool_schema_hash

            if tool_schema_hash(cached) != spec.schema_hash or _schema_key_order(cached) != cache_key[1]:
                raise ValueError(f"tool input_schema mutated during provider snapshot: {spec.name}")
            _strip_host_schema_extensions(cached)
            cached = _prune_unreachable_defs(cached)
            while len(_STRIPPED_SCHEMA_CACHE) >= _STRIPPED_SCHEMA_CACHE_MAX_ENTRIES:
                _STRIPPED_SCHEMA_CACHE.pop(next(iter(_STRIPPED_SCHEMA_CACHE)))
            _STRIPPED_SCHEMA_CACHE[cache_key] = cached
    return deepcopy(cached)


# LLM: 严格核对普通 '#/$defs/<Name>' 指针，任何异常/跨指针形态即整包退回，确保绝不误删合法定义。
# 函数用途: 收集单个节点内所有可解析到根 $defs 的本地引用名；遇到非普通引用或缺失目标返回 None。
def _collect_plain_def_refs(node: Any, defs: dict[str, Any]) -> list[str] | None:
    refs: list[str] = []
    stack: list[Any] = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            if "$ref" in current:
                raw_ref = current["$ref"]
                if not isinstance(raw_ref, str) or not raw_ref.startswith("#/$defs/"):
                    return None
                target = raw_ref[8:]
                if not target or "/" in target or "~" in target or target not in defs:
                    return None
                refs.append(target)
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return refs


# LLM: 仅保留从根 $defs 外部出发沿普通 '#/$defs/<Name>' 闭包可达的条目，空 $defs 直接移除以消除冗余上下文。
# 函数用途: 裁剪 provider 工具输入 Schema 中不可达的根级 $defs；非安全引用形态下原样返回。
def _prune_unreachable_defs(schema: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(schema, dict) or "$defs" not in schema:
        return schema
    defs = schema.get("$defs")
    if not isinstance(defs, dict):
        return schema

    def_edges: dict[str, list[str]] = {}
    for name, sub_schema in defs.items():
        edges = _collect_plain_def_refs(sub_schema, defs)
        if edges is None:
            return schema
        def_edges[name] = edges

    root_without_defs = {key: value for key, value in schema.items() if key != "$defs"}
    root_refs = _collect_plain_def_refs(root_without_defs, defs)
    if root_refs is None:
        return schema

    reachable: set[str] = set()
    queue = deque(root_refs)
    while queue:
        name = queue.popleft()
        if name in reachable:
            continue
        reachable.add(name)
        for dep in def_edges.get(name, ()):
            if dep not in reachable:
                queue.append(dep)

    pruned_defs = {name: value for name, value in defs.items() if name in reachable}
    out = dict(schema)
    if pruned_defs:
        out["$defs"] = pruned_defs
    else:
        out.pop("$defs", None)
    return out


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
        "description": str(spec.description or "").strip() or f"Tool {spec.name}",
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
