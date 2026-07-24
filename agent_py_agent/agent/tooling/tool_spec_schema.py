from __future__ import annotations

"""LLM: 把 ToolSpec 编译成模型展示与执行校验共用的唯一输入 Schema，不执行工具或权限判断。

模块用途: 统一内置工具旧参数声明、MCP 完整 Schema、内部参数和调用协议字段的边界，避免展示与执行漂移。
"""

import math
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from ..contracts.tool_input_schema import ToolInputCoercion, normalize_tool_input
from ..contracts.tool_protocol_v2 import execution_payload_for_tool_protocol
from .models import ToolSpec

_SCHEMA_KEYS = frozenset(
    {
        "$comment",
        "$defs",
        "$id",
        "$ref",
        "$schema",
        "additionalProperties",
        "allOf",
        "anyOf",
        "const",
        "contentEncoding",
        "contentMediaType",
        "default",
        "deprecated",
        "description",
        "enum",
        "examples",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "format",
        "items",
        "maximum",
        "maxItems",
        "maxLength",
        "maxProperties",
        "minimum",
        "minItems",
        "minLength",
        "minProperties",
        "multipleOf",
        "nullable",
        "oneOf",
        "pattern",
        "properties",
        "readOnly",
        "required",
        "title",
        "type",
        "writeOnly",
        "definitions",
    }
)
_JSON_SCHEMA_TYPES = frozenset(
    {"array", "boolean", "integer", "null", "number", "object", "string"}
)
# LLM: 归一化结果保留完整协议 payload 与无敏感值的类型纠正记录，调用方不得再另做参数转换。
# 类用途: 把执行入口需要的规范参数和审计信息一起返回。
@dataclass(frozen=True)
class NormalizedToolPayload:
    payload: dict[str, Any]
    coercions: tuple[ToolInputCoercion, ...] = ()


# LLM: 模型与运行时都必须从此函数取得 ToolSpec 的公开结构；不得在 backend 再拼第三套 Schema。
# 函数用途: 生成一个工具给模型看的完整 JSON Schema，不包含宿主内部注入字段。
def tool_spec_input_schema(spec: ToolSpec) -> dict[str, Any]:
    explicit = getattr(spec, "input_schema", None)
    if explicit is not None:
        if not isinstance(explicit, dict):
            raise ValueError("tool input_schema 必须是对象")
        return _canonical_explicit_schema(explicit)
    schema = _legacy_schema(spec)
    _validate_schema_node(schema, path="$", depth=0)
    _validate_local_references(schema)
    return schema


# LLM: 运行时结构仅在公开 Schema 上加入 ToolSpec 明示的内部字段，不能把协议字段或任意 __ 字段放开。
# 函数用途: 生成执行前校验使用的 Schema，让合法宿主字段通过但仍拒绝模型虚构参数。
def tool_spec_runtime_input_schema(spec: ToolSpec) -> dict[str, Any]:
    schema = tool_spec_input_schema(spec)
    internal = tuple(
        dict.fromkeys(
            str(item).strip()
            for item in (getattr(spec, "internal_parameters", None) or ())
            if str(item).strip()
        )
    )
    if not internal:
        return schema
    runtime = deepcopy(schema)
    properties = runtime.get("properties")
    if not isinstance(properties, dict):
        properties = {}
        runtime["properties"] = properties
    for name in internal:
        properties.setdefault(name, {})
    return runtime


# LLM: 类型纠正发生在所有参数/路径/副作用门之前，只依据当前工具的运行时 Schema 且不原地改 payload。
# 函数用途: 提取真实工具参数，做保守强类型转换，再与原协议字段合成规范调用。
def normalize_tool_payload_for_spec(
    payload: dict[str, Any],
    spec: ToolSpec,
) -> NormalizedToolPayload:
    schema = tool_spec_runtime_input_schema(spec)
    declared_fields = tuple(str(key) for key in (schema.get("properties") or {}))
    canonical = execution_payload_for_tool_protocol(
        payload,
        declared_input_fields=declared_fields,
    )
    if not isinstance(canonical, dict):
        raise ValueError("tool execution payload 必须是对象")
    arguments = canonical.get("input")
    if not isinstance(arguments, dict):
        raise ValueError("tool execution payload.input 必须是对象")
    normalized = normalize_tool_input(arguments, schema)
    value = normalized.value if isinstance(normalized.value, dict) else arguments
    protocol = {
        key: item
        for key, item in canonical.items()
        if key not in {"tool_name", "input"}
    }
    return NormalizedToolPayload(
        {"tool": canonical.get("tool_name"), **protocol, **value},
        normalized.coercions,
    )


# LLM: 完整外部 Schema 必须描述 object 参数；空 Schema 明确表示开放 object，不猜成零参数工具。
# 函数用途: 深复制 MCP 等外部声明并补齐工具调用所需的对象外壳。
def _canonical_explicit_schema(schema: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(schema)
    _validate_schema_node(result, path="$", depth=0)
    _validate_local_references(result)
    raw_type = result.get("type")
    if raw_type is None:
        result["type"] = "object"
    elif raw_type != "object":
        raise ValueError("tool input_schema 顶层 type 必须是 object")
    properties = result.get("properties")
    if properties is None:
        result["properties"] = {}
    elif not isinstance(properties, dict):
        raise ValueError("tool input_schema.properties 必须是对象")
    required = result.get("required")
    if required is not None and not isinstance(required, list):
        raise ValueError("tool input_schema.required 必须是数组")
    return result


# LLM: 外部 Schema 只接受当前运行时能完整执行的有限规则；未知扩展注解可保留，未知断言不得静默弱化。
# 函数用途: 在 MCP 工具注册/展示/执行前检查 Schema 形状和本地引用边界。
def _validate_schema_node(schema: Any, *, path: str, depth: int) -> None:
    if not isinstance(schema, dict):
        raise ValueError(f"{path} 必须是 Schema 对象")
    if depth > 32:
        raise ValueError(f"{path} Schema 嵌套过深")
    unsupported = sorted(
        str(key)
        for key in schema
        if key not in _SCHEMA_KEYS and not str(key).startswith("x-")
    )
    if unsupported:
        raise ValueError(f"{path} 包含未支持的 Schema 规则: {', '.join(unsupported)}")
    raw_ref = schema.get("$ref")
    if raw_ref is not None and (
        not isinstance(raw_ref, str) or not raw_ref.startswith("#/")
    ):
        raise ValueError(f"{path}.$ref 只允许当前 Schema 内的本地引用")
    raw_type = schema.get("type")
    if raw_type is not None and not (
        isinstance(raw_type, str)
        or (
            isinstance(raw_type, list)
            and raw_type
            and all(isinstance(item, str) for item in raw_type)
        )
    ):
        raise ValueError(f"{path}.type 必须是字符串或非空字符串数组")
    declared_types = [raw_type] if isinstance(raw_type, str) else list(raw_type or ())
    invalid_types = sorted(item for item in declared_types if item not in _JSON_SCHEMA_TYPES)
    if invalid_types:
        raise ValueError(f"{path}.type 包含未知 JSON 类型: {', '.join(invalid_types)}")
    _validate_constraint_shapes(schema, path)
    _validate_schema_mapping(schema.get("properties"), f"{path}.properties", depth)
    _validate_schema_mapping(schema.get("$defs"), f"{path}.$defs", depth)
    _validate_schema_mapping(schema.get("definitions"), f"{path}.definitions", depth)
    for keyword in ("items", "additionalProperties"):
        child = schema.get(keyword)
        if isinstance(child, dict):
            _validate_schema_node(child, path=f"{path}.{keyword}", depth=depth + 1)
        elif child is not None and not (keyword == "additionalProperties" and isinstance(child, bool)):
            raise ValueError(f"{path}.{keyword} 必须是 Schema 对象")
    for keyword in ("allOf", "anyOf", "oneOf"):
        branches = schema.get(keyword)
        if branches is None:
            continue
        if not isinstance(branches, list) or not branches:
            raise ValueError(f"{path}.{keyword} 必须是非空 Schema 数组")
        for index, branch in enumerate(branches):
            _validate_schema_node(
                branch,
                path=f"{path}.{keyword}[{index}]",
                depth=depth + 1,
            )
    required = schema.get("required")
    if required is not None and (
        not isinstance(required, list)
        or not all(isinstance(item, str) and item for item in required)
    ):
        raise ValueError(f"{path}.required 必须是非空字段名数组")


# LLM: properties/$defs 的每个值都必须继续是受支持 Schema，不能让畸形外部节点绕过递归校验。
# 函数用途: 检查 Schema 子节点映射并递归验证每个条目。
def _validate_schema_mapping(value: Any, path: str, depth: int) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise ValueError(f"{path} 必须是对象")
    for name, child in value.items():
        _validate_schema_node(
            child,
            path=f"{path}.{name}",
            depth=depth + 1,
        )


# LLM: 本地引用在工具注册时必须指向真实 Schema，纯别名环会使运行时约束消失，因此一律拒绝。
# 函数用途: 遍历所有 Schema 位置并验证每条本地 JSON Pointer 引用存在、类型正确且不会形成别名环。
def _validate_local_references(root_schema: dict[str, Any]) -> None:
    pending: list[tuple[str, dict[str, Any], int]] = [("$", root_schema, 0)]
    while pending:
        path, schema, depth = pending.pop()
        if depth > 32:
            raise ValueError(f"{path} Schema 嵌套过深")
        if "$ref" in schema:
            _validate_local_reference_chain(schema, root_schema, path)
        for child_path, child in _schema_children(schema, path):
            pending.append((child_path, child, depth + 1))


# LLM: 递归对象可以在子字段重新引用自身，但 A->$ref B->$ref A 这种无约束别名环必须 fail-closed。
# 函数用途: 沿一个节点顶层的连续 $ref 检查目标，直到抵达具有实际结构的 Schema。
def _validate_local_reference_chain(
    schema: dict[str, Any],
    root_schema: dict[str, Any],
    path: str,
) -> None:
    current = schema
    seen: set[str] = set()
    for _ in range(33):
        raw_ref = current.get("$ref")
        if not isinstance(raw_ref, str):
            return
        if raw_ref in seen:
            raise ValueError(f"{path}.$ref 形成循环别名")
        seen.add(raw_ref)
        target = _local_reference_target(root_schema, raw_ref)
        if target is None:
            raise ValueError(f"{path}.$ref 指向不存在或非 Schema 的本地节点")
        current = target
    raise ValueError(f"{path}.$ref 引用链过深")


# LLM: 只遍历 JSON Schema 的子 Schema 位置；default/enum 等业务数据里的 $ref 字样不是引用。
# 函数用途: 为本地引用检查列出 properties、definitions、items、additionalProperties 和组合分支。
def _schema_children(
    schema: dict[str, Any],
    path: str,
) -> list[tuple[str, dict[str, Any]]]:
    children: list[tuple[str, dict[str, Any]]] = []
    for keyword in ("properties", "$defs", "definitions"):
        mapping = schema.get(keyword)
        if isinstance(mapping, dict):
            children.extend(
                (f"{path}.{keyword}.{name}", child)
                for name, child in mapping.items()
                if isinstance(child, dict)
            )
    for keyword in ("items", "additionalProperties"):
        child = schema.get(keyword)
        if isinstance(child, dict):
            children.append((f"{path}.{keyword}", child))
    for keyword in ("allOf", "anyOf", "oneOf"):
        branches = schema.get(keyword)
        if isinstance(branches, list):
            children.extend(
                (f"{path}.{keyword}[{index}]", child)
                for index, child in enumerate(branches)
                if isinstance(child, dict)
            )
    return children


# LLM: JSON Pointer 解码只读当前根对象，不读取文件、网络或环境；任何非对象目标都视为无效 Schema。
# 函数用途: 把 #/... 本地引用解析为目标 Schema 对象。
def _local_reference_target(
    root_schema: dict[str, Any],
    raw_ref: str,
) -> dict[str, Any] | None:
    if not raw_ref.startswith("#/"):
        return None
    current: Any = root_schema
    for raw_part in raw_ref[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current if isinstance(current, dict) else None


# LLM: Schema 自身的边界值也必须强类型，畸形 MCP 声明不能被 provider/runtime 各自宽松解释。
# 函数用途: 检查枚举、正则、长度和数值约束的声明形状。
def _validate_constraint_shapes(schema: dict[str, Any], path: str) -> None:
    enum = schema.get("enum")
    if enum is not None and (not isinstance(enum, list) or not enum):
        raise ValueError(f"{path}.enum 必须是非空数组")
    pattern = schema.get("pattern")
    if pattern is not None and not isinstance(pattern, str):
        raise ValueError(f"{path}.pattern 必须是字符串")
    if isinstance(pattern, str):
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"{path}.pattern 不是有效正则") from exc
    nullable = schema.get("nullable")
    if nullable is not None and not isinstance(nullable, bool):
        raise ValueError(f"{path}.nullable 必须是布尔值")
    for keyword in (
        "maxItems",
        "maxLength",
        "maxProperties",
        "minItems",
        "minLength",
        "minProperties",
    ):
        value = schema.get(keyword)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            raise ValueError(f"{path}.{keyword} 必须是非负整数")
    for keyword in (
        "exclusiveMaximum",
        "exclusiveMinimum",
        "maximum",
        "minimum",
        "multipleOf",
    ):
        value = schema.get(keyword)
        if value is not None and (
            not isinstance(value, (int, float)) or isinstance(value, bool)
        ):
            raise ValueError(f"{path}.{keyword} 必须是数字")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{path}.{keyword} 必须是有限数字")
    multiple = schema.get("multipleOf")
    if isinstance(multiple, (int, float)) and not isinstance(multiple, bool) and multiple <= 0:
        raise ValueError(f"{path}.multipleOf 必须大于 0")


# LLM: 内置旧声明只在此处兼容编译，默认封闭额外字段；后续迁移不能再复制这段推导。
# 函数用途: 把 parameters、parameter_schema、required_parameters 合成完整对象 Schema。
def _legacy_schema(spec: ToolSpec) -> dict[str, Any]:
    overrides = getattr(spec, "parameter_schema", None) or {}
    properties: dict[str, Any] = {}
    for name, description in (getattr(spec, "parameters", None) or {}).items():
        override = overrides.get(name)
        if isinstance(override, dict) and override:
            prop = deepcopy(override)
            prop.setdefault("description", str(description or ""))
            properties[name] = prop
        else:
            properties[name] = {"type": "string", "description": str(description or "")}
    required = [
        name
        for name in (getattr(spec, "required_parameters", None) or ())
        if name in properties
    ]
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(dict.fromkeys(required))
    return schema


__all__ = [
    "NormalizedToolPayload",
    "normalize_tool_payload_for_spec",
    "tool_spec_input_schema",
    "tool_spec_runtime_input_schema",
]
