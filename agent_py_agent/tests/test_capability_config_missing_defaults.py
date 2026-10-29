# LLM: 钉住 capability 配置"缺文件就用 dataclass 默认值"的统一入口语义，以及两个曾经读错对象的调用方。
#   生产 Gateway 的 agent 根目录是 owner home，那里通常没有 config/capability_config.yaml；旧实现返回 None，
#   各调用点再用各自的兜底值，派活链深和每对限流因此落到"不限制"。改入口语义时同步看会话工具的三组测试。
# 模块用途: 证明缺文件时拿到默认实例且不缓存、坏文件仍返回 None，决策设置读的是 capability 文件而不是路由器上的 AgentConfig。
from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.capability.runtime_config_reload import capability_config_for_agent
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.settings.decision_settings_defaults import decision_defaults

POLICY = "points.skill_tool.context_policy"


def test_missing_file_returns_the_default_instance_without_caching(tmp_path) -> None:
    """缺文件拿到默认实例；不写回快照缓存，之后建好文件时下一次调用就能读到，不用重启。"""
    config_path = tmp_path / "capability_config.yaml"
    agent = SimpleNamespace(capability_config_path=config_path)

    config = capability_config_for_agent(agent)

    assert config == CapabilityConfig()
    assert config.session_task_max_chain_depth == 4
    assert config.session_pair_hourly_limit == 60
    assert getattr(agent, "_capability_config_runtime_snapshot", None) is None

    config_path.write_text("session_pair_hourly_limit: 7\n", encoding="utf-8")
    assert capability_config_for_agent(agent).session_pair_hourly_limit == 7


def test_missing_file_under_an_owner_home_root_returns_defaults(tmp_path) -> None:
    """生产形态：只给 agent 根目录（owner home），下面没有 config/capability_config.yaml。"""
    owner_home = tmp_path / "owners" / "local" / "main"
    owner_home.mkdir(parents=True)

    assert capability_config_for_agent(SimpleNamespace(root=owner_home)) == CapabilityConfig()


# LLM: 真解码失败才是坏文件，已迁出的决策键不能冒充解析异常。
# 函数用途: 验证损坏的能力文件仍返回失败值。
def test_malformed_file_still_returns_none(tmp_path) -> None:
    """读取失败或格式错误保持原语义（None）；限流类调用方自己落到默认值，由会话工具测试覆盖。"""
    config_path = tmp_path / "capability_config.yaml"
    config_path.write_text("\xff", encoding="latin-1")

    assert capability_config_for_agent(SimpleNamespace(capability_config_path=config_path)) is None


def _decision_context(config_path) -> SimpleNamespace:
    # 与生产 SimpleAgent 同形：没有 capability_config 属性，路由器上挂的是 AgentConfig。
    return SimpleNamespace(config=AgentConfig(), capability_router=SimpleNamespace(config=AgentConfig()),
                           capability_config_path=config_path)


# LLM: 决策默认值唯一归决策代码；capability 文件里的旧值不能重新成为运行权威。
# 函数用途: 验证旧文件不会污染决策默认值。
def test_decision_defaults_read_the_capability_file_not_the_router_config(tmp_path) -> None:
    config_path = tmp_path / "capability_config.yaml"
    config_path.write_text("decision_skill_tool_context_policy: metadata\n", encoding="utf-8")

    values, sources = decision_defaults(_decision_context(config_path))

    assert values[POLICY] == "progressive"
    assert sources[POLICY] == "decision_defaults"


# LLM: 缺文件也不需要恢复已删除的 capability 决策字段，默认只归决策服务。
# 函数用途: 验证缺失文件时采用唯一决策默认值。
def test_decision_defaults_use_dataclass_defaults_when_the_file_is_missing(tmp_path) -> None:
    values, _sources = decision_defaults(_decision_context(tmp_path / "missing.yaml"))

    assert values[POLICY] == "progressive"
    assert not hasattr(CapabilityConfig(), "decision_skill_tool_context_policy")


# LLM: 注入旧形状对象同样没有配置权威，不得绕过迁移恢复旧键。
# 函数用途: 验证旧 capability 对象不改变决策默认值。
def test_decision_defaults_still_prefer_an_injected_capability_config(tmp_path) -> None:
    injected = SimpleNamespace(decision_skill_tool_context_policy="metadata")
    context = SimpleNamespace(config=AgentConfig(), capability_config=injected,
                              capability_config_path=tmp_path / "missing.yaml")

    values, _sources = decision_defaults(context)

    assert values[POLICY] == "progressive"
