# LLM: P5 锁内比较只消费原队列与准入事实，不移动请求；调用方须持原 T 锁至原 claim 写完。
# 模块用途: 保存临时不可变扫描快照，并在认领移动之前只读复核。
from dataclasses import dataclass
from pathlib import Path

from ..common.strict_json import load_strict_json
from .multiprocess_admission import _read_reservation, reservation_file
from .multiprocess_bootstrap import WorkerBootstrap, require_worker_bootstrap
from .multiprocess_dispatch import WorkerDispatchError, require_claimable
from .multiprocess_records import _canonical_bytes, serialize_record
from .paths import gateway_paths_from_root


# LLM: 合并一次扫描已有输入，减少接口参数；只暂存原事实，冻结前不是独立权威或持久状态。
# 类用途: 携带候选请求路径、请求和原预留，交给快照函数完整校验。
@dataclass(frozen=True)
class ClaimCandidate:
    request_path: Path
    request: dict
    reservation: dict


# LLM: 字节只在本次扫描内保存，不落盘、不作为另一份权威记录；根沿 P4 原对象。
# 类用途: 防止调用方就地修改字典把锁外观察一起改掉。
@dataclass(frozen=True)
class ClaimSnapshot:
    bootstrap: WorkerBootstrap
    request_path: Path
    request_bytes: bytes
    reservation_bytes: bytes


# LLM: 输入必须来自原队列扫描和 P3 原预留；无写入，路径身份必须属于冻结队列根。
# 函数用途: 为一次候选认领冻结锁外完整观察。
def freeze_claim_snapshot(frozen, candidate):
    request_path, request, reservation = candidate.request_path, candidate.request, candidate.reservation
    require_claimable(frozen, request, reservation)
    _pending_paths(frozen, request_path, request["id"])
    return ClaimSnapshot(frozen, request_path, _canonical_bytes(request), serialize_record(reservation))


# LLM: 调用方已持 gateway_turn_transition，且必须继续持锁到原 claim 移动/写 fence 完毕。
# 函数用途: 复读原请求和预留后比较；不拿锁、不移动、不建目录、不接执行链。
def recheck_claim_locked(snapshot):
    if type(snapshot) is not ClaimSnapshot:
        raise WorkerDispatchError("claim_snapshot_invalid", "snapshot")
    frozen, path = snapshot.bootstrap, snapshot.request_path
    require_worker_bootstrap(frozen, getattr(frozen, "launch", None))
    paths = _pending_paths(frozen, path, path.stem)
    request = _read_locked_request(path)
    reservation = _read_reservation(reservation_file(paths, path.stem))
    return require_unchanged_claim(snapshot, request, reservation)


# LLM: 原回合 T 锁已由调用方持有，不能用会创建 JSON sidecar 的读器伪称只读；沿原严格解码。
# 函数用途: 只读已存在的请求，坏字节/非对象/缺失均拒绝，不补记录或锁文件。
def _read_locked_request(path):
    try:
        request = load_strict_json(path.read_bytes())
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise WorkerDispatchError("claim_request_unreadable", "request") from error
    if type(request) is not dict or not request:
        raise WorkerDispatchError("claim_request_unreadable", "request")
    return request


# LLM: 全量字节比较包含指纹未覆盖的取消/attempt/epoch/等待事实，不能只重验自洽路由。
# 函数用途: 纯比较锁内实读与锁外快照，变化即拒绝，不改输入。
def require_unchanged_claim(snapshot, request, reservation):
    owner = require_claimable(snapshot.bootstrap, request, reservation)
    _pending_paths(snapshot.bootstrap, snapshot.request_path, request["id"])
    if _canonical_bytes(request) != snapshot.request_bytes:
        raise WorkerDispatchError("claim_snapshot_changed", "request")
    if serialize_record(reservation) != snapshot.reservation_bytes:
        raise WorkerDispatchError("claim_snapshot_changed", "reservation")
    return owner


# LLM: 只复用 GatewayPaths 的路径计算，不 resolve、不创建目录，也不从 owner 配置派生队列根。
# 函数用途: 核文件名和 pending 根来自同一个冻结启动上下文。
def _pending_paths(frozen, request_path, request_id):
    paths = gateway_paths_from_root(frozen.launch.gateway_root)
    if not isinstance(request_path, Path) or request_path != paths.inbox / f"{request_id}.json":
        raise WorkerDispatchError("request_path_mismatch", "request_path")
    return paths
