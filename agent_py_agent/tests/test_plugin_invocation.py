"""显式业务命令的原执行器与真实临时 MCP 组件合同；不计 TUI 或模型验收。"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import pytest

from agent_py_agent.agent.plugin_management import PluginManagement
from agent_py_agent.agent.plugin_runtime import PluginMCPClient, plugin_tool_name
from agent_py_agent.agent.runtime_db.repository import RuntimeConflictError, RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import runtime_db_path
from agent_py_agent.agent.tooling.cancellation import CancellationToken
from agent_py_agent.agent.tooling.mcp_protocol import MCPInbox
from agent_py_agent.tests.plugin_activation_fixtures import (
    _SERVER,
    installed_runtime_plugin,
    plugin_registry,
)
from agent_py_agent.tests.test_plugin_package import _manifest


# LLM: 测试服务只接真实 MCP 参数；在临时目录记录实际 tools/call 次数，帮助/批准回执本身不计执行。
# 函数用途: 构造会拒绝宿主额外参数的原生服务，用磁盘计数验证拒绝、撤销和重放没有重复调用。
def enabled_plugin(tmp_path):
    tools = [{"name": tool["name"], "description": tool["description"], "inputSchema": tool["input_schema"]}
             for tool in _manifest(b"")["tools"]]
    source = _SERVER.replace("__TOOLS__", repr(tools)).replace(
        '        value = Path(request["params"]["arguments"]["path"]).read_text()',
        '        assert set(request["params"]["arguments"]) == {"path"}\n'
        '        path = Path(request["params"]["arguments"]["path"])\n'
        '        with path.with_suffix(".calls").open("a") as counter:\n'
        '            counter.write("called\\n")\n'
        '        value = path.read_text()',
    )
    service = installed_runtime_plugin(tmp_path, module_source=source)
    result = service.command("/plugins enable sample-peek", revision=service.catalog().revision, request_id="enable")
    assert result["state"] == "succeeded", result
    input_file = tmp_path / "中文 input.txt"
    input_file.write_text("插件原生结果")
    return service, input_file, f'/plugins@sample-peek read "{input_file}"'


def test_nonadmin_explicit_call_approves_once_replays_after_remove_and_queries_own_result(tmp_path):
    admin, source, command = enabled_plugin(tmp_path)
    service = PluginManagement(replace(admin.context, is_admin=False))
    revision = service.catalog().revision
    approvals = []
    def approve(value, **kwargs):
        assert not source.with_suffix(".calls").exists()
        approvals.append(value)
        return {"permission_id": value["permission_id"], "decision": "approved"}
    result = service.command(command, revision=revision, request_id="business", request_permission=approve)
    assert result["state"] == "succeeded" and result["connection_cleanup"]["confirmed"], result
    assert "插件原生结果" in result["message"] and len(approvals) == 1
    assert source.with_suffix(".calls").read_text().splitlines() == ["called"]
    management = service.command("/plugins status enable", revision="", request_id="query-management")
    assert management["error_code"] == "PLUGIN_PERMISSION_DENIED"
    removed = admin.command("/plugins remove sample-peek", revision=admin.catalog().revision, request_id="remove")
    assert removed["state"] == "succeeded", removed
    replay = service.command(command, revision=revision, request_id="business",
                             request_permission=lambda *_, **__: pytest.fail("旧结果不得重复审批"))
    assert replay["state"] == "succeeded" and replay["output"] == result["output"]
    assert "connection_cleanup" not in replay
    query = service.command("/plugins status business", revision="", request_id="query")
    assert query["output"] == result["output"] and query["operation_id"] == result["operation_id"]
    with pytest.raises(RuntimeConflictError):
        service.command(command + " ", revision=revision, request_id="business")
    repo = RuntimeRepository(runtime_db_path(service.context.owner.home_dir))
    with repo._runtime_connection() as conn:
        count = conn.execute("SELECT count(*) FROM tool_operations WHERE operation_id=?", (result["operation_id"],)).fetchone()[0]
        assert count == 1


@pytest.mark.parametrize("decision", [None, "denied", "cancelled", "approved_session"])
def test_business_approval_never_defaults_to_approval_and_rejected_call_does_not_reopen(tmp_path, decision):
    service, source, command = enabled_plugin(tmp_path)
    revision = service.catalog().revision
    consumer = None if decision is None else lambda value, **_: {"permission_id": value["permission_id"], "decision": decision}
    result = service.command(command, revision=revision, request_id="business", request_permission=consumer)
    assert result["state"] in {"rejected", "approval_required"}, result
    assert result["connection_cleanup"]["confirmed"] and not source.with_suffix(".calls").exists()
    replay = service.command(command, revision=revision, request_id="business",
                             request_permission=lambda *_, **__: pytest.fail("拒绝的请求不得重开"))
    assert replay["state"] == result["state"] and "connection_cleanup" not in replay


def test_static_errors_and_disabled_tools_do_not_start_plugin_or_create_business_attempt(tmp_path, monkeypatch):
    service, source, command = enabled_plugin(tmp_path)
    def unexpected(*args, **kwargs):
        pytest.fail("拒绝和帮助不能创建插件连接")
    monkeypatch.setattr(PluginMCPClient, "__init__", unexpected)
    assert service.command("/plugins@sample-peek read --help", revision="", request_id="help")["ok"]
    assert service.command("/plugins@sample-peek read", revision=service.catalog().revision,
                           request_id="bad")["error_code"] == "INVALID_COMMAND_ARGUMENTS"
    assert service.command(command, revision="old", request_id="stale")["error_code"] == "PLUGIN_CATALOG_STALE"
    for changes in ({"business_allowed": False}, {"disabled_tools": frozenset({plugin_tool_name("sample-peek", "read")})},
                    {"enabled": False}):
        blocked = PluginManagement(replace(service.context, **changes))
        result = blocked.command(command, revision=blocked.catalog().revision, request_id="blocked")
        assert result["state"] == "rejected", result
    assert not source.with_suffix(".calls").exists()
    repo = RuntimeRepository(runtime_db_path(service.context.owner.home_dir))
    with repo._runtime_connection() as conn:
        assert conn.execute("SELECT count(*) FROM agent_attempts").fetchone()[0] == 2  # 安装、启用


def test_duplicate_and_disable_during_approval_cannot_execute_old_or_new_activation(tmp_path):
    service, source, command = enabled_plugin(tmp_path)
    revision = service.catalog().revision
    entered, release = Event(), Event()
    def approve(value, **kwargs):
        entered.set()
        assert release.wait(20)
        return {"permission_id": value["permission_id"], "decision": "approved"}
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.command, command, revision=revision, request_id="business", request_permission=approve)
        try:
            assert entered.wait(10)
            duplicate = service.command(command, revision=revision, request_id="business",
                                        request_permission=lambda *_, **__: pytest.fail("不得重复审批"))
            assert duplicate["state"] == "running" and "connection_cleanup" not in duplicate
            stopped = service.command("/plugins disable sample-peek", revision=service.catalog().revision, request_id="stop")
            assert stopped["state"] == "succeeded" and stopped["details"]["released"], stopped
            restarted = service.command("/plugins enable sample-peek", revision=service.catalog().revision, request_id="restart")
            assert restarted["state"] == "succeeded", restarted
        finally:
            release.set()
        result = future.result(timeout=15)
    assert result["state"] != "succeeded" and not source.with_suffix(".calls").exists(), result
    replay = service.command(command, revision=revision, request_id="business")
    assert replay["operation_id"] == result["operation_id"] and replay["state"] == result["state"]
    assert service.installations.snapshot()[0].enabled


def test_cancellation_during_approval_closes_only_this_business_connection(tmp_path):
    service, source, command = enabled_plugin(tmp_path)
    registry = plugin_registry(service)
    token = CancellationToken()
    def cancel(value, **kwargs):
        token.cancel()
        return {"permission_id": value["permission_id"], "decision": "approved"}
    try:
        registry.prepare_for_run()
        other = next(client for client in registry._mcp_clients if isinstance(client, PluginMCPClient))
        result = service.command(command, revision=service.catalog().revision, request_id="business",
                                 request_permission=cancel, cancellation_token=token)
        assert result["error_code"] == "CANCELLED" and result["connection_cleanup"]["confirmed"], result
        assert not source.with_suffix(".calls").exists() and service.installations.snapshot()[0].enabled
        assert other.is_running() and registry.tools[plugin_tool_name("sample-peek", "read")].availability().available
        assert "read_file" in registry.tools
    finally:
        registry.close_mcp_clients()


def test_only_trusted_autonomous_mode_can_execute_without_an_interactive_consumer(tmp_path):
    service, source, command = enabled_plugin(tmp_path)
    autonomous = PluginManagement(replace(service.context, approval_mode="auto"))
    result = autonomous.command(command, revision=autonomous.catalog().revision, request_id="business")
    assert result["state"] == "succeeded" and result["connection_cleanup"]["confirmed"], result
    assert source.with_suffix(".calls").read_text().splitlines() == ["called"]


def test_cleanup_ack_failure_does_not_erase_original_success_or_repeat_handler(tmp_path, monkeypatch):
    service, source, command = enabled_plugin(tmp_path)
    service = PluginManagement(replace(service.context, approval_mode="auto"))
    revision = service.catalog().revision
    stop = PluginMCPClient.stop
    def lose_ack(client):
        assert stop(client).confirmed
        raise OSError("fixture cleanup acknowledgement lost")
    monkeypatch.setattr(PluginMCPClient, "stop", lose_ack)
    result = service.command(command, revision=revision, request_id="business")
    assert result["state"] == "succeeded" and not result["connection_cleanup"]["confirmed"], result
    assert "退出尚未确认" in result["message"]
    replay = service.command(command, revision=revision, request_id="business")
    assert replay["state"] == "succeeded" and "connection_cleanup" not in replay
    assert replay["output"] == result["output"]
    assert source.with_suffix(".calls").read_text().splitlines() == ["called"]


def test_external_cancellation_is_observed_inside_original_mcp_initialization_wait(tmp_path, monkeypatch):
    service, source, command = enabled_plugin(tmp_path)
    entered, external_cancel = Event(), Event()
    token = CancellationToken(_external_check=external_cancel.is_set)
    dispatch, await_response = MCPInbox.dispatch, MCPInbox.await_response
    def drop_initialization(self, message):
        if "serverInfo" in message.get("result", {}):
            return None
        return dispatch(self, message)
    def observe_wait(self, req_id, method, deadline):
        if method == "initialize":
            entered.set()
        return await_response(self, req_id, method, deadline)
    monkeypatch.setattr(MCPInbox, "dispatch", drop_initialization)
    monkeypatch.setattr(MCPInbox, "await_response", observe_wait)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.command, command, revision=service.catalog().revision,
                             request_id="business", cancellation_token=token)
        try:
            assert entered.wait(10)
            external_cancel.set()  # 原 external_check 只返回事实，不执行 token.cancel 回调。
            result = future.result(timeout=5)
        finally:
            if not future.done():
                token.cancel()
                future.result(timeout=15)
    assert result["state"] == "rejected" and result["connection_cleanup"]["confirmed"], result
    assert not source.with_suffix(".calls").exists() and service.installations.snapshot()[0].enabled
