"""S12: full host paths in the bound admin's private chat follow the admin's effective Full Access permission mode."""
from __future__ import annotations

from dataclasses import replace

import pytest

from agent_py_agent.agent.conversation.channels import project_host_paths_for_channel
from agent_py_agent.agent.gateway_parts.request_worker import admin_private_host_paths
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space.admin_channel_identity import bind_admin_channel_identity
from agent_py_agent.agent.user_space.approval_mode import execute_approval_mode_operation
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    resolve_owner_home,
)

_PATH = "/Users/alice/projects/heron/src/lexer.py"


def _config():
    return replace(AgentConfig(), admin_channel_identity_enabled=True, gateway_per_user_owner_scoping=True,
                   access_mode="full-access", my_agent_owner_provider="local", my_agent_owner_kind="main",
                   my_agent_owner_id="main")


def _payload():
    return {"user_id": "ou_admin", "metadata": {"user_id": "ou_admin", "channel": "feishu",
                                                "channel_chat_type": "p2p", "host_path_display": "basename"}}


def _main_home(root):
    return home_paths_with_owner(home_paths(root), resolve_owner_home(root, OwnerIdentity.local_main()))


# 函数用途: config.yaml 写 full-access，但管理员用 /permissions 选默认确认或自主工作时，回合工具已按 workspace-write 运行，
#   S12 受理也必须按 basename 冻结；未显式选择时沿用配置，选 Full Access 时保留完整路径。
@pytest.mark.parametrize(("mode", "expected"), [(None, True), ("full-access", True), ("ask", False), ("auto", False)])
def test_admin_private_paths_follow_the_admin_permission_mode(tmp_path, mode, expected):
    bind_admin_channel_identity(tmp_path, "feishu", "ou_admin")
    if mode is not None:
        execute_approval_mode_operation(_main_home(tmp_path), "set", mode)
    flag = admin_private_host_paths(_config(), tmp_path, _payload())
    assert flag is expected
    shown = project_host_paths_for_channel(f"已改 {_PATH}", "feishu", preserve_host_paths=flag)
    assert (_PATH in shown) is expected and "lexer.py" in shown


# 函数用途: 管理员权限配置读不出时按 basename 冻结，不让受理因读取失败报错，也不放宽展示。
def test_unreadable_admin_permission_mode_keeps_basenames(tmp_path):
    bind_admin_channel_identity(tmp_path, "feishu", "ou_admin")
    policy = _main_home(tmp_path).owner_tool_policy_json
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text("{not json")
    assert admin_private_host_paths(_config(), tmp_path, _payload()) is False
