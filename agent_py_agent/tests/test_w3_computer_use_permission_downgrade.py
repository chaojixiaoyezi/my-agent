"""computer_use follows the owner's current /permissions choice at each run, not the mode the agent was built with.

Live clone (2026-11-17): after the local admin switched from Full Access to 默认确认, move_mouse still ran twice
without a prompt.  The MCP server provider was bound to the access mode resolved when the owner agent was built,
and /permissions only rewrites the owner tool policy file, so the computer_use server stayed mounted.
"""
from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

from agent_py_agent.agent import core
from agent_py_agent.agent.settings import AgentConfig

SERVER = 'computer_use'


def _admin(tmp_path, mode):
    policy = tmp_path / 'owners' / 'local' / 'main' / 'config' / 'tool_policy.json'
    policy.parent.mkdir(parents=True, exist_ok=True)
    if mode is not None:
        policy.write_text(json.dumps({'schema_version': 'tool-policy.v1', 'permission_mode': mode}))
    home = SimpleNamespace(owner_provider='local', owner_kind='main', owner_home_dir=str(policy.parent.parent),
                           owner_tool_policy_json=str(policy))
    return SimpleNamespace(home_paths=home, owner_policy=None), policy


def _config(tmp_path, access_mode):
    return replace(AgentConfig(model_backend='echo', my_agent_home=str(tmp_path / 'home'), computer_use_enabled=True),
                   access_mode=access_mode, path_access_mode='full' if access_mode == 'full-access' else 'normal')


def test_downgrade_from_full_access_unmounts_computer_use_at_the_next_run(tmp_path):
    agent, policy = _admin(tmp_path, 'full-access')
    config = _config(tmp_path, 'full-access')
    assert SERVER in core._tool_registry_mcp_servers(agent, config, 'full-access')
    policy.write_text(json.dumps({'schema_version': 'tool-policy.v1', 'permission_mode': 'ask'}))
    assert SERVER not in core._tool_registry_mcp_servers(agent, config, 'full-access')
    policy.write_text(json.dumps({'schema_version': 'tool-policy.v1', 'permission_mode': 'auto'}))
    assert SERVER not in core._tool_registry_mcp_servers(agent, config, 'full-access')


def test_upgrade_to_full_access_mounts_computer_use_at_the_next_run(tmp_path):
    agent, _policy = _admin(tmp_path, 'full-access')
    config = _config(tmp_path, 'workspace-write')
    assert SERVER in core._tool_registry_mcp_servers(agent, config, 'workspace-write')


def test_unreadable_policy_keeps_computer_use_unmounted(tmp_path):
    agent, policy = _admin(tmp_path, 'full-access')
    policy.write_text('{not json')
    assert SERVER not in core._tool_registry_mcp_servers(agent, _config(tmp_path, 'full-access'), 'full-access')


def test_without_an_explicit_choice_the_built_mode_is_kept(tmp_path):
    agent, _policy = _admin(tmp_path, None)
    config = _config(tmp_path, 'full-access')
    assert SERVER in core._tool_registry_mcp_servers(agent, config, 'full-access')
    assert SERVER not in core._tool_registry_mcp_servers(agent, _config(tmp_path, 'workspace-write'), 'workspace-write')
