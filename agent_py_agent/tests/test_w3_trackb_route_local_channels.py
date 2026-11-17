"""Track B: /route true|false|status takes effect for TUI/CLI (chat channel) requests, not only IM."""
from __future__ import annotations

from agent_py_agent.agent.conversation.control_commands import parse_conversation_command
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.control_service import (
    GatewayControlScope,
    execute_gateway_conversation_control,
)
from agent_py_agent.agent.gateway_parts.im_dispatch_policy import (
    capture_gateway_input_context,
    capture_gateway_input_context_for_owner,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

_TUI = GatewayControlScope("local-agent", "chat", "session-route", resolved_owner=OwnerIdentity.local_main())


def _agent(tmp_path):
    return SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), enable_plugins=False),
                       tmp_path / "workspace")


def _route(agent, text, scope=_TUI):
    result = execute_gateway_conversation_control(agent, gateway_paths(agent), parse_conversation_command(text), scope)
    assert result.ok, result.message
    return {key: result.details[key] for key in ("enabled", "source", "revision")}


def _cold(agent, scope=_TUI):
    return capture_gateway_input_context_for_owner(home_paths=agent.home_paths, config=agent.config, scope=scope,
                                                   capability_root=agent.root)


def test_tui_route_true_freezes_a_delegated_snapshot_and_false_restores_plain_admission(tmp_path):
    agent = _agent(tmp_path)
    assert _cold(agent) == (None, None)
    assert capture_gateway_input_context(agent, _TUI) == (None, None)
    assert _route(agent, "/route true") == {"enabled": True, "source": "thread", "revision": 1}
    for dispatch, _space in (_cold(agent), capture_gateway_input_context(agent, _TUI)):
        assert (dispatch["mode"], dispatch["channel"], dispatch["route_mode_frozen"]) == ("delegated", "chat", True)
        assert dispatch["actor_id"] == "local-agent" and dispatch["preference_revision"] == 1
        assert dispatch["route_origin"]["thread_id"]
    assert _route(agent, "/route false") == {"enabled": False, "source": "thread", "revision": 2}
    assert _cold(agent) == (None, None) and capture_gateway_input_context(agent, _TUI) == (None, None)


def test_route_setting_survives_a_store_reload_and_stays_per_session(tmp_path):
    agent = _agent(tmp_path)
    _route(agent, "/route true")
    agent.conversation_store = ConversationStore(agent.conversation_store.storage.root)
    assert _route(agent, "/route status") == {"enabled": True, "source": "thread", "revision": 1}
    other = GatewayControlScope("local-agent", "chat", "session-other", resolved_owner=OwnerIdentity.local_main())
    assert _route(agent, "/route status", other) == {"enabled": False, "source": "default", "revision": 0}
    assert _cold(agent, other) == (None, None)
    assert _cold(agent)[0]["mode"] == "delegated"


def test_policy_without_subagents_freezes_route_but_keeps_original_mode(tmp_path):
    agent = _agent(tmp_path)
    _route(agent, "/route true")
    agent.config.enable_subagents = False
    dispatch, _space = _cold(agent)
    assert (dispatch["mode"], dispatch["route_mode_frozen"]) == ("original", True)
