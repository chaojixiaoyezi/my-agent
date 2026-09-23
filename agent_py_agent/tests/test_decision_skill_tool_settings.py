"""Skill/tool 专属减量字段沿原配置、CAS、通知、模型工具和真实 TUI 输入管道；不联网。"""
import asyncio
import json
import uuid
from pathlib import Path

import pytest

from agent_py_agent.agent.capability.config import CapabilityConfig, load_capability_config
from agent_py_agent.agent.concurrency.interrupt import InterruptHandle
from agent_py_agent.agent.conversation import decision_policy as policy
from agent_py_agent.agent.conversation.decision_service import _policy_revision
from agent_py_agent.agent.settings.decision_settings import (
    execute_decision_settings_operation as settings,
)
from agent_py_agent.agent.settings.decision_settings_schema import (
    DecisionSettingsConflict,
    validate_decision_settings,
)
from agent_py_agent.agent.settings.model_profiles import model_profiles_path, read_model_profiles
from agent_py_agent.agent.settings.model_provider_schema import ModelProfileError
from agent_py_agent.agent.tooling.user_config_tool import _decision_change_properties
from agent_py_agent.tests.test_decision_settings import host_at, patch
from agent_py_agent.tests.test_tui_decision_menu import (
    Gateway,
    choose,
    open_scope,
    press,
    running,
    visible,
)
from agent_py_agent.tests.test_user_config_decision_operations import configured as configured

POLICY = "points.skill_tool.context_policy"
CATEGORIES = "points.skill_tool.optional_categories"


def test_defaults_single_registration_yaml_agreement_and_no_persistent_default_copy(tmp_path):
    host = host_at(tmp_path)
    view = settings(host, "read", {})
    row = view["effective"]["points"]["skill_tool"]
    assert row["context_policy"] == "progressive" and row["optional_categories"] == ["plugins"]
    assert row["effective_mode"] == "off" and view["effective"]["enabled"] is False
    assert view["sources"][POLICY] == "capability_config.decision_skill_tool_context_policy"
    assert view["field_scopes"][POLICY] == ["owner", "thread"]
    assert all("context_policy" not in data and "optional_categories" not in data for name, data in view["effective"]["points"].items() if name != "skill_tool")
    assert view["overrides"] == {"owner": {}, "thread": {}} and not model_profiles_path(host.home_paths).exists()
    config = load_capability_config(Path(__file__).parents[1] / "config" / "capability_config.yaml")
    assert config.decision_skill_tool_context_policy == CapabilityConfig().decision_skill_tool_context_policy
    assert config.decision_skill_tool_optional_categories == CapabilityConfig().decision_skill_tool_optional_categories
    row["optional_categories"].append("mutated")
    assert settings(host, "read", {})["effective"]["points"]["skill_tool"]["optional_categories"] == ["plugins"]


@pytest.mark.parametrize("value", ["plugins", '[]', ("plugins",), None, True, [1], [True], [None], [[]], [""], ["   "]])
def test_categories_reject_non_list_and_non_string_values_without_write(tmp_path, value):
    host = host_at(tmp_path)
    with pytest.raises(ModelProfileError):
        patch(host, {CATEGORIES: value})
    assert not model_profiles_path(host.home_paths).exists()


@pytest.mark.parametrize("value", ["other", True, None, [], 1])
def test_context_policy_is_explicit_enum(tmp_path, value):
    with pytest.raises(ModelProfileError):
        patch(host_at(tmp_path), {POLICY: value})


def test_owner_thread_cas_reset_and_open_categories_use_original_store(tmp_path):
    host = host_at(tmp_path)
    thread = host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})
    owner = patch(host, {POLICY: "metadata", CATEGORIES: ["plugins", "vendor.custom/工具"]})
    current = patch(host, {POLICY: "progressive", CATEGORIES: []}, scope="thread", thread_id=thread.thread_id)
    assert current["effective"]["points"]["skill_tool"]["optional_categories"] == []
    assert current["sources"][CATEGORIES] == "thread"
    assert _policy_revision(owner) != _policy_revision(current)
    with pytest.raises(DecisionSettingsConflict):
        settings(host, "patch", {"scope": "thread", "expected_revision": owner["revision"], "changes": {POLICY: "metadata"}}, thread_id=thread.thread_id)
    result = settings(host, "reset", {"scope": "thread", "expected_revision": current["revision"], "fields": [POLICY, CATEGORIES]}, thread_id=thread.thread_id)
    assert result["effective"]["points"]["skill_tool"]["context_policy"] == "metadata"
    assert result["effective"]["points"]["skill_tool"]["optional_categories"] == ["plugins", "vendor.custom/工具"]
    data = read_model_profiles(model_profiles_path(host.home_paths))
    assert data["decision_settings"]["overrides"] == {POLICY: "metadata", CATEGORIES: ["plugins", "vendor.custom/工具"]}
    assert data["decision_settings"]["schema"] == "decision_settings.v1"


def test_v1_additive_fields_keep_old_overrides_revision_and_do_not_add_defaults():
    old = {"schema": "decision_settings.v1", "revision": 9, "overrides": {"enabled": False, "points.recall.mode": "observe"}}
    assert validate_decision_settings(old) == old
    assert validate_decision_settings({**old, "overrides": {**old["overrides"], POLICY: "metadata", CATEGORIES: []}})["revision"] == 9


@pytest.mark.parametrize("point", ["recall", "curator", "model_selection", "subagent_model"])
def test_special_fields_do_not_exist_on_other_points(tmp_path, point):
    for suffix, value in (("context_policy", "metadata"), ("optional_categories", ["plugins"])):
        with pytest.raises(ModelProfileError):
            patch(host_at(tmp_path), {f"points.{point}.{suffix}": value})


def test_yaml_strict_array_and_null_policy_are_not_optional_inheritance(tmp_path):
    path = tmp_path / "capability.yaml"
    path.write_text('decision_skill_tool_optional_categories: ["plugins", "future.category"]\n')
    assert load_capability_config(path).decision_skill_tool_optional_categories == ["plugins", "future.category"]
    for text in ('decision_skill_tool_optional_categories: "plugins"', 'decision_skill_tool_optional_categories: null',
                 'decision_skill_tool_context_policy: null'):
        path.write_text(text)
        with pytest.raises(ModelProfileError):
            load_capability_config(path)


@pytest.mark.parametrize("field,value", [(POLICY, "metadata"), (CATEGORIES, ["plugins", "new.category"])])
def test_new_field_change_cancels_only_matching_point_and_advances_signature(tmp_path, field, value):
    host = host_at(tmp_path)
    view = settings(host, "read", {})
    active = policy.ActiveDecision(policy.decision_owner_ref(host), "", "skill_tool", InterruptHandle(), host, view)
    unrelated = policy.ActiveDecision(policy.decision_owner_ref(host), "", "recall", InterruptHandle(), host, view)
    tokens = [uuid.uuid4().hex, uuid.uuid4().hex]
    for token, row in zip(tokens, (active, unrelated)):
        assert policy.register_active(token, row)
    try:
        patch(host, {field: value})
        assert active.handle.cancelled and active.settings_cancelled
        assert not unrelated.handle.cancelled
    finally:
        for token, row in zip(tokens, (active, unrelated)):
            policy.unregister_active(token, row)


def test_model_tool_schema_and_original_authenticated_operations(configured):
    host, _key, _thread, tool = configured
    properties = _decision_change_properties()
    assert properties[POLICY]["enum"] == ["metadata", "progressive"]
    assert properties[CATEGORIES] == {"type": "array", "items": {"type": "string", "minLength": 1}}
    assert "points.recall.context_policy" not in properties
    read = json.loads(tool.execute({"action": "decision_read", "scope": "thread"}).output)
    result = tool.execute({"action": "decision_patch", "scope": "thread", "expected_revision": read["revision"], "changes": {POLICY: "metadata", CATEGORIES: ["plugins", "new"]}})
    assert result.ok
    written = json.loads(result.output)
    assert written["effective"]["points"]["skill_tool"]["context_policy"] == "metadata"
    bad = tool.execute({"action": "decision_patch", "scope": "thread", "expected_revision": written["revision"], "changes": {CATEGORIES: "plugins"}})
    assert not bad.ok
    reset = tool.execute({"action": "decision_reset", "scope": "thread", "expected_revision": written["revision"], "fields": [POLICY, CATEGORIES]})
    assert reset.ok and json.loads(reset.output)["overrides"]["thread"] == {}
    assert settings(host, "read", {})["overrides"]["owner"] == {}


def test_pipe_edits_policy_and_strict_category_array_then_resets(tmp_path):
    async def scenario():
        gateway = Gateway(tmp_path)
        async with running(tmp_path, gateway) as ui:
            await open_scope(ui)
            await choose(ui, 5)
            await choose(ui, 2)
            assert "上下文减量策略" in visible(ui.app) and "可选工具类别" in visible(ui.app)
            await choose(ui, 3)
            await press(ui, b"\x1b[A\r")
            assert settings(gateway.host, "read", {})["effective"]["points"]["skill_tool"]["context_policy"] == "metadata"
            await choose(ui, 5)
            await choose(ui, 2)
            await choose(ui, 4)
            await press(ui, b"\x01\x0b")
            await press(ui, '"plugins"\t\r')
            assert "请输入 JSON 字符串数组" in visible(ui.app)
            before = sum(op == "decision_patch" for op, _ in gateway.calls)
            await press(ui, b"\x01\x0b")
            await press(ui, '["plugins", "custom.extra"]\t\r')
            assert sum(op == "decision_patch" for op, _ in gateway.calls) == before + 1
            assert settings(gateway.host, "read", {})["effective"]["points"]["skill_tool"]["optional_categories"] == ["plugins", "custom.extra"]
            await choose(ui, 6)
            await choose(ui, 0)
            assert POLICY not in settings(gateway.host, "read", {})["overrides"]["owner"]
            assert not any(op == "decision_probe" for op, _ in gateway.calls)
    asyncio.run(scenario())


def test_shadowed_categories_change_only_cancels_after_thread_reset(tmp_path):
    host = host_at(tmp_path)
    thread = host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})
    patch(host, {CATEGORIES: ["thread-only"]}, scope="thread", thread_id=thread.thread_id)
    view = settings(host, "read", {"scope": "thread"}, thread_id=thread.thread_id)
    active = policy.ActiveDecision(policy.decision_owner_ref(host), thread.thread_id, "skill_tool", InterruptHandle(), host, view)
    token = uuid.uuid4().hex
    assert policy.register_active(token, active)
    try:
        patch(host, {CATEGORIES: ["owner-changed"]})
        assert not active.handle.cancelled
        current = settings(host, "read", {"scope": "thread"}, thread_id=thread.thread_id)
        settings(host, "reset", {"scope": "thread", "expected_revision": current["revision"], "fields": [CATEGORIES]}, thread_id=thread.thread_id)
        assert active.handle.cancelled
    finally:
        policy.unregister_active(token, active)


def test_default_configuration_change_invalidates_policy_without_faking_persistent_revision(tmp_path):
    host = host_at(tmp_path)
    original = settings(host, "read", {})
    host.capability_config.decision_skill_tool_context_policy = "metadata"
    changed = settings(host, "read", {})
    assert changed["revision"] == original["revision"] == {"owner": 0, "thread": 0}
    assert _policy_revision(changed) != _policy_revision(original)
