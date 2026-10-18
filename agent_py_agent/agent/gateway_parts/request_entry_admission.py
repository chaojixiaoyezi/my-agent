# LLM: 原公开业务入口仅核当前C/W实际Agent和原claim/P3/P5；模式只决定拒缺scope，不签许可；空短T准入后长业务仍在锁外，默认纯内存无I/O。
# 模块用途: 防止正多进程后台旧入口漏接当前请求后回落单进程执行，不一刀切拒合法原前台工具。
from __future__ import annotations

from .multiprocess_coordinator_execution import CoordinatorExecution
from .multiprocess_execution import WorkerExecution, current_worker_execution
from .multiprocess_execution_lifetime import ExecutionScopeLifetime
from .request_binding import GatewayActiveTurnTransition


# LLM: 返回值只拒绝默认旁路；原不可逆host标志/冻结W/C内存事实同源，不新建role或状态账。
# 函数用途: 判断当前是否必须携带原请求执行上下文，默认不读取文件。
def request_execution_required():
    from ..common.global_write_guard import restricted_executor_global_writes
    from .multiprocess_coordinator import coordinator_executor_required
    from .multiprocess_mode import current_worker_partition

    return (current_worker_execution() is not None or current_worker_partition() is not None
            or coordinator_executor_required() or restricted_executor_global_writes())


# LLM: component名称来自原产品绑定，不解析正文/role；同实际Agent/home且原活token才短核claim，合法前台无需BACKGROUND_REQUEST。
# 函数用途: 在原业务实际执行前核当前准确请求，缺scope或跨owner不得进入模型/持久业务。
def require_request_execution(*, agent=None, component=None, component_attribute="", home=None):
    if not request_execution_required():
        return
    execution = current_worker_execution()
    from .multiprocess_dispatch import WorkerDispatchError

    if type(execution) not in (WorkerExecution, CoordinatorExecution):
        raise WorkerDispatchError("execution_context_required", "business_entry")
    actual = execution.agent
    if ((agent is not None and actual is not agent)
            or (component is not None and getattr(actual, component_attribute, None) is not component)
            or (home is not None and actual.home_paths.owner_home_dir != home.owner_home_dir)):
        raise WorkerDispatchError("execution_agent_mismatch", "business_entry")
    claim = execution.binding.claim if type(execution) is WorkerExecution else execution.claim
    transition = GatewayActiveTurnTransition(claim.path, claim.request_id, claim.execution_attempt_id)
    from .multiprocess_background_execution import (
        _uncertain_execution,
        background_short_operation,
        current_background_request,
    )

    scope = current_background_request()
    if scope is not None:
        if scope.execution is not execution:
            raise WorkerDispatchError("execution_context_mismatch", "business_entry")
        # 原BG token/T未进入的失败由原精确typed门承担，不能提前转普通失败释放同request的P3。
        background_short_operation("business_entry", lambda: None)
        return
    if type(execution.lifetime) is not ExecutionScopeLifetime:
        raise WorkerDispatchError("execution_scope_unavailable", "business_entry")
    try:
        execution.lifetime.require_original(execution)
    except ValueError as error:
        raise WorkerDispatchError("execution_scope_unavailable", "business_entry") from error
    try:
        transition("business_entry", lambda: None)
    except Exception as error:
        raise _uncertain_execution(transition, "business_entry") from error
