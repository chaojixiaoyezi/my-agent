# LLM: P6角色只描述原C/W观察范围，不授执行或接手权；原W分区API保持，C明确没有W编号，同步原六类排空/P2范围测试。
# 模块用途: 给停止观察增加真实协调者坐标，避免把C冒充为某个worker。
from dataclasses import dataclass

from .multiprocess_records import MultiprocessRecordError, _identifier, _integer
from .multiprocess_route import WorkerPartition
from .multiprocess_stop import require_stop_partition


# LLM: 本对象纯事实且不创建来源；调用方必须已捕获原C/controller/lease，字段不能代替持锁身份或原内核责任。
# 类用途: 描述本次原协调者的代次和固定worker数量。
@dataclass(frozen=True)
class CoordinatorStopScope:
    count: int
    c_run_id: str


# LLM: 旧W输入仍走原严格校验；C仅显式类型，不从字符串、桶号或PID推角色，无I/O。
# 函数用途: 验证停止观察的准确角色坐标。
def require_stop_scope(scope):
    if type(scope) is WorkerPartition:
        require_stop_partition(scope)
        return
    if type(scope) is not CoordinatorStopScope:
        raise MultiprocessRecordError("stop_partition_invalid", "partition")
    _integer(scope.count, "worker_count")
    _identifier(scope.c_run_id, "c_run_id")


# LLM: 只投影已经验证的原P2范围字段，C的worker_index必须None；投影不产生任何来源证明。
# 函数用途: 对齐停止观察和原C/W执行树证明的角色字段。
def stop_scope_fields(scope):
    require_stop_scope(scope)
    return {"role": "W" if type(scope) is WorkerPartition else "C",
            "worker_index": scope.index if type(scope) is WorkerPartition else None,
            "worker_count": scope.count, "c_run_id": scope.c_run_id}
