"""Runner timeout policy tests kept separate from the large dispatch test module."""

from __future__ import annotations

from unittest.mock import MagicMock

from agent_py_agent.agent.settings.config import AgentConfig


def _timeout_config(value: object) -> AgentConfig:
    config = AgentConfig()
    config.runner_timeout_seconds = value
    return config


def test_role_timeout_uses_exact_structured_role():
    from agent_py_agent.agent.agent_core.runner.gate import get_task_timeout

    config = _timeout_config("off")
    config.runner_timeout_by_role = {"child_coordinator": "12"}

    task = MagicMock()
    task.id = "child-coord-run"
    task.root_id = "root-run"
    task.parent_id = "root-run"
    task.role = "child_coordinator"
    task.attributes = {}
    task.goal = "协调市场研究小组。"
    task.plan = []

    assert get_task_timeout(task, 0.0, config) == 12.0


def test_runner_timeout_config_rejects_old_disable_words():
    from agent_py_agent.agent.settings import normalize_agent_config

    normalized, warnings = normalize_agent_config({"runner_timeout_seconds": "disabled"})

    assert normalized["runner_timeout_seconds"] == "off"
    assert any("runner_timeout_seconds" in warning for warning in warnings)


def test_runner_timeout_config_rejects_non_finite_numbers():
    from agent_py_agent.agent.settings import normalize_agent_config

    normalized, warnings = normalize_agent_config({"runner_timeout_seconds": "inf"})

    assert normalized["runner_timeout_seconds"] == "off"
    assert any("runner_timeout_seconds" in warning for warning in warnings)
