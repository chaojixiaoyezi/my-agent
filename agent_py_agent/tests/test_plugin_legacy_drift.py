"""真实管理与执行器间的授权漂移；外部进程替身不代表 OS 隔离或真实通道验收。"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.plugin_activation import PluginActivationRequest, prepare_activation
from agent_py_agent.agent.plugin_enable_tool import PluginEnableTool
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_management import PluginManagement
from agent_py_agent.agent.plugin_permissions.enable import (
    LegacyEnableContext,
    authorization_identity,
)
from agent_py_agent.agent.runtime_db.host_commands import HostCommandIdentity
from agent_py_agent.tests.test_plugin_legacy_management import (
    confirm,
    fake_processes,
    fake_sandbox_ready,
    fake_settled_deactivation,
    installed_manager,
    preview,
)


def permission_target(tmp_path, kind):
    path = (tmp_path / "授权 中文 空格").resolve()
    if kind.startswith("program"):
        path.write_bytes(b"extra program v1, never executed")
        return path, f'--program-root "{path}"'
    path.mkdir()
    option = "--write-root" if kind == "write_inode" else "--read-root"
    return path, f'{option} "{path}"'


def change_target(path, kind):
    if kind == "program_content":
        path.write_bytes(b"extra program v2, never executed")
        return
    saved = path.with_name("original retained")
    path.rename(saved)
    if kind == "symlink":
        path.symlink_to(saved, target_is_directory=True)
        return
    if kind == "missing":
        return
    if kind.startswith("program"):
        path.write_bytes(saved.read_bytes())
        return
    path.mkdir()


@pytest.mark.parametrize("kind", ["read_inode", "write_inode", "program_inode", "program_content"])
@pytest.mark.parametrize("timing", ["before_constructor", "before_execute"])
def test_changed_root_or_program_requires_new_full_confirmation(tmp_path, monkeypatch, kind, timing):
    service = installed_manager(tmp_path, legacy_sandbox_default=False)
    path, flags = permission_target(tmp_path, kind)
    first = preview(service, flags)
    before = service.installations.snapshot()[0]
    calls = fake_processes(monkeypatch)
    if timing == "before_constructor":
        change_target(path, kind)
    else:
        original = PluginEnableTool.execute

        def execute(tool, params):
            change_target(path, kind)
            return original(tool, params)

        monkeypatch.setattr(PluginEnableTool, "execute", execute)
    result = confirm(service, first)
    assert result.get("error_code") == "PLUGIN_CONFIRMATION_REQUIRED", result
    current = result["details"]["confirmation"]
    previous = first["details"]["confirmation"]
    assert current["confirm_code"] != previous["confirm_code"]
    assert current["authorization_id"] != previous["authorization_id"]
    assert str(path) in result["message"] and current["confirm_command"] in result["message"]
    assert service.installations.snapshot()[0] == before and calls == []


@pytest.mark.parametrize("kind", ["symlink", "missing"])
def test_path_becomes_invalid_after_constructor_never_prepares(tmp_path, monkeypatch, kind):
    service = installed_manager(tmp_path, legacy_sandbox_default=False)
    path, flags = permission_target(tmp_path, kind)
    first = preview(service, flags)
    before = service.installations.snapshot()[0]
    calls = fake_processes(monkeypatch)
    original = PluginEnableTool.execute

    def execute(tool, params):
        change_target(path, kind)
        return original(tool, params)

    monkeypatch.setattr(PluginEnableTool, "execute", execute)
    result = confirm(service, first)
    assert result["state"] != "succeeded", result
    assert result["details"]["reason"] == "legacy_permission_invalid", result
    assert service.installations.snapshot()[0] == before and calls == []


@pytest.mark.parametrize("kind", ["read_inode", "write_inode", "program_inode", "program_content"])
def test_drift_during_environment_preparation_never_starts_candidate(tmp_path, monkeypatch, kind):
    service = installed_manager(tmp_path, legacy_sandbox_default=False)
    path, flags = permission_target(tmp_path, kind)
    first = preview(service, flags)
    calls = fake_processes(monkeypatch)

    def prepare(*args):
        calls.append("prepare")
        change_target(path, kind)

    monkeypatch.setattr("agent_py_agent.agent.plugin_enable_tool.prepare_plugin_environment", prepare)
    result = confirm(service, first)
    assert result["state"] == "failed", result
    assert result["details"]["reason"] == "legacy_permission_changed", result
    current = service.installations.snapshot()[0]
    assert not current.enabled and current.activation.phase == "preparing"
    assert calls == ["prepare"]
    replay = confirm(service, first, "resend")
    assert replay["details"] == result["details"] and calls == ["prepare"]


def test_drift_before_reenable_preserves_old_active_generation(tmp_path, monkeypatch):
    service = installed_manager(tmp_path, legacy_sandbox_default=False)
    calls = fake_processes(monkeypatch)
    assert confirm(service, preview(service))["state"] == "succeeded"
    old = service.installations.snapshot()[0]
    retired = fake_settled_deactivation(monkeypatch)
    path, flags = permission_target(tmp_path, "read_inode")
    first = preview(service, flags, "reenable")
    original = PluginEnableTool.execute

    def execute(tool, params):
        change_target(path, "read_inode")
        return original(tool, params)

    monkeypatch.setattr(PluginEnableTool, "execute", execute)
    result = confirm(service, first)
    assert result.get("error_code") == "PLUGIN_CONFIRMATION_REQUIRED", result
    assert service.installations.snapshot()[0] == old
    assert retired == [] and calls == ["prepare", "client"]


@pytest.mark.parametrize("dimension", ["read", "write", "program", "network"])
def test_each_changed_permission_dimension_invalidates_old_code(tmp_path, monkeypatch, dimension):
    service = installed_manager(tmp_path, legacy_sandbox_default=False)
    path, flags = permission_target(tmp_path, "program_inode" if dimension == "program" else "read_inode")
    option = {"read": "--read-root", "write": "--write-root", "program": "--program-root"}
    flags = "--network" if dimension == "network" else f'{option[dimension]} "{path}"'
    first = preview(service)
    before = service.installations.snapshot()[0]
    calls = fake_processes(monkeypatch)
    changed = service.command(first["details"]["confirmation"]["confirm_command"] + " " + flags,
                              revision=service.catalog().revision, request_id="changed")
    assert changed["error_code"] == "PLUGIN_CONFIRMATION_REQUIRED", changed
    assert changed["details"]["confirmation"]["confirm_code"] != first["details"]["confirmation"]["confirm_code"]
    assert changed["details"]["confirmation"]["authorization_id"] != first["details"]["confirmation"]["authorization_id"]
    assert calls == [] and service.installations.snapshot()[0] == before


@pytest.mark.parametrize("case", [("actor_id", "other-actor", "PLUGIN_CONFIRMATION_REQUIRED"),
    ("channel", "other-channel", "PLUGIN_CATALOG_STALE"),
    ("conversation_id", "other-session", "PLUGIN_CATALOG_STALE")])
def test_original_confirmation_never_authorizes_another_host_identity(tmp_path, monkeypatch, case):
    field, value, code = case
    service = installed_manager(tmp_path, legacy_sandbox_default=False)
    first = preview(service)
    before = service.installations.snapshot()[0]
    calls = fake_processes(monkeypatch)
    other = PluginManagement(replace(service.context, **{field: value}))
    result = confirm(other, first)
    # 目录摘要原合同绑定 channel/conversation；跨入口先明确拒旧目录，不把严格拒绝改为自动授权。
    assert result.get("error_code") == code and result["state"] != "succeeded", result
    fresh = preview(other, request="identity-preview")
    assert fresh["details"]["confirmation"]["confirm_code"] != first["details"]["confirmation"]["confirm_code"]
    assert fresh["details"]["confirmation"]["confirm_command"] in fresh["message"]
    assert service.installations.snapshot()[0] == before and calls == []


@pytest.mark.parametrize("case", [("permit-current", "permit-other"), ("not-permit", "not-permit")])
def test_explicit_authorization_must_match_this_request_and_permit_prefix(case):
    request_id, authorization = case
    identity = HostCommandIdentity("fixture-owner", "admin", "chat", "fixture-thread", request_id)
    context = LegacyEnableContext(None, None, SimpleNamespace(request=identity), {"authorization": authorization})
    with pytest.raises(ValueError, match="确认请求未绑定"):
        authorization_identity(context)


def test_nonadmin_authorization_never_rewrites_request_or_creates_owner(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_plugin_management import manager

    service, _ = manager(tmp_path, is_admin=False)
    calls = fake_processes(monkeypatch)
    result = service.command("/plugins enable sample-peek --authorization permit-smuggled --confirm bad --network",
                             revision=service.catalog().revision, request_id="untrusted-transport")
    assert result["state"] == "rejected" and result["error_code"] == "PLUGIN_PERMISSION_DENIED"
    assert result["request_id"] == "untrusted-transport" and "details" not in result
    assert "permit-smuggled" not in result["message"] and str(service.context.owner.root) not in result["message"]
    assert not service.context.owner.home_dir.exists() and calls == []


def test_cleanup_report_presence_cannot_claim_old_authority_was_revoked(tmp_path, monkeypatch):
    service = installed_manager(tmp_path)
    fake_sandbox_ready(monkeypatch)
    calls = fake_processes(monkeypatch)
    assert confirm(service, preview(service))["state"] == "succeeded"
    old = service.installations.snapshot()[0]
    monkeypatch.setattr("agent_py_agent.agent.plugin_enable_tool.retire_for_enable", lambda context, entry: (
        entry, {"cleanup_confirmed": True, "released": True, "authority_revoked": True}))
    result = confirm(service, preview(service, request="reenable"))
    assert result["details"]["reason"] == "revision_conflict", result
    assert result["details"]["commit_state"] == "not_committed", result
    assert service.installations.snapshot()[0] == old and calls == ["prepare", "client"]


def test_actual_old_release_reenables_restricted_through_b7(tmp_path, monkeypatch):
    service = installed_manager(tmp_path, legacy_sandbox_default=False)
    calls = fake_processes(monkeypatch)
    fake_sandbox_ready(monkeypatch)
    assert confirm(service, preview(service))["state"] == "succeeded"
    old = service.installations.snapshot()[0]
    retired = fake_settled_deactivation(monkeypatch)
    service = PluginManagement(replace(service.context, legacy_sandbox_default=True))
    result = confirm(service, preview(service, request="reenable"))
    assert result["state"] == "succeeded", result
    assert retired == [old.activation_id] and calls == ["prepare", "client", "prepare", "client"]
    current = service.installations.snapshot()[0]
    assert current.activation is not None and current.activation_id != old.activation_id


@pytest.mark.parametrize("kind", ["read_inode", "write_inode", "program_inode", "program_content"])
def test_drift_after_preparing_commit_never_calls_environment_preparation(tmp_path, monkeypatch, kind):
    service = installed_manager(tmp_path, legacy_sandbox_default=False)
    path, flags = permission_target(tmp_path, kind)
    first = preview(service, flags)
    calls = fake_processes(monkeypatch)
    original = PluginInstallStore.change_activation
    changed = []

    def preparing_commit(store, request):
        result = original(store, request)
        if request.activation.phase == "preparing":
            change_target(path, kind)
            changed.append(result.installation.activation_id)
        return result

    # 精确注入最后一次准备前守卫之前；不在 prepare 内漂移，避免发布前守卫兜底造成 M1 假绿。
    monkeypatch.setattr(PluginInstallStore, "change_activation", preparing_commit)
    result = confirm(service, first)
    assert result["state"] == "failed" and result["details"]["reason"] == "legacy_permission_changed", result
    current = service.installations.snapshot()[0]
    assert changed == [first["details"]["confirmation"]["activation_id"]]
    assert not current.enabled and current.activation.phase == "preparing"
    assert calls == []


def test_changed_installation_snapshot_before_retirement_preserves_old_active(tmp_path, monkeypatch):
    service = installed_manager(tmp_path, legacy_sandbox_default=False)
    calls = fake_processes(monkeypatch)
    assert confirm(service, preview(service))["state"] == "succeeded"
    old = service.installations.snapshot()[0]
    retired = fake_settled_deactivation(monkeypatch)
    first = preview(service, request="reenable")
    original_snapshot = PluginInstallStore.snapshot
    original_enable = PluginEnableTool._enable
    observed = []
    # 用领域函数生成合法的并发撤销快照（版本、回执、激活摘要一致），不能拼一条会在数据校验处失败的坏记录。
    concurrent = prepare_activation(PluginActivationRequest(
        "fixture-concurrent-revoke", old.revision, replace(old.activation, phase="revoked")), (old,)).installation

    def changed_snapshot(store):
        observed.append(True)
        return (concurrent,)

    def enable(tool):
        # 确认码已核完才在 _enable 边界换快照；不能靠构造前或撤旧后的其它检查杀 M3。
        monkeypatch.setattr(PluginInstallStore, "snapshot", changed_snapshot)
        try:
            return original_enable(tool)
        finally:
            # 只替换本次执行的 CAS 观察点，响应后的目录投影仍读真实表，不让第二次只读观察污染计数。
            monkeypatch.setattr(PluginInstallStore, "snapshot", original_snapshot)

    monkeypatch.setattr(PluginEnableTool, "_enable", enable)
    result = confirm(service, first)
    monkeypatch.setattr(PluginInstallStore, "snapshot", original_snapshot)
    assert result["state"] == "failed" and result["details"]["reason"] == "revision_conflict", result
    assert observed == [True]
    assert service.installations.snapshot()[0] == old and old.enabled and old.activation.phase == "active"
    assert retired == [] and calls == ["prepare", "client"]


def test_retirement_changing_installation_version_never_publishes(tmp_path, monkeypatch):
    """撤旧过程本身换掉安装版本：与预测 target 不符时必须拒绝、零发布（M3 第二道守卫）。"""
    service = installed_manager(tmp_path, legacy_sandbox_default=False)
    calls = fake_processes(monkeypatch)
    assert confirm(service, preview(service))["state"] == "succeeded"
    old = service.installations.snapshot()[0]
    retired = fake_settled_deactivation(monkeypatch)
    first = preview(service, request="reenable")
    from agent_py_agent.agent.plugin_permissions import enable as enable_module

    def drifted_retire(context, entry):
        # 撤旧真实发生，但换了另一个撤旧操作：产生同版本号、不同事实的快照，与确认时预测的 target 不符。
        result = enable_module.deactivate_plugin(context.owner, context.repository, entry, "fixture-retirement-drift")
        return result.installation, None

    monkeypatch.setattr("agent_py_agent.agent.plugin_enable_tool.retire_for_enable", drifted_retire)
    result = confirm(service, first)
    assert result["state"] == "failed" and result["details"]["reason"] == "revision_conflict", result
    current = service.installations.snapshot()[0]
    assert current.revision == old.revision + 2 and current.activation is None and not current.enabled
    assert retired == [old.activation_id]
    assert calls == ["prepare", "client"]


def test_refresh_requires_new_preview_when_facts_changed(tmp_path, monkeypatch):
    """同身份、新事实：刷新后确认码不匹配，必须重新预览、零执行；新预览的码可继续。"""
    service = installed_manager(tmp_path, legacy_sandbox_default=False)
    path, flags = permission_target(tmp_path, "read_inode")
    first = preview(service, flags)
    before = service.installations.snapshot()[0]
    calls = fake_processes(monkeypatch)
    change_target(path, "read_inode")
    stale = confirm(service, first)
    assert stale.get("error_code") == "PLUGIN_CONFIRMATION_REQUIRED", stale
    fresh = stale["details"]["confirmation"]
    assert fresh["confirm_code"] != first["details"]["confirmation"]["confirm_code"]
    assert fresh["authorization_id"] != first["details"]["confirmation"]["authorization_id"]
    assert fresh["confirm_command"] in stale["message"]
    assert service.installations.snapshot()[0] == before and calls == []
    assert confirm(service, stale, "fresh-transport")["state"] == "succeeded"
