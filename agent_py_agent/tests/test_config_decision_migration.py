# LLM: 验证 v6 决策默认源独立于旧平铺配置；只用临时内存输入，不访问真实 Home 或模型。
# 模块用途: 锁定决策默认值与旧配置脱钩的迁移合同。
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.settings.decision_settings_defaults import decision_defaults
from agent_py_agent.agent.settings.decision_settings_projection import decision_point_mode_from_read
from agent_py_agent.agent.settings.decision_settings_schema import empty_decision_settings


# LLM: 旧对象即使携带开启和超时字段，也不能继续成为新决策设置的默认权威。
# 函数用途: 验证正式默认读取不再接受旧对象的平铺值。
def test_decision_defaults_ignore_legacy_flat_config():
    context = SimpleNamespace(config=SimpleNamespace(decision_enabled=True, decision_timeout_seconds=99),
                              capability_config=SimpleNamespace(decision_subagent_model_mode="apply"))
    values, sources = decision_defaults(context)
    assert len(values) == 25
    assert values["enabled"] is False
    assert values["timeout_seconds"] == 3.0
    assert values["points.subagent_model.mode"] == "off"
    assert set(sources.values()) == {"decision_defaults"}


# LLM: 每次返回独立集合；调用方合并 owner/thread 覆盖不能改坏下一次请求的代码默认值。
# 函数用途: 验证列表默认没有跨请求共享引用。
def test_decision_defaults_return_independent_lists():
    values, _ = decision_defaults(SimpleNamespace())
    values["points.skill_tool.optional_categories"].append("changed")
    next_values, _ = decision_defaults(SimpleNamespace())
    assert next_values["points.skill_tool.optional_categories"] == ["plugins"]


# LLM: 零 I/O 开关入口沿同一 typed 默认；只有正式 owner/thread 覆盖可以开启，不能回落旧配置。
# 函数用途: 验证宿主快退投影和完整投影不再有两种默认来源。
def test_decision_fast_projection_ignores_legacy_flat_config():
    config = SimpleNamespace(decision_enabled=True, decision_model_selection_mode="apply")
    owner = empty_decision_settings()
    assert decision_point_mode_from_read(config, owner, None, point="model_selection") == "off"
    owner["overrides"] = {"enabled": True, "points.model_selection.mode": "observe"}
    assert decision_point_mode_from_read(config, owner, None, point="model_selection") == "observe"


# LLM: 退役设置只能显式转换进入原 catalog 覆盖，不把未填写默认补成用户配置，也不支持旧构造参数。
# 函数用途: 验证二十五个平铺字段退出运行 schema，离线迁移保留开关、列表及小数。
def test_decision_flat_fields_removed_and_explicit_migration_validates_values():
    from dataclasses import fields

    from agent_py_agent.agent.capability.config import CapabilityConfig
    from agent_py_agent.agent.settings.config import AgentConfig
    from agent_py_agent.agent.settings.model_credentials import (
        decision_config_fields,
        migrate_legacy_decision_overrides,
    )

    mapping = decision_config_fields()
    assert len(mapping) == 25
    assert not {key for domain, key in mapping.values() if domain != "capability"} & {item.name for item in fields(AgentConfig)}
    assert not {key for domain, key in mapping.values() if domain == "capability"} & {item.name for item in fields(CapabilityConfig)}
    result = migrate_legacy_decision_overrides(
        {"decision_enabled": True, "decision_timeout_seconds": "3.5"},
        {"decision_skill_tool_optional_categories": ["plugins"]},
    )
    assert result == {"enabled": True, "timeout_seconds": 3.5, "points.skill_tool.optional_categories": ["plugins"]}
    with pytest.raises(ValueError):
        migrate_legacy_decision_overrides({"decision_enabled": "off"}, {})
