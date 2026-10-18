# LLM: P5 只提供认领前的无写入合同；队列移动、执行和终态仍由原链负责。
# 模块用途: 在 worker 消费请求前统一核验冻结分区、盖章和原准入预留。
from ..user_space.owner_resolver import OwnerIdentity
from .multiprocess_admission import verify_claimable_reservation
from .multiprocess_bootstrap import require_worker_bootstrap
from .multiprocess_records import MultiprocessRecordError
from .multiprocess_route import validate_worker_route


# LLM: 沿用既有路由拒绝码，不创建第二套请求状态或恢复动作。
# 类用途: 让调用方以结构化原因放弃本次认领或执行。
class WorkerDispatchError(MultiprocessRecordError):
    error_code = "OWNER_ROUTE_MISMATCH"


# LLM: 输入为 P4 冻结启动对象及 P3 权威记录；无文件、配置、模型或工具副作用。
# 函数用途: 返回可使用的冻结 owner，任何不满足条件的请求都拒绝。
def require_claimable(frozen, request, reservation):
    require_worker_bootstrap(frozen, getattr(frozen, "launch", None))
    admission = verify_claimable_reservation(request, reservation)
    if not admission["ok"]:
        raise WorkerDispatchError(admission["reason"], "reservation")
    route = validate_worker_route(request, frozen.launch.partition)
    if not route["ok"]:
        raise WorkerDispatchError(route["reason"], "route")
    return OwnerIdentity(*route["owner"])
