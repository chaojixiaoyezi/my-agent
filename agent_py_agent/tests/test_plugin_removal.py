"""实际管理/操作账的卸载开发验证；临时原生组件不代替产品 TUI 验收。"""

import json
from contextlib import contextmanager
from dataclasses import replace

import pytest

from agent_py_agent.agent import plugin_install_store
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_management import PluginManagement
from agent_py_agent.agent.runtime_db.managed_operation_store import ManagedOperationStore
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import runtime_db_path
from agent_py_agent.agent.tooling.process_session_store import ProcessSessionStore
from agent_py_agent.tests.plugin_deactivation_fixtures import activation_component
from agent_py_agent.tests.test_plugin_deactivation import installed_manager


def test_remove_reinstall_and_replay_never_select_new_installation(tmp_path):
    service = installed_manager(tmp_path)
    old = service.installations.snapshot()[0]
    revision = service.catalog().revision
    artifact = tmp_path / "user-result.txt"
    artifact.write_text("keep")
    first = service.command("/plugins remove sample-peek", revision=revision, request_id="remove")
    assert first["state"] == "succeeded" and first["details"]["removed"], first
    assert first["cleanup_consumption"] == {"state": "consumed", "count": 0, "package": "removed"}
    assert not service.installations.snapshot() and not service.catalog().plugins
    assert artifact.read_text() == "keep"
    installed = service.command(f'/plugins install "{tmp_path / "source.zip"}"',
                                revision=service.catalog().revision, request_id="reinstall")
    assert installed["state"] == "succeeded", installed
    new = service.installations.snapshot()[0]
    assert old.revision == new.revision and old.installation_ref != new.installation_ref
    assert service.catalog().revision != revision
    stale = service.command("/plugins remove sample-peek", revision=revision, request_id="stale")
    assert stale["error_code"] == "PLUGIN_CATALOG_STALE"
    replay = service.command("/plugins remove sample-peek", revision=revision, request_id="remove")
    assert replay["details"] == first["details"] and replay["cleanup_consumption"]["package"] == "retained_in_use"
    assert service.installations.snapshot() == (new,) and service.installations.package_bytes(new)
    query = service.command("/plugins status remove", revision="", request_id="query")
    assert query["details"] == first["details"] and "cleanup_consumption" not in query


@pytest.mark.parametrize("permission", ["is_admin", "remove_allowed", "enabled"])
def test_remove_obeys_admin_feature_and_tool_policy(tmp_path, permission):
    service = installed_manager(tmp_path)
    before = service.installations.snapshot()
    denied = PluginManagement(replace(service.context, **{permission: False}))
    result = denied.command("/plugins remove sample-peek", revision=denied.catalog().revision, request_id="denied")
    assert result["state"] == "rejected", result
    assert service.installations.snapshot() == before
    assert service.installations.package_bytes(before[0])
    assert not next(row for row in denied.catalog().management_actions if row.name == "remove").available


def test_missing_remove_is_confirmed_without_selecting_any_package(tmp_path, monkeypatch):
    service = installed_manager(tmp_path)
    original = service.installations.snapshot()
    monkeypatch.setattr(PluginInstallStore, "consume_removed_package", lambda *_: pytest.fail("缺失插件没有包可删"))
    result = service.command("/plugins remove missing", revision=service.catalog().revision, request_id="missing")
    assert result["state"] == "succeeded" and result["details"]["outcome"] == "absent", result
    assert result["details"]["receipt"] is None and service.installations.snapshot() == original


def test_live_preparation_blocks_uninstall_and_preserves_environment_and_package(tmp_path):
    from agent_py_agent.tests.test_plugin_release import environment_fixture

    service = installed_manager(tmp_path)
    with activation_component(service, tmp_path, running=True, preparation=False) as state:
        environment, outside = environment_fixture(service, state["entry"], tmp_path)
        revision = service.catalog().revision
        first = service.command("/plugins remove sample-peek", revision=revision, request_id="remove")
        assert first["state"] == "outcome_unknown" and "details" not in first, first
        assert service.installations.snapshot()[0].activation.phase == "revoked"
        assert environment.is_dir() and outside.exists() and service.installations.package_bytes(state["entry"])
        state["release"].set()
    later = service.command("/plugins remove sample-peek", revision=service.catalog().revision, request_id="later")
    assert later["state"] == "succeeded" and later["details"]["removed"], later
    replay = service.command("/plugins remove sample-peek", revision=revision, request_id="remove")
    assert replay["state"] == "outcome_unknown" and replay["operation_id"] == first["operation_id"]


def test_active_uninstall_releases_exact_native_resources_before_removal(tmp_path):
    from agent_py_agent.tests.test_plugin_release import environment_fixture

    service = installed_manager(tmp_path)
    with activation_component(service, tmp_path) as state:
        environment, outside = environment_fixture(service, state["entry"], tmp_path)
        store = ProcessSessionStore(state["prepared_process"].store_root)
        result = service.command("/plugins remove sample-peek", revision=service.catalog().revision, request_id="remove")
        assert result["state"] == "succeeded" and result["details"]["removed"], result
        assert not service.installations.snapshot() and not environment.exists() and outside.exists()
        assert not store.list_records()[0]
        assert result["cleanup_consumption"] == {"state": "consumed", "count": 2, "package": "removed"}


def test_package_cleanup_failure_can_only_retry_from_original_persisted_success(tmp_path, monkeypatch):
    service = installed_manager(tmp_path)
    old = service.installations.snapshot()[0]
    original = plugin_install_store.unlink_file_beneath
    monkeypatch.setattr(plugin_install_store, "unlink_file_beneath",
                        lambda *_: (_ for _ in ()).throw(OSError("fixture cleanup failure")))
    revision = service.catalog().revision
    first = service.command("/plugins remove sample-peek", revision=revision, request_id="remove")
    assert first["state"] == "succeeded" and first["cleanup_consumption"]["state"] == "pending", first
    assert not service.installations.snapshot() and service.installations.package_bytes(old)
    monkeypatch.setattr(plugin_install_store, "unlink_file_beneath", original)
    monkeypatch.setattr(service, "_prepare", lambda *_: pytest.fail("已完成请求不能重跑 handler"))
    query = service.command("/plugins status remove", revision="", request_id="query")
    assert query["details"] == first["details"] and service.installations.package_bytes(old)
    disabled = PluginManagement(replace(service.context, enabled=False))
    assert disabled.command("/plugins remove sample-peek", revision=revision, request_id="remove")["state"] == "succeeded"
    assert service.installations.package_bytes(old)
    replay = service.command("/plugins remove sample-peek", revision=revision, request_id="remove")
    assert replay["details"] == first["details"] and replay["cleanup_consumption"]["package"] == "removed"


def test_original_result_commit_failure_never_authorizes_package_cleanup(tmp_path, monkeypatch):
    service = installed_manager(tmp_path)
    old = service.installations.snapshot()[0]
    original = ManagedOperationStore.finish_tool_operation
    def fail_success(instance, request):
        if request.status == "succeeded":
            raise OSError("fixture operation commit failure")
        return original(instance, request)
    monkeypatch.setattr(ManagedOperationStore, "finish_tool_operation", fail_success)
    revision = service.catalog().revision
    result = service.command("/plugins remove sample-peek", revision=revision, request_id="remove")
    assert result["state"] == "outcome_unknown", result
    assert not service.installations.snapshot() and service.installations.package_bytes(old)
    assert "cleanup_consumption" not in result
    replay = service.command("/plugins remove sample-peek", revision=revision, request_id="remove")
    assert replay["state"] == "outcome_unknown" and service.installations.package_bytes(old)


def test_corrupt_original_result_cannot_authorize_blob_deletion(tmp_path, monkeypatch):
    service = installed_manager(tmp_path)
    old = service.installations.snapshot()[0]
    original = plugin_install_store.unlink_file_beneath
    monkeypatch.setattr(plugin_install_store, "unlink_file_beneath",
                        lambda *_: (_ for _ in ()).throw(OSError("keep fixture package")))
    revision = service.catalog().revision
    first = service.command("/plugins remove sample-peek", revision=revision, request_id="remove")
    assert first["state"] == "succeeded"
    repo = RuntimeRepository(runtime_db_path(service.context.owner.home_dir))
    with repo.transaction() as conn:
        row = conn.execute("SELECT outcome_json FROM tool_operations WHERE operation_id=?", (first["operation_id"],)).fetchone()
        payload = json.loads(row[0])
        payload["result"]["result_envelope"] = "invalid"
        conn.execute("UPDATE tool_operations SET outcome_json=? WHERE operation_id=?",
                     (json.dumps(payload), first["operation_id"]))
    monkeypatch.setattr(plugin_install_store, "unlink_file_beneath", original)
    replay = service.command("/plugins remove sample-peek", revision=revision, request_id="remove")
    assert replay["state"] == "outcome_unknown" and service.installations.package_bytes(old)


@pytest.mark.parametrize("failure", ["replace", "lock"])
def test_confirmed_delete_survives_storage_warning_and_original_request_replay(tmp_path, monkeypatch, failure):
    service = installed_manager(tmp_path)
    old = service.installations.snapshot()[0]
    revision = service.catalog().revision
    original_write = plugin_install_store.write_text_atomic_beneath
    original_lock = plugin_install_store.locked_private_directory
    def fail_after_replace(*args):
        original_write(*args)
        raise OSError("fixture replace completed")
    @contextmanager
    def fail_after_remove_unlock(*args, **kwargs):
        with original_lock(*args, **kwargs):
            yield
        if not service.installations.snapshot():
            raise OSError("fixture unlock failure")
    if failure == "replace":
        monkeypatch.setattr(plugin_install_store, "write_text_atomic_beneath", fail_after_replace)
    else:
        monkeypatch.setattr(plugin_install_store, "locked_private_directory", fail_after_remove_unlock)
    first = service.command("/plugins remove sample-peek", revision=revision, request_id="remove")
    assert first["state"] == "succeeded" and first["details"]["removed"], first
    assert first["details"]["commit_state"] == "committed" and first["details"]["storage_warning"]
    assert first["details"]["receipt"]["package_sha256"] == old.package_sha256
    assert not service.installations.snapshot()
    monkeypatch.setattr(plugin_install_store, "write_text_atomic_beneath", original_write)
    monkeypatch.setattr(plugin_install_store, "locked_private_directory", original_lock)
    replay = service.command("/plugins remove sample-peek", revision=revision, request_id="remove")
    assert replay["state"] == "succeeded" and replay["details"] == first["details"]
    assert replay["cleanup_consumption"]["package"] == "removed"


def test_reinstall_between_deactivation_and_removal_is_preserved(tmp_path, monkeypatch):
    from agent_py_agent.agent.plugin_installation import PluginInstallRequest
    from agent_py_agent.agent.plugin_package import inspect_plugin_package

    service = installed_manager(tmp_path)
    old = service.installations.snapshot()[0]
    package = inspect_plugin_package(service.installations.package_bytes(old))
    remove = PluginInstallStore.remove
    def interleave(instance, operation_id, plugin_id, expected):
        remove(instance, "concurrent-remove", plugin_id, expected)
        instance.install(PluginInstallRequest(package, "concurrent-install", 0))
        return remove(instance, operation_id, plugin_id, expected)
    monkeypatch.setattr(PluginInstallStore, "remove", interleave)
    result = service.command("/plugins remove sample-peek", revision=service.catalog().revision, request_id="remove")
    assert result["state"] == "failed" and result["details"]["reason"] == "revision_conflict", result
    new = service.installations.snapshot()[0]
    assert new.installation_ref != old.installation_ref and service.installations.package_bytes(new)
    assert "cleanup_consumption" not in result


def test_actual_environment_uninstall_revokes_old_snapshot_and_preserves_core_tools(tmp_path):
    from agent_py_agent.agent.plugin_runtime import plugin_tool_name
    from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call
    from agent_py_agent.tests.plugin_activation_fixtures import (
        installed_runtime_plugin,
        invoke_registered_tool,
        plugin_registry,
    )

    service = installed_runtime_plugin(tmp_path)
    registry = plugin_registry(service)
    source = tmp_path / "input.txt"
    source.write_text("卸载前后保留的用户文件")
    name = plugin_tool_name("sample-peek", "read")
    try:
        enabled = service.command("/plugins enable sample-peek", revision=service.catalog().revision, request_id="enable")
        assert enabled["state"] == "succeeded", enabled
        entry = service.installations.snapshot()[0]
        registry.prepare_for_run()
        before = invoke_registered_tool(service, registry, name, {"path": str(source)})
        assert before["state"] == "succeeded", before
        old = registry.runtime_snapshot(run_id="old")
        removed = service.command("/plugins remove sample-peek", revision=service.catalog().revision, request_id="remove")
        assert removed["state"] == "succeeded" and removed["details"]["removed"], removed
        assert removed["cleanup_consumption"]["state"] == "consumed"
        assert not service.installations.snapshot()
        assert not (service.context.owner.plugins_dir / "environments" / entry.activation.plan.environment_ref).exists()
        assert not list((service.context.owner.plugins_dir / "packages").glob("*.zip"))
        rejected = invoke_registered_tool(service, registry, name, {"path": str(source)}, request_id="old", snapshot=old)
        assert rejected["state"] == "failed", rejected
        registry.prepare_for_run()
        assert name not in registry.tools and "read_file" in registry.tools
        core = execute_registry_test_call(registry, "read_file", {"path": str(source)})
        assert core.ok, core
        assert "卸载前后保留的用户文件" in core.output
    finally:
        registry.close_mcp_clients()
