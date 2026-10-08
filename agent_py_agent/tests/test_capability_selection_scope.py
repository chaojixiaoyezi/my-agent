"""选包初始化范围：真实配置加载、既有工具面与只读包快照；不发送模型或执行包资源。"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability import package_selection_scope as selection_scope
from agent_py_agent.agent.capability.config import CapabilityConfig, load_capability_config
from agent_py_agent.agent.capability.runtime_config_reload import capability_config_for_agent
from agent_py_agent.agent.capability.skill_snapshot import SkillSnapshot
from agent_py_agent.agent.capability.subagent_package_entries import subagent_entries_enabled
from agent_py_agent.agent.common.cancellation import ToolCancelled
from agent_py_agent.agent.conversation.capability_selection_state import (
    CAPABILITY_SELECTION_KEY,
    TaskCapabilitySelection,
)
from agent_py_agent.agent.conversation.models import ThreadTaskLink
from agent_py_agent.agent.runtime_context import (
    restore_current_subagent_context,
    set_current_subagent_context,
)
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.settings.parameter_registry import parameter_registry
from agent_py_agent.agent.settings.user_config_capability import (
    EFFECT_GATEWAY_RESTART,
    EFFECT_IMMEDIATE,
)
from agent_py_agent.tests.test_capability_package_discovery import package_fixture


# LLM: 所有配置与 workspace 仅用 pytest 临时目录，包 reader 计数与模型调用反例不能替代真实端到端验收。
# 函数用途: 装配可观察的注册表、冻结工具快照和包快照，并使用真实 YAML 配置入口。
def _fixture(tmp_path, *, enabled=True, extra_config="", package_count=1):
    path = tmp_path / "capability.yaml"
    path.write_text(f"enable_capability_package_selection: {str(enabled).lower()}\n" + extra_config, encoding="utf-8")
    reads, calls = [], []
    packages = tuple(package_fixture(f"content-{index}", reads=reads) for index in range(package_count))
    skills = SkillSnapshot((), (), "snapshot", "local/main", str(tmp_path), packages=packages)
    runtime = SimpleNamespace(availability=SimpleNamespace(available=True), exposure=SimpleNamespace(model_visible=True))

    def find_runtime(name):
        calls.append(("runtime", name))
        return runtime if name == "skill_search" else None

    tools = SimpleNamespace(runtime=find_runtime)

    def runtime_snapshot(**kwargs):
        calls.append(("registry", kwargs))
        return tools

    def current_snapshot():
        calls.append(("current",))
        return skills

    def workspace_snapshot(workspace_root):
        calls.append(("workspace", workspace_root))
        return skills

    agent = SimpleNamespace(
        root=tmp_path, config=AgentConfig(enable_tools=True, enable_plugins=True), capability_config_path=path,
        tools=SimpleNamespace(runtime_snapshot=runtime_snapshot), current_skill_snapshot=current_snapshot,
        skills_service=SimpleNamespace(snapshot_for=workspace_snapshot),
    )
    params = SimpleNamespace(allowed_tools=("skill_search", "read_file"), run_id="run-scope", tool_runtime_snapshot=tools)
    return SimpleNamespace(agent=agent, params=params, skills=skills, tools=tools, runtime=runtime,
                           reads=reads, calls=calls, path=path)


def test_new_selection_defaults_match_shipped_yaml_and_remain_off():
    defaults = CapabilityConfig()
    loaded = load_capability_config(Path(__file__).parents[1] / "config" / "capability_config.yaml")
    for config in (defaults, loaded):
        assert config.enable_capability_package_selection is False
        assert config.capability_package_selection_max_input_tokens == 3000
        assert config.capability_candidate_limit == 5
        assert config.capability_bundle_max_tokens == 3000


def test_default_off_does_not_touch_tools_snapshots_resources_or_serialize_a_key(tmp_path):
    fixture = _fixture(tmp_path)
    fixture.path.write_text("", encoding="utf-8")
    assert selection_scope.package_selection_scope(fixture.agent, fixture.params) is None
    marker = selection_scope.new_task_capability_selection(fixture.agent, fixture.params)
    assert marker is None and fixture.calls == [] and fixture.reads == []
    original = ThreadTaskLink("thread", "task", "核对资料")
    actual = replace(original, capability_selection=marker)
    assert json.dumps(actual.to_dict(), ensure_ascii=False) == json.dumps(original.to_dict(), ensure_ascii=False)
    assert CAPABILITY_SELECTION_KEY not in actual.to_dict()


def test_enabled_main_uses_frozen_tool_scope_and_metadata_only(tmp_path):
    fixture = _fixture(tmp_path, package_count=2)
    scope = selection_scope.package_selection_scope(fixture.agent, fixture.params)
    assert scope.skills is fixture.skills and scope.tools is fixture.tools
    assert scope.config.enable_capability_package_selection is True
    assert fixture.calls == [("runtime", "skill_search"), ("current",)]
    assert fixture.reads == []


def test_eligible_new_main_returns_typed_pending_without_persisting(tmp_path):
    fixture = _fixture(tmp_path)
    before = set(tmp_path.rglob("*"))
    marker = selection_scope.new_task_capability_selection(fixture.agent, fixture.params)
    assert isinstance(marker, TaskCapabilitySelection) and marker == TaskCapabilitySelection.pending()
    assert fixture.reads == []
    assert set(tmp_path.rglob("*")) == before
    assert fixture.calls == [("runtime", "skill_search"), ("current",)]


@pytest.mark.parametrize("condition", ["off", "tools_off", "plugins_off", "empty_allowed", "excluded_allowed", "child"])
def test_ineligible_scope_stops_before_tool_or_skill_snapshot(tmp_path, condition):
    fixture = _fixture(tmp_path, enabled=condition != "off")
    if condition == "tools_off":
        fixture.agent.config.enable_tools = False
    elif condition == "plugins_off":
        fixture.agent.config.enable_plugins = False
    elif condition in {"empty_allowed", "excluded_allowed"}:
        fixture.params.allowed_tools = () if condition == "empty_allowed" else ("read_file",)
    previous = None
    if condition == "child":
        previous = set_current_subagent_context(fixture.agent, run_id="child-scope", task_attributes={})
    try:
        assert selection_scope.package_selection_scope(fixture.agent, fixture.params) is None
        assert selection_scope.new_task_capability_selection(fixture.agent, fixture.params) is None
    finally:
        if previous is not None:
            restore_current_subagent_context(fixture.agent, previous)
    assert fixture.calls == [] and fixture.reads == []


@pytest.mark.parametrize("condition", ["missing", "hidden", "unavailable"])
def test_skill_search_must_exist_be_visible_and_available_before_packages(tmp_path, condition):
    fixture = _fixture(tmp_path)
    if condition == "missing":
        fixture.tools.runtime = lambda _name: None
    elif condition == "hidden":
        fixture.runtime.exposure.model_visible = False
    else:
        fixture.runtime.availability.available = False
    assert selection_scope.package_selection_scope(fixture.agent, fixture.params) is None
    assert selection_scope.new_task_capability_selection(fixture.agent, fixture.params) is None
    assert not any(call[0] in {"current", "workspace", "registry"} for call in fixture.calls)
    assert fixture.reads == []


def test_empty_packages_skip_even_when_search_tool_is_available(tmp_path):
    fixture = _fixture(tmp_path, package_count=0)
    assert selection_scope.package_selection_scope(fixture.agent, fixture.params) is None
    assert selection_scope.new_task_capability_selection(fixture.agent, fixture.params) is None
    assert fixture.reads == []
    assert fixture.calls.count(("current",)) == 2


def test_missing_frozen_tools_uses_original_registry_with_exact_allowed_and_run(tmp_path):
    fixture = _fixture(tmp_path)
    fixture.params.tool_runtime_snapshot = None
    scope = selection_scope.package_selection_scope(fixture.agent, fixture.params)
    assert scope.tools is fixture.tools
    assert fixture.calls == [
        ("registry", {"allowed_tools": fixture.params.allowed_tools, "run_id": "run-scope"}),
        ("runtime", "skill_search"), ("current",),
    ]
    assert fixture.reads == []


def test_new_goal_uses_explicit_trusted_workspace_snapshot_without_reading_resources(tmp_path):
    fixture = _fixture(tmp_path)
    trusted_workspace = tmp_path / "trusted-goal-workspace"
    marker = selection_scope.new_task_capability_selection(fixture.agent, workspace_root=trusted_workspace)
    assert marker == TaskCapabilitySelection.pending()
    assert fixture.calls == [
        ("registry", {"allowed_tools": None, "run_id": ""}),
        ("runtime", "skill_search"), ("workspace", trusted_workspace),
    ]
    assert not trusted_workspace.exists() and fixture.reads == []


@pytest.mark.parametrize("failure_stage", ["registry", "snapshot"])
def test_new_task_optional_failure_warns_without_private_exception_or_marker(tmp_path, caplog, failure_stage):
    fixture = _fixture(tmp_path)

    def fail(*_args, **_kwargs):
        raise RuntimeError("private-credential-and-workspace")

    if failure_stage == "registry":
        fixture.params.tool_runtime_snapshot = None
        fixture.agent.tools.runtime_snapshot = fail
    else:
        fixture.agent.current_skill_snapshot = fail
    assert selection_scope.new_task_capability_selection(fixture.agent, fixture.params) is None
    assert "CAPABILITY_SELECTION_SCOPE_UNAVAILABLE" in caplog.text
    assert "RuntimeError" in caplog.text and "private-credential-and-workspace" not in caplog.text
    assert fixture.reads == []


@pytest.mark.parametrize("error", [InterruptedError("stop"), ToolCancelled("stop"), KeyboardInterrupt()])
def test_new_task_scope_cancellation_propagates_without_optional_failure_warning(tmp_path, caplog, error):
    fixture = _fixture(tmp_path)

    def stop():
        raise error

    fixture.agent.current_skill_snapshot = stop
    with pytest.raises(type(error)):
        selection_scope.new_task_capability_selection(fixture.agent, fixture.params)
    assert "CAPABILITY_SELECTION_SCOPE_UNAVAILABLE" not in caplog.text
    assert fixture.reads == []


@pytest.mark.parametrize("input_limit,candidate_limit,bundle_limit", [(0, 0, 0), (731, 2, 509)])
def test_real_config_overrides_reach_scope_with_shared_candidate_and_entry_budgets(tmp_path, input_limit, candidate_limit, bundle_limit):
    fixture = _fixture(tmp_path, extra_config=(
        f'capability_package_selection_max_input_tokens: "{input_limit}"\n'
        f'capability_candidate_limit: "{candidate_limit}"\n'
        f'capability_bundle_max_tokens: "{bundle_limit}"\n'
    ))
    scope = selection_scope.package_selection_scope(fixture.agent, fixture.params)
    assert scope.config.capability_package_selection_max_input_tokens == input_limit
    assert scope.config.capability_candidate_limit == candidate_limit
    assert scope.config.capability_bundle_max_tokens == bundle_limit
    assert scope.config is fixture.agent._capability_config_runtime_snapshot.config
    assert fixture.reads == []


@pytest.mark.parametrize("value,expected", [('"false"', False), ('"true"', True)])
def test_selection_switch_real_yaml_coercion_controls_scope(tmp_path, value, expected):
    fixture = _fixture(tmp_path)
    fixture.path.write_text(f"enable_capability_package_selection: {value}\n", encoding="utf-8")
    scope = selection_scope.package_selection_scope(fixture.agent, fixture.params)
    assert (scope is not None) is expected
    assert fixture.reads == []


def test_real_installation_and_tool_registry_initialize_without_member_read(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability import package_provider
    from agent_py_agent.tests.test_capability_package_task_refs import _agent

    agent, _store, entries = _agent(tmp_path)
    config_path = tmp_path / "capability-selection.yaml"
    config_path.write_text("enable_capability_package_selection: true\n", encoding="utf-8")
    agent.capability_config_path = config_path
    agent._capability_config_runtime_snapshot = None

    def forbidden_member_read(*_args, **_kwargs):
        pytest.fail("初始化不得读取真实安装包的成员正文")

    monkeypatch.setattr(package_provider, "read_capability_member", forbidden_member_read)
    params = SimpleNamespace(allowed_tools=("skill_search",), run_id="main-scope",
                             tool_runtime_snapshot=agent.tools.runtime_snapshot(allowed_tools=["skill_search"]))
    scope = selection_scope.package_selection_scope(agent, params)
    assert scope is not None and len(scope.skills.packages) == len(entries) == 2
    assert scope.tools is params.tool_runtime_snapshot
    assert selection_scope.new_task_capability_selection(agent, params) == TaskCapabilitySelection.pending()


@pytest.mark.parametrize("key", [
    "capability_pack_self_install_enabled", "plugin_self_install_enabled", "enable_capability_package_selection",
])
def test_fresh_switch_registry_reports_immediate_but_budgets_still_require_restart(key):
    registry = parameter_registry()
    assert registry[key].effect == EFFECT_IMMEDIATE
    assert registry["capability_bundle_max_tokens"].effect == EFFECT_GATEWAY_RESTART


@pytest.mark.parametrize("enabled", [False, True])
def test_selection_scope_reads_switch_fresh_without_refreshing_cached_budgets(tmp_path, enabled):
    fixture = _fixture(tmp_path, enabled=enabled, extra_config="capability_bundle_max_tokens: 731\n")
    assert (selection_scope.package_selection_scope(fixture.agent, fixture.params) is not None) is enabled
    cached = capability_config_for_agent(fixture.agent)
    fixture.path.write_text(
        f"enable_capability_package_selection: {str(not enabled).lower()}\ncapability_bundle_max_tokens: 509\n",
        encoding="utf-8",
    )
    scope = selection_scope.package_selection_scope(fixture.agent, fixture.params)
    assert (scope is not None) is (not enabled)
    assert capability_config_for_agent(fixture.agent) is cached
    assert cached.enable_capability_package_selection is enabled
    assert cached.capability_bundle_max_tokens == 731
    if scope is not None:
        assert scope.config is cached and scope.config.capability_bundle_max_tokens == 731


@pytest.mark.parametrize("damage", ["invalid_config", "invalid_bool", "unreadable"])
def test_damaged_selection_switch_uses_default_instead_of_cached_true(tmp_path, damage):
    fixture = _fixture(tmp_path)
    cached = capability_config_for_agent(fixture.agent)
    assert cached.enable_capability_package_selection is True
    if damage == "unreadable":
        fixture.path.unlink()
        fixture.path.mkdir()
    else:
        text = ("decision_subagent_model_mode: invalid\n" if damage == "invalid_config"
                else "enable_capability_package_selection: not-a-bool\n")
        fixture.path.write_text(text, encoding="utf-8")
    assert selection_scope.package_selection_scope(fixture.agent, fixture.params) is None
    assert subagent_entries_enabled(fixture.agent) is False
    assert capability_config_for_agent(fixture.agent) is cached


@pytest.mark.parametrize("enabled", [False, True])
def test_subagent_entry_switch_reads_next_file_value_with_same_agent(tmp_path, enabled):
    fixture = _fixture(tmp_path, enabled=enabled)
    cached = capability_config_for_agent(fixture.agent)
    assert subagent_entries_enabled(fixture.agent) is enabled
    fixture.path.write_text(f"enable_capability_package_selection: {str(not enabled).lower()}\n", encoding="utf-8")
    assert subagent_entries_enabled(fixture.agent) is (not enabled)
    assert capability_config_for_agent(fixture.agent) is cached


def test_admin_settings_selection_switch_receipt_and_next_scope_match_without_restart(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_self_install_switches import _ADMIN, _settings_run

    fixture = _fixture(tmp_path, enabled=False)
    fixture.agent.config.config_path = str(tmp_path / "desktop.yaml")
    cached = capability_config_for_agent(fixture.agent)
    for enabled in (True, False):
        report = _settings_run(monkeypatch, fixture.agent,
                               f"/settings set enable_capability_package_selection {str(enabled).lower()}", home=_ADMIN)
        assert report.ok and "马上生效" in report.message and "/restart" not in report.message
        assert (selection_scope.package_selection_scope(fixture.agent, fixture.params) is not None) is enabled
        assert capability_config_for_agent(fixture.agent) is cached
