"""C10：IM 复用插件服务，不把命令丢进模型，不拿正文里的管理员声明授权。"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.gateway_parts import control_service, http_handlers
from agent_py_agent.agent.gateway_parts import plugin_command_service as module
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.plugin_management import PluginManagement
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_gateway_plugin_commands import Handler


def _command(text):
    command = parse_conversation_control(text, reject_unknown_slash=True)
    assert command is not None and command.kind == "plugins" and command.valid
    assert command.value == text.strip()
    return command


@pytest.fixture
def host(tmp_path):
    return SimpleNamespace(config=AgentConfig(gateway_per_user_owner_scoping=True), home_paths=home_paths(tmp_path))


def _scope(channel="feishu", admin=False):
    owner = OwnerIdentity("local", "main", "main") if admin else OwnerIdentity(channel, "users", "alice")
    return GatewayControlScope(user_id="alice", channel=channel, conversation_id="session-a",
                               metadata={"message_id": "im-message-1"}, resolved_owner=owner)


def _run(host, text, scope=None):
    return control_service.execute_gateway_conversation_control(host, None, _command(text), scope or _scope())


@pytest.mark.parametrize("text", ["/plugins", "/plugins list", "/PLUGINS@Demo help", "/plugins@",
                                 '/plugins@Demo run --path "中文 空格" -- -x | literal'])
def test_plugin_control_keeps_raw_text_for_the_shared_parser(text):
    _command(text)


@pytest.mark.parametrize("channel", ["feishu", "qq", "custom-im"])
@pytest.mark.parametrize("text", ["/plugins list", '/plugins@Demo run --path "中文 空格"'])
def test_im_routes_to_the_same_management_service_with_host_parameters(host, monkeypatch, channel, text):
    _command(text)
    seen = []

    def execute(manager, command_text, **kwargs):
        seen.append((manager.context, command_text, kwargs))
        return {"ok": True, "message": "第一行\n第二行", "request_id": kwargs["request_id"]}

    monkeypatch.setattr(PluginManagement, "command", execute)
    result = _run(host, text, _scope(channel))
    context, actual_text, kwargs = seen[0]
    assert actual_text == text
    assert (context.actor_id, context.channel, context.conversation_id) == ("alice", channel, "session-a")
    assert context.owner.identity == _scope(channel).resolved_owner and context.is_admin is False
    expected = PluginManagement(context).catalog().revision
    assert kwargs["revision"] == expected and kwargs["request_id"]
    assert result.kind == "plugins" and result.ok and result.message == "第一行\n第二行"
    assert result.request_id == kwargs["request_id"]


@pytest.mark.parametrize("text", ["/plugins install sample.zip", "/plugins configure demo -f config.json",
                                 "/plugins enable demo", "/plugins disable demo", "/plugins remove demo",
                                 "/plugins update demo sample.zip"])
def test_non_admin_mutations_are_rejected_with_visible_and_structured_code(host, text):
    spoofed = replace(_scope(), metadata={"is_admin": True, "owner_id": "main", "actor": "admin"})
    result = _run(host, text, spoofed)
    assert result.ok is False and result.error_code == "PLUGIN_PERMISSION_DENIED"
    assert "错误码：PLUGIN_PERMISSION_DENIED" in result.message
    assert result.to_dict()["error_code"] == "PLUGIN_PERMISSION_DENIED"
    assert not (host.home_paths.root / "owners" / "providers" / "feishu" / "users" / "alice").exists()


@pytest.mark.parametrize("text", ["/plugins", "/plugins help install", "/plugins list", "/plugins list --enabled",
                                 "/plugins list --bad", "/plugins@missing help"])
def test_im_read_results_match_tui_for_the_same_action(host, monkeypatch, text):
    im = _run(host, text)
    handler = Handler({"conversation_id": "session-a"})
    monkeypatch.setattr(module, "resolve_gateway_scope_owner", lambda *_: _scope().resolved_owner)
    tui_catalog = module.plugin_http_response(handler, SimpleNamespace(agent=host), handler.body)
    tui_body = {**handler.body, "catalog_revision": tui_catalog["catalog"]["revision"],
                "plugin_request_id": im.request_id or "tui-1"}
    tui = module.plugin_http_response(handler, SimpleNamespace(agent=host), tui_body, text=text)
    assert (im.ok, im.message, im.error_code) == (tui["ok"], tui["message"], tui.get("error_code", ""))
    assert isinstance(im.message, str) and "PluginCommandCatalog(" not in im.message


def test_bound_admin_private_chat_uses_settings_owner_rule(host, monkeypatch):
    _command("/plugins enable demo")
    seen = []

    def execute(manager, text, **kwargs):
        seen.append(manager.context.is_admin)
        return {"ok": True, "message": "已交给原启用服务。"}

    monkeypatch.setattr(PluginManagement, "command", execute)
    assert _run(host, "/plugins enable demo", _scope(admin=True)).ok
    assert seen == [True]


def test_confirmation_preview_and_confirm_are_not_rewritten(host, monkeypatch):
    _command("/plugins enable demo")
    preview = "将启动外部程序，请先核对。\n确认启用：/plugins enable demo --confirm abc123"
    commands = []

    def execute(manager, text, **kwargs):
        commands.append(text)
        return {"ok": text.endswith("--confirm abc123"), "message": preview if len(commands) == 1 else "已启用。"}

    monkeypatch.setattr(PluginManagement, "command", execute)
    first = _run(host, "/plugins enable demo", _scope(admin=True))
    assert not first.ok and first.message == preview
    assert commands == ["/plugins enable demo"]
    second = _run(host, "/plugins enable demo --confirm abc123", _scope(admin=True))
    assert second.ok and second.message == "已启用。"
    assert commands == ["/plugins enable demo", "/plugins enable demo --confirm abc123"]


@pytest.mark.parametrize("entry", ["ask", "control"])
def test_im_http_entries_use_durable_conversation_control_instead_of_the_old_shortcut(host, monkeypatch, entry):
    handler = Handler({"conversation_id": "session-a", "prompt": "/plugins list",
                       "command": "/plugins list", "metadata": {"message_id": "im-message-1"}})
    monkeypatch.setattr(http_handlers, "require_trusted_source", lambda _: False)
    monkeypatch.setattr(http_handlers, "_request_channel", lambda _: ("alice", "feishu"))
    monkeypatch.setattr(http_handlers, "_request_identity", lambda _: ("alice", None))
    seen = []

    def persistent(_handler, _server, **kwargs):
        seen.append(kwargs)

    monkeypatch.setattr(http_handlers, "_handle_persistent_control_operation", persistent)
    server = SimpleNamespace(agent=host)
    if entry == "ask":
        http_handlers.handle_ask(handler, server, Mock(side_effect=AssertionError("不能创建模型任务")))
    else:
        http_handlers.handle_control(handler, server)
    assert len(seen) == 1 and seen[0]["command"].kind == "plugins"
    assert seen[0]["command_text"] == "/plugins list"
    scope = seen[0]["scope"]
    assert (scope.user_id, scope.channel, scope.conversation_id) == ("alice", "feishu", "session-a")
    assert scope.metadata["message_id"] == "im-message-1"


def test_plugin_control_cannot_fall_into_local_stop_and_round_trips_raw_text():
    from agent_py_agent.cli.chat_parts.control_runtime import _command_text, _execute_local_control

    text = '/plugins@Demo run --path "中文 空格"'
    command = _command(text)
    assert _command_text(command) == text
    result = _execute_local_control(SimpleNamespace(state=SimpleNamespace(request_id="", running=True)), command)
    assert not result.ok and result.kind == "plugins" and "Gateway" in result.message


@pytest.mark.parametrize("kind", ["executable", "interpreter"])
def test_real_confirmation_gate_stays_closed_before_explicit_user_confirmation(tmp_path, monkeypatch, kind):
    from agent_py_agent.tests.test_plugin_any_language import (
        _SERVER,
        PLUGIN_ID,
        _declaration,
        _fake_interpreter,
        _installed,
    )

    interpreter = _fake_interpreter(tmp_path, monkeypatch) if kind == "interpreter" else None
    declaration = _declaration(kind, interpreter.name if interpreter else "")
    source = "server.py" if interpreter else "bin/server.py"
    service = _installed(tmp_path, declaration, {source: _SERVER})
    monkeypatch.setattr(module, "_scope_management", lambda *_: service)
    first = _run(None, f"/plugins enable {PLUGIN_ID}", _scope(admin=True))
    assert not first.ok and "--confirm " in first.message and "/plugins enable " + PLUGIN_ID in first.message
    # 确认预览是等待用户确认的状态，不是参数错误：专用错误码只在结构里，文本不再追加通用错误码行
    assert first.error_code == "PLUGIN_CONFIRMATION_REQUIRED"
    assert "错误码" not in first.message and first.message.startswith("启用前需要你确认")
    if interpreter:
        assert str(interpreter.resolve()) in first.message
    wrong = _run(None, f"/plugins enable {PLUGIN_ID} --confirm 000000000000", _scope(admin=True))
    assert not wrong.ok and "--confirm " in wrong.message
    assert wrong.error_code == "PLUGIN_CONFIRMATION_REQUIRED" and "错误码" not in wrong.message
    assert service.installations.snapshot()[0].activation is None
    owner = service.context.owner
    assert not (owner.plugins_dir / "data" / PLUGIN_ID / "started").exists()
    assert not (owner.plugins_dir / "environments").exists() or not any((owner.plugins_dir / "environments").iterdir())


def test_confirmation_receipt_keeps_code_out_of_text_on_both_im_and_tui(host, monkeypatch):
    _command("/plugins enable demo")
    preview = "启用前需要你确认：这个插件会以你本人的权限在本机运行下面的程序。\n插件：demo 0.1.0（包摘要 abcdef0123456789…）"
    payload = {"ok": False, "state": "not_started", "error_code": "PLUGIN_CONFIRMATION_REQUIRED",
               "request_id": "r-confirm-1", "details": {"reason": "confirmation_required",
                                                        "state": "confirmation_required",
                                                        "commit_state": "not_committed",
                                                        "confirmation": {"plugin_id": "demo", "version": "0.1.0",
                                                                         "package_sha256": "abcdef0123456789",
                                                                         "confirm_code": "abc123"}},
               "message": preview}
    monkeypatch.setattr(PluginManagement, "command", lambda *a, **k: payload)
    im = _run(host, "/plugins enable demo", _scope(admin=True))
    handler = Handler({"conversation_id": "session-a"})
    monkeypatch.setattr(module, "resolve_gateway_scope_owner", lambda *_: _scope(admin=True).resolved_owner)
    tui = module.plugin_http_response(handler, SimpleNamespace(agent=host), handler.body, text="/plugins enable demo")
    assert im.message == preview and "错误码" not in im.message
    assert im.error_code == "PLUGIN_CONFIRMATION_REQUIRED"
    assert tui["message"] == preview and "错误码" not in tui["message"]
    assert tui["error_code"] == "PLUGIN_CONFIRMATION_REQUIRED"
    assert tui["details"]["reason"] == "confirmation_required"
    assert tui["details"]["state"] == "confirmation_required"


def test_unauthorized_source_explains_the_allowed_root_on_both_im_and_tui(host, tmp_path, monkeypatch):
    from agent_py_agent.agent.path_access_policy import PathAccessPolicy
    from agent_py_agent.tests.test_plugin_management import manager
    from agent_py_agent.tests.test_plugin_package import _bundle

    root = tmp_path / "owner-root"
    root.mkdir()
    service, _source = manager(tmp_path, workspace=root,
                               path_policy=PathAccessPolicy.from_values(mode="normal", owner_scope_root=str(root)))
    outside = tmp_path / "elsewhere" / "pkg.zip"
    outside.parent.mkdir()
    outside.write_bytes(_bundle())
    monkeypatch.setattr(module, "_scope_management", lambda *_args, **_kwargs: service)
    text = f'/plugins install "{outside}"'
    im = _run(host, text, _scope(admin=True))
    handler = Handler({"conversation_id": "session-a"})
    monkeypatch.setattr(module, "resolve_gateway_scope_owner", lambda *_: _scope(admin=True).resolved_owner)
    tui_body = {**handler.body, "catalog_revision": service.catalog().revision, "plugin_request_id": "tui-unauthorized"}
    tui = module.plugin_http_response(handler, SimpleNamespace(agent=host), tui_body, text=text)
    expected = f"请把插件包放到 {root.resolve()} 下再试"
    assert expected in im.message and "未获授权" in im.message and str(outside) not in im.message
    assert expected in tui["message"] and str(outside) not in tui["message"]
    assert tui["details"]["reason"] == "source_unauthorized" and tui["details"]["allowed_root"] == str(root.resolve())


@pytest.mark.parametrize("entry", ["ask", "control"])
def test_http_control_receipt_replays_plugin_text_without_resubmitting(host, tmp_path, monkeypatch, entry):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths

    agent = SimpleAgent(AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=True), tmp_path)
    server = SimpleNamespace(agent=agent, paths=gateway_paths(agent))
    monkeypatch.setattr(http_handlers, "require_trusted_source", lambda _: False)
    monkeypatch.setattr(http_handlers, "_request_channel", lambda _: ("alice", "feishu"))
    monkeypatch.setattr(http_handlers, "_request_identity", lambda _: ("alice", None))
    execute = Mock(return_value={"ok": True, "message": "插件原服务的纯文本输出。"})
    monkeypatch.setattr(PluginManagement, "command", execute)
    body = {"conversation_id": "session-a", "command": "/plugins list", "prompt": "/plugins list",
            "metadata": {"message_id": "im-message-1", "channel_chat_type": "p2p"}}
    replies = []
    for _ in range(2):
        handler = Handler(body)
        if entry == "ask":
            http_handlers.handle_ask(handler, server, Mock(side_effect=AssertionError("不能创建模型任务")))
        else:
            http_handlers.handle_control(handler, server)
        replies.append(handler.reply)
    assert execute.call_count == 1 and replies[0] == replies[1]
    status, payload = replies[0]
    assert status == 200 and payload["status"] == "control" and payload["disposition"] == "system_command"
    assert payload["kind"] == "plugins" and payload["message"] == "插件原服务的纯文本输出。"
    assert payload["control_state"] == "completed" and payload["operation_id"]


def test_unconfirmed_service_exception_preserves_original_request_instead_of_retrying(host, monkeypatch):
    execute = Mock(side_effect=RuntimeError("private-secret-path"))
    monkeypatch.setattr(PluginManagement, "command", execute)
    result = _run(host, "/plugins list")
    assert not result.ok and result.error_code == "PLUGIN_COMMAND_OUTCOME_UNKNOWN"
    assert result.request_id and f"/plugins status {result.request_id}" in result.message
    assert "private-secret" not in result.message and execute.call_count == 1
