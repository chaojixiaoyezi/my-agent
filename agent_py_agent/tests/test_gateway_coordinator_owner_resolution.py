"""无业务 Agent 的 C 控制面必须复用主线 owner 规范、私聊门与冻结重放。"""
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import request_worker
from agent_py_agent.agent.gateway_parts.request_owner_binding import REQUEST_OWNER_KEY
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.admin_channel_identity import bind_admin_channel_identity
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity


@pytest.fixture
def context(tmp_path, monkeypatch):
    root = (tmp_path / "home").resolve()
    monkeypatch.setenv("MY_AGENT_HOME", str(root))
    assert root != (Path.home() / ".my-agent").resolve()
    config = AgentConfig(my_agent_home=str(root), gateway_per_user_owner_scoping=True,
                         admin_channel_identity_enabled=True)
    return SimpleNamespace(config=config, home_paths=home_paths(str(root)))


@pytest.mark.parametrize("channel,user_id,chat_type,chat_id,owner", [
    ("local", "fixture-alice", "", "", OwnerIdentity.provider_user("local", "fixture-alice")),
    ("feishu", "fixture-alice", "p2p", "", OwnerIdentity.provider_user("feishu", "fixture-alice")),
    ("feishu", "fixture-alice", "group", "fixture-room", OwnerIdentity.provider_group("feishu", "fixture-room")),
    ("local", "local-agent", "", "", OwnerIdentity.local_main()),
])
def test_config_only_c_resolver_preserves_current_owner_rules(context, channel, user_id, chat_type, chat_id, owner):
    request = {"user_id": user_id, "metadata": {"channel": channel, "channel_chat_type": chat_type,
                                               "channel_chat_id": chat_id}}
    resolved = request_worker._resolve_request_owner_from_config(context.config, request, context.home_paths.root)
    assert resolved == owner
    assert request_worker._resolve_request_owner_identity(context, request) == owner
    assert not context.home_paths.root.exists()


def test_config_only_resolver_uses_real_exact_private_admin_binding(context):
    bind_admin_channel_identity(context.home_paths.root, "feishu", "fixture-alice", now=1.0)
    request = {"user_id": "fixture-alice", "metadata": {"channel": "feishu", "channel_chat_type": "p2p"}}
    assert request_worker._resolve_request_owner_from_config(context.config, request, context.home_paths.root) == OwnerIdentity.local_main()
    request["metadata"].update(channel_chat_type="group", channel_chat_id="fixture-room")
    assert request_worker._resolve_request_owner_from_config(context.config, request, context.home_paths.root) == OwnerIdentity.provider_group("feishu", "fixture-room")


def test_frozen_owner_precedes_new_config_and_admin_reads(context, monkeypatch):
    from agent_py_agent.agent.user_space import admin_channel_identity

    def unexpected(*_args, **_kwargs):
        raise AssertionError("冻结请求不得重新读绑定表")

    monkeypatch.setattr(admin_channel_identity, "read_private_config_json", unexpected)
    context.config.gateway_per_user_owner_scoping = False
    owner = OwnerIdentity.provider_user("feishu", "fixture-alice")
    request = {REQUEST_OWNER_KEY: asdict(owner), "user_id": "other-user",
               "metadata": {"channel": "feishu", "channel_chat_type": "p2p"}}
    assert request_worker._resolve_request_owner_from_config(context.config, request, context.home_paths.root) == owner
    assert request_worker._resolve_request_owner_identity(context, request) == owner
    assert not context.home_paths.root.exists()


def test_config_only_unknown_remote_actor_does_not_fall_back_main(context):
    request = {"metadata": {"channel": "feishu", "channel_chat_type": "p2p"}}
    with pytest.raises(request_worker.OwnerScopeUnavailableError):
        request_worker._resolve_request_owner_from_config(context.config, request, context.home_paths.root)
    assert not context.home_paths.root.exists()
