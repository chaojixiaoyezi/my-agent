# LLM: 控制状态沿 owner/thread 和持久回执处理；首次 Goal 目录共用请求校验，不改变已有执行目录。
# Goal 状态页只读共享 goal_clock，不持有独立计时或用量状态。
# 整任务停止在任务锁内关闭主执行权和冻结资源；实际回收在锁外，不追随恢复轮。
# 模块用途: 将显式控制命令应用到当前会话，保持任务、历史、权限和中断边界。
from __future__ import annotations

"""Conversation task controls backed by request and durable task ledgers.

给人看的解释：
飞书或终端发来的控制命令在这里查找“这个用户、这个会话”正在运行的任务。
短请求结束后仍沿着持久任务链接工作，只允许纠偏和停止自己的当前任务。
"""

import json
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..concurrency.interrupt import (
    interrupt_by_name,
    is_interrupted,
    register_interruptible,
)
from ..conversation.compact import (
    ConversationCompactOptions,
    inspect_conversation_context,
    prepare_conversation_context,
    render_conversation_context_usage,
)
from ..conversation.compact_guard import ConversationCompactError
from ..conversation.compact_provider_surface import ConversationCompactModelSurface
from ..conversation.control_commands import (
    ConversationControlCommand,
    ConversationControlResult,
    ConversationTaskStatus,
    NamedConversationWorkStatus,
    conversation_compact_interrupt_name,
    conversation_request_interrupt_name,
    render_conversation_task_status,
    render_verbose_control,
)
from ..conversation.models import (
    THREAD_TASK_LINK_INACTIVE_STATUSES,
    GuidanceEntry,
    new_id,
    thread_task_run_started_at,
)
from ..conversation.run_claim import (
    ConversationRunLaneRequest,
    claim_heartbeat_interval_seconds,
    conversation_run_lane,
)
from ..memory_store.lifecycle import (
    MemoryCuratorLifecycleRequest,
    request_memory_curator_for_session,
)
from ..user_space.owner_resolver import OwnerIdentity
from .audit_control_service import AuditControlRequest, execute_audit_control_operation
from .goal_control_service import GoalControlRequest, execute_goal_control_operation
from .io import gateway_turn_transition, read_json_file_report, update_json_file_atomic
from .paths import GatewayPaths, gateway_chunk_path, gateway_paths_from_root
from .workspace_scope import GatewayWorkspaceScopeError, gateway_request_workspace_scope

_ACTIVE_SUBAGENT_STATUSES = {
    "PLANNING",
    "PENDING",
    "RUNNING",
    "BLOCKED",
    "PAUSED",
}
_DONE_SUBAGENT_STATUSES = {"DONE", "CANCELLED"}


# LLM: Authenticated issuer facts and a once-resolved owner are separate. Durable control receipts
# persist both; callers must never derive owner authority from command text or a later config read.
# workspace 是未校验的客户端 JSON，必须经过共享目录校验才可写入线程，不能据此取得权限。
# 类用途: 保存一条控制命令的可信来源、会话定位和可选的固定 owner 身份。
@dataclass(frozen=True)
class GatewayControlScope:
    user_id: str
    channel: str
    conversation_id: str
    metadata: dict[str, object] = field(default_factory=dict)
    all_user_access: bool = False
    resolved_owner: OwnerIdentity | None = None
    workspace: object = None


@dataclass(frozen=True)
class _GatewayRequestRecord:
    path: Path | None
    payload: dict[str, object]
    target_kind: str = "request"
    linked_request: _GatewayRequestRecord | None = None


# LLM: 承接本次控制锁内的整树停止准备；错误保留固定清单，接纳不等于资源已退出。
# 类用途: 让控制应答与后台清理共享同一份准备结果，包括部分失败。
@dataclass(frozen=True)
class _MainTaskStop:
    task_link: object | None = None
    resources: object | None = None
    preparation_errors: tuple[str, ...] = ()


# LLM: A receipt state comes only from the owner-scoped ConversationStore. It lets control
# routing distinguish a terminal replay from an in-flight write before inspecting live requests.
# 类用途: 保存一条活动回合补充消息的持久回执及其权威存储位置。
@dataclass(frozen=True)
class _SteerReceiptState:
    store: object
    dedupe_key: str
    status: str
    entry: GuidanceEntry
    input_matches: bool = True


# LLM: A channel message is unique inside one owner/thread. The receipt stores the exact turn id
# as a validated fact, while this lookup identity remains discoverable after that turn disappears.
# 函数用途: 生成跨重试可查询的活动回合补充消息幂等键。
# LLM: New cross-process ingress keys use authenticated channel scope, which is available before
# any active-turn side effect. The owner store remains the outer isolation boundary.
# 函数用途: 为普通消息预先生成可持久化的 guidance 幂等键，Gateway 崩溃后无需猜 thread。
def active_turn_guidance_dedupe_key(scope: GatewayControlScope) -> str:
    return json.dumps(
        {
            "schema_version": "active_turn_guidance_key.v2",
            "kind": "active_turn_user_input",
            "user_id": str(scope.user_id or "").strip(),
            "channel": str(scope.channel or "").strip(),
            "conversation_id": str(scope.conversation_id or "").strip(),
            "channel_message_id": _steer_channel_message_id(scope),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


# LLM: v1 lookup exists only for an explicit on-disk migration read; new writes never use it.
# 函数用途: 生成旧版 thread 作用域幂等键，以便升级后仍能查询已有回执。
def _legacy_active_turn_guidance_dedupe_key(thread_id: str, channel_message_id: str) -> str:
    return json.dumps(
        {
            "kind": "active_turn_user_input",
            "thread_id": str(thread_id or "").strip(),
            "channel_message_id": str(channel_message_id or "").strip(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


# LLM: Expected-turn authority is a typed ingress fact. Message text and current task labels can
# never substitute for this exact id.
# 函数用途: 读取客户端提交时看到的活动回合 ID。
def _steer_expected_turn_id(scope: GatewayControlScope) -> str:
    return str(scope.metadata.get("expected_turn_id") or "").strip()


# LLM: Client correlation accepts only the explicit channel message id carried by the adapter.
# 函数用途: 读取补充消息在客户端侧生成的稳定 ID。
def _steer_channel_message_id(scope: GatewayControlScope) -> str:
    return str(scope.metadata.get("message_id") or "").strip()


# LLM: A durable task may own the guidance queue while its linked processing request remains the
# active turn observed by the client. Exact-turn comparison therefore uses the linked request.
# 函数用途: 返回当前控制目标对应的真实前台回合 ID。
def _active_turn_request_id(active: _GatewayRequestRecord | None) -> str:
    if active is None:
        return ""
    linked = active.linked_request
    if linked is not None and linked.path is not None and linked.path.exists():
        return _record_id(linked)
    return _record_id(active)


# LLM: 插话持久操作经 guidance 领域组件； Receipt lookup resolves the same owner/thread store as control routing and validates all
# available ingress facts. It never scans another owner's files or infers identity from content.
# 函数用途: 查询一条补充消息是否已经有持久化投递结果。
def _active_turn_steer_receipt(
    base_agent: object,
    command: ConversationControlCommand,
    scope: GatewayControlScope,
) -> _SteerReceiptState | None:
    message_id = _steer_channel_message_id(scope)
    if not message_id:
        return None
    owner_agent = _request_agent_for_scope(base_agent, scope)
    store = owner_agent.conversation_store
    thread = store.threads.resolve(
        channel=scope.channel,
        channel_conversation_id=scope.conversation_id,
        channel_user_id=scope.user_id,
    )
    if thread is None:
        return None
    key = active_turn_guidance_dedupe_key(scope)
    receipt = store.guidance.receipt(key)
    if receipt is None:
        legacy_key = _legacy_active_turn_guidance_dedupe_key(thread.thread_id, message_id)
        receipt = store.guidance.receipt(legacy_key)
        if receipt is not None:
            key = legacy_key
    if receipt is None:
        return None
    metadata = receipt.entry.metadata if isinstance(receipt.entry.metadata, dict) else {}
    expected_turn_id = _steer_expected_turn_id(scope)
    input_matches = all(
        (
            receipt.entry.message == str(command.value or "").strip(),
            str(metadata.get("thread_id") or "").strip() == thread.thread_id,
            str(metadata.get("channel_message_id") or "").strip() == message_id,
            not expected_turn_id
            or str(metadata.get("expected_turn_id") or "").strip() == expected_turn_id,
        )
    )
    return _SteerReceiptState(
        store=store,
        dedupe_key=key,
        status=receipt.status,
        entry=receipt.entry,
        input_matches=input_matches,
    )


# LLM: Terminal receipt projection preserves the delivery tri-state separately from command
# prose. Rejected receipts never become a successful queue acknowledgement.
# 函数用途: 把持久回执转换成 Gateway 控制响应。
def _control_result_from_steer_receipt(
    receipt: _SteerReceiptState,
) -> ConversationControlResult:
    if not receipt.input_matches:
        return ConversationControlResult(
            "steer",
            False,
            "同一消息 ID 已用于另一条内容或另一回合，本次补充被拒绝。",
            request_id=str(receipt.entry.metadata.get("expected_turn_id") or ""),
            delivery_status="rejected",
            guidance_dedupe_key=receipt.dedupe_key,
        )
    if receipt.status == "consumed":
        return ConversationControlResult(
            "steer",
            True,
            "该补充消息已由当前回合接收。",
            request_id=str(receipt.entry.metadata.get("expected_turn_id") or "")
            or receipt.entry.target_id,
            delivery_status="accepted",
            guidance_dedupe_key=receipt.dedupe_key,
        )
    if receipt.status == "rejected":
        return ConversationControlResult(
            "steer",
            False,
            "目标回合已结束或切换，补充消息未进入其他任务。",
            request_id=str(receipt.entry.metadata.get("expected_turn_id") or ""),
            delivery_status="rejected",
            guidance_dedupe_key=receipt.dedupe_key,
        )
    return ConversationControlResult(
        "steer",
        True,
        "补充消息仍在确认；请使用同一消息 ID 继续对账。",
        request_id=str(receipt.entry.metadata.get("expected_turn_id") or ""),
        delivery_status="unknown",
        guidance_dedupe_key=receipt.dedupe_key,
    )


# LLM: Durable control-operation reconciliation may only inspect an existing guidance receipt;
# it must never append guidance while servicing a GET/status retry after an ambiguous POST.
# 函数用途: 只读查询同一条 `/btw` 是否已有权威投递回执，不存在时返回 None。
def reconcile_gateway_steer_delivery(
    agent: object,
    paths: GatewayPaths,
    command: ConversationControlCommand,
    scope: GatewayControlScope,
) -> ConversationControlResult | None:
    if command.kind != "steer" or not command.valid:
        return None
    try:
        receipt = _active_turn_steer_receipt(agent, command, scope)
    except Exception:
        return None
    if receipt is not None:
        if not receipt.input_matches or receipt.status in {"consumed", "rejected"}:
            return _control_result_from_steer_receipt(receipt)
        turn_id = str(
            receipt.entry.metadata.get("expected_turn_id")
            if isinstance(receipt.entry.metadata, dict)
            else ""
        ).strip() or _steer_expected_turn_id(scope)
        return _reconcile_existing_steer_receipt(paths, scope, receipt, turn_id)
    turn_id = _steer_expected_turn_id(scope)
    if not turn_id:
        return None
    # A crash may leave the wrapper operation after executing but before append_guidance_once.
    # Re-read the exact receipt while holding T so a concurrent append/terminal cannot be missed.
    with gateway_turn_transition(paths, turn_id):
        try:
            receipt = _active_turn_steer_receipt(agent, command, scope)
        except Exception:
            return None
        lifecycle = _exact_gateway_turn_lifecycle_locked(paths, scope, turn_id)
        if receipt is None and lifecycle == "terminal":
            return ConversationControlResult(
                "steer",
                False,
                "目标回合已结束，且未发现该补充消息的投递记录。",
                request_id=turn_id,
                delivery_status="rejected",
                guidance_dedupe_key=active_turn_guidance_dedupe_key(scope),
            )
    if receipt is None:
        return None
    if not receipt.input_matches or receipt.status in {"consumed", "rejected"}:
        return _control_result_from_steer_receipt(receipt)
    return _reconcile_existing_steer_receipt(paths, scope, receipt, turn_id)


# LLM: 三类控制分别调度 Goal、中断回合或停止任务资源；只有明确 stop 才组合，未知控制不能回退到停止。
# 函数用途: 分派结构化控制，避免已暂停 Goal 或普通任务的 interrupt 落入资源停止。
def execute_gateway_conversation_control(
    agent: object,
    paths: GatewayPaths,
    command: ConversationControlCommand,
    scope: GatewayControlScope,
) -> ConversationControlResult:
    if not command.valid:
        return ConversationControlResult(command.kind, False, command.usage)
    if command.kind == "goal":
        return _execute_goal_control(agent, command, scope)
    if command.kind == "audit":
        return _execute_audit_control(agent, paths, command, scope)
    if command.kind == "verbose":
        return _execute_verbose_control(agent, command, scope)
    if command.kind == "context":
        return _execute_context_control(agent, command, scope)
    if command.kind == "compact":
        return _execute_compact_control(agent, paths, command, scope)
    if command.kind == "effort":
        return _execute_effort_control(agent, command, scope)
    steer_receipt: _SteerReceiptState | None = None
    if command.kind == "steer":
        try:
            steer_receipt = _active_turn_steer_receipt(agent, command, scope)
        except Exception:
            return ConversationControlResult(
                "steer",
                False,
                "补充消息的持久回执暂时无法确认；系统会继续使用同一消息 ID 对账。",
                delivery_status="unknown",
            )
        if steer_receipt is not None and (
            not steer_receipt.input_matches
            or steer_receipt.status in {"consumed", "rejected"}
        ):
            return _control_result_from_steer_receipt(steer_receipt)
    if command.kind == "stop":
        compact_stop = _stop_targeted_manual_compact(scope)
        if compact_stop is not None:
            return compact_stop
        live_requests = _live_window_requests(paths, scope)
        expected_turn_id = _steer_expected_turn_id(scope)
        if expected_turn_id:
            live_requests = [
                record
                for record in live_requests
                if _active_turn_request_id(record) == expected_turn_id
            ]
            if len(live_requests) != 1:
                return ConversationControlResult(
                    "stop",
                    False,
                    "目标回合已结束或切换，未停止其他回合。",
                    request_id=expected_turn_id,
                    delivery_status="rejected",
                )
        if len(live_requests) > 1:
            return ConversationControlResult(
                "stop",
                False,
                "检测到多个冲突的活动回合，未猜测停止其中一个；请先让 Gateway 恢复器收口。",
            )
        if live_requests:
            return _stop_live_window_request(
                agent, paths, live_requests[0], scope,
                interrupt_only=command.operation == "interrupt",
            )
        # 会话运行时 interrupts the session's authoritative active turn.  Our
        # foreground request may already have yielded while its durable root
        # waits for children, so the ConversationThread active-task index—not
        # an ephemeral process/claim probe—owns this fallback.
        live_task = _active_conversation_task(agent, scope, ordinary_only=True)
        if live_task is not None:
            return _stop_active_task(
                agent, live_task, scope, interrupt_only=command.operation == "interrupt",
            )
        return ConversationControlResult(
            "stop",
            False,
            "当前没有运行中的内容，无需停止。",
        )
    active = _active_control_target(agent, paths, scope)
    if command.kind == "steer":
        expected_turn_id = _steer_expected_turn_id(scope)
        receipt_turn_id = (
            str(steer_receipt.entry.metadata.get("expected_turn_id") or "").strip()
            if steer_receipt is not None
            else ""
        )
        required_turn_id = expected_turn_id or receipt_turn_id
        active_turn_id = _active_turn_request_id(active)
        if steer_receipt is not None:
            return _reconcile_existing_steer_receipt(
                paths,
                scope,
                steer_receipt,
                required_turn_id,
            )
        if expected_turn_id and expected_turn_id != active_turn_id:
            return ConversationControlResult(
                "steer",
                False,
                "目标回合已结束或切换，补充消息未进入其他任务。",
                request_id=active_turn_id,
                delivery_status="rejected",
            )
    if command.kind == "status":
        status = _gateway_task_status(agent, paths, scope, active)
        return ConversationControlResult(
            "status",
            True,
            render_conversation_task_status(status),
            request_id=_record_id(active),
            status=status,
        )
    if active is None:
        message = (
            "当前没有运行中的内容，补充要求未保存。"
            if command.kind == "steer"
            else "当前没有运行中的内容，无需停止。"
        )
        return ConversationControlResult(
            command.kind,
            False,
            message,
            delivery_status="rejected" if command.kind == "steer" else "",
        )
    if command.kind == "steer":
        return _steer_active_request(agent, paths, active, command, scope)
    return ConversationControlResult(command.kind, False, "不支持的会话控制命令。", error_code="UNKNOWN_CONVERSATION_CONTROL")


# LLM: Gateway 只解析已认证的 typed session event 并解析 owner；真正 durable reason 仍由 Memory Service 持有。
# 函数用途: 把会话 close/reset 信号提交给正确 owner 的唯一 MemoryCuratorService。
def request_gateway_memory_curator_lifecycle(
    base_agent: object,
    *,
    scope: GatewayControlScope,
    event: str,
) -> MemoryCuratorLifecycleRequest:
    owner_agent = _request_agent_for_scope(base_agent, scope)
    return request_memory_curator_for_session(owner_agent, event=event)


def _execute_audit_control(
    base_agent: object,
    paths: GatewayPaths,
    command: ConversationControlCommand,
    scope: GatewayControlScope,
) -> ConversationControlResult:
    """Resolve owner/thread authority, then delegate exact Audit lifecycle semantics."""
    try:
        owner_agent = _request_agent_for_scope(base_agent, scope)
        store = owner_agent.conversation_store
        pending = (
            _pending_exact_audit_requests(paths, scope, command.name)
            if command.operation == "clear"
            else []
        )
        stopped_request_ids: list[str] = []
        for record in pending:
            request_id = _record_id(record)
            marked, _failure = _mark_request_stopping(record, scope)
            if not marked:
                continue
            stopped_request_ids.append(request_id)
            interrupt_by_name(conversation_request_interrupt_name(request_id))
            from ..conversation.task_resources import freeze_task_resources

            with owner_agent.subagents.creation_guard():
                resources = freeze_task_resources(owner_agent, None, request_id)
            _cancel_request_execution(request_id, resources=resources)
        thread = store.threads.get_or_create(
            {
                "canonical_user_id": scope.user_id,
                "owner_id": str(
                    getattr(getattr(owner_agent, "home_paths", None), "owner_id", "") or ""
                ),
                "owner_home": str(
                    getattr(getattr(owner_agent, "home_paths", None), "owner_home_dir", "") or ""
                ),
                "channel": scope.channel,
                "channel_conversation_id": scope.conversation_id,
                "channel_user_id": scope.user_id,
                "title": "Audit",
            }
        )
        result = execute_audit_control_operation(
            AuditControlRequest(
                owner_agent=owner_agent,
                store=store,
                thread=thread,
                command=command,
            )
        )
        if (
            not result.ok
            and command.operation == "clear"
            and stopped_request_ids
            and result.message == "没有找到这个 Audit。"
        ):
            return ConversationControlResult(
                "audit",
                True,
                f"Audit“{command.name}”已停止。",
                request_id=stopped_request_ids[0],
            )
        return result
    except Exception:
        return ConversationControlResult("audit", False, "Audit 状态暂时不可用，请稍后重试。")


def _pending_exact_audit_requests(
    paths: GatewayPaths,
    scope: GatewayControlScope,
    name: str,
) -> list[_GatewayRequestRecord]:
    selected: list[_GatewayRequestRecord] = []
    for folder in (paths.processing, paths.inbox):
        for record in _matching_requests(folder, scope):
            system_task = record.payload.get("system_task")
            attributes = (
                system_task.get("attributes")
                if isinstance(system_task, dict)
                and str(system_task.get("kind") or "") in {"audit", "audit_prepare"}
                else None
            )
            if (
                isinstance(attributes, dict)
                and (
                    not name
                    or str(attributes.get("conversation_work_name") or "") == name
                )
                and not bool(record.payload.get("cancel_requested"))
            ):
                selected.append(record)
    return selected


# LLM: 普通输入复用精确回合投递；后台执行须有同任务的 running claim，单有活动链接不能冒充执行权。
# 函数用途:把前台或后台正在执行时的补充消息交给当前代理，不让后台 Goal 的插话排到整个任务之后。
def steer_active_conversation_if_running(
    agent: object,
    paths: GatewayPaths,
    *,
    message: str,
    scope: GatewayControlScope,
) -> ConversationControlResult | None:
    """Route ordinary input into the exact active turn, or report no active turn.

    This adapts 会话运行时 ``steer_input`` semantics to the durable Gateway thread:
    structured owner/conversation state selects the exact live run, while the
    message remains opaque user input.  A caller can safely fall back to a normal
    queued request when this returns ``None`` or a non-success result.
    """
    processing = _active_request(paths, scope)
    if processing is None:
        active = _running_background_input_target(agent, scope)
        if active is None:
            return None
    else:
        linked_task_id = _linked_conversation_task_id(processing)
        active = (
            _linked_active_task_record(agent, processing, scope, linked_task_id)
            if linked_task_id else None
        ) or processing
    user_input = str(message or "").strip()
    result = _steer_active_request(
        agent,
        paths,
        active,
        ConversationControlCommand("steer", value=user_input, valid=bool(user_input)),
        scope,
    )
    if result.ok and processing is not None and active is not processing:
        # `/ask` callers keep polling the live Gateway request that already owns
        # the response stream.  The guidance itself remains bound to the linked
        # durable task so it survives the foreground/background handoff.
        return ConversationControlResult(
            result.kind,
            result.ok,
            result.message,
            request_id=_record_id(processing),
            status=result.status,
            delivery_status=result.delivery_status,
            guidance_dedupe_key=result.guidance_dedupe_key,
        )
    return result


# LLM: 只读取本 owner/thread 的原后台执行权；模型文字、Goal active 和 TUI 快照均不能单独证明在运行。
# 函数用途: 为后台执行的普通插话找到原任务邮箱；缺失、损坏、暂停和独立 Audit 均回到普通排队。
def _running_background_input_target(
    agent: object, scope: GatewayControlScope,
) -> _GatewayRequestRecord | None:
    active = _active_conversation_task(agent, scope, ordinary_only=True)
    if active is None:
        return None
    store = _request_agent_for_scope(agent, scope).conversation_store
    thread_id = str(active.payload.get("conversation_thread_id") or "")
    claim = store.claims.load(thread_id)
    if (claim.get("load_error") or claim.get("status") != "running"
            or str(claim.get("task_id") or "") != _record_id(active)):
        return None
    return active


# LLM: 先解析可信 owner；首次创建时共用请求目录校验，仅初始化空 cwd，不能重定向已绑定任务。
# 函数用途: 为显式 `/goal` 命令解析当前 owner/thread 并执行目标操作。
def _execute_goal_control(
    base_agent: object,
    command: ConversationControlCommand,
    scope: GatewayControlScope,
) -> ConversationControlResult:
    """Execute an explicit `/goal` lifecycle operation on this exact conversation."""
    try:
        owner_agent = _request_agent_for_scope(base_agent, scope)
        store = owner_agent.conversation_store
        cwd, roots = "", ()
        if command.operation == "create" and scope.workspace is not None:
            cwd, roots = gateway_request_workspace_scope(owner_agent, {"workspace": scope.workspace})
        thread = store.threads.get_or_create(
            {
                "canonical_user_id": scope.user_id,
                "owner_id": str(getattr(getattr(owner_agent, "home_paths", None), "owner_id", "") or ""),
                "owner_home": str(
                    getattr(getattr(owner_agent, "home_paths", None), "owner_home_dir", "") or ""
                ),
                "channel": scope.channel,
                "channel_conversation_id": scope.conversation_id,
                "channel_user_id": scope.user_id,
                "title": command.value[:80] or "持续目标",
            }
        )
        return execute_goal_control_operation(
            GoalControlRequest(
                owner_agent=owner_agent,
                store=store,
                thread=thread,
                command=command,
                scope=scope,
                initial_cwd=cwd,
                initial_workspace_roots=roots,
                resume_registry=lambda task_id: _resume_task_registry_record(owner_agent, task_id),
            )
        )
    except GatewayWorkspaceScopeError as exc:
        return ConversationControlResult("goal", False, str(exc), error_code=exc.error_code)
    except Exception:
        return ConversationControlResult("goal", False, "持续目标状态暂时不可用，请稍后重试。")


# LLM: Verbose is a per-thread control setting; the command text never enters a model turn.
# 函数用途:在精确 owner/thread 上读取或修改过程显示档位，并返回确定性系统回执。
def _execute_verbose_control(
    base_agent: object,
    command: ConversationControlCommand,
    scope: GatewayControlScope,
) -> ConversationControlResult:
    try:
        owner_agent = _request_agent_for_scope(base_agent, scope)
        store = owner_agent.conversation_store
        thread = store.threads.get_or_create(
            {
                "canonical_user_id": scope.user_id,
                "owner_id": str(
                    getattr(getattr(owner_agent, "home_paths", None), "owner_id", "") or ""
                ),
                "owner_home": str(
                    getattr(getattr(owner_agent, "home_paths", None), "owner_home_dir", "") or ""
                ),
                "channel": scope.channel,
                "channel_conversation_id": scope.conversation_id,
                "channel_user_id": scope.user_id,
                "title": "会话设置",
            }
        )
        if command.value:
            thread = store.threads.update_verbose_level(thread.thread_id, command.value)
        level = str(getattr(thread, "verbose_level", "off") or "off")
        return ConversationControlResult(
            "verbose",
            True,
            render_verbose_control(level, command),
        )
    except Exception:
        return ConversationControlResult(
            "verbose",
            False,
            "当前会话的详细过程设置暂时不可用，请稍后重试。",
        )


# LLM: `/context` is a read-only projection of the same owner/thread, prompt estimator, and
# compact policy used by Gateway preflight; it must not create a thread or trigger compaction.
# 函数用途: 查看当前会话的估算占用、模型窗口、自动压缩线和压缩代际。
def _execute_context_control(
    base_agent: object,
    command: ConversationControlCommand,
    scope: GatewayControlScope,
) -> ConversationControlResult:
    del command
    try:
        owner_agent = _request_agent_for_scope(base_agent, scope)
        store = owner_agent.conversation_store
        thread = _conversation_thread_for_scope(store, scope)
        from ..settings.model_scope import selected_model_scope

        with selected_model_scope(owner_agent, thread_id=thread.thread_id if thread else "", active=False):
            usage = inspect_conversation_context(owner_agent, store, thread)
            model_name = str(getattr(getattr(owner_agent, "config", None), "model_name", "") or "")
        return ConversationControlResult(
            "context",
            True,
            render_conversation_context_usage(usage, model_name=model_name),
        )
    except Exception:
        return ConversationControlResult(
            "context",
            False,
            "当前会话的上下文状态暂时不可用，请稍后重试。",
        )


# LLM: Manual Compact is a first-class cancellable turn like 会话运行时's Compact task.  Register the
# authenticated conversation plus original control message before any admission check so a TUI Esc
# can target only this operation and provider transport can close its exact socket.
# 函数用途: 为手动 Compact 建立精确中断身份，再进入唯一压缩实现。
def _execute_compact_control(
    base_agent: object,
    paths: GatewayPaths,
    command: ConversationControlCommand,
    scope: GatewayControlScope,
) -> ConversationControlResult:
    client_message_id = str(scope.metadata.get("message_id") or "").strip()
    if not client_message_id:
        client_message_id = new_id("manual-compact-local")
    interrupt_name = conversation_compact_interrupt_name(
        scope.user_id,
        scope.channel,
        scope.conversation_id,
        client_message_id,
    )
    with register_interruptible(interrupt_name):
        return _execute_registered_compact_control(
            base_agent,
            paths,
            command,
            scope,
        )


# LLM: 手动Compact只覆盖车道内冻结的全线程已完成来源；没有未来业务prompt，回执的历史估算不能冒充下一请求容量证明。
# 原摘要面仍只准备一次，取消或冲突不推进来源游标；按source.messages裁决空来源，不能凭旧全局游标猜。
# 函数用途: 对精确空闲会话提交一次全线程摘要，并清楚区分无来源、用户停止、占用与真实失败。
def _execute_registered_compact_control(
    base_agent: object,
    paths: GatewayPaths,
    command: ConversationControlCommand,
    scope: GatewayControlScope,
) -> ConversationControlResult:
    if _live_window_request(paths, scope) is not None:
        return ConversationControlResult(
            "compact",
            False,
            "当前任务仍在运行；请等本轮完成或先使用 /stop，再执行 /compact。",
            error_code="COMPACT_TURN_ACTIVE",
        )
    try:
        owner_agent = _request_agent_for_scope(base_agent, scope)
        store = owner_agent.conversation_store
        thread = _conversation_thread_for_scope(store, scope)
        if thread is None:
            return ConversationControlResult(
                "compact",
                True,
                "当前会话还没有可压缩的历史。",
            )
        from ..conversation.compact import load_conversation_compact_source
        from ..conversation.compact_scope import THREAD_COMPACT_SCOPE
        from ..settings.model_scope import selected_model_scope

        with _manual_compact_lane(owner_agent, store, thread.thread_id), selected_model_scope(owner_agent, thread_id=thread.thread_id):
            refreshed = store.threads.load(thread.thread_id)
            if refreshed is None:
                raise OSError("conversation thread disappeared before manual compact")
            source = load_conversation_compact_source(
                owner_agent, store, refreshed, scope=THREAD_COMPACT_SCOPE,
            )
            if not source.messages:
                return _manual_compact_noop_result()
            before = _manual_compact_source_tokens(owner_agent, source)
            compacted = prepare_conversation_context(
                owner_agent,
                store,
                refreshed,
                options=ConversationCompactOptions(
                    force=True,
                    custom_instructions=command.value,
                    interrupt_check=is_interrupted,
                    source=source,
                    model_surface=ConversationCompactModelSurface(
                        context_scope="conversation",
                    ),
                ),
            )
        if not compacted.compacted:
            return _manual_compact_noop_result()
        return ConversationControlResult(
            "compact",
            True,
            (
                f"Context compacted · generation {compacted.thread.compact_generation}\n"
                f"会话历史估算（不含下一请求）：{before:,} → "
                f"{compacted.projected_tokens:,} tokens；"
                f"压缩前待处理历史 {len(source.messages)} 条已完成消息。"
            ),
            status=ConversationTaskStatus(
                state="idle",
                compact_generation=compacted.thread.compact_generation,
            ),
        )
    except InterruptedError:
        if is_interrupted():
            return ConversationControlResult(
                "compact",
                False,
                "上下文压缩已中断；原历史、游标和 compact 次数均保持不变。",
                error_code="COMPACT_INTERRUPTED",
            )
        return ConversationControlResult(
            "compact",
            False,
            "当前会话正被另一个执行轮占用，本次未压缩；请稍后重试。",
            error_code="COMPACT_LANE_BUSY",
        )
    except ConversationCompactError as exc:
        # LLM: 压缩失败是 typed 运行时事实。像 COMPACT_FIXED_PREFIX_TOO_LARGE 这种"当前窗口根本装不下"
        #   的失败重试永远不会成功，不能统一回一句"请稍后重试"误导用户反复等——如实回报失败码与原话。
        # 函数用途: 把 typed 压缩失败如实上报，保留原始原因与失败码。
        code = str(getattr(exc, "error_code", "") or getattr(exc, "code", "") or "COMPACT_FAILED")
        detail = str(exc) or "手动 compact 未完成，会话游标保持原状。"
        return ConversationControlResult(
            "compact",
            False,
            f"{detail}（失败码 {code}；会话游标保持原状）",
            error_code=code,
        )
    except Exception:
        return ConversationControlResult(
            "compact",
            False,
            "手动 compact 未完成，会话游标保持原状；请稍后重试。",
            error_code="COMPACT_FAILED",
        )


# LLM: 只估算车道内冻结的thread来源；复用原计量器，不读取更晚head或将数字当未来业务容量证明。
# 函数用途: 给手动回执计算与本次摘要同范围的压缩前历史大小。
def _manual_compact_source_tokens(agent: object, source: object) -> int:
    from ..conversation.compact import _projected_context_tokens

    context = source.compact_context
    return _projected_context_tokens(
        agent, context.view.summary if context is not None else source.thread.summary,
        list(source.messages), "",
        operation_evidence=(context.view.operation_evidence if context is not None else source.thread.compact_operation_evidence),
        recent_operation_evidence=source.recent_operation_evidence,
    )


# LLM: source为空或原Compact返回未提交时均只投影同一个成功no-op，不能补写空摘要或推进代次。
# 函数用途: 返回手动压缩没有新历史时的统一回执。
def _manual_compact_noop_result() -> ConversationControlResult:
    return ConversationControlResult("compact", True, "当前会话没有新的已完成历史需要压缩。")


# LLM: The lane has a short admission deadline and also observes the registered user-interrupt
# token; once acquired, its heartbeat covers the complete summary-model call and guarded commit.
# 函数用途: 为手动 Compact 快速申请单会话执行权，并在排队期间响应精确 Esc 停止。
def _manual_compact_lane(owner_agent: object, store: object, thread_id: str):
    config = getattr(owner_agent, "config", None)
    ttl_seconds = max(
        1,
        int(getattr(config, "background_claim_ttl_seconds", 90) or 90),
    )
    deadline = time.monotonic() + 1.0
    return conversation_run_lane(
        ConversationRunLaneRequest(
            store=store,
            thread_id=thread_id,
            claim_task_id=new_id("manual-compact"),
            reason="manual_conversation_compact",
            lease_seconds=ttl_seconds,
            heartbeat_interval_seconds=claim_heartbeat_interval_seconds(
                ttl_seconds=ttl_seconds,
                configured_interval_seconds=getattr(
                    config,
                    "background_claim_heartbeat_interval_seconds",
                    0,
                ),
            ),
            interrupt_check=lambda: is_interrupted() or time.monotonic() >= deadline,
            runtime_facts={"execution_source": "conversation_control"},
        )
    )


# LLM: A stop carrying target_control_message_id may interrupt only the manual Compact registered
# under the same authenticated user/channel/conversation.  It never falls through to a foreground
# task, child tree, or another owner's operation when that exact target is absent.
# 函数用途: 处理 TUI 在手动 Compact 动画期间发来的精确 Esc 停止请求。
def _stop_targeted_manual_compact(
    scope: GatewayControlScope,
) -> ConversationControlResult | None:
    target = str(scope.metadata.get("target_control_message_id") or "").strip()
    if not target:
        return None
    if len(target) > 256:
        return ConversationControlResult(
            "stop",
            False,
            "目标 Compact 身份无效，未停止其他内容。",
            request_id=target[:256],
            delivery_status="rejected",
            error_code="COMPACT_STOP_TARGET_INVALID",
        )
    interrupt_name = conversation_compact_interrupt_name(
        scope.user_id,
        scope.channel,
        scope.conversation_id,
        target,
    )
    if interrupt_by_name(interrupt_name):
        return ConversationControlResult(
            "stop",
            True,
            "已请求停止当前上下文压缩；正在保留原历史。",
            request_id=target,
            delivery_status="accepted",
        )
    return ConversationControlResult(
        "stop",
        False,
        "目标上下文压缩已经结束或切换，未停止其他内容。",
        request_id=target,
        delivery_status="rejected",
        error_code="COMPACT_STOP_TARGET_INACTIVE",
    )


# LLM: Effort is an inference parameter, not a prose style. Until a backend exposes a verified
# structured setter, every requested level must fail explicitly without changing temperature/prompt.
# 函数用途: 查看或设置 effort 时说明当前真实能力，防止显示已设置但实际没生效。
def _execute_effort_control(
    base_agent: object,
    command: ConversationControlCommand,
    scope: GatewayControlScope,
) -> ConversationControlResult:
    try:
        owner_agent = _request_agent_for_scope(base_agent, scope)
        backend = getattr(owner_agent, "backend", None)
        levels = tuple(getattr(backend, "supported_effort_levels", ()) or ())
    except Exception:
        levels = ()
    if not levels:
        viewing = command.operation in {"view", "help"}
        prefix = (
            "用法：/effort [low|medium|high|max|auto]\n"
            if command.operation == "help"
            else ""
        )
        return ConversationControlResult(
            "effort",
            viewing,
            prefix + "当前模型接口由供应商管理推理强度，未声明可调 effort 档位；未改变任何模型参数。",
        )
    if command.operation in {"view", "help"}:
        prefix = (
            "用法：/effort [low|medium|high|max|auto]\n"
            if command.operation == "help"
            else ""
        )
        return ConversationControlResult(
            "effort",
            True,
            prefix + f"后端声明的 effort 档位：{', '.join(map(str, levels))}。",
        )
    return ConversationControlResult(
        "effort",
        False,
        "后端声明了 effort 档位，但当前运行时尚无结构化设置入口；本次未应用。",
    )


# LLM: Context controls resolve only the exact authenticated channel binding; query/manual
# compact must not create a new thread as a side effect or fall back to another conversation.
# 函数用途: 按用户、通道和会话 id 读取当前唯一 thread，找不到就返回空。
def _conversation_thread_for_scope(store: object, scope: GatewayControlScope):
    return store.threads.resolve(
        channel=scope.channel,
        channel_conversation_id=scope.conversation_id,
        channel_user_id=scope.user_id,
    )


# LLM: Resume updates the existing task projection; it never registers a second task identity.
# 函数用途: 恢复原持续任务在全局任务索引中的 running 状态。
def _resume_task_registry_record(owner_agent: object, task_id: str) -> None:
    try:
        registry = owner_agent.local_store.task_registry
        current = registry.lookup_task(task_id)
        if current is None:
            return
        registry.register_task(
            task_id,
            status="running",
            goal=str(current.get("goal") or ""),
            session_id=str(current.get("session_id") or ""),
            user_id=str(current.get("user_id") or ""),
        )
    except Exception:
        return


# LLM: Durable task selection wins over a transient chat request and uses only owner/thread links.
# 函数用途:先找会话仍活跃的根任务；尚未晋升时才回落 processing 请求。
def _active_control_target(
    base_agent: object,
    paths: GatewayPaths,
    scope: GatewayControlScope,
) -> _GatewayRequestRecord | None:
    processing = _active_request(paths, scope)
    linked_task_id = _linked_conversation_task_id(processing)
    if linked_task_id:
        durable = _linked_active_task_record(base_agent, processing, scope, linked_task_id)
        if durable is not None:
            return durable
        # The durable task may have crossed to interrupted while its live request
        # is still draining.  Keep controls on that exact request instead of
        # jumping to an unrelated stale task in the same conversation.
        return processing
    return _active_conversation_task(base_agent, scope) or processing


def _linked_active_task_record(
    base_agent: object,
    processing: _GatewayRequestRecord,
    scope: GatewayControlScope,
    task_id: str,
) -> _GatewayRequestRecord | None:
    """Bind one live request only to the durable task named by its runtime facts."""
    durable = _active_conversation_task(base_agent, scope, task_id=task_id)
    if durable is None:
        return None
    return _GatewayRequestRecord(
        durable.path,
        durable.payload,
        durable.target_kind,
        processing,
    )


# LLM: Request selection uses structured owner/channel/conversation facts and never message text.
# 函数用途:找到当前会话唯一的 processing 请求；同会话单飞时通常只有一个。
def _active_request(paths: GatewayPaths, scope: GatewayControlScope) -> _GatewayRequestRecord | None:
    records = [
        record
        for record in _matching_requests(paths.processing, scope)
        if not _request_is_detached(record) and _request_accepts_active_input(record)
    ]
    if len(records) != 1:
        # A scope has one active-turn authority. Multiple processing records
        # are a recovery conflict, so ordinary input queues instead of guessing.
        return None
    return records[0]


# LLM: Presence in processing is not enough once terminalization has closed the turn. Admission
# reads this structured phase while holding the same exact-turn lock that writes it.
# 函数用途: 判断 processing 请求是否仍处于允许普通输入插入的开放阶段。
def _request_accepts_active_input(record: _GatewayRequestRecord) -> bool:
    phase = str(record.payload.get("turn_phase") or "open").strip().lower()
    status = str(record.payload.get("status") or "processing").strip().lower()
    control_status = str(record.payload.get("control_status") or "").strip().lower()
    return (
        phase == "open"
        and not bool(record.payload.get("cancel_requested"))
        and control_status != "stopping"
        and status not in {"done", "failed", "cancelled", "stopped"}
    )


def _live_window_request(
    paths: GatewayPaths,
    scope: GatewayControlScope,
) -> _GatewayRequestRecord | None:
    """Return the sole exact live turn, including a detached task's ingress turn."""
    records = _live_window_requests(paths, scope)
    return records[0] if len(records) == 1 else None


# LLM: Stop/status selection exposes all matching open turns so callers can fail closed on a
# recovery conflict instead of choosing the newest timestamp as an invented authority.
# 函数用途: 列出本用户会话所有仍开放的精确前台回合，供唯一性检查。
def _live_window_requests(
    paths: GatewayPaths,
    scope: GatewayControlScope,
) -> list[_GatewayRequestRecord]:
    return [
        record
        for record in _matching_requests(paths.processing, scope)
        if _request_accepts_active_input(record)
    ]


def _request_is_detached(record: _GatewayRequestRecord) -> bool:
    if str(record.payload.get("conversation_cancellation_scope") or "") == "detached":
        return True
    system_task = record.payload.get("system_task")
    if not isinstance(system_task, dict):
        return False
    if str(system_task.get("kind") or "") == "audit":
        return True
    attributes = system_task.get("attributes")
    return bool(
        isinstance(attributes, dict)
        and str(attributes.get("conversation_cancellation_scope") or "") == "detached"
    )


# LLM: Resolve one foreground root from canonical task links; an active Goal belongs to that root, not a second detached executor.
# 函数用途: 按当前用户会话定位可操作的主任务；普通窗口停止也要覆盖 Goal 两轮之间的等待，命名 Audit 保持独立。
def _active_conversation_task(
    base_agent: object,
    scope: GatewayControlScope,
    *,
    task_id: str = "",
    ordinary_only: bool = False,
) -> _GatewayRequestRecord | None:
    """Resolve the latest user-selectable active root task in this exact owner conversation."""
    scope_payload = _scope_request_payload(scope)
    try:
        owner_agent = _request_agent_for_scope(base_agent, scope)
        store = owner_agent.conversation_store
        thread = store.threads.resolve(
            channel=scope.channel,
            channel_conversation_id=scope.conversation_id,
            channel_user_id=scope.user_id,
        )
        if thread is None:
            return None
        links, load_errors = store.tasks.list_report(thread.thread_id)
    except Exception:
        return None
    if load_errors:
        return None
    active = _root_control_links(
        owner_agent,
        _control_active_conversation_links(store, thread.thread_id, links),
    )
    if ordinary_only:
        active = [
            link
            for link in active
            if str(getattr(link, "work_kind", "") or "").strip().lower()
            != "audit"
        ]
    if not active:
        return None
    selected = _select_active_conversation_link(
        active,
        task_id,
        preferred_task_id=str(getattr(thread, "workspace_task_id", "") or ""),
    )
    if selected is None:
        return None
    task_id = str(getattr(selected, "task_id", "") or "").strip()
    created_at = float(getattr(selected, "created_at", 0.0) or 0.0)
    payload = {
        **scope_payload,
        "id": task_id,
        "request_id": task_id,
        "goal": str(getattr(selected, "goal", "") or ""),
        "created_at": created_at,
        "lease_started_at": created_at,
        "status": "background",
        "conversation_thread_id": str(getattr(thread, "thread_id", "") or ""),
        "conversation_task_path": str(getattr(selected, "task_path", "") or ""),
        "conversation_task_link_status": str(getattr(selected, "status", "") or ""),
        "conversation_work_kind": str(getattr(selected, "work_kind", "") or ""),
        "conversation_work_name": str(getattr(selected, "work_name", "") or ""),
        "conversation_cancellation_scope": str(
            getattr(selected, "cancellation_scope", "") or "foreground"
        ),
    }
    return _GatewayRequestRecord(None, payload, "task")


def _control_active_conversation_links(
    store: object,
    thread_id: str,
    links: list[object],
) -> list[object]:
    """Resolve control-active roots from task projection plus durable execution state.

    A task link can cross to ``completed`` just before its background turn consumes
    newly queued guidance.  The exact live claim/progress policy remains the runtime
    authority during that handoff, matching 会话运行时's active-turn semantics.  An
    unreadable execution state never revives a terminal link.
    """
    from ..conversation.task_promotion import (
        conversation_thread_execution_state,
        is_reusable_conversation_workspace,
    )

    execution = conversation_thread_execution_state(store, thread_id)
    running_task_ids = execution.get("running_task_ids")
    running_task_ids = (
        {str(item) for item in running_task_ids}
        if isinstance(running_task_ids, list)
        else set()
    )
    execution_available = execution.get("state_available") is True
    candidates: list[object] = []
    for link in links:
        if not is_reusable_conversation_workspace(link):
            continue
        if str(getattr(link, "cancellation_scope", "") or "foreground") == "detached":
            continue
        status = str(getattr(link, "status", "") or "").strip().lower()
        if status == "active":
            candidates.append(link)
            continue
        if status != "completed":
            continue
        task_id = str(getattr(link, "task_id", "") or "")
        if execution_available and task_id in running_task_ids:
            candidates.append(link)
    return candidates


# LLM: Conversation active_task_ids may contain root and child projections.
# User controls target the root just as 会话运行时 user input targets the current
# root session; child turns are interrupted recursively from that root.
# 函数用途: 从会话活动任务中剔除子代理投影，避免普通 /stop 只停到某一个 child。
def _root_control_links(owner_agent: object, active: list[object]) -> list[object]:
    manager = getattr(owner_agent, "subagents", None)
    loader = getattr(manager, "load", None)
    if not callable(loader):
        return active
    roots: list[object] = []
    for link in active:
        task_id = str(getattr(link, "task_id", "") or "").strip()
        if not task_id:
            continue
        try:
            child = loader(task_id)
        except FileNotFoundError:
            roots.append(link)
            continue
        except (OSError, ValueError):
            continue
        if not str(getattr(child, "parent_id", "") or "").strip():
            roots.append(link)
    return roots


# LLM: An explicit task id wins, then the thread's canonical sticky root.  The
# timestamp fallback is only for pre-v3 records lacking workspace_task_id; no
# natural-language goal or model summary participates.
# 函数用途: 选择当前会话应被状态、插入或停止控制的唯一根任务。
def _select_active_conversation_link(
    active: list[object],
    task_id: str,
    *,
    preferred_task_id: str = "",
):
    selected_id = str(task_id or "").strip()
    if selected_id:
        return next(
            (
                link
                for link in active
                if str(getattr(link, "task_id", "") or "").strip() == selected_id
            ),
            None,
        )
    preferred_id = str(preferred_task_id or "").strip()
    if preferred_id:
        preferred = next(
            (
                link
                for link in active
                if str(getattr(link, "task_id", "") or "").strip() == preferred_id
            ),
            None,
        )
        if preferred is not None:
            return preferred
    return max(
        active,
        key=lambda link: (
            float(getattr(link, "created_at", 0.0) or 0.0),
            str(getattr(link, "task_id", "") or ""),
        ),
    )


def _linked_conversation_task_id(record: _GatewayRequestRecord | None) -> str:
    if record is None:
        return ""
    runtime = record.payload.get("conversation_runtime")
    if not isinstance(runtime, dict):
        return ""
    return str(runtime.get("task_id") or "").strip()


# LLM: Corrupt request records cannot prove ownership and are therefore excluded fail-closed.
# 函数用途:读取目录中属于当前用户会话的请求记录。
def _matching_requests(folder: Path, scope: GatewayControlScope) -> list[_GatewayRequestRecord]:
    records: list[_GatewayRequestRecord] = []
    try:
        paths = list(folder.glob("*.json"))
    except OSError:
        return records
    for path in paths:
        report = read_json_file_report(path, context="gateway.control.request.read")
        if report.load_error is not None or not report.payload:
            continue
        payload = dict(report.payload)
        if _request_matches_scope(payload, scope):
            records.append(_GatewayRequestRecord(path, payload))
    return records


# LLM: Even administrators get conversation-scoped selection unless they explicitly omit a user fact.
# 函数用途:比较请求里的结构化渠道、会话和用户身份。
def _request_matches_scope(payload: dict[str, object], scope: GatewayControlScope) -> bool:
    conversation = payload.get("conversation")
    conversation = conversation if isinstance(conversation, dict) else {}
    metadata = payload.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    request_channel = str(conversation.get("channel") or metadata.get("channel") or "").strip()
    request_conversation = str(conversation.get("channel_conversation_id") or "").strip()
    request_user = str(
        payload.get("user_id")
        or conversation.get("canonical_user_id")
        or conversation.get("channel_user_id")
        or metadata.get("user_id")
        or ""
    ).strip()
    if scope.channel and request_channel != scope.channel:
        return False
    if scope.conversation_id and request_conversation != scope.conversation_id:
        return False
    if scope.user_id and request_user != scope.user_id:
        return False
    return bool(scope.conversation_id or (scope.all_user_access and scope.user_id))


# LLM: 插话持久操作经 guidance 领域组件； Steering targets exactly one active request or durable root task, never a later chat turn.
# 函数用途:把用户补充写入当前 owner 的当前任务引导收件箱。
def _steer_active_request(
    base_agent: object,
    paths: GatewayPaths,
    active: _GatewayRequestRecord,
    command: ConversationControlCommand,
    scope: GatewayControlScope,
) -> ConversationControlResult:
    request_id = _record_id(active)
    target_type = "task" if active.target_kind == "task" else "request"
    turn_id = _active_turn_request_id(active)
    try:
        with gateway_turn_transition(paths, turn_id):
            expected_turn_id = _steer_expected_turn_id(scope)
            if expected_turn_id and expected_turn_id != turn_id:
                return ConversationControlResult(
                    "steer",
                    False,
                    "目标回合已经结束或已切换，补充消息未进入其他任务。",
                    request_id=turn_id,
                    delivery_status="rejected",
                )
            linked_state = (
                _linked_request_target_state(active.linked_request, request_id)
                if target_type == "task" and active.linked_request is not None
                else ""
            )
            live_record = (
                active.linked_request
                if target_type == "task" and linked_state == "current"
                else active
                if target_type == "request"
                else None
            )
            if live_record is not None:
                fresh_payload = _load_open_exact_turn(paths, live_record, scope, turn_id)
                if fresh_payload is None:
                    return ConversationControlResult(
                        "steer",
                        False,
                        "当前任务刚刚结束或正在停止，补充要求未应用到下一任务。",
                        request_id=turn_id,
                        delivery_status="rejected",
                    )
                if target_type == "request":
                    active = _GatewayRequestRecord(
                        live_record.path,
                        fresh_payload,
                        live_record.target_kind,
                        live_record.linked_request,
                    )
            owner_agent = _request_agent_for_scope(base_agent, scope)
            store = owner_agent.conversation_store
            thread_id = _control_thread_id(store, active, scope)
            if not thread_id:
                raise ValueError("conversation thread is unavailable")
            with store.guidance.ledger.turn_guard(turn_id):
                if target_type == "task":
                    with store.tasks.transition_guard(request_id):
                        if not _durable_task_is_current(owner_agent, active):
                            return ConversationControlResult(
                                "steer",
                                False,
                                "当前任务刚刚结束或已切换，补充要求未应用到下一任务。",
                                request_id=request_id,
                                delivery_status="rejected",
                            )
                        entry = _append_control_guidance(
                            store,
                            target_type,
                            request_id,
                            command,
                            scope,
                            thread_id,
                            turn_id,
                        )
                        if not _durable_task_is_current(owner_agent, active):
                            winner = _mark_control_guidance_result(store, entry, "rejected")
                            return _control_result_for_guidance_winner(winner, request_id)
                else:
                    entry = _append_control_guidance(
                        store,
                        target_type,
                        request_id,
                        command,
                        scope,
                        thread_id,
                        turn_id,
                    )
    except Exception:
        return ConversationControlResult(
            "steer",
            False,
            "当前任务的补充通道状态暂时无法确认；系统会继续使用同一消息 ID 对账。",
            request_id=turn_id,
            delivery_status="unknown",
        )
    if target_type == "task" and (
        active.linked_request is None
        or _linked_request_target_state(active.linked_request, request_id) == "retired"
    ):
        _wake_for_task_guidance(owner_agent, active, entry.guidance_id, scope)
    return ConversationControlResult(
        "steer",
        True,
        "补充消息已进入精确回合的待认领队列，正在等待下一个模型安全点确认。",
        request_id=turn_id,
        delivery_status="unknown",
        guidance_dedupe_key=str(entry.metadata.get("dedupe_key") or ""),
    )


# LLM: Admission must re-read the selected processing record while holding its exact-turn lock.
# Stale preselection, owner drift, stop, close, and expected-turn switches all fail before append.
# 函数用途: 在插入补充消息前重新确认同一个请求仍开放且仍属于当前用户会话。
def _load_open_exact_turn(
    paths: GatewayPaths,
    record: _GatewayRequestRecord,
    scope: GatewayControlScope,
    expected_turn_id: str,
) -> dict[str, object] | None:
    if (paths.terminal / f"{expected_turn_id}.json").exists():
        return None
    if record.path is None or not record.path.exists():
        return None
    report = read_json_file_report(record.path, context="gateway.control.exact_turn.read")
    if report.load_error is not None or not report.payload:
        return None
    payload = dict(report.payload)
    request_id = str(payload.get("id") or record.path.stem).strip()
    if request_id != str(expected_turn_id or "").strip():
        return None
    fresh = _GatewayRequestRecord(record.path, payload)
    if not _request_matches_scope(payload, scope) or not _request_accepts_active_input(fresh):
        return None
    return payload


# LLM: Gateway 控制入口以明确目标和回合写入 guidance；稳定消息 ID 决定幂等路径，联测重复控制请求。
# 函数用途: 构造补充消息及宿主元数据，写入原插话回执和队列。
def _append_control_guidance(
    store: object,
    target_type: str,
    request_id: str,
    command: ConversationControlCommand,
    scope: GatewayControlScope,
    thread_id: str,
    turn_id: str,
):
    channel_message_id = _steer_channel_message_id(scope)
    payload = {
        "target_type": target_type,
        "target_id": request_id,
        "message": command.value,
        "sender": scope.user_id,
        "priority": "high",
        "delivery": "current_task" if target_type == "task" else "current_request",
        "metadata": {
            "kind": "active_turn_user_input",
            "record_in_transcript": True,
            "thread_id": thread_id,
            "channel": scope.channel,
            "conversation_id": scope.conversation_id,
            "channel_message_id": channel_message_id,
            "expected_turn_id": str(turn_id or "").strip(),
            "gateway_input_request_id": str(
                scope.metadata.get("gateway_input_request_id") or ""
            ).strip(),
        },
    }
    if channel_message_id:
        return store.guidance.append_once(
            payload,
            dedupe_key=active_turn_guidance_dedupe_key(scope),
        )
    return store.guidance.append(payload)


# LLM: 插话持久操作经 guidance 领域组件； The Gateway writes a terminal receipt before returning or retiring the guidance row.
# Missing dedupe metadata denotes an internal non-idempotent caller; rejection must still retire its
# legacy queue row so a later task cannot consume input that lost the exact-task race.
# 函数用途: 将已写入的补充消息回执确定为接收或拒绝。
def _mark_control_guidance_result(store: object, entry: GuidanceEntry, status: str):
    metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
    dedupe_key = str(metadata.get("dedupe_key") or "").strip()
    if dedupe_key:
        return store.guidance.mark_status(dedupe_key, status)
    if status == "rejected":
        store.guidance.ledger.mark_delivered([entry.guidance_id])
    return None


# LLM: A concurrent runtime claim may beat rejection. Callers project the returned durable winner
# instead of assuming their requested transition succeeded.
# 函数用途: 把补充消息状态竞争的真实结果转换成控制响应。
def _control_result_for_guidance_winner(winner: object, request_id: str) -> ConversationControlResult:
    status = str(getattr(winner, "status", "rejected") or "rejected")
    dedupe_key = str(getattr(winner, "dedupe_key", "") or "")
    if status == "consumed":
        return ConversationControlResult(
            "steer",
            True,
            "该补充消息已由当前回合接收。",
            request_id=request_id,
            delivery_status="accepted",
            guidance_dedupe_key=dedupe_key,
        )
    if status in {"reserved", "submitted"}:
        return ConversationControlResult(
            "steer",
            True,
            "补充消息可能已经进入模型请求，当前只能继续按同一消息 ID 对账。",
            request_id=request_id,
            delivery_status="unknown",
            guidance_dedupe_key=dedupe_key,
        )
    return ConversationControlResult(
        "steer",
        False,
        "目标回合已结束或切换，补充消息未进入其他任务。",
        request_id=request_id,
        delivery_status="rejected",
        guidance_dedupe_key=dedupe_key,
    )


# LLM: 插话持久操作经 guidance 领域组件； Existing guidance is bound to its receipt's exact turn, never the current active projection.
# T and the store mailbox guard stay held through terminal rejection, matching provider admission's
# T-to-M lock order; missing or corrupt lifecycle evidence remains UNKNOWN instead of losing input.
# 函数用途: 按回执首次绑定的回合对账补充消息，只在锁内证明该回合终态时拒绝尚未认领的记录。
def _reconcile_existing_steer_receipt(
    paths: GatewayPaths,
    scope: GatewayControlScope,
    receipt: _SteerReceiptState,
    expected_turn_id: str,
) -> ConversationControlResult:
    turn_id = str(expected_turn_id or "").strip()
    if not turn_id:
        return _unknown_existing_steer_result(receipt, "")
    with gateway_turn_transition(paths, turn_id):
        lifecycle = _exact_gateway_turn_lifecycle_locked(paths, scope, turn_id)
        if lifecycle != "terminal":
            return _unknown_existing_steer_result(receipt, turn_id)
        with receipt.store.guidance.ledger.turn_guard(turn_id):
            winner = receipt.store.guidance.mark_status(
                receipt.dedupe_key,
                "rejected",
            )
    return _control_result_from_steer_receipt(
        _SteerReceiptState(
            store=receipt.store,
            dedupe_key=receipt.dedupe_key,
            status=winner.status,
            entry=winner.entry,
        )
    )


# LLM: This reader runs only while the exact Gateway turn transition lock is held. Canonical or
# closed hot facts prove terminal; open processing and validated inbox facts remain live/recoverable;
# absence, owner mismatch, and damaged JSON are UNKNOWN rather than guessed terminal.
# 函数用途: 在精确回合锁内判定活动、待恢复、终态或证据不足。
def _exact_gateway_turn_lifecycle_locked(
    paths: GatewayPaths,
    scope: GatewayControlScope,
    turn_id: str,
) -> str:
    terminal_path = paths.terminal / f"{turn_id}.json"
    if terminal_path.is_file():
        return _validated_exact_turn_file_state(terminal_path, scope, turn_id, "terminal")
    processing_path = paths.processing / f"{turn_id}.json"
    if processing_path.is_file():
        report = read_json_file_report(
            processing_path,
            context="gateway.control.receipt_processing.read",
        )
        if report.load_error is not None or not report.payload:
            return "unknown"
        record = _GatewayRequestRecord(processing_path, dict(report.payload))
        if _record_id(record) != turn_id or not _request_matches_scope(record.payload, scope):
            return "unknown"
        return "active" if _request_accepts_active_input(record) else "terminal"
    inbox_path = paths.inbox / f"{turn_id}.json"
    if inbox_path.is_file():
        return _validated_exact_turn_file_state(inbox_path, scope, turn_id, "recoverable")
    for folder in (paths.done, paths.failed):
        archive_path = folder / f"{turn_id}.json"
        if archive_path.is_file():
            return _validated_exact_turn_file_state(archive_path, scope, turn_id, "terminal")
    return "unknown"


# LLM: Exact lifecycle files must be readable, identify the requested turn, and retain the same
# authenticated scope before they can drive receipt state.
# 函数用途: 校验一个精确请求文件，并在身份一致时返回指定生命周期。
def _validated_exact_turn_file_state(
    path: Path,
    scope: GatewayControlScope,
    turn_id: str,
    state: str,
) -> str:
    report = read_json_file_report(path, context="gateway.control.receipt_lifecycle.read")
    if report.load_error is not None or not report.payload:
        return "unknown"
    record = _GatewayRequestRecord(path, dict(report.payload))
    if _record_id(record) != turn_id or not _request_matches_scope(record.payload, scope):
        return "unknown"
    return state


# LLM: UNKNOWN keeps the original exact target and dedupe key so HTTP/TUI reconciliation can retry
# the same receipt without rerouting to a later active turn.
# 函数用途: 构造已有补充消息仍待精确回合证据的统一响应。
def _unknown_existing_steer_result(
    receipt: _SteerReceiptState,
    request_id: str,
) -> ConversationControlResult:
    return ConversationControlResult(
        "steer",
        False,
        "补充消息仍在按首次绑定的精确回合确认；请使用同一消息 ID 继续对账。",
        request_id=request_id,
        delivery_status="unknown",
        guidance_dedupe_key=receipt.dedupe_key,
    )


def _control_thread_id(
    store: object,
    active: _GatewayRequestRecord,
    scope: GatewayControlScope,
) -> str:
    direct = str(active.payload.get("conversation_thread_id") or "").strip()
    if direct:
        return direct
    runtime = active.payload.get("conversation_runtime")
    if isinstance(runtime, dict):
        direct = str(runtime.get("thread_id") or "").strip()
        if direct:
            return direct
    thread = store.threads.resolve(
        channel=scope.channel,
        channel_conversation_id=scope.conversation_id,
        channel_user_id=scope.user_id,
    )
    if thread is None:
        thread = store.threads.get_or_create(
            {
                "canonical_user_id": scope.user_id,
                "channel": scope.channel,
                "channel_conversation_id": scope.conversation_id,
                "channel_user_id": scope.user_id,
                "title": "当前会话",
            }
        )
    return str(getattr(thread, "thread_id", "") or "").strip()


def _durable_task_is_current(owner_agent: object, active: _GatewayRequestRecord) -> bool:
    """会话运行时 expected-turn check: the selected task must still be current.

    The initial lookup and durable append cannot share one filesystem lock.  Re-resolve
    the latest user-selectable active root after the append; if another task became
    current, retire this steer instead of applying it to either task.
    """
    task_id = _record_id(active)
    if active.linked_request is not None:
        linked_state = _linked_request_target_state(active.linked_request, task_id)
        if linked_state == "current":
            return True
        if linked_state != "retired":
            return False
        # The foreground request file is atomically moved to done before the
        # durable continuation owns the next turn.  Keep the same TaskRun
        # steerable through that handoff, while the active-link check below
        # still rejects a real task switch.
    thread_id = str(active.payload.get("conversation_thread_id") or "").strip()
    try:
        links, load_errors = owner_agent.conversation_store.tasks.list_report(thread_id)
    except Exception:
        return False
    if load_errors:
        return False
    candidates = _control_active_conversation_links(
        owner_agent.conversation_store,
        thread_id,
        links,
    )
    if not candidates:
        return False
    current = max(
        candidates,
        key=lambda link: (
            float(getattr(link, "created_at", 0.0) or 0.0),
            str(getattr(link, "task_id", "") or ""),
        ),
    )
    return str(getattr(current, "task_id", "") or "").strip() == task_id


def _linked_request_target_state(linked: _GatewayRequestRecord, task_id: str) -> str:
    """Classify a claimed foreground turn without conflating handoff with corruption."""
    if linked.path is None:
        return "unavailable"
    if not linked.path.exists():
        return "retired"
    report = read_json_file_report(linked.path, context="gateway.control.linked_request.read")
    if report.load_error is not None or not report.payload:
        return "retired" if not linked.path.exists() else "unavailable"
    current = _GatewayRequestRecord(linked.path, dict(report.payload))
    matches = (
        _record_id(current) == _record_id(linked)
        and _linked_conversation_task_id(current) == task_id
        and _request_accepts_active_input(current)
    )
    return "current" if matches else "mismatch"


# LLM: Publish a wake only when no linked live execution turn exists; live turns consume the durable FIFO guidance in place.
# 函数用途:只唤醒当前没有执行线的持久任务，避免 `/btw` 为同一任务启动第二个主执行器。
def _wake_for_task_guidance(
    owner_agent: object,
    active: _GatewayRequestRecord,
    guidance_id: str,
    scope: GatewayControlScope,
) -> None:
    """Wake an idle durable root; a linked live turn consumes guidance in-place."""
    try:
        from ..conversation.task_promotion import conversation_task_execution_state

        thread_id = str(active.payload.get("conversation_thread_id") or "")
        execution = conversation_task_execution_state(
            owner_agent.conversation_store,
            thread_id,
            _record_id(active),
        )
        if execution.get("state_available") is not True:
            return
        if execution.get("running") is True:
            return
        reason = "user_guidance"
        metadata = {
            "guidance_id": guidance_id,
            "channel": scope.channel,
            "conversation_id": scope.conversation_id,
        }
        goal = owner_agent.conversation_store.goals.load(
            thread_id,
            task_id=_record_id(active),
        )
        if (
            goal is not None
            and goal.status == "active"
            and goal.task_id == _record_id(active)
        ):
            reason = "thread_goal_continue"
            metadata["goal_id"] = goal.goal_id
        owner_agent.conversation_store.wakes.raise_signal(
            {
                "thread_id": thread_id,
                "root_task_id": _record_id(active),
                "urgency": "urgent",
                "reason": reason,
                "summary": "用户补充了当前任务要求。",
                "dedupe_key": f"guidance:{guidance_id}",
                "metadata": metadata,
            }
        )
    except Exception:
        return


# LLM: T 内关闭请求并读取新鲜绑定，释放 T 后才取 task guard；无持久链接也必须沿正式绑定关闭主执行权。
# 函数用途: 中断当前窗口执行链，明确 stop 时按实际主链冻结资源；缺失身份保留未知，不猜 run/attempt。
def _stop_live_window_request(
    base_agent: object,
    paths: GatewayPaths,
    live_request: _GatewayRequestRecord,
    scope: GatewayControlScope,
    *,
    interrupt_only: bool = False,
) -> ConversationControlResult:
    request_id = _record_id(live_request)
    try:
        owner_agent = _request_agent_for_scope(base_agent, scope)
        store = owner_agent.conversation_store
    except Exception:
        owner_agent = None
        store = None
    binding, binding_error = {}, ""
    with gateway_turn_transition(paths, request_id):
        marked, failure_message = _mark_request_stopping_locked(live_request, scope)
        if marked:
            try:
                from .request_binding import gateway_runtime_authority

                report = read_json_file_report(live_request.path)
                if report.load_error or not report.payload:
                    raise ValueError("停止后的请求身份不可读")
                live_request = replace(live_request, payload=report.payload)
                binding = gateway_runtime_authority(report.payload, request_id)
                if store is not None:
                    store.guidance.recovery.reject_pending(request_id)
            except (OSError, RuntimeError, TypeError, ValueError):
                binding_error = "request_binding_unconfirmed"
    if not marked:
        return ConversationControlResult(
            "stop",
            False,
            failure_message or "当前执行刚刚结束，无需停止。",
            request_id=request_id,
        )
    interrupt_by_name(conversation_request_interrupt_name(request_id))

    linked_task_id = _linked_conversation_task_id(live_request)
    if _request_is_detached(live_request):
        return ConversationControlResult(
            "stop",
            True,
            "已停止当前会话正在执行的内容。",
            request_id=request_id,
        )
    durable = (
        _linked_active_task_record(base_agent, live_request, scope, linked_task_id)
        if linked_task_id
        else None
    )
    if durable is not None:
        result = _stop_active_task(
            base_agent,
            durable,
            scope,
            linked_request_already_stopped=True,
            interrupt_only=interrupt_only,
        )
        if interrupt_only or not result.ok:
            return result
    else:
        if not interrupt_only:
            return _stop_unpromoted_main(owner_agent, live_request, scope, binding, binding_error)
    return ConversationControlResult(
        "stop",
        True,
        "已中断本轮执行链。" if interrupt_only else "已受理停止请求，正在清理本任务资源。",
        request_id=request_id,
    )


# LLM: 释放 T 后只用冻结四元组；task guard 内再次排除已晋升任务及 Goal，未知不能降级为空，也不能猜线程。
# 函数用途: 为尚未晋升持久任务的请求关闭实际主执行权并冻结资源，清理失败与中断受理分别汇报。
def _stop_unpromoted_main(
    owner_agent: object,
    active: _GatewayRequestRecord,
    scope: GatewayControlScope,
    binding: dict,
    binding_error: str,
) -> ConversationControlResult:
    request_id = _record_id(active)
    try:
        if owner_agent is None or binding_error or not binding or _linked_conversation_task_id(active):
            raise ValueError("本请求缺少可确认的主执行身份")
        store = owner_agent.conversation_store
        with store.tasks.transition_guard(binding["task_id"]), owner_agent.subagents.creation_guard():
            from ..conversation.task_resources import (
                close_main_task_authority,
                freeze_task_resources,
            )

            thread = store.threads.resolve(
                channel=scope.channel, channel_conversation_id=scope.conversation_id, channel_user_id=scope.user_id,
            )
            if thread is None:
                raise ValueError("停止请求的会话身份无法确认")
            if store.tasks.load(binding["task_id"]) is not None:
                raise ValueError("任务已晋升，不能按无持久任务停止")
            if store.goals.load(thread.thread_id, task_id=binding["task_id"]) is not None:
                raise ValueError("任务已有持续目标，不能跳过目标控制")
            selected = close_main_task_authority(
                getattr(getattr(owner_agent, "subagents", None), "runtime_db", None),
                owner_home=getattr(getattr(owner_agent, "home_paths", None), "owner_home_dir", ""),
                task_id=binding["task_id"], thread_id=thread.thread_id, binding=binding,
            )
            if selected is None:
                raise ValueError("主执行链未受管理，资源归属无法确认")
            resources = freeze_task_resources(
                owner_agent, selected, request_id, related_request_ids=(binding["task_id"],),
            )
        _cancel_request_execution(request_id, resources=resources)
        if resources.unconfirmed:
            raise ValueError("主资源冻结存在未确认项")
    except (OSError, RuntimeError, TypeError, ValueError, KeyError):
        return ConversationControlResult(
            "stop", False, "本轮中断已受理；任务资源停止尚未确认。", request_id=request_id,
            delivery_status="unknown", error_code="TASK_RESOURCE_STOP_UNCONFIRMED",
        )
    return ConversationControlResult(
        "stop", True, "已受理停止请求，正在清理本任务资源。", request_id=request_id,
        delivery_status="accepted",
    )


# LLM: Every stop mutation shares the exact Gateway T lock with steer, provider admission, ACK,
# and terminal closeout. Callers already holding T must use the locked helper below.
# 函数用途: 为任意精确热请求获取回合锁，再安全保存停止状态。
def _mark_request_stopping(
    active: _GatewayRequestRecord,
    scope: GatewayControlScope,
) -> tuple[bool, str]:
    if active.path is None:
        return False, "当前任务刚刚结束，无需停止。"
    processing_dir = active.path.parent
    request_root = processing_dir.parent
    root = request_root.parent if request_root.name == "requests" else request_root
    paths = gateway_paths_from_root(root)
    with gateway_turn_transition(paths, _record_id(active)):
        return _mark_request_stopping_locked(active, scope)


# LLM: 在 T 内核对同请求、同运输代次及开放状态；只允许正式绑定在同代次更新，不停止换代后的新执行。
# 函数用途: 在已持回合锁时二次校验并写入 closing/cancel 事实。
def _mark_request_stopping_locked(
    active: _GatewayRequestRecord,
    scope: GatewayControlScope,
) -> tuple[bool, str]:
    request_id = _record_id(active)
    updated_ref = [False]

    def mark_cancel(current: dict) -> dict:
        path_stem = active.path.stem if active.path is not None else ""
        if str(current.get("id") or path_stem) != request_id:
            return current
        if str(current.get("execution_attempt_id") or "") != str(active.payload.get("execution_attempt_id") or ""):
            return current
        fresh = _GatewayRequestRecord(active.path, dict(current))
        if not _request_matches_scope(current, scope) or not _request_accepts_active_input(fresh):
            return current
        now = time.time()
        current.update(
            {
                "cancel_requested": True,
                "cancel_requested_at": now,
                "cancel_requested_by": scope.user_id,
                "control_status": "stopping",
                "turn_phase": "closing",
                "updated_at": now,
            }
        )
        updated_ref[0] = True
        return current

    try:
        if active.path is None:
            raise FileNotFoundError(request_id)
        update_json_file_atomic(active.path, mark_cancel, require_existing=True)
    except FileNotFoundError:
        return False, "当前任务刚刚结束，无需停止。"
    except OSError:
        return False, "停止请求暂时无法保存，请稍后重试。"
    if not updated_ref[0]:
        return False, "当前任务刚刚结束，无需停止。"
    return True, ""


# LLM: interrupt 不改变任何 Goal/任务状态或资源；仅已中断的 active Goal 可沿同一 wake/claim 续跑。
# 函数用途: 将当前回合中断与明确任务停止分支隔离，旧执行退出前不另起执行器。
def _stop_active_task(
    base_agent: object,
    active: _GatewayRequestRecord,
    scope: GatewayControlScope,
    *,
    linked_request_already_stopped: bool = False,
    interrupt_only: bool = False,
) -> ConversationControlResult:
    task_id = _record_id(active)
    linked_request_id = _record_id(active.linked_request)
    if linked_request_already_stopped:
        linked_marked = bool(linked_request_id)
    else:
        linked_request_id, linked_marked = _stop_linked_live_request(active, scope)
    if interrupt_only:
        task_interrupted = interrupt_by_name(conversation_request_interrupt_name(task_id))
        if linked_request_id:
            interrupt_by_name(conversation_request_interrupt_name(linked_request_id))
        if not (task_interrupted or linked_marked):
            return ConversationControlResult("stop", False, "当前没有执行中的回合，无需中断。", request_id=task_id)
        continuing = _continue_goal_after_turn_interrupt(base_agent, active, scope)
        return ConversationControlResult(
            "stop", True,
            ("已中断本轮；目标仍在进行，当前执行退出后继续。/goal pause 只暂停自动续跑，/stop 停止任务资源。"
             if continuing else "已中断当前回合；目标状态和已启动的独立任务资源保留。"),
            request_id=task_id,
        )
    try:
        owner_agent = _request_agent_for_scope(base_agent, scope)
        prepared = _prepare_main_task_stop(owner_agent, active, task_id, scope)
    except Exception:
        if linked_marked:
            interrupt_by_name(conversation_request_interrupt_name(linked_request_id))
        return ConversationControlResult(
            "stop",
            False,
            "本轮已请求中断，但任务资源停止结果尚未确认。",
            request_id=task_id,
            delivery_status="unknown",
            error_code="TASK_RESOURCE_STOP_UNCONFIRMED",
        )
    if prepared.task_link is None:
        return ConversationControlResult("stop", False, "任务状态已经变化，未停止新的执行轮。", request_id=task_id)
    _interrupt_task_registry_record(owner_agent, task_id)
    if linked_request_id:
        interrupt_by_name(conversation_request_interrupt_name(linked_request_id))
    _cancel_request_execution(
        task_id, resources=prepared.resources,
    )
    if prepared.preparation_errors or (prepared.resources is not None and prepared.resources.unconfirmed):
        return ConversationControlResult(
            "stop", False, "原执行轮已关闭，部分资源停止仍未确认；已选中的资源继续清理。",
            request_id=task_id, delivery_status="unknown", error_code="TASK_RESOURCE_STOP_UNCONFIRMED",
        )
    return ConversationControlResult(
        "stop",
        True,
        "已收到停止请求，当前任务正在停止。",
        request_id=task_id,
    )


# LLM: 仅精确 active Goal 可保留执行授权；原 claim 未释放时 wake 不能运行，暂停/预算/未知状态不会自动恢复。
# 函数用途: Esc 后保留原目标和任务，并发布一个去重续接事件；不取消仍独立工作的子代理。
def _continue_goal_after_turn_interrupt(
    base_agent: object, active: _GatewayRequestRecord, scope: GatewayControlScope,
) -> bool:
    from ..conversation.goal_runtime import raise_goal_continuation_wake

    store = _request_agent_for_scope(base_agent, scope).conversation_store
    task_id = _record_id(active)
    thread_id = str(active.payload.get("conversation_thread_id") or "")
    with store.goals.transition_guard(thread_id):
        goal = store.goals.load(thread_id, task_id=task_id)
        if goal is None or goal.task_id != task_id or goal.status != "active":
            return False
        raise_goal_continuation_wake(
            store, goal, channel=scope.channel, conversation_id=scope.conversation_id,
        )
        return True


# LLM: 沿主 Goal→task 短读 Gateway T，释放 T 后进入 creation→子 Goal；精确关闭权限并固定树，不在锁内等进程退出。
# 函数用途: 关闭主任务准入、冻结资源并暂停续跑；清理在所有控制锁之外，部分错误不丢清单。
def _prepare_main_task_stop(
    owner_agent: object,
    active: _GatewayRequestRecord,
    task_id: str,
    scope: GatewayControlScope,
) -> _MainTaskStop:
    store = owner_agent.conversation_store
    expected_status = str(
        active.payload.get("conversation_task_link_status") or "active"
    ).strip().lower()
    thread_id = str(active.payload.get("conversation_thread_id") or "")
    preparation_errors = []
    with store.goals.transition_guard(thread_id):
        with store.tasks.transition_guard(task_id):
            current = store.tasks.load(task_id)
            if current is None or current.status != expected_status or current.thread_id != thread_id:
                return _MainTaskStop()
            from ..conversation.task_resources import (
                close_main_task_authority,
                freeze_task_resources,
            )

            binding = _fresh_stop_runtime_binding(active.linked_request, scope)
            with owner_agent.subagents.creation_guard():
                resource_scope = close_main_task_authority(
                    getattr(getattr(owner_agent, "subagents", None), "runtime_db", None),
                    owner_home=getattr(getattr(owner_agent, "home_paths", None), "owner_home_dir", ""),
                    task_id=task_id, thread_id=thread_id,
                    binding=binding,
                )
                interrupt_by_name(conversation_request_interrupt_name(task_id))
                stopped = store.tasks.update_status(
                    {"task_id": task_id, "status": "interrupted", "expected_status": expected_status}
                )
                if stopped is None:
                    raise ValueError("停止时任务状态发生冲突")
                resources = freeze_task_resources(
                    owner_agent, resource_scope, task_id, related_request_ids=(_record_id(active.linked_request),),
                )
                if resource_scope is None:
                    preparation_errors.extend(_request_unmanaged_pty_stop(owner_agent, thread_id, task_id))
        try:
            _pause_goal_for_stopped_task(store, stopped)
        except (OSError, RuntimeError, ValueError) as exc:
            preparation_errors.append(f"goal_pause:{type(exc).__name__}")
    return _MainTaskStop(stopped, resources, tuple(preparation_errors))


# LLM: 无受管主链时保留原精确 task/thread PTY 入口；异常只能标记未确认，不能丢弃已冻结的孩子清单。
# 函数用途: 请求回收旧本地模式的原终端，把失败交回停止应答，后续清理仍继续。
def _request_unmanaged_pty_stop(owner_agent: object, thread_id: str, task_id: str) -> tuple[str, ...]:
    from ..tooling.pty_sessions import pty_session_registry

    try:
        pty_session_registry.request_stop(
            owner_home=getattr(getattr(owner_agent, "home_paths", None), "owner_home_dir", ""),
            thread_id=thread_id, task_id=task_id,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return (f"pty_stop:{type(exc).__name__}",)
    return ()


# LLM: 调用方已持任务锁；请求锁只短读本请求和原运输代次，不从过时 discovery payload 取得执行权。
# 函数用途: 读取被停止热请求的正式运行绑定，无热请求时交由唯一持久主链定位。
def _fresh_stop_runtime_binding(
    record: _GatewayRequestRecord | None, scope: GatewayControlScope,
) -> dict | None:
    from .request_binding import gateway_runtime_authority

    if record is None:
        return None
    if record.path is None:
        raise ValueError("停止请求缺少持久地址")
    request_id = _record_id(record)
    request_root = record.path.parent.parent
    root = request_root.parent if request_root.name == "requests" else request_root
    with gateway_turn_transition(gateway_paths_from_root(root), request_id):
        report = read_json_file_report(record.path)
        if (report.load_error or not _request_matches_scope(report.payload, scope)
                or _record_id(_GatewayRequestRecord(record.path, report.payload)) != request_id
                or str(report.payload.get("execution_attempt_id") or "") != str(record.payload.get("execution_attempt_id") or "")):
            raise ValueError("停止请求的持久身份无法确认")
        return gateway_runtime_authority(report.payload, request_id)


def _stop_linked_live_request(
    active: _GatewayRequestRecord,
    scope: GatewayControlScope,
) -> tuple[str, bool]:
    linked = active.linked_request
    linked_request_id = _record_id(linked)
    if linked is None:
        return linked_request_id, False
    marked, _failure = _mark_request_stopping(linked, scope)
    return linked_request_id, marked


# LLM: 调用者已持 Goal 锁且释放任务锁；只暂停精确绑定的 active Goal，错误须交还控制回执。
# 函数用途: `/stop` 完成资源冻结后暂停同一目标，保持与显式恢复的先后顺序。
def _pause_goal_for_stopped_task(store: object, task_link: object) -> None:
    thread_id = str(getattr(task_link, "thread_id", "") or "")
    task_id = str(getattr(task_link, "task_id", "") or "")
    goal = store.goals.load(thread_id, task_id=task_id)
    if goal is None or goal.status != "active" or goal.task_id != task_id:
        return
    updated = store.goals.update(
        {"thread_id": thread_id, "goal_id": goal.goal_id, "status": "paused", "expected_status": "active"}
    )
    if updated is None:
        raise ValueError("停止时目标状态发生冲突")


# LLM: The global task projection records an interrupt as resumable, never as terminal cancellation.
# 函数用途:同步任务索引中的中断状态，后续明确续接时可以恢复为 running。
def _interrupt_task_registry_record(owner_agent: object, task_id: str) -> None:
    try:
        registry = owner_agent.local_store.task_registry
        current = registry.lookup_task(task_id)
        status = str((current or {}).get("status") or "").strip()
        if status:
            registry.update_task_status(task_id, "interrupted", expected_status=status)
    except Exception:
        return


# LLM: 异步 worker 不再接收 agent 或任何查询选择器；只能消费在原控制锁内准备好的整树资源。
# 函数用途: 锁外清理原主任务及其原孩子，恢复后新建的任务不会被旧停止请求纳入。
def _cancel_request_execution(request_id: str, *, resources: object) -> None:
    from ..conversation.task_resources import cleanup_task_resources

    threading.Thread(
        target=cleanup_task_resources, args=(resources,),
        name=f"cancel-{request_id}", daemon=True,
    ).start()


# LLM: 状态仅投影当前请求事实；空闲时读同 owner 的当前模型选择，不把 Gateway 启动占位配置当真实模型。
# 函数用途:汇总当前请求、排队数、最近阶段、子代理和压缩状态；读取模型设置不发请求或写配置。
def _gateway_task_status(
    base_agent: object,
    paths: GatewayPaths,
    scope: GatewayControlScope,
    active: _GatewayRequestRecord | None,
) -> ConversationTaskStatus:
    queued = [
        record
        for record in _matching_requests(paths.inbox, scope)
        if not _request_is_detached(record)
    ]
    selected = active or (min(queued, key=lambda item: _request_timestamp(item.payload)) if queued else None)
    live_request = active.linked_request if active is not None else None
    display_selected = live_request or selected
    payload = display_selected.payload if display_selected is not None else _scope_request_payload(scope)
    if display_selected is None and scope.resolved_owner is not None:
        try:
            owner_agent = _request_agent_for_scope(base_agent, scope)
        except Exception:
            owner_agent = base_agent
    else:
        owner_agent = _request_agent_or_base(base_agent, payload)
    compact_generation, verbose_level = _conversation_profile(owner_agent, payload)
    subagents = (
        _subagent_status(owner_agent, [_record_id(active), _record_id(live_request)])
        if active is not None
        else (0, 0, 0, 0)
    )
    state = "idle"
    if active is not None:
        if bool(payload.get("cancel_requested")):
            state = "stopping"
        elif _control_record_is_executing(owner_agent, active, subagents):
            state = "running"
        elif queued:
            state = "queued"
    elif queued:
        state = "queued"
    is_executing = state in {"running", "stopping"}
    started_at = _request_started_at(payload) if is_executing else 0.0
    progress_request_id = _record_id(live_request or active)
    return ConversationTaskStatus(
        state=state,
        task=_request_prompt(display_selected.payload) if display_selected is not None else "",
        elapsed_seconds=max(0.0, time.time() - started_at) if started_at else 0.0,
        queued_count=len(queued),
        recent_progress=_recent_progress(paths, progress_request_id) if is_executing else "",
        subagent_total=subagents[0],
        subagent_running=subagents[1],
        subagent_done=subagents[2],
        subagent_failed=subagents[3],
        model_name=_status_model_name(owner_agent, idle=display_selected is None, scope=scope),
        compact_generation=compact_generation,
        verbose_level=verbose_level,
        durable_work=_named_durable_statuses(owner_agent, paths, scope),
    )


# LLM: 活跃工作片只读实际冻结模型名投影；空闲读取 canonical thread 选择，未绑定才读未来默认；不初始化 backend。
# 函数用途: /status 区分“本轮仍使用原模型”和“下一轮已选择新模型”，不会串到其他会话或暴露密钥。
def _status_model_name(owner_agent: object, *, idle: bool, scope: GatewayControlScope | None = None) -> str:
    config = getattr(owner_agent, "config", None)
    if getattr(owner_agent, "home_paths", None) is not None:
        from ..settings.model_profiles import selected_model_config
        from ..settings.model_scope import active_thread_model_name
        from ..settings.thread_model_selection import thread_model_config

        try:
            store = getattr(owner_agent, "conversation_store", None)
            thread = _conversation_thread_for_scope(store, scope) if store is not None and scope is not None else None
            if thread is not None:
                live_name = active_thread_model_name(owner_agent, thread.thread_id) if not idle else ""
                if live_name:
                    return live_name
                config = thread_model_config(owner_agent, thread.thread_id)
            elif idle:
                config = selected_model_config(owner_agent)
        except (ValueError, OSError, TypeError):
            return "当前模型配置不可用"
    return str(getattr(config, "model_name", "") or "")


def _named_durable_statuses(
    owner_agent: object,
    paths: GatewayPaths,
    scope: GatewayControlScope,
) -> tuple[NamedConversationWorkStatus, ...]:
    try:
        store = owner_agent.conversation_store
        thread = store.threads.resolve(
            channel=scope.channel,
            channel_conversation_id=scope.conversation_id,
            channel_user_id=scope.user_id,
        )
        if thread is None:
            links, goals = [], []
        else:
            links, link_errors = store.tasks.list_report(thread.thread_id)
            goals = store.goals.list(thread.thread_id)
            if link_errors:
                return ()
    except Exception:
        return ()
    now_value = time.time()
    items: list[NamedConversationWorkStatus] = []
    seen: set[tuple[str, str]] = set()
    for link in links:
        if str(getattr(link, "work_kind", "") or "") != "audit":
            continue
        name = str(getattr(link, "work_name", "") or "").strip()
        if not name:
            continue
        status = str(getattr(link, "status", "") or "").strip().lower()
        if status in THREAD_TASK_LINK_INACTIVE_STATUSES:
            continue
        created_at = float(getattr(link, "created_at", 0.0) or 0.0)
        started_at = thread_task_run_started_at(link, fallback=created_at)
        audit_health = _audit_brief_health(owner_agent, link)
        items.append(
            NamedConversationWorkStatus(
                kind="audit",
                name=name,
                status=(
                    audit_health or "时长已到"
                    if status == "active"
                    and float(getattr(link, "expires_at", 0.0) or 0.0) > 0
                    and float(getattr(link, "expires_at", 0.0) or 0.0) <= now_value
                    else audit_health or _named_work_status_label(status)
                ),
                elapsed_seconds=max(
                    0.0,
                    now_value - (started_at or now_value),
                ),
            )
        )
        seen.add(("audit", name))
    for record in _pending_exact_audit_requests(paths, scope, ""):
        system_task = record.payload.get("system_task")
        attributes = system_task.get("attributes") if isinstance(system_task, dict) else {}
        name = (
            str(attributes.get("conversation_work_name") or "").strip()
            if isinstance(attributes, dict)
            else ""
        )
        if not name or ("audit", name) in seen:
            continue
        created_at = _request_timestamp(record.payload)
        items.append(
            NamedConversationWorkStatus(
                kind="audit",
                name=name,
                status=(
                    "排队中"
                    if record.path is not None and record.path.parent == paths.inbox
                    else "运行中"
                ),
                elapsed_seconds=max(0.0, now_value - created_at) if created_at else 0.0,
            )
        )
        seen.add(("audit", name))
    for goal in goals:
        name = str(getattr(goal, "name", "") or "").strip()
        status = str(getattr(goal, "status", "") or "").strip().lower()
        if not name or status == "complete":
            continue
        items.append(
            NamedConversationWorkStatus(
                kind="goal",
                name=name,
                status=_named_work_status_label(status),
                elapsed_seconds=float(store.goal_clock.current_time_seconds(goal)),
            )
        )
    return tuple(
        sorted(
            items,
            key=lambda item: (item.kind, item.name.casefold()),
        )
    )


def _audit_brief_health(owner_agent: object, link: object) -> str:
    """Project only actionable Audit health into the compact global status."""
    try:
        from ..ingestion.audit_state import audit_task_source_facts

        sources = audit_task_source_facts(
            owner_agent,
            str(getattr(link, "task_id", "") or ""),
        )
    except Exception:
        return ""
    if any(
        isinstance(source.get("source_worker"), dict)
        and source["source_worker"].get("state") == "awaiting_operator"
        for source in sources
    ):
        return "额度暂停"
    if any(
        isinstance(source.get("capacity"), dict)
        and isinstance(source["capacity"].get("capacity_alert"), dict)
        and source["capacity"]["capacity_alert"].get("active") is True
        for source in sources
    ):
        return "容量告警"
    return ""


def _named_work_status_label(status: str) -> str:
    return {
        "active": "运行中",
        "preparing": "准备中",
        "paused": "已暂停",
        "blocked": "已阻塞",
        "usage_limited": "额度受限",
        "budget_limited": "时长或预算已到",
        "interrupted": "已停止",
    }.get(status, status or "未知")


# LLM: A durable task is resumable conversation state, not proof that an executor is live.
# 函数用途:只按 processing 记录、活跃子代理和持久执行台账判断当前任务是否真在运行。
def _control_record_is_executing(
    owner_agent: object,
    active: _GatewayRequestRecord,
    subagents: tuple[int, int, int, int],
) -> bool:
    if active.target_kind == "request" or active.linked_request is not None:
        return True
    if subagents[1] > 0:
        return True
    thread_id = str(active.payload.get("conversation_thread_id") or "").strip()
    task_id = _record_id(active)
    if not thread_id or not task_id:
        return True
    try:
        from ..conversation.task_promotion import conversation_task_execution_state

        execution = conversation_task_execution_state(
            owner_agent.conversation_store,
            thread_id,
            task_id,
        )
    except Exception:
        return True
    if execution.get("state_available") is not True:
        return True
    return execution.get("running") is True


# LLM: Owner resolution reuses the request worker's fail-closed multi-user boundary.
# 函数用途:按请求身份取得与真实执行相同的 owner-scoped agent。
def _request_agent(base_agent: object, payload: dict[str, object]):
    from .request_worker import _resolve_request_agent

    return _resolve_request_agent(base_agent, payload)


# LLM: A bound control scope carries the owner identity chosen before the operation receipt was
# prepared. Exact recovery must use that identity instead of rerunning mutable routing config.
# 函数用途: 为首次控制请求冻结 owner；已有回执恢复时保留其中已经固定的 owner。
def bind_gateway_control_scope_owner(
    base_agent: object,
    scope: GatewayControlScope,
) -> GatewayControlScope:
    if scope.resolved_owner is not None:
        return scope
    from .request_worker import _resolve_request_owner_identity

    owner = _resolve_request_owner_identity(base_agent, _scope_request_payload(scope))
    return replace(scope, resolved_owner=owner)


# LLM: Scope-based controls prefer the persisted owner identity. Raw channel facts are consulted
# only once, before a durable control receipt exists.
# 函数用途: 按控制作用域取得唯一 Agent，确保配置变化后仍回到原 owner 对账。
def _request_agent_for_scope(base_agent: object, scope: GatewayControlScope):
    if scope.resolved_owner is None:
        return _request_agent(base_agent, _scope_request_payload(scope))
    from .request_worker import _resolve_request_agent_for_owner

    return _resolve_request_agent_for_owner(base_agent, scope.resolved_owner)


# LLM: Read-only status may degrade to base model facts when an idle owner cannot be materialized.
# 函数用途:状态查询尽量解析 owner；失败时只返回基础 agent，不扩大写权限。
def _request_agent_or_base(base_agent: object, payload: dict[str, object]):
    try:
        return _request_agent(base_agent, payload)
    except Exception:
        return base_agent


# LLM: Thread profile is resolved by the same structured channel binding as ordinary conversation history.
# 函数用途:读取当前会话已压缩次数和过程显示档位；未知时明确省略。
def _conversation_profile(agent: object, payload: dict[str, object]) -> tuple[int | None, str]:
    conversation = payload.get("conversation")
    if not isinstance(conversation, dict):
        return None, ""
    store = getattr(agent, "conversation_store", None)
    if store is None:
        return None, ""
    try:
        thread = store.threads.resolve(
            channel=str(conversation.get("channel") or ""),
            channel_conversation_id=str(conversation.get("channel_conversation_id") or ""),
            channel_user_id=str(conversation.get("channel_user_id") or ""),
        )
    except Exception:
        return None, ""
    if thread is None:
        return 0, "off"
    return int(getattr(thread, "compact_generation", 0) or 0), str(
        getattr(thread, "verbose_level", "off") or "off"
    )


# LLM: Subagent counts are derived from durable parent/root ids, never from streamed chatter.
# 函数用途:统计当前主请求派生子代理的运行、完成和异常数量。
def _subagent_status(agent: object, request_ids: list[str]) -> tuple[int, int, int, int]:
    selected_ids = list(dict.fromkeys(item for item in request_ids if item))
    if not selected_ids:
        return 0, 0, 0, 0
    try:
        request_run_ids = {
            run_id
            for request_id in selected_ids
            for run_id in agent.subagent_run_ids_for_request(request_id)
        }
        tasks = agent.subagents.list_runs()
    except Exception:
        return 0, 0, 0, 0
    related = [task for task in tasks if str(getattr(task, "id", "") or "") in request_run_ids]
    statuses = [str(getattr(task, "status", "") or "").upper() for task in related]
    running = sum(status in _ACTIVE_SUBAGENT_STATUSES for status in statuses)
    done = sum(status in _DONE_SUBAGENT_STATUSES for status in statuses)
    return len(related), running, done, max(0, len(related) - running - done)


# LLM: Progress summarizes typed phase/ok only; display text and raw tool data never drive /status.
# 函数用途:从最近一条 typed 工具事件生成不泄露内部执行细节的阶段描述。
def _recent_progress(paths: GatewayPaths, request_id: str) -> str:
    if not request_id:
        return ""
    for row in reversed(_tail_json_rows(gateway_chunk_path(paths, request_id))):
        if row.get("kind") != "tool_progress" or not isinstance(row.get("progress"), dict):
            continue
        phase = str(row["progress"].get("phase") or "")
        if phase == "started":
            return "正在执行一个步骤"
        if phase == "finished":
            if row["progress"].get("ok") is False:
                return "一个步骤失败，正在处理"
            return "刚完成一个执行步骤"
        if phase == "interrupted":
            return "正在停止"
    return "正在处理"


# LLM: The tail reader is byte-bounded so /status cannot load an unbounded stream file into memory.
# 函数用途:读取进度文件末尾至多 64 KiB 的 JSON 行。
def _tail_json_rows(path: Path, max_bytes: int = 64 * 1024) -> list[dict[str, object]]:
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, 2)
            handle.seek(max(0, size - max_bytes))
            data = handle.read(max_bytes)
    except OSError:
        return []
    text = data.decode("utf-8", "replace")
    if size > max_bytes and "\n" in text:
        text = text.split("\n", 1)[1]
    rows: list[dict[str, object]] = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


# LLM: Synthetic idle status facts preserve the same owner and conversation schema as /ask.
# 函数用途:没有活跃请求时，根据控制请求身份构造只读会话定位信息。
def _scope_request_payload(scope: GatewayControlScope) -> dict[str, object]:
    # `/ask` and control commands must resolve the same owner for every
    # channel, including trusted local/CLI channels.  The authenticated scope
    # wins over optional message metadata so callers cannot redirect a control
    # command into another owner.
    metadata = {
        **dict(scope.metadata),
        "user_id": scope.user_id,
        "channel": scope.channel,
    }
    return {
        "user_id": scope.user_id,
        "metadata": metadata,
        "conversation": {
            "channel": scope.channel,
            "channel_conversation_id": scope.conversation_id,
            "channel_user_id": scope.user_id,
            "canonical_user_id": scope.user_id,
        },
    }


# LLM: 非会话控制的 Gateway 服务也必须复用同一 owner 裁决；调用方只能传入已认证的 GatewayControlScope。
# 函数用途: 为记忆、历史等薄客户端服务解析与普通请求完全相同的 owner-scoped Agent。
def resolve_gateway_scope_agent(base_agent: object, scope: GatewayControlScope):
    return _request_agent_for_scope(base_agent, scope)


# LLM: Passive disk projections need the same authenticated owner decision as active requests but
# must not construct an Agent. The returned identity is the only authority callers may use to
# locate an owner home; channel text and filesystem discovery cannot select another owner.
# 函数用途: 只解析当前控制请求对应的精确 owner，不加载模型和运行时。
def resolve_gateway_scope_owner(
    base_agent: object,
    scope: GatewayControlScope,
):
    from .request_worker import _resolve_request_owner_identity

    if scope.resolved_owner is not None:
        return scope.resolved_owner
    return _resolve_request_owner_identity(base_agent, _scope_request_payload(scope))


# LLM: TUI/Web passive status reads follow 会话运行时 residency: loaded owners
# are queried in memory and cold owners stay cold. This function never records
# activity or constructs an Agent; callers must not use it for writes/controls.
# 函数用途: 只为状态轮询查找已经加载的用户 Agent，避免空闲客户端把 Gateway 拖进反复初始化。
def resolve_loaded_gateway_scope_agent(
    base_agent: object,
    scope: GatewayControlScope,
):
    from .request_worker import _resolve_loaded_request_agent_for_owner

    owner = resolve_gateway_scope_owner(base_agent, scope)
    return _resolve_loaded_request_agent_for_owner(base_agent, owner)


# LLM: Request ids are taken only from the claimed record or its filename.
# 函数用途:读取请求记录的稳定 id。
def _record_id(record: _GatewayRequestRecord | None) -> str:
    if record is None:
        return ""
    path_stem = record.path.stem if record.path is not None else ""
    return str(record.payload.get("id") or record.payload.get("request_id") or path_stem)


# LLM: Timestamp ordering uses durable queue/lease fields with a zero fallback.
# 函数用途:取得请求排序时间。
def _request_timestamp(payload: dict[str, object]) -> float:
    for key in ("lease_started_at", "created_at", "submitted_at"):
        try:
            if value := float(payload.get(key) or 0.0):
                return value
        except (TypeError, ValueError):
            continue
    return 0.0


# LLM: Elapsed time starts at the claimed lease, not user-controlled prompt metadata.
# 函数用途:取得任务实际开始时间。
def _request_started_at(payload: dict[str, object]) -> float:
    try:
        return float(payload.get("lease_started_at") or payload.get("updated_at") or 0.0)
    except (TypeError, ValueError):
        return 0.0


# LLM: Status exposes a bounded user prompt only; internal injection and tool plans are excluded.
# 函数用途:读取用户提交的任务正文。
def _request_prompt(payload: dict[str, object]) -> str:
    return str(payload.get("prompt") or payload.get("goal") or "").strip()


__all__ = [
    "GatewayControlScope",
    "execute_gateway_conversation_control",
    "reconcile_gateway_steer_delivery",
    "request_gateway_memory_curator_lifecycle",
    "resolve_gateway_scope_owner",
    "resolve_gateway_scope_agent",
    "resolve_loaded_gateway_scope_agent",
    "steer_active_conversation_if_running",
]
