"""累计配额读失败必须在派生前拒绝，不能退化为零历史。"""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.capacity import (
    SubagentCapacityStateError,
    checked_creation_capacity,
    cumulative_created_child_count,
)


class BrokenDatabase:
    @contextmanager
    def _runtime_connection(self):
        raise OSError("sensitive source path must not reach receipt")
        yield


class BrokenManager:
    def load(self, run_id):
        raise ValueError("sensitive record must not reach receipt")


@pytest.mark.parametrize("broken", ["database", "parent"])
def test_cumulative_read_failure_is_not_zero_history(broken):
    agent = SimpleNamespace(
        runtime_db=BrokenDatabase() if broken == "database" else None,
        subagents=BrokenManager() if broken == "parent" else None,
    )
    with pytest.raises(SubagentCapacityStateError) as result:
        cumulative_created_child_count(agent, "parent-1")
    assert "sensitive" not in str(result.value)
    assert "cumulative." in str(result.value)


@pytest.mark.parametrize("broken", ["database", "parent"])
def test_checked_capacity_explains_read_failure_before_creation(monkeypatch, broken):
    from agent_py_agent.agent.agent_core.orchestration import capacity
    agent = SimpleNamespace(
        runtime_db=BrokenDatabase() if broken == "database" else None,
        subagents=BrokenManager() if broken == "parent" else None,
    )
    monkeypatch.setattr(capacity, "available_creation_slots", lambda agent: (
        cumulative_created_child_count(agent, "parent-1"), {}))
    outcome = checked_creation_capacity(agent)
    assert not outcome.ok
    assert outcome.reported_error_code == "SUBAGENT_CAPACITY_UNAVAILABLE"
    assert outcome.effect_outcome == "not_started"
    assert "cumulative." in outcome.output
    assert "sensitive" not in outcome.output
    assert "重试" in outcome.output


def test_cumulative_count_is_per_parent_not_shared_ancestor(tmp_path):
    from agent_py_agent.agent.subagents.manager import SubAgentManager
    manager = SubAgentManager(tmp_path)
    root = manager.create_run(goal="root", thought="plan", plan=["work"])
    parents = [manager.create_run(goal=str(i), thought="plan", plan=["work"],
                                  parent_id=root.id) for i in range(2)]
    for parent in parents:
        child = manager.create_run(goal="child", thought="plan", plan=["work"],
                                   parent_id=parent.id)
        parent.child_ids.append(child.id)
        manager.save(parent)
    agent = SimpleNamespace(subagents=manager, runtime_db=None)
    assert [cumulative_created_child_count(agent, p.id) for p in parents] == [1, 1]
