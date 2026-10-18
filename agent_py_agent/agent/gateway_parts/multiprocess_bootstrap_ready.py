# LLM: P4 合同消费原 P0 记录与 B1a 健康门；不派发、不启动进程、不把 ready 当接手授权。
# 模块用途: 构造并核对当前 worker 实例的心跳，拒绝迟到实例和旧代。
import math

from .multiprocess_bootstrap import WorkerBootstrapError
from .multiprocess_executor_identity import process_birth
from .multiprocess_records import MultiprocessRecordError, validate_record
from .multiprocess_route import WorkerPartition
from .multiprocess_state import stampable_workers, worker_heartbeat


# LLM: 沿原错误信封输出精确字段，不把未知身份降为 PID-only。
# 类用途: 报告当前 registry 与心跳不能匹配的结构化原因。
class WorkerReadyError(WorkerBootstrapError):
    error_code = "WORKER_READY_REJECTED"


# LLM: registry 必须由当前持锁 C 读回；纯身份匹配后仍保原健康/端口门，不证明 OS 隔离或执行树死亡。
# 函数用途: 返回通过原身份及健康核验的当前实例，失败关闭。
def require_current_worker(registry, heartbeat, partition, now):
    registry, heartbeat, entry = current_worker_identity(registry, heartbeat, partition)
    _healthy_readback(registry, heartbeat, partition, now)
    return entry


# LLM: 纯提原record/current_generation/P4 entry匹配；只分类同代原实例，不读OS、不查活issuer、不授ready/执行权。
# 函数用途: 返回完整校验后的登记、心跳及实例独立副本，供ready门与角色双读复用同一身份合同。
def current_worker_identity(registry, heartbeat, partition):
    registry = _record(registry, "multiprocess_worker_registry.v1")
    heartbeat = _record(heartbeat, "multiprocess_worker_heartbeat.v1")
    _current_generation(registry, heartbeat, partition)
    entry = next((item for item in registry["workers"] if item["worker"] == heartbeat["worker"]), None)
    if entry is None:
        raise WorkerReadyError("worker_not_registered", "worker")
    if heartbeat["instance_id"] != entry["instance_id"]:
        raise WorkerReadyError("worker_instance_mismatch", "instance_id")
    if heartbeat["process_identity"] != entry["process_identity"]:
        raise WorkerReadyError("worker_process_mismatch", "process_identity")
    return registry, heartbeat, entry


# LLM: 实例号由 C 的登记传入；只构造数据，不发布、不改模式。
# 函数用途: 沿原心跳结构补上精确出生身份和实例号。
def registered_worker_heartbeat(launch, instance_id, readiness, now):
    heartbeat = worker_heartbeat(launch, readiness, now)
    identity = {**heartbeat["process_identity"], "schema_version": "multiprocess_process_identity.v1",
                "start_time": process_birth(heartbeat["pid"])}
    heartbeat.update(schema_version="multiprocess_worker_heartbeat.v1", instance_id=instance_id, process_identity=identity)
    return _record(heartbeat, "multiprocess_worker_heartbeat.v1")


# LLM: 旧 schema 不做自动适配；无出生、损坏或类型错误必须结构化拒绝。
# 函数用途: 从原记录入口取得独立副本并限制其记录种类。
def _record(payload, schema: str) -> dict:
    try:
        record = validate_record(payload)
    except MultiprocessRecordError as error:
        raise WorkerReadyError(error.reason, error.field) from error
    if record["schema_version"] != schema:
        raise WorkerReadyError("wrong_record_kind", "schema_version")
    return record


# LLM: C 的当前分区合同和 registry/心跳都要同代，不能只比 worker 号或相互自洽的旧快照。
# 函数用途: 拒绝旧代、分区数变化或错误的当前上下文。
def _current_generation(registry, heartbeat, partition) -> None:
    if type(partition) is not WorkerPartition or type(partition.count) is not int:
        raise WorkerReadyError("current_partition_invalid", "partition")
    if registry["c_run_id"] != partition.c_run_id:
        raise WorkerReadyError("registry_generation_mismatch", "c_run_id")
    if heartbeat["c_run_id"] != registry["c_run_id"]:
        raise WorkerReadyError("worker_generation_mismatch", "c_run_id")
    if registry["worker_count"] != partition.count or heartbeat["of"] != partition.count:
        raise WorkerReadyError("worker_count_mismatch", "of")


# LLM: 沿 B1a 的 15 秒健康窗口，额外核实际端口读回；心跳不是网络可达或 OS 沙箱证明。
# 函数用途: 同时核对健康状态、时间和原沙箱端口表投影。
def _healthy_readback(registry, heartbeat, partition, now) -> None:
    try:
        finite = type(now) in {int, float} and math.isfinite(now)
    except OverflowError:
        finite = False
    if not finite or not stampable_workers([heartbeat], partition, now):
        raise WorkerReadyError("worker_unhealthy", "ready")
    readiness = heartbeat["readiness"]
    ports = readiness.get("gateway_ports")
    if (readiness.get("ready") is not True or type(ports) is not list or not ports
            or any(type(port) is not int or not 0 < port <= 65535 for port in ports)
            or registry["http_port"] not in ports):
        raise WorkerReadyError("gateway_port_readback", "readiness")
