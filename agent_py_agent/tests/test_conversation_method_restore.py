# LLM: 合成安装和真实 store Compact 提交验证带回，模型/生产均不接；RuntimeFacts 是实际上下文观察点。
# 模块用途: 守住每代一次、原读取准入、资料过滤、预算和成功投递后记账。
from dataclasses import fields, replace

import pytest

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.backends.tool_ir import RuntimeFactsTurn
from agent_py_agent.agent.capability import method_carry as module
from agent_py_agent.agent.capability.runtime_config_reload import capability_config_path_for
from agent_py_agent.agent.capability.skill_search_tool import SkillSearchTool
from agent_py_agent.agent.common.cancellation import ToolCancelled
from agent_py_agent.agent.conversation.models import ConversationCompactCommit
from agent_py_agent.agent.memory_archive import estimate_tokens
from agent_py_agent.tests.test_conversation_method_carry import (
    method_fixture,
    read_package,
    records,
    switch,
)


def compact(fixture):
    threads, tid = fixture.agent.conversation_store.threads, fixture.link.thread_id
    previous = threads.require(tid)
    commit = ConversationCompactCommit("合成摘要", {}, f"checkpoint-{previous.compact_generation + 1}", "", 0, 0, 0)
    return threads.update_compact_state(tid, commit=commit, expected_generation=previous.compact_generation)


def carry(fixture, budget=3000):
    path = capability_config_path_for(fixture.agent)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"conversation_method_carry_enabled: true\ncapability_bundle_max_tokens: {budget}\n", encoding="utf-8")
    if not isinstance(fixture.params, ToolLoopExecuteParams):
        values = {field.name: getattr(fixture.params, field.name) for field in fields(ToolLoopExecuteParams)
                  if hasattr(fixture.params, field.name)}
        fixture.params = ToolLoopExecuteParams(
            **{**dict(user_prompt="核对资料", memories=[], runtime_injections=[], prompt_files=[], tool_catalog_section="",
                      tool_recommendations_section="", tool_context=[], effective_on_chunk=None,
                      one_shot_tool_calls=set(), executed_tools=[], archive_tool_calls=[]), **values})
        fixture.agent._current_run_params = fixture.params
    module.prepare_conversation_method_carry(fixture.agent, fixture.params)
    return [turn for turn in fixture.params.tool_ir_history if isinstance(turn, RuntimeFactsTurn)
            and turn.source == "conversation_method_carry"]


def test_compact_restores_entry_and_resource_next_read_without_resource_body_once(tmp_path):
    fixture = method_fixture(tmp_path)
    assert read_package(fixture).ok and read_package(fixture, path="methods/2.md").ok
    compact(fixture)
    turns = carry(fixture)
    assert len(turns) == 1 and "METHOD_ENTRY_0" in turns[0].text
    assert "压缩前读过的包内资料" in turns[0].text and '"resource_path":"methods/2.md"' in turns[0].text
    package, = fixture.scope.skills.packages
    assert package.content_sha256 in turns[0].text and package.activation_id in turns[0].text
    assert "RESOURCE_BODY_2" not in turns[0].text
    assert records(fixture)[0]["carried_generation"] == 1
    assert carry(fixture) == turns
    # 同代的新 run 不只是不能再投递，连准备读取也不应重复；覆盖代次门与提交门彼此冗余的情况。
    before_reads = list(fixture.reads)
    fixture.params = replace(fixture.params, conversation_method_carry_attempts=set())
    fixture.agent._current_run_params = fixture.params
    assert carry(fixture) == turns and fixture.reads == before_reads


def test_no_compact_means_no_restore_and_off_means_no_restore(tmp_path):
    fixture = method_fixture(tmp_path)
    assert read_package(fixture).ok
    assert carry(fixture) == []
    compact(fixture)
    switch(fixture, False)
    module.prepare_conversation_method_carry(fixture.agent, fixture.params)
    assert not any(isinstance(turn, RuntimeFactsTurn) for turn in fixture.params.tool_ir_history)
    assert records(fixture)[0]["carried_generation"] == 0


def test_bundle_covers_full_text_packages_before_skills_and_recent_first(tmp_path):
    fixture = method_fixture(tmp_path, count=2)
    skill = fixture.scope.skills.enabled_entries()[0]
    assert read_package(fixture, 0).ok and read_package(fixture, 1).ok
    assert SkillSearchTool(fixture.agent).execute({"action": "get", "skill_id": skill.stable_id}).ok
    compact(fixture)
    turns = carry(fixture, 10000)
    assert len(turns) == 1
    text = turns[0].text
    assert estimate_tokens(text) <= 10000
    assert text.index("METHOD_ENTRY_1") < text.index("METHOD_ENTRY_0") < text.index(skill.stable_id)


def test_tiny_budget_does_not_consume_generation(tmp_path):
    fixture = method_fixture(tmp_path)
    assert read_package(fixture).ok
    compact(fixture)
    assert carry(fixture, 1) == []
    assert records(fixture)[0]["carried_generation"] == 0


def test_current_version_filters_missing_paths_not_authorization_or_old_pins(tmp_path):
    fixture = method_fixture(tmp_path, resources=1)
    assert read_package(fixture, path="methods/0.md").ok
    store, tid = fixture.agent.conversation_store, fixture.link.thread_id
    prior = dict(records(fixture)[0], version="older", content_sha256="0" * 64,
                 resource_paths=["missing-old.md", "methods/0.md"])
    store.threads.update_atomic(tid, lambda thread: replace(thread, conversation_methods=(prior,)))
    compact(fixture)
    turns = carry(fixture)
    assert len(turns) == 1 and "missing-old.md" not in turns[0].text
    assert fixture.scope.skills.packages[0].content_sha256 in turns[0].text
    assert '"resource_path":"methods/0.md"' in turns[0].text


def test_failed_entry_logs_code_without_leaking_text_and_delivers_only_reference(tmp_path, monkeypatch, caplog):
    fixture = method_fixture(tmp_path)
    assert read_package(fixture).ok
    compact(fixture)
    def broken(_path):
        raise OSError("PRIVATE_ERROR_SENTINEL")
    package, = fixture.scope.skills.packages
    fixture.agent._current_skill_snapshot = replace(fixture.scope.skills, packages=(replace(package, reader=broken),))
    turns = carry(fixture)
    assert len(turns) == 1 and "正文这次没带回" in turns[0].text
    assert '"package_id":"entry-0"' in turns[0].text and "METHOD_ENTRY_0" not in turns[0].text
    assert "PRIVATE_ERROR_SENTINEL" not in turns[0].text
    assert records(fixture)[0]["carried_generation"] == 1
    assert "CONVERSATION_METHOD_ENTRY_UNAVAILABLE" in caplog.text and "PRIVATE_ERROR_SENTINEL" not in caplog.text


def test_cancellation_and_execution_replacement_cannot_restore(tmp_path):
    fixture = method_fixture(tmp_path)
    assert read_package(fixture).ok
    compact(fixture)
    fixture.params.cancellation_token.cancel()
    with pytest.raises(ToolCancelled):
        carry(fixture)
    assert records(fixture)[0]["carried_generation"] == 0


def test_failed_runtime_facts_delivery_does_not_consume_generation(tmp_path, monkeypatch):
    fixture = method_fixture(tmp_path)
    assert read_package(fixture).ok
    compact(fixture)
    monkeypatch.setattr("agent_py_agent.agent.agent_core.tool_ir_history.record_runtime_facts_turn_ir", lambda *a, **kw: False)
    assert carry(fixture) == []
    assert records(fixture)[0]["carried_generation"] == 0
    # 投递失败也不能在同一 run 热重试；独立观察读取次数，避免提交门掩盖 attempts 失效。
    before_reads = list(fixture.reads)
    assert carry(fixture) == []
    assert fixture.reads == before_reads
    assert records(fixture)[0]["carried_generation"] == 0


@pytest.mark.parametrize("field,value", [("request_id", "other-request"), ("attempt_id", "other-attempt")])
def test_authority_rejects_execution_replacement_before_any_read(tmp_path, field, value):
    from types import SimpleNamespace
    fixture = method_fixture(tmp_path)
    assert read_package(fixture).ok
    compact(fixture)
    authority = module.MethodCarryAuthority(fixture.agent, fixture.params, fixture.link.thread_id)
    fixture.agent._current_run_params = SimpleNamespace(**{**vars(fixture.params), field: value})
    before = list(fixture.reads)
    with pytest.raises(ToolCancelled):
        authority.check()
    assert fixture.reads == before and records(fixture)[0]["carried_generation"] == 0


def test_carry_budget_stays_cached_when_fresh_switch_file_changes(tmp_path):
    from agent_py_agent.agent.capability.runtime_config_reload import capability_config_for_agent
    fixture = method_fixture(tmp_path)
    assert read_package(fixture).ok
    compact(fixture)
    assert carry(fixture, 1) == []
    assert capability_config_for_agent(fixture.agent).capability_bundle_max_tokens == 1
    fixture.params = replace(fixture.params, conversation_method_carry_attempts=set())
    fixture.agent._current_run_params = fixture.params
    assert carry(fixture, 10000) == []
    assert records(fixture)[0]["carried_generation"] == 0
