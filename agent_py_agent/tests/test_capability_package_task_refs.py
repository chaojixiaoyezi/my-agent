"""能力包沿真实安装、任务与授权组件的版本固定；不启动模型、Gateway 或外部进程。"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.capability.skill_search_tool import SkillSearchTool
from agent_py_agent.agent.capability.skill_snapshot import SkillSnapshotError
from agent_py_agent.agent.capability.task_references import (
    normalize_skill_reference,
    task_skill_references,
)
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.agent.plugin_content_activation import PluginContentActivation
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_installation import PluginInstallRequest
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.runtime_context import (
    restore_current_subagent_context,
    set_current_subagent_context,
)
from agent_py_agent.agent.scheduler.repository import ScheduleValidationError, _normalize_skill_refs
from agent_py_agent.agent.scheduler.service import SchedulerService
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.services.hierarchy.context import inherited_hierarchy_attributes
from agent_py_agent.agent.subagents.services.hierarchy.scheduler_models import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)
from agent_py_agent.agent.subagents.services.lifecycle import RecordCapabilityGrantParams
from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
from agent_py_agent.tests.test_capability_package import content_bundle


# LLM: 测试只在 conftest 隔离 home 创建真实安装事实；不用假摘要充当原安装校验，也不启动模型。
# 函数用途: 装配支持两份同名内部资源的 Agent 和可停用的安装引用。
def _agent(tmp_path):
    agent = SimpleAgent(AgentConfig(enable_plugins=True, enable_subagents=True, prompt_files=[]), tmp_path / "repo")
    store = PluginInstallStore(resolve_owner_home(agent.home_paths.root))
    entries = []
    for index, package_id in enumerate(("story-a", "story-b")):
        package = inspect_plugin_package(content_bundle(
            files={"CAPABILITY.md": f"# {package_id}".encode(), "methods/SKILL.md": package_id.encode()},
            change=lambda row, name=package_id: row.update(plugin_id=name),
        ))
        row = store.install(PluginInstallRequest(package, f"install-{index}", 0)).installation
        activation = PluginContentActivation(f"enable-{index}", package_id, row.package_sha256,
                                             row.revision, row.settings_revision)
        entries.append(store.change_activation(PluginActivationRequest(f"enable-{index}", row.revision, activation)).installation)
    return agent, store, entries


# LLM: 主任务使用真实会话/任务库与可信回合属性；不把输出文案当成任务身份。
# 函数用途: 为包首次读取建立可在重新装配 Agent 后恢复的任务关联。
def _bind_main_task(agent, task_id="main-task"):
    thread = agent.conversation_store.threads.get_or_create({"canonical_user_id": "local/main", "channel": "cli", "channel_conversation_id": "test-chat"})
    agent.conversation_store.tasks.bind({"thread_id": thread.thread_id, "task_id": task_id, "goal": "整理长篇内容"})
    attrs = {"conversation_thread_id": thread.thread_id, "conversation_task_id": task_id}
    agent._current_run_params = RunParams(task_id=task_id, task_attributes=attrs)
    return attrs


def test_native_creation_and_child_tool_keep_private_packages_scoped(tmp_path):
    agent, store, entries = _agent(tmp_path)
    result = CreateSubagentsTool(agent).execute({"goal": "按故事包完成只读分析", "allowed_skills": ["capability:story-a"], "defer_start": True})
    assert result.ok, result.output
    task = agent.subagents.list_runs()[0]
    assert task.allowed_skills == ["capability:story-a"]
    assert task.attributes["skill_snapshot_refs"][0]["activation_id"] == entries[0].activation_id
    previous = set_current_subagent_context(agent, run_id=task.id, task_attributes=task.attributes)
    try:
        allowed = SkillSearchTool(agent).execute({"action": "get", "package_id": "story-a"})
        denied = SkillSearchTool(agent).execute({"action": "get", "package_id": "story-b"})
        assert allowed.ok and "story-a" in allowed.output
        assert not denied.ok
        store.change_activation(PluginActivationRequest("disable-a", entries[0].revision, replace(entries[0].activation, phase="revoked")))
        assert not SkillSearchTool(agent).execute({"action": "get", "package_id": "story-a"}).ok
    finally:
        restore_current_subagent_context(agent, previous)


def test_main_read_pins_in_original_task_and_reopening_keeps_other_packages_visible(tmp_path):
    agent, _store, entries = _agent(tmp_path)
    attrs = _bind_main_task(agent)
    result = SkillSearchTool(agent).execute({"action": "get", "package_id": "story-a"})
    assert result.ok, result.output
    task = agent.conversation_store.tasks.load("main-task")
    assert len(task.skill_snapshot_refs) == 1
    assert task.skill_snapshot_refs[0]["activation_id"] == entries[0].activation_id
    agent.conversation_store = ConversationStore(agent.conversation_store.storage.root)
    snapshot = agent.skill_snapshot_for_run_scope(agent.effective_workspace_root)
    assert {row.package_id for row in snapshot.packages} == {"story-a", "story-b"}
    assert SkillSearchTool(agent).execute({"action": "get", "package_id": "story-b"}).ok
    agent.conversation_store.tasks.bind({"thread_id": attrs["conversation_thread_id"], "task_id": "main-task", "goal": "续做"})
    assert len(agent.conversation_store.tasks.load("main-task").skill_snapshot_refs) == 2


def test_main_task_isolates_reenabled_same_bytes_without_silent_rebinding(tmp_path):
    agent, store, entries = _agent(tmp_path)
    _bind_main_task(agent)
    assert SkillSearchTool(agent).execute({"action": "get", "package_id": "story-a"}).ok
    old = entries[0]
    revoked = store.change_activation(PluginActivationRequest("disable", old.revision, replace(old.activation, phase="revoked"))).installation
    released, _evidence = store.release_activation(resolve_owner_home(agent.home_paths.root), None, revoked, "disable")
    row = released.installation
    new = PluginContentActivation("new-enable", row.manifest.plugin_id, row.package_sha256, row.revision, row.settings_revision)
    store.change_activation(PluginActivationRequest("new-enable", row.revision, new))
    snapshot = agent.skill_snapshot_for_run_scope(agent.effective_workspace_root)
    assert snapshot.resolve_package("story-a") is None
    assert snapshot.resolve_package("story-b") is not None
    assert any(error.code == "CAPABILITY_PACKAGE_PIN_STALE" for error in snapshot.errors)
    assert not SkillSearchTool(agent).execute({"action": "get", "package_id": "story-a"}).ok
    assert agent.conversation_store.tasks.load("main-task").skill_snapshot_refs[0]["activation_id"] == old.activation_id


def test_main_pin_concurrency_keeps_both_packages_and_conflict_preserves_record(tmp_path):
    agent, _store, entries = _agent(tmp_path)
    attrs = _bind_main_task(agent)
    refs = [row.to_ref() for row in agent.current_skill_snapshot().packages]
    tasks = agent.conversation_store.tasks
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda ref: tasks.pin_skill_reference(task_id="main-task", thread_id=attrs["conversation_thread_id"], reference=ref), refs))
    before = tasks.load("main-task")
    assert len(before.skill_snapshot_refs) == 2
    with pytest.raises(ValueError, match="CONFLICT"):
        tasks.pin_skill_reference(task_id="main-task", thread_id=attrs["conversation_thread_id"], reference={**refs[0], "activation_id": "f" * 64})
    assert tasks.load("main-task") == before
    with pytest.raises(ValueError, match="BINDING"):
        tasks.pin_skill_reference(task_id="main-task", thread_id="another-thread", reference=refs[0])
    tasks.update_status({"task_id": "main-task", "status": "cancelled"})
    with pytest.raises(ValueError, match="INACTIVE"):
        tasks.pin_skill_reference(task_id="main-task", thread_id=attrs["conversation_thread_id"], reference=refs[0])
    assert entries[0].activation_id in json.dumps(before.skill_snapshot_refs)


def test_hierarchy_inherits_parent_package_and_rejects_expansion(tmp_path):
    agent, _store, _entries = _agent(tmp_path)
    result = CreateSubagentsTool(agent).execute({"goal": "父任务", "allowed_skills": ["capability:story-a"], "defer_start": True})
    assert result.ok
    parent = agent.subagents.list_runs()[0]
    blocked = agent.subagents.hierarchy.schedule_child_runs(params=HierarchyScheduleRequest(parent_run_id=parent.id, child_specs=[HierarchyChildSpec(goal="越权", allowed_skills=["capability:story-b"])], apply=True))
    assert blocked.blocked
    inherited = inherited_hierarchy_attributes(parent, HierarchyChildSpec(goal="合法", attributes={"skill_snapshot_refs": [{"stable_id": "capability:story-a", "activation_id": "f" * 64}]}))
    assert inherited["skill_snapshot_refs"] == parent.attributes["skill_snapshot_refs"]
    parent.attributes.pop("skill_snapshot_refs")
    parent.capability_grants = [SimpleNamespace(capability_cards=inherited["skill_snapshot_refs"])]
    granted = inherited_hierarchy_attributes(parent, HierarchyChildSpec(goal="已获授予的后代"))
    assert granted["skill_snapshot_refs"] == inherited["skill_snapshot_refs"]


def test_scheduler_preserves_activation_and_refuses_missing_or_conflicting_identity(tmp_path):
    agent, _store, _entries = _agent(tmp_path)
    reference = agent.current_skill_snapshot().packages[0].to_ref()
    assert _normalize_skill_refs([reference]) == [reference]
    broken = {key: value for key, value in reference.items() if key != "activation_id"}
    with pytest.raises(ScheduleValidationError):
        _normalize_skill_refs([broken])
    with pytest.raises(ScheduleValidationError):
        _normalize_skill_refs([reference, {**reference, "activation_id": "f" * 64}])
    service = SchedulerService(SimpleNamespace(), conversation_store=agent.conversation_store, skill_snapshot_provider=agent.current_skill_snapshot)
    assert service._skill_reference_error({"skill_refs": [reference]}) == ""
    for field in ("kind", "activation_id", "content_sha256"):
        incomplete = {key: value for key, value in reference.items() if key != field}
        assert service._skill_reference_error({"skill_refs": [incomplete]}) == "SCHEDULER_SKILL_REFERENCE_INVALID"
    assert service._skill_reference_error({"skill_refs": [{**reference, "kind": "skill"}]}) == "SCHEDULER_SKILL_REFERENCE_INVALID"
    assert service._skill_reference_error({"skill_refs": [{**reference, "activation_id": "f" * 64}]}) == "SCHEDULER_SKILL_SNAPSHOT_STALE"
    assert normalize_skill_reference({"stable_id": "shared:one", "content_sha256": "a" * 64}) == {"stable_id": "shared:one", "content_sha256": "a" * 64}


def test_child_consumes_new_canonical_grant_without_stale_run_params_narrowing(tmp_path):
    agent, _store, _entries = _agent(tmp_path)
    refs = {row.stable_id: row.to_ref() for row in agent.current_skill_snapshot().packages}
    assert CreateSubagentsTool(agent).execute({"goal": "父任务", "allowed_skills": ["capability:story-a"], "defer_start": True}).ok
    task = agent.subagents.list_runs()[0]
    initial_attrs = dict(task.attributes)
    agent.subagents.lifecycle.record_capability_grant(task.id, params=RecordCapabilityGrantParams(
        request_id="grant-story-b", skills=["capability:story-b"], capability_cards=[refs["capability:story-b"]], reason="新增范围内包",
    ))
    agent._current_run_params = RunParams(task_attributes=initial_attrs)
    previous = set_current_subagent_context(agent, run_id=task.id, task_attributes=initial_attrs)
    try:
        result = SkillSearchTool(agent).execute({"action": "get", "package_id": "story-b"})
        assert result.ok, result.output
    finally:
        restore_current_subagent_context(agent, previous)


@pytest.mark.parametrize("broken", [
    {"kind": "capability_package", "stable_id": "capability:story-a"},
    {"kind": "skill", "stable_id": "capability:story-a", "content_sha256": "a" * 64},
    {"kind": "capability_package", "content_sha256": "a" * 64},
])
def test_malformed_package_task_reference_is_not_silently_discarded(broken):
    task = SimpleNamespace(attributes={"skill_snapshot_refs": [broken]}, capability_grants=[])
    with pytest.raises(SkillSnapshotError):
        task_skill_references(task)
