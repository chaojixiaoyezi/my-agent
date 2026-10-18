# LLM: P0 v1/golden不变；P1 v2域字段不授死亡权；generation v2独立关闭范围仅由原producer封存，不复活lease/native或签P2。
#   本模块只定义结构，无文件、锁、进程、模型或配置副作用。同步 records 和协议17节。
# 模块用途: 定义唯一记录字段及显式版本，原代次关闭事实与旧count记录分开，旧版原字节保留。
from __future__ import annotations

from typing import Literal, TypedDict


# LLM: c_run_id 沿用 B1a，worker 的代次仍是它；不另设 epoch 或 generation 权威。
# 类用途: 记录协调者本次运行的分区数量。
class CoordinatorGeneration(TypedDict):
    schema_version: Literal["multiprocess_c_generation.v1"]
    c_run_id: str
    worker_count: int


# LLM: v1仍只有count；v2初始closure=None，只允许原正常producer一次封存完整范围，不提供P2/lease/native权限。
# 类用途: 沿唯一generation历史保存本代正常关闭事实，旧协议不补字段或追补关闭。
class CoordinatorGenerationV2(TypedDict):
    schema_version: Literal["multiprocess_c_generation.v2"]
    c_run_id: str
    worker_count: int
    closure: dict | None


# LLM: host_id/pid/start_time 沿 daemon_metadata 的身份合同，版本字段只包格式；未知出生身份拒绝。
# 类用途: 持久识别一个具体进程实例，避免 PID 复用被误认。
class ProcessIdentity(TypedDict):
    schema_version: Literal["multiprocess_process_identity.v1"]
    host_id: str
    pid: int
    start_time: int | str


# LLM: 不含 argv、环境或凭据；空身份槽只表示未完成登记，不代表执行树已停止。
# 类用途: 将执行者和精确请求、工具调用、执行轮、租约绑定。
class ExecutorReservation(TypedDict):
    schema_version: Literal["multiprocess_executor_reservation.v1"]
    reservation_id: str
    kind: str
    c_run_id: str
    owner_sha: str
    request_id: str
    execution_attempt_id: str
    lease_epoch: int
    tool_call_id: str
    state: Literal["reserved", "registered", "released", "abandoned"]
    process_identity: ProcessIdentity | None
    pgid: int | None
    sid: int | None


# LLM: 显式v2原P1字段与v1相同，execution_domain仅reserved可空；registered由原C内核捕获creator/init/ns/source角色，纯磁盘结构不授域历史证明。
# 类用途: 保留原唯一执行者记录并追加原R前物理域事实，旧v1不默补字段。
class ExecutorReservationV2(TypedDict):
    schema_version: Literal["multiprocess_executor_reservation.v2"]
    reservation_id: str
    kind: str
    c_run_id: str
    owner_sha: str
    request_id: str
    execution_attempt_id: str
    lease_epoch: int
    tool_call_id: str
    state: Literal["reserved", "registered", "released", "abandoned"]
    process_identity: ProcessIdentity | None
    pgid: int | None
    sid: int | None
    execution_domain: dict | None


# LLM: route 沿 B1a 的 owner/binding/c_run_id 结构；终态仅存原事实引用及摘要，不复制响应正文。
# 类用途: 保留跨目录、跨代、隔离分区都计入占用的原预留。
class AdmissionReservation(TypedDict):
    schema_version: Literal["multiprocess_admission_reservation.v1"]
    reservation_id: str
    owner_sha: str
    session_scope: dict
    request_id: str
    route: dict
    state: Literal["occupied", "released"]
    terminal_fact: dict | None


# LLM: v1 字段与 golden 不变；v2 显式替换会话范围为唯一版本化顺序键摘要。
# 类用途: 给 P3 共用投影预留正式格式，不在 C 中解析内部 thread。
class AdmissionReservationV2(TypedDict):
    schema_version: Literal["multiprocess_admission_reservation.v2"]
    reservation_id: str
    owner_sha: str
    session_scope: dict
    request_id: str
    route: dict
    state: Literal["occupied", "released"]
    terminal_fact: dict | None


# LLM: 证明必须有平台方法、覆盖范围和残余风险；这里只验证结构，不作真实存活探测。
# 类用途: 区分整树范围内停止、仍活和无法判断，供 P2 失败即隔离使用。
class TakeoverProof(TypedDict):
    schema_version: Literal["multiprocess_takeover_proof.v1"]
    c_run_id: str
    status: Literal["stopped", "live", "unknown"]
    platform: str
    method: str
    covered_scope: dict
    unresolved_instances: list[str]
    residual_risks: list[str]


# LLM: C 独立范围不能借用 W 编号；范围代次与顶层同源，旧 v1 编码保持不变。
# 类用途: 以显式角色、所属代次及原 owner/执行者引用表达接手证据。
class TakeoverProofV2(TypedDict):
    schema_version: Literal["multiprocess_takeover_proof.v2"]
    c_run_id: str
    status: Literal["stopped", "live", "unknown"]
    platform: str
    method: str
    covered_scope: dict
    unresolved_instances: list[str]
    residual_risks: list[str]


# LLM: registry 是协议约定的当前 worker 登记；实例号不是另一代次，C 仍只有 c_run_id。
# 类用途: 保存当前协调者、实际绑定端口及已登记 worker 的完整快照。
class WorkerRegistry(TypedDict):
    schema_version: Literal["multiprocess_worker_registry.v1"]
    c_run_id: str
    worker_count: int
    process_identity: ProcessIdentity
    http_port: int
    workers: list[dict]


# LLM: B1a 心跳成为显式版本信封，身份使用同一个 P0 格式；健康投影不提供接手权。
# 类用途: 携带 worker 实例号、出生身份、端口读回及原健康字段。
class WorkerHeartbeat(TypedDict):
    schema_version: Literal["multiprocess_worker_heartbeat.v1"]
    worker: int
    of: int
    pid: int
    process_identity: ProcessIdentity
    c_run_id: str
    instance_id: str
    ready: bool
    loop_health: str
    inflight: int
    updated_at: float
    readiness: dict


# LLM: 仅投影原 processing 取消事实；不得创建第二份取消权威或据此绕过 P5 最新实读。
# 类用途: 运输一次精确在途取消的原身份与提交时间。
class CancellationRecord(TypedDict):
    schema_version: Literal["multiprocess_cancellation.v1"]
    request_id: str
    execution_attempt_id: str
    lease_epoch: int
    lease_owner: str
    worker_index: int
    worker_count: int
    c_run_id: str
    owner: list[str]
    route_binding: str
    cancel_requested_at: float


# 旧 v1 字段全集冻结，只增独立记录种类，不通过运行对象 __dict__/asdict 扩展旧摘要。
RECORD_V1_FIELDS = {
    "multiprocess_c_generation.v1": ("schema_version", "c_run_id", "worker_count"),
    "multiprocess_process_identity.v1": ("schema_version", "host_id", "pid", "start_time"),
    "multiprocess_executor_reservation.v1": (
        "schema_version", "reservation_id", "kind", "c_run_id", "owner_sha", "request_id",
        "execution_attempt_id", "lease_epoch", "tool_call_id", "state", "process_identity", "pgid", "sid",
    ),
    "multiprocess_admission_reservation.v1": (
        "schema_version", "reservation_id", "owner_sha", "session_scope", "request_id", "route", "state", "terminal_fact",
    ),
    "multiprocess_takeover_proof.v1": (
        "schema_version", "c_run_id", "status", "platform", "method", "covered_scope", "unresolved_instances", "residual_risks",
    ),
    "multiprocess_worker_registry.v1": (
        "schema_version", "c_run_id", "worker_count", "process_identity", "http_port", "workers",
    ),
    "multiprocess_worker_heartbeat.v1": (
        "schema_version", "worker", "of", "pid", "process_identity", "c_run_id", "instance_id",
        "ready", "loop_health", "inflight", "updated_at", "readiness",
    ),
    "multiprocess_cancellation.v1": (
        "schema_version", "request_id", "execution_attempt_id", "lease_epoch", "lease_owner",
        "worker_index", "worker_count", "c_run_id", "owner", "route_binding", "cancel_requested_at",
    ),
}

# 新版固定字段独立声明；不得更新旧版字段全集或对旧记录静默补 schema 默认值。
RECORD_V2_FIELDS = {
    "multiprocess_c_generation.v2": (*RECORD_V1_FIELDS["multiprocess_c_generation.v1"], "closure"),
    "multiprocess_executor_reservation.v2": (*RECORD_V1_FIELDS["multiprocess_executor_reservation.v1"], "execution_domain"),
    "multiprocess_admission_reservation.v2": (
        "schema_version", "reservation_id", "owner_sha", "session_scope", "request_id", "route", "state", "terminal_fact",
    ),
    "multiprocess_takeover_proof.v2": (
        "schema_version", "c_run_id", "status", "platform", "method", "covered_scope", "unresolved_instances", "residual_risks",
    ),
}
