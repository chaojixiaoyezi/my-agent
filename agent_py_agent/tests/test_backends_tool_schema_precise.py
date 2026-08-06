from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends.tool_schema import tool_model_spec_to_input_schema
from agent_py_agent.agent.tooling.models import (
    EffectResolverPolicy,
    ToolModelSpec,
    ToolRuntime,
    ToolRuntimePolicy,
    ToolRuntimeSnapshot,
)


def _model_spec() -> ToolModelSpec:
    return ToolModelSpec(
        name="typed_tool",
        description="Exercise the exact canonical provider schema.",
        input_schema={
            "type": "object",
            "properties": {
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "URL 数组",
                },
                "mode": {
                    "type": "string",
                    "enum": ["auto", "extract"],
                    "description": "读取模式",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "最大条数",
                },
            },
            "required": ["urls"],
            "additionalProperties": False,
        },
    )


def test_provider_receives_the_exact_canonical_schema() -> None:
    spec = _model_spec()

    schema = tool_model_spec_to_input_schema(spec)

    assert schema == spec.input_schema
    assert schema["properties"]["urls"]["items"] == {"type": "string"}
    assert schema["properties"]["mode"]["enum"] == ["auto", "extract"]
    assert schema["required"] == ["urls"]


def test_provider_schema_is_an_isolated_copy() -> None:
    spec = _model_spec()
    projected = tool_model_spec_to_input_schema(spec)

    projected["properties"]["limit"]["minimum"] = 99

    assert spec.input_schema["properties"]["limit"]["minimum"] == 1


def test_model_spec_rejects_non_object_and_unsupported_schemas() -> None:
    with pytest.raises(ValueError, match="top-level type"):
        ToolModelSpec("bad", "bad schema", {"type": "string"})
    with pytest.raises(ValueError, match="unsupported schema rules"):
        ToolModelSpec(
            "bad_keyword",
            "bad schema",
            {"type": "object", "properties": {}, "unevaluatedProperties": False},
        )


def test_schema_hash_detects_post_snapshot_mutation() -> None:
    spec = _model_spec()
    spec.input_schema["properties"]["limit"]["minimum"] = 0

    with pytest.raises(ValueError, match="mutated after snapshot"):
        tool_model_spec_to_input_schema(spec)


def test_runtime_snapshot_rechecks_schema_hash_before_lookup() -> None:
    spec = _model_spec()
    tool = SimpleNamespace(
        model_spec=spec,
        runtime_policy=ToolRuntimePolicy(EffectResolverPolicy("read_only")),
    )
    snapshot = ToolRuntimeSnapshot(
        run_id="run-1",
        runtimes=(ToolRuntime(spec, tool.runtime_policy, tool),),
        available_tool_names=frozenset({spec.name}),
        unavailable_tools=(),
        allowed_tools=None,
    )
    spec.input_schema["properties"]["limit"]["minimum"] = 0

    with pytest.raises(ValueError, match="mutated after snapshot"):
        snapshot.runtime(spec.name)
