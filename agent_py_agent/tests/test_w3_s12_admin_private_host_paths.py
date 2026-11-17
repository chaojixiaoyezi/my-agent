"""S12: a verified admin's Feishu private chat on a full-access local/main keeps full host paths; other IM views keep basenames."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.channels import project_host_paths_for_channel
from agent_py_agent.agent.conversation.im_tool_approval import approval_card
from agent_py_agent.agent.gateway_parts.input_delivery_service import read_gateway_input_receipt
from agent_py_agent.agent.gateway_parts.request_worker import admin_private_host_paths
from agent_py_agent.agent.user_space import admin_channel_identity
from agent_py_agent.agent.user_space.admin_channel_identity import (
    bind_admin_channel_identity,
    read_admin_channel_identity_snapshot,
)
from agent_py_agent.tests.test_gateway_http_prepare_boundary import (  # noqa: F401
    _body,
    _submit,
    server,
)

_PATH = "/Users/alice/projects/heron/src/lexer.py"


def _config(access="full-access", *, enabled=True):
    return SimpleNamespace(gateway_per_user_owner_scoping=True, admin_channel_identity_enabled=enabled,
                           my_agent_owner_provider="local", my_agent_owner_kind="main", my_agent_owner_id="main",
                           access_mode=access)


def _payload(user="ou_admin", chat="p2p", display="basename"):
    return {"user_id": user, "metadata": {"user_id": user, "channel": "feishu", "channel_chat_type": chat,
                                          "host_path_display": display}}


@pytest.mark.parametrize(("user", "chat", "access", "enabled", "display", "expected"), [
    ("ou_admin", "p2p", "full-access", True, "basename", True),
    ("ou_admin", "group", "full-access", True, "basename", False),
    ("ou_other", "p2p", "full-access", True, "basename", False),
    ("ou_admin", "p2p", "workspace-write", True, "basename", False),
    ("ou_admin", "", "full-access", True, "basename", False),
    ("ou_admin", "private", "full-access", True, "basename", False),
    ("ou_admin", "p2p", "full-access", False, "basename", False),
    ("ou_admin", "p2p", "full-access", True, "", False),
])
def test_admin_private_full_access_matrix(tmp_path, user, chat, access, enabled, display, expected):
    bind_admin_channel_identity(tmp_path, "feishu", "ou_admin")
    flag = admin_private_host_paths(_config(access, enabled=enabled), tmp_path, _payload(user, chat, display))
    assert flag is expected
    shown = project_host_paths_for_channel(f"已改 {_PATH}", "feishu", preserve_host_paths=flag)
    assert (_PATH in shown) is expected and "lexer.py" in shown


def test_c_snapshot_answers_without_another_binding_read(tmp_path, monkeypatch):
    bind_admin_channel_identity(tmp_path, "feishu", "ou_admin")
    snapshot = read_admin_channel_identity_snapshot(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("C 受理已读过绑定快照，不能再读文件")

    monkeypatch.setattr(admin_channel_identity, "find_admin_channel_identity", forbidden)
    assert admin_private_host_paths(_config(), tmp_path, _payload(), binding_snapshot=snapshot) is True
    assert admin_private_host_paths(_config(), tmp_path, _payload("ou_other"), binding_snapshot=snapshot) is False


@pytest.mark.parametrize(("user", "chat", "expected"), [
    ("ou_admin", "p2p", "full"), ("ou_admin", "group", "basename"), ("ou_other", "p2p", "basename"),
])
def test_fresh_capture_freezes_the_display_and_replay_never_rereads(server, monkeypatch, user, chat, expected):  # noqa: F811
    config = server.agent.config
    config.admin_channel_identity_enabled, config.access_mode = True, "full-access"
    bind_admin_channel_identity(server.agent.home_paths.root, "feishu", "ou_admin")
    body = _body()
    body["metadata"]["channel_chat_type"] = chat
    (status, _result), request_id = _submit(server, body, channel="feishu", user_id=user)
    assert status == 202
    prepared = read_gateway_input_receipt(server.paths, request_id).prepared_request
    assert prepared["metadata"]["host_path_display"] == expected

    def forbidden(*_args, **_kwargs):
        raise AssertionError("已有回执的重放不能再读管理员绑定")

    monkeypatch.setattr(admin_channel_identity, "read_private_config_json", forbidden)
    assert _submit(server, body, channel="feishu", user_id=user)[0][0] == 202
    assert read_gateway_input_receipt(server.paths, request_id).prepared_request == prepared


# 函数用途: 生产多进程网关走 C 受理：用 prepare_input 同一次绑定快照冻结 full，且不物化业务 Agent。
def test_c_capture_freezes_full_paths_for_the_bound_admin(server, monkeypatch):  # noqa: F811
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts.control_plane import GatewayControlPlane
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
    from agent_py_agent.agent.gateway_parts.multiprocess_takeover_runtime import (
        prepare_coordinator_takeover,
    )

    config, constructor_root = server.agent.config, server.agent.root
    config.admin_channel_identity_enabled, config.access_mode = True, "full-access"
    bind_admin_channel_identity(server.agent.home_paths.root, "feishu", "ou_admin")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("C受理不能物化业务Agent")

    monkeypatch.setattr(SimpleAgent, "__init__", forbidden)
    with coordinator_guard(server.paths.root, 2):
        prepare_coordinator_takeover(server.paths, 2, "fixture-c")
        control = GatewayControlPlane(config, server.agent.home_paths.root, server.paths, 2, "fixture-c",
                                      agent_root=constructor_root)
        server.agent, server.control_plane = None, control
        displays = {}
        for user in ("ou_admin", "ou_other"):
            body = _body()
            body["metadata"].update({"channel_chat_type": "p2p", "message_id": f"m-{user}"})
            (status, _result), request_id = _submit(server, body, channel="feishu", user_id=user)
            assert status == 202
            displays[user] = read_gateway_input_receipt(server.paths, request_id).prepared_request["metadata"]
    assert displays["ou_admin"]["host_path_display"] == "full"
    assert displays["ou_admin"]["gateway_admin_binding_version"] != "none"
    assert displays["ou_other"]["host_path_display"] == "basename"


def test_approval_card_keeps_full_paths_only_when_asked():
    request = SimpleNamespace(description=f"rm -r {_PATH}", tool_name="run_command")
    scope = SimpleNamespace(agent_name="my-agent")
    _card, hidden = approval_card("ref", request, scope)
    _card, shown = approval_card("ref", request, scope, preserve_host_paths=True)
    assert _PATH not in hidden and "lexer.py" in hidden and _PATH in shown
