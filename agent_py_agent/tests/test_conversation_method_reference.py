# LLM: 合成安装、原线程锁和真实 RuntimeFacts 投递验证引用降级，不修改授权、pins 或生产文件。
# 模块用途: 守住正文缺席时的完整重读参数、总预算、资料过滤和每代一次。
import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.capability import method_carry as module
from agent_py_agent.agent.capability.package_snapshot import package_read_parameters
from agent_py_agent.agent.capability.skill_search_tool import SkillSearchTool
from agent_py_agent.agent.memory_archive import estimate_tokens
from agent_py_agent.tests.test_conversation_method_carry import method_fixture, read_package, records
from agent_py_agent.tests.test_conversation_method_restore import carry, compact


def _reference(arguments):
    return "本会话用过的方法，正文这次没带回；需要时用 skill_search 重读：" + json.dumps(
        arguments, ensure_ascii=False, separators=(",", ":"))


def _arguments(text):
    line = next(line for line in text.splitlines() if "正文这次没带回" in line)
    return json.loads(line.split("重读：", 1)[1])


@pytest.mark.parametrize("oversized", [False, True])
def test_skill_empty_or_oversized_fragment_falls_back_to_complete_reference_once(tmp_path, monkeypatch, oversized):
    fixture = method_fixture(tmp_path)
    skill = fixture.scope.skills.enabled_entries()[0]
    assert SkillSearchTool(fixture.agent).execute({"action": "get", "skill_id": skill.stable_id}).ok
    compact(fixture)
    monkeypatch.setattr(module, "_prepare_skill_method", lambda *_args: "正文" * 4000 if oversized else "")
    turns = carry(fixture, 500)
    assert len(turns) == 1
    assert _arguments(turns[0].text) == {"action": "get", "skill_id": skill.stable_id}
    assert "本会话用过" in turns[0].text and "正文正文" not in turns[0].text
    assert estimate_tokens(turns[0].text) <= 500 and records(fixture)[0]["carried_generation"] == 1
    fixture.params = replace(fixture.params, conversation_method_carry_attempts=set())
    fixture.agent._current_run_params = fixture.params
    assert carry(fixture, 500) == turns


@pytest.mark.parametrize("with_resources", [False, True])
def test_package_reference_keeps_current_get_pins_and_only_affordable_resource_list(tmp_path, monkeypatch, with_resources):
    fixture = method_fixture(tmp_path, resources=1)
    assert read_package(fixture, path="methods/0.md").ok
    package, = fixture.scope.skills.packages
    compact(fixture)
    monkeypatch.setattr(module, "_prepare_package_method", lambda *_args: "")
    arguments = package_read_parameters(package.to_ref())
    budget = 3000 if with_resources else estimate_tokens(module._CARRY_HEADER + "\n\n" + _reference(arguments)) + 4
    turns = carry(fixture, budget)
    assert len(turns) == 1 and _arguments(turns[0].text) == arguments
    assert ("压缩前读过的包内资料" in turns[0].text) is with_resources
    if with_resources:
        resources = json.loads(turns[0].text.split("压缩前读过的包内资料：\n", 1)[1])
        assert resources == [{"resource_path": "methods/0.md", "next_read": {**arguments, "resource_path": "methods/0.md"}}]
    assert "METHOD_ENTRY_0" not in turns[0].text and "RESOURCE_BODY_0" not in turns[0].text
    assert estimate_tokens(turns[0].text) <= budget and records(fixture)[0]["carried_generation"] == 1


def test_reference_line_and_separators_must_fit_total_budget_before_consuming_generation(tmp_path, monkeypatch):
    fixture = method_fixture(tmp_path)
    assert read_package(fixture).ok
    compact(fixture)
    package, = fixture.scope.skills.packages
    arguments = package_read_parameters(package.to_ref())
    budget = estimate_tokens(module._CARRY_HEADER + "\n\n" + _reference(arguments)) - 1
    monkeypatch.setattr(module, "_prepare_package_method", lambda *_args: "")
    assert carry(fixture, budget) == []
    assert records(fixture)[0]["carried_generation"] == 0


def test_failed_reference_runtime_facts_delivery_does_not_consume_generation(tmp_path, monkeypatch):
    fixture = method_fixture(tmp_path)
    assert read_package(fixture).ok
    compact(fixture)
    monkeypatch.setattr(module, "_prepare_package_method", lambda *_args: "")
    monkeypatch.setattr("agent_py_agent.agent.agent_core.tool_ir_history.record_runtime_facts_turn_ir", lambda *_a, **_kw: False)
    assert carry(fixture) == [] and records(fixture)[0]["carried_generation"] == 0


def test_missing_current_package_has_no_reference_or_generation_write(tmp_path):
    fixture = method_fixture(tmp_path)
    assert read_package(fixture).ok
    compact(fixture)
    fixture.agent._current_skill_snapshot = replace(fixture.scope.skills, packages=())
    assert carry(fixture) == [] and records(fixture)[0]["carried_generation"] == 0


def test_package_reference_filters_removed_resource_paths(tmp_path, monkeypatch):
    fixture = method_fixture(tmp_path, resources=1)
    assert read_package(fixture, path="methods/0.md").ok
    store, tid = fixture.agent.conversation_store, fixture.link.thread_id
    prior = dict(records(fixture)[0], resource_paths=["removed.md", "methods/0.md"])
    store.threads.update_atomic(tid, lambda thread: replace(thread, conversation_methods=(prior,)))
    compact(fixture)
    monkeypatch.setattr(module, "_prepare_package_method", lambda *_args: "")
    turns = carry(fixture)
    assert len(turns) == 1
    text = turns[0].text
    assert "removed.md" not in text and '"resource_path":"methods/0.md"' in text
