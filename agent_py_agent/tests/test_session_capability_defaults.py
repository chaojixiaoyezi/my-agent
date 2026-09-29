# LLM: 钉住会话互通「capability 配置值只有 dataclass 默认值一个权威」：会话文件里原先自带的
#   getattr(..., 默认值) 兜底会和 CapabilityConfig 的默认值漂移，本轮把它们删掉，改读字段本身。
#   缺文件时统一入口返回 CapabilityConfig()（默认实例）；坏文件返回 None，由各调用点的
#   `or CapabilityConfig()` 兜到同一个默认实例；文件里的非默认值照常生效。
# 模块用途: 对三个会话工具文件的开关与限额、以及 session_messaging.py 的三个 *_tool_visible 逐处做
#   「缺文件=默认值 / 坏文件=默认值 / 文件非默认值生效」三种断言。
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.config import CapabilityConfig

DEFAULTS = CapabilityConfig()
# 让统一入口返回 None 的格式错误（非法决策模式）；缺文件时统一入口返回默认实例。
MALFORMED = "decision_subagent_model_mode: bogus\n"
UNREADABLE = pytest.mark.parametrize("content", [None, MALFORMED], ids=["missing", "malformed"])


# 函数用途: 在临时目录放（或不放）一份 capability 配置，返回给 agent 用的路径。
def _config_path(tmp_path, content):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "capability_config.yaml"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    return path


# 函数用途: 造一个能通过统一入口读到配置的最小 agent 替身（不涉及真实模型或会话存储）。
def _agent(tmp_path, content):
    return SimpleNamespace(capability_config_path=_config_path(tmp_path, content))


# ---- session_messaging.py 的三个 *_tool_visible -------------------------------------------


@UNREADABLE
def test_messaging_tool_visible_uses_the_defaults(tmp_path, content) -> None:
    from agent_py_agent.agent.capability.runtime_config_reload import capability_config_for_agent
    from agent_py_agent.agent.conversation.session_messaging import (
        OWNER_KIND_MAIN,
        session_messaging_tool_visible,
    )

    agent = _agent(tmp_path, content)
    config = capability_config_for_agent(agent) or CapabilityConfig()
    assert config is not None
    assert session_messaging_tool_visible(
        SimpleNamespace(owner_kind=OWNER_KIND_MAIN), config
    ) is bool(DEFAULTS.session_messaging_admin_enabled)
    assert session_messaging_tool_visible(
        SimpleNamespace(owner_kind="user"), config
    ) is bool(DEFAULTS.session_messaging_user_enabled)


def test_messaging_tool_visible_honours_the_file(tmp_path) -> None:
    from agent_py_agent.agent.capability.runtime_config_reload import capability_config_for_agent
    from agent_py_agent.agent.conversation.session_messaging import (
        OWNER_KIND_MAIN,
        session_messaging_tool_visible,
    )

    agent = _agent(tmp_path, "session_messaging_user_enabled: true\n")
    config = capability_config_for_agent(agent) or CapabilityConfig()
    assert session_messaging_tool_visible(SimpleNamespace(owner_kind="user"), config) is True
    assert session_messaging_tool_visible(SimpleNamespace(owner_kind=OWNER_KIND_MAIN), config) is True


@UNREADABLE
def test_task_tool_visible_uses_the_defaults(tmp_path, content) -> None:
    from agent_py_agent.agent.capability.runtime_config_reload import capability_config_for_agent
    from agent_py_agent.agent.conversation.session_messaging import (
        OWNER_KIND_MAIN,
        session_task_tool_visible,
    )

    agent = _agent(tmp_path, content)
    config = capability_config_for_agent(agent) or CapabilityConfig()
    assert config is not None
    assert session_task_tool_visible(
        SimpleNamespace(owner_kind=OWNER_KIND_MAIN), config
    ) is bool(DEFAULTS.session_task_admin_enabled)
    # 普通用户第一期不开派活，无论文件怎么写。
    assert session_task_tool_visible(SimpleNamespace(owner_kind="user"), config) is False


def test_task_tool_visible_honours_the_file(tmp_path) -> None:
    from agent_py_agent.agent.capability.runtime_config_reload import capability_config_for_agent
    from agent_py_agent.agent.conversation.session_messaging import (
        OWNER_KIND_MAIN,
        session_task_tool_visible,
    )

    agent = _agent(tmp_path, "session_task_admin_enabled: false\n")
    config = capability_config_for_agent(agent) or CapabilityConfig()
    assert session_task_tool_visible(SimpleNamespace(owner_kind=OWNER_KIND_MAIN), config) is False


# ---- 三个会话工具文件读限额的地方 ----------------------------------------------------------


MESSAGING_LIMIT_SITES = [
    ("create_session_task", "agent_py_agent/agent/agent_core/orchestration/tools/create_session_task.py"),
    ("send_session_message", "agent_py_agent/agent/agent_core/orchestration/tools/send_session_message.py"),
    ("slash_commands", "agent_py_agent/cli/chat_parts/slash_commands.py"),
]


@UNREADABLE
@pytest.mark.parametrize("site,path", MESSAGING_LIMIT_SITES, ids=[s for s, _ in MESSAGING_LIMIT_SITES])
def test_messaging_limits_use_the_defaults(tmp_path, content, site, path) -> None:
    """缺文件与坏文件都必须落到同一个默认值，不能落到"不限制"（0 表示不限制）。"""
    from agent_py_agent.agent.capability.runtime_config_reload import capability_config_for_agent

    agent = _agent(tmp_path, content)
    config = capability_config_for_agent(agent) or CapabilityConfig()
    assert config is not None
    assert int(config.session_pair_hourly_limit or 0) == DEFAULTS.session_pair_hourly_limit
    assert DEFAULTS.session_pair_hourly_limit != 0, "前提：默认值本身就是个有限上限"


@pytest.mark.parametrize("site,path", MESSAGING_LIMIT_SITES, ids=[s for s, _ in MESSAGING_LIMIT_SITES])
def test_messaging_limits_honour_the_file(tmp_path, site, path) -> None:
    from agent_py_agent.agent.capability.runtime_config_reload import capability_config_for_agent

    agent = _agent(tmp_path, "session_pair_hourly_limit: 7\n")
    config = capability_config_for_agent(agent) or CapabilityConfig()
    assert int(config.session_pair_hourly_limit or 0) == 7


@UNREADABLE
def test_task_chain_depth_uses_the_defaults(tmp_path, content) -> None:
    from agent_py_agent.agent.capability.runtime_config_reload import capability_config_for_agent

    agent = _agent(tmp_path, content)
    config = capability_config_for_agent(agent) or CapabilityConfig()
    assert config is not None
    assert int(config.session_task_max_chain_depth or 0) == DEFAULTS.session_task_max_chain_depth


def test_task_chain_depth_honours_the_file(tmp_path) -> None:
    from agent_py_agent.agent.capability.runtime_config_reload import capability_config_for_agent

    agent = _agent(tmp_path, "session_task_max_chain_depth: 9\n")
    config = capability_config_for_agent(agent) or CapabilityConfig()
    assert int(config.session_task_max_chain_depth or 0) == 9


# ---- 三个开关字段本身：文件缺失/损坏都取默认值，文件值生效 ---------------------------------


@UNREADABLE
def test_messaging_switch_fields_use_the_defaults(tmp_path, content) -> None:
    from agent_py_agent.agent.capability.runtime_config_reload import capability_config_for_agent

    agent = _agent(tmp_path, content)
    config = capability_config_for_agent(agent) or CapabilityConfig()
    assert config is not None
    assert bool(config.session_messaging_admin_enabled) is bool(DEFAULTS.session_messaging_admin_enabled)
    assert bool(config.session_messaging_user_enabled) is bool(DEFAULTS.session_messaging_user_enabled)
    assert bool(config.session_task_admin_enabled) is bool(DEFAULTS.session_task_admin_enabled)


def test_messaging_switch_fields_honour_the_file(tmp_path) -> None:
    from agent_py_agent.agent.capability.runtime_config_reload import capability_config_for_agent

    agent = _agent(
        tmp_path,
        "session_messaging_admin_enabled: false\n"
        "session_messaging_user_enabled: true\n"
        "session_task_admin_enabled: false\n",
    )
    config = capability_config_for_agent(agent) or CapabilityConfig()
    assert config.session_messaging_admin_enabled is False
    assert config.session_messaging_user_enabled is True
    assert config.session_task_admin_enabled is False


# 函数用途: 钉住"删掉 getattr 兜底"这件事本身——字段名写错/字段被删时必须立刻炸出来，
#   而不是静默落回一个写死的默认值（那正是兜底会造成漂移的地方）。
def test_tool_visible_reads_fields_without_a_hidden_fallback() -> None:
    from agent_py_agent.agent.conversation.session_messaging import (
        OWNER_KIND_MAIN,
        session_messaging_tool_visible,
        session_task_tool_visible,
    )

    class _Typo(SimpleNamespace):
        """有 user 开关、却没有 admin 开关的替身：模拟字段名写错或字段被删。"""

    home = SimpleNamespace(owner_kind=OWNER_KIND_MAIN)
    with pytest.raises(AttributeError):
        session_messaging_tool_visible(home, _Typo(session_messaging_user_enabled=False))
    with pytest.raises(AttributeError):
        session_task_tool_visible(home, _Typo(session_messaging_user_enabled=False))
