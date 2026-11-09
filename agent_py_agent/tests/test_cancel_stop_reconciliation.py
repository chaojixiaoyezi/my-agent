"""取消执行产生的结构化诊断保留；诊断不签发恢复许可。"""


def test_cancel_receipt_persisted_before_cleanup_and_archived_without_preview(
    tmp_path, monkeypatch
):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.subagents import cancellation
    from agent_py_agent.agent.subagents.services.base import CreateRunParams
    from agent_py_agent.tests.test_orchestration_cancel_subagents_tool import (
        _execute_cancel_subagents,
    )

    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path
    )
    task = agent.subagents.create_run(
        params=CreateRunParams(
            goal="stop", thought="test", plan=["run"], allowed_tools=["read_file"]
        )
    )
    original = cancellation.cleanup_subagent_stops
    seen = []

    def check(batch):
        frozen = agent.subagents.load(task.id).attributes["cancel_subagents"]["stop_receipt"]
        assert frozen["run_id"] == task.id and frozen["schema"] == "runner-stop.v1"
        seen.append(frozen)
        return original(batch)

    monkeypatch.setattr(cancellation, "cleanup_subagent_stops", check)
    result = _execute_cancel_subagents(agent, {"run_id": task.id})
    assert result.ok and len(seen) == 1
    assert result.metadata["handler_details"]["stop_receipts"]["records"][0]["run_id"] == task.id


def test_unknown_wrapper_keeps_structured_receipt_outside_truncated_preview(tmp_path, monkeypatch):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.subagents import cancellation
    from agent_py_agent.agent.subagents.cancellation_hosts import FrozenRunnerStop
    from agent_py_agent.agent.subagents.services.base import CreateRunParams
    from agent_py_agent.tests.test_orchestration_cancel_subagents_tool import (
        _execute_cancel_subagents,
    )

    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path
    )
    task = agent.subagents.create_run(
        params=CreateRunParams(
            goal="stop", thought="test", plan=["run"], allowed_tools=["read_file"]
        )
    )
    monkeypatch.setattr(
        cancellation,
        "freeze_runner_stop",
        lambda *args, **kwargs: FrozenRunnerStop(
            {"status": "cooperative_requested", "pid": 12345}, 12345, "birth"
        ),
    )
    monkeypatch.setattr(
        cancellation,
        "cleanup_runner_stop",
        lambda *args: {
            "status": "cooperative_requested",
            "host_exited": False,
            "identity_confirmed": False,
        },
    )
    result = _execute_cancel_subagents(agent, {"run_id": task.id})
    assert result.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    frozen = result.metadata["handler_details"]["stop_receipts"]
    assert frozen["purpose"] == "diagnostic_only"
    assert frozen["records"][0]["run_id"] == task.id
    assert frozen["records"][0]["runner_confirmed"] is False
    assert frozen["failure_codes"] == ["RUNNER_WORKER_EXIT_UNCONFIRMED"]
