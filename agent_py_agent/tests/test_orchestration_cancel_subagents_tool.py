from __future__ import annotations

import json


def test_cancel_subagents_tool_abandons_active_attempt_and_audits(tmp_path):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
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
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

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


def test_cancel_subagents_tool_rejects_old_status_filter_alias(tmp_path):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    parent, child, _done_child = _create_cancel_tree(agent)

    result = agent.tools.execute_call(
        {
            "tool": "cancel_subagents",
            "root_id": parent.id,
            "status": ["completed"],
            "reason": "旧别名不能驱动取消筛选",
        }
    )
    payload = json.loads(result.output)

    assert result.ok is False
    assert payload["error"] == "invalid_status_filter"
    assert payload["invalid_statuses"] == ["completed"]
    assert agent.subagents.load(parent.id).status == "RUNNING"
    assert agent.subagents.load(child.id).status == "PLANNING"


def test_cancel_subagents_tool_reports_list_runs_failure_for_tree_filters(tmp_path, monkeypatch):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)

    def broken_list_runs():
        raise RuntimeError("state index unreadable")

    monkeypatch.setattr(agent.subagents, "list_runs", broken_list_runs)

    result = agent.tools.execute_call(
        {
            "tool": "cancel_subagents",
            "root_id": "run-missing",
            "reason": "清理子树",
        }
    )
    payload = json.loads(result.output)

    assert result.ok is False
    assert payload["ok"] is False
    assert payload["error"]["context"] == "cancel_subagents.list_runs"
    assert "state index unreadable" in payload["error"]["message"]


def test_cancel_subagents_tool_reports_corrupt_locator_without_private_recovery(tmp_path):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.subagents.services.base import CreateRunParams

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    task = agent.subagents.create_run(
        params=CreateRunParams(goal="读项目 A", thought="取消测试", plan=["创建任务"], allowed_tools=["read_file"])
    )
    task.status = "RUNNING"
    agent.subagents.save(task)
    task_json = agent.subagents.workspace / task.id / "task.json"
    run_json = agent.subagents.workspace / task.id / "run.json"
    task_json.write_text(task_json.read_text(encoding="utf-8") + "}", encoding="utf-8")
    run_json.write_text(run_json.read_text(encoding="utf-8") + "}", encoding="utf-8")

    result = agent.tools.execute_call(
        {
            "tool": "cancel_subagents",
            "run_id": task.id,
            "reason": "取消腐坏账本",
        }
    )
    payload = json.loads(result.output)

    assert result.ok is False
    assert payload["failed"][0]["run_id"] == task.id
    assert payload["failed"][0]["error"]["context"] == "cancel_subagents.load"
    assert payload["cancelled"] == []


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
