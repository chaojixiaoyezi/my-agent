# LLM: 唯一纯记录接口；generation v2只校完整关闭范围，不认证producer或签P2；旧v1/P1/P3字节和校验保持。
#   放行、整树死亡、终态来源均由未来 P1/P2/P3 在真实锁和持久边界证明，本接口不替代它们。
# 模块用途: 提供版本记录、规范编码和状态转换签名，不启动执行者或创建目录。
from __future__ import annotations

import copy
import hashlib
import json
import math
import re

from ..common.strict_json import load_strict_json
from ..user_space.owner_resolver import OwnerIdentity
from .multiprocess_record_schema import RECORD_V1_FIELDS, RECORD_V2_FIELDS
from .multiprocess_route import owner_digest


# LLM: 错误只暴露字段和原因，不回显可能含敏感信息的输入；同步记录合同测试。
# 类用途: 让未来账本调用方获得可机器处理的校验错误。
class MultiprocessRecordError(ValueError):
    error_code = "MULTIPROCESS_RECORD_INVALID"

    # LLM: 只保存拒绝原因和字段，无 I/O。
    # 函数用途: 构造结构化拒绝。
    def __init__(self, reason: str, field: str = "") -> None:
        self.reason, self.field = reason, field
        super().__init__(reason)

    # LLM: 不包含输入正文；调用方不得将失败视作默认记录。
    # 函数用途: 生成可运输的错误回执。
    def to_payload(self) -> dict:
        return {"ok": False, "error_code": self.error_code, "reason": self.reason, "field": self.field}


# LLM: 精确版本决定字段全集；v2 generation独立完整形状，旧v1/P1/P3校验不补字段，未知版本仍结构化拒绝。
# 函数用途: 验证并返回独立记录副本。
def validate_record(payload: object) -> dict:
    if type(payload) is not dict:
        raise MultiprocessRecordError("record_not_object")
    version = payload.get("schema_version")
    fields = {**RECORD_V1_FIELDS, **RECORD_V2_FIELDS}
    if type(version) is not str or version not in fields:
        raise MultiprocessRecordError("unsupported_schema", "schema_version")
    _shape(payload, fields[version], "record")
    validators = {
        "multiprocess_c_generation.v1": _coordinator,
        "multiprocess_c_generation.v2": _closed_generation,
        "multiprocess_process_identity.v1": _identity,
        "multiprocess_executor_reservation.v1": _executor,
        "multiprocess_executor_reservation.v2": _executor_v2,
        "multiprocess_admission_reservation.v1": _admission,
        "multiprocess_admission_reservation.v2": _admission,
        "multiprocess_takeover_proof.v1": _proof,
        "multiprocess_takeover_proof.v2": _proof,
        "multiprocess_worker_registry.v1": _bootstrap_record,
        "multiprocess_worker_heartbeat.v1": _bootstrap_record,
        "multiprocess_cancellation.v1": _stop_record,
    }
    validators[version](payload)
    return copy.deepcopy(payload)


# LLM: 延迟导入防止校验辅助与原公共验证入口循环依赖；不增加第二套编码或字段权威。
# 函数用途: 分派 P4 的 registry/心跳结构校验。
def _bootstrap_record(payload: dict) -> None:
    from .multiprocess_bootstrap_schema import validate_bootstrap_record

    validate_bootstrap_record(payload)


# LLM: 取消格式复用唯一纯schema实现，延迟导入仅断开记录helper回引；未来P6运行消费者须导同一函数，不复制校验或启动dispatch。
# 函数用途: 分派原取消事实运输格式的校验。
def _stop_record(payload: dict) -> None:
    from .multiprocess_stop_schema import validate_cancellation_record

    validate_cancellation_record(payload)


# LLM: 纯格式只绑定完整C/W与原路径摘要，不能由JSON签发退出或P2；同步原publisher/runtime来源检查及v1 golden。
# 函数用途: 校验显式v2代次关闭范围，不读盘、不升级旧记录。
def _closed_generation(value):
    _coordinator(value)
    closure = value['closure']
    if closure is None:
        return
    _shape(closure, ('registry', 'registry_sha256', 'role_markers', 'source_files', 'tree_evidence'), 'closure')
    registry = closure['registry']
    _expect_schema(registry, 'multiprocess_worker_registry.v1')
    validate_record(registry)
    if (registry['c_run_id'] != value['c_run_id'] or registry['worker_count'] != value['worker_count']
            or {row['worker'] for row in registry['workers']} != set(range(value['worker_count']))):
        raise MultiprocessRecordError('generation_role_coverage_invalid', 'closure')
    _sha(closure['registry_sha256'], 'registry_sha256')
    if hashlib.sha256(_canonical_bytes(registry)).hexdigest() != closure['registry_sha256']:
        raise MultiprocessRecordError('generation_registry_digest_invalid', 'closure')
    roles = {'gateway_state.json', *(f'workers/w{i}.json' for i in range(value['worker_count']))}
    _closure_digests(closure['role_markers'], roles=roles)
    _closure_digests(closure['source_files'])
    _closure_digests(closure['tree_evidence'], evidence=True)


# LLM: 路径只可落原固定命名空间，摘要完整且无相对跳转；格式合法仍不是关闭权威。
# 函数用途: 拒绝生命周期记录里的任意路径或坏摘要，不进行文件访问。
def _closure_digests(values, *, roles=None, evidence=False):
    if type(values) is not dict or (roles is not None and set(values) != roles):
        raise MultiprocessRecordError('generation_manifest_invalid', 'closure')
    source_pattern = (r'workers/executors/w(0|[1-9][0-9]*)/[0-9a-f]{64}\.json|'
                      r'admission/reservations/[A-Za-z0-9_.-]+\.json|'
                      r'requests/processing/[A-Za-z0-9_.-]+\.json|control_operations/[A-Za-z0-9_.-]+\.json')
    for path, digest in values.items():
        pattern = r'workers/takeover_evidence/[0-9a-f]{64}\.json' if evidence else source_pattern
        if type(path) is not str or (roles is None and re.fullmatch(pattern, path) is None):
            raise MultiprocessRecordError('generation_manifest_path_invalid', 'closure')
        _sha(digest, 'closure')


# LLM: 规范编码入口，无文件写入。
# 函数用途: 返回版本固定的 UTF-8 字节。
def serialize_record(payload: object) -> bytes:
    return _canonical_bytes(validate_record(payload))


# LLM: 只从版本规范字节计算摘要。
# 函数用途: 返回跨进程一致的记录摘要。
def record_digest(payload: object) -> str:
    return hashlib.sha256(serialize_record(payload)).hexdigest()


# LLM: 严格解码入口，不作缺字段回退。
# 函数用途: 解码记录字节并验证。
def deserialize_record(content: bytes) -> dict:
    try:
        payload = load_strict_json(content)
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise MultiprocessRecordError("invalid_json") from exc
    return validate_record(payload)


# LLM: 显式v1/v2各自固定registration字段；v1字节不变，v2原namespace事实不补默认/不迁移旧账，转换不代表R或整树死亡；同步P0/P1 tests。
# 函数用途: 校验执行者状态迁移。
def advance_executor(payload: dict, state: str, registration: dict | None = None) -> dict:
    result = validate_record(payload)
    if result["schema_version"] not in {"multiprocess_executor_reservation.v1", "multiprocess_executor_reservation.v2"}:
        raise MultiprocessRecordError("wrong_record_kind", "schema_version")
    transitions = {"reserved": {"registered", "abandoned"}, "registered": {"released", "abandoned"},
                   "released": set(), "abandoned": set()}
    if type(state) is not str or state not in transitions[result["state"]]:
        raise MultiprocessRecordError("invalid_executor_transition", "state")
    if state == "registered":
        fields = ("process_identity", "pgid", "sid")
        if result["schema_version"] == "multiprocess_executor_reservation.v2":
            fields += ("execution_domain",)
        _shape(registration, fields, "registration")
        result.update(copy.deepcopy(registration))
    elif registration is not None:
        raise MultiprocessRecordError("unexpected_registration", "registration")
    result["state"] = state
    return validate_record(result)


# LLM: v1/v2 保留各自会话格式，只消费 P3 在原终态锁内读出的规范信封，不自行查目录。
# 函数用途: 校验终态释放，不以线程返回或投影释放占用。
def release_admission(payload: dict, terminal: dict) -> dict:
    result = validate_record(payload)
    if result["schema_version"] not in {"multiprocess_admission_reservation.v1", "multiprocess_admission_reservation.v2"}:
        raise MultiprocessRecordError("wrong_record_kind", "schema_version")
    _canonical_terminal(result, terminal)
    fact = {"request_id": result["request_id"], "canonical_ref": f"terminal/{result['request_id']}.json",
            "record_sha256": hashlib.sha256(_canonical_bytes(terminal)).hexdigest()}
    if result["state"] == "released" and result["terminal_fact"] != fact:
        raise MultiprocessRecordError("terminal_fact_conflict", "terminal_fact")
    result.update(state="released", terminal_fact=fact)
    return validate_record(result)


# LLM: 规范 JSON 与 B1a 摘要相同的 UTF-8/sort_keys/无空白/禁 NaN 口径，不编码对象默认字段。
# 函数用途: 为固定线格式生成字节，坏值只返回结构化原因而不回显正文。
def _canonical_bytes(value: dict) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise MultiprocessRecordError("noncanonical_json") from exc


# LLM: 线格式只认精确字段，不忽略未知键；新版本须独立冻结，不能影响旧版编码。
# 函数用途: 检查必填字段全集，阻止隐藏命令、凭据及不完整记录混入账本。
def _shape(value: object, names: tuple[str, ...], field: str) -> None:
    if type(value) is not dict or set(value) != set(names):
        raise MultiprocessRecordError("invalid_fields", field)


# LLM: 类型断言以 type 判断，bool 不能充当 PID、分区数或租约。
# 函数用途: 检查非负或正整数，拒绝静默转换。
def _integer(value: object, field: str, minimum: int = 1) -> None:
    if type(value) is not int or value < minimum:
        raise MultiprocessRecordError("invalid_integer", field)


# LLM: 身份采用路径安全的非空标识，不让命令正文落在任意字符串字段；不补空值。
# 函数用途: 检查请求、预留、运行和工具身份的线格式。
def _identifier(value: object, field: str) -> None:
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value) is None:
        raise MultiprocessRecordError("invalid_identifier", field)


# LLM: owner/binding/终态摘要均必须为完整小写 SHA256，不能用展示短哈希。
# 函数用途: 检查不可歧义的摘要字段。
def _sha(value: object, field: str) -> None:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise MultiprocessRecordError("invalid_digest", field)


# LLM: 纯操作不能接收其它合法记录冒充目标种类；不隐式转换。
# 函数用途: 限定状态转换的记录类型。
def _expect_schema(value: object, schema: str) -> None:
    if type(value) is not dict or value.get("schema_version") != schema:
        raise MultiprocessRecordError("wrong_record_kind", "schema_version")


# LLM: 协调者运行号即 worker 代次，只保存原 c_run_id；零 count 属于默认模式而非内部记录。
# 函数用途: 校验唯一 C 代次和正分区数。
def _coordinator(value: dict) -> None:
    _identifier(value["c_run_id"], "c_run_id")
    _integer(value["worker_count"], "worker_count")


# LLM: 兼容 daemon_metadata 的 Linux ticks 和 macOS 出生字符串，不认可 None 或 PID-only 身份。
# 函数用途: 校验同主机具体进程实例，不调用 ps 或判断它是否存活。
def _identity(value: dict) -> None:
    _identifier(value["host_id"], "host_id")
    _integer(value["pid"], "pid")
    birth = value["start_time"]
    integer_birth = type(birth) is int and birth >= 0
    text_birth = (type(birth) is str and 0 < len(birth) <= 256 and birth == birth.strip()
                  and all(ord(char) >= 32 for char in birth))
    if not integer_birth and not text_birth:
        raise MultiprocessRecordError("birth_identity_required", "start_time")


# LLM: reserved 槽全空；registered/released 槽必须完整。abandoned 保留原完整或全空槽，不能充当死亡证明。
# 函数用途: 校验执行者绑定及当前登记状态。
def _executor(value: dict) -> None:
    for field in ("reservation_id", "kind", "c_run_id", "request_id", "execution_attempt_id", "tool_call_id"):
        _identifier(value[field], field)
    _sha(value["owner_sha"], "owner_sha")
    _integer(value["lease_epoch"], "lease_epoch", 0)
    state = value["state"]
    if type(state) is not str or state not in {"reserved", "registered", "released", "abandoned"}:
        raise MultiprocessRecordError("invalid_executor_state", "state")
    empty = all(value[field] is None for field in ("process_identity", "pgid", "sid"))
    if state == "reserved" and not empty:
        raise MultiprocessRecordError("reserved_has_process", "process_identity")
    if empty and state in {"reserved", "abandoned"}:
        return
    _expect_schema(value["process_identity"], "multiprocess_process_identity.v1")
    validate_record(value["process_identity"])
    _integer(value["pgid"], "pgid")
    _integer(value["sid"], "sid")


# LLM: v2只新增原执行域事实，保v1原身份/FSM；空槽只限reserved/abandoned，完整domain必须匹配原process_identity，不授死亡/执行权。
# 函数用途: 校验显式Linux PID namespace记录，不给旧v1自动补监控证明。
def _executor_v2(value: dict) -> None:
    from .multiprocess_executor_domain_schema import validate_execution_domain
    _executor(value)
    domain = value['execution_domain']
    if value['process_identity'] is None:
        if domain is not None:
            raise MultiprocessRecordError('empty_executor_has_domain', 'execution_domain')
        return
    validate_execution_domain(domain, value['process_identity'])


# LLM: v1 外部会话文本仅兼容解码；v2 采用唯一投影。两版均不改变 route/owner 或终态权威。
# 函数用途: 校验准入预留和唯一终态引用，不枚举 pending/processing 或创建锁。
def _admission(value: dict) -> None:
    for field in ("reservation_id", "request_id"):
        _identifier(value[field], field)
    _sha(value["owner_sha"], "owner_sha")
    _admission_scope(value["schema_version"], value["session_scope"])
    _route(value["route"], value["owner_sha"])
    if value["state"] == "occupied" and value["terminal_fact"] is None:
        return
    if value["state"] != "released":
        raise MultiprocessRecordError("invalid_admission_state", "state")
    _terminal_fact(value["terminal_fact"], value["request_id"])


# LLM: 修正把外部会话号误当路径的 v1 拒绝域，但绝不增删旧字段；正式共同合同是显式 v2。
# 函数用途: 分版本验证会话范围，拒绝跨版本夹带默认字段。
def _admission_scope(version: str, value: object) -> None:
    from .multiprocess_session_key import session_text, validate_session_scope

    if version == "multiprocess_admission_reservation.v2":
        validate_session_scope(value)
        return
    _shape(value, ("channel", "thread_id"), "session_scope")
    for field in ("channel", "thread_id"):
        session_text(value[field])


# LLM: 复用 B1a 的 owner 三元组及完整摘要，当前记录并不校验最新身份配置或授予执行权。
# 函数用途: 拒绝自洽但非规范的 owner、类型错误、空 binding 和 owner/SHA 不同源。
def _route(route: object, owner_sha: str) -> None:
    _shape(route, ("worker", "owner", "owner_sha", "c_run_id", "stamped_at", "binding"), "route")
    _identifier(route["c_run_id"], "route.c_run_id")
    _sha(route["binding"], "route.binding")
    _sha(route["owner_sha"], "route.owner_sha")
    owner = _canonical_owner(route["owner"])
    if owner_digest(owner) != owner_sha or route["owner_sha"] != owner_sha:
        raise MultiprocessRecordError("owner_route_conflict", "route.owner_sha")
    if owner == OwnerIdentity.local_main():
        if route["worker"] != "C":
            raise MultiprocessRecordError("wrong_coordinator_route", "route.worker")
    else:
        _integer(route["worker"], "route.worker", 0)
    stamp = route["stamped_at"]
    try:
        valid_stamp = type(stamp) in {int, float} and math.isfinite(stamp) and stamp >= 0
    except OverflowError:
        valid_stamp = False
    if not valid_stamp:
        raise MultiprocessRecordError("invalid_stamp", "route.stamped_at")


# LLM: 使用 owner_resolver 的构造规范，拒绝清洗后才相同的别名，不读配置或物化 owner。
# 函数用途: 从数组恢复精确规范身份。
def _canonical_owner(raw: object) -> OwnerIdentity:
    if type(raw) is not list or len(raw) != 3 or any(type(item) is not str or not item for item in raw):
        raise MultiprocessRecordError("invalid_owner", "route.owner")
    owner = OwnerIdentity(*raw)
    constructors = {"main": lambda: OwnerIdentity.local_main(),
                    "user": lambda: OwnerIdentity.provider_user(owner.provider, owner.owner_id),
                    "group": lambda: OwnerIdentity.provider_group(owner.provider, owner.owner_id)}
    constructor = constructors.get(owner.owner_kind)
    if constructor is None or constructor() != owner:
        raise MultiprocessRecordError("noncanonical_owner", "route.owner")
    return owner


# LLM: 释放记录仅保存原规范事实的相对引用与摘要，不能保存 responses/done 投影作为依据。
# 函数用途: 校验匹配本请求的唯一终态引用。
def _terminal_fact(value: object, request_id: str) -> None:
    _shape(value, ("request_id", "canonical_ref", "record_sha256"), "terminal_fact")
    if value["request_id"] != request_id or value["canonical_ref"] != f"terminal/{request_id}.json":
        raise MultiprocessRecordError("noncanonical_terminal_ref", "terminal_fact")
    _sha(value["record_sha256"], "terminal_fact.record_sha256")


# LLM: 必须由 P3 在原 gateway_turn_transition 内读取 terminal/<id>.json；本纯函数无法认证磁盘来源。
#   信封沿 recovery._terminal_gateway_request_payload，不创造终态权威；旧 C 路由可变而 binding/owner 不变。
# 函数用途: 拒绝响应投影、未闭合请求、错 owner/请求绑定和不完整终态，绝不凭线程返回释放。
def _canonical_terminal(reservation: dict, terminal: object) -> None:
    if type(terminal) is not dict or terminal.get("schema_version") != "gateway_terminal_request.v1":
        raise MultiprocessRecordError("canonical_terminal_required", "terminal")
    response = terminal.get("terminal_response")
    if (terminal.get("id") != reservation["request_id"] or terminal.get("turn_phase") != "closed"
            or type(response) is not dict or response.get("id") != reservation["request_id"]
            or type(response.get("ok")) is not bool or type(response.get("status")) is not str
            or response["status"] not in {"done", "failed", "cancelled", "interrupted"}
            or terminal.get("status") != response["status"]):
        raise MultiprocessRecordError("terminal_binding_conflict", "terminal")
    route = terminal.get("route")
    _route(route, reservation["owner_sha"])
    if route["binding"] != reservation["route"]["binding"]:
        raise MultiprocessRecordError("terminal_request_binding_conflict", "terminal.route.binding")


# LLM: v1/v2 状态和证明能力分别校验；显式角色不扩大覆盖，stopped 仍不是 P2 接手许可。
# 函数用途: 保存平台枚举方法、未结清实例和范围残余风险，不做真实树探测。
def _proof(value: dict) -> None:
    _identifier(value["c_run_id"], "c_run_id")
    status = value["status"]
    if type(status) is not str or status not in {"stopped", "live", "unknown"}:
        raise MultiprocessRecordError("invalid_proof_status", "status")
    methods = {"linux": {"pid_namespace", "cgroup", "proc_pgid_sid", "unavailable"},
               "darwin": {"sysctl_pgid_sid", "unavailable"}, "win32": {"unavailable"}}
    platform, method = value["platform"], value["method"]
    if type(platform) is not str or type(method) is not str or method not in methods.get(platform, set()):
        raise MultiprocessRecordError("invalid_platform_method", "method")
    generation = value["c_run_id"] if value["schema_version"] == "multiprocess_takeover_proof.v2" else None
    _proof_scope(value["covered_scope"], generation)
    _identifiers(value["unresolved_instances"], "unresolved_instances")
    _identifiers(value["residual_risks"], "residual_risks")
    if status == "stopped" and (value["unresolved_instances"] or method == "unavailable"):
        raise MultiprocessRecordError("tree_not_proven_stopped", "status")
    if method in {"proc_pgid_sid", "sysctl_pgid_sid"} and not value["residual_risks"]:
        raise MultiprocessRecordError("platform_residual_risk_required", "residual_risks")


# LLM: v2 显式 C/W 与代次；v1 兼容 C 标签而不动字段/golden，均保留 owner 与账本引用。
# 函数用途: 校验证明范围，不允许 C 冒用某个 W 编号或越界范围。
def _proof_scope(value: object, generation: str | None = None) -> None:
    fields = ("state_scope", "worker_index", "worker_count", "owner_shas", "executor_reservation_ids")
    _shape(value, fields + (("role", "c_run_id") if generation is not None else ()), "covered_scope")
    if value["state_scope"] != "agent_state":
        raise MultiprocessRecordError("wrong_state_scope", "covered_scope.state_scope")
    _integer(value["worker_count"], "covered_scope.worker_count")
    if generation is not None:
        _proof_role(value, generation)
    elif not (type(value["worker_index"]) is str and value["worker_index"] == "C"):
        _proof_worker_index(value)
    if type(value["owner_shas"]) is not list or not value["owner_shas"]:
        raise MultiprocessRecordError("owner_scope_required", "covered_scope.owner_shas")
    for sha in value["owner_shas"]:
        _sha(sha, "covered_scope.owner_shas")
    if len(set(value["owner_shas"])) != len(value["owner_shas"]):
        raise MultiprocessRecordError("duplicate_owner_scope", "covered_scope.owner_shas")
    _identifiers(value["executor_reservation_ids"], "covered_scope.executor_reservation_ids")


# LLM: v2 C 明确无 W 编号；W 要合法整数，所属代次必须与原证明同源，不静默补默认。
# 函数用途: 拒绝角色、分区和代次互相冒充。
def _proof_role(value: dict, generation: str) -> None:
    role = value["role"]
    if type(role) is not str or role not in {"C", "W"} or value["c_run_id"] != generation:
        raise MultiprocessRecordError("proof_role_generation_conflict", "covered_scope")
    if role == "C":
        if value["worker_index"] is not None:
            raise MultiprocessRecordError("coordinator_has_worker_index", "covered_scope.worker_index")
        return
    _proof_worker_index(value)


# LLM: W 分区沿 B1a 的严格整数语义，bool/字符串/PID 都不能冒充分区。
# 函数用途: 核对 worker 编号在正 count 的固定范围内。
def _proof_worker_index(value: dict) -> None:
    _integer(value["worker_index"], "covered_scope.worker_index", 0)
    if value["worker_index"] >= value["worker_count"]:
        raise MultiprocessRecordError("partition_out_of_range", "covered_scope.worker_index")


# LLM: 未解决实例、风险和账本引用均为结构化唯一标识，不能用报告文字影响机器决定。
# 函数用途: 校验无重复的标识列表。
def _identifiers(value: object, field: str) -> None:
    if type(value) is not list:
        raise MultiprocessRecordError("invalid_identifier_list", field)
    for item in value:
        _identifier(item, field)
    if len(set(value)) != len(value):
        raise MultiprocessRecordError("duplicate_identifier", field)
