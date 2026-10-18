# LLM: 只使用原C生命周期、真实进程出生和规范控制回执；分类/JSON不签发业务许可。
# 模块用途: 验证控制producer原子冻结、claimed校验与精确目标投影，不物化远端owner。
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.gateway_parts import control_operation_service as ops
from agent_py_agent.agent.gateway_parts import multiprocess_control_forward as forward
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.daemon_metadata import process_host_id
from agent_py_agent.agent.gateway_parts.execution_target import GatewayExecutionTarget
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import (
    bind_coordinator_generation,
    coordinator_guard,
)
from agent_py_agent.agent.gateway_parts.multiprocess_executor_identity import process_birth
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity


# LLM: 原C生产者写真实prepared回执，目标为显式合成typed值；不构造W或授予P3。
# 函数用途: 为纯回执边界提供独立临时控制及真实进程出生。
def remote_case(tmp_path):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    target = GatewayExecutionTarget("thread-a", "task", "turn-a", "claim-a", task_id="turn-a")
    scope = GatewayControlScope("alice", "feishu", "room-a",
        {"message_id": "control-a", "expected_turn_id": "turn-a",
         "expected_execution": target.to_dict()},
        resolved_owner=OwnerIdentity.provider_user("feishu", "alice"))
    with coordinator_guard(paths.root, 2):
        bind_coordinator_generation(paths.root, 2, "control-generation")
        command = parse_conversation_control("/stop")
        receipt = ops.execute_gateway_control_operation(
            SimpleNamespace(), paths, command, scope, command_text="/stop")
    identity = {"schema_version": "multiprocess_process_identity.v1", "host_id": process_host_id(),
                "pid": os.getpid(), "start_time": process_birth(os.getpid())}
    return paths, receipt, target, identity


@pytest.mark.parametrize("state", ["claimed", "executing", "completed"])
def test_dispatch_complete_executor_roundtrips(tmp_path, state):
    _, receipt, target, identity = remote_case(tmp_path)
    route = {**receipt.dispatch, "executor": identity, "instance_id": "worker-instance"}
    result = {"kind": receipt.command_kind, "ok": True, "message": "合成回执"} if state == "completed" else {}
    claimed = replace(receipt, state=state, dispatch=route, result=result)
    restored = ops.GatewayControlOperationReceipt.from_dict(claimed.to_dict())
    assert restored == claimed
    scope = ops._receipt_scope(restored, dispatched_execution=True)
    assert scope.metadata["expected_execution"] == target.to_dict()
    assert scope.agents_ingress is scope.memory_ingress is None


@pytest.mark.parametrize("damage", ["missing", "pid_only", "bool_pid", "missing_instance"])
def test_claimed_requires_original_complete_process(tmp_path, damage):
    _, receipt, _, identity = remote_case(tmp_path)
    route = {**receipt.dispatch, "executor": identity, "instance_id": "worker-instance"}
    if damage == "missing":
        route.pop("executor")
    elif damage == "pid_only":
        route["executor"] = {"pid": os.getpid()}
    elif damage == "bool_pid":
        route["executor"] = {**identity, "pid": True}
    else:
        route.pop("instance_id")
    with pytest.raises(DataCorruptionError):
        ops.GatewayControlOperationReceipt.from_dict(replace(receipt, state="claimed", dispatch=route).to_dict())


def test_claimed_without_dispatch_is_rejected(tmp_path):
    _, receipt, _, _ = remote_case(tmp_path)
    with pytest.raises(DataCorruptionError):
        ops.GatewayControlOperationReceipt.from_dict(replace(receipt, state="claimed", dispatch=None).to_dict())


@pytest.mark.parametrize("damage", ["target", "partition", "prepared_executor"])
def test_self_consistent_dispatch_cannot_change_original_target(tmp_path, damage):
    _, receipt, target, identity = remote_case(tmp_path)
    route = dict(receipt.dispatch)
    if damage == "target":
        route["execution"] = replace(target, claim_id="another-claim").to_dict()
    elif damage == "partition":
        route["worker"] = 1 - route["worker"]
    else:
        route.update(executor=identity, instance_id="worker-instance")
    route["binding"] = forward.control_dispatch_binding(receipt, route)
    with pytest.raises(DataCorruptionError):
        ops.GatewayControlOperationReceipt.from_dict(replace(receipt, dispatch=route).to_dict())


def test_v4_null_target_presence_survives_restoration(tmp_path):
    _, receipt, _, _ = remote_case(tmp_path)
    scope = ops._receipt_scope(receipt)
    scope = replace(scope, metadata={**scope.metadata, "expected_execution": None})
    changed = replace(receipt, expected_execution=None, dispatch=None,
        input_digest=ops._control_operation_input_digest(scope, receipt.command_text, version=4))
    restored = ops.GatewayControlOperationReceipt.from_dict(changed.to_dict())
    assert "expected_execution" in ops._receipt_scope(restored).metadata
    assert ops._receipt_scope(restored).metadata["expected_execution"] is None
