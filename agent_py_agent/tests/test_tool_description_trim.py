from __future__ import annotations

"""工具说明精简（只删重复文字）合同单测：字段、类型、枚举、必填和顶层权威说明都不变。

- create_subagents：items[] 里与顶层同名同义的字段只写“同顶层 X。”，本项专属的 goal/description/role、covers 和
  input_media_refs（第 14 条，开关打开时才有）照旧；开关关闭时说明里不出现 input_media_refs。
- remember：batch 单项的 scope 只写“同顶层 scope。”，结构与顶层 scope 完全一致。
- user_config：points.<点>.profile_id 只写“同 profile_id”，changes.profile_id 保留完整说明。
"""

import json
from copy import deepcopy

import pytest

from agent_py_agent.agent.agent_core.orchestration.tool_spec_data import (
    _CREATE_ITEM_PARAMETER_DETAILS,
    _CREATE_PARAMETER_DETAILS,
)
from agent_py_agent.agent.agent_core.orchestration.tool_specs import (
    build_create_subagents_model_spec,
)
from agent_py_agent.agent.capability.memory_tool import build_remember_model_spec
from agent_py_agent.agent.tooling.user_config_tool import UserConfigTool


# 函数用途: 去掉一棵 Schema 里所有 description，只比结构、类型与枚举。
def _without_descriptions(value):
    if isinstance(value, dict):
        return {key: _without_descriptions(item) for key, item in value.items() if key != "description"}
    if isinstance(value, list):
        return [_without_descriptions(item) for item in value]
    return value


@pytest.mark.parametrize("input_media", [False, True])
def test_create_subagents_items_point_back_to_top_level_without_changing_shapes(input_media):
    spec = build_create_subagents_model_spec(input_media=input_media)
    schema = spec.input_schema
    top = schema["properties"]
    items = top["items"]["items"]["properties"]
    assert ("input_media_refs" in json.dumps(schema, ensure_ascii=False)) is input_media, "开关关闭时说明里不出现 input_media_refs"
    for name, shape in items.items():
        if name in _CREATE_ITEM_PARAMETER_DETAILS:
            assert shape["description"] == _CREATE_ITEM_PARAMETER_DETAILS[name]
            continue
        if name in {"covers", "input_media_refs"}:
            assert shape["description"] == top[name]["description"], f"{name} 在 item 里填，保留完整说明"
            continue
        reference = f"同顶层 {name}。"
        full = top[name]["description"]
        assert shape["description"] == (reference if len(full) > len(reference) else full), name
        assert _without_descriptions(shape) == _without_descriptions(top[name]), name
    assert top["covers"]["description"] == _CREATE_PARAMETER_DETAILS["covers"]
    assert top["items"]["items"]["required"] == ["goal"]


def test_remember_batch_scope_is_the_top_level_scope_without_repeated_text():
    schema = build_remember_model_spec().input_schema
    top_scope = schema["properties"]["scope"]
    batch_scope = schema["properties"]["operations"]["items"]["properties"]["scope"]
    assert batch_scope["description"] == "同顶层 scope。"
    assert all("description" not in shape for shape in batch_scope["properties"].values())
    assert _without_descriptions(batch_scope) == _without_descriptions(top_scope)
    assert "personal/personal" in top_scope["description"]
    assert top_scope["properties"]["scope_key"]["description"]


def test_user_config_point_profile_ids_reference_the_shared_field():
    changes = deepcopy(UserConfigTool.model_spec.input_schema["properties"]["changes"]["properties"])
    point_ids = [path for path in changes if path.startswith("points.") and path.endswith(".profile_id")]
    assert point_ids
    for path in point_ids:
        assert changes[path] == {"type": "string", "description": "同 profile_id，只作用于本接入点。"}
    assert "Decision 编号" in changes["profile_id"]["description"]
