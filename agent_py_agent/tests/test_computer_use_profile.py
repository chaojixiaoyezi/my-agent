"""Computer Use profile eligibility, packaging, environment, and effect contracts."""

from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.settings.config import AgentConfig, load_config, normalize_agent_config
from agent_py_agent.agent.tooling.computer_use_profile import (
    COMPUTER_USE_MCP_SERVER_NAME,
    computer_use_mcp_servers,
)


def test_computer_use_is_disabled_by_default_and_reserved_name_cannot_bypass() -> None:
    configured = {
        "normal": {"command": "normal-server"},
        COMPUTER_USE_MCP_SERVER_NAME: {"command": "untrusted-override"},
    }

    servers = computer_use_mcp_servers(
        configured,
        enabled=False,
        is_local_admin=True,
        access_mode="full-access",
    )

    assert servers == {"normal": {"command": "normal-server"}}
    assert configured[COMPUTER_USE_MCP_SERVER_NAME]["command"] == "untrusted-override"


def test_computer_use_requires_both_local_admin_and_full_access() -> None:
    for is_local_admin, access_mode in (
        (False, "full-access"),
        (True, "workspace-write"),
        (True, "restricted"),
    ):
        servers = computer_use_mcp_servers(
            {},
            enabled=True,
            is_local_admin=is_local_admin,
            access_mode=access_mode,
        )
        assert COMPUTER_USE_MCP_SERVER_NAME not in servers


def test_authorized_profile_uses_current_python_gui_session_and_safe_effects() -> None:
    servers = computer_use_mcp_servers(
        {"normal": {"command": "normal-server"}},
        enabled=True,
        is_local_admin=True,
        access_mode="full-access",
        environ={
            "DISPLAY": ":99",
            "WAYLAND_DISPLAY": "wayland-1",
            "API_KEY": "must-not-leak",
        },
        python_executable="/runtime/python",
    )

    profile = servers[COMPUTER_USE_MCP_SERVER_NAME]
    assert servers["normal"] == {"command": "normal-server"}
    assert profile["command"] == "/runtime/python"
    assert profile["args"] == [
        "-m",
        "agent_py_agent.agent.tooling.computer_use_server",
    ]
    assert profile["env"] == {
        "DISPLAY": ":99",
        "WAYLAND_DISPLAY": "wayland-1",
        "ENV": "development",
    }
    assert profile["default_effect"] == "dangerous"
    assert profile["catalog_category"] == "computer_use"
    assert profile["tool_effects"] == {
        "get_screen_size": "read_only",
        "list_windows": "read_only",
        "wait_milliseconds": "read_only",
        "move_mouse": "mutating",
        "activate_window": "mutating",
        "scroll_screen": "mutating",
    }
    assert profile["timeout"] >= 120
    assert profile["max_line_chars"] >= 8 * 1024 * 1024


def test_computer_use_config_normalizes_boolean_and_shipped_default_matches() -> None:
    normalized, warnings = normalize_agent_config({"computer_use_enabled": "yes"})
    assert normalized["computer_use_enabled"] is True
    assert not [warning for warning in warnings if "computer_use_enabled" in str(warning)]

    shipped = load_config(
        Path(__file__).resolve().parents[1] / "config" / "agent_config.yaml"
    )
    assert shipped.computer_use_enabled is AgentConfig().computer_use_enabled is False
