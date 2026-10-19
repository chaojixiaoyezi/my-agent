# LLM: 原builder/codec唯一；cold/ready固定HTTP源只try原op T并准备原回执，既存全状态只读；v5/claimed/活封印及默认本地执行不变。
# 模块用途: 原私卡action仅扩同一回执v5，旧slash和v1-v4字节不变；保存唯一控制回执并处理安全重试，目录、目标或命令变化不能借用原消息编号执行。
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
    persisted_control_command_text,
)
from ..runtime_errors import DataCorruptionError, runtime_error_report
from ..user_space.owner_resolver import OwnerIdentity
from .control_service import (
    GatewayControlScope,
    bind_gateway_control_scope_owner,
    execute_gateway_conversation_control,
    reconcile_gateway_steer_delivery,
)
from .event_points import command_completed
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
_CONTROL_INPUT_DIGEST_CURRENT_VERSION = 4
_CONTROL_INPUT_DIGEST_VERSIONS = {
    5,  # 仅原签名私卡决定action；正常slash仍选择原v1-v4。
    _CONTROL_INPUT_DIGEST_LEGACY_VERSION,
    _CONTROL_INPUT_DIGEST_TARGET_VERSION,
    3,
    _CONTROL_INPUT_DIGEST_CURRENT_VERSION,
}
_CONTROL_OPERATION_STATES = {
    "prepared",
    "claimed",
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
    "details",
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
# 类用途: 保存原控制身份、精确目标和分派阶段；私卡action不是执行许可，完整回执校验先于执行。
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
    # v2（tref2）：本回合的会话线程 id（执行时解析一次，与工具事件同源）；旧回执没有该字段时为空。
    conversation_thread_id: str = ""
    workspace: object = None
    expected_execution: object = None
    dispatch: object = None
    private_approval: object = None

    # LLM: 保存固定身份、可选目录与摘要版本；准备后执行和对账只能读回执，不重读当前 HTTP 输入。
    # 函数用途: 保存规范控制回执及可选私卡action/分派事实，不序列化AGENTS、Memory封印、RunParams或writer。
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
            "conversation_thread_id": self.conversation_thread_id,
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
        if self.expected_execution is not None:
            payload["expected_execution"] = self.expected_execution
        if self.dispatch is not None:
            payload["dispatch"] = self.dispatch
        if self.private_approval is not None:
            payload["private_approval"] = self.private_approval
        return payload

    # LLM: 读取时重算身份与对应版本的摘要；目录篡改或旧版未签名目录都必须当作账本损坏拒绝。
    # 函数用途: 恢复完整控制输入与可选原私卡动作、分派绑定及claimed执行者，拒绝缺来源的认领。
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
            conversation_thread_id=str(data.get("conversation_thread_id") or "").strip(),
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
            expected_execution=data.get("expected_execution"),
            dispatch=data.get("dispatch"),
            private_approval=data.get("private_approval"),
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
# /admin、/approve 的密码在写回执、算摘要之前就换成脱敏正文；明文只随内存中的 command 交给执行服务一次。
# 事件中心（B6 观察计数）由调用方放进 scope.event_hub 带进来；它是只读展示依赖，不落盘、不参与身份判定。
# 函数用途: 幂等执行一条控制命令，并把崩溃边界保存为 completed 或 terminal_unknown。
# LLM: 可选model_transaction仅来自当前真实HTTP模型producer；沿原receipt身份/状态机，原Count0和其它kind参数缺省时完全走原调用。
def execute_gateway_control_operation(
    agent: object,
    paths: GatewayPaths,
    command: ConversationControlCommand,
    scope: GatewayControlScope,
    *,
    command_text: str,
    model_transaction=None,
) -> GatewayControlOperationReceipt:
    if model_transaction is not None:
        from .model_metadata_transaction import require_model_control_source
        require_model_control_source(model_transaction, command, scope, command_text)
    else:
        scope = bind_gateway_control_scope_owner(agent, scope)
    command_text = persisted_control_command_text(command, command_text)
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
                **({} if model_transaction is None else {"model_transaction": model_transaction}),
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
                **({} if model_transaction is None else {"model_transaction": model_transaction}),
        )


# LLM: 原op锁首次执行者冻结同一控制；实际local/model source仅传固定receipt叶，远端仍原分派，Count0无source保持原调用。
# 函数用途: 在原操作锁内准备同一控制；远端只分派，本机沿唯一副作用入口执行并记原回执。
def _execute_gateway_control_operation_locked(
    agent: object,
    paths: GatewayPaths,
    *,
    operation_id: str,
    input_digest: str,
    command: ConversationControlCommand,
    scope: GatewayControlScope,
    command_text: str,
    model_transaction=None,
) -> GatewayControlOperationReceipt:
    receipt = _load_or_prepare_control_operation_locked(
        agent, paths,
        operation_id=operation_id,
        input_digest=input_digest,
        command=command,
        scope=scope,
        command_text=command_text,
        **({} if model_transaction is None else {"model_transaction": model_transaction}),
    )
    if model_transaction is not None:
        return _execute_local_control_operation_locked(agent, paths, receipt, command, scope, model_transaction=model_transaction)
    from .multiprocess_control_forward import prepare_remote_control
    from .multiprocess_local_control import local_control_operation_scope

    forwarded = prepare_remote_control(agent, paths, receipt)
    if forwarded is not None:
        return forwarded
    with local_control_operation_scope(agent, paths, receipt) as source:
        return _execute_local_control_operation_locked(agent, paths, receipt, command, scope,
            **({} if source is None else {'owner_management_source': source}))


# LLM: 原op锁与同一C生命周期内执行；当次HTTP/model/local来源不落盘，每个回执状态沿固定写叶，重放不重复业务。
# 函数用途: 保留本机控制的唯一副作用与终态路径，以原来源完成准备、执行和末态写入。
def _execute_local_control_operation_locked(agent, paths, receipt, command, scope, *, model_transaction=None, owner_management_source=None):
    operation_id = receipt.operation_id
    if receipt.state in {"completed", "terminal_unknown"}:
        return _reconcile_control_operation_locked(agent, paths, receipt,
            **({} if owner_management_source is None else {"owner_management_source": owner_management_source}))
    if model_transaction is None:
        receipt = _with_control_conversation_thread(agent, receipt, scope)
    elif receipt.state != "executing":
        from .model_metadata_transaction import model_control_thread_id
        receipt = replace(receipt, conversation_thread_id=model_control_thread_id(model_transaction, _receipt_scope(receipt, scope.event_hub)))
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
        _write_control_operation_json(
            gateway_control_operation_path(paths, operation_id),
            uncertain.to_dict(),
            model_transaction=model_transaction,
            **({} if owner_management_source is None else {"owner_management_source": owner_management_source}),
        )
        return _reconcile_control_operation_locked(agent, paths, uncertain,
            **({} if owner_management_source is None else {"owner_management_source": owner_management_source}))
    executing = replace(receipt, state="executing", updated_at=time.time())
    _write_control_operation_json(
        gateway_control_operation_path(paths, operation_id),
        executing.to_dict(),
        model_transaction=model_transaction,
            **({} if owner_management_source is None else {"owner_management_source": owner_management_source}),
    )
    effect_scope = _receipt_scope(receipt, scope.event_hub)
    if command.kind == "agents":
        from .agents_control_service import bind_agents_effect_scope
        effect_scope = bind_agents_effect_scope(scope, effect_scope, command)
    if command.kind == "memory":
        from .memory_candidate_control import bind_memory_effect_scope
        effect_scope = bind_memory_effect_scope(scope, effect_scope, command)
    try:
        if model_transaction is None:
            result = execute_gateway_conversation_control(agent, paths, command, effect_scope)
        else:
            from .model_profile_service import execute_model_text_control
            result = execute_model_text_control(None, command, effect_scope, metadata_source=model_transaction)
    except Exception as exc:  # noqa: BLE001 - any partial command effect must remain unknown.
        if model_transaction is not None:
            from .model_metadata_transaction import raise_model_transport_error
            raise_model_transport_error(exc)
        uncertain = replace(
            executing,
            state="terminal_unknown",
            error=runtime_error_report(exc, context="gateway.control_operation.execute"),
            updated_at=time.time(),
        )
        _write_control_operation_json(
            gateway_control_operation_path(paths, operation_id),
            uncertain.to_dict(),
            model_transaction=model_transaction,
            **({} if owner_management_source is None else {"owner_management_source": owner_management_source}),
        )
        return _reconcile_control_operation_locked(agent, paths, uncertain,
            **({} if owner_management_source is None else {"owner_management_source": owner_management_source}))
    if model_transaction is not None:
        from .model_metadata_transaction import model_control_thread_id
        executing = replace(executing, conversation_thread_id=model_control_thread_id(model_transaction, effect_scope))
    completed = replace(
        executing,
        state="completed",
        result=result.to_dict(),
        error={},
        updated_at=time.time(),
    )
    _write_control_operation_json(
        gateway_control_operation_path(paths, operation_id),
        completed.to_dict(),
        model_transaction=model_transaction,
            **({} if owner_management_source is None else {"owner_management_source": owner_management_source}),
    )
    command_completed(agent, completed)
    return completed


# LLM: Count0默认原写口；管理/model/local仅透传实际source给固定receipt叶，同op/owner/raw CAS/readback后登记原增量，不扫描收编。
# 函数用途: 使模型prepare/executing/完成写与原C关闭互斥，网络不在这里执行。
def _write_control_operation_json(path, payload, *, model_transaction=None, owner_management_source=None):
    if owner_management_source is not None:
        from .multiprocess_local_control import _LocalControlWriteSource
        if (type(owner_management_source) is not _LocalControlWriteSource or model_transaction is not None
                or path != gateway_control_operation_path(owner_management_source.paths, owner_management_source.receipt.operation_id)):
            raise ValueError("control receipt source binding is invalid")
        owner_management_source.commit_control_receipt(payload)
        return
    if model_transaction is None:
        write_json_file_atomic(path, payload)
        return
    from .multiprocess_local_control import _write_original_control_receipt
    model_transaction.perform(lambda: _write_original_control_receipt(model_transaction, path, payload))


# LLM: v2（tref2）：命令事件要用权威会话线程（与工具事件同源）。新执行的回执在写盘前按冻结的渠道
#   身份只读解析一次；解析不到（新绑定/多 owner 未挂载）留空，绝不影响控制命令本身。
#   已存在的回执（重放/对账路径）不经过这里，不回填旧行。
# 函数用途: 给新执行的回执补上本回合会话线程 id；已有值或解析失败时原样返回。
def _with_control_conversation_thread(
    agent: object, receipt: GatewayControlOperationReceipt, scope: GatewayControlScope
) -> GatewayControlOperationReceipt:
    if receipt.conversation_thread_id:
        return receipt
    if isinstance(receipt.dispatch, dict) and "execution" in receipt.dispatch:
        from .execution_target import GatewayExecutionTarget

        target = GatewayExecutionTarget.from_dict(receipt.dispatch["execution"])
        if target is not None:
            return replace(receipt, conversation_thread_id=target.thread_id)
    thread_id = _control_operation_thread_id(agent, scope)
    return replace(receipt, conversation_thread_id=thread_id) if thread_id else receipt


# LLM: 只读解析：按回执冻结的 (channel, conversation_id, user_id) 查渠道绑定；多 owner 时先物化
#   scope 指向的 owner agent（与控制命令执行同一解析），失败退回传入的 agent；任何异常都按解析不到处理。
# 函数用途: 解析一条控制命令对应的会话线程 id，失败返回空串。
def _control_operation_thread_id(agent: object, scope: GatewayControlScope) -> str:
    try:
        store = getattr(_control_operation_agent(agent, scope), "conversation_store", None)
        thread = store.threads.resolve(
            channel=str(scope.channel or "").strip(),
            channel_conversation_id=str(scope.conversation_id or "").strip(),
            channel_user_id=str(scope.user_id or "").strip(),
        )
        return str(getattr(thread, "thread_id", "") or "")
    except Exception:  # noqa: BLE001 事件身份解析失败不能阻断控制命令
        return ""


# 函数用途: 取 scope 指向 owner 的 agent；没有 owner 信息或物化失败时退回 base agent。
def _control_operation_agent(agent: object, scope: GatewayControlScope):
    owner = getattr(scope, "resolved_owner", None)
    if owner is None:
        return agent
    try:
        from .request_worker import _resolve_request_agent_for_owner

        return _resolve_request_agent_for_owner(agent, owner)
    except Exception:  # noqa: BLE001 退回 base agent，解析线程失败时同样留空
        return agent


# LLM: 查询不重放控制副作用；正count原local来源仅包住现有回执对账写，Count0仍原链，源失效返回原观察。
# 函数用途: 查询并对账一条控制操作，不重新执行停止、压缩或持续任务。
def reconcile_gateway_control_operation(
    agent: object,
    paths: GatewayPaths,
    operation_id: str,
) -> GatewayControlOperationReceipt | None:
    observed = read_gateway_control_operation(paths, operation_id)
    if observed is None:
        return None
    from .multiprocess_control_forward import read_remote_control_status
    from .multiprocess_local_control import (
        LocalControlCoordinatorUnavailable,
        local_control_operation_scope,
    )

    forwarded = read_remote_control_status(paths, observed, agent=agent)
    if forwarded is not None:
        return forwarded
    with try_gateway_control_operation_transition(paths, operation_id) as acquired:
        if not acquired:
            return observed
        try:
            with local_control_operation_scope(agent, paths, observed) as source:
                return _reconcile_local_control_operation_locked(agent, paths, operation_id,
                    **({} if source is None else {'owner_management_source': source}))
        except LocalControlCoordinatorUnavailable:
            return observed


# LLM: 原op锁私卡先读原permission实际callee；变化才将expected/actual updated送固定本机对账叶，Count0原写，GET不重投输入或业务。
# 函数用途: 结算本机旧executing及插话回执；每次原状态更新保持完整来源和原件检查。
def _reconcile_local_control_operation_locked(agent, paths, operation_id, *, owner_management_source=None):
    receipt = read_gateway_control_operation(paths, operation_id)
    if receipt is None:
        return None
    if receipt.private_approval is not None:
        from .private_approval_control import reconcile_private_approval_locked
        updated = reconcile_private_approval_locked(paths, receipt, agent=agent)
        if updated != receipt:
            if owner_management_source is None:
                _write_control_operation_json(gateway_control_operation_path(paths, operation_id), updated.to_dict())
            else:
                from .multiprocess_local_control import (
                    _commit_original_private_approval_reconciliation,
                )
                _commit_original_private_approval_reconciliation(owner_management_source, receipt, updated)
        return updated
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
        _write_control_operation_json(gateway_control_operation_path(paths, operation_id), receipt.to_dict(),
            **({} if owner_management_source is None else {'owner_management_source': owner_management_source}))
    return _reconcile_control_operation_locked(agent, paths, receipt,
        **({} if owner_management_source is None else {"owner_management_source": owner_management_source}))


# LLM: Reads distinguish an absent row from an empty/corrupt row and validate the filename anchor.
# 函数用途: 读取并校验一条控制操作回执，不存在时返回 None。
# LLM: C模型状态显式read_only=True只读原原子发布JSON，不创建.lock/修账；默认False保Count0原gateway reader，schema/ID校验仍共用。
def read_gateway_control_operation(
    paths: GatewayPaths,
    operation_id: str,
    *, read_only: bool = False,
) -> GatewayControlOperationReceipt | None:
    path = gateway_control_operation_path(paths, operation_id)
    if read_only:
        from ..common.json_io import read_json_object_report
        report = read_json_object_report(path, context="gateway.control_operation.read")
    else:
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
    elif receipt.state in {"prepared", "claimed", "executing"}:
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


# LLM: 原op锁内唯一callee非None结果与expected receipt送固定本机对账叶；三投递态保持，Count0原直接写，不创建guidance或发新权。
# 函数用途: 用已有插话回执刷新投递结果，其他命令保持原终态，不重做控制副作用。
def _reconcile_control_operation_locked(
    agent: object,
    paths: GatewayPaths,
    receipt: GatewayControlOperationReceipt,
    *, owner_management_source=None,
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
    if owner_management_source is not None:
        from .multiprocess_local_control import _commit_original_steer_reconciliation
        return _commit_original_steer_reconciliation(owner_management_source, receipt, result)
    updated = replace(
        receipt,
        state="completed",
        result=result.to_dict(),
        error={},
        updated_at=time.time(),
    )
    write_json_file_atomic(gateway_control_operation_path(paths, receipt.operation_id), updated.to_dict())
    return updated


# LLM: 原op锁内先验证同id完整输入；首次沿原factory冻结目标，正count实际local/model来源固定写prepared，不补选未来任务。
# 函数用途: 原子准备唯一规范控制回执，原消息重试只读同输入，不新增来源或业务重投。
def _load_or_prepare_control_operation_locked(
    agent: object,
    paths: GatewayPaths,
    *,
    operation_id: str,
    input_digest: str,
    command: ConversationControlCommand,
    scope: GatewayControlScope,
    command_text: str,
    model_transaction=None,
) -> GatewayControlOperationReceipt:
    existing = read_gateway_control_operation(paths, operation_id)
    if existing is not None:
        if existing.input_digest != input_digest:
            raise GatewayControlOperationConflict(
                "gateway control message id was reused with different input"
            )
        return existing
    receipt = _new_control_operation_receipt(scope, command.kind, command_text, operation_id, input_digest)
    _validate_control_operation_receipt(receipt)
    if model_transaction is not None:
        _write_control_operation_json(gateway_control_operation_path(paths, operation_id), receipt.to_dict(), model_transaction=model_transaction)
        return receipt
    from .multiprocess_control_forward import prepare_remote_control
    from .multiprocess_local_control import local_control_operation_scope

    # 目标和路由随首次prepared一起写入，重试不得补选未来任务。
    forwarded = prepare_remote_control(agent, paths, receipt, fresh=True)
    if forwarded is not None:
        return forwarded
    with local_control_operation_scope(agent, paths, receipt) as source:
        if source is None:
            write_json_file_atomic(gateway_control_operation_path(paths, operation_id), receipt.to_dict())
        else:
            _write_control_operation_json(gateway_control_operation_path(paths, operation_id), receipt.to_dict(),
                                          owner_management_source=source)
    return receipt


# LLM: 普通命令与私卡决定共用原owner/identity/时间字段公式；action只选v5摘要，不从JSON恢复执行源。
# 函数用途: 创建唯一原控制回执的规范初值，旧slash调用不携新字段。
def _new_control_operation_receipt(scope, command_kind, command_text, operation_id, input_digest, *, private_approval=None):
    return GatewayControlOperationReceipt(
        operation_id=operation_id,
        input_digest=input_digest,
        command_text=str(command_text or "").strip(),
        command_kind=str(command_kind or "").strip(),
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
        expected_execution=scope.metadata.get("expected_execution"),
        input_digest_version=5 if private_approval is not None else _control_input_version(scope),
        private_approval=private_approval,
        state="prepared",
        updated_at=time.time(),
    )


# LLM: 只有本次真实cold/ready HTTP固定producer可调用；op T在Root外只try，既存同ID全状态只读，不进本地reconcile/effect或重新挑目标。
# 函数用途: 复用原identity、builder和codec为C准备一次远端控制，忙时保原事实。
def prepare_coordinator_control_operation(source):
    from .coordinator_control_transport import _CoordinatorControlTransport
    from .multiprocess_control_forward import read_control_operation_raw
    from .multiprocess_global_write import GlobalWriteError

    if type(source) is not _CoordinatorControlTransport:
        raise GlobalWriteError('control_http_source_required', 'control_transport')
    source.require_current()
    with try_gateway_control_operation_transition(source.paths, source.operation_id) as acquired:
        raw = read_control_operation_raw(source.paths, source.operation_id, deadline=source.deadline)
        existing = _coordinator_control_receipt(source, raw)
        if existing is not None:
            return existing
        if not acquired:
            raise GlobalWriteError('control_operation_busy', 'control_transport')
        receipt = _new_control_operation_receipt(source.scope, source.command.kind, source.command_text,
            source.operation_id, source.input_digest)
        _validate_control_operation_receipt(receipt)
        return source.publish(receipt, raw)


# LLM: 原完整raw只由固定原路径读取；不可读/非法/身份不符必须拒绝，不能默认{}修复或丢未知字段后回写。
# 函数用途: 沿原严格codec读取固定HTTP运输看到的既存回执，同ID输入冲突仍报原冲突。
def _coordinator_control_receipt(source, raw):
    source.require_current()
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise DataCorruptionError('gateway control operation receipt is unreadable') from error
    receipt = GatewayControlOperationReceipt.from_dict(payload)
    if receipt.operation_id != source.operation_id:
        raise DataCorruptionError('gateway control operation id does not match its path')
    if receipt.input_digest != source.input_digest:
        raise GatewayControlOperationConflict('gateway control message id was reused with different input')
    return receipt


# LLM: 从已校验回执恢复首次身份、目录和目标，供执行及只读对账；当前 HTTP 输入不能重定向旧操作。
#   event_hub 是只读展示依赖、不落盘，恢复时从本次调用传进来（缺省 None→展示“暂无记录”）。
# 函数用途: 投影原控制作用域或已验证分派目标，保留v4空目标存在性，不从JSON恢复封印。
def _receipt_scope(
    receipt: GatewayControlOperationReceipt, event_hub: object | None = None, *, dispatched_execution: bool = False
) -> GatewayControlScope:
    execution = receipt.expected_execution
    if dispatched_execution and isinstance(receipt.dispatch, dict) and "execution" in receipt.dispatch:
        execution = receipt.dispatch["execution"]
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
            **({"expected_execution": execution} if receipt.input_digest_version >= 4 or execution is not None else {}),
        },
        all_user_access=receipt.all_user_access,
        resolved_owner=_receipt_owner_identity(receipt),
        workspace=receipt.workspace,
        event_hub=event_hub,
    )


# LLM: Every read recomputes the versioned id/digest used at prepare time and enforces state/result
# invariants. Rows written before exact Compact targets existed may use legacy digest v1 only when
# the target remains empty；v3 之前不能携带未签名的 workspace，字段扩展不能削弱旧账防篡改。
# 函数用途: 核对原身份摘要与分派/认领完整性；私卡v5完整签入点击输入，不恢复执行身份。
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
    if receipt.private_approval is not None:
        from .private_approval_control import require_private_approval_action
        require_private_approval_action(receipt.private_approval)
        if (receipt.input_digest_version != 5 or receipt.command_kind != 'private_tool_approval'
                or receipt.command_text != 'private_tool_approval'):
            raise DataCorruptionError("private approval action identity is invalid")
    elif receipt.input_digest_version == 5 or receipt.command_kind == 'private_tool_approval':
        raise DataCorruptionError("private approval action is missing")
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
        if version < 4 and receipt.expected_execution is not None:
            continue
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
            **({"private_approval": receipt.private_approval} if version == 5 else {}),
        ):
            matched_digest_version = version
            break
    if receipt.operation_id != expected_id or matched_digest_version == 0:
        raise DataCorruptionError("gateway control operation digest conflicts with its content")
    owner = _receipt_owner_identity(receipt)
    if receipt.owner_ref_digest != _owner_ref_digest(owner):
        raise DataCorruptionError("gateway control operation owner digest is invalid")
    if receipt.private_approval is None:
        parsed = parse_conversation_control(receipt.command_text, reject_unknown_slash=True)
        if parsed is None or parsed.kind != receipt.command_kind:
            raise DataCorruptionError("gateway control operation command kind is invalid")
    if receipt.state in {"prepared", "claimed", "executing"} and (receipt.result or receipt.error):
        raise DataCorruptionError("unfinished gateway control operation has terminal fields")
    if receipt.dispatch is not None:
        from .multiprocess_control_forward import validate_control_dispatch

        validate_control_dispatch(receipt)
    elif receipt.state == "claimed":
        raise DataCorruptionError("claimed gateway control operation lacks dispatch")
    if receipt.state == "completed":
        if not receipt.result or receipt.error:
            raise DataCorruptionError("completed gateway control operation is invalid")
        _validate_control_operation_result(receipt)
    if receipt.state == "terminal_unknown" and (receipt.result or not receipt.error):
        raise DataCorruptionError("unknown gateway control operation is invalid")
    if not math.isfinite(receipt.updated_at) or receipt.updated_at < 0:
        raise DataCorruptionError("gateway control operation timestamp is invalid")
    return matched_digest_version


# LLM: 控制回执是副作用服务的投影；身份、投递和详情必须在交给 HTTP/TUI/IM 前校验，详情中的领域状态不改变外层完成状态。
# 函数用途: 校验持久控制结果，保留预览确认所需的结构化详情而不把它解释成新授权。
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
    for key in ("task_status", "details"):
        if key in result and not isinstance(result.get(key), dict):
            raise DataCorruptionError(f"gateway control operation result {key} is invalid")


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


# LLM: v1 不含 Compact 目标，v2 签入目标，v3 签入 workspace，v4 签入真实执行绑定；旧版本签名不可改变。
# 函数用途: v1-v4原字节保持，仅v5签入完整私卡action；摘要不产生执行权。
def _control_operation_input_digest(
    scope: GatewayControlScope,
    command_text: str,
    *,
    version: int,
    private_approval=None,
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
    if version >= 4:
        operation_input["expected_execution"] = scope.metadata.get("expected_execution")
    if version >= 5:
        operation_input['private_approval'] = private_approval
    return hashlib.sha256(_canonical_json(operation_input).encode("utf-8")).hexdigest()


# LLM: 原无目录/有目录输入分别保留 v2/v3 可重试；带执行绑定必须用 v4，重试不能切换目标。
# 函数用途: 按结构化输入选择摘要合同版本，避免新字段绕过回执完整性检查。
def _control_input_version(scope: GatewayControlScope) -> int:
    if "expected_execution" in scope.metadata:
        return _CONTROL_INPUT_DIGEST_CURRENT_VERSION
    return 3 if scope.workspace is not None else _CONTROL_INPUT_DIGEST_TARGET_VERSION


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
