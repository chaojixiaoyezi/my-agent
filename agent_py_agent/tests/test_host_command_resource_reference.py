"""原操作引用与资源读取的开发合同，真实临时 RuntimeDB，不启动模型或产品 TUI。"""

import json
import time

import pytest

from agent_py_agent.agent.local_storage.tool_operations import (
    ToolOperationClaimRequest,
    ToolOperationStateError,
    new_tool_operation_holder,
)
from agent_py_agent.agent.runtime_db.host_commands import HostCommandRequest
from agent_py_agent.agent.runtime_db.managed_operation_store import ManagedOperationStore
from agent_py_agent.agent.runtime_db.repository import RuntimeConflictError, RuntimeRepository


@pytest.mark.parametrize("scopes", [(), ("logical:z", "logical:a"), ("logical:a", "logical:a", "logical:z")])
def test_read_original_claim_matches_resource_set_without_rewriting_history(tmp_path, scopes):
    repo = RuntimeRepository(tmp_path / "runtime.db")
    request = HostCommandRequest("owner", "actor", "chat", "thread", "request", "component", "a" * 64)
    binding = repo.register_host_command(request)
    repo.create_attempt(binding.agent_run_id, reuse_pending=True, reject_running=True,
                        expected_pending_attempt_id=binding.attempt_id)
    store = ManagedOperationStore(repo)
    store.claim_tool_operation(ToolOperationClaimRequest(
        owner_id=request.owner_id, run_id=binding.run_id, task_id=binding.task_id,
        operation_id=request.operation_id, tool=request.command_name, args_hash="sha256:" + request.input_digest,
        idempotency_key=request.operation_id, idempotency_scope="operation", idempotency_namespace="component",
        holder=new_tool_operation_holder(), lease_expires_at=time.time() + 30,
        resource_scopes=scopes, attempt_id=binding.attempt_id,
    ))
    args = dict(owner_id=request.owner_id, run_id=binding.run_id, task_id=binding.task_id,
                attempt_id=binding.attempt_id, operation_id=request.operation_id,
                tool_name=request.command_name, args_hash="sha256:" + request.input_digest)
    with repo._runtime_connection() as conn:
        original = conn.execute("SELECT outcome_json FROM tool_operations").fetchone()[0]
    assert store.get_tool_operation(**args, resource_scopes=scopes)
    assert store.get_tool_operation(**args, resource_scopes=tuple(reversed(scopes)))
    with pytest.raises(ToolOperationStateError):
        store.get_tool_operation(**args, resource_scopes=(*scopes, "logical:other"))
    if scopes:
        with pytest.raises(ToolOperationStateError):
            store.get_tool_operation(**args, resource_scopes=())
    with repo._runtime_connection() as conn:
        assert conn.execute("SELECT outcome_json FROM tool_operations").fetchone()[0] == original
    for bad in (None, {}, "logical:a", [False], [""]):
        with repo.transaction() as conn:
            changed = json.loads(original)
            changed["resource_scopes"] = bad
            conn.execute("UPDATE tool_operations SET outcome_json=?", (json.dumps(changed),))
        with pytest.raises(ToolOperationStateError):
            store.get_tool_operation(**args, resource_scopes=scopes)


@pytest.mark.parametrize("damage", ["owner", "request", "event", "attempt", "schema", "json"])
def test_original_operation_reference_never_adopts_other_or_damaged_chain(tmp_path, damage):
    repo = RuntimeRepository(tmp_path / "runtime.db")
    request = HostCommandRequest("owner", "actor", "chat", "thread", "request", "component", "a" * 64)
    assert repo.find_host_command_by_operation(owner_id=request.owner_id, operation_id=request.operation_id) is None
    binding = repo.register_host_command(request)
    assert repo.find_host_command_by_operation(owner_id=request.owner_id, operation_id=request.operation_id).attempt_id == binding.attempt_id
    with repo.transaction() as conn:
        if damage == "owner":
            conn.execute("UPDATE tasks SET owner_id='other'")
        elif damage == "event":
            conn.execute("UPDATE runtime_events SET event_type='other' WHERE event_id=?", (request.operation_id,))
        elif damage == "attempt":
            conn.execute("UPDATE agent_attempts SET attempt_generation=2")
        else:
            value = {"host_command": request.to_payload()}
            if damage == "request":
                value["host_command"]["request_id"] = "other"
            if damage == "schema":
                value["host_command"]["schema_version"] = "future"
            conn.execute("UPDATE task_runs SET metadata_json=?", ("bad-json" if damage == "json" else json.dumps(value),))
    with pytest.raises(RuntimeConflictError):
        repo.find_host_command_by_operation(owner_id=request.owner_id, operation_id=request.operation_id)
