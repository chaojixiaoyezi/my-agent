from __future__ import annotations

import json


def test_cancel_subagents_tool_abandons_active_attempt_and_audits(tmp_path):
    from agent_py_agent.agent.config import AgentConfig
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.subagents.services.base import CreateRunParams

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    task = agent.subagents.create_run(
        params=CreateRunParams(goal="读项目 A", thought="取消测试", plan=["创建任务"], allowed_tools=["read_file"])
    )
    task.status = "RUNNING"
    task.runner_active_attempt_id = "attempt-live"
    agent.subagents.save(task)

    result = agent.tools.execute_call(
        {
            "tool": "cancel_subagents",
            "run_ids": [task.id],
            "reason": "测试取消",
        }
    )
    payload = json.loads(result.output)
    loaded = agent.subagents.load(task.id)

    assert result.ok is True
    assert payload["cancelled"][0]["run_id"] == task.id
    assert loaded.status == "ABANDONED"
    assert loaded.failure_type == "cancelled"
    assert loaded.runner_active_attempt_id == ""
    assert "attempt-live" in loaded.runner_abandoned_attempt_ids
    assert loaded.attributes["cancel_subagents"]["cancel_status"] == "CANCELLED"
    assert loaded.attributes["cancel_subagents"]["abandoned_attempt_id"] == "attempt-live"


def test_cancel_subagents_tool_filters_by_root_and_status(tmp_path):
    from agent_py_agent.agent.config import AgentConfig
    from agent_py_agent.agent.core import SimpleAgent

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    parent, child, done_child = _create_cancel_tree(agent)

    result = agent.tools.execute_call(
        {
            "tool": "cancel_subagents",
            "root_id": parent.id,
            "status": ["RUNNING", "PLANNING"],
            "reason": "清理子树",
        }
    )
    payload = json.loads(result.output)
    cancelled_ids = {item["run_id"] for item in payload["cancelled"]}

    assert result.ok is True
    assert cancelled_ids == {parent.id, child.id}
    assert agent.subagents.load(parent.id).status == "ABANDONED"
    assert agent.subagents.load(child.id).status == "ABANDONED"
    assert agent.subagents.load(done_child.id).status == "DONE"


def _create_cancel_tree(agent):
    from agent_py_agent.agent.subagents.services.base import CreateRunParams

    parent = agent.subagents.create_run(
        params=CreateRunParams(goal="父任务", thought="取消测试", plan=["创建父任务"], allowed_tools=["read_file"])
    )
    child = agent.subagents.create_run(
        params=CreateRunParams(goal="子任务", thought="取消测试", plan=["创建子任务"], allowed_tools=["read_file"], parent_id=parent.id)
    )
    done_child = agent.subagents.create_run(
        params=CreateRunParams(goal="完成任务", thought="取消测试", plan=["创建完成任务"], allowed_tools=["read_file"], parent_id=parent.id)
    )
    parent.status = "RUNNING"
    child.status = "PLANNING"
    done_child.status = "DONE"
    for task in (parent, child, done_child):
        agent.subagents.save(task)
    return parent, child, done_child
