# LLM: P0/P6共用唯一取消结构校验；从固定来源stop完整搬出纯函数以隔离dispatch运行依赖，未来stop须导同一对象。
# 模块用途: 只检查精确取消记录和观察时间，不取得取消授权、不写盘、不发中断或证明退出。
import math

from .multiprocess_records import (
    MultiprocessRecordError,
    _canonical_owner,
    _identifier,
    _integer,
    _sha,
)
from .multiprocess_route import owner_worker


# LLM: 只由 P0 字段全集校验后调用；沿 P0 规范 owner/标识和严格整数，不补缺省身份。
# 函数用途: 校验取消记录的类型、固定分区和时间。
def validate_cancellation_record(record):
    for field in ("request_id", "execution_attempt_id", "lease_owner", "c_run_id"):
        _identifier(record[field], field)
    _integer(record["lease_epoch"], "lease_epoch")
    _integer(record["worker_count"], "worker_count")
    _integer(record["worker_index"], "worker_index", 0)
    owner = _canonical_owner(record["owner"])
    if owner_worker(owner, record["worker_count"]) != record["worker_index"]:
        raise MultiprocessRecordError("cancellation_partition_mismatch", "worker_index")
    _sha(record["route_binding"], "route_binding")
    require_stop_time(record["cancel_requested_at"])


# LLM: 时间只是观察值，不提供死亡证明；bool、非有限值、负值及无法转为有限浮点的大整数均按同一原原因拒绝。
# 函数用途: 校验取消观察时间，保持结构化错误而不让数值溢出逃离记录合同。
def require_stop_time(value):
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        raise MultiprocessRecordError("invalid_stop_time", "observed_at")
