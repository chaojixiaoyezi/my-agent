# LLM: 检验发现结果可原样重用、错误选择器仅给受限建议；真实组件留在临时目录，不调用模型或替模型交付。
# 模块用途: 覆盖普通 Skill、能力包及私有资源的读取入口，并守住首次任务晋升、撤销和同名优先级。
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace

import pytest

from agent_py_agent.agent.capability import task_references
from agent_py_agent.agent.capability.skill_search_tool import SkillSearchTool
from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.agent.plugin_content_activation import PluginContentActivation
from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
from agent_py_agent.tests.test_capability_package_discovery import (
    discovery_fixture,
    package_fixture,
)
from agent_py_agent.tests.test_capability_package_runtime_binding import _execute, _unpromoted_turn
from agent_py_agent.tests.test_capability_package_task_refs import _agent


# LLM: 保留真实 pin 实现，只观察调用次数；测试建议生成时不得偷读正文或提前修改任务引用。
# 函数用途: 为只读发现和错误恢复断言安装包引用写入的边界。
def _observe_pins(monkeypatch):
    original = task_references.pin_package_reference
    pins = []

    # LLM: 记录原参数后继续执行真实 pin，不替换任务事实或伪造成功。
    # 函数用途: 在原生产入口外计数，供测试比较发现前后是否产生副作用。
    def observe(agent, reference):
        pins.append(dict(reference))
        return original(agent, reference)

    monkeypatch.setattr(task_references, "pin_package_reference", observe)
    return pins


def test_public_skill_search_returns_reusable_read_arguments(tmp_path, skill_catalog_factory):
    _, snapshot, _, tool = discovery_fixture(tmp_path, skill_catalog_factory, [package_fixture("story-a")])
    search = tool.execute({"action": "search", "query": "普通用户方法"})
    card = next(row for row in json.loads(search.output)["matches"] if row["name"] == "story-content")
    assert card["kind"] == "skill"
    assert card["next_read"] == {"action": "get", "skill_id": snapshot.entries[0].stable_id}
    arguments = json.loads(json.dumps(card["next_read"]))
    original = deepcopy(arguments)
    result = tool.execute(arguments)
    assert result.ok, result.output
    assert arguments == original
    assert json.loads(result.output)["body"].endswith("PUBLIC-BODY")


def test_public_package_search_exposes_entry_call_without_private_members(
    tmp_path, skill_catalog_factory, monkeypatch,
):
    reads = []
    package = package_fixture("story-a", reads=reads)
    _, _, _, tool = discovery_fixture(tmp_path, skill_catalog_factory, [package])
    pins = _observe_pins(monkeypatch)
    result = tool.execute({"action": "search", "query": "分镜"})
    card = json.loads(result.output)["matches"][0]
    assert card["kind"] == "capability_package"
    assert card["stable_id"] == package.stable_id
    assert card["next_read"] == {
        "action": "get", "package_id": package.package_id,
        "expected_package_sha256": package.package_sha256, "expected_activation_id": package.activation_id,
    }
    assert reads == pins == []
    assert all(member.path not in result.output for member in package.members)
    arguments = deepcopy(card["next_read"])
    read = tool.execute(arguments)
    assert read.ok, read.output
    assert arguments == card["next_read"]
    assert reads == [package.entry_document]
    assert pins == [package.to_ref()]


def test_private_resource_calls_keep_same_named_members_in_the_selected_package(
    tmp_path, skill_catalog_factory, monkeypatch,
):
    reads = []
    first = package_fixture("first", files={"CAPABILITY.md": b"first-entry", "methods/review.md": b"first"}, reads=reads)
    second = package_fixture("second", files={"CAPABILITY.md": b"other-entry", "methods/review.md": b"second"})
    _, _, _, tool = discovery_fixture(tmp_path, skill_catalog_factory, [first, second])
    pins = _observe_pins(monkeypatch)
    result = tool.execute({"action": "search", "package_id": "first", "query": "review"})
    card = json.loads(result.output)["matches"][0]
    assert card["next_read"] == {
        "action": "get", "package_id": "first", "resource_path": "methods/review.md",
        "expected_package_sha256": first.package_sha256, "expected_activation_id": first.activation_id,
    }
    assert reads == pins == []
    arguments = deepcopy(card["next_read"])
    read = tool.execute(arguments)
    assert read.ok, read.output
    assert arguments == card["next_read"]
    assert json.loads(read.output)["body"] == "first"
    assert reads == ["methods/review.md"]
    assert pins == [first.to_ref()]


@pytest.mark.parametrize("selector", ["package_id", "stable_id"])
def test_wrong_skill_selector_fails_with_exact_scoped_suggestion_without_reading(
    tmp_path, skill_catalog_factory, monkeypatch, selector,
):
    reads = []
    package = package_fixture("story-a", reads=reads)
    _, _, _, tool = discovery_fixture(tmp_path, skill_catalog_factory, [package])
    pins = _observe_pins(monkeypatch)
    arguments = {"action": "get", "skill_id": getattr(package, selector)}
    original = deepcopy(arguments)
    result = tool.execute(arguments)
    payload = json.loads(result.output)
    assert not result.ok and result.error_code == "TOOL_INVALID_ARGUMENTS"
    assert payload["selector_mismatch"] == {
        "received": "skill_id", "expected": "package_id", "kind": "capability_package",
    }
    assert payload["next_read"] == {
        "action": "get", "package_id": package.package_id,
        "expected_package_sha256": package.package_sha256, "expected_activation_id": package.activation_id,
    }
    assert arguments == original
    assert reads == pins == []
    assert "body" not in payload and all(member.path not in result.output for member in package.members)


@pytest.mark.parametrize("selector", ["package_id", "stable_id"])
def test_existing_public_skill_name_wins_without_becoming_a_package_alias(
    tmp_path, skill_catalog_factory, monkeypatch, selector,
):
    reads = []
    package = package_fixture(reads=reads)
    _, snapshot, agent, tool = discovery_fixture(tmp_path, skill_catalog_factory, [package])
    entry = replace(snapshot.entries[0], name=getattr(package, selector))
    scoped = replace(snapshot, entries=(entry,))
    agent.current_skill_snapshot = lambda: scoped
    pins = _observe_pins(monkeypatch)
    result = tool.execute({"action": "get", "skill_id": getattr(package, selector)})
    payload = json.loads(result.output)
    assert result.ok, result.output
    assert payload["skill_id"] == entry.stable_id and payload["body"].endswith("PUBLIC-BODY")
    assert "selector_mismatch" not in payload and "next_read" not in payload
    assert reads == pins == []


@pytest.mark.parametrize("reference", [
    "hidden-package", "capability:hidden-package", "missing-package", "capability:missing-package",
    "方法/私有独门/SKILL.md", "story", "capability:visible-package/方法/私有独门/SKILL.md",
])
def test_ungranted_unknown_or_guessed_selector_does_not_disclose_package_data(
    tmp_path, skill_catalog_factory, monkeypatch, reference,
):
    reads = []
    first, second = package_fixture("visible-package", reads=reads), package_fixture("hidden-package", reads=reads)
    _, snapshot, agent, tool = discovery_fixture(tmp_path, skill_catalog_factory, [first, second])
    scoped = snapshot.restricted([first.stable_id], expected_refs=[first.to_ref()])
    agent.current_skill_snapshot = lambda: scoped
    pins = _observe_pins(monkeypatch)
    result = tool.execute({"action": "get", "skill_id": reference})
    payload = json.loads(result.output)
    assert not result.ok and result.error_code == "TOOL_INVALID_ARGUMENTS"
    assert "selector_mismatch" not in payload and "next_read" not in payload and "body" not in payload
    assert "hidden-package" not in result.output and "私有独门" not in result.output
    assert reads == pins == []


@pytest.mark.parametrize("reference", ["story-a", "capability:story-a"])
def test_revoked_package_cannot_be_reintroduced_by_selector_recovery(tmp_path, monkeypatch, reference):
    agent, store, entries = _agent(tmp_path)
    tool = SkillSearchTool(agent)
    search = tool.execute({"action": "search", "query": "story-a"})
    card = next(row for row in json.loads(search.output)["matches"] if row.get("package_id") == "story-a")
    old = entries[0]
    store.change_activation(PluginActivationRequest("disable-a", old.revision, replace(old.activation, phase="revoked")))
    assert agent.current_skill_snapshot().resolve_package("story-a") is None
    pins = _observe_pins(monkeypatch)
    wrong = tool.execute({"action": "get", "skill_id": reference})
    payload = json.loads(wrong.output)
    assert not wrong.ok and "selector_mismatch" not in payload and "next_read" not in payload
    assert "story-a" not in wrong.output
    stale = tool.execute(card["next_read"])
    assert not stale.ok and "body" not in json.loads(stale.output)
    assert pins == []


@pytest.mark.parametrize("discovery", ["public_search", "selector_error", "resource_search"])
def test_reusing_suggested_arguments_promotes_real_task_before_first_package_pin(tmp_path, monkeypatch, discovery):
    agent, _store, entries = _agent(tmp_path)
    thread, params = _unpromoted_turn(agent)
    if discovery == "public_search":
        result = _execute(agent, params, {"action": "search", "query": "story-a"}, "discover-package")
        payload = next(row for row in json.loads(result.output)["matches"] if row.get("package_id") == "story-a")
    elif discovery == "resource_search":
        result = _execute(agent, params, {"action": "search", "package_id": "story-a"}, "discover-resource")
        payload = json.loads(result.output)["matches"][0]
    else:
        result = _execute(agent, params, {"action": "get", "skill_id": "capability:story-a"}, "wrong-selector")
        assert not result.ok
        payload = json.loads(result.output)
    assert agent.conversation_store.tasks.list(thread.thread_id) == []
    assert "conversation_task_id" not in params.task_attributes
    original_pin = task_references.pin_package_reference
    seen = []

    # LLM: 首次正文读取必须已由真实工具执行缝晋升；只观察原任务绑定，不为被测入口补任务或引用。
    # 函数用途: 在实际 pin 前确认任务存在，再继续执行原持久化操作。
    def observe_pin(current, reference):
        task_id = current._current_run_params.task_attributes["conversation_task_id"]
        task = current.conversation_store.tasks.load(task_id)
        assert task.thread_id == thread.thread_id and task.skill_snapshot_refs == ()
        seen.append(task_id)
        original_pin(current, reference)

    monkeypatch.setattr(task_references, "pin_package_reference", observe_pin)
    arguments = json.loads(json.dumps(payload["next_read"]))
    original_arguments = deepcopy(arguments)
    read = _execute(agent, params, arguments, "read-selected-package")
    assert read.ok, read.output
    assert arguments == original_arguments
    task = agent.conversation_store.tasks.load(params.task_attributes["conversation_task_id"])
    assert seen == [task.task_id]
    assert task.skill_snapshot_refs[0]["activation_id"] == entries[0].activation_id


def test_existing_action_defaults_and_mutually_exclusive_selectors_remain_explicit(
    tmp_path, skill_catalog_factory, monkeypatch,
):
    reads = []
    package = package_fixture("story-a", reads=reads)
    _, snapshot, _, tool = discovery_fixture(tmp_path, skill_catalog_factory, [package])
    pins = _observe_pins(monkeypatch)
    ordinary = tool.execute({"skill_id": snapshot.entries[0].stable_id})
    assert ordinary.ok and json.loads(ordinary.output)["body"].endswith("PUBLIC-BODY")
    listing = tool.execute({"package_id": package.package_id})
    assert listing.ok and "matches" in json.loads(listing.output)
    invalid = tool.execute({"action": "get", "package_id": package.package_id, "skill_id": snapshot.entries[0].stable_id})
    assert not invalid.ok and "next_read" not in json.loads(invalid.output)
    assert reads == pins == []


@pytest.mark.parametrize("discovery", ["public_search", "selector_error", "resource_search"])
def test_old_read_suggestion_rejects_reenabled_same_content_before_first_pin(tmp_path, monkeypatch, discovery):
    agent, store, entries = _agent(tmp_path)
    tool = SkillSearchTool(agent)
    if discovery == "public_search":
        found = tool.execute({"action": "search", "query": "story-a"})
        payload = next(row for row in json.loads(found.output)["matches"] if row.get("package_id") == "story-a")
    elif discovery == "resource_search":
        found = tool.execute({"action": "search", "package_id": "story-a"})
        payload = json.loads(found.output)["matches"][0]
    else:
        found = tool.execute({"action": "get", "skill_id": "capability:story-a"})
        payload = json.loads(found.output)
    old = entries[0]
    revoked = store.change_activation(PluginActivationRequest(
        "disable", old.revision, replace(old.activation, phase="revoked"),
    )).installation
    released, _ = store.release_activation(resolve_owner_home(agent.home_paths.root), None, revoked, "disable")
    row = released.installation
    activation = PluginContentActivation("new-enable", row.manifest.plugin_id, row.package_sha256, row.revision, row.settings_revision)
    store.change_activation(PluginActivationRequest("new-enable", row.revision, activation))
    fresh = agent.current_skill_snapshot().resolve_package("story-a")
    assert fresh.package_sha256 == old.package_sha256 and fresh.activation_id != old.activation_id
    pins = _observe_pins(monkeypatch)
    stale = tool.execute(payload["next_read"])
    assert not stale.ok and stale.error_code == "SKILL_SNAPSHOT_UNAVAILABLE"
    assert "SKILL_SNAPSHOT_STALE" in stale.output and "body" not in json.loads(stale.output)
    assert pins == []
