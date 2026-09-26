"""能力包发现与读取合同：只用本地快照和临时原安装表，不调用模型或执行包内脚本。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability import CapabilityRouter, SkillSnapshotError
from agent_py_agent.agent.capability.decision_candidates import (
    capability_candidates,
    required_capabilities,
)
from agent_py_agent.agent.capability.decision_recommendation import _project
from agent_py_agent.agent.capability.package_provider import (
    enabled_capability_packages,
    read_capability_member,
)
from agent_py_agent.agent.capability.package_snapshot import CapabilityPackageSnapshot
from agent_py_agent.agent.capability.skill_search_tool import SkillSearchTool
from agent_py_agent.agent.capability_package_manifest import CapabilityFile
from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_installation import PluginInstallationError
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
from agent_py_agent.tests.test_capability_activation import content_activation_fixture


# LLM: 测试包只有声明字节和读回函数；摘要覆盖测试清单，不能把这类替身当真实安装/模型验收。
# 函数用途: 构造可观察读取次数、换代和篡改的不可变内容快照。
def package_fixture(package_id="story-content", *, files=None, generation="one", reads=None):
    files = files or {"CAPABILITY.md": "入口正文abcdefghijk".encode(),
                      "方法/私有独门/SKILL.md": b"PRIVATE-METHOD-BODY", "scripts/private.py": b"raise RuntimeError('do not execute')"}
    members = tuple(CapabilityFile(path, hashlib.sha256(body).hexdigest()) for path, body in files.items())

    # LLM: 只返回已声明字节，reads 记录测试调用；不写业务产物或执行资源。
    # 函数用途: 帮助证明发现不预读，而显式 get 才读取选定成员。
    def reader(path):
        if reads is not None:
            reads.append(path)
        return files[path]

    digest = hashlib.sha256(b"".join(item.path.encode() + files[item.path] for item in members)).hexdigest()
    return CapabilityPackageSnapshot(package_id, "1.0", "内容制作能力", "把长篇故事整理成分镜",
                                     ("分镜", "故事"), "CAPABILITY.md", digest,
                                     hashlib.sha256(generation.encode()).hexdigest(), members, reader)


# LLM: 所有源只在 pytest 临时目录；同名公开 Skill 刻意用于检验两个命名域不互相去重。
# 函数用途: 用真实配置、SkillsService 和 Router 装配包快照及一个普通用户 Skill。
def discovery_fixture(tmp_path, skill_catalog_factory, packages):
    catalog = skill_catalog_factory(tmp_path)
    skill_dir = catalog.home.owner_home_dir / "skills" / "story-content"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: story-content\ndescription: 普通用户方法\n---\nPUBLIC-BODY", encoding="utf-8")
    catalog.service.package_provider = lambda: packages
    snapshot = catalog.service.snapshot_for()
    agent = SimpleNamespace(current_skill_snapshot=lambda: snapshot,
                            capability_router=CapabilityRouter(skill_snapshot=snapshot),
                            config=AgentConfig(tool_read_max_chars=7))
    return catalog, snapshot, agent, SkillSearchTool(agent)


def test_private_members_never_enter_global_catalog_search_counts_or_decision(tmp_path, skill_catalog_factory):
    reads = []
    package = package_fixture(reads=reads)
    _, snapshot, agent, tool = discovery_fixture(tmp_path, skill_catalog_factory, [package])
    assert len(snapshot.entries) == 1 and snapshot.resolve("story-content").source == "owner"
    assert snapshot.resolve(package.stable_id) is None
    assert snapshot.resolve_reference(package.stable_id) is package
    assert [card.kind for card in agent.capability_router.cards(kinds={"skill"})] == ["skill"]
    assert "能力包" not in agent.capability_router.render_category_index()
    rendered = agent.capability_router.render_skill_metadata_index()
    assert "# 能力包" in rendered and "package_id: story-content" in rendered
    assert "私有独门" not in rendered and "PRIVATE-METHOD-BODY" not in rendered
    miss = json.loads(tool.execute({"query": "私有独门"}).output)
    assert miss["matches"] == []
    hit = json.loads(tool.execute({"query": "分镜"}).output)["matches"]
    assert len(hit) == 1 and hit[0]["kind"] == "capability_package"
    rows = capability_candidates(SimpleNamespace(runtimes=()), snapshot, categories=[], skills_discoverable=True)
    assert [row["kind"] for row in rows] == ["skill", "capability_package"]
    assert "私有独门" not in json.dumps(rows, ensure_ascii=False) and reads == []


def test_package_scope_uses_composite_member_identity_and_exact_task_refs(tmp_path, skill_catalog_factory):
    first, second = package_fixture("first"), package_fixture("second")
    _, snapshot, _, _ = discovery_fixture(tmp_path, skill_catalog_factory, [first, second])
    selected = snapshot.restricted([first.stable_id], expected_sha256={first.stable_id: first.package_sha256},
                                   expected_refs=[first.to_ref()])
    assert selected.entries == () and selected.packages == (first,)
    assert selected.resolve_in_package("second", "CAPABILITY.md") is None
    assert selected.resolve_in_package("first", "CAPABILITY.md") is first.members[0]
    assert selected.resolve_in_package("first", "../CAPABILITY.md") is None
    for refs in ([], [{**first.to_ref(), "activation_id": second.activation_id + "x"}],
                 [{**first.to_ref(), "content_sha256": "0" * 64}]):
        with pytest.raises(SkillSnapshotError):
            snapshot.restricted([first.stable_id], expected_refs=refs)
    with pytest.raises(SkillSnapshotError):
        snapshot.restricted([first.stable_id], expected_sha256={first.stable_id: "0" * 64})
    with pytest.raises(SkillSnapshotError, match="CONFLICT"):
        snapshot.restricted([first.stable_id], expected_refs=[
            {**first.to_ref(), "activation_id": "0" * 64}, first.to_ref(),
        ])


def test_canonical_package_reference_cannot_be_shadowed_by_public_skill_name(tmp_path, skill_catalog_factory):
    package = package_fixture()
    _, snapshot, _, _ = discovery_fixture(tmp_path, skill_catalog_factory, [package])
    masquerade = replace(snapshot.entries[0], name=package.stable_id)
    colliding = replace(snapshot, entries=(masquerade,))
    assert colliding.resolve(package.stable_id) is masquerade
    assert colliding.resolve_reference(package.stable_id) is package
    assert colliding.resolve_reference(masquerade.stable_id) is masquerade
    assert replace(colliding, packages=()).resolve_reference(package.stable_id) is None


def test_fingerprint_tracks_reinstall_and_provider_failure_preserves_ordinary_skills(tmp_path, skill_catalog_factory):
    packages = [package_fixture()]
    catalog, first, _, _ = discovery_fixture(tmp_path, skill_catalog_factory, packages)
    assert catalog.service.snapshot_for() is first
    packages[0] = replace(packages[0], activation_id="f" * 64)
    second = catalog.service.snapshot_for()
    assert second is not first and second.fingerprint != first.fingerprint
    with pytest.raises(SkillSnapshotError, match="SKILL_SNAPSHOT_STALE"):
        second.restricted([first.packages[0].stable_id], expected_refs=[first.packages[0].to_ref()])

    def broken():
        raise ValueError("unreadable installation table")

    catalog.service.package_provider = broken
    failed = catalog.service.snapshot_for()
    assert len(failed.entries) == 1 and failed.packages == ()
    assert failed.errors[0].code == "CAPABILITY_PACKAGE_DISCOVERY_FAILED"
    catalog.policy.skills_enabled = False
    assert catalog.service.snapshot_for().reference_entries() == ()


def test_package_resource_search_and_get_are_explicit_and_paginated(tmp_path, skill_catalog_factory):
    package = package_fixture()
    _, _, _, tool = discovery_fixture(tmp_path, skill_catalog_factory, [package])
    listed = json.loads(tool.execute({"action": "search", "package_id": package.package_id, "limit": 1}).output)
    assert len(listed["matches"]) == 1 and listed["has_more"]
    assert listed["matches"][0]["source_ref"] == {
        **package.to_ref(), "resource_path": listed["matches"][0]["resource_path"],
        "resource_sha256": listed["matches"][0]["content_sha256"],
    }
    next_page = json.loads(tool.execute(listed["continuation"]).output)
    assert next_page["offset"] == 1 and next_page["matches"] != listed["matches"]
    scoped = json.loads(tool.execute({"query": "私有独门", "package_id": package.package_id}).output)
    assert scoped["matches"][0]["resource_path"] == "方法/私有独门/SKILL.md"
    params = {"action": "get", "package_id": package.package_id, "max_chars": 200}
    bodies = []
    while True:
        outcome = tool.execute(params)
        assert outcome.ok, outcome.output
        payload = json.loads(outcome.output)
        assert len(payload["body"]) <= 7 and "path" not in payload
        assert payload["source_ref"] == {**package.to_ref(), "resource_path": "CAPABILITY.md",
                                         "resource_sha256": package.members[0].sha256}
        bodies.append(payload["body"])
        if not payload["has_more"]:
            break
        params = payload["continuation"]
    assert "".join(bodies) == package.read().decode()
    stale = tool.execute({**params, "expected_activation_id": "0" * 64})
    assert not stale.ok and stale.error_code == "SKILL_SNAPSHOT_UNAVAILABLE"


@pytest.mark.parametrize("params", [
    {"action": "get", "resource_path": "CAPABILITY.md"},
    {"action": "get", "package_id": "story-content", "resource_path": "../CAPABILITY.md"},
    {"action": "get", "package_id": "story-content", "skill_id": "owner:story-content"},
    {"action": "get", "package_id": "story-content", "offset": True},
    {"action": "get", "package_id": "story-content", "max_chars": 0},
    {"action": "get", "package_id": "story-content", "offset": 9999},
    {"action": "get", "skill_id": "capability:story-content"},
])
def test_guessed_scope_and_invalid_windows_fail_closed(tmp_path, skill_catalog_factory, params):
    _, _, _, tool = discovery_fixture(tmp_path, skill_catalog_factory, [package_fixture()])
    outcome = tool.execute(params)
    assert not outcome.ok and "入口正文" not in outcome.output


def test_read_tampering_binary_and_pin_failure_do_not_deliver_body(tmp_path, skill_catalog_factory, monkeypatch):
    package = package_fixture()
    packages = [replace(package, reader=lambda path: b"changed")]
    _, _, _, tool = discovery_fixture(tmp_path, skill_catalog_factory, packages)
    params = {"action": "get", "package_id": package.package_id}
    assert not tool.execute(params).ok
    binary = package_fixture(files={"CAPABILITY.md": b"\xff\xfe"})
    _, _, _, binary_tool = discovery_fixture(tmp_path / "binary", skill_catalog_factory, [binary])
    outcome = binary_tool.execute(params)
    assert not outcome.ok and "CAPABILITY_RESOURCE_NOT_TEXT" in outcome.output

    def deny_pin(*args):
        raise SkillSnapshotError("SKILL_TASK_REFERENCE_UNAVAILABLE")

    monkeypatch.setattr("agent_py_agent.agent.capability.task_references.pin_package_reference", deny_pin)
    _, _, _, valid_tool = discovery_fixture(tmp_path / "pin", skill_catalog_factory, [package])
    outcome = valid_tool.execute(params)
    assert not outcome.ok and "入口正文" not in outcome.output


def test_decision_selection_uses_only_package_ref_and_preserves_required_package(tmp_path, skill_catalog_factory):
    package = package_fixture()
    _, skills, agent, _ = discovery_fixture(tmp_path, skill_catalog_factory, [package])
    params = SimpleNamespace(task_attributes={"skill_snapshot_refs": [package.to_ref()]},
                             context_scope="default", allowed_tools=[])
    tools, required = required_capabilities(params, None, skills)
    assert tools == set() and required == (package.stable_id,)
    snapshot = SimpleNamespace(runtimes=(), available_tool_names=frozenset(), allowed_tools=[])
    presentation = _project(params, snapshot, None, skills, [{"kind": "capability_package", "ref": package.stable_id}],
                            {"optional_categories": [], "context_policy": "progressive"}, True)
    assert presentation.selected_skill_ids == (package.stable_id,) and presentation.required_skill_ids == required
    rendered = agent.capability_router.render_skill_metadata_index(selected_skill_ids=(), required_skill_ids=required)
    assert "package_id: story-content" in rendered and "私有独门" not in rendered


def test_real_store_provider_reads_private_archive_and_rejects_revocation(tmp_path):
    owner, store, _, request = content_activation_fixture(tmp_path)
    assert enabled_capability_packages(owner) == ()
    active = store.change_activation(request).installation
    package = enabled_capability_packages(owner)[0]
    assert package.read("methods/SKILL.md") == b"private method"
    assert not (owner.plugins_dir / "environments").exists()
    other = PluginInstallStore(resolve_owner_home(tmp_path / "another"))
    with pytest.raises(PluginInstallationError):
        read_capability_member(other, active, "CAPABILITY.md")
    store.change_activation(PluginActivationRequest("disable", active.revision, replace(active.activation, phase="revoked")))
    assert enabled_capability_packages(owner) == ()
    with pytest.raises(PluginInstallationError):
        package.read("CAPABILITY.md")


def test_read_rechecks_original_installation_after_member_bytes(tmp_path, monkeypatch):
    from agent_py_agent.agent import plugin_package

    owner, store, _, request = content_activation_fixture(tmp_path)
    active = store.change_activation(request).installation
    package = enabled_capability_packages(owner)[0]
    original = plugin_package.read_plugin_member

    def revoke_after_read(*args, **kwargs):
        content = original(*args, **kwargs)
        store.change_activation(PluginActivationRequest("disable", active.revision, replace(active.activation, phase="revoked")))
        return content

    monkeypatch.setattr(plugin_package, "read_plugin_member", revoke_after_read)
    with pytest.raises(PluginInstallationError):
        package.read("CAPABILITY.md")
