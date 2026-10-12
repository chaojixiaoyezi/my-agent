"""公共范围沿真实安装与消费用户快照验证，不启动插件、模型或网络。"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.capability.package_provider import (
    enabled_public_capability_packages,
    package_usage_enabled,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.agent.plugin_content_activation import PluginContentActivation
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_installation import PluginInstallationError, PluginInstallRequest
from agent_py_agent.agent.plugin_installation_state import decode_installation_state
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from agent_py_agent.tests.test_capability_package import content_bundle


# LLM: 全部状态在 conftest 隔离 home，通过正式 install/activation 而非手写启用标记；scope 由测试管理员动作选择。
# 函数用途: 安装并启用可读的内容包，让后续检查真实范围、摘要、撤销与持久化。
def install_content(store, name, scope):
    package = inspect_plugin_package(content_bundle(change=lambda payload: payload.update(plugin_id=name)))
    row = store.install(PluginInstallRequest(package, f"install-{name}", 0, installation_scope=scope)).installation
    activation = PluginContentActivation(f"enable-{name}", name, row.package_sha256, row.revision, row.settings_revision)
    row = store.change_activation(PluginActivationRequest(f"enable-{name}", row.revision, activation)).installation
    return row


def test_two_users_discover_public_only_without_plugin_execution_permission(tmp_path):
    admin = SimpleAgent(AgentConfig(enable_plugins=True, prompt_files=[]), tmp_path / "admin-work")
    store = PluginInstallStore(resolve_owner_home(admin.home_paths.root))
    install_content(store, "shared-methods", "public")
    install_content(store, "admin-private", "private")
    users = []
    for user_id in ("ou_a", "ou_b"):
        config = replace(admin.config, enable_plugins=False, my_agent_owner_provider="feishu", my_agent_owner_kind="user", my_agent_owner_id=user_id)
        owner = resolve_owner_home(admin.home_paths.root, OwnerIdentity("feishu", "user", user_id))
        agent = SimpleAgent(config, owner.workspace_dir)
        snapshot = agent.current_skill_snapshot()
        assert [package.package_id for package in snapshot.packages] == ["public/shared-methods"]
        assert package_usage_enabled(agent)
        assert snapshot.packages[0].read("CAPABILITY.md") == b"# Content\n"
        assert not agent._plugin_skill_roots()
        assert not agent.config.enable_plugins
        users.append(agent)
    assert users[0].home_paths.owner_home_dir != users[1].home_paths.owner_home_dir
    assert {package.package_id for package in admin.current_skill_snapshot().packages} == {"admin-private", "public/shared-methods"}


def test_only_admin_can_install_public_and_manifest_cannot_publish_itself(tmp_path):
    owner = resolve_owner_home(tmp_path, OwnerIdentity("feishu", "user", "ou_a"))
    store = PluginInstallStore(owner)
    request = PluginInstallRequest(inspect_plugin_package(content_bundle()), "publish", 0, installation_scope="public")
    with pytest.raises(PluginInstallationError, match="管理员"):
        store.install(request)
    assert not store.snapshot()


def test_public_reader_checks_revocation_and_old_snapshot_does_not_switch_generation(tmp_path):
    owner = resolve_owner_home(tmp_path)
    store = PluginInstallStore(owner)
    row = install_content(store, "shared-methods", "public")
    consumer = resolve_owner_home(tmp_path, OwnerIdentity("feishu", "user", "ou_a"))
    snapshot = enabled_public_capability_packages(consumer)[0]
    revoked = replace(row.activation, phase="revoked")
    store.change_activation(PluginActivationRequest("revoke", row.revision, revoked))
    assert not enabled_public_capability_packages(consumer)
    with pytest.raises(PluginInstallationError):
        snapshot.read("CAPABILITY.md")


def test_v4_migration_is_private_and_new_schema_missing_scope_is_rejected(tmp_path):
    owner = resolve_owner_home(tmp_path)
    store = PluginInstallStore(owner)
    install_content(store, "private-methods", "private")
    payload = json.loads((store.root / "installations.json").read_text())
    assert payload["schema_version"] == "plugin_installations.v5"
    del payload["installations"][0]["installation_scope"]
    with pytest.raises(ValueError):
        decode_installation_state(json.dumps(payload).encode(), owner.identity)
    payload["schema_version"] = "plugin_installations.v4"
    migrated = decode_installation_state(json.dumps(payload).encode(), owner.identity)
    assert migrated.entries[0].installation_scope == "private"
    assert json.loads(migrated.migration_json)["from_schema"] == "plugin_installations.v4"


def test_public_scope_update_is_preserved_and_private_request_cannot_reclassify(tmp_path):
    from agent_py_agent.agent.plugin_update import PluginUpdateRequest

    store = PluginInstallStore(resolve_owner_home(tmp_path))
    old = store.install(PluginInstallRequest(inspect_plugin_package(content_bundle()), "install", 0, installation_scope="public")).installation
    package = inspect_plugin_package(content_bundle(change=lambda payload: payload.update(version="2.0")))
    with pytest.raises(PluginInstallationError, match="范围"):
        store.update_package(PluginUpdateRequest(old.manifest.plugin_id, package, "update-private", old.revision))
    request = PluginUpdateRequest(old.manifest.plugin_id, package, "update-public", old.revision, installation_scope="public")
    updated = store.update_package(request).installation
    assert updated.installation_scope == "public"
    assert store.update_package(request).outcome == "replayed"
    with pytest.raises(PluginInstallationError, match="重送"):
        store.update_package(replace(request, installation_scope="private"))


def test_public_read_and_child_grant_keep_user_authority_and_exact_version(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
    from agent_py_agent.agent.capability.skill_search_tool import SkillSearchTool
    from agent_py_agent.agent.runtime_context import (
        restore_current_subagent_context,
        set_current_subagent_context,
    )

    admin = SimpleAgent(AgentConfig(enable_plugins=True, prompt_files=[]), tmp_path / "admin")
    installs = PluginInstallStore(resolve_owner_home(admin.home_paths.root))
    published = install_content(installs, "shared", "public")
    owner = resolve_owner_home(admin.home_paths.root, OwnerIdentity("feishu", "user", "ou_a"))
    config = replace(admin.config, enable_plugins=False, my_agent_owner_provider="feishu", my_agent_owner_kind="user", my_agent_owner_id="ou_a")
    user = SimpleAgent(config, owner.workspace_dir)
    result = SkillSearchTool(user).execute({"action": "get", "package_id": "public/shared"})
    assert result.ok, result.output
    created = CreateSubagentsTool(user).execute({"goal": "读取共享方法", "allowed_skills": ["capability:public/shared"], "defer_start": True})
    assert created.ok, created.output
    child = user.subagents.list_runs()[0]
    ref = child.attributes["skill_snapshot_refs"][0]
    assert ref["activation_id"] == published.activation_id
    previous = set_current_subagent_context(user, run_id=child.id, task_attributes=child.attributes)
    try:
        assert SkillSearchTool(user).execute({"action": "get", "package_id": "public/shared"}).ok
        assert not SkillSearchTool(user).execute({"action": "get", "package_id": "admin-private"}).ok
        installs.change_activation(PluginActivationRequest("disable", published.revision, replace(published.activation, phase="revoked")))
        assert not SkillSearchTool(user).execute({"action": "get", "package_id": "public/shared"}).ok
    finally:
        restore_current_subagent_context(user, previous)
