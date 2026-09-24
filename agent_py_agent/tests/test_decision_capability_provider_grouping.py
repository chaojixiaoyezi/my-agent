"""能力推荐按结构化 provider_id 把同一插件的工具合成一题：选中时展开为全部成员工具，未选中时整体延迟，不按描述文字归组。"""
from dataclasses import replace

import pytest

from agent_py_agent.agent.backends.typesafe_decision import TypesafeDecisionBackend
from agent_py_agent.agent.backends.typesafe_decision_wire import (
    parse_typesafe_response,
    typesafe_payload,
)
from agent_py_agent.agent.capability import decision_recommendation as module
from agent_py_agent.agent.capability.decision_candidates import (
    group_provider_candidates,
    selection_questions,
)
from agent_py_agent.tests.test_decision_capability_consumer import surface  # noqa: F401
from agent_py_agent.tests.test_tool_presentation_projection import OptionalTool
from agent_py_agent.tests.test_tool_presentation_projection import (
    prepared as tool_surface,  # noqa: F401
)


# 函数用途: 生成一条测试用工具候选行。
def _tool(ref, *, provider="", description="内置工具", version=None):
    row = {"kind": "tool", "ref": ref, "name": ref, "description": description, "category": "plugins",
           "version": version or f"v-{ref}"}
    if provider:
        row["provider_id"] = provider
    return row


def test_grouping_uses_only_structured_provider_id_and_keeps_first_member_position():
    rows = [
        _tool("builtin_a"),
        _tool("alpha_render", provider="plugin:alpha", description="插件 alpha（设计卡片）的 render 工具。"),
        _tool("lookalike", description="插件 alpha（设计卡片）的 lookalike 工具。"),
        _tool("alpha_export", provider="plugin:alpha", description="插件 alpha（设计卡片）的 export 工具。"),
        _tool("beta_lookup", provider="plugin:beta"),
        {"kind": "skill", "ref": "workspace:s", "name": "s", "description": "技能", "when_to_use": "",
         "version": "sv", "tools_required": []},
    ]
    grouped = group_provider_candidates(rows)
    assert [row["ref"] for row in grouped] == ["builtin_a", "plugin:alpha", "lookalike", "plugin:beta", "workspace:s"]
    alpha = grouped[1]
    assert alpha["kind"] == "provider" and alpha["tool_refs"] == ["alpha_render", "alpha_export"]
    assert [tool["name"] for tool in alpha["tools"]] == ["alpha_render", "alpha_export"]
    # 只有结构化 provider_id 决定归属：描述里写着"插件 alpha"的内置工具仍单独成题。
    assert grouped[2]["kind"] == "tool"
    changed = group_provider_candidates([
        {**row, "version": "v-new"} if row["ref"] == "alpha_export" else row for row in rows])
    assert changed[1]["version"] != alpha["version"] and changed[3]["version"] == grouped[3]["version"]
    questions = selection_questions(grouped)
    assert len(questions) == 5
    assert "provider" in questions["candidate_1"]["instructions"]
    assert "provider" not in questions["candidate_0"]["instructions"]


# 函数用途: 构造带结构化插件归属的测试工具，schema 与原测试工具相同。
def _plugin_tool(name: str, provider: str) -> OptionalTool:
    tool = OptionalTool(name)
    tool.model_spec = replace(tool.model_spec, hints=replace(tool.model_spec.hints, provider_id=provider))
    return tool


@pytest.fixture
def plugin_surface(surface):  # noqa: F811
    tools = {"alpha": [_plugin_tool("plugin_alpha_render", "plugin:alpha"),
                       _plugin_tool("plugin_alpha_export", "plugin:alpha")],
             "beta": [_plugin_tool("plugin_beta_lookup", "plugin:beta")]}
    for group in tools.values():
        for tool in group:
            surface.host.tools.register(tool)
    surface.snapshot = surface.host.tools.runtime_snapshot(run_id="capability-run")
    surface.params = replace(surface.params, tool_runtime_snapshot=surface.snapshot)
    surface.plugin_tools = {key: {tool.model_spec.name for tool in group} for key, group in tools.items()}
    return surface


# LLM: 只替换原生 provider 调用；逐题答案仍过正式 wire 校验，记录每次发出的题目。
# 函数用途: 按候选引用回答 include/not_needed，并返回实际发出的题目列表。
def _provider(monkeypatch, desired: set[str]) -> list[dict]:
    sent = []

    def decide(backend, request, *, deadline):
        payload = typesafe_payload(request, backend.model_name)
        sent.append(payload["questions"])
        answers = {}
        for key, question in payload["questions"].items():
            selected = "include" if question["instructions"]["candidate"]["ref"] in desired else "not_needed"
            answers[key] = {"type": "choice", "choice": selected, "confidence": 1.0,
                            "probabilities": {option: float(option == selected) for option in question["criteria"]}}
        return parse_typesafe_response(request, backend.model_name,
                                       {"model": "native-decision", "answers": answers, "usage": {"input_tokens": 41}})

    monkeypatch.setattr(TypesafeDecisionBackend, "decide", decide)
    return sent


def test_selected_plugin_expands_to_all_tools_and_unselected_plugin_stays_hidden(plugin_surface, monkeypatch):
    sent = _provider(monkeypatch, {"plugin:alpha", "presentation_optional_a", "workspace:method-001"})
    result = module.recommend_capabilities(plugin_surface.host, plugin_surface.params, plugin_surface.snapshot,
                                           plugin_surface.contract)
    assert result.finding.endswith("applied"), result.finding
    refs = [question["instructions"]["candidate"]["ref"] for question in sent[0].values()]
    # 三个插件工具只出两题（每个插件一题），不再逐工具出题。
    assert refs.count("plugin:alpha") == refs.count("plugin:beta") == 1
    assert not any(ref in plugin_surface.plugin_tools["alpha"] | plugin_surface.plugin_tools["beta"] for ref in refs)
    shortlist = result.tool_snapshot.presentation_shortlist_names
    deferred = result.tool_snapshot.presentation_deferred_names
    assert plugin_surface.plugin_tools["alpha"] <= shortlist
    assert plugin_surface.plugin_tools["beta"].isdisjoint(shortlist)
    assert deferred is None or plugin_surface.plugin_tools["beta"] <= deferred
