# LLM: 原native stderr诊断的有限公开schema；只投影机器枚举，不把日志变成权限、健康或任务终态事实，不新增I/O或日志账。
# 模块用途: 共用原producer安全值并验证日志reader的有界无正文投影。
from __future__ import annotations

import re

from ..common.strict_json import load_strict_json

# 常量用途: 与原producer同一已审拒绝literal集合；未知异常正文不能借reason输出。
NATIVE_REJECTION_REASONS = {
    "commit_connection_eof",
    "commit_connection_unconfirmed",
    "commit_frame_duplicate_key",
    "commit_frame_invalid",
    "commit_frame_size_invalid",
    "commit_owner_invalid",
    "commit_request_invalid",
    "executor_source_observer_invalid",
    "kernel_stream_state_unavailable",
    "native_bootstrap_invalid",
    "native_client_closed",
    "native_consumer_stop_unconfirmed",
    "native_control_fds_unexpected",
    "native_executor_consumer_not_stopped",
    "native_executor_consumer_stop_unconfirmed",
    "native_executor_context_fd_invalid",
    "native_executor_context_fd_required",
    "native_executor_context_invalid",
    "native_executor_context_size_invalid",
    "native_executor_context_timeout",
    "native_executor_delivery_unknown",
    "native_executor_request_binding_required",
    "native_executor_stdio_invalid",
    "native_executor_text_invalid",
    "native_host_bootstrap_invalid",
    "native_host_consumer_stop_unconfirmed",
    "native_namespace_pidfd_abi_unavailable",
    "native_namespace_pidfd_instance_unconfirmed",
    "native_namespace_reap_instance_changed",
    "native_namespace_reap_observation_gap",
    "native_namespace_reference_birth_changed",
    "native_namespace_reference_observation_gap",
    "native_namespace_reference_unavailable",
    "native_namespace_signal_observation_gap",
    "native_operation_consumed",
    "native_operation_unavailable",
    "native_pid_namespace_pre_release_unconfirmed",
    "native_reply_invalid",
    "native_reply_unconfirmed",
    "native_session_closed",
    "original_channel_eof",
    "original_channel_peer_required",
    "original_channel_pin_required",
    "original_channel_process_required",
    "original_channel_stream_required",
    "original_commit_handle_required",
    "original_commit_issuer_required",
    "original_commit_lease_required",
    "original_commit_pin_required",
    "original_coordinator_context_required",
    "original_coordinator_executor_binding_required",
    "original_coordinator_executor_callback_required",
    "original_coordinator_executor_effect_required",
    "original_coordinator_executor_pin_required",
    "original_coordinator_executor_source_changed",
    "original_coordinator_executor_source_required",
    "original_coordinator_executor_source_unavailable",
    "original_coordinator_executor_submission_required",
    "original_coordinator_message_required",
    "original_coordinator_native_host_process_required",
    "original_coordinator_native_host_required",
    "original_coordinator_native_host_source_required",
    "original_coordinator_parent_required",
    "original_coordinator_process_required",
    "original_executor_effect_required",
    "original_executor_owner_required",
    "original_executor_process_required",
    "original_executor_reap_unconfirmed",
    "original_executor_request_required",
    "original_executor_source_changed",
    "original_executor_source_required",
    "original_executor_source_unavailable",
    "original_executor_submission_required",
    "original_idle_worker_required",
    "original_namespace_reference_required",
    "original_native_admission_required",
    "original_native_client_required",
    "original_native_command_attempt_required",
    "original_native_consumer_already_started",
    "original_native_endpoint_required",
    "original_native_executor_client_required",
    "original_native_executor_consumer_already_started",
    "original_native_executor_domain_pending",
    "original_native_executor_lane_already_bound",
    "original_native_executor_lane_required",
    "original_native_executor_lane_retired",
    "original_native_executor_lane_source_required",
    "original_native_executor_lane_unbound",
    "original_native_executor_process_required",
    "original_native_executor_responsibility_pending",
    "original_native_host_admission_required",
    "original_native_host_already_connected",
    "original_native_host_authority_required",
    "original_native_host_binding_required",
    "original_native_host_completion_unconfirmed",
    "original_native_host_exit_observation_required",
    "original_native_host_kernel_instance_required",
    "original_native_host_lane_required",
    "original_native_host_lane_retired",
    "original_native_host_not_connected",
    "original_native_host_process_required",
    "original_native_host_reaped_before_completion",
    "original_native_host_responsibility_pending",
    "original_native_host_source_required",
    "original_native_host_start_pin_required",
    "original_native_host_start_required",
    "original_native_lane_already_bound",
    "original_native_lane_required",
    "original_native_lane_retired",
    "original_native_lane_source_required",
    "original_native_lane_unbound",
    "original_native_namespace_attempt_required",
    "original_native_namespace_fact_required",
    "original_native_namespace_pin_required",
    "original_native_namespace_process_required",
    "original_native_namespace_reference_required",
    "original_native_namespace_registration_required",
    "original_native_namespace_required",
    "original_native_namespace_source_required",
    "original_native_operation_conflict",
    "original_native_operation_required",
    "original_native_owner_home_required",
    "original_native_responsibility_pending",
    "original_native_session_required",
    "original_native_singleton_domain_required",
    "original_native_source_required",
    "original_native_waiting_singleton_required",
    "original_peer_channel_required",
    "original_worker_launch_required",
    "original_worker_process_required",
    "original_worker_registry_required",
    "original_worker_responsibility_pending",
    "original_worker_source_conflict",
    "original_worker_source_required",
    "original_worker_sources_changed",
    "owner_commit_unavailable",
    "owner_executor_commit_unavailable",
    "owner_partition_mismatch",
}
NATIVE_STAGES = frozenset(
    {
        "original_source",
        "receive",
        "authenticate",
        "decode",
        "identity",
        "dispatch",
        "send",
        "worker_future_result",
    }
)
NATIVE_LINKED_STAGES = frozenset({"dispatch", "send", "worker_future_result"})
NATIVE_OPS = frozenset({"begin", "apply", "finish", "materialize"})
NATIVE_KERNEL_CODES = frozenset(
    {
        "KERNEL_MESSAGE_FORKED",
        "KERNEL_MESSAGE_COPIED",
        "KERNEL_MESSAGE_IO",
        "KERNEL_MESSAGE_MALFORMED",
        "KERNEL_MESSAGE_INVALID",
        "KERNEL_MESSAGE_TOO_LARGE",
        "KERNEL_MESSAGE_CLOSED",
        "KERNEL_MESSAGE_TIMEOUT",
        "KERNEL_MESSAGE_UNAVAILABLE",
        "KERNEL_MESSAGE_UNSUPPORTED_LOCATOR",
    }
)
# 常量用途: worker_home原enumerate(_owner_directories)固定26项；正式回归绑定原generator，任意字段或越界index不公开。
NATIVE_OWNER_RESOURCE_FIELDS = frozenset(
    {"owner_home", *(f"owner_directory_{index}" for index in range(26))}
)
_NATIVE_STANDARD_ERRORS = frozenset(
    {
        "RuntimeError",
        "ValueError",
        "TypeError",
        "OSError",
        "PermissionError",
        "FileNotFoundError",
        "EOFError",
        "BrokenPipeError",
        "ConnectionError",
        "TimeoutError",
        "InterruptedError",
        "AssertionError",
    }
)
_NATIVE_FIELDS = frozenset(
    {
        "schema_version",
        "stage",
        "op",
        "seq",
        "request",
        "attempt",
        "error_type",
        "error_code",
        "reason",
        "field",
    }
)
_NATIVE_PREFIX = "[gateway-native-diagnostic] "


# LLM: 仅既有不安全owner路径和原缺目录的有限机器pair公开；不读异常str/路径或文件系统。
# 函数用途: 判断原资源错误是否可安全保留原因及字段。
def native_resource_pair_is_public(reason: object, field: object) -> bool:
    return (
        type(reason) is str
        and type(field) is str
        and (
            (reason, field) == ("resource_unsafe_path", "owner_home")
            or reason == "resource_missing"
            and field in NATIVE_OWNER_RESOURCE_FIELDS
        )
    )


# LLM: 原producer只写SHA及UTF8长度；校验不恢复ID，也不将其返回公开模型投影。
# 函数用途: 拒绝夹带正文的关联对象、布尔数字和不可能长度。
def _valid_fingerprint(value: object) -> bool:
    if value is None:
        return True
    return (
        type(value) is dict
        and set(value) == {"sha256", "chars", "bytes"}
        and type(value["sha256"]) is str
        and re.fullmatch("[0-9a-f]{64}", value["sha256"]) is not None
        and type(value["chars"]) is int
        and 1 <= value["chars"] <= 512
        and type(value["bytes"]) is int
        and value["chars"] <= value["bytes"] <= value["chars"] * 4
    )


# LLM: 准确内部schema才可展示；未知类型/字段/enum不兜底，不从任意body推导可信诊断。
# 函数用途: 验证原native诊断身份关联和有限错误分类。
def _valid_native_row(row: object) -> bool:
    if (
        type(row) is not dict
        or set(row) != _NATIVE_FIELDS
        or row["schema_version"] != "gateway_native_diagnostic.v1"
    ):
        return False
    if type(row["stage"]) is not str or row["stage"] not in NATIVE_STAGES:
        return False
    if row["op"] is not None and (type(row["op"]) is not str or row["op"] not in NATIVE_OPS):
        return False
    if row["seq"] is not None and (type(row["seq"]) is not int or not 0 <= row["seq"] < 2**63):
        return False
    if not _valid_fingerprint(row["request"]) or not _valid_fingerprint(row["attempt"]):
        return False
    if row["stage"] not in NATIVE_LINKED_STAGES and any(
        row[k] is not None for k in ("op", "seq", "request", "attempt")
    ):
        return False
    return _valid_native_error(row)


# LLM: 错误类名同样不是安全凭证；仅准许固定类/码与原literal集合，未知文本整条丢弃。
# 函数用途: 核对错误分类，避免同字段名带出动态秘密。
def _valid_native_error(row: dict) -> bool:
    error_type, code, reason, field = (
        row[k] for k in ("error_type", "error_code", "reason", "field")
    )
    if type(error_type) is not str:
        return False
    if error_type in _NATIVE_STANDARD_ERRORS:
        return code is None and reason is None and field is None
    if error_type == "KernelMessageError":
        return (
            type(code) is str
            and code in NATIVE_KERNEL_CODES
            and reason is None
            and field is None
            and not (row["stage"] == "receive" and code == "KERNEL_MESSAGE_TIMEOUT")
        )
    if error_type == "WorkerCommitRejected":
        return (
            code == "WORKER_COMMIT_REJECTED"
            and field is None
            and (reason is None or type(reason) is str and reason in NATIVE_REJECTION_REASONS)
        )
    if error_type == "WorkerSharedResourceError":
        return code == "WORKER_SHARED_RESOURCE_INVALID" and (
            reason is None and field is None or native_resource_pair_is_public(reason, field)
        )
    return False


# LLM: 原64KiB尾读之外不再读文件；每行额外4KiB上限、严格JSON拒重复键/非有限数，只返六项固定安全字段。
# 函数用途: 将一条原native日志变成无ID、路径、正文或任意异常的观察记录。
def native_diagnostic_record(line: str) -> dict | None:
    if not line.startswith(_NATIVE_PREFIX):
        return None
    try:
        if len(line.encode("utf-8")) > 4096:
            return None
        row = load_strict_json(line[len(_NATIVE_PREFIX) :])
        if not _valid_native_row(row):
            return None
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None
    return {key: row[key] for key in ("stage", "op", "error_type", "error_code", "reason", "field")}
