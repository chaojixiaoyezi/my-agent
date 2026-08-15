from __future__ import annotations

"""MY_AGENT_HOME 环境变量优先于配置值的回归测试（2026-08-15 VM 真机发现）。"""

import os

import pytest

from agent_py_agent.agent.core import _configured_home_root
from agent_py_agent.agent.settings.config import AgentConfig


# LLM: 配置 yaml 的 my_agent_home 默认非空 (~/.my-agent)，若"配置优先"会遮蔽环境变量隔离通道；
# 该回归钉死"env 优先、config 兜底"的优先级语义，防止后续改动回退。
# 函数用途: 验证 MY_AGENT_HOME 环境变量优先于 AgentConfig.my_agent_home。
def test_env_home_wins_over_config_value(monkeypatch):
    monkeypatch.setenv("MY_AGENT_HOME", "/tmp/env-priority-home")
    config = AgentConfig(my_agent_home="~/.my-agent")
    assert _configured_home_root(config) == "/tmp/env-priority-home"


# LLM: 环境变量为空时回退配置值，保持原有"显式配置可用"路径不变。
# 函数用途: 验证未设置环境变量时配置值生效。
def test_config_value_used_when_env_absent(monkeypatch):
    monkeypatch.delenv("MY_AGENT_HOME", raising=False)
    config = AgentConfig(my_agent_home="/tmp/config-home")
    assert _configured_home_root(config) == "/tmp/config-home"


# LLM: 环境变量与配置都为空时返回 None（调用方走默认 ~/.my-agent），行为与修复前一致。
# 函数用途: 验证全空时返回 None 的兜底路径。
def test_none_when_both_empty(monkeypatch):
    monkeypatch.delenv("MY_AGENT_HOME", raising=False)
    config = AgentConfig(my_agent_home="")
    assert _configured_home_root(config) is None
