# LLM: 原配置解析、回合策略和管理员强制共用规范字段；别名只允许在可信管理员配置源规范化。
# 模块用途: 核三种管理员开关、YAML及运行时统一阈值，禁止用户层解除锁。
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.compact_threshold_policy import resolve_thread_compact_policy
from agent_py_agent.agent.settings.config import load_config


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证三个管理员强制配置名都进入实际运行策略。
@pytest.mark.parametrize("key", ["force_compact_trigger_percent", "admin_force_compact_trigger_percent", "memory_compact_admin_forced"])
def test_all_admin_force_switches_apply_to_runtime(key):
    config = SimpleNamespace(memory_compact_trigger_percent=80, **{key: True})
    thread = SimpleNamespace(metadata={"settings": {"compact_trigger_percent": 90}})
    policy = resolve_thread_compact_policy(SimpleNamespace(config=config), thread)
    assert policy["effective_trigger_percent"] == 80
    assert policy["effective_source"] == "admin_forced" and policy["force_locked"]


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证管理员YAML开关和每拍闲置上限不会被加载器忽略。
@pytest.mark.parametrize("key", ["force_compact_trigger_percent", "admin_force_compact_trigger_percent", "memory_compact_admin_forced"])
def test_admin_yaml_switches_and_idle_limit_are_not_ignored(tmp_path, key):
    path = tmp_path / "agent.yaml"
    path.write_text(f"{key}: true\nmemory_compact_idle_max_per_tick: 1\n")
    config = load_config(path)
    assert config.force_compact_trigger_percent is True
    assert getattr(config, "memory_compact_idle_max_per_tick", None) == 1


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证别名配置沿用已有布尔解析规则。
@pytest.mark.parametrize("key", ["admin_force_compact_trigger_percent", "memory_compact_admin_forced"])
def test_alias_normalization_keeps_existing_boolean_coercion(tmp_path, key):
    path = tmp_path / "agent.yaml"
    path.write_text(f'force_compact_trigger_percent: "true"\n{key}: false\n')
    assert load_config(path).force_compact_trigger_percent is True
    path.write_text(f'{key}: "true"\n')
    assert load_config(path).force_compact_trigger_percent is True


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证owner覆盖不能借别名改写管理员强制比例。
@pytest.mark.parametrize("key", ["admin_force_compact_trigger_percent", "memory_compact_admin_forced"])
def test_owner_layer_cannot_override_alias_forced_admin_default(tmp_path, key):
    from agent_py_agent.agent.settings.config import AgentConfig
    from agent_py_agent.agent.settings.runtime_scope_config import (
        RuntimeConfigLayer,
        merge_runtime_config_layers,
    )

    path = tmp_path / "admin.yaml"
    path.write_text(f"{key}: true\nmemory_compact_trigger_percent: 80\n")
    merged = merge_runtime_config_layers(load_config(path), [RuntimeConfigLayer("owner", "owner", {
        key: False, "force_compact_trigger_percent": False, "memory_compact_trigger_percent": 95})])
    config = AgentConfig(**merged.values, config_sources=merged.sources)
    result = resolve_thread_compact_policy(SimpleNamespace(config=config))
    assert result["effective_trigger_percent"] == 80 and result["force_locked"]
