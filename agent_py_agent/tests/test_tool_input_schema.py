from __future__ import annotations

"""完整工具输入 Schema 的纯合同回归，不启动模型、网络或真实副作用。"""

import pytest

from agent_py_agent.agent.contracts.tool_input_schema import (
    normalize_tool_input,
    validate_tool_input,
)
from agent_py_agent.agent.tooling.models import ToolSpec
from agent_py_agent.agent.tooling.tool_spec_schema import (
    tool_spec_input_schema,
    tool_spec_runtime_input_schema,
)


def test_normalizer_recurses_and_only_applies_unambiguous_conversions() -> None:
    schema = {
        "type": "object",
        "properties": {
            "count": {"type": "integer"},
            "enabled": {"type": "boolean"},
            "rows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"score": {"type": "number"}},
                    "required": ["score"],
                    "additionalProperties": False,
                },
            },
            "label": {"type": ["string", "null"]},
        },
        "additionalProperties": False,
    }

    result = normalize_tool_input(
        {
            "count": "42",
            "enabled": "false",
            "rows": '[{"score":"1.5"}]',
            "label": "null",
        },
        schema,
    )

    assert result.value == {
        "count": 42,
        "enabled": False,
        "rows": [{"score": 1.5}],
        "label": "null",
    }
    assert [item.to_dict() for item in result.coercions] == [
        {"path": "$.count", "source_type": "string", "target_type": "integer"},
        {"path": "$.enabled", "source_type": "string", "target_type": "boolean"},
        {"path": "$.rows", "source_type": "string", "target_type": "array"},
        {"path": "$.rows[0].score", "source_type": "string", "target_type": "number"},
    ]


def test_normalizer_does_not_wrap_scalar_or_guess_noncanonical_numbers() -> None:
    schema = {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": {"type": "string"}},
            "count": {"type": "integer"},
        },
        "additionalProperties": False,
    }

    result = normalize_tool_input({"items": "one", "count": "01"}, schema)

    assert result.value == {"items": "one", "count": "01"}
    assert result.coercions == ()


def test_numeric_validation_and_coercion_remain_bounded() -> None:
    schema = {
        "type": "object",
        "properties": {
            "count": {"type": "integer", "multipleOf": 1},
        },
        "additionalProperties": False,
    }
    huge_integer = 10**1000
    oversized_text = "9" * 5000

    assert validate_tool_input({"count": huge_integer}, schema).ok is True
    normalized = normalize_tool_input({"count": oversized_text}, schema)
    assert normalized.value == {"count": oversized_text}
    assert normalized.coercions == ()


def test_validator_enforces_nested_rules_refs_and_closed_objects() -> None:
    schema = {
        "type": "object",
        "$defs": {
            "row": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["fast", "safe"]},
                    "score": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["mode", "score"],
                "additionalProperties": False,
            }
        },
        "properties": {
            "rows": {
                "type": "array",
                "minItems": 1,
                "maxItems": 2,
                "items": {"$ref": "#/$defs/row"},
            }
        },
        "required": ["rows"],
        "additionalProperties": False,
    }

    result = validate_tool_input(
        {
            "rows": [{"mode": "other", "score": 2, "extra": True}],
            "unknown": "x",
        },
        schema,
    )

    assert result.ok is False
    keywords = {item.keyword for item in result.issues}
    assert {"enum", "maximum", "additionalProperties"} <= keywords
    assert result.primary_error_code == "TOOL_INVALID_ARGUMENTS"


def test_validator_distinguishes_missing_and_type_errors() -> None:
    schema = {
        "type": "object",
        "properties": {"limit": {"type": "integer"}},
        "required": ["limit"],
        "additionalProperties": False,
    }

    missing = validate_tool_input({}, schema)
    wrong = validate_tool_input({"limit": "ten"}, schema)

    assert missing.primary_error_code == "TOOL_PARAMETER_REQUIRED"
    assert wrong.primary_error_code == "TOOL_PARAMETER_TYPE_INVALID"


def test_tool_spec_public_and_runtime_schema_share_one_source() -> None:
    spec = ToolSpec(
        name="demo",
        category="test",
        description="demo",
        use_cases=[],
        avoid_when=[],
        keywords=[],
        parameters={"value": "值"},
        parameter_schema={"value": {"type": "integer", "minimum": 1}},
        required_parameters=["value"],
        internal_parameters=["__scope"],
    )

    public = tool_spec_input_schema(spec)
    runtime = tool_spec_runtime_input_schema(spec)

    assert public["properties"]["value"]["minimum"] == 1
    assert public["additionalProperties"] is False
    assert "__scope" not in public["properties"]
    assert runtime["properties"]["__scope"] == {}


def test_explicit_schema_rejects_unimplemented_assertions_and_external_refs() -> None:
    base = {
        "name": "demo",
        "category": "test",
        "description": "demo",
        "use_cases": [],
        "avoid_when": [],
        "keywords": [],
        "parameters": {},
    }
    unsupported = ToolSpec(
        **base,
        input_schema={"type": "object", "properties": {}, "uniqueItems": True},
    )
    external_ref = ToolSpec(
        **base,
        input_schema={"type": "object", "$ref": "https://example.com/schema.json"},
    )
    missing_ref = ToolSpec(
        **base,
        input_schema={
            "type": "object",
            "properties": {"value": {"$ref": "#/$defs/missing"}},
        },
    )
    cyclic_ref = ToolSpec(
        **base,
        input_schema={
            "type": "object",
            "$defs": {
                "left": {"$ref": "#/$defs/right"},
                "right": {"$ref": "#/$defs/left"},
            },
            "properties": {"value": {"$ref": "#/$defs/left"}},
        },
    )
    invalid_pattern = ToolSpec(
        **base,
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string", "pattern": "("}},
        },
    )

    with pytest.raises(ValueError, match="未支持"):
        tool_spec_input_schema(unsupported)
    with pytest.raises(ValueError, match="本地引用"):
        tool_spec_input_schema(external_ref)
    with pytest.raises(ValueError, match="不存在"):
        tool_spec_input_schema(missing_ref)
    with pytest.raises(ValueError, match="循环别名"):
        tool_spec_input_schema(cyclic_ref)
    with pytest.raises(ValueError, match="有效正则"):
        tool_spec_input_schema(invalid_pattern)


def test_local_reference_alias_chain_is_fully_resolved() -> None:
    schema = {
        "type": "object",
        "$defs": {
            "count": {"type": "integer", "minimum": 2},
            "count_alias": {"$ref": "#/$defs/count"},
        },
        "properties": {"count": {"$ref": "#/$defs/count_alias"}},
        "required": ["count"],
        "additionalProperties": False,
    }
    spec = ToolSpec(
        name="demo",
        category="test",
        description="demo",
        use_cases=[],
        avoid_when=[],
        keywords=[],
        parameters={},
        input_schema=schema,
    )

    canonical = tool_spec_input_schema(spec)
    normalized = normalize_tool_input({"count": "2"}, canonical)

    assert normalized.value == {"count": 2}
    assert validate_tool_input(normalized.value, canonical).ok is True
    assert validate_tool_input({"count": 1}, canonical).ok is False
