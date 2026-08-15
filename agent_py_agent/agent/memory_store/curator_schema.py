from __future__ import annotations

"""Memory Curator provider 结构化输出 Schema 构造器。"""

# LLM: Schema construction is isolated from parsing/runtime so every provider receives one
# immutable contract without growing the Curator service into a second protocol layer.
# 模块用途: 分段构造递归 strict 的 Curator JSON Schema，避免各 provider 出现字段分叉。

from collections.abc import Collection
from typing import Any


# LLM: The provider selects only message_id; role, exact bounded quote, hash, runtime IDs, and
# every other authoritative field are always projected from ConversationStore by the host.
# 函数用途: 构造模型可提交的单字段消息引用。
def _message_reference_schema() -> dict[str, Any]:
    return _strict_object({"message_id": {"type": "string"}})


# LLM: An audit event_id is sufficient to bind tool status/call/operation/artifact metadata; the
# model must not repeat or forge those canonical fields in its response.
# 函数用途: 构造最小工具事件引用。
def _tool_reference_schema() -> dict[str, Any]:
    return _strict_object({"event_id": {"type": "string"}})


# LLM: Artifact identity is selected by exact input artifact_ref, while size/hash/path remain
# host-owned projections from the corresponding audit event.
# 函数用途: 构造最小大内容引用。
def _artifact_reference_schema() -> dict[str, Any]:
    return _strict_object({"artifact_ref": {"type": "string"}})


# LLM: Processed declarations carry only their stream identity; they cannot smuggle evidence or
# content fields into cursor validation.
# 函数用途: 构造 processed message/audit 的单字段严格引用。
def _processed_reference_schema(key: str) -> dict[str, Any]:
    return _strict_object({key: {"type": "string"}})


# LLM: An unresolved item must name exactly one stream at host validation time; both nullable
# fields remain present so strict providers do not need a provider-specific oneOf dialect.
# 函数用途: 构造 unresolved message/audit 引用。
def _unresolved_reference_schema() -> dict[str, Any]:
    nullable_string = {"type": ["string", "null"]}
    return _strict_object(
        {
            "message_id": nullable_string,
            "event_id": nullable_string,
        }
    )


# LLM: Every Curator object is closed and requires its complete declared field set, even when a
# nullable value is intentionally empty.
# 函数用途: 统一构造递归 strict 的对象 Schema。
def _strict_object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


# LLM: Scope is a typed host-validated object; applies/excludes text never expands its key.
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


# LLM: Daily drafts expose summary/reference fields only and omit every host-owned ordering
# identity so a model cannot choose sequence, event_id, or curator_run_id.
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


# LLM: Candidate Schema contains proposal fields only; candidate_id/status/reviewer remain
# exclusively host-owned regardless of model or provider.
# 函数用途: 构造统一候选观察的严格 Schema。
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
        "conflicts_with": strings,
        "promotion_target": {"type": "string", "enum": sorted(promotion_targets)},
    }
    return _strict_object(properties)


# LLM: The top-level builder takes version/enums from the canonical models module, avoiding
# duplicated business constants and circular imports.
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


# LLM: Anthropic-compatible providers may only receive the schema through the prompt, so the
# host must enforce the exact same recursive contract instead of filling omitted fields.
# 函数用途: 依据 Curator 自有 JSON Schema 校验一次 provider 输出，拒绝缺字段、额外字段和坏类型。
def validate_curator_response_payload(
    payload: object,
    schema: dict[str, Any],
) -> None:
    _validate_schema_node(payload, schema, path="$", depth=0)


# LLM: This deliberately supports only keywords emitted by this module; accepting an unknown
# schema keyword would make the host validator silently weaker than the provider contract.
# 函数用途: 递归核对 object/array/string/number/null、enum、required 和数值上下界。
def _validate_schema_node(
    value: object,
    schema: dict[str, Any],
    *,
    path: str,
    depth: int,
) -> None:
    if depth > 32:
        raise ValueError("curator response schema nesting is too deep")
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
        raise ValueError(f"curator response field {path} has an invalid type")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"curator response field {path} is outside the allowed enum")
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


# LLM: Strict objects require every nullable property to be present and reject every undeclared
# property; this mirrors structured-output providers exactly.
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
        raise ValueError(f"curator response field {path} contains a non-string key")
    names = set(value)
    missing = set(required) - names
    extra = names - set(properties)
    if missing:
        raise ValueError(
            f"curator response field {path} is missing {sorted(missing)[0]}"
        )
    if extra and schema.get("additionalProperties") is False:
        raise ValueError(
            f"curator response field {path} contains unknown {sorted(extra)[0]}"
        )
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


# LLM: JSON booleans are not numbers here even though Python bool subclasses int; nullable
# fields match only an explicit null member.
# 函数用途: 判断一个 JSON 值是否满足 schema type 或联合 type。
def _schema_type_matches(value: object, expected: object) -> bool:
    choices = expected if isinstance(expected, list) else [expected]
    return any(_single_schema_type_matches(value, item) for item in choices)


# LLM: Type matching stays closed over the types emitted by build_curator_response_schema.
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


# LLM: Confidence bounds are host-enforced even when a provider ignores JSON Schema numeric
# constraints.
# 函数用途: 校验 number 的 minimum/maximum。
def _validate_number_bounds(
    value: int | float,
    schema: dict[str, Any],
    *,
    path: str,
) -> None:
    if "minimum" in schema and value < schema["minimum"]:
        raise ValueError(f"curator response field {path} is below minimum")
    if "maximum" in schema and value > schema["maximum"]:
        raise ValueError(f"curator response field {path} is above maximum")


# LLM: Model cursor output is advisory and structurally bounded; host calculation remains
# authoritative after exact processed/unresolved coverage validation.
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
