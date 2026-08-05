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


def test_cancel_subagents_tool_requires_same_run_retry_before_cancellation(tmp_path):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.subagents.services.base import CreateRunParams

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    task = agent.subagents.create_run(
        params=CreateRunParams(
            goal="继续原审计",
            thought="重试保护",
            plan=["复用 checkpoint"],
            allowed_tools=["read_file"],
        )
    )
    task.status = "BLOCKED"
    task.failure_type = "structured_output_parse_error"
    task.runner_attempts = 1
    agent.subagents.save(task)

    result = agent.tools.execute_call(
        {
            "tool": "cancel_subagents",
            "run_id": task.id,
            "reason": "模型认为永久阻塞",
        }
    )
    payload = json.loads(result.output)
    loaded = agent.subagents.load(task.id)

    assert result.ok is False
    assert result.error_code == "SUBAGENT_RETRY_REQUIRED"
    assert payload["error_code"] == "SUBAGENT_RETRY_REQUIRED"
    assert payload["protected_runs"] == [
        {
            "run_id": task.id,
            "status": "BLOCKED",
            "failure_type": "structured_output_parse_error",
            "runner_attempts": 1,
        }
    ]
    assert payload["next_action"]["tool"] == "dispatch_subagents"
    assert payload["next_action"]["params"]["run_ids"] == [task.id]
    assert loaded.status == "BLOCKED"
    assert loaded.failure_type == "structured_output_parse_error"


def test_cancel_subagents_tool_allows_cancellation_after_same_run_retry_exhausted(tmp_path):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.subagents.services.base import CreateRunParams

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    task = agent.subagents.create_run(
        params=CreateRunParams(
            goal="耗尽重试的审计",
            thought="取消测试",
            plan=["结束"],
            allowed_tools=["read_file"],
        )
    )
    task.status = "BLOCKED"
    task.failure_type = "structured_output_parse_error"
    task.runner_attempts = 2
    agent.subagents.save(task)

    result = agent.tools.execute_call(
        {
            "tool": "cancel_subagents",
            "run_id": task.id,
            "reason": "重试预算已耗尽",
        }
    )

    assert result.ok is True
    assert agent.subagents.load(task.id).status == "CANCELLED"


def test_cancel_subagents_tool_cannot_cancel_system_managed_audit_source_worker(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration.tools.cancel import (
        CancelSubagentTaskRequest,
        cancel_subagent_task,
    )
    from agent_py_agent.agent.common.audit_activation import (
        AUDIT_SOURCE_ID_ATTR,
        AUDIT_SOURCE_OWNER_HOME_ATTR,
        AUDIT_SOURCE_WATCH_ID_ATTR,
        AUDIT_SOURCE_WORKER_ATTR,
        AUDIT_SOURCE_WORKER_KEY_ATTR,
        audit_source_worker_key,
    )
    from agent_py_agent.agent.conversation.authority import CONVERSATION_REQUEST_ID_ATTR
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.subagents.services.base import CreateRunParams

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    task = agent.subagents.create_run(
        params=CreateRunParams(
            goal="持续处理来源",
            thought="系统来源工作者",
            plan=["消费来源账"],
            allowed_tools=["watch_stream"],
        )
    )
    audit_id = "audit-1"
    watch_id = "watch-1"
    task.status = "BLOCKED"
    task.failure_type = "structured_output_parse_error"
    task.runner_attempts = 99
    task.attributes = {
        **dict(task.attributes or {}),
        AUDIT_SOURCE_WORKER_ATTR: True,
        AUDIT_SOURCE_ID_ATTR: "source-1",
        AUDIT_SOURCE_WATCH_ID_ATTR: watch_id,
        AUDIT_SOURCE_WORKER_KEY_ATTR: audit_source_worker_key(audit_id, watch_id),
        AUDIT_SOURCE_OWNER_HOME_ATTR: str(tmp_path / "home"),
        CONVERSATION_REQUEST_ID_ATTR: audit_id,
    }
    agent.subagents.save(task)

    result = agent.tools.execute_call(
        {
            "tool": "cancel_subagents",
            "run_id": task.id,
            "reason": "模型认为它卡住了",
        }
    )
    payload = json.loads(result.output)

    assert result.ok is False
    assert result.error_code == "AUDIT_SOURCE_WORKER_SYSTEM_MANAGED"
    assert payload["protected_runs"][0]["run_id"] == task.id
    assert agent.subagents.load(task.id).status == "BLOCKED"

    # The exact lifecycle controller still uses the canonical cancellation
    # primitive when the named Audit itself is cleared.
    cancelled = cancel_subagent_task(
        agent,
        CancelSubagentTaskRequest(
            task=agent.subagents.load(task.id),
            reason="audit_watch_closed",
            source="audit_source_worker",
        ),
    )
    assert cancelled["status"] == "CANCELLED"
    assert agent.subagents.load(task.id).status == "CANCELLED"


def test_cancel_subagents_retry_protection_is_atomic_for_mixed_targets(tmp_path):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.subagents.services.base import CreateRunParams

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    running = agent.subagents.create_run(
        params=CreateRunParams(
            goal="正在运行",
            thought="取消测试",
            plan=["运行"],
            allowed_tools=["read_file"],
        )
    )
    retryable = agent.subagents.create_run(
        params=CreateRunParams(
            goal="可恢复失败",
            thought="重试保护",
            plan=["继续"],
            allowed_tools=["read_file"],
        )
    )
    running.status = "RUNNING"
    retryable.status = "FAILED"
    retryable.failure_type = "runner_error"
    retryable.runner_attempts = 1
    agent.subagents.save(running)
    agent.subagents.save(retryable)

    result = agent.tools.execute_call(
        {
            "tool": "cancel_subagents",
            "run_ids": [running.id, retryable.id],
            "reason": "整批清理",
        }
    )

    assert result.ok is False
    assert result.error_code == "SUBAGENT_RETRY_REQUIRED"
    assert agent.subagents.load(running.id).status == "RUNNING"
    assert agent.subagents.load(retryable.id).status == "FAILED"


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


def test_cancel_subagents_fences_one_task_without_killing_shared_subprocess_host(
    tmp_path,
    monkeypatch,
):
    from agent_py_agent.agent.agent_core.orchestration.tools import cancel
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.subagents.services.base import CreateRunParams

    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")),
        tmp_path,
    )
    tasks = [
        agent.subagents.create_run(
            params=CreateRunParams(
                goal=f"共享宿主任务 {index}",
                thought="取消隔离测试",
                plan=["运行"],
                allowed_tools=["read_file"],
            )
        )
        for index in range(2)
    ]
    for task in tasks:
        task.status = "RUNNING"
        task.attributes = {
            **dict(task.attributes or {}),
            "runner_session": _fresh_runner_session(
                worker_pid=5353,
                in_process=False,
            ),
        }
        agent.subagents.save(task)
    signalled: list[int] = []

    def _terminate(pid: int) -> dict[str, object]:
        signalled.append(pid)
        return {"status": "terminated", "pid": pid, "escalated": False}

    monkeypatch.setattr(cancel, "terminate_pid_with_escalation", _terminate)

    first = agent.tools.execute_call(
        {
            "tool": "cancel_subagents",
            "run_id": tasks[0].id,
            "reason": "只停止第一个任务",
        }
    )
    first_payload = json.loads(first.output)

    assert first.ok is True
    assert signalled == []
    assert first_payload["cancelled"][0]["pid_report"] == {
        "status": "shared_host_cooperative",
        "pid": 5353,
        "escalated": False,
        "shared_run_ids": [tasks[1].id],
    }
    assert agent.subagents.load(tasks[0].id).status == "CANCELLED"
    assert agent.subagents.load(tasks[1].id).status == "RUNNING"

    second = agent.tools.execute_call(
        {
            "tool": "cancel_subagents",
            "run_id": tasks[1].id,
            "reason": "再停止最后一个任务",
        }
    )
    second_payload = json.loads(second.output)

    assert second.ok is True
    assert signalled == [5353]
    assert second_payload["cancelled"][0]["pid_report"]["status"] == "terminated"


def test_cancel_subagents_does_not_interrupt_shared_in_process_dispatch(
    tmp_path,
    monkeypatch,
):
    from agent_py_agent.agent.agent_core.orchestration.tools import cancel
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.subagents.services.base import CreateRunParams

    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")),
        tmp_path,
    )
    tasks = [
        agent.subagents.create_run(
            params=CreateRunParams(
                goal=f"共享线程任务 {index}",
                thought="取消隔离测试",
                plan=["运行"],
                allowed_tools=["read_file"],
            )
        )
        for index in range(2)
    ]
    for task in tasks:
        task.status = "RUNNING"
        task.attributes = {
            **dict(task.attributes or {}),
            "runner_session": _fresh_runner_session(
                worker_pid=4242,
                in_process=True,
            ),
        }
        agent.subagents.save(task)
    agent._background_subagent_dispatches = {
        "launch-shared": {
            "run_ids": [task.id for task in tasks],
            "thread_name": "my-agent-launch-shared",
        }
    }
    interrupted: list[str] = []
    monkeypatch.setattr(
        cancel,
        "interrupt_by_name",
        lambda name: interrupted.append(name) or True,
    )

    first = agent.tools.execute_call(
        {
            "tool": "cancel_subagents",
            "run_id": tasks[0].id,
            "reason": "只停止第一个线程任务",
        }
    )
    first_payload = json.loads(first.output)

    assert first.ok is True
    assert interrupted == []
    assert (
        first_payload["cancelled"][0]["pid_report"]["thread_interrupt"]
        == "shared_host_cooperative"
    )
    assert agent.subagents.load(tasks[1].id).status == "RUNNING"

    second = agent.tools.execute_call(
        {
            "tool": "cancel_subagents",
            "run_id": tasks[1].id,
            "reason": "停止最后一个线程任务",
        }
    )

    assert second.ok is True
    assert interrupted == ["my-agent-launch-shared"]


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
