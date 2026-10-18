# LLM: 只传递原C准入上下文和原claim；本地工具登记沿同一原T/P3/P5，不建请求/租约，不借W身份，改动同步C执行/P1合同。
# 模块用途: C认领后的owner/模型/工具检查沿同一冻结代次和原预留，安装本地主体工具登记，默认单进程无磁盘读。
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from ..user_space.owner_resolver import OwnerIdentity
from .multiprocess_admission import (
    _read_reservation,
    _runtime_context_rejection,
    _runtime_owner_rejection,
    reservation_file,
    verify_claimable_reservation,
)
from .multiprocess_dispatch import WorkerDispatchError
from .multiprocess_execution_lifetime import ExecutionScopeLifetime
from .multiprocess_route import validate_coordinator_route

_EXECUTION = ContextVar("gateway_coordinator_execution", default=None)


# LLM: 包装引用原claim/lifetime及可选自产child绑定；默认None仍只认gate.agent，child须原dispatch/ref/实际对象，退出不授执行。
# 类用途: 保留准确C认领与实际Agent，区分原main和本次已验证child而不改协调者身份。
@dataclass(frozen=True)
class CoordinatorExecution:
    gate: object
    claim: object
    agent: object
    creator_pid: int
    lifetime: ExecutionScopeLifetime | None = None
    child_binding: object | None = None


# LLM: 原C区间安装工具/短写工厂，可选child绑定只接受本次自产actualexecution一次；原claim不变，finally失效，默认None/count0无新I/O。
# 函数用途: 将原C认领带到路由/模型/工具/P1及实际全局短写，安装本身不产生账本或进程。
@contextmanager
def coordinator_execution_scope(gate, claim, agent, *, child_binding=None):
    from ..common.tool_write_context import tool_write_factory_scope
    from .multiprocess_coordinator_executor_scope import coordinator_request_executor_scope
    from .multiprocess_tool_global_write import CoordinatorToolWriteFactory

    lifetime = ExecutionScopeLifetime() if gate is not None and gate.count > 0 else None
    execution = CoordinatorExecution(gate, claim, agent, os.getpid(), lifetime, child_binding) if gate is not None else None
    if lifetime is not None:
        lifetime.bind_original(execution)
    if child_binding is not None:
        from .multiprocess_child_execution_binding import ChildExecutionBinding

        try:
            if type(child_binding) is not ChildExecutionBinding or execution is None or lifetime is None:
                raise WorkerDispatchError("coordinator_agent_binding_required", "agent")
            child_binding.bind_original(execution)
        except Exception:
            if lifetime is not None:
                lifetime.close()
            raise
    token = _EXECUTION.set(execution)
    try:
        factory = CoordinatorToolWriteFactory(execution).capture if execution is not None and gate.count > 0 else None
        with coordinator_request_executor_scope(execution), tool_write_factory_scope(factory):
            yield
    finally:
        if lifetime is not None:
            lifetime.close()
        _EXECUTION.reset(token)


# LLM: 返回对象只来自当前线程上下文；异步回调须显式捕获它，不靠线程隐式继承。
# 函数用途: 取得原C的本次执行上下文。
def current_coordinator_execution():
    return _EXECUTION.get()


# LLM: 原T实读先核同claim/P3/P5，actualAgent仅原main或自产child绑定；活token/outer source须准确，不刷新租约或补权。
# 函数用途: 在owner物化和模型/工具前拒绝退出scope、C代次/路由及原claim漂移。
def check_coordinator_transition(execution, payload):
    if execution is None:
        return None
    if type(execution) is not CoordinatorExecution:
        raise WorkerDispatchError("execution_context_required", "execution")
    gate, claim, pid = execution.gate, execution.claim, execution.creator_pid
    if gate.count > 0:
        try:
            if type(execution.lifetime) is not ExecutionScopeLifetime:
                raise ValueError('execution_scope_required')
            execution.lifetime.require_original(execution)
        except ValueError as error:
            raise WorkerDispatchError('execution_scope_unavailable', 'execution') from error
    if pid != os.getpid() or _runtime_context_rejection(gate) is not None:
        raise WorkerDispatchError("coordinator_generation_unavailable", "coordinator")
    checked = validate_coordinator_route(payload, gate.count, gate.c_run_id)
    if not checked["ok"]:
        raise WorkerDispatchError(checked["reason"], "route")
    expected = {"id": claim.request_id, "execution_attempt_id": claim.execution_attempt_id,
                "lease_epoch": claim.lease_epoch, "lease_owner": claim.lease_owner}
    if claim.path != gate.paths.processing / (claim.request_id + ".json") or any(
            type(payload.get(key)) is not type(value) or payload[key] != value for key, value in expected.items()):
        raise WorkerDispatchError("claim_fence_mismatch", "claim")
    from .multiprocess_coordinator_claim import CoordinatorClaimSnapshot
    from .multiprocess_records import deserialize_record, serialize_record

    snapshot = claim.coordinator_snapshot
    if type(snapshot) is not CoordinatorClaimSnapshot or snapshot.gate is not gate:
        raise WorkerDispatchError("coordinator_claim_snapshot_required", "snapshot")
    original = verify_claimable_reservation(payload, deserialize_record(snapshot.reservation_bytes))
    if not original["ok"]:
        raise WorkerDispatchError("claim_snapshot_changed", "snapshot")
    reservation = _read_reservation(reservation_file(gate.paths, claim.request_id))
    checked = verify_claimable_reservation(payload, reservation)
    if not checked["ok"]:
        raise WorkerDispatchError(checked["reason"], "reservation")
    if serialize_record(reservation) != snapshot.reservation_bytes:
        raise WorkerDispatchError("claim_snapshot_changed", "reservation")
    owner = OwnerIdentity.local_main()
    if _runtime_owner_rejection(gate, owner) is not None:
        raise WorkerDispatchError("owner_partition_quarantined", "owner")
    require_coordinator_execution_agent(execution, payload=payload)
    return owner


# LLM: 物理read/finish仅静态自产身份；payload仅原T门内提供时才重核child活性/ref。默认None只认原gate.agent，不从home/role猜许可。
# 函数用途: 让原C P1和工具短写共同识别准确main或本次实际child来源。
def require_coordinator_execution_agent(execution, *, payload=None):
    if execution.agent is execution.gate.agent and execution.child_binding is None:
        return execution.agent
    from .multiprocess_child_execution_binding import ChildExecutionBinding

    binding = execution.child_binding
    try:
        if type(binding) is not ChildExecutionBinding:
            raise ValueError("original_child_execution_binding_required")
        binding.require_identity(execution)
        if payload is not None:
            binding.require_claim(execution, payload)
    except ValueError as error:
        raise WorkerDispatchError("coordinator_agent_binding_invalid", "agent") from error
    return execution.agent
