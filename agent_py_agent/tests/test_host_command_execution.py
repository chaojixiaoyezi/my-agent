"""显式宿主执行的原账收口；假工具、真实临时 SQLite，不运行模型。"""

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from agent_py_agent.agent.runtime_db import host_command_execution as execution
from agent_py_agent.agent.runtime_db.host_commands import HostCommandIdentity, HostCommandRequest
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.tooling.executor import ToolExecutor, ToolExecutorRequest
from agent_py_agent.agent.tooling.models import (
    ToolExposure,
    ToolHandlerOutcome,
    ToolInputPolicy,
    ToolRuntime,
    ToolRuntimeSnapshot,
)
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall, tool_arguments_hash
from agent_py_agent.tests._tool_runtime_harness import make_test_model_spec
from agent_py_agent.tests.test_host_command_operation_replay import _CountingTool


# LLM: 隐藏假工具仍须走真实执行器和原操作账；prepare 故意不提供 Store，由生产宿主适配固定它。
# 函数用途: 构造相同请求的可重复提交参数，计数可直接证明 handler 是否重跑。
def case(tmp_path):
    repo = RuntimeRepository(tmp_path / "runtime.db")
    tool = _CountingTool()
    request = HostCommandRequest("owner-a", "actor-a", "chat", "thread-a", "request-a",
                                 tool.model_spec.name, tool_arguments_hash({}).removeprefix("sha256:"))

    def prepare(binding):
        runtime = ToolRuntime(tool.model_spec, tool.runtime_policy, tool, exposure=ToolExposure(model_visible=False))
        snapshot = ToolRuntimeSnapshot(binding.run_id, (runtime,), frozenset({tool.model_spec.name}), (), None)
        call = ToolCall("call-a", tool.model_spec.name, {}, "native", tool.model_spec.schema_hash,
                        binding.run_id, "turn-a", binding.attempt_id, operation_id=request.operation_id)
        return ToolExecutorRequest(call, snapshot, tmp_path, operation_owner_id=request.owner_id,
                                   operation_store=None, operation_store_required=False)
    return repo, request, tool, prepare


def test_hidden_tool_keeps_model_gate_and_host_owns_original_store(tmp_path):
    repo, request, tool, prepare = case(tmp_path)
    binding = repo.register_host_command(request)
    assert ToolExecutor().execute(prepare(binding)).result.error_code == "TOOL_NOT_MODEL_VISIBLE"
    first = execution.execute_host_command(repo, request, prepare)
    assert first["state"] == "succeeded", first
    assert tool.calls == 1
    assert execution.execute_host_command(repo, request, prepare)["state"] == "succeeded"
    assert tool.calls == 1
    assert repo.get_attempt(binding.attempt_id)["status"] == "done"
    assert len(repo.attempts_for_run(binding.agent_run_id)) == 1


def test_preparation_failure_records_unstarted_and_releases_execution_lock(tmp_path):
    repo, request, tool, _prepare = case(tmp_path)
    result = execution.execute_host_command(repo, request, lambda binding: (_ for _ in ()).throw(ValueError("test")))
    assert result["state"] == "rejected" and result["error_code"] == "HOST_COMMAND_PREPARATION_FAILED"
    assert tool.calls == 0
    binding = repo.find_host_command(request)
    assert repo.get_attempt(binding.attempt_id)["status"] == "failed"
    with repo._runtime_connection() as conn:
        assert conn.execute("SELECT count(*) FROM resource_locks").fetchone()[0] == 0


def test_concurrent_duplicate_and_query_observe_live_execution_without_reentering(tmp_path, monkeypatch):
    repo, request, tool, prepare = case(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = tool.execute

    def wait(params):
        entered.set()
        assert release.wait(5)
        return original(params)

    monkeypatch.setattr(tool, "execute", wait)
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(execution.execute_host_command, repo, request, prepare)
        try:
            assert entered.wait(5)
            assert execution.query_host_command(repo, request)["state"] == "running"
            assert execution.execute_host_command(repo, request, prepare)["state"] == "running"
        finally:
            release.set()
        assert future.result(timeout=5)["state"] == "succeeded"
    assert tool.calls == 1


def test_unknown_effect_keeps_original_attempt_and_does_not_reexecute(tmp_path, monkeypatch):
    repo, request, tool, prepare = case(tmp_path)
    calls = []

    def uncertain(params):
        calls.append(params)
        return ToolHandlerOutcome(tool.model_spec.name, False, "未知", effect_outcome="unknown",
                                  error_code="TOOL_EXECUTION_FAILED")

    monkeypatch.setattr(tool, "execute", uncertain)
    result = execution.execute_host_command(repo, request, prepare)
    assert result["state"] == "outcome_unknown", result
    assert result["attempt_status"] == "unknown"
    assert execution.execute_host_command(repo, request, prepare)["state"] == "outcome_unknown"
    assert len(calls) == 1
    with repo._runtime_connection() as conn:
        assert conn.execute("SELECT count(*) FROM resource_locks").fetchone()[0] > 0


@pytest.mark.parametrize("fault", ["operation_read", "agent_settle", "task_settle"])
def test_known_operation_can_finish_original_run_after_transient_failure(tmp_path, monkeypatch, fault):
    repo, request, tool, prepare = case(tmp_path)
    target, name = ((execution, "_operation") if fault == "operation_read" else
                    (repo, "settle_agent_run" if fault == "agent_settle" else "settle_task_run_if_agent_tree_terminal"))
    original, calls = getattr(target, name), []

    def fail_once(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise OSError("injected persistence failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(target, name, fail_once)
    try:
        execution.execute_host_command(repo, request, prepare)
    except OSError:
        pass
    assert execution.execute_host_command(repo, request, prepare)["state"] == "succeeded"
    binding = repo.find_host_command(request)
    assert repo.get_attempt(binding.attempt_id)["status"] == "done"
    assert repo.get_task_run(binding.task_run_id)["closed_at"] > 0
    assert tool.calls == 1


def test_parameter_completion_cannot_change_frozen_command_input(tmp_path):
    repo, request, tool, prepare = case(tmp_path)
    tool.model_spec = make_test_model_spec(tool.model_spec.name, input_schema={
        "type": "object", "properties": {"value": {"type": "integer"}}, "additionalProperties": False,
    })
    tool.runtime_policy = replace(tool.runtime_policy, input_policy=ToolInputPolicy(safe_parameter_defaults=(("value", 1),)))
    result = execution.execute_host_command(repo, request, prepare)
    assert result["state"] == "rejected" and result["error_code"] == "TOOL_INVALID_ARGUMENTS"
    assert tool.calls == 0


def test_normal_cancel_event_is_not_forged_into_unstarted_receipt(tmp_path):
    repo, request, _tool, _prepare = case(tmp_path)
    binding = repo.register_host_command(request)
    repo.settle_agent_run(agent_run_id=binding.agent_run_id, attempt_id=binding.attempt_id, status="cancelled")
    assert execution.query_host_command(repo, request)["state"] == "outcome_unknown"


def test_unstarted_event_can_finish_original_task_after_close_failure(tmp_path, monkeypatch):
    repo, request, tool, prepare = case(tmp_path)
    original = repo.settle_task_run_if_agent_tree_terminal
    monkeypatch.setattr(repo, "settle_task_run_if_agent_tree_terminal",
                        lambda **kw: (_ for _ in ()).throw(OSError("close unavailable")))
    with pytest.raises(OSError):
        execution.execute_host_command(repo, request, lambda binding: (_ for _ in ()).throw(ValueError("prepare")))
    result = execution.query_host_command(repo, request)
    assert result["state"] == "rejected" and result["finalization_pending"]
    monkeypatch.setattr(repo, "settle_task_run_if_agent_tree_terminal", original)
    result = execution.execute_host_command(repo, request, prepare)
    assert result["state"] == "rejected" and not result["finalization_pending"]
    assert tool.calls == 0


def test_query_identity_is_scoped_and_repository_is_readonly(tmp_path, monkeypatch):
    repo, request, _tool, prepare = case(tmp_path)
    execution.execute_host_command(repo, request, prepare)
    monkeypatch.setattr(RuntimeRepository, "_init_runtime_schema", lambda *a: pytest.fail("查询不能初始化"))
    readonly = RuntimeRepository(repo.db_path, read_only=True)
    identity = HostCommandIdentity(request.owner_id, request.actor_id, request.channel, request.thread_id, request.request_id)
    assert execution.query_host_command(readonly, identity)["state"] == "succeeded"
    assert execution.query_host_command(readonly, replace(identity, actor_id="other"))["state"] == "not_found"
    with pytest.raises(Exception, match="readonly"):
        with readonly.transaction() as conn:
            conn.execute("DELETE FROM runtime_events")
