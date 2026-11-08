"""R5B-W6：create_subagents 工具说明每次构造只读一次角色模板索引，结果与逐键替换一致。"""
from __future__ import annotations

import json

from agent_py_agent.agent.agent_core.orchestration import tool_specs
from agent_py_agent.agent.subagents import role_templates


def _count_index_loads(monkeypatch):
    calls = []
    original = role_templates.load_role_template_store

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(role_templates, "load_role_template_store", counted)
    return calls


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def test_create_subagents_spec_loads_role_templates_once(monkeypatch):
    calls = _count_index_loads(monkeypatch)
    spec = tool_specs.build_create_subagents_model_spec()
    assert len(calls) == 1
    assert "{role_template_index}" not in json.dumps(spec.input_schema, ensure_ascii=False)
    index = role_templates.role_template_index_text()
    assert index and any(index in text for text in _strings(spec.input_schema))


def test_role_template_index_replacement_matches_per_key_replacement(monkeypatch):
    monkeypatch.setattr(tool_specs, "role_template_index_text", lambda: "<<INDEX>>")
    details = {**tool_specs._CREATE_PARAMETER_DETAILS, "plain": "无占位符"}
    expected = {key: value.replace("{role_template_index}", "<<INDEX>>") for key, value in details.items()}
    assert tool_specs._with_role_template_index(details) == expected


def test_details_without_placeholder_do_not_touch_template_store(monkeypatch):
    calls = _count_index_loads(monkeypatch)
    assert tool_specs._with_role_template_index({"a": "x", "b": "y"}) == {"a": "x", "b": "y"}
    assert calls == []
