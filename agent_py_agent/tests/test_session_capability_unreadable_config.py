"""主代理构造时 capability 配置读不了或有非法值也不能崩（ae 复审必须改 2）。

`capability_config_for_agent` 在三种情况下返回 None：文件读不了（没有读权限、路径是目录），以及内容里有非法值
（决策设置校验抛 ValueError）。缺文件返回默认实例，未知字段只告警。注册会话工具时若直接把返回值传下去，
这几种 owner 一构造主代理就崩：`AttributeError: 'NoneType' object has no attribute 'session_messaging_admin_enabled'`。

修法：core.py 算一次 `capability_config_for_agent(agent) or CapabilityConfig()`，三个会话工具共用。
这里真构造 SimpleAgent：去掉 core.py 的兜底，这组测试必须变红。
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.runtime_config_reload import capability_config_for_agent
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig

# 会话工具三类：会话消息、会话派活（三个工具）、列本 owner 会话；可见性都读 capability 开关。
_SESSION_TOOLS = frozenset({
    "send_session_message", "create_session_task", "get_session_task", "cancel_session_task", "list_owner_sessions",
})


# 函数用途: 写一份没有读权限的配置文件；root 能读 0 权限文件，这个情形在 root 下不成立，调用方需 skip。
def _unreadable_file(config_dir):
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "capability_config.yaml"
    path.write_text("session_messaging_admin_enabled: true\n", encoding="utf-8")
    os.chmod(path, 0o000)
    return path


# 函数用途: 让配置路径是一个目录（读文件时报 IsADirectoryError）。
def _directory_path(config_dir):
    path = config_dir / "capability_config.yaml"
    path.mkdir(parents=True, exist_ok=True)
    return path


# 函数用途: 写一份带非法决策设置的配置（校验抛 ValueError），文件本身读得出来。
def _invalid_value(config_dir):
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "capability_config.yaml"
    path.write_text("decision_skill_tool_mode: not-a-mode\n", encoding="utf-8")
    return path


_CASES = pytest.mark.parametrize(
    "make", [_unreadable_file, _directory_path, _invalid_value],
    ids=["no-read-permission", "is-a-directory", "invalid-value"],
)


@pytest.fixture
def restore_permissions(tmp_path):
    yield
    for child in tmp_path.rglob("*"):
        if child.is_file():
            os.chmod(child, 0o600)


# 函数用途: 按默认配置路径（<root>/config/capability_config.yaml）造一份坏配置，返回 root。
def _root_with(tmp_path, make):
    if make is _unreadable_file and hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root 能读 0 权限文件，这个情形在 root 下不成立")
    root = tmp_path / "root"
    make(root / "config")
    return root


# 函数用途: 真构造一个主代理，返回它注册的会话工具名。
def _session_tools(root):
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), root)
    # 会话工具属于 orchestration 类，specs() 默认不列；直接看注册表里登记了哪些。
    return set(agent.tools.tools) & _SESSION_TOOLS, agent


@_CASES
def test_unreadable_config_makes_the_lookup_return_none(tmp_path, make, restore_permissions) -> None:
    """前提：这三种情形下统一入口确实返回 None——所以调用点必须自己兜默认实例。"""
    root = _root_with(tmp_path, make)
    path = root / "config" / "capability_config.yaml"
    assert capability_config_for_agent(SimpleNamespace(capability_config_path=str(path), root=str(root))) is None


@_CASES
def test_agent_construction_survives_a_bad_capability_config(tmp_path, make, restore_permissions) -> None:
    """真构造 SimpleAgent：不崩，会话工具按默认实例注册，和没有配置文件时一致。"""
    root = _root_with(tmp_path, make)
    tools, agent = _session_tools(root)
    assert capability_config_for_agent(agent) is None, "前提不成立：坏配置没有让统一入口返回 None"
    expected, _baseline = _session_tools(tmp_path / "clean-root")
    assert expected, "前提不成立：默认配置下本机管理员应当注册会话工具"
    assert tools == expected
