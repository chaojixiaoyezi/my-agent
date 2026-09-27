"""纯内容包沿原安装和管理链的组件验证，不运行 Gateway、模型或包内程序。"""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.agent.plugin_content_activation import PluginContentActivation
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_installation import PluginInstallationError, PluginInstallRequest
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.user_space.owner_quota import owner_quota_enforcer_from_policy
from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
from agent_py_agent.tests.test_capability_package import content_bundle
from agent_py_agent.tests.test_plugin_management import manager


# LLM: 所有声明与状态只在临时 owner；此夹具不创建环境或进程，CAS 测试不冒充实际产品验收。
# 函数用途: 生成停用内容安装及其宿主启用请求。
def content_activation_fixture(tmp_path):
    owner = resolve_owner_home(tmp_path / "home")
    store = PluginInstallStore(owner)
    entry = store.install(PluginInstallRequest(inspect_plugin_package(content_bundle()), "install", 0)).installation
    activation = PluginContentActivation("enable", entry.manifest.plugin_id, entry.package_sha256,
                                         entry.revision, entry.settings_revision)
    return owner, store, entry, PluginActivationRequest("enable", entry.revision, activation)


# LLM: 安装/启停全部经过原 HostCommand/ToolExecutor；替身只将本不该发生的进程副作用变为测试失败。
# 函数用途: 创建内容包管理组件，并禁止所有环境、MCP 和清理资源路径。
def content_manager(tmp_path, monkeypatch):
    service, source = manager(tmp_path)
    source.write_bytes(content_bundle())

    def forbidden(*args, **kwargs):
        raise AssertionError("content lifecycle must not create or clean process resources")

    for module, names in (
        ("plugin_enable_tool", ("plan_plugin_environment", "_runtime_facts", "prepare_plugin_environment", "PluginMCPClient")),
        ("plugin_deactivation", ("ProcessSessionStore", "preparation_binding", "cancel_runtime_run")),
        ("plugin_install_store", ("plugin_release_evidence", "remove_tree_beneath")),
        ("plugin_cleanup", ("ProcessSessionStore",)),
    ):
        for name in names:
            monkeypatch.setattr(f"agent_py_agent.agent.{module}.{name}", forbidden)
    installed = service.command(f'/plugins install "{source}"', revision=service.catalog().revision, request_id="install")
    assert installed["state"] == "succeeded", installed
    return service, source


def test_management_content_lifecycle_never_creates_mcp_environment_or_process_cleanup(tmp_path, monkeypatch):
    service, source = content_manager(tmp_path, monkeypatch)
    revision = service.catalog().revision
    enabled = service.command("/plugins enable story-content", revision=revision, request_id="enable")
    assert enabled["state"] == "succeeded", enabled
    assert enabled["details"]["runtime"] == "none"
    entry = service.installations.snapshot()[0]
    assert entry.enabled and isinstance(entry.activation, PluginContentActivation)
    assert entry.revision == 2 and not hasattr(entry.activation, "plan")
    replay = service.command("/plugins enable story-content", revision=revision, request_id="enable")
    assert replay["details"] == enabled["details"]
    again = service.command("/plugins enable story-content", revision=service.catalog().revision, request_id="again")
    assert again["state"] == "succeeded" and again["details"]["outcome"] == "unchanged", again
    disabled = service.command("/plugins disable story-content", revision=service.catalog().revision, request_id="disable")
    assert disabled["state"] == "succeeded", disabled
    assert disabled["details"]["cleanup_required"] is False
    assert disabled["details"]["release"]["kind"] == "content"
    assert service.installations.snapshot()[0].activation is None
    with pytest.raises(PluginInstallationError):
        service.installations.require_activation(entry.manifest.plugin_id, entry.activation_id)
    reenabled = service.command("/plugins enable story-content", revision=service.catalog().revision, request_id="reenable")
    assert reenabled["state"] == "succeeded", reenabled
    assert reenabled["details"]["activation_id"] != entry.activation_id
    artifact = tmp_path / "user-output.txt"
    artifact.write_text("keep")
    removed = service.command("/plugins remove story-content", revision=service.catalog().revision, request_id="remove")
    assert removed["state"] == "succeeded", removed
    assert removed["cleanup_consumption"] == {"state": "consumed", "count": 0, "package": "removed"}
    assert service.installations.snapshot() == () and artifact.read_text() == "keep" and source.exists()
    assert not (service.context.owner.plugins_dir / "environments").exists()


def test_content_activation_round_trip_replay_and_release_use_original_store(tmp_path):
    owner, store, original, request = content_activation_fixture(tmp_path)
    result = store.change_activation(request)
    assert result.installation.revision == original.revision + 1
    path = store.root / "installations.json"
    before = path.read_bytes(), path.stat().st_mtime_ns
    assert store.change_activation(request).outcome == "replayed"
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert store.snapshot()[0] == result.installation
    assert str(tmp_path) not in json.dumps(json.loads(path.read_text()))
    revoked = store.change_activation(PluginActivationRequest("disable", result.installation.revision,
                                     replace(request.activation, phase="revoked"))).installation
    assert revoked.activation_id == result.installation.activation_id and not revoked.enabled
    released, evidence = store.release_activation(owner, None, revoked, "disable")
    assert released.installation.activation is None
    assert evidence == {"kind": "content", "activation_id": revoked.activation_id, "package_sha256": revoked.package_sha256}


@pytest.mark.parametrize("change,reason", [
    ({"plugin_id": "missing"}, "plugin_missing"),
    ({"package_sha256": "b" * 64}, "activation_binding_conflict"),
    ({"installation_revision": 2}, "activation_binding_conflict"),
    ({"settings_revision": 1}, "activation_binding_conflict"),
])
def test_content_activation_refuses_mismatched_frozen_identity(tmp_path, change, reason):
    _owner, store, original, request = content_activation_fixture(tmp_path)
    with pytest.raises(PluginInstallationError) as error:
        store.change_activation(replace(request, activation=replace(request.activation, **change)))
    assert error.value.reason == reason
    assert store.snapshot() == (original,)


def test_two_content_enables_share_cas_and_old_reference_cannot_follow_reinstallation(tmp_path):
    owner, store, original, request = content_activation_fixture(tmp_path)
    barrier = Barrier(2)

    def submit(operation):
        barrier.wait(timeout=5)
        try:
            return store.change_activation(replace(request, operation_id=operation,
                activation=replace(request.activation, operation_id=operation))).outcome
        except PluginInstallationError as error:
            return error.reason

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, ("enable-a", "enable-b")))
    assert sorted(results) == ["activate", "revision_conflict"]
    active = store.snapshot()[0]
    revoked = store.change_activation(PluginActivationRequest("disable", active.revision,
                                     replace(active.activation, phase="revoked"))).installation
    released, _ = store.release_activation(owner, None, revoked, "disable")
    store.remove("remove", original.manifest.plugin_id, released.installation)
    fresh = store.install(PluginInstallRequest(inspect_plugin_package(content_bundle()), "new-install", 0)).installation
    fresh_activation = PluginContentActivation("new-enable", fresh.manifest.plugin_id, fresh.package_sha256,
                                               fresh.revision, fresh.settings_revision)
    store.change_activation(PluginActivationRequest("new-enable", fresh.revision, fresh_activation))
    assert fresh_activation.activation_id != active.activation_id
    with pytest.raises(PluginInstallationError):
        store.require_activation(active.manifest.plugin_id, active.activation_id)


def test_content_update_and_explicit_old_package_rollback(tmp_path, monkeypatch):
    service, source = content_manager(tmp_path, monkeypatch)
    newer = tmp_path / "newer.zip"
    newer.write_bytes(content_bundle(version="2.0"))
    updated = service.command(f'/plugins update story-content "{newer}"', revision=service.catalog().revision, request_id="update")
    assert updated["state"] == "succeeded", updated
    assert service.installations.snapshot()[0].manifest.version == "2.0"
    rolled_back = service.command(f'/plugins update story-content "{source}"', revision=service.catalog().revision,
                                  request_id="rollback")
    assert rolled_back["state"] == "succeeded", rolled_back
    assert service.installations.snapshot()[0].manifest.version == "1.0"


def test_content_revoke_and_release_do_not_wait_for_quota(tmp_path):
    owner, store, _original, request = content_activation_fixture(tmp_path)
    active = store.change_activation(request).installation
    quota = owner_quota_enforcer_from_policy(owner.home_dir, quota_path=owner.quota_json)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with quota.admission():
            revoked = pool.submit(store.change_activation, PluginActivationRequest("disable", active.revision,
                replace(active.activation, phase="revoked"))).result(timeout=2).installation
            released, _ = pool.submit(store.release_activation, owner, None, revoked, "disable").result(timeout=2)
    assert released.installation.activation is None


@pytest.mark.parametrize("phase,commit_state", [("before", "not_committed"), ("after", "committed"), ("unknown", "unknown")])
def test_content_commit_fault_preserves_actual_publication_state(tmp_path, monkeypatch, phase, commit_state):
    from agent_py_agent.agent import plugin_install_store

    _owner, store, original, request = content_activation_fixture(tmp_path)
    real_write = plugin_install_store.write_text_atomic_beneath

    def failed_write(anchor, parts, content):
        if phase != "before":
            real_write(anchor, parts, content if phase == "after" else "unreadable")
        raise OSError("fault")

    monkeypatch.setattr(plugin_install_store, "write_text_atomic_beneath", failed_write)
    with pytest.raises(PluginInstallationError) as error:
        store.change_activation(request)
    assert error.value.commit_state == commit_state
    assert error.value.receipt.action == "activate"
    if phase == "before":
        assert store.snapshot() == (original,)
    elif phase == "after":
        assert store.snapshot()[0].enabled
    else:
        with pytest.raises(PluginInstallationError):
            store.snapshot()


def test_failed_content_release_keeps_revoked_authority_and_allows_explicit_cleanup(tmp_path, monkeypatch):
    service, _ = content_manager(tmp_path, monkeypatch)
    assert service.command("/plugins enable story-content", revision=service.catalog().revision, request_id="enable")["ok"]
    old = service.installations.snapshot()[0]

    def failed_release(*args, **kwargs):
        raise PluginInstallationError("storage_unavailable", "unavailable")

    with monkeypatch.context() as change:
        change.setattr(PluginInstallStore, "release_activation", failed_release)
        result = service.command("/plugins disable story-content", revision=service.catalog().revision, request_id="disable")
    assert result["state"] == "outcome_unknown"
    assert service.installations.snapshot()[0].activation.phase == "revoked"
    with pytest.raises(PluginInstallationError):
        service.installations.require_activation(old.manifest.plugin_id, old.activation_id)
    cleaned = service.command("/plugins disable story-content", revision=service.catalog().revision, request_id="cleanup")
    assert cleaned["state"] == "succeeded", cleaned
    assert service.installations.snapshot()[0].activation is None
    assert service.command("/plugins status disable", revision="", request_id="query")["state"] == "outcome_unknown"


def test_content_enable_checks_actual_blob_and_required_configuration(tmp_path, monkeypatch):
    service, source = manager(tmp_path)
    source.write_bytes(content_bundle(change=lambda p: p.update(settings_schema={"type": "object", "properties": {
        "style": {"type": "string"}}, "required": ["style"], "additionalProperties": False})))
    assert service.command(f'/plugins install "{source}"', revision=service.catalog().revision, request_id="install")["ok"]
    refused = service.command("/plugins enable story-content", revision=service.catalog().revision, request_id="missing-setting")
    assert refused["details"]["reason"] == "invalid_settings"
    assert service.installations.snapshot()[0].activation is None
    settings = tmp_path / "settings.json"
    settings.write_text('{"style":"private preference"}')
    configured = service.command(f'/plugins configure story-content --file "{settings}"', revision=service.catalog().revision,
                                 request_id="configure")
    assert configured["state"] == "succeeded", configured
    entry = service.installations.snapshot()[0]
    blob = service.context.owner.plugins_dir / "packages" / f"{entry.package_sha256}.zip"
    blob.write_bytes(b"damaged")
    damaged = service.command("/plugins enable story-content", revision=service.catalog().revision, request_id="damaged")
    assert damaged["details"]["reason"] == "package_integrity"
    assert service.installations.snapshot()[0].activation is None
    assert "private preference" not in json.dumps(damaged)


def test_content_activation_rejects_cross_owner_release_and_cross_kind_record(tmp_path):
    from agent_py_agent.agent.plugin_activation_record import PluginActivation
    from agent_py_agent.agent.plugin_environment_plan import PluginEnvironmentPlan

    owner, store, original, request = content_activation_fixture(tmp_path)
    process = PluginActivation(PluginEnvironmentPlan("process", original.manifest.plugin_id, original.package_sha256,
                                                    original.revision, 0, "a" * 64), "preparing")
    with pytest.raises(PluginInstallationError):
        store.change_activation(PluginActivationRequest("process", original.revision, process))
    active = store.change_activation(request).installation
    revoked = store.change_activation(PluginActivationRequest("disable", active.revision,
                                     replace(active.activation, phase="revoked"))).installation
    other = resolve_owner_home(tmp_path / "other")
    with pytest.raises(PluginInstallationError) as error:
        store.release_activation(other, None, revoked, "disable")
    assert error.value.reason == "owner_conflict"
    assert store.snapshot() == (revoked,)
    assert owner.home_dir != other.home_dir
