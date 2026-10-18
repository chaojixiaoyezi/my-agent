# LLM: P6复用原P2证明/屏障及C/W独立范围，不探测进程/改账；原W API保持，C明确无W编号，同步生产collector与范围拒绝测试。
#   可信来源/冻结期间禁止新执行/证据时效由原调用方保证；规范字节相等不是证据真实性的认证。
# 模块用途: 冻结并核对一次执行树停止观察。
from dataclasses import dataclass

from ..common.strict_json import load_strict_json
from .multiprocess_admission import _takeover_barrier_rejection, _takeover_proof_rejection
from .multiprocess_records import (
    MultiprocessRecordError,
    _canonical_bytes,
    deserialize_record,
    serialize_record,
)
from .multiprocess_stop_scope import stop_scope_fields


# LLM: 原覆盖范围由可信调用方在冻结枚举后给出，不可从证明反推以自证。
# 类用途: 以不可变字节保留独立范围和 P2 输出。
@dataclass(frozen=True)
class TreeStopObservation:
    scope_bytes: bytes
    result_bytes: bytes
    executor_bytes: tuple[bytes, ...]


# LLM: 只读冻结，不判断死亡；未知结果仍可保存，不能用冻结成功冒充核实成功。
# 函数用途: 冻结覆盖范围和停止证据。
def freeze_tree_observation(scope, result, executors):
    return TreeStopObservation(_canonical_bytes(scope), _canonical_bytes(result), tuple(serialize_record(r) for r in executors))


# LLM: 沿 P3 原证明/屏障验证器；额外精确核独立 P1 范围，不提供接手或新执行权。
# 函数用途: 核对整树停止与精确分区范围。
def require_stopped_tree(partition, observation):
    scope, result = _decode_observation(observation)
    if type(result) is not dict or set(result) != {"barrier", "proof", "error"} or result["error"] is not None:
        raise MultiprocessRecordError("tree_result_unconfirmed", "tree")
    proof = result["proof"]
    reason = _takeover_proof_rejection(proof)
    if reason:
        raise MultiprocessRecordError(reason, "proof")
    _require_tree_scope(partition, scope, proof)
    _require_p1_coverage(observation, scope)
    reason = _takeover_barrier_rejection(result["barrier"], partition.c_run_id)
    if reason:
        raise MultiprocessRecordError(reason, "barrier")
    return proof


# LLM: 只读解析原规范字节；损坏观察不能变成空集合或 stopped 默认值。
# 函数用途: 取出独立范围与原 P2 结果。
def _decode_observation(observation):
    if type(observation) is not TreeStopObservation:
        raise MultiprocessRecordError("tree_observation_invalid", "tree")
    try:
        return load_strict_json(observation.scope_bytes), load_strict_json(observation.result_bytes)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise MultiprocessRecordError("tree_observation_invalid", "tree") from exc


# LLM: v2 role/代次精确核原C/W坐标，独立完整scope不能从proof反推；C的None不兼容W0，同步原范围反例。
# 函数用途: 拒绝跨角色/分区、旧代和原执行者覆盖缺口。
def _require_tree_scope(partition, scope, proof):
    expected = stop_scope_fields(partition)
    if proof["schema_version"] != "multiprocess_takeover_proof.v2":
        raise MultiprocessRecordError("tree_scope_mismatch", "covered_scope")
    if _canonical_bytes(scope) != _canonical_bytes(proof["covered_scope"]):
        raise MultiprocessRecordError("tree_scope_mismatch", "covered_scope")
    actual = {field: scope[field] for field in expected}
    if _canonical_bytes(actual) != _canonical_bytes(expected):
        raise MultiprocessRecordError("tree_partition_mismatch", "covered_scope")


# LLM: 原P1显式v1/v2沿唯一P0解码，v2域字段不自证死亡，旧v1仍按原unknown/残余范围；同步P6覆盖/接手 tests。
# 函数用途: 返回本观察冻结的原执行者记录。
def executor_records(observation):
    if type(observation) is not TreeStopObservation or type(observation.executor_bytes) is not tuple:
        raise MultiprocessRecordError("executor_snapshot_invalid", "executors")
    records = tuple(deserialize_record(content) for content in observation.executor_bytes)
    if any(r["schema_version"] not in {"multiprocess_executor_reservation.v1", "multiprocess_executor_reservation.v2"} for r in records):
        raise MultiprocessRecordError("executor_record_required", "executors")
    return records


# LLM: 独立 P1 清单精确覆盖 scope；省略、重复、旧代或另一 owner 的记录都不能由证明自证。
# 函数用途: 核对实际提供的执行者与声明集合一致。
def _require_p1_coverage(observation, scope):
    records = executor_records(observation)
    identifiers = [r["reservation_id"] for r in records]
    if sorted(identifiers) != sorted(scope["executor_reservation_ids"]):
        raise MultiprocessRecordError("executor_coverage_mismatch", "executors")
    for record in records:
        if record["c_run_id"] != scope["c_run_id"] or record["owner_sha"] not in scope["owner_shas"]:
            raise MultiprocessRecordError("executor_scope_mismatch", "executors")
