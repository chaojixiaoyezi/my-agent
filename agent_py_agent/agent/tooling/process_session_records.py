# LLM: v1 不推导执行身份，v2 显式保留原任务协议，v3 区分任务与激活；同一句柄不换版本或归属，联测 Store/redo/host。
# 模块用途: 校验后台会话的版本、启动阶段和单向转换，防止共享插件进程被当作业务任务资源。
from __future__ import annotations

import math
import re
from pathlib import Path

from .process_scope import ProcessActivationScope

PROCESS_SESSION_SCHEMA = "managed_process_session.v3"
PREVIOUS_PROCESS_SESSION_SCHEMA = "managed_process_session.v2"
MANAGED_PROCESS_SESSION_SCHEMAS = frozenset({PROCESS_SESSION_SCHEMA, PREVIOUS_PROCESS_SESSION_SCHEMA})
LEGACY_PROCESS_SESSION_SCHEMA = "managed_process_session.v1"
PROCESS_TERMINAL_STATUSES = frozenset({"exited", "killed", "not_started"})
_SESSION_ID_RE = re.compile(r"^bg-[A-Za-z0-9][A-Za-z0-9-]{0,95}$")
_EXECUTION_KEYS = frozenset({"owner_home", "thread_id", "root_task_id", "run_id", "attempt_id"})


# LLM: 文件定位只接受已有 bg ID 语法；不得从路径、命令或通知正文生成资源身份。
# 函数用途: 校验一个已提供的进程句柄，拒绝路径穿越或非字符串身份。
def validate_session_id(value: object) -> str:
    if not isinstance(value, str) or not _SESSION_ID_RE.fullmatch(value):
        raise ValueError("invalid managed process session id")
    return value


# LLM: 旧协议保持原版本，v3 必须显式声明激活归属；不从旧字段推断或补默认值，空 PID 只属于明确启动阶段。
# 函数用途: 分版本验证完整记录并复制嵌套身份，坏记录不得成为可发信号的对象。
def validate_process_record(record: object) -> dict[str, object]:
    if not isinstance(record, dict):
        raise TypeError("managed process session record must be an object")
    schema = record.get("schema")
    if schema == LEGACY_PROCESS_SESSION_SCHEMA:
        return _validate_legacy_record(record)
    if not isinstance(schema, str) or schema not in MANAGED_PROCESS_SESSION_SCHEMAS:
        raise ValueError("unsupported managed process session schema")
    payload = dict(record)
    validate_session_id(payload.get("session_id"))
    _validate_v2_scope(payload)
    _validate_activation_scope(payload)
    _validate_v2_instances(payload)
    _validate_v2_lifecycle(payload)
    return payload


# LLM: v3 共享连接不能携带任务或通知身份，v2 不能通过额外字段声明激活；归属校验不代替原安装表的执行准入。
# 函数用途: 明确区分普通任务和共享插件资源，拒绝混绑 owner 或借用首个业务调用的身份。
def _validate_activation_scope(payload: dict[str, object]) -> None:
    if payload["schema"] == PREVIOUS_PROCESS_SESSION_SCHEMA:
        if "activation_scope" in payload:
            raise ValueError("v2 managed process cannot declare activation scope")
        return
    if "activation_scope" not in payload:
        raise ValueError("managed process activation scope required")
    value = payload["activation_scope"]
    if value is None:
        return
    if not isinstance(value, dict) or set(value) != {"owner_id", "owner_home", "plugin_id", "activation_id"}:
        raise ValueError("invalid managed process activation scope fields")
    scope = ProcessActivationScope(**value)
    access, execution = payload["access_scope"], payload["execution_scope"]
    if (scope.owner_id != access["owner_id"] or scope.owner_home != access["owner_home"]
            or access["conversation_id"] or any(execution[key] for key in _EXECUTION_KEYS - {"owner_home"})
            or payload["completion_target"] or payload.get("completion_notice_id")):
        raise ValueError("shared activation cannot bind business task identities")
    payload["activation_scope"] = dict(value)


# LLM: 新持久身份要求字符串精确字段；访问回退不补执行身份，通知地址永不授权资源停止。
# 函数用途: 核对 v2 用户、会话与执行归属的一致性，并复制嵌套身份防止调用方事后修改。
def _validate_v2_scope(payload: dict[str, object]) -> None:
    access = payload.get("access_scope")
    execution = payload.get("execution_scope")
    if not isinstance(access, dict) or set(access) != {"owner_id", "conversation_id", "owner_home"}:
        raise ValueError("invalid managed process access scope")
    if not isinstance(execution, dict) or set(execution) != _EXECUTION_KEYS:
        raise ValueError("invalid managed process execution scope")
    if not all(isinstance(value, str) for value in (*access.values(), *execution.values())):
        raise ValueError("managed process identities must be strings")
    if execution["owner_home"] != access["owner_home"]:
        raise ValueError("managed process owner conflict")
    if execution["thread_id"] and execution["thread_id"] != access["conversation_id"]:
        raise ValueError("managed process thread conflict")
    payload["access_scope"], payload["execution_scope"] = dict(access), dict(execution)
    target = payload.get("completion_target") or {}
    _validate_completion_target(target, access["conversation_id"])
    payload["completion_target"] = dict(target)


# LLM: bool、字符串和非有限数不能转成实例身份；未绑定实例不能留出生标识，launcher 必须已绑定。
# 函数用途: 验证启动者、托管进程和业务子进程的 PID 与出生标识，不读取操作系统或发送信号。
def _validate_v2_instances(payload: dict[str, object]) -> None:
    for pid_key, birth_key in (
        ("launcher_pid", "launcher_birth_token"),
        ("pid", "pid_birth_token"),
        ("child_pid", "child_pid_birth_token"),
    ):
        pid, birth = payload.get(pid_key), payload.get(birth_key)
        if type(pid) is not int or pid < 0 or not isinstance(birth, str):
            raise ValueError("invalid managed process instance")
        if bool(pid) != bool(birth):
            raise ValueError("managed process instance is partially bound")
    if payload["launcher_pid"] <= 0:
        raise ValueError("managed process launcher identity is required")
    if payload["child_pid"] and not payload["pid"]:
        raise ValueError("managed process child requires a bound host")
    revision = payload.get("revision")
    if type(revision) is not int or revision < 0:
        raise ValueError("invalid managed process revision")


# LLM: stop_requested 只是意图；完整 session 清理与 child 终止回执分开，新增清理字段必须严格有效，未确认不能伪装成功。
# 函数用途: 核对阶段、时间、交接及可选清理证据，保持原命令状态不被资源回收改写。
def _validate_v2_lifecycle(payload: dict[str, object]) -> None:
    for key in ("stop_requested", "handoff_confirmed", "child_launch_started"):
        if type(payload.get(key)) is not bool:
            raise ValueError("invalid managed process lifecycle flag")
    for key in ("reserved_at", "started_at"):
        value = payload.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError("invalid managed process timestamp")
    if not payload["reserved_at"]:
        raise ValueError("managed process reservation time is required")
    status = payload.get("status")
    if status not in {"starting", "running", "unknown", *PROCESS_TERMINAL_STATUSES}:
        raise ValueError("invalid managed process status")
    if payload["child_pid"] and not payload["child_launch_started"]:
        raise ValueError("managed process child was never admitted")
    if (status == "running" or payload["handoff_confirmed"]) and not payload["child_pid"]:
        raise ValueError("managed process running or handoff requires child identity")
    if status == "running" and not payload["started_at"]:
        raise ValueError("managed process start time is required")
    if status == "not_started" and payload["child_launch_started"]:
        raise ValueError("admitted child cannot be declared not started")
    if payload["child_launch_started"] and not payload["pid"]:
        raise ValueError("managed process launch requires a bound host")
    if payload["child_pid"] and not payload["started_at"]:
        raise ValueError("bound managed process child requires start time")
    if status in {"exited", "killed"} and not payload["child_pid"]:
        raise ValueError("managed process termination requires child identity")
    finished = payload.get("finished_at")
    if status in PROCESS_TERMINAL_STATUSES:
        if type(finished) not in (int, float) or not math.isfinite(finished) or finished <= 0:
            raise ValueError("managed process termination time is required")
    elif finished is not None:
        raise ValueError("unfinished managed process cannot have termination time")
    exit_code = payload.get("exit_code")
    if exit_code is not None and (type(exit_code) is not int or status not in {"exited", "killed"}):
        raise ValueError("invalid managed process exit code")
    for key in ("command", "cwd", "output_file", "host_state_file", "completion_notice_id"):
        if not isinstance(payload.get(key), str):
            raise ValueError("invalid managed process text field")
    if status not in PROCESS_TERMINAL_STATUSES and payload["completion_notice_id"]:
        raise ValueError("unfinished managed process cannot have completion notice")
    _validate_cleanup_evidence(payload.get("termination"))


# LLM: cleanup 只附加在原 termination 中，身份沿不可变 session 字段；缺失表示未记录，不能从 child-only confirmed 补齐。
# 函数用途: 拒绝畸形或自相矛盾的完整资源清理回执，不修改既有 host 的终止协议。
def _validate_cleanup_evidence(termination) -> None:
    if not isinstance(termination, dict) or "cleanup" not in termination:
        return
    cleanup = termination["cleanup"]
    if (not isinstance(cleanup, dict) or set(cleanup) != {"confirmed", "instances"}
            or type(cleanup["confirmed"]) is not bool or not isinstance(cleanup["instances"], list)):
        raise ValueError("invalid managed session cleanup evidence")
    keys = {"method", "confirmed", "return_code", "observed_processes", "unresolved_pids"}
    for receipt in cleanup["instances"]:
        if (not isinstance(receipt, dict) or set(receipt) != keys or type(receipt["confirmed"]) is not bool
                or not isinstance(receipt["method"], str) or not receipt["method"]
                or type(receipt["observed_processes"]) is not int or receipt["observed_processes"] < 0
                or receipt["return_code"] is not None and type(receipt["return_code"]) is not int
                or not isinstance(receipt["unresolved_pids"], (tuple, list))
                or any(type(pid) is not int or pid <= 0 for pid in receipt["unresolved_pids"])
                or receipt["confirmed"] and receipt["unresolved_pids"]
                or cleanup["confirmed"] and not receipt["confirmed"]):
            raise ValueError("invalid managed session cleanup receipt")


# LLM: 保留 v1 的显式读取合同；它的附加字段不会让 v1 获得 v2 的任务停止授权。
# 函数用途: 读取旧安装创建的已绑定进程，沿原 session 访问边界管理，不猜测迁移执行归属。
def _validate_legacy_record(record: dict[str, object]) -> dict[str, object]:
    payload = dict(record)
    session_id = validate_session_id(str(payload.get("session_id") or "").strip())
    try:
        pid, started_at = int(payload.get("pid") or 0), float(payload.get("started_at") or 0.0)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid managed process numeric fields") from exc
    if pid <= 0 or started_at <= 0:
        raise ValueError("managed process pid and started_at are required")
    birth = str(payload.get("pid_birth_token") or "").strip()
    if not birth:
        raise ValueError("managed process pid_birth_token is required")
    status = str(payload.get("status") or "")
    if status not in {"running", "exited", "killed"}:
        raise ValueError("invalid managed process status")
    scope = payload.get("access_scope")
    if not isinstance(scope, dict):
        raise ValueError("managed process access_scope is required")
    owner, conversation = (
        str(scope.get("owner_id") or "").strip(),
        str(scope.get("conversation_id") or "").strip(),
    )
    if not owner or not conversation:
        raise ValueError("managed process access_scope is not bound")
    _validate_completion_target(payload.get("completion_target") or {}, conversation)
    payload.update(
        session_id=session_id,
        pid=pid,
        pid_birth_token=birth,
        started_at=started_at,
        status=status,
        access_scope={
            "owner_id": owner,
            "conversation_id": conversation,
            "owner_home": str(scope.get("owner_home") or ""),
        },
    )
    return payload


# LLM: 完成目标只是已声明的投递地址；其线程必须等于访问会话，不参与执行归属选择。
# 函数用途: 拒绝跨会话或不完整的完成通知地址，保留关闭通知时的空对象。
def _validate_completion_target(target: object, conversation_id: str) -> None:
    if not isinstance(target, dict) or (
        target
        and (
            set(target) != {"store_root", "thread_id", "task_id", "run_id"}
            or not all(isinstance(value, str) and value for value in target.values())
            or target["thread_id"] != conversation_id
            or not Path(target["store_root"]).is_absolute()
        )
    ):
        raise ValueError("invalid managed process completion target scope")


# LLM: 同一 session 不能换身份或复活；完整 cleanup 确认单调保留，迟到 host 更新不能擦除，原命令终态仍冻结。
# 函数用途: 合并版本化记录的停止、交接、清理和通知事实，再由原 Store 递增版本号。
def merge_process_record(
    existing: dict[str, object], incoming: dict[str, object]
) -> dict[str, object]:
    keys = ("schema", "session_id", "access_scope", "completion_target")
    if any(existing.get(key, {}) != incoming.get(key, {}) for key in keys):
        raise ValueError("managed process immutable authority conflict")
    managed = existing["schema"] in MANAGED_PROCESS_SESSION_SCHEMAS
    if managed:
        _assert_v2_authority(existing, incoming)
    elif any(existing.get(key) != incoming.get(key) for key in ("pid", "pid_birth_token")):
        raise ValueError("managed process immutable instance conflict")
    merged = dict(incoming)
    if existing.get("completion_notice_id"):
        merged["completion_notice_id"] = existing["completion_notice_id"]
    if managed:
        for key in ("stop_requested", "handoff_confirmed", "child_launch_started"):
            merged[key] = existing[key] or incoming[key]
    old, new = existing["status"], incoming["status"]
    terminal = old in PROCESS_TERMINAL_STATUSES and not (old == "exited" and new == "killed")
    if managed and ("termination" in existing or "termination" in incoming):
        previous = existing.get("termination") or {}
        updated = incoming.get("termination") or {}
        cleanup = previous.get("cleanup")
        if not isinstance(cleanup, dict) or cleanup.get("confirmed") is not True:
            cleanup = updated.get("cleanup", cleanup)
        merged["termination"] = dict(previous if terminal else updated)
        if cleanup is not None:
            merged["termination"]["cleanup"] = cleanup
    if terminal:
        controls = {
            key: merged[key]
            for key in ("stop_requested", "handoff_confirmed", "child_launch_started", "termination")
            if managed and key in merged
        }
        merged = {
            **existing,
            **controls,
            "completion_notice_id": merged.get("completion_notice_id", ""),
        }
    return validate_process_record(merged)


# LLM: launcher、任务/激活归属不可更换；child 进入创建后即使 PID 未知，也不能伪称没有启动副作用。
# 函数用途: 核对 v2/v3 不可变字段及唯一实例绑定，拒绝把同一句柄换成另一条命令或插件代次。
def _assert_v2_authority(existing: dict[str, object], incoming: dict[str, object]) -> None:
    keys = (
        "execution_scope",
        "activation_scope",
        "launcher_pid",
        "launcher_birth_token",
        "reserved_at",
        "command",
        "cwd",
        "output_file",
        "host_state_file",
    )
    if any(existing.get(key) != incoming.get(key) for key in keys):
        raise ValueError("managed process execution authority conflict")
    for pid_key, birth_key in (("pid", "pid_birth_token"), ("child_pid", "child_pid_birth_token")):
        if existing[pid_key] and any(
            existing[key] != incoming[key] for key in (pid_key, birth_key)
        ):
            raise ValueError("managed process instance cannot be rebound")
        if (
            not existing[pid_key]
            and incoming[pid_key]
            and (existing["status"] in PROCESS_TERMINAL_STATUSES or existing["stop_requested"])
        ):
            raise ValueError("stopped managed process cannot bind a new instance")
    if (
        existing["stop_requested"]
        and not existing["handoff_confirmed"]
        and incoming["handoff_confirmed"]
    ):
        raise ValueError("stopped managed process cannot be handed off")
    if (
        existing["stop_requested"]
        and not existing["child_launch_started"]
        and incoming["child_launch_started"]
    ):
        raise ValueError("stopped managed process cannot begin child launch")
    if existing["status"] in {"running", "unknown"} and incoming["status"] == "starting":
        raise ValueError("managed process cannot return to reservation")
    if existing["started_at"] and incoming["started_at"] != existing["started_at"]:
        raise ValueError("managed process start time cannot change")
