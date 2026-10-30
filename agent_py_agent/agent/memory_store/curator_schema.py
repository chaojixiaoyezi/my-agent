# LLM: Schema 构造独立于解析和运行时，各供应商共用同一严格合同；候选链接只选择已有锚点，不产生新的身份或权威。
# 模块用途: 构造 Curator 严格输出 Schema 并递归校验模型响应，不授予工具或记忆写入权限。
from __future__ import annotations

"""Memory Curator provider 结构化输出 Schema 构造器。"""

from collections.abc import Collection
from typing import Any


# LLM: 模型只能选 message_id；角色、精确有界引文、哈希、运行 ID 等权威字段始终由宿主从 ConversationStore 投影。
# 函数用途: 构造模型可提交的单字段消息引用。
def _message_reference_schema() -> dict[str, Any]:
    return _strict_object({"message_id": {"type": "string"}})


# LLM: 审计 event_id 足以绑定工具状态、调用、操作和产物元数据；模型不得在响应中重复填写或伪造这些规范字段。
# 函数用途: 构造最小工具事件引用。
def _tool_reference_schema() -> dict[str, Any]:
    return _strict_object({"event_id": {"type": "string"}})


# LLM: 产物身份按输入中精确 artifact_ref 选择，大小、哈希和路径仍由宿主从对应审计事件投影。
# 函数用途: 构造最小大内容引用。
def _artifact_reference_schema() -> dict[str, Any]:
    return _strict_object({"artifact_ref": {"type": "string"}})


# LLM: 已处理声明只携带所属流的身份，不能向游标验证夹带证据或正文字段。
# 函数用途: 构造 processed message/audit 的单字段严格引用。
def _processed_reference_schema(key: str) -> dict[str, Any]:
    return _strict_object({key: {"type": "string"}})


# LLM: 宿主校验时每条未解决项必须恰指一条流；两个可空字段仍完整声明，避免依赖供应商特有的 oneOf 方言。
# 函数用途: 构造 unresolved message/audit 引用。
def _unresolved_reference_schema() -> dict[str, Any]:
    nullable_string = {"type": ["string", "null"]}
    return _strict_object(
        {
            "message_id": nullable_string,
            "event_id": nullable_string,
        }
    )


# LLM: 每个 Curator 对象都拒绝额外字段并要求完整声明字段集，即使某个可空值有意留空也不能省略键。
# 函数用途: 统一构造递归 strict 的对象 Schema。
def _strict_object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


# LLM: 范围是由宿主验证的类型化对象；适用或排除条件的文字不能扩大其范围键。
# 函数用途: 构造 Candidate scope 的严格字段和枚举。
def _scope_schema() -> dict[str, Any]:
    nullable_string = {"type": ["string", "null"]}
    return _strict_object(
        {
            "scope_type": {
                "type": "string",
                "enum": [
                    "global",
                    "personal",
                    "company",
                    "project",
                    "task_class",
                    "session",
                    "temporary",
                ],
            },
            "scope_key": {"type": "string"},
            "applies_when": nullable_string,
            "excludes_when": nullable_string,
        }
    )


# LLM: 日报草稿只开放摘要与引用字段，不开放宿主排序身份，模型不能选择序号、event_id 或 curator_run_id。
# 函数用途: 构造 DailyMemoryEvent 草稿的严格 Schema。
def _daily_schema(
    message_reference: dict[str, Any],
    tool_reference: dict[str, Any],
    artifact_reference: dict[str, Any],
    *,
    origins: Collection[str],
) -> dict[str, Any]:
    short_strings = {"type": "array", "items": {"type": "string"}}
    properties: dict[str, Any] = {
        "event_type": {"type": "string"},
        "summary": {"type": "string"},
        "actor": {"type": "string"},
        "origin": {"type": "string", "enum": sorted(origins)},
        "message_refs": {"type": "array", "items": message_reference},
        "tool_refs": {"type": "array", "items": tool_reference},
        "artifact_refs": {"type": "array", "items": artifact_reference},
        "decisions": short_strings,
        "lessons": short_strings,
        "next_actions": short_strings,
    }
    return _strict_object(properties)


# LLM: 候选Schema仅含提案字段；link_candidate_id只能选择输入已有锚点，candidate_id、状态和审核者仍由宿主独占，须同步检查提示字段清单。
# 函数用途: 构造统一候选观察及可空锚点链接的严格Schema，不向模型开放候选身份或状态写入权。
def _candidate_schema(
    message_reference: dict[str, Any],
    tool_reference: dict[str, Any],
    artifact_reference: dict[str, Any],
    *,
    candidate_types: Collection[str],
    origins: Collection[str],
    actions: Collection[str],
    promotion_targets: Collection[str],
) -> dict[str, Any]:
    nullable_string = {"type": ["string", "null"]}
    strings = {"type": "array", "items": {"type": "string"}}
    properties: dict[str, Any] = {
        "candidate_type": {"type": "string", "enum": sorted(candidate_types)},
        "content": {"type": "string"},
        "subject_key": {"type": "string"},
        "scope": _scope_schema(),
        "origin": {"type": "string", "enum": sorted(origins)},
        "source_message_refs": {"type": "array", "items": message_reference},
        "source_tool_refs": {"type": "array", "items": tool_reference},
        "source_artifact_refs": {"type": "array", "items": artifact_reference},
        "observed_at": nullable_string,
        "valid_from": nullable_string,
        "valid_until": nullable_string,
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "proposed_action": {"type": "string", "enum": sorted(actions)},
        "target_entry_id": nullable_string,
        "link_candidate_id": nullable_string,
        "conflicts_with": strings,
        "promotion_target": {"type": "string", "enum": sorted(promotion_targets)},
    }
    return _strict_object(properties)


# LLM: 顶层构造器接收规范模型模块提供的版本和枚举，避免重复业务常量与循环导入。
# 函数用途: 组装所有 provider 共用的 Curator strict response Schema。
def build_curator_response_schema(
    *,
    output_version: str,
    candidate_types: Collection[str],
    origins: Collection[str],
    actions: Collection[str],
    promotion_targets: Collection[str],
) -> dict[str, Any]:
    message_reference = _message_reference_schema()
    tool_reference = _tool_reference_schema()
    artifact_reference = _artifact_reference_schema()
    properties: dict[str, Any] = {
        "schema_version": {"type": "string", "enum": [output_version]},
        "daily_events": {
            "type": "array",
            "items": _daily_schema(
                message_reference,
                tool_reference,
                artifact_reference,
                origins=origins,
            ),
        },
        "candidates": {
            "type": "array",
            "items": _candidate_schema(
                message_reference,
                tool_reference,
                artifact_reference,
                candidate_types=candidate_types,
                origins=origins,
                actions=actions,
                promotion_targets=promotion_targets,
            ),
        },
        "processed_message_refs": {
            "type": "array",
            "items": _processed_reference_schema("message_id"),
        },
        "processed_audit_refs": {
            "type": "array",
            "items": _processed_reference_schema("event_id"),
        },
        "unresolved_refs": {"type": "array", "items": _unresolved_reference_schema()},
        "warnings": {"type": "array", "items": {"type": "string"}},
        "next_cursor": _cursor_schema(),
    }
    return _strict_object(properties)


# LLM: 模型输出不合合同时抛出：仍是 ValueError（curator._failure_code 照旧归 CURATOR_SCHEMA_INVALID，消息原文不变），
#   另带两个结构化字段供失败诊断：violation_code 是宿主写死的短码，violation_path 只由 schema 字段名和下标拼成
#   （JSONPath，不含模型写的键名或值；不知道下标的嵌套字段写 $..<字段名>）。改动同步 curator._violation_facts 与
#   test_curator_failure_attribution.py。
# 类用途: 标记“模型输出哪一项不合格”，不带任何正文。
class CuratorOutputViolation(ValueError):
    # LLM: 保持原 ValueError 消息及分类不变；违规码与路径由宿主校验器提供，不把模型正文当作诊断权威。
    # 函数用途: 保存原消息、违规码和字段路径。
    def __init__(self, message: str, *, code: str, path: str) -> None:
        super().__init__(message)
        self.violation_code = code
        self.violation_path = path


# LLM: Anthropic 兼容供应商可能只通过提示词收到 Schema；宿主必须执行相同递归规则，不能替模型填补漏字段。
# 函数用途: 依据 Curator 自有 JSON Schema 校验一次 provider 输出，拒绝缺字段、额外字段和坏类型。
def validate_curator_response_payload(
    payload: object,
    schema: dict[str, Any],
) -> None:
    _validate_schema_node(payload, schema, path="$", depth=0)


# LLM: 仅支持本模块构造的 Schema 关键字；未知关键字必须报错，不能使宿主校验静默弱于供应商约束。
# 函数用途: 递归核对 object/array/string/number/null、enum、required 和数值上下界。
def _validate_schema_node(
    value: object,
    schema: dict[str, Any],
    *,
    path: str,
    depth: int,
) -> None:
    if depth > 32:
        raise CuratorOutputViolation("curator response schema nesting is too deep", code="nesting_too_deep", path=path)
    allowed_keywords = {
        "type",
        "enum",
        "minimum",
        "maximum",
        "additionalProperties",
        "required",
        "properties",
        "items",
    }
    unknown = set(schema) - allowed_keywords
    if unknown:
        raise RuntimeError(f"unsupported curator schema keyword: {sorted(unknown)[0]}")
    if not _schema_type_matches(value, schema.get("type")):
        raise CuratorOutputViolation(f"curator response field {path} has an invalid type", code="invalid_type", path=path)
    if "enum" in schema and value not in schema["enum"]:
        raise CuratorOutputViolation(f"curator response field {path} is outside the allowed enum", code="enum_mismatch", path=path)
    if isinstance(value, dict):
        _validate_schema_object(value, schema, path=path, depth=depth)
    elif isinstance(value, list):
        item_schema = schema.get("items")
        if not isinstance(item_schema, dict):
            raise RuntimeError("curator array schema lacks items")
        for index, item in enumerate(value):
            _validate_schema_node(
                item,
                item_schema,
                path=f"{path}[{index}]",
                depth=depth + 1,
            )
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        _validate_number_bounds(value, schema, path=path)


# LLM: 严格对象要求可空属性也必须有键，且拒绝全部未声明属性，与结构化输出供应商的规则一致。
# 函数用途: 校验一个 Curator JSON 对象的字段全集并递归验证属性。
def _validate_schema_object(
    value: dict[object, object],
    schema: dict[str, Any],
    *,
    path: str,
    depth: int,
) -> None:
    properties = schema.get("properties")
    required = schema.get("required")
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise RuntimeError("curator object schema lacks properties or required")
    if any(not isinstance(key, str) for key in value):
        raise CuratorOutputViolation(f"curator response field {path} contains a non-string key", code="non_string_key", path=path)
    names = set(value)
    missing = set(required) - names
    extra = names - set(properties)
    if missing:
        # 缺的字段名来自 schema 的 required，可以进路径；多出来的字段名是模型写的，只记所在对象。
        raise CuratorOutputViolation(f"curator response field {path} is missing {sorted(missing)[0]}",
                                     code="missing_property", path=f"{path}.{sorted(missing)[0]}")
    if extra and schema.get("additionalProperties") is False:
        raise CuratorOutputViolation(f"curator response field {path} contains unknown {sorted(extra)[0]}",
                                     code="unknown_property", path=path)
    for name, child_schema in properties.items():
        if name not in value:
            continue
        if not isinstance(child_schema, dict):
            raise RuntimeError("curator property schema must be an object")
        _validate_schema_node(
            value[name],
            child_schema,
            path=f"{path}.{name}",
            depth=depth + 1,
        )


# LLM: 即使 Python 的 bool 继承 int，JSON 布尔值也不能匹配数字；可空字段仅在显式包含 null 类型时接受空值。
# 函数用途: 判断一个 JSON 值是否满足 schema type 或联合 type。
def _schema_type_matches(value: object, expected: object) -> bool:
    choices = expected if isinstance(expected, list) else [expected]
    return any(_single_schema_type_matches(value, item) for item in choices)


# LLM: 类型匹配只接受 build_curator_response_schema 实际生成的类型，未知类型必须报错。
# 函数用途: 匹配一个基础 JSON Schema 类型。
def _single_schema_type_matches(value: object, expected: object) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "null":
        return value is None
    raise RuntimeError(f"unsupported curator schema type: {expected}")


# LLM: 即使供应商忽略 JSON Schema 数值约束，置信度上下界仍由宿主强制执行。
# 函数用途: 校验 number 的 minimum/maximum。
def _validate_number_bounds(
    value: int | float,
    schema: dict[str, Any],
    *,
    path: str,
) -> None:
    if "minimum" in schema and value < schema["minimum"]:
        raise CuratorOutputViolation(f"curator response field {path} is below minimum", code="below_minimum", path=path)
    if "maximum" in schema and value > schema["maximum"]:
        raise CuratorOutputViolation(f"curator response field {path} is above maximum", code="above_maximum", path=path)


# LLM: 模型游标输出只作有界建议；处理与未解决声明经过宿主校验后，最终游标仍以宿主计算为准。
# 函数用途: 构造 next_cursor 的严格 Schema。
def _cursor_schema() -> dict[str, Any]:
    cursor_item = {
        "type": "object",
        "additionalProperties": False,
        "required": ["thread_id", "message_id"],
        "properties": {
            "thread_id": {"type": "string"},
            "message_id": {"type": "string"},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["per_thread_cursors", "last_audit_event_id"],
        "properties": {
            "per_thread_cursors": {"type": "array", "items": cursor_item},
            "last_audit_event_id": {"type": ["string", "null"]},
        },
    }


__all__ = ["build_curator_response_schema", "validate_curator_response_payload"]
