"""Runner timeout alias tests kept separate from the large dispatch test module."""

from __future__ import annotations

from unittest.mock import MagicMock

from agent_py_agent.agent.settings.config import AgentConfig


# LLM: _timeout_config builds the minimal AgentConfig surface needed by runner_gate tests.
# 函数用途: 生成带 runner_timeout_seconds 的配置对象，避免复用过大的测试类。
def _timeout_config(value: object) -> AgentConfig:
    config = AgentConfig()
    config.runner_timeout_seconds = value
    return config


# LLM: child_coordinator must inherit coordinator timeout aliases from config.
# 函数用途: 复现 child_coordinator 没吃 coordinator 超时桶导致干净 E2E 卡住的问题。
def test_role_timeout_coordinator_alias_matches_child_coordinator():
    from agent_py_agent.agent.agent_core.runner_gate import get_task_timeout

    config = _timeout_config("off")
    config.runner_timeout_by_role = {"coordinator": "12"}

    task = MagicMock()
    task.id = "child-coord-run"
    task.root_id = "root-run"
    task.parent_id = "root-run"
    task.role = "child_coordinator"
    task.attributes = {}
    task.goal = "协调市场研究小组。"
    task.plan = []

    assert get_task_timeout(task, 0.0, config) == 12.0
