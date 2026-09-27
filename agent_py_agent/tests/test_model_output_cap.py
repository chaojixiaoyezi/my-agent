"""模型单次输出上限统一为 64K，并且只在一处按已知窗口夹取（2026-09-27，用户要求“所有模型统一 64K”）。

锁定：随包 YAML、AgentConfig 默认值与常量同值；窗口已明确时输出上限 = min(配置, 窗口 ÷ 4)，未明确时按配置原值；
后端工厂对默认模型与模型档案走同一规则（原来只有模型档案路径夹取，默认模型不夹）；没有读取方的 vision_* 配置已删除，
用户配置里残留时只告警、不影响加载。
"""
from __future__ import annotations

from dataclasses import fields, replace
from pathlib import Path

import pytest

from agent_py_agent.agent.backends.factory import get_backend
from agent_py_agent.agent.settings.config import AgentConfig, load_config
from agent_py_agent.agent.settings.config_io import load_simple_yaml
from agent_py_agent.agent.settings.defaults import (
    DEFAULT_MODEL_MAX_TOKENS,
    effective_max_output_tokens,
)

_PACKAGED = Path(__file__).resolve().parents[1] / "config" / "agent_config.yaml"


def test_packaged_yaml_dataclass_and_constant_agree_on_64k():
    assert DEFAULT_MODEL_MAX_TOKENS == 65_536
    assert AgentConfig().max_tokens == DEFAULT_MODEL_MAX_TOKENS
    assert int(load_simple_yaml(_PACKAGED)["max_tokens"]) == DEFAULT_MODEL_MAX_TOKENS


@pytest.mark.parametrize("window,explicit,expected", [
    (1_000_000, True, 65_536), (262_144, True, 65_536), (204_800, True, 51_200), (128_000, True, 32_000),
    (32_768, True, 8_192), (128_000, False, 65_536), (0, True, 65_536),
])
def test_output_cap_is_clamped_only_by_a_known_window(window, explicit, expected):
    config = replace(AgentConfig(), model_context_window_tokens=window, model_context_window_explicit=explicit)
    assert effective_max_output_tokens(config) == expected


def test_backend_factory_sends_the_clamped_cap_for_the_default_model_too():
    # 默认模型（不经模型档案）原来直接发送配置值；现在与模型档案同一规则。
    config = replace(AgentConfig(), model_backend="openai_compatible", api_base="http://127.0.0.1:9/v1",
                     api_key="test-key", model_name="m", model_context_window_tokens=128_000,
                     model_context_window_explicit=True)
    assert get_backend(config.model_backend, config).max_tokens == 32_000
    unknown_window = replace(config, model_context_window_explicit=False)
    assert get_backend(unknown_window.model_backend, unknown_window).max_tokens == 65_536


def test_dead_vision_config_is_removed_and_leftovers_only_warn(tmp_path):
    assert not [item.name for item in fields(AgentConfig) if item.name.startswith("vision_")]
    assert not [key for key in load_simple_yaml(_PACKAGED) if key.startswith("vision_")]
    user_config = tmp_path / "user.yaml"
    user_config.write_text("vision_max_tokens: 1024\nmax_tokens: 65536\n", encoding="utf-8")
    config = load_config(user_config)
    assert config.max_tokens == 65_536
    assert any("vision_max_tokens" in warning for warning in config.config_warnings)
