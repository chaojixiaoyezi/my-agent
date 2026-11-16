"""计划重启配置必须从公开 YAML 传到运行时，非法预算不能扩成无限等待。"""

import math
from pathlib import Path

import pytest

from agent_py_agent.agent.settings.config import AgentConfig, load_simple_yaml
from agent_py_agent.agent.settings.normalize import normalize_agent_config


def test_public_restart_defaults_and_explicit_graceful():
    path = Path(__file__).parents[1] / "config" / "agent_config.yaml"
    template = load_simple_yaml(path)
    template, _ = normalize_agent_config(template)
    defaults = AgentConfig()
    assert template["gateway_restart_mode"] == defaults.gateway_restart_mode == "fast"
    assert template["gateway_restart_fast_timeout_seconds"] == defaults.gateway_restart_fast_timeout_seconds == 2.0
    normalized, warnings = normalize_agent_config({"gateway_restart_mode": "graceful", "gateway_restart_fast_timeout_seconds": 0.25})
    assert normalized["gateway_restart_mode"] == "graceful"
    assert normalized["gateway_restart_fast_timeout_seconds"] == 0.25
    assert not warnings


@pytest.mark.parametrize("value", [0, -1, True, math.inf, math.nan, "bad"])
def test_invalid_fast_budget_is_explained_and_not_unlimited(value):
    with pytest.raises(ValueError, match="gateway_restart_fast_timeout_seconds"):
        AgentConfig(gateway_restart_fast_timeout_seconds=value)
    normalized, warnings = normalize_agent_config({"gateway_restart_fast_timeout_seconds": value})
    assert normalized["gateway_restart_fast_timeout_seconds"] == 2.0
    assert any("gateway_restart_fast_timeout_seconds" in warning for warning in warnings)


def test_unknown_restart_mode_is_reported_and_legacy_zero_stays_zero():
    normalized, warnings = normalize_agent_config({"gateway_restart_mode": "instant-force", "gateway_restart_drain_timeout_seconds": 0})
    assert normalized["gateway_restart_mode"] == "fast"
    assert normalized["gateway_restart_drain_timeout_seconds"] == 0
    assert any("gateway_restart_mode" in warning for warning in warnings)
