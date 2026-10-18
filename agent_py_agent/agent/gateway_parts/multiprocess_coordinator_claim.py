# LLM: C认领仍沿原T锁和原claim；只复读原请求/预留，生命周期门覆盖rename/fence，不新建权威记录。
# 模块用途: 阻止C拿盖章前的内存返回值认领已经漂移的请求。
from contextlib import contextmanager
from dataclasses import dataclass

from ..user_space.owner_resolver import OwnerIdentity
from .multiprocess_admission import (
    _read_reservation,
    _runtime_commit_scope,
    reservation_file,
    verify_claimable_reservation,
)
from .multiprocess_dispatch import WorkerDispatchError
from .multiprocess_dispatch_claim import _read_locked_request
from .multiprocess_records import _canonical_bytes, serialize_record
from .multiprocess_route import validate_coordinator_route


# LLM: 只保存在一次派发内；gate来自启动组合根，字节从准入真实返回冻结，不从请求自建上下文。
# 类用途: 保存盖章结果供T锁内认领对照。
@dataclass(frozen=True)
class CoordinatorClaimSnapshot:
    gate: object
    request_bytes: bytes
    reservation_bytes: bytes


# LLM: 不做I/O、不改准入结果；调用者随后必须在原T内消费此快照。
# 函数用途: 冻结C准入刚返回的请求和预留。
def freeze_coordinator_claim(gate, decision):
    return CoordinatorClaimSnapshot(gate, _canonical_bytes(decision["request"]),
                                    serialize_record(decision["reservation"]))


# LLM: 原T已持有，原队列唯一等待状态是pending；异常不移动/释放，生产gate必须带原P2运行时门。
# 函数用途: 复核原pending请求的盖章身份和全量快照，再持撤权门直到claim完成。
@contextmanager
def coordinator_claim_scope(paths, path, snapshot):
    if type(snapshot) is not CoordinatorClaimSnapshot or snapshot.gate.paths.root != paths.root:
        raise WorkerDispatchError("coordinator_claim_snapshot_required", "snapshot")
    gate = snapshot.gate
    request = _read_locked_request(path)
    if path != paths.inbox / (str(request.get("id")) + ".json"):
        raise WorkerDispatchError("claim_path_mismatch", "request_path")
    checked = validate_coordinator_route(request, gate.count, gate.c_run_id)
    if not checked["ok"]:
        raise WorkerDispatchError(checked["reason"], "route")
    reservation = _read_reservation(reservation_file(paths, path.stem))
    checked = verify_claimable_reservation(request, reservation)
    if not checked["ok"]:
        raise WorkerDispatchError(checked["reason"], "reservation")
    if (_canonical_bytes(request) != snapshot.request_bytes
            or serialize_record(reservation) != snapshot.reservation_bytes):
        raise WorkerDispatchError("claim_snapshot_changed", "snapshot")
    if (request.get("status", "pending") != "pending" or request.get("cancel_requested", False) is not False
            or request.get("turn_phase") not in (None, "open")):
        raise WorkerDispatchError("request_not_claimable", "request")
    with _runtime_commit_scope(gate, OwnerIdentity.local_main()) as rejection:
        if rejection is not None:
            raise WorkerDispatchError(rejection["reason"], "coordinator")
        yield
