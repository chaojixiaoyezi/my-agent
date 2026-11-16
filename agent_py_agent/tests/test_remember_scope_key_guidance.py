# 模块用途: remember 的 scope/scope_key 说明与修复提示写明 global 与 <类型>:<id> 的必填形式；
#   校验本身不变：缺键或裸 id 仍在写候选前拒绝，typed key 照常进入候选链。只用 tmp 隔离 home。
from __future__ import annotations

import json

import pytest

from agent_py_agent.agent.capability.memory_tool import RememberTool, build_remember_model_spec
from agent_py_agent.tests.test_memory_tool import _agent_with_current_user


def test_scope_schema_names_the_required_typed_forms():
    properties = build_remember_model_spec().input_schema["properties"]
    batch_scope = properties["operations"]["items"]["properties"]["scope"]
    for scope in (properties["scope"], batch_scope):
        key = scope["properties"]["scope_key"]["description"]
        assert "global 写 global" in key and "<类型>:<id>" in key and "project:moneywise" in key
    assert "global" in properties["scope"]["description"].split("宿主绑定真实 thread；")[1]


@pytest.mark.parametrize("scope", [
    {"scope_type": "global"},
    {"scope_type": "project"},
    {"scope_type": "project", "scope_key": "moneywise"},
])
def test_missing_or_raw_scope_key_is_still_rejected_with_the_typed_form(tmp_path, scope):
    agent = _agent_with_current_user(tmp_path)
    result = RememberTool(agent).execute({"content": "moneywise 项目使用 UTC 保存时间", "kind": "project",
        "origin": "user_explicit", "subject_key": "project.moneywise.timezone", "scope": scope})
    assert result.ok is False and result.error_code == "TOOL_INVALID_ARGUMENTS"
    assert result.effect_outcome == "not_started"
    hint = json.loads(result.output)["hint"]
    assert "个人记忆请省略 scope" in hint and '"global"' in hint and '"project:<id>"' in hint
    assert agent.memory_candidates.list() == []


def test_typed_project_scope_key_still_reaches_the_candidate_chain(tmp_path):
    agent = _agent_with_current_user(tmp_path)
    result = RememberTool(agent).execute({"content": "moneywise 项目使用 UTC 保存时间", "kind": "project",
        "origin": "user_explicit", "subject_key": "project.moneywise.timezone",
        "scope": {"scope_type": "project", "scope_key": "project:moneywise"}})
    assert result.error_code != "TOOL_INVALID_ARGUMENTS", result.output
    [candidate] = agent.memory_candidates.list()
    assert (candidate.scope["scope_type"], candidate.scope["scope_key"]) == ("project", "project:moneywise")
