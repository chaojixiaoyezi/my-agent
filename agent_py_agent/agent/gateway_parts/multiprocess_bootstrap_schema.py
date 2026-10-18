# LLM: 原 records 唯一入口已核字段全集；此处复用其校验函数，不重新定义字段、身份或默认值。
# 模块用途: 核对 P4 原 registry 与实例心跳的语义，不读取 OS、不发布状态或授予 ready。
import math

from .multiprocess_records import (
    MultiprocessRecordError,
    _coordinator,
    _expect_schema,
    _identifier,
    _integer,
    _shape,
    validate_record,
)


# LLM: 字段全集已由原入口核过；此处不返回默认值、不读取 OS 或批准 ready。
# 函数用途: 按精确记录种类核对 P4 语义。
def validate_bootstrap_record(record: dict) -> None:
    if record["schema_version"] == "multiprocess_worker_registry.v1":
        _registry(record)
    else:
        _heartbeat(record)


# LLM: process_identity 必须是同一个 P0 版本，不能以 PID 或 B1a 未知出生投影代替。
# 函数用途: 校验嵌套进程身份。
def _process_identity(identity) -> None:
    _expect_schema(identity, "multiprocess_process_identity.v1")
    validate_record(identity)


# LLM: 分区只出现一次、实例号不复用；允许启动过程中空/部分集合，未登记分区不能 ready。
# 函数用途: 校验当前协调者、实际端口与唯一实例集合。
def _registry(record: dict) -> None:
    _coordinator(record)
    _process_identity(record["process_identity"])
    _integer(record["http_port"], "http_port")
    if record["http_port"] > 65535:
        raise MultiprocessRecordError("invalid_port", "http_port")
    workers = record["workers"]
    if type(workers) is not list:
        raise MultiprocessRecordError("invalid_workers", "workers")
    for entry in workers:
        _registry_entry(entry, record)
    for field in ("worker", "instance_id"):
        if len({entry[field] for entry in workers}) != len(workers):
            raise MultiprocessRecordError("duplicate_worker_identity", field)


# LLM: 执行者引用复用 P1a 唯一分区命名空间，不准任意路径或复制账本。
# 函数用途: 核对当前实例的分区、出生身份和账本位置。
def _registry_entry(entry, registry: dict) -> None:
    _shape(entry, ("worker", "instance_id", "process_identity", "executor_ledger_ref"), "workers")
    _integer(entry["worker"], "worker", 0)
    _identifier(entry["instance_id"], "instance_id")
    _process_identity(entry["process_identity"])
    if entry["worker"] >= registry["worker_count"]:
        raise MultiprocessRecordError("partition_out_of_range", "worker")
    if entry["process_identity"]["host_id"] != registry["process_identity"]["host_id"]:
        raise MultiprocessRecordError("worker_host_mismatch", "process_identity.host_id")
    if entry["executor_ledger_ref"] != f"workers/executors/w{entry['worker']}":
        raise MultiprocessRecordError("executor_ledger_ref_mismatch", "executor_ledger_ref")


# LLM: 心跳字段不放宽原 PID/分区语义，readiness 仅为原就绪结果投影；真正端口匹配归 ready 门。
# 函数用途: 拒绝 bool 数值、无出生身份、PID 自相矛盾和异常时间。
def _heartbeat(record: dict) -> None:
    _integer(record["worker"], "worker", 0)
    _integer(record["of"], "of")
    _integer(record["pid"], "pid")
    _integer(record["inflight"], "inflight", 0)
    _identifier(record["c_run_id"], "c_run_id")
    _identifier(record["instance_id"], "instance_id")
    _identifier(record["loop_health"], "loop_health")
    _process_identity(record["process_identity"])
    if record["worker"] >= record["of"] or record["pid"] != record["process_identity"]["pid"]:
        raise MultiprocessRecordError("heartbeat_identity_conflict", "process_identity")
    if type(record["ready"]) is not bool or type(record["readiness"]) is not dict:
        raise MultiprocessRecordError("invalid_readiness", "readiness")
    try:
        finite = type(record["updated_at"]) in {int, float} and math.isfinite(record["updated_at"]) and record["updated_at"] >= 0
    except OverflowError:
        finite = False
    if not finite:
        raise MultiprocessRecordError("invalid_stamp", "updated_at")
