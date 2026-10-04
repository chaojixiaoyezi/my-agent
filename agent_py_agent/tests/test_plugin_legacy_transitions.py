"""真实管理/安装迁移入口；仅外部清理证明使用替身，不证明 OS 退出或真实用户渠道。"""
import pytest

from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.agent.plugin_activation_record import PluginActivation
from agent_py_agent.agent.plugin_environment_plan import plan_plugin_environment
from agent_py_agent.agent.plugin_permissions.grants import permission_details, select_permissions
from agent_py_agent.agent.plugin_permissions.state import (
    canonical_permission_json,
    legacy_permissions,
)
from agent_py_agent.tests.test_plugin_activation import publication
from agent_py_agent.tests.test_plugin_legacy_management import (
    confirm,
    fake_processes,
    fake_settled_deactivation,
    installed_manager,
    preview,
)
from agent_py_agent.tests.test_plugin_legacy_state import upgrade_source
from agent_py_agent.tests.test_plugin_package import _bundle


# LLM: 仅准备明确来源的原表固定激活，不运行插件；兼容必须经真实 v3 来源，不能直接塞标签。
# 函数用途: 为更新/重装矩阵提供三种可核验旧状态。
def active_source(service, mode):
    entry = service.installations.snapshot()[0]
    plan = plan_plugin_environment(entry, "old-fixed-activation")
    grant = None if mode == "legacy_compat" else canonical_permission_json(
        permission_details(entry, plan, select_permissions({}), mode))
    request = PluginActivationRequest(plan.operation_id, entry.revision, PluginActivation(plan, "preparing", permission_json=grant))
    service.installations.change_activation(publication(service.installations.change_activation(request)))
    if mode == "legacy_compat":
        upgrade_source(service.installations)
    old = service.installations.snapshot()[0]
    assert legacy_permissions(old)["mode"] == mode
    return old


@pytest.mark.parametrize("mode", ["legacy_compat", "restricted", "wide"])
@pytest.mark.parametrize("action", ["update", "reinstall"])
def test_update_reinstall_end_old_grant_and_require_fresh_complete_confirmation(tmp_path, monkeypatch, mode, action):
    from agent_py_agent.agent.plugin_permissions import enable

    service = installed_manager(tmp_path)
    old = active_source(service, mode)
    old_preview = preview(service, request="old-preview")
    source = tmp_path / "next.zip"
    source.write_bytes(_bundle(change=lambda manifest: manifest.update(version="2.0")))
    command = f'/plugins update sample-peek "{source}"'
    refused = service.command(command, revision=service.catalog().revision, request_id="active-update")
    assert refused["details"]["reason"] == "activation_unsettled" and service.installations.snapshot()[0] == old
    fake_settled_deactivation(monkeypatch)
    monkeypatch.setattr("agent_py_agent.agent.plugin_disable_tool.deactivate_plugin", enable.deactivate_plugin)
    disabled = service.command("/plugins disable sample-peek", revision=service.catalog().revision, request_id="disable")
    assert disabled["state"] == "succeeded", disabled
    if action == "reinstall":
        removed = service.command("/plugins remove sample-peek", revision=service.catalog().revision, request_id="remove")
        assert removed["state"] == "succeeded", removed
        command = f'/plugins install "{source}"'
    changed = service.command(command, revision=service.catalog().revision, request_id="change-package")
    assert changed["state"] == "succeeded", changed
    current = service.installations.snapshot()[0]
    assert current.activation is None and legacy_permissions(current) is None and current.installation_ref != old.installation_ref
    assert current.manifest.version == "2.0" and current.package_sha256 != old.package_sha256
    stale = confirm(service, old_preview, "stale-confirm")
    assert stale["state"] != "succeeded" and service.installations.snapshot()[0] == current
    fresh = preview(service, request="fresh-preview")
    facts = fresh["details"]["confirmation"]
    assert facts["mode"] == "restricted" and facts["confirm_code"] != old_preview["details"]["confirmation"]["confirm_code"]
    calls = fake_processes(monkeypatch)
    result = confirm(service, fresh)
    assert result["details"]["reason"] == "legacy_sandbox_pending" and calls == []


@pytest.mark.parametrize("mode,default", [("legacy_compat", True), ("wide", True), ("restricted", False)])
def test_single_mode_decision_reenable_retires_old_grant_but_never_starts_without_b7(tmp_path, monkeypatch, mode, default):
    service = installed_manager(tmp_path, legacy_sandbox_default=default)
    old = active_source(service, mode)
    calls = fake_settled_deactivation(monkeypatch)
    response = preview(service)
    assert response["details"]["confirmation"]["mode"] == "restricted"
    assert service.installations.snapshot()[0] == old and calls == []
    startups = fake_processes(monkeypatch)
    result = confirm(service, response)
    assert result["details"]["reason"] == "legacy_sandbox_pending" and calls == [old.activation_id]
    assert startups == [] and service.installations.snapshot()[0].activation is None
    assert legacy_permissions(service.installations.snapshot()[0]) is None
