from __future__ import annotations

"""LLM: verifies SubAgentManager persistence now routes through a service.

给人看的解释：
这个测试确保 service 化没有改变子代理工单的创建、保存、读取和扫描行为。
"""

from agent_py_agent.agent.subagents.manager import SubAgentManager


def test_subagent_persistence_service_round_trips_task(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)

    task = manager.create_run(
        goal="治理持久化边界",
        thought="把读写职责移到 service 后保持兼容。",
        plan=["创建", "保存", "读取"],
    )
    task.status = "DONE"
    manager.save(task)

    loaded = manager.load(task.id)
    runs = manager.list_runs()

    assert manager.persistence is not None
    assert loaded.id == task.id
    assert loaded.status == "DONE"
    assert loaded.goal == "治理持久化边界"
    assert any(item.id == task.id for item in runs)
    assert (tmp_path / task.id / "task.json").exists()
    assert (tmp_path / task.id / "thought.md").exists()
