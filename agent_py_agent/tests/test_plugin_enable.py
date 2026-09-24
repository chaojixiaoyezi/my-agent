"""真实临时环境/MCP 的启用组件验收；不启动产品 TUI 或模型。"""

import atexit
import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.plugin_management import PluginManagement
from agent_py_agent.agent.plugin_runtime import PluginMCPClient, plugin_tool_name
from agent_py_agent.agent.tooling.mcp_client import redact_env_for_log
from agent_py_agent.agent.tooling.process_session_store import (
    ProcessSessionStore,
    process_session_store_root,
)
from agent_py_agent.tests.plugin_activation_fixtures import (
    installed_runtime_plugin,
    invoke_registered_tool,
    plugin_registry,
)


def test_enable_active_is_unchanged_and_missing_plugin_has_explicit_failure(tmp_path, monkeypatch):
    service = installed_runtime_plugin(tmp_path)
    initial = service.command("/plugins enable sample-peek", revision=service.catalog().revision, request_id="enable")
    assert initial["state"] == "succeeded", initial
    before = service.installations.snapshot()
    def unexpected(*args, **kwargs):
        pytest.fail("已启用或不存在的插件不得准备新环境或连接")
    monkeypatch.setattr(PluginMCPClient, "__init__", unexpected)
    repeated = service.command("/plugins enable sample-peek", revision=service.catalog().revision, request_id="repeat")
    assert repeated["state"] == "succeeded" and repeated["details"]["outcome"] == "unchanged", repeated
    assert service.installations.snapshot() == before
    missing = service.command("/plugins enable missing-peek", revision=service.catalog().revision, request_id="missing")
    assert missing["state"] == "failed" and missing["details"]["reason"] == "plugin_missing", missing
    assert service.installations.snapshot() == before


def test_actual_enable_and_registry_view_call_then_disable(tmp_path):
    service = installed_runtime_plugin(tmp_path, configure=True)
    registry = plugin_registry(service)
    view = registry.with_access_policy(access_mode="full-access", path_access_mode="full", owner_scope_root="")
    assert registry._mcp_clients == [] and view._mcp_clients is registry._mcp_clients
    revision = service.catalog().revision
    enabled = service.command("/plugins enable sample-peek", revision=revision, request_id="enable")
    assert enabled["state"] == "succeeded", enabled
    assert enabled["details"]["candidate_cleanup"]["confirmed"]
    assert "private-settings-value" not in json.dumps(enabled)
    replay = service.command("/plugins enable sample-peek", revision=revision, request_id="enable")
    assert replay["details"] == enabled["details"]
    owner = service.context.owner
    store = ProcessSessionStore(process_session_store_root(owner.home_dir, owner.home_dir))
    records, errors = store.list_records()
    assert not errors and len(records) == 4  # venv/probe/pip 和一次 MCP 候选
    assert all(row["status"] in {"exited", "killed"} for row in records)
    store.prune_finished(0)
    assert len(store.list_records()[0]) == 4  # 准备记录仍由原管理结果消费，不能按普通历史先删。
    assert all(row["retain_until_consumed"] for row in records if row["activation_scope"] is None)
    name = plugin_tool_name("sample-peek", "read")
    try:
        view.prepare_for_run()
        assert name in view.tools and name not in registry.tools
        client = next(item for item in registry._mcp_clients if isinstance(item, PluginMCPClient))
        assert client.validated_tools[0].runtime_policy.effect_resolver.default_effect == "dangerous"
        data_dir = owner.plugins_dir / "data" / "sample-peek"
        assert client.config.env["MY_AGENT_PLUGIN_DATA_DIR"] == str(data_dir) and data_dir.is_dir()
        registry.prepare_for_run()
        assert registry.tools[name] is view.tools[name]
        assert len([item for item in registry._mcp_clients if isinstance(item, PluginMCPClient)]) == 1
        source = tmp_path / "input.txt"
        source.write_text("实际插件读取：中文内容")
        result = invoke_registered_tool(service, registry, name, {"path": str(source)})
        assert result["state"] == "succeeded", result
        assert "实际插件读取" in json.dumps(result, ensure_ascii=False)
        old = registry.runtime_snapshot(run_id="old")
        disable_revision = service.catalog().revision
        disabled = service.command("/plugins disable sample-peek", revision=disable_revision, request_id="disable")
        assert disabled["state"] == "succeeded", disabled
        assert disabled["details"]["released"]
        old_entry = client.installation
        assert not (owner.plugins_dir / "environments" / old_entry.activation.plan.environment_ref).exists()
        assert not registry.tools[name].availability().available
        rejected = invoke_registered_tool(service, registry, name, {"path": str(source)}, request_id="old-call", snapshot=old)
        # 停用先撤销激活：旧快照上的调用在发送前按激活失效拒绝（TOOL_UNAVAILABLE，不建议重试），不再是可重试的执行失败。
        assert rejected["state"] == "failed" and rejected["error_code"] == "TOOL_UNAVAILABLE", rejected
        view.prepare_for_run()
        registry.prepare_for_run()
        assert name not in view.tools and name not in registry.tools
        assert "read_file" in registry.tools
        new = service.command("/plugins enable sample-peek", revision=service.catalog().revision, request_id="reenable")
        assert new["state"] == "succeeded", new
        assert service.installations.snapshot()[0].activation_id != old_entry.activation_id
        replay = service.command("/plugins disable sample-peek", revision=disable_revision, request_id="disable")
        assert replay["details"] == disabled["details"]
        assert service.installations.snapshot()[0].enabled
        registry.prepare_for_run()
        assert name in registry.tools and "read_file" in registry.tools
        current_call = invoke_registered_tool(service, registry, name, {"path": str(source)}, request_id="new-call")
        assert current_call["state"] == "succeeded", current_call
        stale = invoke_registered_tool(service, registry, name, {"path": str(source)}, request_id="still-old", snapshot=old)
        assert stale["state"] == "failed", stale
        final = service.command("/plugins disable sample-peek", revision=service.catalog().revision, request_id="final")
        assert final["state"] == "succeeded" and final["details"]["released"], final
        assert not store.list_records()[0]
    finally:
        registry.close_mcp_clients()


@pytest.mark.parametrize("change", ["missing", "extra", "description", "schema", "startup"])
def test_bad_complete_directory_never_publishes(tmp_path, change):
    tool = {"name": "read", "description": "读取文本", "inputSchema": {
        "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"],
    }}
    tools = [tool]
    if change == "missing":
        tools = []
    elif change == "extra":
        tools.append({**tool, "name": "extra"})
    elif change == "description":
        tool["description"] = "另一个工具"
    elif change == "schema":
        tool["inputSchema"]["properties"]["path"] = {"type": "integer"}
    service = installed_runtime_plugin(tmp_path, tools_override=tools,
        module_source="raise RuntimeError('fixture startup failure')" if change == "startup" else None)
    result = service.command("/plugins enable sample-peek", revision=service.catalog().revision, request_id="enable")
    assert result["state"] != "succeeded", result
    entry = service.installations.snapshot()[0]
    assert not entry.enabled and entry.activation.phase == "preparing"
    registry = plugin_registry(service)
    try:
        registry.prepare_for_run()
        assert not registry._mcp_clients
        assert plugin_tool_name("sample-peek", "read") not in registry.tools
    finally:
        registry.close_mcp_clients()
    disabled = service.command("/plugins disable sample-peek", revision=service.catalog().revision, request_id="disable")
    assert disabled["state"] == "succeeded", disabled


@pytest.mark.parametrize("context", [{"is_admin": False}, {"enable_allowed": False}, {"enabled": False}])
def test_enable_policy_refuses_before_environment_creation(tmp_path, context):
    service = installed_runtime_plugin(tmp_path)
    refused = PluginManagement(replace(service.context, **context))
    result = refused.command("/plugins enable sample-peek", revision=refused.catalog().revision, request_id="enable")
    assert result["state"] == "rejected", result
    assert not (service.context.owner.plugins_dir / "environments").exists()
    assert service.installations.snapshot()[0].activation is None


def test_plugin_identity_and_private_environment_projection():
    names = {plugin_tool_name(plugin, tool) for plugin in ("example-a", "example_a", "Example-a")
             for tool in ("read-file", "read_file", "Read-file")}
    assert len(names) == 9 and all(len(name) <= 64 for name in names)
    assert redact_env_for_log({"MY_AGENT_PLUGIN_SETTINGS": '{"note":"private"}'}) == {
        "MY_AGENT_PLUGIN_SETTINGS": "<redacted>",
    }


def test_candidate_cleanup_storage_failure_prevents_active(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling.process_session_records import PROCESS_TERMINAL_STATUSES
    from agent_py_agent.agent.tooling.process_session_store import ProcessSessionTransaction

    service = installed_runtime_plugin(tmp_path)
    original = ProcessSessionTransaction.write
    initialize, clients = PluginMCPClient.__init__, []

    def observe_client(self, *args, **kwargs):
        initialize(self, *args, **kwargs)
        clients.append(self)

    monkeypatch.setattr(PluginMCPClient, "__init__", observe_client)

    def fail_candidate_cleanup(self, payload):
        if payload.get("activation_scope") and "cleanup" in payload.get("termination", {}):
            raise OSError("fixture candidate cleanup persistence failure")
        return original(self, payload)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(ProcessSessionTransaction, "write", fail_candidate_cleanup)
            result = service.command("/plugins enable sample-peek", revision=service.catalog().revision, request_id="enable")
        assert result["state"] == "outcome_unknown", result
        entry = service.installations.snapshot()[0]
        assert not entry.enabled and entry.activation.phase == "preparing"
        owner = service.context.owner
        store = ProcessSessionStore(process_session_store_root(owner.home_dir, owner.home_dir))
        candidate = next(row for row in store.list_records()[0] if row["activation_scope"])
        assert "cleanup" not in (candidate.get("termination") or {})
        disabled = service.command("/plugins disable sample-peek", revision=service.catalog().revision, request_id="disable")
        # 独立 host 可能先保存自然终态；仅该事实允许重新核验，丢失清理回执的 unknown 不能靠 PID 消失洗白。
        expected = "succeeded" if candidate["status"] in PROCESS_TERMINAL_STATUSES else "outcome_unknown"
        assert disabled["state"] == expected, disabled
        activation = service.installations.snapshot()[0].activation
        assert activation is None if expected == "succeeded" else activation.phase == "revoked"
        registry = plugin_registry(service)
        try:
            registry.prepare_for_run()
            assert not registry._mcp_clients and "read_file" in registry.tools
        finally:
            registry.close_mcp_clients()
        assert service.command("/plugins status enable", revision="", request_id="query")["state"] == "outcome_unknown"
    finally:
        for client in clients:
            # 只移除已确认测试原生资源退出后的 atexit 重复回调；持久 UNKNOWN 和原异常都不修改。
            error = client._transport._cleanup_error
            assert error is not None and error.report["termination_receipts"]
            assert all(item["confirmed"] for item in error.report["termination_receipts"])
            atexit.unregister(client.stop)
