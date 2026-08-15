from __future__ import annotations

"""Validate and canonicalize the sole model-visible tool input schema.

This module accepts only the finite JSON Schema subset enforced by the runtime.
It contains no legacy tool-contract compilation or parameter-completion logic.
"""

import math
import re
from copy import deepcopy
from typing import Any

from ..contracts.tool_input_schema import (
    ToolInputCoercion,
    ToolInputIssue,
    ToolInputNormalization,
    ToolInputValidation,
    normalize_tool_input,
    validate_tool_input,
)

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
        "definitions",
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
    }
)
_JSON_SCHEMA_TYPES = frozenset(
    {"array", "boolean", "integer", "null", "number", "object", "string"}
)


def canonicalize_tool_input_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return an isolated, validated object schema for provider and runtime use."""

    if not isinstance(schema, dict):
        raise ValueError("tool input_schema must be an object")
    canonical = deepcopy(schema)
    _validate_schema_node(canonical, path="$", depth=0)
    _validate_local_references(canonical)
    raw_type = canonical.get("type")
    if raw_type is None:
        canonical["type"] = "object"
    elif raw_type != "object":
        raise ValueError("tool input_schema top-level type must be object")
    properties = canonical.get("properties")
    if properties is None:
        canonical["properties"] = {}
    elif not isinstance(properties, dict):
        raise ValueError("tool input_schema.properties must be an object")
    required = canonical.get("required")
    if required is not None and not isinstance(required, list):
        raise ValueError("tool input_schema.required must be an array")
    return canonical


__all__ = [
    "ToolInputCoercion",
    "ToolInputIssue",
    "ToolInputNormalization",
    "ToolInputValidation",
    "canonicalize_tool_input_schema",
    "normalize_tool_input",
    "validate_tool_input",
]


def _validate_schema_node(schema: Any, *, path: str, depth: int) -> None:
    if not isinstance(schema, dict):
        raise ValueError(f"{path} must be a schema object")
    if depth > 32:
        raise ValueError(f"{path} schema nesting is too deep")
    unsupported = sorted(
        str(key)
        for key in schema
        if key not in _SCHEMA_KEYS and not str(key).startswith("x-")
    )
    if unsupported:
        raise ValueError(f"{path} has unsupported schema rules: {', '.join(unsupported)}")
    raw_ref = schema.get("$ref")
    if raw_ref is not None and (
        not isinstance(raw_ref, str) or not raw_ref.startswith("#/")
    ):
        raise ValueError(f"{path}.$ref must be a local schema reference")
    raw_type = schema.get("type")
    if raw_type is not None and not (
        isinstance(raw_type, str)
        or (
            isinstance(raw_type, list)
            and raw_type
            and all(isinstance(item, str) for item in raw_type)
        )
    ):
        raise ValueError(f"{path}.type must be a string or non-empty string array")
    declared_types = [raw_type] if isinstance(raw_type, str) else list(raw_type or ())
    invalid_types = sorted(item for item in declared_types if item not in _JSON_SCHEMA_TYPES)
    if invalid_types:
        raise ValueError(f"{path}.type has unknown JSON types: {', '.join(invalid_types)}")
    _validate_constraint_shapes(schema, path)
    _validate_schema_mapping(schema.get("properties"), f"{path}.properties", depth)
    _validate_schema_mapping(schema.get("$defs"), f"{path}.$defs", depth)
    _validate_schema_mapping(schema.get("definitions"), f"{path}.definitions", depth)
    for keyword in ("items", "additionalProperties"):
        child = schema.get(keyword)
        if isinstance(child, dict):
            _validate_schema_node(child, path=f"{path}.{keyword}", depth=depth + 1)
        elif child is not None and not (
            keyword == "additionalProperties" and isinstance(child, bool)
        ):
            raise ValueError(f"{path}.{keyword} must be a schema object")
    for keyword in ("allOf", "anyOf", "oneOf"):
        branches = schema.get(keyword)
        if branches is None:
            continue
        if not isinstance(branches, list) or not branches:
            raise ValueError(f"{path}.{keyword} must be a non-empty schema array")
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
        raise ValueError(f"{path}.required must contain non-empty field names")


def _validate_schema_mapping(value: Any, path: str, depth: int) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object")
    for name, child in value.items():
        _validate_schema_node(child, path=f"{path}.{name}", depth=depth + 1)


def _validate_local_references(root_schema: dict[str, Any]) -> None:
    pending: list[tuple[str, dict[str, Any], int]] = [("$", root_schema, 0)]
    while pending:
        path, schema, depth = pending.pop()
        if depth > 32:
            raise ValueError(f"{path} schema nesting is too deep")
        if "$ref" in schema:
            _validate_local_reference_chain(schema, root_schema, path)
        for child_path, child in _schema_children(schema, path):
            pending.append((child_path, child, depth + 1))


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
            raise ValueError(f"{path}.$ref forms an alias cycle")
        seen.add(raw_ref)
        target = _local_reference_target(root_schema, raw_ref)
        if target is None:
            raise ValueError(f"{path}.$ref targets a missing or non-schema node")
        current = target
    raise ValueError(f"{path}.$ref chain is too deep")


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


def _validate_constraint_shapes(schema: dict[str, Any], path: str) -> None:
    enum = schema.get("enum")
    if enum is not None and (not isinstance(enum, list) or not enum):
        raise ValueError(f"{path}.enum must be a non-empty array")
    pattern = schema.get("pattern")
    if pattern is not None and not isinstance(pattern, str):
        raise ValueError(f"{path}.pattern must be a string")
    if isinstance(pattern, str):
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"{path}.pattern must be a valid regular expression") from exc
    nullable = schema.get("nullable")
    if nullable is not None and not isinstance(nullable, bool):
        raise ValueError(f"{path}.nullable must be boolean")
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
            raise ValueError(f"{path}.{keyword} must be a non-negative integer")
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
            raise ValueError(f"{path}.{keyword} must be numeric")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{path}.{keyword} must be finite")
    multiple = schema.get("multipleOf")
    if (
        isinstance(multiple, (int, float))
        and not isinstance(multiple, bool)
        and multiple <= 0
    ):
        raise ValueError(f"{path}.multipleOf must be greater than zero")


__all__ = ["canonicalize_tool_input_schema"]
