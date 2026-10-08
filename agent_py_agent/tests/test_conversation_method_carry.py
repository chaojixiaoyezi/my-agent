# LLM: 用真实离线安装、原线程锁和 skill_search 回执守住会话方法账本，不接真实配置或供应商。
# 模块用途: 验证成功读取才记账、隐藏序列化、容量与当前主会话边界。
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.agent_core.runtime_mixin import _bind_main_agent_authority
from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.capability.runtime_config_reload import capability_config_path_for
from agent_py_agent.agent.capability.skill_search_tool import SkillSearchTool
from agent_py_agent.agent.conversation.background_context import _minimal_context_bundle
from agent_py_agent.agent.conversation.models import ConversationThread
from agent_py_agent.agent.settings.parameter_registry import parameter_registry
from agent_py_agent.agent.settings.user_config_capability import (
    BOUNDARY_KEYS,
    EFFECT_IMMEDIATE,
    USER_SETTINGS_BOUNDARY_KEYS,
)
from agent_py_agent.tests.test_capability_package import content_bundle
from agent_py_agent.tests.test_capability_package_entry_context import _fixture


def method_fixture(tmp_path, *, count=1, resources=12):
    bundles = [content_bundle(
        files={"CAPABILITY.md": f"METHOD_ENTRY_{index}".encode(),
               **{f"methods/{n}.md": f"RESOURCE_BODY_{n}".encode() for n in range(resources)}},
        change=lambda row, index=index: row.update(plugin_id=f"entry-{index}"),
    ) for index in range(count)]
    fixture = _fixture(tmp_path, bundles=bundles)
    fixture.params.context_scope = "default"
    # 原入口夹具只验 reader，不包含真实工具循环所需字段；沿用测试补齐相同参数表面。
    fixture.params.run_scope = None
    fixture.params.tool_context = []
    fixture.params.tool_protocol_snapshot = None
    fixture.params.conversation_method_carry_attempts = set()
    bound = _bind_main_agent_authority(fixture.agent, RunParams(
        run_id=fixture.params.run_id, request_id=fixture.params.request_id, task_id=fixture.params.task_id,
        task_attributes=fixture.params.task_attributes))
    fixture.params.run_id, fixture.params.attempt_id = bound.run_id, bound.attempt_id
    fixture.agent._current_run_params = fixture.params
    fixture.agent._current_skill_snapshot = fixture.scope.skills
    return fixture


def records(fixture):
    thread = fixture.agent.conversation_store.threads.require(fixture.link.thread_id)
    return getattr(thread, "conversation_methods", ())


def read_package(fixture, index=0, path=""):
    return SkillSearchTool(fixture.agent).execute(
        {"action": "get", "package_id": f"entry-{index}", **({"resource_path": path} if path else {})})


def switch(fixture, enabled):
    path = capability_config_path_for(fixture.agent)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"conversation_method_carry_enabled: {str(enabled).lower()}\n", encoding="utf-8")


def test_carry_config_defaults_to_on_and_is_immediate_user_only():
    key = "conversation_method_carry_enabled"
    assert getattr(CapabilityConfig(), key, False) is True
    registry = parameter_registry()
    assert key in registry
    assert registry[key].effect == EFFECT_IMMEDIATE and registry[key].writable is False
    assert key in USER_SETTINGS_BOUNDARY_KEYS
    assert BOUNDARY_KEYS[key] == "决定是否往模型提示里带本会话在用的方法（用户决定）"


@pytest.mark.parametrize("path", ["", "CAPABILITY.md", "methods/2.md"])
def test_successful_package_get_records_current_version_and_only_nonentry_path(tmp_path, path):
    fixture = method_fixture(tmp_path)
    assert read_package(fixture, path=path).ok
    assert len(records(fixture)) == 1
    row, = records(fixture)
    package, = fixture.scope.skills.packages
    assert row["stable_id"] == package.stable_id and row["kind"] == "capability_package"
    assert row["content_sha256"] == package.content_sha256 and row["version"] == package.version
    assert row["read_generation"] == 0 and row["carried_generation"] == 0
    assert row["first_used_at"] > 0 and row["last_used_at"] >= row["first_used_at"]
    assert list(row["resource_paths"]) == ([path] if path.startswith("methods/") else [])


def test_successful_skill_get_records_canonical_skill_id(tmp_path):
    fixture = method_fixture(tmp_path)
    skill = fixture.scope.skills.enabled_entries()[0]
    result = SkillSearchTool(fixture.agent).execute({"action": "get", "skill_id": skill.stable_id})
    assert result.ok
    assert len(records(fixture)) == 1
    assert records(fixture)[0]["stable_id"] == skill.stable_id and records(fixture)[0]["kind"] == "skill"


def test_package_paths_are_unique_recent_eight_and_entry_does_not_evict(tmp_path):
    fixture = method_fixture(tmp_path)
    for n in range(11):
        assert read_package(fixture, path=f"methods/{n}.md").ok
    assert list(records(fixture)[0]["resource_paths"]) == [f"methods/{n}.md" for n in range(3, 11)]
    assert read_package(fixture, path="methods/5.md").ok
    assert read_package(fixture).ok
    assert list(records(fixture)[0]["resource_paths"]) == [
        "methods/3.md", "methods/4.md", "methods/6.md", "methods/7.md", "methods/8.md",
        "methods/9.md", "methods/10.md", "methods/5.md",
    ]


def test_repeated_reads_keep_first_order_and_eviction_uses_recent_usage(tmp_path):
    fixture = method_fixture(tmp_path, count=6)
    for n in range(5):
        assert read_package(fixture, n).ok
    assert len(records(fixture)) == 5
    first = next(row for row in records(fixture) if row["stable_id"] == "capability:entry-0")
    assert read_package(fixture, 0).ok
    updated = next(row for row in records(fixture) if row["stable_id"] == first["stable_id"])
    assert updated["first_used_at"] == first["first_used_at"]
    assert updated["last_used_at"] >= first["last_used_at"]
    assert read_package(fixture, 5).ok
    assert {row["stable_id"] for row in records(fixture)} == {f"capability:entry-{n}" for n in (0, 2, 3, 4, 5)}


@pytest.mark.parametrize("scope", ["isolated", "control_plane"])
def test_nonconversation_scopes_never_record(tmp_path, scope):
    fixture = method_fixture(tmp_path)
    fixture.params.context_scope = scope
    assert read_package(fixture).ok
    assert records(fixture) == ()


def test_child_never_records_in_parent_thread(tmp_path, monkeypatch):
    fixture = method_fixture(tmp_path)
    monkeypatch.setattr("agent_py_agent.agent.runtime_context.current_subagent_run_id", lambda _agent: "child-run")
    assert read_package(fixture).ok
    assert records(fixture) == ()


@pytest.mark.parametrize("arguments", [
    {"action": "get", "package_id": "entry-0", "resource_path": "missing.md"},
    {"action": "get", "skill_id": "missing:skill"},
    {"action": "search", "package_id": "entry-0", "query": "methods"},
])
def test_failed_get_and_successful_search_do_not_record(tmp_path, arguments):
    fixture = method_fixture(tmp_path)
    outcome = SkillSearchTool(fixture.agent).execute(arguments)
    assert outcome.ok is (arguments["action"] == "search")
    assert records(fixture) == ()


def test_read_failure_never_creates_a_record(tmp_path):
    fixture = method_fixture(tmp_path)
    package, = fixture.scope.skills.packages
    fixture.agent._current_skill_snapshot = replace(fixture.scope.skills, packages=(replace(package, reader=lambda _path: b"tampered"),))
    assert read_package(fixture).ok is False
    assert records(fixture) == ()


def test_off_switch_is_fresh_without_losing_existing_records(tmp_path):
    fixture = method_fixture(tmp_path, count=2)
    switch(fixture, True)
    assert read_package(fixture).ok and len(records(fixture)) == 1
    before = records(fixture)
    switch(fixture, False)
    assert read_package(fixture, 1).ok and records(fixture) == before
    switch(fixture, True)
    assert read_package(fixture, 1).ok and len(records(fixture)) == 2


def test_new_ledger_is_hidden_and_empty_old_record_bytes_stay_unchanged(tmp_path):
    fixture = method_fixture(tmp_path)
    store, tid = fixture.agent.conversation_store, fixture.link.thread_id
    old = store.threads.require(tid).to_dict()
    assert "conversation_methods" not in old
    assert ConversationThread.from_dict(old).to_dict() == old
    before = json.dumps(store.context_bundle(tid)["thread"], ensure_ascii=False, sort_keys=True)
    assert read_package(fixture).ok and len(records(fixture)) == 1
    loaded = store.threads.require(tid)
    assert ConversationThread.from_dict(loaded.to_dict()).conversation_methods == loaded.conversation_methods
    for bundle in (store.context_bundle(tid), _minimal_context_bundle(loaded)):
        assert "conversation_methods" not in bundle["thread"]
    # pin 是原 get 的副作用，和隐藏的会话账本不是同一字段；线程摘要/压缩游标不得被记账改写。
    assert json.dumps(store.context_bundle(tid)["thread"], ensure_ascii=False, sort_keys=True) == before


def test_attached_bookkeeping_failure_does_not_flip_successful_get(tmp_path, monkeypatch, caplog):
    fixture = method_fixture(tmp_path)
    def fail_update(*args, **kwargs):
        raise OSError("PRIVATE_BOOKKEEPING_ERROR")
    monkeypatch.setattr(type(fixture.agent.conversation_store.threads), "update_atomic", fail_update)
    assert read_package(fixture).ok
    assert records(fixture) == ()
    assert "CONVERSATION_METHOD_WRITE_UNAVAILABLE" in caplog.text
    assert "PRIVATE_BOOKKEEPING_ERROR" not in caplog.text


def test_record_update_preserves_existing_summary_bytes_and_fingerprint_metadata(tmp_path):
    fixture = method_fixture(tmp_path)
    threads, tid = fixture.agent.conversation_store.threads, fixture.link.thread_id
    summary = "已存摘要\r\n中文\t末行无换行"
    fingerprint = {"summary_source_fingerprint": "stored-fingerprint"}
    threads.update_atomic(tid, lambda thread: replace(thread, summary=summary, metadata=fingerprint))
    before = threads.require(tid).to_dict()
    assert read_package(fixture).ok
    after = threads.require(tid).to_dict()
    assert after["summary"].encode() == before["summary"].encode()
    assert after["metadata"] == before["metadata"] == fingerprint
