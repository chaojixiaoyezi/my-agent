# LLM: 控制副作用之前先冻结 owner、输入和版本化摘要；显式目录进入 v3，旧回执不能承载未签名字段。
# 模块用途: 保存唯一控制回执并处理安全重试，目录、目标或命令变化不能借用原消息编号执行。
from __future__ import annotations

"""LLM: Persist one authenticated slash-control operation before any side effect.

模块用途: 为 `/control` 与 `/ask` 中的系统命令保存唯一操作回执；网络重试只重放同一
结果，进程在副作用边界崩溃时保留明确 unknown，而不是再次执行 `/stop`、`/compact`
或 `/goal`。
"""

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..conversation.control_commands import (
    ConversationControlCommand,
    parse_conversation_control,
)
from ..runtime_errors import DataCorruptionError, runtime_error_report
from ..user_space.owner_resolver import OwnerIdentity
from .control_service import (
    GatewayControlScope,
    bind_gateway_control_scope_owner,
    execute_gateway_conversation_control,
    reconcile_gateway_steer_delivery,
)
from .io import (
    gateway_turn_transition,
    read_json_file_report,
    try_gateway_turn_transition,
    write_json_file_atomic,
)
from .paths import GatewayPaths

_CONTROL_OPERATION_SCHEMA = "gateway_control_operation.v2"
_CONTROL_INPUT_DIGEST_LEGACY_VERSION = 1
_CONTROL_INPUT_DIGEST_TARGET_VERSION = 2
_CONTROL_INPUT_DIGEST_CURRENT_VERSION = 3
_CONTROL_INPUT_DIGEST_VERSIONS = {
    _CONTROL_INPUT_DIGEST_LEGACY_VERSION,
    _CONTROL_INPUT_DIGEST_TARGET_VERSION,
    _CONTROL_INPUT_DIGEST_CURRENT_VERSION,
}
_CONTROL_OPERATION_STATES = {
    "prepared",
    "executing",
    "completed",
    "terminal_unknown",
}
_CONTROL_OPERATION_ID = re.compile(r"^gwctl-msg-[0-9a-f]{32}$")
_CONTROL_RESULT_PROJECTION_FIELDS = {
    "kind",
    "ok",
    "message",
    "request_id",
    "delivery_status",
    "guidance_dedupe_key",
    "error_code",
    "task_status",
}


# LLM: Missing stable client identity is a protocol error, not permission to invent a retry key.
# 类用途: 表示外部控制命令没有提供可跨网络重试复用的消息 ID。
class GatewayControlOperationIdentityRequired(ValueError):
    pass


# LLM: A stable client message id has exactly one canonical command digest for its lifetime.
# 类用途: 表示同一消息 ID 被另一条命令、另一回合或另一份权限事实重复使用。
class GatewayControlOperationConflict(ValueError):
    pass


# LLM: This file is the sole transport-level fact for one control operation. It freezes an exact
# turn or manual-Compact target when present; command-specific stores remain effect authority and
# ``terminal_unknown`` never claims that an effect failed.
# 显式 workspace 与命令一起冻结并签入 v3 摘要，不能由重试或当前 HTTP 请求替换。
# 类用途: 保存控制命令的固定身份、精确目标、执行阶段、结果和无法确认的错误事实。
@dataclass(frozen=True)
class GatewayControlOperationReceipt:
    operation_id: str
    input_digest: str
    command_text: str
    command_kind: str
    user_id: str
    channel: str
    conversation_id: str
    channel_chat_type: str
    channel_chat_id: str
    owner_provider: str
    owner_kind: str
    owner_id: str
    owner_ref_digest: str
    client_message_id: str
    expected_turn_id: str
    target_control_message_id: str
    all_user_access: bool
    state: str
    input_digest_version: int = _CONTROL_INPUT_DIGEST_CURRENT_VERSION
    result: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] = field(default_factory=dict)
    updated_at: float = 0.0
    workspace: object = None

    # LLM: 保存固定身份、可选目录与摘要版本；准备后执行和对账只能读回执，不重读当前 HTTP 输入。
    # 函数用途: 把控制操作回执转换为可原子写入的 JSON 字典。
    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": _CONTROL_OPERATION_SCHEMA,
            "operation_id": self.operation_id,
            "input_digest": self.input_digest,
            "command_text": self.command_text,
            "command_kind": self.command_kind,
            "user_id": self.user_id,
            "channel": self.channel,
            "conversation_id": self.conversation_id,
            "channel_chat_type": self.channel_chat_type,
            "channel_chat_id": self.channel_chat_id,
            "owner_provider": self.owner_provider,
            "owner_kind": self.owner_kind,
            "owner_id": self.owner_id,
            "owner_ref_digest": self.owner_ref_digest,
            "client_message_id": self.client_message_id,
            "expected_turn_id": self.expected_turn_id,
            "target_control_message_id": self.target_control_message_id,
            "all_user_access": self.all_user_access,
            "state": self.state,
            "input_digest_version": self.input_digest_version,
            "updated_at": self.updated_at,
        }
        if self.result:
            payload["result"] = dict(self.result)
        if self.error:
            payload["error"] = dict(self.error)
        if self.workspace is not None:
            payload["workspace"] = self.workspace
        return payload

    # LLM: 读取时重算身份与对应版本的摘要；目录篡改或旧版未签名目录都必须当作账本损坏拒绝。
    # 函数用途: 从磁盘字典恢复并完整校验一条控制操作回执。
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GatewayControlOperationReceipt:
        if str(data.get("schema_version") or "").strip() != _CONTROL_OPERATION_SCHEMA:
            raise DataCorruptionError("gateway control operation schema is invalid")
        state = str(data.get("state") or "").strip()
        if state not in _CONTROL_OPERATION_STATES:
            raise DataCorruptionError("gateway control operation state is invalid")
        if not isinstance(data.get("all_user_access", False), bool):
            raise DataCorruptionError("gateway control operation access fact is invalid")
        if "result" in data and not isinstance(data.get("result"), dict):
            raise DataCorruptionError("gateway control operation result is invalid")
        if "error" in data and not isinstance(data.get("error"), dict):
            raise DataCorruptionError("gateway control operation error is invalid")
        raw_digest_version = data.get("input_digest_version")
        if raw_digest_version is None:
            digest_version = 0
        elif isinstance(raw_digest_version, bool) or not isinstance(
            raw_digest_version, int
        ):
            raise DataCorruptionError("gateway control operation digest version is invalid")
        elif raw_digest_version not in _CONTROL_INPUT_DIGEST_VERSIONS:
            raise DataCorruptionError("gateway control operation digest version is unsupported")
        else:
            digest_version = raw_digest_version
        receipt = cls(
            operation_id=str(data.get("operation_id") or "").strip(),
            input_digest=str(data.get("input_digest") or "").strip(),
            command_text=str(data.get("command_text") or "").strip(),
            command_kind=str(data.get("command_kind") or "").strip(),
            user_id=str(data.get("user_id") or "").strip(),
            channel=str(data.get("channel") or "").strip(),
            conversation_id=str(data.get("conversation_id") or "").strip(),
            channel_chat_type=str(data.get("channel_chat_type") or "").strip().lower(),
            channel_chat_id=str(data.get("channel_chat_id") or "").strip(),
            owner_provider=str(data.get("owner_provider") or "").strip(),
            owner_kind=str(data.get("owner_kind") or "").strip(),
            owner_id=str(data.get("owner_id") or "").strip(),
            owner_ref_digest=str(data.get("owner_ref_digest") or "").strip(),
            client_message_id=str(data.get("client_message_id") or "").strip(),
            expected_turn_id=str(data.get("expected_turn_id") or "").strip(),
            target_control_message_id=str(
                data.get("target_control_message_id") or ""
            ).strip(),
            all_user_access=bool(data.get("all_user_access", False)),
            state=state,
            input_digest_version=digest_version,
            result=dict(data.get("result")) if isinstance(data.get("result"), dict) else {},
            error=dict(data.get("error")) if isinstance(data.get("error"), dict) else {},
            updated_at=_control_operation_timestamp(data.get("updated_at")),
            workspace=data.get("workspace"),
        )
        matched_digest_version = _validate_control_operation_receipt(receipt)
        return (
            replace(receipt, input_digest_version=matched_digest_version)
            if receipt.input_digest_version == 0
            else receipt
        )


# LLM: Receipt filenames expose neither provider message ids nor conversation names.
# 函数用途: 返回一条控制操作回执的唯一文件位置。
def gateway_control_operation_path(paths: GatewayPaths, operation_id: str) -> Path:
    normalized = str(operation_id or "").strip()
    if not _CONTROL_OPERATION_ID.fullmatch(normalized):
        raise ValueError("gateway control operation id is invalid")
    return paths.root / "control_operations" / f"{normalized}.json"


# LLM: The operation lock is ordered outside exact-turn and conversation-mailbox locks. The
# namespace prefix prevents an operation id from aliasing a real Gateway turn id.
# 函数用途: 取得单条控制操作跨进程共用的状态转换锁。
def gateway_control_operation_transition(paths: GatewayPaths, operation_id: str):
    normalized = str(operation_id or "").strip()
    if not _CONTROL_OPERATION_ID.fullmatch(normalized):
        raise ValueError("gateway control operation id is invalid")
    return gateway_turn_transition(paths, f"control-operation:{normalized}")


# LLM: Duplicate POST and GET status probes use the same C lock in non-blocking mode. Failure to
# acquire permits only atomic receipt replay; it never permits another effect execution.
# 函数用途: 非阻塞尝试领取一条控制操作锁，供活跃长命令立即返回 executing 状态。
def try_gateway_control_operation_transition(paths: GatewayPaths, operation_id: str):
    normalized = str(operation_id or "").strip()
    if not _CONTROL_OPERATION_ID.fullmatch(normalized):
        raise ValueError("gateway control operation id is invalid")
    return try_gateway_turn_transition(paths, f"control-operation:{normalized}")


# LLM: Identity is based only on authenticated scope plus the opaque provider/client message id;
# command content belongs to the separate digest so same-id/different-input conflicts deterministically.
# 目录沿对应摘要版本签名，不能因相同命令正文而忽略重试时的路径变化。
# 函数用途: 为一条可重试控制命令生成稳定操作 ID 和服务端内容指纹。
def gateway_control_operation_identity(
    scope: GatewayControlScope,
    command_text: str,
) -> tuple[str, str]:
    message_id = str(scope.metadata.get("message_id") or "").strip()
    if not message_id:
        raise GatewayControlOperationIdentityRequired(
            "control command requires metadata.message_id"
        )
    identity = _control_operation_identity_payload(scope, message_id)
    identity_json = _canonical_json(identity)
    operation_id = "gwctl-msg-" + hashlib.sha256(identity_json.encode("utf-8")).hexdigest()[:32]
    input_digest = _control_operation_input_digest(
        scope,
        command_text,
        version=_control_input_version(scope),
    )
    return operation_id, input_digest


# LLM: The prepare row is committed before the executing marker and before any command-specific
# mutation. Existing completed/unknown rows are replayed without touching the effect service.
# 函数用途: 幂等执行一条控制命令，并把崩溃边界保存为 completed 或 terminal_unknown。
def execute_gateway_control_operation(
    agent: object,
    paths: GatewayPaths,
    command: ConversationControlCommand,
    scope: GatewayControlScope,
    *,
    command_text: str,
) -> GatewayControlOperationReceipt:
    scope = bind_gateway_control_scope_owner(agent, scope)
    operation_id, input_digest = gateway_control_operation_identity(scope, command_text)
    with try_gateway_control_operation_transition(paths, operation_id) as acquired:
        if acquired:
            return _execute_gateway_control_operation_locked(
                agent,
                paths,
                operation_id=operation_id,
                input_digest=input_digest,
                command=command,
                scope=scope,
                command_text=command_text,
            )
        observed = read_gateway_control_operation(paths, operation_id)
        if observed is not None:
            if observed.input_digest != input_digest:
                raise GatewayControlOperationConflict(
                    "gateway control message id was reused with different input"
                )
            return observed
    # 只有首个进程刚取得 C、尚未写 prepared 时会看不到回执；该极短窗口才允许等待并复用同一状态机。
    with gateway_control_operation_transition(paths, operation_id):
        return _execute_gateway_control_operation_locked(
            agent,
            paths,
            operation_id=operation_id,
            input_digest=input_digest,
            command=command,
            scope=scope,
            command_text=command_text,
        )


# LLM: This helper is entered only by the C-lock winner. It writes executing before the generic
# effect and is the only path allowed to invoke the command service.
# 函数用途: 在已持控制操作锁时推进 prepared、执行一次副作用并保存 completed/unknown。
def _execute_gateway_control_operation_locked(
    agent: object,
    paths: GatewayPaths,
    *,
    operation_id: str,
    input_digest: str,
    command: ConversationControlCommand,
    scope: GatewayControlScope,
    command_text: str,
) -> GatewayControlOperationReceipt:
    receipt = _load_or_prepare_control_operation_locked(
        paths,
        operation_id=operation_id,
        input_digest=input_digest,
        command=command,
        scope=scope,
        command_text=command_text,
    )
    if receipt.state in {"completed", "terminal_unknown"}:
        return _reconcile_control_operation_locked(agent, paths, receipt)
    if receipt.state == "executing":
        uncertain = replace(
            receipt,
            state="terminal_unknown",
            error={
                "error_code": "CONTROL_OPERATION_INTERRUPTED",
                "message": "the prior control attempt crossed an unknown side-effect boundary",
            },
            updated_at=time.time(),
        )
        write_json_file_atomic(
            gateway_control_operation_path(paths, operation_id),
            uncertain.to_dict(),
        )
        return _reconcile_control_operation_locked(agent, paths, uncertain)
    executing = replace(receipt, state="executing", updated_at=time.time())
    write_json_file_atomic(
        gateway_control_operation_path(paths, operation_id),
        executing.to_dict(),
    )
    try:
        result = execute_gateway_conversation_control(
            agent,
            paths,
            command,
            _receipt_scope(receipt),
        )
    except Exception as exc:  # noqa: BLE001 - any partial command effect must remain unknown.
        uncertain = replace(
            executing,
            state="terminal_unknown",
            error=runtime_error_report(exc, context="gateway.control_operation.execute"),
            updated_at=time.time(),
        )
        write_json_file_atomic(
            gateway_control_operation_path(paths, operation_id),
            uncertain.to_dict(),
        )
        return _reconcile_control_operation_locked(agent, paths, uncertain)
    completed = replace(
        executing,
        state="completed",
        result=result.to_dict(),
        error={},
        updated_at=time.time(),
    )
    write_json_file_atomic(
        gateway_control_operation_path(paths, operation_id),
        completed.to_dict(),
    )
    return completed


# LLM: GET/status reconciliation is read-only with respect to control effects. Only the existing
# `/btw` guidance receipt may advance a completed/unknown delivery projection.
# 函数用途: 查询并对账一条控制操作；不会重新执行停止、压缩或持续任务等副作用。
def reconcile_gateway_control_operation(
    agent: object,
    paths: GatewayPaths,
    operation_id: str,
) -> GatewayControlOperationReceipt | None:
    observed = read_gateway_control_operation(paths, operation_id)
    if observed is None:
        return None
    with try_gateway_control_operation_transition(paths, operation_id) as acquired:
        if not acquired:
            return observed
        receipt = read_gateway_control_operation(paths, operation_id)
        if receipt is None:
            return None
        if receipt.state == "executing":
            receipt = replace(
                receipt,
                state="terminal_unknown",
                error={
                    "error_code": "CONTROL_OPERATION_INTERRUPTED",
                    "message": "the prior control attempt crossed an unknown side-effect boundary",
                },
                updated_at=time.time(),
            )
            write_json_file_atomic(
                gateway_control_operation_path(paths, operation_id),
                receipt.to_dict(),
            )
        return _reconcile_control_operation_locked(agent, paths, receipt)


# LLM: Reads distinguish an absent row from an empty/corrupt row and validate the filename anchor.
# 函数用途: 读取并校验一条控制操作回执，不存在时返回 None。
def read_gateway_control_operation(
    paths: GatewayPaths,
    operation_id: str,
) -> GatewayControlOperationReceipt | None:
    path = gateway_control_operation_path(paths, operation_id)
    report = read_json_file_report(path, context="gateway.control_operation.read")
    if report.load_error is not None:
        raise DataCorruptionError("gateway control operation receipt is unreadable")
    if not report.payload:
        if path.exists():
            raise DataCorruptionError("gateway control operation receipt is empty or invalid")
        return None
    receipt = GatewayControlOperationReceipt.from_dict(report.payload)
    if receipt.operation_id != str(operation_id or "").strip() or path.stem != receipt.operation_id:
        raise DataCorruptionError("gateway control operation id does not match its path")
    return receipt


# LLM: HTTP/TUI/adapter consume one typed projection. Target turn id remains ``request_id`` for
# compatibility, while ``operation_id`` is the independent receipt key used for polling.
# 函数用途: 把控制操作回执转换成客户端可展示和对账的状态字典。
def gateway_control_operation_status_payload(
    receipt: GatewayControlOperationReceipt,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "operation_id": receipt.operation_id,
        "receipt_id": receipt.operation_id,
        "control_state": receipt.state,
        "status": "control",
        "disposition": "system_command",
    }
    if receipt.result:
        payload.update(
            {
                key: receipt.result[key]
                for key in _CONTROL_RESULT_PROJECTION_FIELDS
                if key in receipt.result
            }
        )
    elif receipt.state in {"prepared", "executing"}:
        payload.update(
            {
                "kind": receipt.command_kind,
                "ok": True,
                "message": "控制操作正在处理；客户端可使用同一 operation_id 查询进度。",
                "request_id": receipt.expected_turn_id,
                "delivery_status": "unknown",
            }
        )
    else:
        payload.update(
            {
                "kind": receipt.command_kind,
                "ok": False,
                "message": "控制操作的最终结果无法确认；系统不会自动重复执行。",
                "request_id": receipt.expected_turn_id,
                "delivery_status": "unknown",
                "error_code": "CONTROL_OPERATION_DELIVERY_UNKNOWN",
            }
        )
    payload["operation_id"] = receipt.operation_id
    payload["receipt_id"] = receipt.operation_id
    payload["control_state"] = receipt.state
    payload["kind"] = receipt.command_kind
    payload["status"] = "control"
    payload["disposition"] = "system_command"
    return payload


# LLM: This helper runs only while the operation lock is held; it never creates a guidance row.
# 函数用途: 用已有 `/btw` 回执刷新投递结果，其他命令保持原终态不动。
def _reconcile_control_operation_locked(
    agent: object,
    paths: GatewayPaths,
    receipt: GatewayControlOperationReceipt,
) -> GatewayControlOperationReceipt:
    if receipt.command_kind != "steer":
        return receipt
    delivery_status = str(receipt.result.get("delivery_status") or "").strip()
    if receipt.state == "completed" and delivery_status in {"accepted", "rejected"}:
        return receipt
    command = parse_conversation_control(receipt.command_text, reject_unknown_slash=True)
    if command is None or command.kind != "steer" or not command.valid:
        return receipt
    result = reconcile_gateway_steer_delivery(
        agent,
        paths,
        command,
        _receipt_scope(receipt),
    )
    if result is None:
        return receipt
    updated = replace(
        receipt,
        state="completed",
        result=result.to_dict(),
        error={},
        updated_at=time.time(),
    )
    write_json_file_atomic(
        gateway_control_operation_path(paths, receipt.operation_id),
        updated.to_dict(),
    )
    return updated


# LLM: Prepare validates a same-id retry before any effect. This function is called only under
# the operation transition and writes exactly one canonical receipt file.
# 目录声明与命令共同冻结，显式目录使用 v3，其余输入保留 v2 重试身份。
# 函数用途: 读取或首次写入 prepared 控制回执，并拒绝同 ID 不同命令。
def _load_or_prepare_control_operation_locked(
    paths: GatewayPaths,
    *,
    operation_id: str,
    input_digest: str,
    command: ConversationControlCommand,
    scope: GatewayControlScope,
    command_text: str,
) -> GatewayControlOperationReceipt:
    existing = read_gateway_control_operation(paths, operation_id)
    if existing is not None:
        if existing.input_digest != input_digest:
            raise GatewayControlOperationConflict(
                "gateway control message id was reused with different input"
            )
        return existing
    receipt = GatewayControlOperationReceipt(
        operation_id=operation_id,
        input_digest=input_digest,
        command_text=str(command_text or "").strip(),
        command_kind=str(command.kind or "").strip(),
        user_id=str(scope.user_id or "").strip(),
        channel=str(scope.channel or "").strip(),
        conversation_id=str(scope.conversation_id or "").strip(),
        channel_chat_type=str(
            scope.metadata.get("channel_chat_type") or ""
        ).strip().lower(),
        channel_chat_id=str(scope.metadata.get("channel_chat_id") or "").strip(),
        owner_provider=str(scope.resolved_owner.provider if scope.resolved_owner else ""),
        owner_kind=str(scope.resolved_owner.owner_kind if scope.resolved_owner else ""),
        owner_id=str(scope.resolved_owner.owner_id if scope.resolved_owner else ""),
        owner_ref_digest=_owner_ref_digest(scope.resolved_owner),
        client_message_id=str(scope.metadata.get("message_id") or "").strip(),
        expected_turn_id=str(scope.metadata.get("expected_turn_id") or "").strip(),
        target_control_message_id=str(
            scope.metadata.get("target_control_message_id") or ""
        ).strip(),
        all_user_access=bool(scope.all_user_access),
        workspace=scope.workspace,
        input_digest_version=_control_input_version(scope),
        state="prepared",
        updated_at=time.time(),
    )
    _validate_control_operation_receipt(receipt)
    write_json_file_atomic(gateway_control_operation_path(paths, operation_id), receipt.to_dict())
    return receipt


# LLM: 从已校验回执恢复首次身份、目录和目标，供执行及只读对账；当前 HTTP 输入不能重定向旧操作。
# 函数用途: 从控制回执恢复最小、固定的 Gateway 控制作用域。
def _receipt_scope(receipt: GatewayControlOperationReceipt) -> GatewayControlScope:
    return GatewayControlScope(
        user_id=receipt.user_id,
        channel=receipt.channel,
        conversation_id=receipt.conversation_id,
        metadata={
            "message_id": receipt.client_message_id,
            "expected_turn_id": receipt.expected_turn_id,
            "target_control_message_id": receipt.target_control_message_id,
            "channel_chat_type": receipt.channel_chat_type,
            "channel_chat_id": receipt.channel_chat_id,
        },
        all_user_access=receipt.all_user_access,
        resolved_owner=_receipt_owner_identity(receipt),
        workspace=receipt.workspace,
    )


# LLM: Every read recomputes the versioned id/digest used at prepare time and enforces state/result
# invariants. Rows written before exact Compact targets existed may use legacy digest v1 only when
# the target remains empty；v3 之前不能携带未签名的 workspace，字段扩展不能削弱旧账防篡改。
# 函数用途: 检查控制回执身份、版本化摘要、时间和终态字段，并返回匹配的摘要版本。
def _validate_control_operation_receipt(receipt: GatewayControlOperationReceipt) -> int:
    if not all(
        (
            receipt.operation_id,
            receipt.input_digest,
            receipt.command_text,
            receipt.command_kind,
            receipt.user_id,
            receipt.channel,
            receipt.conversation_id,
            receipt.owner_provider,
            receipt.owner_kind,
            receipt.owner_id,
            receipt.owner_ref_digest,
            receipt.client_message_id,
        )
    ):
        raise DataCorruptionError("gateway control operation identity is incomplete")
    scope = _receipt_scope(receipt)
    identity = _control_operation_identity_payload(scope, receipt.client_message_id)
    expected_id = "gwctl-msg-" + hashlib.sha256(
        _canonical_json(identity).encode("utf-8")
    ).hexdigest()[:32]
    candidate_versions = (
        (receipt.input_digest_version,)
        if receipt.input_digest_version in _CONTROL_INPUT_DIGEST_VERSIONS
        else tuple(sorted(_CONTROL_INPUT_DIGEST_VERSIONS, reverse=True))
    )
    matched_digest_version = 0
    for version in candidate_versions:
        if version < 3 and receipt.workspace is not None:
            continue
        if (
            version == _CONTROL_INPUT_DIGEST_LEGACY_VERSION
            and receipt.target_control_message_id
        ):
            continue
        if receipt.input_digest == _control_operation_input_digest(
            scope,
            receipt.command_text,
            version=version,
        ):
            matched_digest_version = version
            break
    if receipt.operation_id != expected_id or matched_digest_version == 0:
        raise DataCorruptionError("gateway control operation digest conflicts with its content")
    owner = _receipt_owner_identity(receipt)
    if receipt.owner_ref_digest != _owner_ref_digest(owner):
        raise DataCorruptionError("gateway control operation owner digest is invalid")
    parsed = parse_conversation_control(receipt.command_text, reject_unknown_slash=True)
    if parsed is None or parsed.kind != receipt.command_kind:
        raise DataCorruptionError("gateway control operation command kind is invalid")
    if receipt.state in {"prepared", "executing"} and (receipt.result or receipt.error):
        raise DataCorruptionError("unfinished gateway control operation has terminal fields")
    if receipt.state == "completed":
        if not receipt.result or receipt.error:
            raise DataCorruptionError("completed gateway control operation is invalid")
        _validate_control_operation_result(receipt)
    if receipt.state == "terminal_unknown" and (receipt.result or not receipt.error):
        raise DataCorruptionError("unknown gateway control operation is invalid")
    if not math.isfinite(receipt.updated_at) or receipt.updated_at < 0:
        raise DataCorruptionError("gateway control operation timestamp is invalid")
    return matched_digest_version


# LLM: Result dictionaries remain an effect-service projection, but identity and delivery fields
# must be well typed before any HTTP/TUI/adapter consumer sees them.
# 函数用途: 校验 completed 控制结果的命令类型、布尔终态、目标与投递三态。
def _validate_control_operation_result(receipt: GatewayControlOperationReceipt) -> None:
    result = receipt.result
    if str(result.get("kind") or "").strip() != receipt.command_kind:
        raise DataCorruptionError("gateway control operation result kind is invalid")
    if not isinstance(result.get("ok"), bool):
        raise DataCorruptionError("gateway control operation result ok is invalid")
    if not isinstance(result.get("message"), str):
        raise DataCorruptionError("gateway control operation result message is invalid")
    for key in ("request_id", "guidance_dedupe_key", "error_code"):
        if key in result and not isinstance(result.get(key), str):
            raise DataCorruptionError(f"gateway control operation result {key} is invalid")
    delivery_status = result.get("delivery_status", "")
    if not isinstance(delivery_status, str) or delivery_status not in {
        "",
        "accepted",
        "rejected",
        "unknown",
    }:
        raise DataCorruptionError("gateway control operation delivery status is invalid")
    if "task_status" in result and not isinstance(result.get("task_status"), dict):
        raise DataCorruptionError("gateway control operation task status is invalid")


# LLM: Stable operation identity contains only authenticated routing facts and the opaque message id.
# 函数用途: 构造不含命令正文的控制操作身份字典。
def _control_operation_identity_payload(
    scope: GatewayControlScope,
    message_id: str,
) -> dict[str, str]:
    return {
        "user_id": str(scope.user_id or "").strip(),
        "channel": str(scope.channel or "").strip(),
        "conversation_id": str(scope.conversation_id or "").strip(),
        "message_id": str(message_id or "").strip(),
    }


# LLM: v1 不含 Compact 目标，v2 签入目标，v3 再签入 workspace；已发布版本的签名字段不可改变。
# 函数用途: 按指定合同版本计算控制输入摘要，保证升级后仍能读旧账且新字段不能被篡改。
def _control_operation_input_digest(
    scope: GatewayControlScope,
    command_text: str,
    *,
    version: int,
) -> str:
    if version not in _CONTROL_INPUT_DIGEST_VERSIONS:
        raise ValueError("gateway control operation digest version is unsupported")
    message_id = str(scope.metadata.get("message_id") or "").strip()
    operation_input: dict[str, object] = {
        **_control_operation_identity_payload(scope, message_id),
        "command_text": str(command_text or "").strip(),
        "expected_turn_id": str(scope.metadata.get("expected_turn_id") or "").strip(),
        "channel_chat_type": str(
            scope.metadata.get("channel_chat_type") or ""
        ).strip().lower(),
        "channel_chat_id": str(scope.metadata.get("channel_chat_id") or "").strip(),
        "all_user_access": bool(scope.all_user_access),
    }
    if version >= _CONTROL_INPUT_DIGEST_TARGET_VERSION:
        operation_input["target_control_message_id"] = str(
            scope.metadata.get("target_control_message_id") or ""
        ).strip()
    if version >= 3:
        operation_input["workspace"] = scope.workspace
    return hashlib.sha256(_canonical_json(operation_input).encode("utf-8")).hexdigest()


# LLM: 没有目录声明的输入继续使用 v2，保证既有回执可重试；显式目录必须使用 v3 并冻结原值。
# 函数用途: 按结构化输入选择摘要合同版本，避免新字段绕过回执完整性检查。
def _control_input_version(scope: GatewayControlScope) -> int:
    return (
        _CONTROL_INPUT_DIGEST_CURRENT_VERSION
        if scope.workspace is not None
        else _CONTROL_INPUT_DIGEST_TARGET_VERSION
    )


# LLM: Owner refs use a separate integrity digest so authenticated client input can be replayed
# after routing configuration changes without treating that server-side change as new input.
# 函数用途: 计算控制回执中固定 owner 三元组的完整性摘要。
def _owner_ref_digest(owner: OwnerIdentity | None) -> str:
    if owner is None:
        return ""
    payload = {
        "provider": owner.provider,
        "owner_kind": owner.owner_kind,
        "owner_id": owner.owner_id,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


# LLM: Persisted owner fields are validated as an exact OwnerIdentity before any store lookup.
# 函数用途: 从控制回执恢复并校验唯一 owner 身份，拒绝非法路径片段和身份组合。
def _receipt_owner_identity(receipt: GatewayControlOperationReceipt) -> OwnerIdentity:
    if receipt.owner_kind == "main":
        owner = OwnerIdentity.local_main()
    elif receipt.owner_kind == "group":
        owner = OwnerIdentity.provider_group(receipt.owner_provider, receipt.owner_id)
    elif receipt.owner_kind == "user":
        owner = OwnerIdentity.provider_user(receipt.owner_provider, receipt.owner_id)
    else:
        raise DataCorruptionError("gateway control operation owner kind is invalid")
    if (
        owner.provider != receipt.owner_provider
        or owner.owner_kind != receipt.owner_kind
        or owner.owner_id != receipt.owner_id
    ):
        raise DataCorruptionError("gateway control operation owner identity is invalid")
    return owner


# LLM: Hash inputs use one deterministic JSON representation across processes and Python versions.
# 函数用途: 把控制身份字典编码为稳定 JSON 文本。
def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# LLM: Non-numeric or infinite timestamps are corruption, never a zero-value compatibility alias.
# 函数用途: 严格解析控制回执更新时间。
def _control_operation_timestamp(value: object) -> float:
    try:
        timestamp = float(value)
    except (TypeError, ValueError) as exc:
        raise DataCorruptionError("gateway control operation timestamp is invalid") from exc
    if not math.isfinite(timestamp) or timestamp < 0:
        raise DataCorruptionError("gateway control operation timestamp is invalid")
    return timestamp


__all__ = [
    "GatewayControlOperationConflict",
    "GatewayControlOperationIdentityRequired",
    "GatewayControlOperationReceipt",
    "execute_gateway_control_operation",
    "gateway_control_operation_identity",
    "gateway_control_operation_path",
    "gateway_control_operation_status_payload",
    "read_gateway_control_operation",
    "reconcile_gateway_control_operation",
]
