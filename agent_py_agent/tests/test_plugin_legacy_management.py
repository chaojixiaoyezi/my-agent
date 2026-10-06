"""原管理员/HostCommand/ToolExecutor 入口合同；进程边界用替身，不宣称 OS 或真实通道通过。"""
import json
import shlex
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.plugin_activation import PluginActivationRequest, prepare_release
from agent_py_agent.agent.plugin_activation_record import plugin_catalog_digest
from agent_py_agent.agent.plugin_management import PluginManagement
from agent_py_agent.agent.plugin_permissions.state import legacy_permissions
from agent_py_agent.tests.test_plugin_management import manager


def installed_manager(tmp_path, **changes):
    service, source = manager(tmp_path, **changes)
    result = service.command(f'/plugins install "{source}"', revision=service.catalog().revision, request_id="install")
    assert result["state"] == "succeeded", result
    return service


def preview(service, flags="", request="preview"):
    result = service.command(f"/plugins enable sample-peek {flags}", revision=service.catalog().revision,
                             request_id=request)
    assert result["error_code"] == "PLUGIN_CONFIRMATION_REQUIRED", result
    return result


def confirm(service, response, request="transport-confirm"):
    facts = response["details"]["confirmation"]
    return service.command(facts["confirm_command"], revision=service.catalog().revision, request_id=request)


def fake_processes(monkeypatch):
    """只替换外部准备/MCP；实际安装 CAS、授权、原 operation claim 和发布继续运行。"""
    calls = []
    monkeypatch.setattr("agent_py_agent.agent.plugin_enable_tool.prepare_plugin_environment",
                        lambda *args: calls.append("prepare"))

    class Client:
        def __init__(self, owner, entry, **kwargs):
            calls.append("client")
            self.entry = entry

        def start(self):
            return object()

        def discover_tools(self, transport):
            assert self.entry.activation.phase == "preparing"
            return []

        def stop(self):
            return SimpleNamespace(confirmed=True, record={"session_id": "fake-candidate"}, terminations=())

    monkeypatch.setattr("agent_py_agent.agent.plugin_enable_tool.PluginMCPClient", Client)
    return calls


def fake_sandbox_ready(monkeypatch):
    """沙箱内没有嵌套 Seatbelt/bwrap；只放行平台就绪检查。

    真实隔离由沙箱外车道与合同用例（argv 包装、spec 字段）覆盖，不把替身当 OS 验收。
    """
    monkeypatch.setattr("agent_py_agent.agent.plugin_sandbox.AttemptExecutionSandbox.require_ready",
                        lambda _self: None)


def _sandbox_unavailable(_self):
    from agent_py_agent.agent.attempt.sandbox import SandboxUnavailableError

    raise SandboxUnavailableError("SANDBOX_UNAVAILABLE: test")


def fake_settled_deactivation(monkeypatch, *, settled=True):
    """注入清理证明边界；撤销/释放仍在原安装 Store，明确不作 OS 退出证据。"""
    from agent_py_agent.agent.plugin_install_store import PluginInstallStore

    calls = []

    def deactivate(owner, repo, entry, operation):
        calls.append(entry.activation_id)
        store = PluginInstallStore(owner)
        stopped = store.change_activation(PluginActivationRequest(
            operation, entry.revision, replace(entry.activation, phase="revoked"))).installation
        if settled:
            stopped = store._write(lambda quota: store._update_locked(
                quota, lambda entries: prepare_release(operation, stopped, entries)), reserve_quota=False).installation
        return SimpleNamespace(installation=stopped, report={
            "plugin_id": entry.manifest.plugin_id, "activation_id": entry.activation_id,
            "authority_revoked": True, "cleanup_confirmed": settled, "released": settled,
            "sessions": [], "errors": [],
        })

    monkeypatch.setattr("agent_py_agent.agent.plugin_permissions.enable.deactivate_plugin", deactivate)
    return calls


def test_admin_flags_complete_preview_and_confirm_reaches_restricted_gate(tmp_path, monkeypatch):
    import platform as platform_module

    service = installed_manager(tmp_path)
    fake_sandbox_ready(monkeypatch)
    root = (tmp_path / "project").resolve()
    root.mkdir()
    program = root / "program"
    program.write_bytes(b"fixture extra program, never executed")
    flags = f"--read-root {shlex.quote(str(root))} --write-root {shlex.quote(str(root))} --network --program-root {shlex.quote(str(program))}"
    if platform_module.system() == "Linux":
        # Linux 端口隔离未接入前，network:true 的收紧插件在预览阶段就结构化拒绝、零副作用。
        refused = service.command(f"/plugins enable sample-peek {flags}",
                                  revision=service.catalog().revision, request_id="linux-network")
        assert refused["details"]["reason"] == "gateway_port_isolation_unavailable", refused
        assert service.installations.snapshot()[0].activation is None
        return
    response = preview(service, flags)
    facts = response["details"]["confirmation"]
    assert facts["kind"] == "plugin_legacy_permissions"
    assert facts["mode"] == "restricted" and facts["permissions"]["network"] is True
    assert facts["permissions"]["program_roots"][0]["content_sha256"]
    assert str(root) in response["message"] and str(program) in response["message"]
    assert "这个插件能连网，也能连本机端口" in response["message"]
    assert "--authorization" in facts["confirm_command"] and "--read-root" in facts["confirm_command"]
    calls = fake_processes(monkeypatch)
    result = confirm(service, response)
    assert result["state"] == "succeeded", result
    assert calls == ["prepare", "client"]
    assert service.installations.snapshot()[0].activation is not None


def test_restricted_preflight_requires_unified_sandbox(tmp_path, monkeypatch):
    """restricted 预检必须要求统一沙箱可用；不可用时结构化拒绝、零副作用。"""
    service = installed_manager(tmp_path)
    monkeypatch.setattr("agent_py_agent.agent.plugin_sandbox.AttemptExecutionSandbox.require_ready",
                        _sandbox_unavailable)
    result = service.command("/plugins enable sample-peek", revision=service.catalog().revision, request_id="no-sandbox")
    assert result["details"]["reason"] == "sandbox_unavailable", result
    assert service.installations.snapshot()[0].activation is None


def test_wide_still_requires_confirmation_and_persists_actual_plan(tmp_path, monkeypatch):
    service = installed_manager(tmp_path, legacy_sandbox_default=False)
    response = preview(service)
    facts = response["details"]["confirmation"]
    assert facts["mode"] == "wide" and "不限制系统用户可读写范围" in response["message"]
    calls = fake_processes(monkeypatch)
    result = confirm(service, response)
    assert result["state"] == "succeeded", result
    entry = service.installations.snapshot()[0]
    assert legacy_permissions(entry)["mode"] == "wide"
    assert entry.activation_id == facts["activation_id"]
    assert entry.activation.plan.operation_id == facts["plan"]["operation_id"]
    assert calls == ["prepare", "client"]
    replay = confirm(service, response, "different-transport-id")
    assert replay["details"] == result["details"] and calls == ["prepare", "client"]


def test_changed_permission_or_path_identity_invalidates_confirmation(tmp_path, monkeypatch):
    service = installed_manager(tmp_path)
    fake_sandbox_ready(monkeypatch)
    root = (tmp_path / "project").resolve()
    root.mkdir()
    first = preview(service, f"--read-root {shlex.quote(str(root))}")
    facts = first["details"]["confirmation"]
    # 权限集合变化用“再加一个读目录”触发：Linux 上沙箱插件请求网络会先被端口隔离预检拒绝（plugin_sandbox_problem），
    #   到不了确认门；加读目录在两个平台都会真正改变授权事实（3a 10-05，Linux 车道发现）。
    other = (tmp_path / "other").resolve()
    other.mkdir()
    command = facts["confirm_command"] + f" --read-root {shlex.quote(str(other))}"
    changed = service.command(command, revision=service.catalog().revision, request_id="changed")
    assert changed["error_code"] == "PLUGIN_CONFIRMATION_REQUIRED", changed
    assert changed["details"]["confirmation"]["confirm_code"] != facts["confirm_code"]
    assert service.installations.snapshot()[0].activation is None


def test_nonadmin_cannot_submit_permission_flags_or_read_preview(tmp_path, monkeypatch):
    service = installed_manager(tmp_path)
    fake_sandbox_ready(monkeypatch)
    response = preview(service)
    user = PluginManagement(replace(service.context, is_admin=False))
    before = (service.installations.root / "installations.json").read_bytes()
    denied = confirm(user, response)
    assert denied["error_code"] == "PLUGIN_PERMISSION_DENIED"
    queried = user.command("/plugins status preview", revision="", request_id="query")
    assert queried["error_code"] == "PLUGIN_PERMISSION_DENIED"
    assert (service.installations.root / "installations.json").read_bytes() == before


def test_active_reenable_previews_then_revokes_old_generation(tmp_path, monkeypatch):
    service = installed_manager(tmp_path, legacy_sandbox_default=False)
    fake_processes(monkeypatch)
    assert confirm(service, preview(service))["state"] == "succeeded"
    old = service.installations.snapshot()[0]
    calls = fake_settled_deactivation(monkeypatch)
    second = preview(service, request="reenable")
    assert service.installations.snapshot()[0] == old and calls == []
    result = confirm(service, second)
    assert result["state"] == "succeeded", result
    current = service.installations.snapshot()[0]
    assert calls == [old.activation_id] and current.activation_id != old.activation_id
    assert current.activation_id == second["details"]["confirmation"]["activation_id"]
    assert current.activation.catalog_sha256 == plugin_catalog_digest(current.manifest)


def test_unconfirmed_old_exit_never_prepares_new_generation(tmp_path, monkeypatch):
    service = installed_manager(tmp_path, legacy_sandbox_default=False)
    calls = fake_processes(monkeypatch)
    assert confirm(service, preview(service))["state"] == "succeeded"
    fake_settled_deactivation(monkeypatch, settled=False)
    result = confirm(service, preview(service, request="reenable"))
    assert result["state"] == "outcome_unknown", result
    assert result["details"]["previous_cleanup"]["released"] is False
    assert result["details"]["previous_cleanup"]["source"] == "installation_table"
    assert result["details"]["previous_cleanup"]["cleanup_state"] == "unverified"
    assert service.installations.snapshot()[0].activation.phase == "revoked"
    assert calls == ["prepare", "client"]


def test_default_off_never_downgrades_current_restricted_activation(tmp_path, monkeypatch):
    from agent_py_agent.agent.plugin_activation_record import PluginActivation
    from agent_py_agent.agent.plugin_environment_plan import plan_plugin_environment
    from agent_py_agent.agent.plugin_permissions.grants import (
        permission_details,
        select_permissions,
    )
    from agent_py_agent.agent.plugin_permissions.state import canonical_permission_json
    from agent_py_agent.tests.test_plugin_activation import publication

    service = installed_manager(tmp_path, legacy_sandbox_default=False)
    entry = service.installations.snapshot()[0]
    plan = plan_plugin_environment(entry, "fixture-restricted")
    grant = permission_details(entry, plan, select_permissions({}), "restricted")
    prepared = service.installations.change_activation(PluginActivationRequest(
        plan.operation_id, entry.revision, PluginActivation(plan, "preparing", permission_json=canonical_permission_json(grant))))
    service.installations.change_activation(publication(prepared))
    fake_sandbox_ready(monkeypatch)
    response = preview(service)
    assert response["details"]["confirmation"]["mode"] == "restricted"
    fake_settled_deactivation(monkeypatch)
    calls = fake_processes(monkeypatch)
    result = confirm(service, response)
    assert result["state"] == "succeeded", result
    assert calls == ["prepare", "client"]
    current = service.installations.snapshot()[0]
    assert current.activation is not None and legacy_permissions(current)["mode"] == "restricted"


def test_complete_preview_has_no_twenty_path_truncation(tmp_path, monkeypatch):
    service = installed_manager(tmp_path)
    fake_sandbox_ready(monkeypatch)
    roots = [(tmp_path / f"project-{i}").resolve() for i in range(25)]
    for root in roots:
        root.mkdir()
    response = preview(service, " ".join(f"--read-root {shlex.quote(str(root))}" for root in roots))
    assert all(str(root) in response["message"] for root in roots)
    assert len(response["details"]["confirmation"]["permissions"]["read_roots"]) == 25
    queried = service.command("/plugins status preview", revision="", request_id="query")
    assert queried["details"] == response["details"]


@pytest.mark.parametrize("flags", ["--read-root /", "--program-root /", "--authorization bad --confirm bad"])
def test_invalid_or_unbound_authorization_does_not_activate(tmp_path, flags):
    service = installed_manager(tmp_path)
    result = service.command(f"/plugins enable sample-peek {flags}", revision=service.catalog().revision,
                             request_id="invalid")
    assert result["state"] != "succeeded"
    assert service.installations.snapshot()[0].activation is None


@pytest.mark.parametrize("setting,mode", [("true", "restricted"), ("false", "wide"), ('"false"', "wide")])
def test_loaded_legacy_default_reaches_real_management_executor(tmp_path, monkeypatch, setting, mode):
    """从 YAML 正式加载到实际管理/HostCommand；仍不将替身启动当 OS 验收。"""
    from agent_py_agent.agent.plugin_management import plugin_management_context
    from agent_py_agent.agent.settings.config import load_config
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_resolver import home_paths_with_owner

    original = installed_manager(tmp_path)
    path = tmp_path / "configured.yaml"
    path.write_text("plugin_legacy_sandbox_default: " + setting + "\n")
    owner = original.context.owner
    context = plugin_management_context(owner, home_paths_with_owner(home_paths(tmp_path / "home"), owner),
        load_config(path), original.context.threads, actor_id="tester", channel="chat", conversation_id="session", is_admin=True)
    service = PluginManagement(context)
    calls = fake_processes(monkeypatch)
    fake_sandbox_ready(monkeypatch)
    response = preview(service)
    assert response["details"]["confirmation"]["mode"] == mode
    result = confirm(service, response)
    assert result["state"] == "succeeded", result
    assert legacy_permissions(service.installations.snapshot()[0])["mode"] == mode
    assert calls == ["prepare", "client"]
