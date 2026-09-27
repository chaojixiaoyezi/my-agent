"""资源引用只解析当前宿主快照；使用本地安装/任务记录，不执行模型或包脚本。"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
from agent_py_agent.agent.capability.package_provider import enabled_capability_packages
from agent_py_agent.agent.capability.package_resources import (
    package_resource_reference,
    package_resource_reference_schema,
    resolve_package_resource,
)
from agent_py_agent.agent.capability.skill_snapshot import SkillSnapshot
from agent_py_agent.agent.contracts.tool_input_schema import validate_tool_input
from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.agent.runtime_context import (
    restore_current_subagent_context,
    set_current_subagent_context,
)
from agent_py_agent.agent.tooling.content_transport_policy import FileSourceUnavailableError
from agent_py_agent.tests.test_capability_activation import content_activation_fixture
from agent_py_agent.tests.test_capability_package_discovery import package_fixture
from agent_py_agent.tests.test_capability_package_task_refs import _agent, _bind_main_task


# LLM: 夹具只提供不可变快照与宿主闭包，不能用模型参数指定任意 owner 或回退到全局目录。
# 函数用途: 建立可记录实际读取次数的组件视图。
def _view(*packages):
    snapshot = SkillSnapshot((), (), "test", "local/main", "", tuple(packages))
    return SimpleNamespace(current_skill_snapshot=lambda: snapshot)


def test_resource_reference_transfers_binary_bytes_without_decoding_or_execution():
    raw = b"\x00\xff\xef\xbb\xbf\r\nprint('no execution')\r\n"
    reads = []
    package = package_fixture(files={"CAPABILITY.md": b"entry", "私有/asset.bin": raw}, reads=reads)
    reference = package_resource_reference(package, "私有/asset.bin")
    assert reads == []
    result = resolve_package_resource(_view(package), reference)
    assert result.data == raw and dict(result.source_ref) == reference
    assert reads == ["私有/asset.bin"]
    reference["resource_path"] = "different"
    assert result.source_ref["resource_path"] == "私有/asset.bin"
    with pytest.raises(TypeError):
        result.source_ref["name"] = "other"


def test_resource_schema_matches_produced_reference_and_is_an_isolated_host_value():
    reads = []
    package = package_fixture(reads=reads)
    reference = package_resource_reference(package, "CAPABILITY.md")
    schema = package_resource_reference_schema()
    assert set(schema["properties"]) == set(schema["required"]) == set(reference)
    assert schema["additionalProperties"] is False
    assert validate_tool_input(reference, schema).ok
    schema["properties"]["name"]["type"] = "integer"
    schema["required"].remove("name")
    fresh = package_resource_reference_schema()
    assert fresh["properties"]["name"]["type"] == "string" and "name" in fresh["required"]
    assert reads == []


@pytest.mark.parametrize("field", tuple(package_resource_reference(package_fixture(), "CAPABILITY.md")))
def test_direct_resource_resolution_preserves_every_required_field(field):
    reads = []
    package = package_fixture(reads=reads)
    reference = package_resource_reference(package, "CAPABILITY.md")
    del reference[field]
    with pytest.raises(ValueError, match="CAPABILITY_RESOURCE_REFERENCE_INVALID"):
        resolve_package_resource(_view(package), reference)
    assert reads == []


@pytest.mark.parametrize("change", [
    lambda ref: ref.pop("activation_id"),
    lambda ref: ref.update(owner_id="other"),
    lambda ref: ref.update(run_id="other"),
    lambda ref: ref.update(kind="skill"),
    lambda ref: ref.update(stable_id="capability:other"),
    lambda ref: ref.update(name="other"),
    lambda ref: ref.update(source="owner"),
    lambda ref: ref.update(content_sha256="bad"),
    lambda ref: ref.update(resource_path="../outside"),
    lambda ref: ref.update(resource_path="CAPABILITY.md/../other"),
    lambda ref: ref.update(resource_sha256="A" * 64),
    lambda ref: ref.update(resource_sha256=True),
])
def test_reference_rejects_incomplete_identity_and_extra_authority_fields_before_read(change):
    reads = []
    package = package_fixture(reads=reads)
    reference = package_resource_reference(package, "CAPABILITY.md")
    change(reference)
    with pytest.raises(ValueError):
        resolve_package_resource(_view(package), reference)
    assert reads == []


@pytest.mark.parametrize("change", [
    {"content_sha256": "f" * 64}, {"activation_id": "f" * 64},
    {"resource_sha256": "f" * 64}, {"resource_path": "not-declared.py"},
])
def test_valid_shaped_but_changed_source_is_not_followed(change):
    reads = []
    package = package_fixture(reads=reads)
    reference = {**package_resource_reference(package, "CAPABILITY.md"), **change}
    with pytest.raises(FileSourceUnavailableError):
        resolve_package_resource(_view(package), reference)
    assert reads == []


def test_resource_does_not_fall_back_to_unscoped_service_or_another_owner():
    package = package_fixture()
    reference = package_resource_reference(package, "CAPABILITY.md")
    missing_current = SimpleNamespace(skills_service=SimpleNamespace(snapshot_for=lambda: _view(package)))
    for agent in (missing_current, _view()):
        with pytest.raises(FileSourceUnavailableError):
            resolve_package_resource(agent, reference)


def test_corrupt_returned_bytes_fail_declared_digest_check():
    package = package_fixture()
    bad = replace(package, reader=lambda _: b"tampered")
    with pytest.raises(FileSourceUnavailableError):
        resolve_package_resource(_view(bad), package_resource_reference(package, "CAPABILITY.md"))


def test_old_turn_cannot_materialize_after_real_activation_revocation(tmp_path):
    owner, store, _entry, enable = content_activation_fixture(tmp_path)
    active = store.change_activation(enable).installation
    package = enabled_capability_packages(owner)[0]
    agent = _view(package)
    reference = package_resource_reference(package, "CAPABILITY.md")
    assert resolve_package_resource(agent, reference).data
    store.change_activation(PluginActivationRequest("revoke", active.revision, replace(active.activation, phase="revoked")))
    with pytest.raises(FileSourceUnavailableError):
        resolve_package_resource(agent, reference)


def test_resource_read_pins_original_main_task_and_child_keeps_its_scope(tmp_path):
    agent, _store, _entries = _agent(tmp_path)
    _bind_main_task(agent)
    packages = {item.package_id: item for item in agent.current_skill_snapshot().packages}
    ref_a = package_resource_reference(packages["story-a"], "methods/SKILL.md")
    ref_b = package_resource_reference(packages["story-b"], "methods/SKILL.md")
    assert resolve_package_resource(agent, ref_a).data == b"story-a"
    pins = agent.conversation_store.tasks.load("main-task").skill_snapshot_refs
    assert list(pins) == [packages["story-a"].to_ref()]
    result = CreateSubagentsTool(agent).execute({"goal": "使用已授权包分析材料", "allowed_skills": ["capability:story-a"], "defer_start": True})
    assert result.ok, result.output
    task = agent.subagents.list_runs()[0]
    previous = set_current_subagent_context(agent, run_id=task.id, task_attributes=task.attributes)
    try:
        assert resolve_package_resource(agent, ref_a).data == b"story-a"
        with pytest.raises(FileSourceUnavailableError):
            resolve_package_resource(agent, ref_b)
        task.attributes["skill_snapshot_refs"] = []
        agent.subagents.save(task)
        with pytest.raises(FileSourceUnavailableError):
            resolve_package_resource(agent, ref_a)
    finally:
        restore_current_subagent_context(agent, previous)


def test_main_task_pin_conflict_prevents_materialization(tmp_path, monkeypatch):
    agent, _store, _entries = _agent(tmp_path)
    _bind_main_task(agent)
    package = agent.current_skill_snapshot().packages[0]
    reference = package_resource_reference(package, "CAPABILITY.md")

    def reject_pin(**_kwargs):
        raise ValueError("SKILL_PACKAGE_REFERENCE_CONFLICT")

    monkeypatch.setattr(agent.conversation_store.tasks, "pin_skill_reference", reject_pin)
    with pytest.raises(FileSourceUnavailableError, match="REFERENCE_UNAVAILABLE"):
        resolve_package_resource(agent, reference)
