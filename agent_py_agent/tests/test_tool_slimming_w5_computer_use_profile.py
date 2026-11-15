"""Computer Use profile tier & observation switch behavioral tests (isolated from server/transport spawning)."""

import asyncio
import json

from mcp import types
from mcp.server.fastmcp import FastMCP

from agent_py_agent.agent.plugin_observation import OBSERVATION_ERROR_KEY, OBSERVATION_KEY
from agent_py_agent.agent.tooling import computer_use_observation_tools as obs_glue
from agent_py_agent.agent.tooling.computer_use_profile import (
    COMPUTER_USE_MCP_SERVER_NAME,
    OBSERVATION_ENV_FLAG,
    OBSERVE_ONLY_ENV_FLAG,
    ComputerUseTier,
    computer_use_mcp_servers,
    with_computer_use_observation,
)


class _ObserverStub:
    def __init__(self, *, supports_ui_candidates: bool) -> None:
        self.supports_ui_candidates = supports_ui_candidates
        self.calls: list[str] = []

    def observe(self, window: str | None = None, *, want_screenshot: bool = False) -> dict[str, object]:
        self.calls.append("observe")
        return {
            "window": "win:1",
            "generation": "g-1",
            "candidate_count": 0,
            OBSERVATION_KEY: {
                "schema": "plugin_observation.v1",
                "target": {"ref": "win:1", "generation": "g-1"},
                "candidates": [],
            },
        }


def test_observation_switch_and_profile_tiers() -> None:
    full_servers = with_computer_use_observation(
        computer_use_mcp_servers(
            {},
            ComputerUseTier(
                enabled=True,
                is_local_admin=True,
                access_mode="full-access",
                observe_only=False,
                environ={"DISPLAY": ":99"},
                python_executable="/py",
            ),
        ),
        enabled=True,
    )
    full_profile = full_servers[COMPUTER_USE_MCP_SERVER_NAME]
    assert full_profile["env"][OBSERVATION_ENV_FLAG] == "1"
    assert OBSERVE_ONLY_ENV_FLAG not in full_profile["env"]
    assert {"observe_window", "click_candidate", "type_into_candidate"} <= set(full_profile["tool_effects"])

    full_obs_mcp = FastMCP("my-agent Computer Use Profile Full")
    low_full = obs_glue.build_adapter_server(
        full_obs_mcp,
        {OBSERVATION_ENV_FLAG: "1"},
        lambda: _ObserverStub(supports_ui_candidates=True),
    )
    listed_full = asyncio.run(
        low_full.request_handlers[types.ListToolsRequest](
            types.ListToolsRequest(method="tools/list")
        )
    )
    assert {"observe_window", "click_candidate", "type_into_candidate"} == {
        tool.name for tool in listed_full.root.tools
    }

    # Backend without UI candidate typing registers observe_window + click_candidate, omitting type_into_candidate.
    no_typing_mcp = FastMCP("my-agent Computer Use")
    low_no_typing = obs_glue.build_adapter_server(
        no_typing_mcp,
        {OBSERVATION_ENV_FLAG: "1"},
        lambda: _ObserverStub(supports_ui_candidates=False),
    )
    listed_no_typing = asyncio.run(
        low_no_typing.request_handlers[types.ListToolsRequest](
            types.ListToolsRequest(method="tools/list")
        )
    )
    assert [tool.name for tool in listed_no_typing.root.tools] == ["observe_window", "click_candidate"]

    # Observe-only tier from host-generated env loads zero upstream tools and blocks non-observe calls with 3-part facts.
    observe_only_servers = with_computer_use_observation(
        computer_use_mcp_servers(
            {},
            ComputerUseTier(
                enabled=False,
                is_local_admin=True,
                access_mode="full-access",
                observe_only=True,
                environ={"DISPLAY": ":99"},
                python_executable="/py",
            ),
        ),
        enabled=True,
    )
    obs_env = observe_only_servers[COMPUTER_USE_MCP_SERVER_NAME]["env"]
    assert obs_glue.observe_only_enabled(obs_env) is True
    assert obs_env[OBSERVE_ONLY_ENV_FLAG] == "1"
    assert OBSERVATION_ENV_FLAG not in obs_env
    obs_mcp = FastMCP("my-agent Computer Use")
    observer_obs_only = _ObserverStub(supports_ui_candidates=True)
    low_obs = obs_glue.build_adapter_server(obs_mcp, obs_env, lambda: observer_obs_only)

    async def _verify_observe_only() -> None:
        listed_obs = await low_obs.request_handlers[types.ListToolsRequest](
            types.ListToolsRequest(method="tools/list")
        )
        assert [tool.name for tool in listed_obs.root.tools] == ["observe_window"]
        obs_call = low_obs.request_handlers[types.CallToolRequest]
        denied = await obs_call(
            types.CallToolRequest(
                method="tools/call",
                params=types.CallToolRequestParams(
                    name="click_candidate",
                    arguments={"candidate_id": "cand-1"},
                ),
            )
        )
        assert denied.root.isError is True
        assert denied.root.structuredContent == {
            OBSERVATION_ERROR_KEY: {"code": "tool_not_available_in_observe_only"}
        }
        denied_body = json.loads(denied.root.content[0].text)
        assert denied_body["code"] == "OBSERVATION_TOOL_NOT_AVAILABLE_IN_OBSERVE_ONLY"
        assert denied_body["effect_outcome"] == "not_started"
        assert denied_body["side_effect"] == "none"
        assert "observe_window" in denied_body["next_action"]
        assert observer_obs_only.calls == []

    asyncio.run(_verify_observe_only())
