"""参数中心登记表（2026-09-27）：每个 AgentConfig 字段一条，说明取自随包 YAML 注释，安全等级结构化判定。

锁定：登记表覆盖全部字段；凭据、权限、身份、路径、外部地址、会运行代码的设置、执行权威链与内部元数据不可由模型改；
普通节奏/预算/上限参数可改；显式白名单（飞书凭据）可改但脱敏；搜索先按键名再按说明。
"""
from __future__ import annotations

from dataclasses import fields

import pytest

from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.settings.parameter_registry import (
    SAFETY_BOUNDARY,
    applied_value,
    parameter_registry,
    search_parameters,
)
from agent_py_agent.agent.settings.user_config_capability import BOUNDARY_KEYS, TUNABLE_KEYS


def test_applied_value_uses_the_single_output_cap_formula():
    """64K 在 128K 窗口模型上实际是 32768；窗口未知时等于配置值；没有派生规则的参数不给实际使用值。"""
    from types import SimpleNamespace

    value, rule = applied_value("max_tokens", SimpleNamespace(max_tokens=65536, model_context_window_tokens=131072))
    assert value == 32768 and "窗口" in rule
    assert applied_value("max_tokens", SimpleNamespace(max_tokens=65536, model_context_window_tokens=0))[0] == 65536
    assert applied_value("request_timeout", SimpleNamespace(request_timeout=300)) is None
    assert applied_value("max_tokens", None) is None


def test_registry_covers_every_config_field_with_yaml_descriptions():
    registry = parameter_registry()
    assert set(registry) == {item.name for item in fields(AgentConfig)}
    assert "64K" in registry["max_tokens"].description
    assert registry["max_tokens"].value_type == "int" and registry["enable_self_learning"].value_type == "bool"


@pytest.mark.parametrize("key", [
    *sorted(BOUNDARY_KEYS), "api_base", "model_backend", "memory_embedding_api_base", "system_prompt", "prompt_files",
    "audit_enabled", "enable_tools", "enable_gateway_restart_tool", "additional_write_roots", "mcp_servers",
    "execution_mode", "computer_use_enabled", "concurrency_lock_enabled", "result_check_execute_tests", "config_sources",
    "self_dev_worktree", "protect_running_runtime",
])
def test_security_relevant_keys_are_never_model_writable(key):
    spec = parameter_registry()[key]
    assert spec.safety == SAFETY_BOUNDARY and spec.writable is False


@pytest.mark.parametrize("key", ["max_tokens", "request_timeout", "memory_compact_auto_trigger_percent",
                                 "enable_self_learning", "tool_read_max_chars", "agent_name"])
def test_ordinary_parameters_are_model_writable(key):
    assert parameter_registry()[key].writable is True


def test_secret_like_keys_are_writable_only_when_explicitly_listed_and_always_masked():
    for spec in parameter_registry().values():
        if spec.masked:
            assert spec.writable is (spec.key in TUNABLE_KEYS), spec.key
    secret = parameter_registry()["feishu_app_secret"]
    assert secret.masked and secret.writable


def test_search_prefers_key_matches_then_descriptions():
    keys = [spec.key for spec in search_parameters("max_tokens", limit=5)]
    assert keys[0] == "max_tokens"
    assert any(spec.key == "max_tokens" for spec in search_parameters("64K"))
