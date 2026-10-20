# LLM: 只在原C首次materialize拒绝时消费原operation/claim/P3出处，不创建准入、身份、恢复或执行权。同步native短操作与终态核收测试。
# 模块用途: 严格核对同一原请求的未执行来源；缺失、历史及任何未知均保原拒绝，不把wire NACK当业务证明。
import hashlib
import json
import math

from . import multiprocess_admission as ma
from .multiprocess_records import deserialize_record, record_digest
from .multiprocess_worker_commit import WorkerCommitRejected


# LLM: 返回同controller首个实际gate；指针只定位，原Root/tuple/lease逐is及current检查才是既有权威，不补建或替换gate。
# 函数用途: 找回本C原本已登记的终态释放消费者。
def original_terminal_gate(issuer):
    from .multiprocess_drain_runtime import GatewayDrainRuntime

    controller = issuer._controller
    gate = getattr(controller, "_native_terminal_admission_origin", None)
    source = getattr(gate, "_ordinary_admission_origin", None)
    if type(gate) is not ma.PersistentAdmission or type(source) is not tuple or len(source) != 2:
        raise WorkerCommitRejected("original_terminal_admission_required")
    runtime, original = source
    if type(runtime) is not GatewayDrainRuntime:
        raise WorkerCommitRejected("original_terminal_admission_required")
    runtime.require_original()
    if (
        type(original) is not tuple
        or len(original) != 5
        or not runtime._admission_origins
        or runtime._admission_origins[0] is not original
        or not any(row is original for row in runtime._admission_origins)
        or original[0] is not gate
        or original[1] is not gate.paths
        or original[2] is not gate.agent
        or original[3] != gate.count
        or original[4] != gate.c_run_id
        or runtime.control is not issuer.control
        or runtime.controller is not controller
        or runtime._lease_origin is not issuer._lease
        or controller._lease is not issuer._lease
        or gate.paths is not issuer.control.paths
        or gate._runtime_takeover is not controller
        or type(gate.count) is not int
        or gate.count != issuer.launch.partition.count
        or gate.c_run_id != issuer.launch.partition.c_run_id
    ):
        raise WorkerCommitRejected("original_terminal_admission_required")
    issuer._require_current()
    return gate


# LLM: canonical完整JSON首像只保不可改bytes，不从字段猜实际运行；route/P3原门仍先独立执行，消费时再逐型核身份。
# 函数用途: 在原T首次读回后冻结完整claim，后续心跳或正文漂移也不能重新签首像。
def claim_bytes(payload):
    return json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode()


# LLM: 首片只接受真实新claim，旧attempt历史不靠缺当前P1推空；W出生采用原issuer已捕获同源kernel身份。
# 函数用途: 核准首拒的完整身份与原route，既不补字段也不兼容字符串epoch。
def require_first_claim(issuer, transition, payload):
    from .multiprocess_route import validate_worker_route

    identity = {key: value for key, value in issuer._identity.items() if key != "schema_version"}
    expected = {
        "id": transition.request_id,
        "execution_attempt_id": transition.execution_attempt_id,
        "lease_owner": transition.execution_attempt_id,
        "lease_epoch": 1,
        "attempts": 1,
        "lease_process_identity": identity,
        "status": "processing",
        "turn_phase": "open",
    }
    if (
        type(payload) is not dict
        or any(
            type(payload.get(key)) is not type(value) or payload[key] != value
            for key, value in expected.items()
        )
        or any(key in payload for key in ("conversation_claim", "runtime_authority"))
        or ("cancel_requested" in payload and payload["cancel_requested"] is not False)
        or not validate_worker_route(payload, issuer.launch.partition)["ok"]
    ):
        raise WorkerCommitRejected("first_native_claim_required")
    if "request_id" in payload and (
        type(payload["request_id"]) is not str or payload["request_id"] != transition.request_id
    ):
        raise WorkerCommitRejected("first_native_claim_required")
    if claim_bytes(payload["lease_process_identity"]) != claim_bytes(identity):
        raise WorkerCommitRejected("first_native_claim_required")
    for field in ("lease_started_at", "lease_heartbeat_at"):
        if (
            type(payload.get(field)) not in (int, float)
            or not math.isfinite(payload[field])
            or payload[field] <= 0
        ):
            raise WorkerCommitRejected("first_native_claim_required")


# LLM: 原registry本身是唯一短操作登记；只有本原对象一个同request记录且未pin，不能用另一facade/已回收_sources重签来源。
# 函数用途: 排除该请求先前任何phase，并严格检查全域原P1记录无该请求执行责任。
def require_no_native_effect(operation, issuer, transition):
    from .multiprocess_generation_metadata import generation_source_files

    registry = getattr(issuer._controller, "_native_short_operations", None)
    if type(registry) is not dict or not getattr(operation, "_first_materialize_origin", False):
        raise WorkerCommitRejected("first_native_effect_absence_required")
    matching = [value for key, value in registry.items() if key[2] == transition.request_path]
    if len(matching) != 1 or matching[0] is not operation:
        raise WorkerCommitRejected("first_native_effect_absence_required")
    require_no_owner_pin(operation, issuer)
    before = generation_source_files(issuer.control.paths)
    records = {}
    for name, content in before.items():
        if name.startswith("workers/executors/"):
            row = deserialize_record(content)
            if (
                not row["schema_version"].startswith("multiprocess_executor_reservation.")
                or row["request_id"] == transition.request_id
                or row["reservation_id"] in records
                or name.rsplit("/", 1)[-1]
                != hashlib.sha256(row["reservation_id"].encode()).hexdigest() + ".json"
            ):
                raise WorkerCommitRejected("first_native_effect_absence_required")
            records[row["reservation_id"]] = row
    if any(
        identifier not in records or record_digest(records[identifier]) != digest
        for identifier, digest in issuer._executor_records.items()
    ):
        raise WorkerCommitRejected("first_native_executor_source_required")
    if before != generation_source_files(issuer.control.paths):
        raise WorkerCommitRejected("first_native_sources_changed")


# LLM: caller持原T；首完整claim和原P3摘要都必须逐值一致，不使用读回的新摘要覆盖原来源。
# 函数用途: 复核本次消费仍是登记的原请求，不读最新状态来冒充已冻结身份。
def require_captured_claim(operation, issuer, owner, transition):
    from .multiprocess_native_reclaim_io import read_reservation
    from .multiprocess_native_short_operation import require_original_native_admission

    paths = issuer.control.paths
    strict_row = read_reservation(paths.root, ma.reservation_file(paths, transition.request_id))
    payload = read_claim_payload(paths, transition.request_path)
    row = require_original_native_admission(issuer, owner, transition)
    if row != strict_row:
        raise WorkerCommitRejected("first_native_admission_changed")
    if claim_bytes(payload) != getattr(operation, "_claim_origin_bytes", None):
        raise WorkerCommitRejected("first_native_claim_changed")
    require_first_claim(issuer, transition, payload)
    if record_digest(row) != operation._p3_digest:
        raise WorkerCommitRejected("first_native_admission_changed")
    require_no_native_effect(operation, issuer, transition)
    return payload


# LLM: caller已持原T；只用原strict JSON和no-follow读完整canonical普通文件，缺失/坏schema/特殊文件均不变空账。
# 函数用途: 读取用于首拒收口的完整请求，不创建JSON锁或目录。
def read_claim_payload(paths, path):
    from ..common.nofollow_fs import read_bytes_beneath
    from ..common.strict_json import load_strict_json

    content = read_bytes_beneath(
        paths.root,
        path.relative_to(paths.root).parts,
        max_bytes=16 * 1024 * 1024,
        require_dir_fd=True,
    )
    payload = load_strict_json(content) if content is not None else None
    if type(payload) is not dict or not payload:
        raise WorkerCommitRejected("first_native_claim_unreadable")
    return payload


# LLM: caller持原T/controller/lease；逐原lease完整pin集合核来源，同owner/未知均拒，只原issuer引用证明的其它owner可保留，绝不凭标签认foreign。
# 函数用途: 排除整个本租约中可能属于当前owner的责任，不清pin或授予执行/恢复权。
def require_no_owner_pin(operation, issuer):
    from .multiprocess_coordinator import _CoordinatorCommitPin
    from .multiprocess_records import _sha
    from .multiprocess_route import owner_digest

    pins = issuer._lease._commit_pins
    if type(pins) is not set or type(issuer._pins) is not set or not issuer._pins.issubset(pins):
        raise WorkerCommitRejected("first_native_effect_absence_required")
    sources = _original_pin_issuers(issuer, pins)
    digest = owner_digest(operation._sources[1])
    for pin in pins:
        if type(pin) is not _CoordinatorCommitPin:
            raise WorkerCommitRejected("first_native_effect_absence_required")
        owners = [source for source in sources if pin in source._pins]
        if len(owners) != 1:
            raise WorkerCommitRejected("first_native_effect_absence_required")
        original = owners[0]
        expected = (
            issuer.control.c_run_id,
            "W",
            str(original.launch.partition.index),
            original.launch.instance_id,
        )
        if (
            type(pin.scope_key) is not tuple
            or len(pin.scope_key) != 5
            or pin.scope_key[:4] != expected
            or pin.scope_key[4] == digest
        ):
            raise WorkerCommitRejected("first_native_effect_absence_required")
        _sha(pin.scope_key[4], "pin.owner_sha")


# LLM: 原current worker collector逐is核已有来源；仅为pin归属定位，不登记/替换source，缺原C/Popen/lease或复制对象均保未知。
# 函数用途: 找到实际持有原pin的当前worker issuer；C-short/旧facade无来源即不猜owner。
def _original_pin_issuers(issuer, pins):
    from .multiprocess_takeover_runtime import original_drain_sources
    from .multiprocess_worker_commit import WorkerCommitIssuer

    sources = [issuer]
    if pins.issubset(issuer._pins):
        return sources
    workers, _coordinator, _hosts = original_drain_sources(issuer._controller)
    for index, (candidate, launch, process) in workers:
        candidate._require_current()
        candidate._require_worker(process.pid)
        if (
            type(candidate) is not WorkerCommitIssuer
            or candidate._issuer_origin is not candidate
            or candidate._creator_pid != issuer._creator_pid
            or candidate._lease is not issuer._lease
            or candidate._controller is not issuer._controller
            or type(candidate._pins) is not set
            or not candidate._pins.issubset(pins)
            or candidate._source_origin[0] is not launch
            or candidate._source_origin[1] is not process
            or type(index) is not int
            or index != launch.partition.index
            or launch.partition.count != issuer.launch.partition.count
            or launch.partition.c_run_id != issuer.control.c_run_id
        ):
            raise WorkerCommitRejected("first_native_pin_source_required")
        if not any(candidate is source for source in sources):
            sources.append(candidate)
    return sources
