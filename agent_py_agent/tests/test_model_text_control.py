"""聊天里的 /model 文字命令：IM 用户查看并选择自己的或管理员共享的模型，不收发密钥。"""

from __future__ import annotations

import json
from types import SimpleNamespace

from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.gateway_parts import control_service, model_profile_service
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.request_errors import gateway_client_error_message
from agent_py_agent.agent.settings.shared_model_catalog import set_shared_profile
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.cli.chat_parts import control_runtime, tui_actions
from agent_py_agent.tests.test_model_profiles import Host, add


def _gateway_setup(tmp_path, monkeypatch):
    base = Host(tmp_path)
    base.home_paths = home_paths(tmp_path)
    monkeypatch.setattr(
        model_profile_service, "resolve_gateway_scope_owner",
        lambda _base, scope: OwnerIdentity.provider_user(scope.channel, scope.user_id),
    )
    admin = Host(base.home_paths.config_dir, owner="local/main")
    admin.home_paths.owner_kind = "main"
    return base, admin


def _run(base, text: str, user: str = "ou_feishu_user"):
    scope = control_service.GatewayControlScope(user_id=user, channel="feishu", conversation_id="oc_chat")
    command = parse_conversation_control(text, reject_unknown_slash=True)
    return control_service.execute_gateway_conversation_control(base, gateway_paths_from_root(base.home_paths.root), command, scope)


def test_model_text_forms_parse_to_structured_operations():
    view = parse_conversation_control("/model", reject_unknown_slash=True)
    assert (view.kind, view.operation, view.valid) == ("model", "view", True)
    chosen = parse_conversation_control("/model 2")
    assert (chosen.operation, chosen.value, chosen.valid) == ("select", "2", True)
    default = parse_conversation_control("/model DEFAULT 1")
    assert (default.operation, default.value, default.valid) == ("set_default", "1", True)
    assert parse_conversation_control("/model default").valid is False
    assert parse_conversation_control("/model 用 第二个").valid is False


def test_im_user_without_models_gets_guidance_instead_of_unsupported(tmp_path, monkeypatch):
    base, _admin = _gateway_setup(tmp_path, monkeypatch)
    base.config.model_backend = ""  # 部署配置没有模型，与真实飞书用户同样处境
    result = _run(base, "/model")
    assert result.ok is True and result.kind == "model"
    assert "还没有可选模型" in result.message and "管理员共享模型" in result.message
    assert "/model" in gateway_client_error_message("MODEL_NOT_CONFIGURED")


def test_im_user_selects_shared_model_for_this_conversation_without_secrets(tmp_path, monkeypatch):
    base, admin = _gateway_setup(tmp_path, monkeypatch)
    base.config.model_backend = ""
    shared, _ = add(admin, model_name="Shared-M2.7")
    add(admin, model_name="Admin-Private")
    set_shared_profile(admin, shared, True)

    listing = _run(base, "/model")
    assert "1. Shared-M2.7" in listing.message and "（管理员共享）" in listing.message
    assert "Admin-Private" not in listing.message
    assert "secret" not in listing.message and "example.test" not in listing.message

    chosen = _run(base, "/model 1")
    assert chosen.ok is True and "本会话已选择 Shared-M2.7" in chosen.message
    again = _run(base, "/model")
    assert "当前会话模型：Shared-M2.7" in again.message and "1. ● Shared-M2.7" in again.message
    assert "新会话默认：未设置" in again.message

    defaulted = _run(base, "/model default 1")
    assert defaulted.ok is True and "新会话默认使用 Shared-M2.7" in defaulted.message
    assert "新会话默认：Shared-M2.7" in _run(base, "/model").message

    missing = _run(base, "/model 9")
    assert missing.ok is False and "没有编号为 9" in missing.message
    assert "secret" not in json.dumps([listing.to_dict(), chosen.to_dict(), missing.to_dict()])


def test_selection_is_per_owner_and_other_users_are_unaffected(tmp_path, monkeypatch):
    base, admin = _gateway_setup(tmp_path, monkeypatch)
    base.config.model_backend = ""
    shared, _ = add(admin, model_name="Shared-M2.7")
    set_shared_profile(admin, shared, True)
    assert _run(base, "/model 1", user="ou_a").ok is True
    assert "当前会话模型：未选择" in _run(base, "/model", user="ou_b").message


def test_tui_keeps_bare_model_for_its_menu_and_serializes_text_forms():
    params = SimpleNamespace(use_gateway=True)
    assert tui_actions._tui_submit_control_operation(params, "/model") is False
    assert control_runtime._command_text(parse_conversation_control("/model 3")) == "/model 3"
    assert control_runtime._command_text(parse_conversation_control("/model default 3")) == "/model default 3"
    assert control_runtime._command_text(parse_conversation_control("/model")) == "/model"
