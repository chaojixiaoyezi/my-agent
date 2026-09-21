"""原 ToolExecutor 与 RuntimeDB 的宿主命令重放；仅假工具，不调用模型或产品 TUI。"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.local_storage import ToolOperationStateError
from agent_py_agent.agent.runtime_db.executor_liveness import (
    attempt_executor,
    exited_attempt_facts,
    mark_exited_attempt_unknown,
)
from agent_py_agent.agent.runtime_db.host_commands import HostCommandRequest
from agent_py_agent.agent.runtime_db.managed_operation_store import ManagedOperationStore
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.tooling.models import BaseTool, ToolHandlerOutcome
from agent_py_agent.agent.tooling.runtime_contracts import tool_arguments_hash
from agent_py_agent.agent.tooling.tool_operation_coordinator import replay_completed_tool_operation
from agent_py_agent.tests._tool_runtime_harness import (
    execute_canonical_test_call,
    make_test_model_spec,
    make_test_runtime_policy,
)


# LLM: 仅测试原执行器是否调用副作用 handler；不模拟权限裁决或自建操作表。
# 类用途: 记录一次假副作用调用，供结束后只读重放与未知结果测试使用。
class _CountingTool(BaseTool):
    # LLM: 声明真实 mutating 策略，令原协调器登记操作；计数只留测试对象内。
    # 函数用途: 创建不访问文件、进程或网络的假工具。
    def __init__(self):
        self.calls = 0
        self.model_spec = make_test_model_spec("manage_sample")
        self.runtime_policy = make_test_runtime_policy("mutating")

    # LLM: 此 handler 只能由原执行器进入，返回结构化结果供原操作账保存。
    # 函数用途: 增加测试计数并返回可核对的成功回执。
    def execute(self, params):
        self.calls += 1
        return ToolHandlerOutcome(tool=self.model_spec.name, ok=True, output="已执行假工具")


@pytest.fixture
def executed(tmp_path):
    repo = RuntimeRepository(tmp_path / "runtime.db")
    request = HostCommandRequest("owner-a", "actor-a", "cli", "thread-a", "message-a", "install", "a" * 64)
    binding = repo.register_host_command(request)
    repo.create_attempt(binding.agent_run_id, reuse_pending=True, reject_running=True,
                        expected_pending_attempt_id=binding.attempt_id)
    tool = _CountingTool()
    store = ManagedOperationStore(repo)
    with attempt_executor(repo, binding.run_id, binding.attempt_id):
        execution = execute_canonical_test_call(
            tmp_path, tools={tool.model_spec.name: tool}, tool_name=tool.model_spec.name, arguments={},
            run_id=binding.run_id, attempt_id=binding.attempt_id,
            operation_id=request.operation_id, operation_owner_id=request.owner_id,
            operation_store=store, operation_store_required=True,
            trusted_run_context={"run_scope": {"task_id": binding.task_id}},
        )
        assert execution.result.ok
    query = dict(owner_id=request.owner_id, run_id=binding.run_id, task_id=binding.task_id,
                 attempt_id=binding.attempt_id, operation_id=request.operation_id,
                 tool_name=tool.model_spec.name, args_hash=tool_arguments_hash({}))
    assert store.get_tool_operation(**query).status == "succeeded"
    return repo, binding, tool, query


def test_terminal_run_replays_original_operation_without_execution(executed):
    repo, binding, tool, query = executed
    assert repo.settle_agent_run(agent_run_id=binding.agent_run_id, attempt_id=binding.attempt_id, status="done")["settled"]
    assert repo.settle_task_run_if_agent_tree_terminal(task_run_id=binding.task_run_id, task_id=binding.task_id)["settled"]
    fresh = RuntimeRepository(repo.db_path)
    assert not fresh.register_host_command(binding.request).created
    record = ManagedOperationStore(fresh).get_tool_operation(**query)
    result = replay_completed_tool_operation(record)
    assert result.ok and result.output == "已执行假工具"
    assert result.result_envelope["tool_operation"]["replayed"] is True
    assert result.result_envelope["tool_operation"]["action"] == "replay"
    assert result.result_envelope["tool_operation"]["original_tool_execution"]["handler_executed"] is True
    assert result.handler_executed is False
    assert record.result["handler_executed"] is True
    assert tool.calls == 1
    assert len(fresh.attempts_for_run(binding.agent_run_id)) == 1
    assert fresh.get_attempt(binding.attempt_id)["status"] == "done"
    assert not fresh.has_active_exec_lock(binding.agent_run_id)


def test_original_three_parameter_query_returns_canonical_identity(executed):
    repo, binding, _, query = executed
    record = ManagedOperationStore(repo).get_tool_operation(
        owner_id=query["owner_id"], run_id=query["run_id"], operation_id=query["operation_id"],
    )
    assert (record.owner_id, record.run_id, record.task_id) == (binding.request.owner_id, binding.run_id, binding.task_id)


@pytest.mark.parametrize("field", ["owner_id", "run_id", "task_id", "attempt_id", "tool_name", "args_hash"])
def test_operation_read_rejects_wrong_canonical_identity_or_input(executed, field):
    repo, _, tool, query = executed
    with pytest.raises(ToolOperationStateError):
        ManagedOperationStore(repo).get_tool_operation(**{**query, field: "wrong"})
    assert tool.calls == 1


@pytest.mark.parametrize("damage", [
    "DELETE FROM tasks", "DELETE FROM task_runs", "DELETE FROM agent_attempts",
    "UPDATE agent_attempts SET attempt_generation=2", "UPDATE agent_runs SET task_run_id='wrong'",
])
def test_operation_missing_original_chain_is_not_reported_absent(executed, damage):
    repo, _, _, query = executed
    with repo.transaction() as conn:
        conn.execute(damage)
    with pytest.raises(ToolOperationStateError):
        ManagedOperationStore(repo).get_tool_operation(**query)


def test_only_truly_absent_operation_returns_none(executed):
    repo, _, _, query = executed
    assert ManagedOperationStore(repo).get_tool_operation(**{**query, "operation_id": "never-created"}) is None


@pytest.mark.parametrize("state", ["running", "unknown"])
def test_unsettled_record_never_replays_success(executed, state):
    repo, _, tool, query = executed
    original = ManagedOperationStore(repo).get_tool_operation(**query)
    result = replay_completed_tool_operation(replace(original, status=state))
    assert not result.ok and result.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert tool.calls == 1


@pytest.mark.parametrize("damage", ["missing_schema", "wrong_tool", "wrong_success"])
def test_corrupt_saved_result_cannot_claim_terminal_success(executed, damage):
    repo, _, _, query = executed
    original = ManagedOperationStore(repo).get_tool_operation(**query)
    payload = dict(original.result)
    if damage == "missing_schema":
        payload.pop("schema_version")
    elif damage == "wrong_tool":
        payload["tool"] = "another_tool"
    else:
        payload["ok"] = False
    result = replay_completed_tool_operation(replace(original, result=payload))
    assert not result.ok and result.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"


@pytest.mark.parametrize("field", ["tool", "output", "ok", "handler_executed"])
@pytest.mark.parametrize("change", ["missing", "wrong_type"])
def test_truncated_or_untyped_core_result_is_unknown(executed, field, change):
    repo, _, _, query = executed
    original = ManagedOperationStore(repo).get_tool_operation(**query)
    payload = dict(original.result)
    if change == "missing":
        payload.pop(field)
    else:
        payload[field] = 1
    result = replay_completed_tool_operation(replace(original, result=payload))
    assert not result.ok and result.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"


@pytest.mark.parametrize("state", ["failed", "cancelled"])
def test_valid_negative_terminal_replay_keeps_failure_and_never_executes(executed, state):
    repo, _, tool, query = executed
    original = ManagedOperationStore(repo).get_tool_operation(**query)
    payload = {"schema_version": "tool_execution_result.v1", "tool": original.tool, "ok": False,
               "output": "执行前取消" if state == "cancelled" else "参数拒绝", "handler_executed": False,
               "effect_outcome": "not_started", "error_code": "TOOL_OPERATION_CANCELLED_NOT_STARTED"}
    record = replace(original, status=state, result=payload)
    result = replay_completed_tool_operation(record)
    assert not result.ok and result.error_code != "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert result.effect_outcome == "not_started" and result.handler_executed is False
    assert result.result_envelope["tool_operation"]["action"] == "replay"
    assert tool.calls == 1
    assert record.result == payload


@pytest.mark.parametrize("effect", ["unknown", " UNKNOWN ", "Unknown", "FAILED"])
def test_failed_operation_with_unknown_effect_is_not_a_definite_failure(executed, effect):
    repo, _, _, query = executed
    original = ManagedOperationStore(repo).get_tool_operation(**query)
    payload = {**original.result, "ok": False, "effect_outcome": effect}
    result = replay_completed_tool_operation(replace(original, status="failed", result=payload))
    assert not result.ok and result.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert result.handler_executed is False


def test_cancelled_but_executed_record_is_not_replayed(executed):
    repo, _, _, query = executed
    original = ManagedOperationStore(repo).get_tool_operation(**query)
    payload = {**original.result, "ok": False, "effect_outcome": "not_started", "handler_executed": True}
    result = replay_completed_tool_operation(replace(original, status="cancelled", result=payload))
    assert result.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN" and not result.handler_executed


def test_unknown_error_in_terminal_record_cannot_inherit_previous_execution(executed):
    repo, _, _, query = executed
    original = ManagedOperationStore(repo).get_tool_operation(**query)
    payload = {**original.result, "ok": False, "effect_outcome": "failed",
               "error_code": "TOOL_OPERATION_OUTCOME_UNKNOWN"}
    result = replay_completed_tool_operation(replace(original, status="failed", result=payload))
    assert result.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN" and not result.handler_executed
    assert result.result_envelope.get("tool_operation", {}).get("action") != "executed"


def test_replay_does_not_mutate_nested_original_receipt(executed):
    repo, _, _, query = executed
    original = ManagedOperationStore(repo).get_tool_operation(**query)
    payload = {**original.result, "result_envelope": {"delivery_evidence": {"deduplicated": False}}}
    result = replay_completed_tool_operation(replace(original, result=payload))
    assert result.result_envelope["delivery_evidence"]["deduplicated"] is True
    assert payload["result_envelope"]["delivery_evidence"]["deduplicated"] is False


def test_exited_gateway_thread_with_unsettled_effect_stays_unknown(executed):
    repo, binding, tool, query = executed
    # 模拟 handler 完成后结果提交丢失；执行器退出事实来自上面的真实 contextmanager。
    with repo.transaction() as conn:
        conn.execute("UPDATE tool_operations SET status='EXECUTING', settled_at=0 WHERE operation_id=?",
                     (binding.request.operation_id,))
    facts = exited_attempt_facts(repo, binding.run_id, binding.attempt_id)
    assert facts and facts["uncertain_effects"]
    mark_exited_attempt_unknown(repo, facts)
    assert repo.get_attempt(binding.attempt_id)["status"] == "unknown"
    assert repo.has_active_exec_lock(binding.agent_run_id)
    replay = repo.register_host_command(binding.request)
    assert not replay.created and replay.attempt_id == binding.attempt_id
    assert not repo.settle_task_run_if_agent_tree_terminal(task_run_id=binding.task_run_id)["settled"]
    result = replay_completed_tool_operation(ManagedOperationStore(repo).get_tool_operation(**query))
    assert not result.ok and result.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert len(repo.attempts_for_run(binding.agent_run_id)) == 1 and tool.calls == 1
    metadata = json.loads(repo.get_attempt(binding.attempt_id)["metadata_json"])
    assert metadata["executor"]["status"] == "exited"
