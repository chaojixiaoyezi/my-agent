"""原客户端与 IM/HTTP handler 的完整回执合同；没有真实网络、Gateway 或用户接收端。"""
from types import SimpleNamespace

from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.gateway_parts import plugin_command_service as gateway
from agent_py_agent.agent.plugin_management import PluginManagement
from agent_py_agent.cli.chat_parts.plugin_command_client import PluginCommandClient
from agent_py_agent.tests.test_plugin_legacy_management import fake_processes, installed_manager


def test_im_and_tui_client_preserve_full_long_preview_and_exact_confirm_command(tmp_path, monkeypatch):
    service = installed_manager(tmp_path, legacy_sandbox_default=False)
    roots = [(tmp_path / (f"授权目录 {index:02d} 中文 空格 " + "x" * 100)).resolve() for index in range(32)]
    for root in roots:
        root.mkdir()
    text = '/plugins enable sample-peek ' + ' '.join(f'--read-root "{root}"' for root in roots) + ' --network'
    monkeypatch.setattr(gateway, "_scope_management", lambda *args: service)
    monkeypatch.setattr(gateway, "_management", lambda *args: service)
    command = parse_conversation_control(text, reject_unknown_slash=True)
    monkeypatch.setattr(gateway, "uuid", SimpleNamespace(uuid4=lambda: SimpleNamespace(hex="long-preview")))
    im = gateway.execute_plugin_control(None, command, None)
    agent = SimpleNamespace(owner_identity=service.context.owner.identity, config=SimpleNamespace(gateway_port=7777))
    client = PluginCommandClient(agent, "session", use_gateway=True)
    seen = []

    def transport(host, payload, interaction):
        if payload["command"] == text:
            payload = {**payload, "plugin_request_id": "long-preview"}
        seen.append(payload.copy())
        return gateway.plugin_http_response(None, SimpleNamespace(agent=object()), payload, text=payload["command"])

    monkeypatch.setattr("agent_py_agent.cli.chat_parts.plugin_command_client._request_gateway_plugins", transport)
    tui = client.command(text, revision=service.catalog().revision)
    facts = tui["details"]["confirmation"]
    assert im.message == tui["message"] and len(im.message) > 20000
    assert all(str(root) in im.message for root in roots) and len(facts["permissions"]["read_roots"]) == 32
    assert facts["confirm_command"] in im.message and "--network" in facts["confirm_command"]
    assert im.message.index("固定确认事实") < im.message.index("确认无误后输入：")
    calls = fake_processes(monkeypatch)
    result = client.command(facts["confirm_command"], revision=service.catalog().revision)
    assert result["state"] == "succeeded", result
    assert result["request_id"] == facts["authorization_id"] and calls == ["prepare", "client"]
    assert service.installations.snapshot()[0].activation_id == facts["activation_id"]
    assert seen[-1]["command"] == facts["confirm_command"]
    replay = gateway.execute_plugin_control(None, parse_conversation_control(facts["confirm_command"]), None)
    assert replay.ok and replay.request_id == facts["authorization_id"] and calls == ["prepare", "client"]


def test_unknown_reenable_observation_never_borrows_later_generation(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_plugin_legacy_management import (
        confirm,
        fake_settled_deactivation,
        preview,
    )

    service = installed_manager(tmp_path, legacy_sandbox_default=False)
    fake_processes(monkeypatch)
    assert confirm(service, preview(service))["state"] == "succeeded"
    fake_settled_deactivation(monkeypatch, settled=False)
    unknown = confirm(service, preview(service, request="reenable"))
    assert unknown["state"] == "outcome_unknown"
    old = unknown["details"]["previous_cleanup"]["activation_id"]
    before = service.installations.snapshot()[0]
    monkeypatch.setattr(service.installations, "snapshot", lambda: ())
    observed = service._reply({"state": "outcome_unknown", "tool_name": "plugin_enable",
                              "operation_id": unknown["operation_id"]}, unknown["request_id"])
    assert observed["state"] == "outcome_unknown" and "details" not in observed and old not in observed["message"]
    monkeypatch.setattr(service.installations, "snapshot", lambda: (before,))
    wrong = service._reply({"state": "outcome_unknown", "tool_name": "plugin_enable",
                           "operation_id": "different-original-operation"}, unknown["request_id"])
    assert wrong["state"] == "outcome_unknown" and "details" not in wrong and old not in wrong["message"]
