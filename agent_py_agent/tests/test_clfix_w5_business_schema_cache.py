"""W5: business validation schemas are cached read-only closures; thaw keeps its output with an exact-type fast path."""
from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

import pytest

from agent_py_agent.agent.plugin_host import business_action_specs as specs
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.plugin_host.schemas import freeze, thaw


class _Mapping(Mapping):
    def __init__(self, data):
        self._data = data

    def __getitem__(self, key):
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)


def test_validation_schema_is_cached_and_matches_the_public_copy():
    spec = specs.business_action_spec("spaces.list")
    first = specs._validation_schema(spec._record_json, False)
    assert specs._validation_schema(spec._record_json, False) is first
    assert first == spec.result_schema
    field = specs.business_result_field_schema("spaces.list", "items", True)
    assert specs.business_result_field_schema("spaces.list", "items", True) is field
    assert field == specs.standalone_schema(spec.result_schema["properties"]["items"]["items"])


def test_mutating_a_public_schema_copy_does_not_change_validation():
    spec = specs.business_action_spec("spaces.list")
    copy = spec.result_schema
    copy["properties"].clear()
    copy["required"] = ["missing_field"]
    assert spec.result_schema != copy
    with pytest.raises(HostAPIError):
        spec.validate_result({"unexpected": True})


def test_thaw_output_is_unchanged_for_frozen_and_mapping_inputs():
    value = {"a": [1, {"b": (2, 3)}, MappingProxyType({"c": None})], "d": "x", "e": 1.5, "f": True}
    assert thaw(freeze(value)) == {"a": [1, {"b": [2, 3]}, {"c": None}], "d": "x", "e": 1.5, "f": True}
    assert thaw(_Mapping({"k": _Mapping({"n": (1,)})})) == {"k": {"n": [1]}}
    assert thaw(MappingProxyType({"x": [MappingProxyType({})]})) == {"x": [{}]}


def test_catalog_record_validation_builds_each_route_schema_once(monkeypatch, tmp_path):
    specs.business_result_field_schema.cache_clear()
    calls = []
    original = specs.standalone_schema
    monkeypatch.setattr(specs, "standalone_schema", lambda schema: calls.append(1) or original(schema))
    counts = []
    for _ in range(10):
        specs.business_result_field_schema("spaces.list", "items", True)
        specs.business_result_field_schema("interfaces.get", "binding")
        specs.business_result_field_schema("assets.get", "asset")
        specs.business_result_field_schema("assets.versions.list", "items", True)
        counts.append(len(calls))
    assert counts[0] > 0 and set(counts) == {counts[0]}
