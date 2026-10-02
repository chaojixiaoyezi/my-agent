"""主任务按包隔离合法失效引用，保留唯一 pins、模型回复与原权限边界。"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.capability.package_resources import (
    package_resource_reference,
    resolve_package_resource,
)
from agent_py_agent.agent.capability.router import CapabilityRouter
from agent_py_agent.agent.capability.skill_search_tool import SkillSearchTool
from agent_py_agent.agent.capability.skill_snapshot import (
    SkillLoadError,
    SkillSnapshot,
    SkillSnapshotError,
)
from agent_py_agent.agent.conversation.authority import CONVERSATION_TASK_TURN_ACTIVE_ATTR
from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.agent.plugin_content_activation import PluginContentActivation
from agent_py_agent.agent.runtime_context import (
    restore_current_subagent_context,
    set_current_subagent_context,
)
from agent_py_agent.agent.tooling.content_transport_policy import FileSourceUnavailableError
from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
from agent_py_agent.tests.test_agent.backends import _TestNativeBackend
from agent_py_agent.tests.test_capability_package_discovery import discovery_fixture
from agent_py_agent.tests.test_capability_package_task_refs import _agent, _bind_main_task
from agent_py_agent.tests.test_decision_skill_projection import build


# LLM: 仅变更临时 owner 的原安装生命周期；不改快照接口，不伪造任务版本或自动安装替代包。
# 函数用途: 在已用包退出后产生真实的缺包或同字节新激活状态。
def _invalidate(agent, store, active, mode):
    revoked = store.change_activation(PluginActivationRequest("revoke-a", active.revision,
                                     replace(active.activation, phase="revoked"))).installation
    released, _ = store.release_activation(resolve_owner_home(agent.home_paths.root), None, revoked, "revoke-a")
    row = released.installation
    if mode == "removed":
        store.remove("remove-a", row.manifest.plugin_id, row)
    else:
        activation = PluginContentActivation("enable-a-again", row.manifest.plugin_id, row.package_sha256,
                                            row.revision, row.settings_revision)
        store.change_activation(PluginActivationRequest(activation.operation_id, row.revision, activation))


@pytest.mark.parametrize("mode,code", [("removed", "CAPABILITY_PACKAGE_PIN_UNAVAILABLE"),
                                      ("reenabled", "CAPABILITY_PACKAGE_PIN_STALE")])
def test_main_lost_package_keeps_other_package_but_rejects_old_and_current_sources(tmp_path, mode, code):
    agent, store, entries = _agent(tmp_path)
    attrs = _bind_main_task(agent)
    tool = SkillSearchTool(agent)
    first = tool.execute({"action": "get", "package_id": "story-a"})
    assert first.ok and tool.execute({"action": "get", "package_id": "story-b"}).ok
    source = json.loads(first.output)["source_ref"]
    pins = agent.conversation_store.tasks.load("main-task").skill_snapshot_refs
    _invalidate(agent, store, entries[0], mode)
    snapshot = agent.current_skill_snapshot()
    raw = agent.skills_service.snapshot_for(agent.effective_workspace_root)
    assert [row.package_id for row in snapshot.packages] == ["story-b"]
    assert [(error.path, error.code) for error in snapshot.errors if error.code == code] == [("capability:story-a", code)]
    assert snapshot.fingerprint != raw.fingerprint
    assert snapshot.fingerprint == agent.current_skill_snapshot().fingerprint
    assert not tool.execute({"action": "get", "package_id": "story-a"}).ok
    assert tool.execute({"action": "get", "package_id": "story-b"}).ok
    with pytest.raises(FileSourceUnavailableError, match="NOT_AVAILABLE"):
        resolve_package_resource(agent, source)
    if mode == "reenabled":
        current = raw.resolve_package("story-a")
        assert current.activation_id != source["activation_id"]
        with pytest.raises(FileSourceUnavailableError, match="NOT_AVAILABLE"):
            resolve_package_resource(agent, package_resource_reference(current, "CAPABILITY.md"))
    for selected in (None, ()):
        prompt = str(build(agent.prompts, selected=selected))
        assert prompt.count("# 当前任务不可用的能力包") == 1
        assert code in prompt and "capability:story-a" in prompt
        assert "package_id: story-a" not in prompt
        assert "methods/SKILL.md" not in prompt
    assert agent.conversation_store.tasks.load(attrs["conversation_task_id"]).skill_snapshot_refs == pins


@pytest.mark.parametrize("mode,code", [("removed", "CAPABILITY_PACKAGE_PIN_UNAVAILABLE"),
                                      ("reenabled", "CAPABILITY_PACKAGE_PIN_STALE")])
def test_main_lost_package_tells_model_to_report_instead_of_reauthorizing(tmp_path, mode, code):
    agent, store, entries = _agent(tmp_path)
    _bind_main_task(agent)
    tool = SkillSearchTool(agent)
    assert tool.execute({"action": "get", "package_id": "story-a"}).ok
    _invalidate(agent, store, entries[0], mode)
    # 真实 C3 形态：主任务旧 pin 停用或换代后续读，回执不能再叫模型找父代理重新授权。
    for arguments in ({"action": "get", "package_id": "story-a", "resource_path": "methods/SKILL.md"},
                      {"action": "search", "package_id": "story-a", "query": "方法"}):
        failed = tool.execute(arguments)
        assert not failed.ok and failed.error_code == "CAPABILITY_PACKAGE_TASK_PIN_UNAVAILABLE"
        assert failed.recommended_action == "report_blocker" and "父代理" not in failed.recovery_hint
        payload = json.loads(failed.output)
        assert payload["package_id"] == "story-a" and payload["details"] == {"error_code": code}
        assert "next_read" not in payload and "body" not in payload and "matches" not in payload
    never_pinned = tool.execute({"action": "get", "package_id": "story-missing"})
    assert never_pinned.error_code == "SKILL_SNAPSHOT_UNAVAILABLE"
    assert json.loads(never_pinned.output)["details"] == {"error_code": "CAPABILITY_PACKAGE_NOT_AVAILABLE"}


def test_non_pin_package_diagnostic_keeps_original_unavailable_error(tmp_path, skill_catalog_factory):
    _, snapshot, agent, tool = discovery_fixture(tmp_path, skill_catalog_factory, [])
    broken = SkillLoadError("capability:story-x", "CAPABILITY_PACKAGE_INVALID", "安装记录损坏", "capability_package")
    agent.current_skill_snapshot = lambda: replace(snapshot, errors=(*snapshot.errors, broken))
    failed = tool.execute({"action": "get", "package_id": "story-x"})
    assert failed.error_code == "SKILL_SNAPSHOT_UNAVAILABLE"
    assert json.loads(failed.output)["details"] == {"error_code": "CAPABILITY_PACKAGE_NOT_AVAILABLE"}


def test_removed_package_still_allows_real_model_turn_to_close_original_task(tmp_path):
    agent, store, entries = _agent(tmp_path)
    attrs = _bind_main_task(agent)
    assert SkillSearchTool(agent).execute({"action": "get", "package_id": "story-a"}).ok
    pins = agent.conversation_store.tasks.load("main-task").skill_snapshot_refs
    _invalidate(agent, store, entries[0], "removed")

    class Backend(_TestNativeBackend):
        name = "main-package-availability"

        def generate(self, prompt, on_chunk=None, **kwargs):
            assert "CAPABILITY_PACKAGE_PIN_UNAVAILABLE" in str(prompt) + json.dumps(kwargs.get("messages", []), ensure_ascii=False)
            return ModelResponse(text="现有来源可继续整理。", backend=self.name)

    agent.backend = Backend()
    result = agent.run("继续整理当前可用来源。", params=RunParams(
        save=False, request_id="normal-reply", task_id="main-task", source="gateway", context_scope="conversation",
        task_attributes={**attrs, CONVERSATION_TASK_TURN_ACTIVE_ATTR: True}, allowed_tools=["skill_search"],
    ))
    assert result.response == "现有来源可继续整理。"
    saved = agent.conversation_store.tasks.load("main-task")
    assert saved.status == "completed" and saved.skill_snapshot_refs == pins


def test_child_remains_strict_after_same_package_is_removed(tmp_path):
    agent, store, entries = _agent(tmp_path)
    assert CreateSubagentsTool(agent).execute({"goal": "整理已授权资料", "allowed_skills": ["capability:story-a"],
                                               "defer_start": True}).ok
    task = agent.subagents.list_runs()[0]
    _invalidate(agent, store, entries[0], "removed")
    previous = set_current_subagent_context(agent, run_id=task.id, task_attributes=task.attributes)
    try:
        with pytest.raises(SkillSnapshotError) as missing:
            agent.current_skill_snapshot()
        assert missing.value.error_code == "SKILL_NOT_AVAILABLE"
    finally:
        restore_current_subagent_context(agent, previous)


@pytest.mark.parametrize(("invalid", "error_code"), [
    ("malformed", "SKILL_TASK_BINDING_INVALID"), ("conflicting", "SKILL_PACKAGE_REFERENCE_CONFLICT"),
])
def test_bad_canonical_main_pins_are_not_downgraded_to_availability(tmp_path, invalid, error_code):
    agent, _store, _entries = _agent(tmp_path)
    _bind_main_task(agent)
    assert SkillSearchTool(agent).execute({"action": "get", "package_id": "story-a"}).ok
    path = agent.conversation_store.storage.task_path("main-task")
    data = json.loads(path.read_text(encoding="utf-8"))
    reference = data["skill_snapshot_refs"][0]
    if invalid == "malformed":
        reference.pop("activation_id")
    else:
        data["skill_snapshot_refs"].append({**reference, "activation_id": "f" * 64})
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SkillSnapshotError) as rejected:
        agent.current_skill_snapshot()
    assert rejected.value.error_code == error_code


def test_unknown_snapshot_failure_is_not_downgraded_to_missing_package(tmp_path, monkeypatch):
    agent, _store, _entries = _agent(tmp_path)
    _bind_main_task(agent)
    assert SkillSearchTool(agent).execute({"action": "get", "package_id": "story-a"}).ok

    def broken(_self, _package_id):
        raise RuntimeError("UNKNOWN_SNAPSHOT_FAILURE")

    monkeypatch.setattr(SkillSnapshot, "resolve_package", broken)
    with pytest.raises(RuntimeError, match="UNKNOWN_SNAPSHOT_FAILURE"):
        agent.current_skill_snapshot()


def test_unavailable_reference_projection_is_bounded_and_does_not_render_arbitrary_errors(tmp_path):
    agent, _store, _entries = _agent(tmp_path)
    snapshot = agent.current_skill_snapshot()
    snapshot = replace(snapshot, errors=(SkillLoadError("/private/hidden-path", "OTHER_ERROR", "private text", "capability_package"),))
    unavailable = {f"capability:package-{index:03d}": "CAPABILITY_PACKAGE_PIN_UNAVAILABLE" for index in range(50)}
    filtered = snapshot.without_unavailable_packages(unavailable)
    assert filtered.without_unavailable_packages(unavailable) is filtered
    text = CapabilityRouter(skill_snapshot=filtered).render_skill_metadata_index(selected_skill_ids=())
    diagnostic = text.split("\n\n", 1)[0]
    assert len(diagnostic) < 2000
    assert diagnostic.count('"reference"') == 8 and "另有 42 个" in diagnostic
    assert "private text" not in text and "/private/hidden-path" not in text
    with pytest.raises(ValueError, match="AVAILABILITY_INVALID"):
        snapshot.without_unavailable_packages({"capability:story-a": "UNKNOWN_FAILURE"})
