# LLM: P6 只运输原 C/P5 取消事实；原 processing/T 锁仍是唯一权威，不写文件或发中断；纯取消/时间校验必须导入B1唯一schema实现。
# 模块用途: 定义精确取消格式与 worker 消费门。
from .multiprocess_dispatch import WorkerDispatchError
from .multiprocess_dispatch_binding import require_cancellation
from .multiprocess_records import (
    MultiprocessRecordError,
    _identifier,
    _integer,
    serialize_record,
    validate_record,
)
from .multiprocess_route import WorkerPartition, validate_worker_route
from .multiprocess_stop_schema import require_stop_time as require_stop_time
from .multiprocess_stop_schema import validate_cancellation_record as validate_cancellation_record


# LLM: C 在原 T 锁校验/写回后调用，纯格式不证明持久化成功。
# 函数用途: 投影已保存取消请求的精确身份。
def cancellation_record(partition, request):
    checked = validate_worker_route(request, partition)
    if not checked["ok"]:
        raise WorkerDispatchError(checked["reason"], "route")
    _require_cancelled_processing(request)
    record = {"schema_version": "multiprocess_cancellation.v1", "request_id": request["id"],
              "worker_index": partition.index, "worker_count": partition.count, "c_run_id": partition.c_run_id,
              "owner": checked["owner"], "route_binding": request["route"]["binding"]}
    for field in ("execution_attempt_id", "lease_epoch", "lease_owner", "cancel_requested_at"):
        record[field] = request.get(field)
    return validate_record(record)


# LLM: worker 在原中断入口前消费；不能借运输记录取得新执行权。
# 函数用途: 用原 P5 binding 匹配最新取消事实与运输记录。
def require_cancel_record(binding, request, record):
    owner = require_cancellation(binding, request)
    expected = cancellation_record(binding.snapshot.bootstrap.launch.partition, request)
    if serialize_record(record) != serialize_record(expected):
        raise WorkerDispatchError("cancellation_identity_mismatch", "cancellation")
    return owner


# LLM: 仅校验原取消结构，不替代 C 的 scope/T 锁，也不准许 closing 后的新模型/工具执行。
# 函数用途: 拒绝未取消、非在途、已闭合和别名身份冲突。
def _require_cancelled_processing(request):
    if request.get("cancel_requested") is not True:
        raise MultiprocessRecordError("cancellation_not_requested", "cancel_requested")
    if request.get("status") != "processing" or request.get("turn_phase") not in ("open", "closing"):
        raise MultiprocessRecordError("claim_not_processing", "request")
    if request.get("request_id", request["id"]) != request["id"]:
        raise MultiprocessRecordError("claim_fence_mismatch", "request_id")


# LLM: 纯合同调用方只能提供 P4 冻结的 WorkerPartition；类型校验不是自行取得 worker 权限。
# 函数用途: 拒绝 bool 分区、空代次和越界编号。
def require_stop_partition(partition):
    if type(partition) is not WorkerPartition:
        raise MultiprocessRecordError("stop_partition_invalid", "partition")
    _integer(partition.index, "worker_index", 0)
    _integer(partition.count, "worker_count")
    _identifier(partition.c_run_id, "c_run_id")
    if partition.index >= partition.count:
        raise MultiprocessRecordError("stop_partition_invalid", "partition")
