"""真实原管理/资源组件测试；假启用夹具不代替产品 enable、真实 TUI 或模型验收。"""

import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.plugin_deactivation import deactivate_plugin
from agent_py_agent.agent.plugin_installation import PluginInstallationError
from agent_py_agent.agent.plugin_management import PluginManagement
from agent_py_agent.agent.runtime_db.managed_operation_store import AuthorityContextMissing
from agent_py_agent.agent.runtime_db.operations import RuntimeConflictError
from agent_py_agent.agent.tooling import process_session_store
from agent_py_agent.agent.tooling.mcp_client import MCPError
from agent_py_agent.agent.tooling.process_registry import _process_instance_terminated
from agent_py_agent.agent.tooling.process_session_cleanup import ProcessSessionCleanup
from agent_py_agent.tests.plugin_deactivation_fixtures import activation_component
from agent_py_agent.tests.test_plugin_management import manager


# LLM: 安装使用产品管理入口；包内容仅作静态夹具，后续进程由独立的假启用工具构造。
# 函数用途: 提供同一 owner、原线程和已安装包，便于测试真实停用命令。
def installed_manager(tmp_path, **changes):
    service, source = manager(tmp_path, **changes)
    assert service.command(f'/plugins install "{source}"', revision=service.catalog().revision,
                           request_id="install")["state"] == "succeeded"
    return service


@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("phase", ["preparing", "active"])
def test_management_disable_revokes_and_cleans_both_exact_resource_kinds(tmp_path, running, phase):
    service = installed_manager(tmp_path)
    with activation_component(service, tmp_path, running=running, phase=phase) as state:
        client = state["client"]
        if phase == "active":
            assert client.call_tool("echo", {"text": "before"})["content"] == "before"
        else:
            assert client.list_tools()
        revision = service.catalog().revision
        result = service.command("/plugins disable sample-peek", revision=revision, request_id="disable")
        assert result["state"] == "succeeded", result
        details = result["details"]
        assert details["authority_revoked"] and details["cleanup_confirmed"]
        assert {row["kind"] for row in details["sessions"]} == {"preparation", "activation"}
        assert len(details["sessions"]) == 2 and all(row["confirmed"] for row in details["sessions"])
        activation = service.installations.snapshot()[0].activation
        assert details["released"] is not running
        assert activation.phase == "revoked" if running else activation is None
        assert not service.catalog().plugins[0].enabled
        with pytest.raises(MCPError):
            client.call_tool("echo", {"text": "after"})
        with pytest.raises((RuntimeConflictError, AuthorityContextMissing)):
            state["operation"].authorize()
        query = service.command("/plugins status disable", revision="", request_id="query")
        assert query["details"] == details
        replay = service.command("/plugins disable sample-peek", revision=revision, request_id="disable")
        assert replay["details"] == details
        binding = state["binding"]
        assert state["repo"].get_attempt(binding.attempt_id)["status"] == ("cancelled" if running else "done")
        hosted = state["prepared_process"]
        records, errors = process_session_store.ProcessSessionStore(hosted.store_root).list_records()
        assert not errors and len(records) == (2 if running else 0)
        if not running:
            assert result["cleanup_consumption"] == {"state": "consumed", "count": 2}
            assert len(details["release"]["resources"]) == 2
        for row in records:
            assert row["stop_requested"]
            assert all(_process_instance_terminated(row[key], row[birth]) for key, birth in (
                ("pid", "pid_birth_token"), ("child_pid", "child_pid_birth_token")))


@pytest.mark.parametrize("permission", ["is_admin", "disable_allowed", "enabled"])
def test_disable_denial_and_stale_catalog_preserve_current_activation(tmp_path, permission):
    service = installed_manager(tmp_path)
    with activation_component(service, tmp_path, preparation=False) as state:
        denied = PluginManagement(replace(service.context, **{permission: False}))
        result = denied.command("/plugins disable sample-peek", revision=denied.catalog().revision, request_id="denied")
        assert result["state"] == "rejected"
        assert service.installations.snapshot()[0].enabled
        assert state["client"].call_tool("echo", {"text": "alive"})["content"] == "alive"
        stale = service.command("/plugins disable sample-peek", revision="old", request_id="stale")
        assert stale["error_code"] == "PLUGIN_CATALOG_STALE"


def test_disable_without_activation_checks_original_revision_and_is_idempotent(tmp_path):
    service = installed_manager(tmp_path)
    inactive = service.installations.snapshot()[0]
    first = service.command("/plugins disable sample-peek", revision=service.catalog().revision, request_id="one")
    assert first["state"] == "succeeded" and first["details"]["sessions"] == []
    assert service.installations.snapshot()[0] == inactive
    with activation_component(service, tmp_path, preparation=False) as state:
        with pytest.raises(PluginInstallationError, match="变化"):
            deactivate_plugin(service.context.owner, state["repo"], inactive, "stale-op")
        assert service.installations.snapshot()[0].enabled


@pytest.mark.parametrize("damage", ["owner", "operation", "chain", "scopes"])
def test_bad_original_preparation_binding_does_not_stop_unrelated_attempt(tmp_path, damage):
    service = installed_manager(tmp_path)
    with activation_component(service, tmp_path, preparation=False) as state:
        repo, binding = state["repo"], state["binding"]
        if damage == "owner":
            with pytest.raises(RuntimeConflictError):
                repo.find_host_command_by_operation(owner_id="other", operation_id=binding.request.operation_id)
            return
        with repo.transaction() as conn:
            if damage in {"operation", "chain"}:
                raw = conn.execute("SELECT metadata_json FROM task_runs WHERE task_run_id=?", (binding.task_run_id,)).fetchone()[0]
                value = json.loads(raw)
                value["host_command"]["request_id" if damage == "operation" else "command_name"] = "wrong"
                conn.execute("UPDATE task_runs SET metadata_json=? WHERE task_run_id=?", (json.dumps(value), binding.task_run_id))
            else:
                raw = conn.execute("SELECT outcome_json FROM tool_operations WHERE operation_id=?", (binding.request.operation_id,)).fetchone()[0]
                value = json.loads(raw)
                value["resource_scopes"] = []
                conn.execute("UPDATE tool_operations SET outcome_json=? WHERE operation_id=?", (json.dumps(value), binding.request.operation_id))
        result = deactivate_plugin(service.context.owner, repo, state["entry"], "revoke-bad-binding").report
        assert result["authority_revoked"] and not result["cleanup_confirmed"]
        assert result["errors"][0]["stage"] == "preparation"
        assert len(result["sessions"]) == 1 and result["sessions"][0]["confirmed"]
        assert repo.get_attempt(binding.attempt_id)["status"] == "done"


def test_cleanup_unknown_keeps_revoked_state_and_original_resource_evidence(tmp_path, monkeypatch):
    from agent_py_agent.agent import plugin_deactivation

    service = installed_manager(tmp_path)
    with activation_component(service, tmp_path, preparation=False) as state:
        store = process_session_store.ProcessSessionStore(state["client"].connection().binding.managed.hosted.store_root)
        original = plugin_deactivation.stop_process_session
        def unconfirmed(store, selected):
            cleaned = original(store, selected)
            return ProcessSessionCleanup(cleaned.record, False, cleaned.terminations)
        monkeypatch.setattr(plugin_deactivation, "stop_process_session", unconfirmed)
        result = service.command("/plugins disable sample-peek", revision=service.catalog().revision, request_id="disable")
        assert result["state"] == "outcome_unknown"
        assert service.installations.snapshot()[0].activation.phase == "revoked"
        assert len(store.list_records()[0]) == 1
        monkeypatch.setattr(plugin_deactivation, "stop_process_session", original)
        retry = service.command("/plugins disable sample-peek", revision=service.catalog().revision, request_id="retry")
        assert retry["state"] == "succeeded" and retry["details"]["cleanup_confirmed"]
        assert service.command("/plugins status disable", revision="", request_id="query")["state"] == "outcome_unknown"
