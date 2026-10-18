# LLM: 仅在原 queue_service 的 T 锁内检查 P5 快照；不获取第二把请求锁、不移动文件或释放预留。
# 模块用途: 把已有 P4/P5 只读合同接到真实认领动作之前，拒绝 worker 从旧单进程入口绕过。
from ..common.strict_json import load_strict_json
from .multiprocess_dispatch import WorkerDispatchError
from .multiprocess_dispatch_claim import ClaimSnapshot, recheck_claim_locked
from .multiprocess_mode import current_worker_partition


# LLM: 原T持到rename/fence；只消费原队列pending状态，InputReceipt的queued不是请求状态别名，默认模式无磁盘观察。
# 函数用途: 在原pending请求移动前核worker冻结根、完整快照及精确认领资格。
def require_worker_claim_locked(paths, request_path, snapshot):
    if current_worker_partition() is None and snapshot is None:
        return
    if type(snapshot) is not ClaimSnapshot:
        raise WorkerDispatchError("claim_snapshot_required", "snapshot")
    if snapshot.request_path != request_path or snapshot.bootstrap.launch.gateway_root != paths.root:
        raise WorkerDispatchError("claim_path_mismatch", "request_path")
    recheck_claim_locked(snapshot)
    request = load_strict_json(snapshot.request_bytes)
    if (request.get("status", "pending") != "pending" or request.get("cancel_requested", False) is not False
            or request.get("turn_phase") not in (None, "open")):
        raise WorkerDispatchError("request_not_claimable", "request")
