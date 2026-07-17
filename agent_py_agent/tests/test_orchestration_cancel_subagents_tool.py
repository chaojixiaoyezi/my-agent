from __future__ import annotations

import json
import time


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
    assert loaded.status == "CANCELLED"  # 主代理主动取消 = CANCELLED(不再误标 ABANDONED 烂尾)
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
    assert agent.subagents.load(parent.id).status == "CANCELLED"
    assert agent.subagents.load(child.id).status == "CANCELLED"
    assert agent.subagents.load(done_child.id).status == "DONE"


def test_cancel_subagents_retires_existing_conversation_task_link(tmp_path):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.subagents.services.base import CreateRunParams

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    task = agent.subagents.create_run(
        params=CreateRunParams(goal="会话绑定任务", thought="取消测试", plan=["运行"], allowed_tools=["read_file"])
    )
    task.status = "RUNNING"
    agent.subagents.save(task)
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "user-1",
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": task.id,
            "goal": task.goal,
            "status": "active",
        }
    )

    result = agent.tools.execute_call(
        {"tool": "cancel_subagents", "run_id": task.id, "reason": "停止会话绑定任务"}
    )
    payload = json.loads(result.output)
    link = store.task_links(thread.thread_id)[0]
    updated_thread = store.load_thread(thread.thread_id)

    assert result.ok is True
    assert payload["cancelled"][0]["conversation_link"] == {"status": "updated"}
    assert link.status == "cancelled"
    assert updated_thread is not None
    assert task.id not in updated_thread.active_task_ids


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


def test_cancel_subagents_never_signals_gateway_pid_for_in_process_runner(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.orchestration.tools import cancel
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.subagents.services.base import CreateRunParams

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    task = agent.subagents.create_run(
        params=CreateRunParams(goal="进程内任务", thought="取消测试", plan=["运行"], allowed_tools=["read_file"])
    )
    task.status = "RUNNING"
    task.attributes = {
        **dict(task.attributes or {}),
        "pid": 4242,
        "runner_session": _fresh_runner_session(worker_pid=4242, in_process=True),
    }
    agent.subagents.save(task)
    signalled: list[int] = []
    monkeypatch.setattr(cancel, "terminate_pid_with_escalation", lambda pid: signalled.append(pid) or {})

    result = agent.tools.execute_call({"tool": "cancel_subagents", "run_id": task.id, "reason": "停止进程内任务"})
    payload = json.loads(result.output)

    assert result.ok is True
    assert signalled == []
    assert payload["cancelled"][0]["pid_report"]["status"] == "no_pid"
    assert "thread_interrupt" in payload["cancelled"][0]["pid_report"]


def test_cancel_subagents_signals_fresh_subprocess_runner_pid(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.orchestration.tools import cancel
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.subagents.services.base import CreateRunParams

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    task = agent.subagents.create_run(
        params=CreateRunParams(goal="独立进程任务", thought="取消测试", plan=["运行"], allowed_tools=["read_file"])
    )
    task.status = "RUNNING"
    task.attributes = {
        **dict(task.attributes or {}),
        "runner_session": _fresh_runner_session(worker_pid=5252, in_process=False),
    }
    agent.subagents.save(task)
    signalled: list[int] = []

    def _terminate(pid: int) -> dict[str, object]:
        signalled.append(pid)
        return {"status": "terminated", "pid": pid, "escalated": False}

    monkeypatch.setattr(cancel, "terminate_pid_with_escalation", _terminate)

    result = agent.tools.execute_call({"tool": "cancel_subagents", "run_id": task.id, "reason": "停止独立进程"})
    payload = json.loads(result.output)

    assert result.ok is True
    assert signalled == [5252]
    assert payload["cancelled"][0]["pid_report"]["status"] == "terminated"


def test_cancel_subagents_missing_runner_topology_fails_closed(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.orchestration.tools import cancel
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.subagents.services.base import CreateRunParams

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    task = agent.subagents.create_run(
        params=CreateRunParams(goal="旧版会话", thought="取消测试", plan=["运行"], allowed_tools=["read_file"])
    )
    task.status = "RUNNING"
    session = _fresh_runner_session(worker_pid=6262, in_process=True)
    session.pop("in_process")
    task.attributes = {**dict(task.attributes or {}), "pid": 6262, "runner_session": session}
    agent.subagents.save(task)
    signalled: list[int] = []
    monkeypatch.setattr(cancel, "terminate_pid_with_escalation", lambda pid: signalled.append(pid) or {})

    result = agent.tools.execute_call({"tool": "cancel_subagents", "run_id": task.id, "reason": "停止旧版会话"})
    payload = json.loads(result.output)

    assert result.ok is True
    assert signalled == []
    assert payload["cancelled"][0]["pid_report"]["status"] == "no_pid"


def test_cancel_subagents_missing_runner_session_ignores_legacy_pid(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.orchestration.tools import cancel
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.subagents.services.base import CreateRunParams

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    task = agent.subagents.create_run(
        params=CreateRunParams(goal="缺少会话记录", thought="取消测试", plan=["运行"], allowed_tools=["read_file"])
    )
    task.status = "RUNNING"
    task.attributes = {**dict(task.attributes or {}), "pid": 7373, "runner_process": {"pid": 7474}}
    agent.subagents.save(task)
    signalled: list[int] = []
    monkeypatch.setattr(cancel, "terminate_pid_with_escalation", lambda pid: signalled.append(pid) or {})

    result = agent.tools.execute_call({"tool": "cancel_subagents", "run_id": task.id, "reason": "停止旧记录"})
    payload = json.loads(result.output)

    assert result.ok is True
    assert signalled == []
    assert payload["cancelled"][0]["pid_report"]["status"] == "no_pid"


def test_cancel_subagents_stale_subprocess_session_never_signals_reused_pid(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.orchestration.tools import cancel
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.subagents.services.base import CreateRunParams

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    task = agent.subagents.create_run(
        params=CreateRunParams(goal="过期独立进程", thought="取消测试", plan=["运行"], allowed_tools=["read_file"])
    )
    task.status = "RUNNING"
    session = _fresh_runner_session(worker_pid=8383, in_process=False)
    session["heartbeat_at"] = time.time() - 3600
    task.attributes = {**dict(task.attributes or {}), "pid": 8484, "runner_session": session}
    agent.subagents.save(task)
    signalled: list[int] = []
    monkeypatch.setattr(cancel, "terminate_pid_with_escalation", lambda pid: signalled.append(pid) or {})

    result = agent.tools.execute_call({"tool": "cancel_subagents", "run_id": task.id, "reason": "停止过期记录"})
    payload = json.loads(result.output)

    assert result.ok is True
    assert signalled == []
    assert payload["cancelled"][0]["pid_report"]["status"] == "no_pid"


def _fresh_runner_session(*, worker_pid: int, in_process: bool) -> dict[str, object]:
    now = time.time()
    return {
        "schema_version": "runner_session_pool.v1",
        "session_id": f"runsess-{worker_pid}",
        "run_id": "run-cancel-test",
        "worker_pid": worker_pid,
        "worker_id": f"worker-{worker_pid}",
        "status": "running",
        "heartbeat_at": now,
        "interval_seconds": 5.0,
        "started_at": now - 1.0,
        "ended_at": 0.0,
        "in_process": in_process,
    }


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
