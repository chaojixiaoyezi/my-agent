# LLM: 只在进程内运输原P5绑定和实际Agent；不新增请求、租约、身份登记或恢复权限。
# 模块用途: 让原模型/工具的T锁检查点保留worker执行归属，跨线程回调也不能改绑。
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from .multiprocess_execution_lifetime import ExecutionScopeLifetime


# LLM: Agent引用核真实home，lifetime只限原same-object作用域活性；PID/fork/退出拒旧上下文，不授新P3/P5或物理收尾许可。
# 类用途: 保存本次请求原绑定与正在使用的智能体。
@dataclass(frozen=True)
class WorkerExecution:
    binding: object
    agent: object
    creator_pid: int
    lifetime: ExecutionScopeLifetime | None = None


_EXECUTION = ContextVar("gateway_worker_execution", default=None)


# LLM: 原请求执行器创建/bind同源lifetime，finally失效再恢复上下文；模型设施线程可持同一对象，但scope退出后不能借processing欠账延寿。
# 函数用途: 把一个真实worker请求的归属贯穿到原执行链，不把它变成新权限来源。
@contextmanager
def worker_execution_scope(binding, agent):
    lifetime = ExecutionScopeLifetime()
    execution = WorkerExecution(binding, agent, os.getpid(), lifetime)
    lifetime.bind_original(execution)
    token = _EXECUTION.set(execution)
    try:
        yield
    finally:
        lifetime.close()
        _EXECUTION.reset(token)


# LLM: 默认单进程只有内存读取，无目录、配置或进程观察；C/W上下文都供原异步回调捕获。
# 函数用途: 在原回调构造时取得本次多进程执行绑定。
def current_worker_execution():
    from .multiprocess_coordinator_execution import current_coordinator_execution

    return _EXECUTION.get() or current_coordinator_execution()


# LLM: 原请求T锁内的实读检查；C与W分别核原上下文，捕获对象在跨线程回调也不丢身份。
# 函数用途: 阻止跨代、跨owner、fork旧上下文或漏绑定的进程提交模型和工具。
def check_worker_transition(execution, payload):
    from .multiprocess_coordinator_execution import (
        CoordinatorExecution,
        check_coordinator_transition,
    )
    from .multiprocess_mode import current_worker_partition

    if type(execution) is CoordinatorExecution and current_worker_partition() is None:
        check_coordinator_transition(execution, payload)
        return
    if execution is None and current_worker_partition() is None:
        return
    from .multiprocess_dispatch import WorkerDispatchError
    from .multiprocess_dispatch_binding import require_execution_agent

    if (type(execution) is not WorkerExecution or execution.creator_pid != os.getpid()
            or type(execution.lifetime) is not ExecutionScopeLifetime):
        raise WorkerDispatchError("execution_context_required", "execution")
    try:
        execution.lifetime.require_original(execution)
    except ValueError as error:
        raise WorkerDispatchError("execution_scope_unavailable", "execution") from error
    require_execution_agent(execution.binding, payload, execution.agent)


# LLM: 仅用于原W终态补交；拒绝C、漏上下文和fork复制，不放宽模型/工具活跃执行门。
# 函数用途: 核对closed终态的原执行身份和真实owner Agent。
def check_worker_terminal(execution, payload):
    from .multiprocess_dispatch import WorkerDispatchError
    from .multiprocess_dispatch_binding import require_terminal_execution_agent

    if type(execution) is not WorkerExecution or execution.creator_pid != os.getpid():
        raise WorkerDispatchError("execution_context_required", "execution")
    return require_terminal_execution_agent(execution.binding, payload, execution.agent)


# LLM: 进入时原T已持有；C先在生命周期门外核来源，最终许可及同步副作用共用原撤权/隔离门。
# 函数用途: 将已有模型/工具提交和C实际撤权串行，不延长租约，也不改变默认单进程行为。
def execute_worker_transition(execution, payload, operation):
    from ..user_space.owner_resolver import OwnerIdentity
    from .multiprocess_admission import _runtime_commit_scope
    from .multiprocess_coordinator_execution import CoordinatorExecution
    from .multiprocess_dispatch import WorkerDispatchError

    check_worker_transition(execution, payload)
    if type(execution) is CoordinatorExecution:
        with _runtime_commit_scope(execution.gate, OwnerIdentity.local_main()) as rejection:
            if rejection is not None:
                raise WorkerDispatchError(rejection["reason"], "coordinator")
            return operation()
    return operation()


# LLM: 消费原claim及P4池；C物化/finish在T外完成，ACK不授W写许可，消费前重新实读原fence，终态不释放P3。
# 函数用途: 把W认领的请求交给现有业务链，不重新创建请求、车道、执行代次或释放预留。
def execute_worker_claim(snapshot, claim, pool):
    from .io import read_json_file
    from .multiprocess_dispatch_binding import (
        bind_claimed_request,
        require_execution_agent,
        require_execution_binding,
    )
    from .paths import gateway_paths_from_root
    from .request_worker import _process_claimed_gateway_request_path

    payload = read_json_file(claim.path)
    binding = bind_claimed_request(snapshot, claim, payload)
    owner = require_execution_binding(binding, payload)
    native_client = getattr(pool, 'native_client', None)
    if native_client is not None:
        materialized = native_client.materialize_owner(claim.request_id, claim.execution_attempt_id)
        native_client.finish(materialized['handle'])
        owner = require_execution_binding(binding, read_json_file(claim.path))
    agent = pool.get(owner)
    require_execution_agent(binding, read_json_file(claim.path), agent)
    paths = gateway_paths_from_root(snapshot.bootstrap.launch.gateway_root)
    from .multiprocess_native_executor_scope import native_request_executor_scope

    executor_client = getattr(native_client, 'executor_client', None)
    with pool.pin(agent), worker_execution_scope(binding, agent), native_request_executor_scope(binding, executor_client):
        return _process_claimed_gateway_request_path(agent, paths, claim.path, claim.lease_owner, claim=claim)
