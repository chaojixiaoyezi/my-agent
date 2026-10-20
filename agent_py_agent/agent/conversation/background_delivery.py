# LLM: 普通与必要通知共核原canonical工具及工作attempt，已外发在canonical前冻结；后台交付沿原来源、canonical和整封冻结顺序；首次认证拒绝的宿主义务独立于普通结果静默，不能扩大owner/actor或重执业务。
# 模块用途: 维护后台回复和必要失败提醒的原投递、发送前代次复核与冻结重投，回执仍唯一证明外发。
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol

from ..scheduler.execution import scheduled_local_delivery
from .background_receipt_contract import public_receipt_delivery_block
from .channels import (
    PROACTIVE_PUSH_CHANNELS,
    ChannelAttachment,
    DeliveryContext,
    DeliveryServiceProtocol,
    ReplyEnvelope,
    project_user_reply,
    supports_transcript_delivery,
)
from .learning_source import (
    LEARNING_SOURCE_KEY,
    background_projection,
    derive_source_projection,
    projection_from_wake_metadata,
)
from .models import BackgroundDeliveryCommit, WakeSignal
from .store import ConversationStore


# LLM: 身份来自宿主冻结请求，正文不得改变 thread/task/wake 或回合编号；接口不依赖调度器实现。
# 类用途: 声明后台回复落账和重投所需的最小请求字段。
class BackgroundDeliveryRequest(Protocol):
    thread_id: str
    task_id: str
    reason: str
    conversation_turn_id: str
    wake_signal: dict[str, Any] | None


# LLM: task_status 在原抑制位置查询最新事实；store 与执行层共用，不创建额外持久队列或状态源。
# 类用途: 显式绑定交付所需的 Agent、会话账、投递服务和只读任务状态能力。
@dataclass(frozen=True)
class BackgroundDeliveryDependencies:
    agent: object
    store: ConversationStore
    channels: DeliveryServiceProtocol
    task_status: Callable[[BackgroundDeliveryRequest], str]


# 路线归属读取部署声明，适配器暂不可用不会撤销已有的外发义务。
_ROUTE_LOCAL = "local"
ROUTE_EXTERNAL = "external"
_ROUTE_UNDECLARED = "undeclared"


# LLM: canonical 记录与外部投递是两条独立事实：正文归属看路线归属（声明级），外发成功与否只看回执。
# 未送达但有外发义务的正文必须整封冻结在唤醒上重投，不能在无记录的情况下确认唤醒；
# frozen_retry 非空表示这是重投：整封 envelope（正文/附件/过程/metadata）都来自冻结载荷。
# 已外发的冻结回执只补canonical，不受新准入否决而再次外发；未外发事件仍须原generation与开始CAS。
# 函数用途: 委派只留原执行线程内部结果，其余沿原渠道/冻结回执提交；主sender的模型通知仍走原外发门。
def record_background_response(
    delivery: BackgroundDeliveryDependencies,
    request: BackgroundDeliveryRequest,
    delivery_context: DeliveryContext,
    internal_content: str,
    *,
    public_receipt: dict[str, object] | None = None,
    delivery_artifacts: tuple[dict[str, object], ...] = (),
    operation_verification: dict[str, object] | None = None,
    assistant_commentaries: tuple[str, ...] = (),
    display_snapshot: dict[str, object] | None = None,
    deliver: bool,
    delivery_reason: str,
    route_supports_transcript: bool | None = None,
    frozen_retry: FrozenOwnerDelivery | None = None,
    local_transcript_only: bool = False,
) -> BackgroundDeliveryCommit:
    # 内部协议仍交给真实 DeliveryService 做主动消息抑制，但普通 transcript/report
    # 只能保存用户投影，否则下一轮 compact 和 owner-local 搜索会被机器协议污染。
    if (internal := _record_delegated_internal_response(
        delivery, request, delivery_context, internal_content, delivery_artifacts=delivery_artifacts,
        assistant_commentaries=assistant_commentaries, display_snapshot=display_snapshot, frozen_retry=frozen_retry
    )) is not None:
        return internal
    return _record_background_owner_response(
        delivery, request, delivery_context, internal_content, public_receipt=public_receipt,
        delivery_artifacts=delivery_artifacts, operation_verification=operation_verification,
        assistant_commentaries=assistant_commentaries, display_snapshot=display_snapshot,
        deliver=deliver, delivery_reason=delivery_reason, route_supports_transcript=route_supports_transcript,
        frozen_retry=frozen_retry, local_transcript_only=local_transcript_only)


# LLM: 原owner信封提交保留冻结回执、代次、渠道与canonical的完整顺序；仅由上层排除未外发委派内部结果。
# 函数用途: 执行普通后台通知或已外发回执补账，保持附件、历史和失败重投原语义。
def _record_background_owner_response(
    delivery: BackgroundDeliveryDependencies,
    request: BackgroundDeliveryRequest,
    delivery_context: DeliveryContext,
    internal_content: str,
    *,
    public_receipt: dict[str, object] | None = None,
    delivery_artifacts: tuple[dict[str, object], ...] = (),
    operation_verification: dict[str, object] | None = None,
    assistant_commentaries: tuple[str, ...] = (),
    display_snapshot: dict[str, object] | None = None,
    deliver: bool,
    delivery_reason: str,
    route_supports_transcript: bool | None = None,
    frozen_retry: FrozenOwnerDelivery | None = None,
    local_transcript_only: bool = False,
) -> BackgroundDeliveryCommit:
    local_transcript_only = local_transcript_only or scheduled_local_delivery(delivery.agent, request)
    if block := public_receipt_delivery_block(delivery.agent, request, public_receipt, internal_content,
                                              has_attachments=bool(_channel_attachments(delivery_artifacts)), frozen_retry=frozen_retry):
        return _uncommitted_delivery("", block)
    projection = project_user_reply(internal_content)
    if _background_reply_suppressed(
        delivery,
        request,
        delivery_reason=delivery_reason,
        deliver=deliver,
        projection_content=projection.content,
        has_attachments=bool(_channel_attachments(delivery_artifacts)),
        local_transcript_only=local_transcript_only,
    ):
        return _uncommitted_delivery(projection.content if deliver else "", "suppressed")
    # 信封只携带用户投影，避免投递层把内部控制正文当作普通回复。
    attachments = _channel_attachments(delivery_artifacts, owner_root=str(getattr(getattr(delivery.agent, 'home_paths', None), 'owner_home_dir', '') or ''))
    evidence_refs = _background_evidence_refs_for(delivery, request, frozen_retry)
    # 重投路径由 redeliver_cached_wake 把冻结载荷的 delivery_artifacts 当作 delivery_artifacts
    # 传进来，因此附件与过程回复必须继续从参数推导；重投只额外接管 metadata 与"是否已外发"。
    envelope = ReplyEnvelope(
        content=projection.content,
        attachments=attachments,
        evidence_refs=evidence_refs,
    )
    message_metadata = _background_owner_message_metadata(
        request,
        delivery_reason=delivery_reason,
        projection_status=projection.projection_status,
        delivery_artifacts=delivery_artifacts,
        evidence_refs=evidence_refs,
        operation_verification=operation_verification,
        frozen_retry=frozen_retry,
        public_receipt=public_receipt,
    )
    if display_snapshot is not None and frozen_retry is None:
        message_metadata["goal_continuation_allowed"] = display_snapshot.get("goal_continuation_allowed") is True
    route = _local_only_route(_background_delivery_route(
        delivery.channels,
        delivery_context,
        route_supports_transcript=route_supports_transcript,
    ), local_transcript_only)
    origin_valid = _validated_background_origin(delivery.agent, request, delivery_context, frozen_retry)
    receipt, delivery_status = _background_delivery_for_origin(
        delivery, delivery_context, envelope,
        origin_valid=origin_valid,
        external_options=_ExternalDeliveryOptions(frozen_retry=frozen_retry, local_transcript_only=local_transcript_only, request=request),
        message_metadata=message_metadata,
    )
    # 投递服务执行最终脱敏，历史保存其回执正文；缺少正文的既有替身保留已净化投影。
    committed_content = str(getattr(receipt, "content", projection.content) or "")
    freeze = _OwnerDeliveryFreeze(request=request, content=committed_content,
        delivery_artifacts=delivery_artifacts, assistant_commentaries=assistant_commentaries,
        evidence_refs=evidence_refs, message_metadata=message_metadata, delivery_status=delivery_status,
        receipt_id=str(getattr(receipt, 'receipt_id', '') or ''), target=delivery_context.target,
        ownership=route.ownership, canonical_record=route.canonical_record or delivery_status == 'sent', persisted=False)
    if delivery_status == 'sent':
        # 原外发已发生，先保存已有wake的整封回执；canonical失败后只能补账，不能重执模型或运输。
        _freeze_pending_owner_delivery(delivery.store, freeze)
    commit = _commit_background_response(
        delivery,
        request,
        delivery_context,
        receipt=receipt,
        delivery_status=delivery_status,
        committed_content=committed_content,
        canonical_record=route.canonical_record and origin_valid,
        transcript_route=route.transcript_route,
        evidence_refs=evidence_refs,
        message_metadata=message_metadata,
        assistant_commentaries=assistant_commentaries,
        display_snapshot=display_snapshot,
        audit_refs_settled=bool(frozen_retry is not None and frozen_retry.external_sent),
    )
    frozen_now = _freeze_pending_owner_delivery(delivery.store, replace(freeze, persisted=commit.persisted))
    return replace(commit, outbox_frozen=frozen_now) if frozen_now else commit


# LLM: 仅固定委派内部留档，canonical scheduler与已external_sent重投返回原路径；不发送、换owner或冒充主sender，不创建第二队列。
# 函数用途: 保存执行线程自己的模型结果；终态回报与主会话通知由原SessionTask/guidance/wake负责。
def _record_delegated_internal_response(delivery, request, context, content, *,
        delivery_artifacts, assistant_commentaries, display_snapshot, frozen_retry):
    if frozen_retry is not None and frozen_retry.external_sent:
        return None
    from .im_delegation_delivery import delegated_delivery_task

    delegated = delegated_delivery_task(delivery.agent, request.thread_id, task_id=request.task_id)
    if delegated is None:
        return None
    internal_context = replace(context, channel="internal", target="", thread_id=request.thread_id)
    return _commit_background_response(delivery, request, internal_context,
        receipt=SimpleNamespace(content=content), delivery_status="local_only",
        committed_content=project_user_reply(content).content,
        canonical_record=True, transcript_route=True, evidence_refs=(),
        message_metadata={"session_task_id": delegated.task_id, "delivery_reason": "delegated_internal_result",
                          "delivery_artifacts": list(delivery_artifacts)},
        assistant_commentaries=assistant_commentaries, display_snapshot=display_snapshot)


# LLM: 已external_sent冻结事实只授权canonical收尾，未外发信封仍核当前代次且实际运输另走begin CAS；普通任务仅只读。
# 函数用途: 区分已发生外发的原回执收口与尚待发送的来源预检，不产生副作用或新重投身份。
def _validated_background_origin(agent, request, context, frozen_retry):
    from ..scheduler.execution import validate_delivery_origin

    if frozen_retry is not None and frozen_retry.external_sent:
        return validate_delivery_origin(agent, request, context, admitted_completion=True)
    return validate_delivery_origin(agent, request, context)


# LLM: 重投载荷必须是整封 owner envelope 的不可变快照：正文、附件（delivery_artifacts）、
# 过程回复、审计引用、canonical metadata、以及"外发是否已经成功"这一事实。
# 只存正文会让重投丢附件/丢过程/重复外发；只存状态会让重投无从重建消息。
# 类用途: 承载一条已冻结的 owner 交付载荷（v1/v2 载荷统一投影）。
@dataclass(frozen=True)
class FrozenOwnerDelivery:
    content: str
    delivery_artifacts: tuple[dict[str, object], ...] = ()
    assistant_commentaries: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    message_metadata: dict[str, object] = field(default_factory=dict)
    external_sent: bool = False
    receipt_id: str = ""
    ownership: str = ""


# LLM: 冻结判定所需的全部事实；target 必须用已解析的投递目标，不能回读 request.route_target。
# 类用途: 承载“这条答复是否要整封冻结在唤醒上”的结构化输入。
@dataclass(frozen=True)
class _OwnerDeliveryFreeze:
    request: BackgroundDeliveryRequest
    content: str
    delivery_artifacts: tuple[dict[str, object], ...]
    assistant_commentaries: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    message_metadata: dict[str, object]
    delivery_status: str
    target: str
    ownership: str
    canonical_record: bool
    persisted: bool
    receipt_id: str = ""


# LLM: 外发义务未完成、应有 canonical 记录未提交，或容量报告要求保留时，冻结完整载荷。
# 纯附件也需保留；suppressed 和空载荷不冻结。store 写入结果才证明重投载荷已落盘。
# 函数用途: 从原request取得唯一wake编号并冻结未完成交付的整封载荷，返回实际保存结果。
def _freeze_pending_owner_delivery(
    store: ConversationStore,
    freeze: _OwnerDeliveryFreeze,
) -> bool:
    wake = freeze.request.wake_signal if isinstance(freeze.request.wake_signal, dict) else {}
    wake_signal_id = str(wake.get("wake_signal_id") or "").strip()
    if not wake_signal_id:
        return False
    if not (str(freeze.content or "").strip() or freeze.delivery_artifacts):
        return False
    status = str(freeze.delivery_status or "").strip().lower()
    if status in {"suppressed", "local_only"}:
        return False
    obligation = _background_delivery_obligation(
        target=freeze.target,
        ownership=freeze.ownership,
    )
    external_pending = bool(obligation and status != "sent")
    canonical_pending = bool(freeze.canonical_record and not freeze.persisted)
    if not (
        external_pending
        or canonical_pending
        or audit_capacity_report_event(freeze.request)
    ):
        return False
    # 冻结成功与否必须来自 store 的真实写入结果：唤醒不存在/已不是 pending 时
    # 载荷不会落盘，此时不能对外声称"已冻结、可重投"。
    cached = store.wakes.cache_delivery(
        wake_signal_id,
        {
            "schema_version": "wake-owner-delivery.v2",
            "reason": freeze.request.reason,
            "task_id": freeze.request.task_id,
            "content": freeze.content,
            "delivery_artifacts": [dict(item) for item in freeze.delivery_artifacts],
            "assistant_commentaries": list(freeze.assistant_commentaries),
            "evidence_refs": list(freeze.evidence_refs),
            "message_metadata": dict(freeze.message_metadata),
            "external_sent": bool(status == "sent"),
            "receipt_id": freeze.receipt_id,
            "ownership": freeze.ownership,
            "last_delivery_status": status,
            "created_at": time.time(),
        },
    )
    return cached is not None


# LLM: 调用方先经 cached_owner_delivery 校验身份和版本；这里只投影已验证载荷，不自行推断外发成功。
# 函数用途: 把 v1 正文或 v2 完整信封转换成重投时使用的冻结对象。
def frozen_owner_delivery(payload: dict[str, object]) -> FrozenOwnerDelivery:
    artifacts = payload.get("delivery_artifacts")
    commentaries = payload.get("assistant_commentaries")
    refs = payload.get("evidence_refs")
    metadata = payload.get("message_metadata")
    return FrozenOwnerDelivery(
        content=str(payload.get("content") or ""),
        delivery_artifacts=tuple(
            dict(item) for item in artifacts if isinstance(item, dict)
        )
        if isinstance(artifacts, list)
        else (),
        assistant_commentaries=tuple(
            str(item) for item in commentaries if str(item or "").strip()
        )
        if isinstance(commentaries, list)
        else (),
        evidence_refs=tuple(str(item) for item in refs if isinstance(item, str) and item.strip())
        if isinstance(refs, list)
        else (),
        message_metadata=dict(metadata) if isinstance(metadata, dict) else {},
        external_sent=payload.get("external_sent") is True,
        receipt_id=str(payload.get("receipt_id") or ""),
        ownership=str(payload.get("ownership") or ""),
    )


# LLM: canonical 行 metadata 只能在这里构造：重投时 frozen metadata 是权威，
# 只补齐本次重投的时间无关字段，避免重投把原始 task/refs/过程信息覆盖成重投时的值。
# 函数用途: 组装后台公开消息 metadata，与本片原生历史关联；重投保留原编号，不重复写入模型事实。
def _background_owner_message_metadata(
    request: BackgroundDeliveryRequest,
    *,
    delivery_reason: str,
    projection_status: str,
    delivery_artifacts: tuple[dict[str, object], ...],
    evidence_refs: tuple[str, ...],
    operation_verification: dict[str, object] | None,
    frozen_retry: FrozenOwnerDelivery | None,
    public_receipt: dict[str, object] | None = None,
) -> dict[str, object]:
    if frozen_retry is not None and frozen_retry.message_metadata:
        restored = {
            **frozen_retry.message_metadata,
            "background_delivery_retry_reason": delivery_reason,
        }
        restored.setdefault(LEARNING_SOURCE_KEY, background_projection())
        return restored
    wake = request.wake_signal if isinstance(request.wake_signal, dict) else {}
    wake_metadata = wake.get("metadata") if isinstance(wake.get("metadata"), dict) else {}
    source = projection_from_wake_metadata(wake_metadata)
    if source is None:
        source = derive_source_projection(wake_metadata)
    if source is None:
        source = background_projection()
    metadata: dict[str, object] = {
        "conversation_request_id": request.conversation_turn_id,
        "reason": request.reason,
        "task_id": request.task_id,
        "delivery_artifacts": [dict(item) for item in delivery_artifacts],
        "projection_status": projection_status,
        "background_delivery_reason": delivery_reason,
        "evidence_refs": list(evidence_refs),
        LEARNING_SOURCE_KEY: source,
    }
    if operation_verification is not None:
        metadata["operation_verification"] = operation_verification
    if public_receipt is not None:
        metadata["public_receipt"] = dict(public_receipt)
    return metadata


# LLM: 本次外部投递的“既有事实”打包：重投载荷与空片本地转录标记互不排斥，
#   合并后 _background_external_delivery 参数回到 4 个以内，调用点也不容易漏传。
# 类用途: 承载原请求及冻结/本地投递事实，使新通知在运输前复核准确原任务，不从正文重建身份。
@dataclass(frozen=True)
class _ExternalDeliveryOptions:
    frozen_retry: FrozenOwnerDelivery | None = None
    local_transcript_only: bool = False
    request: BackgroundDeliveryRequest | None = None


# LLM: 原来源校验失败不能发网络请求，也不能把失败记为sent；成功只委托原投递出口，metadata记录同一事实。
# 函数用途: 将来源失效和原实际投递分成明确接缝，保留既有本地、冻结重投及外发顺序。
def _background_delivery_for_origin(
    delivery: BackgroundDeliveryDependencies, context: DeliveryContext, envelope: ReplyEnvelope, *,
    origin_valid: bool, external_options: _ExternalDeliveryOptions, message_metadata: dict[str, object],
) -> tuple[object, str]:
    if origin_valid:
        return _background_external_delivery(delivery, context, envelope, external_options)
    message_metadata["delivery_error_code"] = "SCHEDULER_ORIGIN_BINDING_CHANGED"
    return SimpleNamespace(content=envelope.content), "failed"


# LLM: 外部发送只有这一个出口：重投时若冻结载荷已证明外发成功，就绝不能再次外发
# （只补本地 canonical），否则会对真实用户重复发消息。事件实际运输先经原job CAS准入，普通任务零scheduler写。
# 函数用途: 沿原运输执行已准入投递，认证提醒最后复核当前代次；已外发仅补记录，不持锁跨运输。
def _background_external_delivery(
    delivery: BackgroundDeliveryDependencies,
    delivery_context: DeliveryContext,
    envelope: ReplyEnvelope,
    external_options: _ExternalDeliveryOptions,
) -> tuple[object, str]:
    frozen_retry = external_options.frozen_retry
    if frozen_retry is not None and frozen_retry.external_sent:
        return (
            SimpleNamespace(
                delivery_status="sent",
                channel=delivery_context.channel,
                target=delivery_context.target,
                content=envelope.content,
                evidence_refs=envelope.evidence_refs,
                receipt_id=frozen_retry.receipt_id,
                replayed=True,
            ),
            "sent",
        )
    if external_options.local_transcript_only:
        # 空片正文不主动推给用户，只作为本地转录保存；delivery_status 用 local_only
        # 表示"这条不欠外发"，唤醒确认与冻结判定都按本地转录处理。
        return (
            SimpleNamespace(
                delivery_status="local_only",
                channel=delivery_context.channel,
                target=delivery_context.target,
                content=envelope.content,
                evidence_refs=envelope.evidence_refs,
                receipt_id="",
            ),
            "local_only",
        )
    from .required_user_action import notice_is_current, required_notice
    request = external_options.request
    if request is not None and required_notice(delivery.agent, request) is not None and not notice_is_current(delivery.agent, request):
        return SimpleNamespace(content=''), 'suppressed'
    from ..scheduler.effect_scope import SchedulerEffectRejected, scheduler_effect_scope

    try:
        with scheduler_effect_scope(delivery.agent, delivery_context.task_id, delivery_context.idempotency_key,
                kind='delivery', allow_terminal=frozen_retry is not None):
            receipt = delivery.channels.deliver(delivery_context, envelope)
    except SchedulerEffectRejected as exc:
        return SimpleNamespace(content=envelope.content, error_code=exc.error_code), 'failed'
    return receipt, str(getattr(receipt, "delivery_status", "") or "sent")


# LLM: 投递前的三道抑制判定必须同源：未授权投递、任务已终态、没有可交付正文。它们都表示
# “这片回复不面向 owner”，因此不外发也不保存公开正文；原生执行历史独立保留，goal 终态交付是例外。
# 函数用途: 判断这片后台回复是否在调用投递服务之前就应被抑制。
def _background_reply_suppressed(
    delivery: BackgroundDeliveryDependencies,
    request: BackgroundDeliveryRequest,
    *,
    delivery_reason: str,
    deliver: bool,
    projection_content: str,
    has_attachments: bool,
    local_transcript_only: bool = False,
) -> bool:
    if not deliver and not local_transcript_only:
        return True
    terminal_status = delivery.task_status(request)
    goal_terminal_delivery = delivery_reason in {
        "thread_goal_blocked",
        "thread_goal_budget_limited",
        "thread_goal_usage_limited",
    }
    if (
        terminal_status in {"abandoned", "cancelled", "interrupted", "superseded"}
        and not goal_terminal_delivery
    ):
        return True
    return not str(projection_content or "").strip() and not has_attachments


# LLM: canonical final 保存正文、过程和 host-owned turn-end；commentary 不携带终态，缺失原因不从文字补造。
# 外发结果只作为 metadata 事实，不参与“要不要给 owner 留下这条回复”的判定。
# 函数用途: 幂等保存后台回复及长度限制原因，并返回结构化落账结果。
def _commit_background_response(
    delivery: BackgroundDeliveryDependencies,
    request: BackgroundDeliveryRequest,
    delivery_context: DeliveryContext,
    *,
    receipt: object,
    delivery_status: str,
    committed_content: str,
    canonical_record: bool,
    transcript_route: bool,
    evidence_refs: tuple[str, ...],
    message_metadata: dict[str, object],
    assistant_commentaries: tuple[str, ...] = (),
    display_snapshot: dict[str, object] | None = None,
    outbox_frozen: bool = False,
    audit_refs_settled: bool = False,
) -> BackgroundDeliveryCommit:
    status = str(delivery_status or "").strip().lower()
    has_body = bool(str(committed_content or "").strip()) or bool(
        message_metadata.get("delivery_artifacts")
    )
    if status == "suppressed":
        # 投递层判定这是内部协议正文：既不外发，也不能落成用户消息。
        return BackgroundDeliveryCommit(
            content=committed_content,
            delivery_status=status,
            persisted=False,
            commit_kind="suppressed",
            transcript_route=transcript_route,
            outbox_frozen=outbox_frozen,
        )
    if status != "sent" and (not canonical_record or not has_body):
        # 没有可交付正文，或这条路线既不能外发也没有本地归属：只保留冻结重投事实。
        return BackgroundDeliveryCommit(
            content=committed_content,
            delivery_status=status,
            persisted=False,
            commit_kind="outbox_pending" if outbox_frozen else "none",
            transcript_route=transcript_route,
            outbox_frozen=outbox_frozen,
        )
    if display_snapshot:
        message_metadata = {
            **message_metadata,
            "background_transcript_request_id": display_snapshot.get("request_id", ""),
        }
    from ..turn_end import normalize_turn_end_reason

    end_reason = normalize_turn_end_reason((display_snapshot or {}).get("turn_end_reason"))
    delivery_key = background_delivery_idempotency_key(request)
    _append_owner_commentaries(
        delivery.store,
        request,
        delivery_context,
        message_metadata=message_metadata,
        delivery_key=delivery_key,
        assistant_commentaries=assistant_commentaries,
    )
    message_request = {
        "thread_id": delivery_context.thread_id,
        "role": "assistant",
        "content": committed_content,
        "channel": delivery_context.channel,
        "metadata": {
            **message_metadata, "assistant_part_id": "final",
            **({"background_display_turn": display_snapshot} if display_snapshot else {}),
            **({"turn_end_reason": end_reason} if end_reason else {}),
        },
    }
    if delivery_key:
        message_entry = delivery.store.messages.append_once(
            message_request,
            dedupe_key=f"owner_delivery:{delivery_key}",
        )
    else:
        message_entry = delivery.store.messages.append(message_request)
    if audit_refs_settled:
        # 这次只是补本地落账：审计交付回执在真正外发成功时已经记过，绝不能重复记一遍。
        pass
    elif status == "sent":
        _record_delivered_audit_refs(delivery.agent, receipt)
    elif transcript_route and audit_finding_report_event(request):
        # 本地会话追加本身就是交付回执，外发失败的路线不能冒用它登记审计成功。
        _record_transcript_audit_refs(
            delivery.agent,
            evidence_refs,
            message_entry=message_entry,
            channel=delivery_context.channel,
        )
    return BackgroundDeliveryCommit(
        content=committed_content,
        delivery_status=status,
        persisted=True,
        message_id=str(getattr(message_entry, "message_id", "") or ""),
        commit_kind="external_delivery" if status == "sent" else "canonical_record",
        transcript_route=transcript_route,
        outbox_frozen=outbox_frozen,
    )


# LLM: 过程回复与 final 共享同一批 metadata 和同一份投递幂等键；重投时必须逐条去重，
# 不能因为重放而把 commentary 追加第二遍。
# 函数用途: 按 typed part 顺序幂等追加一片后台工作的过程回复。
def _append_owner_commentaries(
    store: ConversationStore,
    request: BackgroundDeliveryRequest,
    delivery_context: DeliveryContext,
    *,
    message_metadata: dict[str, object],
    delivery_key: str,
    assistant_commentaries: tuple[str, ...],
) -> None:
    for index, commentary in enumerate(assistant_commentaries, start=1):
        text = str(commentary or "").strip()
        if not text:
            continue
        commentary_request = {
            "thread_id": delivery_context.thread_id,
            "role": "assistant",
            "content": text,
            "channel": delivery_context.channel,
            "metadata": {
                **message_metadata,
                "assistant_part_id": f"commentary:{index}",
                "process": True,
            },
        }
        if delivery_key:
            store.messages.append_once(
                commentary_request,
                dedupe_key=f"owner_delivery:{delivery_key}:commentary:{index}",
            )
        else:
            store.messages.append(commentary_request)


# LLM: 这里只保留宿主唤醒给出的结构化引用，不从模型正文、路径或 URL 猜交付权威；调用方区分普通证据与审计交付引用。
# 函数用途: 按原顺序去重后台请求的解释性证据引用，不写任何账本。
def background_evidence_refs(
    request: BackgroundDeliveryRequest,
) -> tuple[str, ...]:
    wake = request.wake_signal if isinstance(request.wake_signal, dict) else {}
    refs = wake.get("evidence_refs")
    if not isinstance(refs, list):
        return ()
    return tuple(
        dict.fromkeys(
            str(item).strip() for item in refs if isinstance(item, str) and str(item).strip()
        )
    )


# LLM: 审计交付引用统一由载荷解析器裁决，准备层与交付层必须共用同一规则。
# 函数用途: 从后台请求取得真正可登记为已交付的审计引用。
def background_delivery_evidence_refs(
    request: BackgroundDeliveryRequest,
) -> tuple[str, ...]:
    wake = request.wake_signal if isinstance(request.wake_signal, dict) else {}
    return audit_finding_payload_delivery_refs(wake)


# LLM: typed 引用优先；历史唤醒只接受与 watch_id 精确匹配的审计源引用，保留现存持久数据恢复合同。
# 函数用途: 区分可登记交付的审计来源与仅供解释的普通证据，不扩大旧记录的交付权限。
def audit_finding_payload_delivery_refs(
    wake: dict[str, Any],
) -> tuple[str, ...]:
    metadata = wake.get("metadata") if isinstance(wake.get("metadata"), dict) else {}
    typed = metadata.get("delivery_evidence_refs")
    if isinstance(typed, list):
        refs = tuple(
            dict.fromkeys(
                str(item).strip() for item in typed if isinstance(item, str) and str(item).strip()
            )
        )
        if refs:
            return refs

    # 老唤醒没有独立的交付字段，只能从精确 watch 范围恢复来源权限。
    watch_id = str(metadata.get("watch_id") or "").strip()
    refs = wake.get("evidence_refs")
    if not watch_id or not isinstance(refs, list):
        return ()
    from ..ingestion.harvester import parse_audit_source_ref

    selected: list[str] = []
    for item in refs:
        source_ref = str(item).strip() if isinstance(item, str) else ""
        parsed = parse_audit_source_ref(source_ref)
        if parsed is not None and parsed[0] == watch_id:
            selected.append(source_ref)
    return tuple(dict.fromkeys(selected))


# LLM: 重投沿持久 wake 或 scheduler run 身份去重，不能使用本次新建的会话回合编号；缺身份时不编造键。
# 函数用途: 为原投递和后续重投生成同一幂等键，供渠道及 canonical 消息共用。
def background_delivery_idempotency_key(request: BackgroundDeliveryRequest) -> str:
    wake = request.wake_signal if isinstance(request.wake_signal, dict) else {}
    wake_signal_id = str(wake.get("wake_signal_id") or "").strip()
    if wake_signal_id:
        return f"background-wake:{wake_signal_id}"
    metadata = wake.get("metadata") if isinstance(wake.get("metadata"), dict) else {}
    scheduler_run_id = str(metadata.get("scheduler_run_id") or "").strip()
    if scheduler_run_id:
        return f"scheduler-run:{scheduler_run_id}"
    return ""


# LLM: 仅在真实外发成功后调用；写本 owner 审计交付账，缺来源或回执编号不补造成功事实。
# 函数用途: 用外发回执把精确来源登记为已向用户报告。
def _record_delivered_audit_refs(agent: object, receipt: object) -> None:
    refs = tuple(getattr(receipt, "evidence_refs", ()) or ())
    owner_home = str(
        getattr(getattr(agent, "home_paths", None), "owner_home_dir", "") or ""
    ).strip()
    receipt_id = str(getattr(receipt, "receipt_id", "") or "").strip()
    if not refs or not owner_home or not receipt_id:
        return
    from ..ingestion.harvester import record_audit_delivery_refs

    record_audit_delivery_refs(
        Path(owner_home),
        refs,
        receipt_id=receipt_id,
        channel=str(getattr(receipt, "channel", "") or ""),
    )


# LLM: 只用于 canonical 本地交付面，必须先追加用户消息再用 message_id 写审计账；外发失败不得冒用。
# 函数用途: 为没有外部发送回执的本地会话登记真实交付引用。
def _record_transcript_audit_refs(
    agent: object,
    refs: tuple[str, ...],
    *,
    message_entry: object,
    channel: str,
) -> None:
    owner_home = str(
        getattr(getattr(agent, "home_paths", None), "owner_home_dir", "") or ""
    ).strip()
    message_id = str(getattr(message_entry, "message_id", "") or "").strip()
    if not refs or not owner_home or not message_id:
        return
    from ..ingestion.harvester import record_audit_delivery_refs

    record_audit_delivery_refs(
        Path(owner_home),
        refs,
        receipt_id=message_id,
        channel=str(channel or "internal"),
    )


# LLM: normal与required必须复读同一原操作/信封/owner/目标及工作attempt；通知轮身份不能替代原工作身份，正文不作匹配。
# 函数用途: 筛选已经真实回到本次来源会话的工具回复，假字典和其它合法目标均不能阻止宿主通知。
def committed_source_replies(agent, request, context, deliveries, *, execution_identity=None):
    from ..local_storage.tool_operations import ToolOperationStateError
    from ..runtime_db.managed_operation_store import ManagedOperationStore
    from ..scheduler.execution import validate_delivery_origin
    from .host_actor import background_host_actor
    from .runtime import _scheduler_run_id, _session_message_run_id, _session_task_run_id

    identity = execution_identity or {}
    if not identity or not all(identity.get(key) for key in ('request_id', 'run_id', 'task_id', 'attempt_id')):
        return ()
    actor = background_host_actor(agent, request.thread_id, request.task_id)
    if actor is None or not validate_delivery_origin(agent, request, context):
        return ()
    expected = {'owner_id': agent.home_paths.owner_id, 'owner_home': str(agent.home_paths.owner_home_dir),
        'request_id': _scheduler_run_id(request) or _session_task_run_id(request) or _session_message_run_id(request) or request.task_id,
        'run_id': identity['run_id'], 'attempt_id': identity['attempt_id'],
        'task_id': request.task_id, 'thread_id': request.thread_id,
        'source_thread_id': context.thread_id, 'entry_request_id': actor.entry_request_id,
        'conversation_id': actor.conversation_id, 'actor_id': actor.actor_id,
        'channel': context.channel, 'target': context.target}
    if identity['request_id'] != expected['request_id'] or identity['task_id'] != expected['task_id']:
        return ()
    verified = []
    for item in deliveries:
        binding = item.get('reply_binding') if isinstance(item, dict) else None
        if (not isinstance(binding, dict) or any(binding.get(key) != value for key, value in expected.items())
                or not binding.get('operation_id') or item.get('schema_version') != 'message_tool_delivery.v1'
                or item.get('delivery_status') != 'sent' or item.get('source_owner_delivery') is not True
                or item.get('channel') != context.channel or not item.get('receipt_id')
                or not (str(item.get('content') or '').strip() or item.get('attachments'))):
            continue
        evidence = {**item, 'reply_binding': {key: value for key, value in binding.items() if key != 'operation_id'}}
        try:
            operation = ManagedOperationStore(agent.subagents.runtime_db).get_tool_operation(
                owner_id=expected['owner_id'], run_id=expected['run_id'], task_id=expected['task_id'],
                attempt_id=expected['attempt_id'], operation_id=binding['operation_id'], tool_name='send_message')
        except ToolOperationStateError:
            continue
        if (operation is None or operation.status != 'succeeded' or operation.unknown_reason
                or operation.result.get('ok') is not True or operation.result.get('handler_executed') is not True
                or operation.result.get('result_envelope', {}).get('delivery_evidence') != evidence):
            continue
        verified.append(item)
    return tuple(verified)


# LLM: 报告资格只读精确事件类型、schema、发现编号和交付引用；准备、投递和唤醒确认共用此判据。
# 函数用途: 判断审计发现事件是否具有完整的用户报告事实。
def audit_finding_report_event(request: BackgroundDeliveryRequest) -> bool:
    if str(request.reason or "").strip().lower() != "audit_finding":
        return False
    wake = request.wake_signal if isinstance(request.wake_signal, dict) else {}
    metadata = wake.get("metadata") if isinstance(wake.get("metadata"), dict) else {}
    return (
        str(metadata.get("schema_version") or "") == "audit-finding-event.v1"
        and metadata.get("requires_llm_report") is True
        and bool(str(metadata.get("finding_id") or "").strip())
        and bool(background_delivery_evidence_refs(request))
    )


# LLM: 容量报告仅接受现行 schema 与显式 alert/recovered 状态，不能解析正文或迁移旧标签。
# 函数用途: 识别需要保留交付回执的审计容量通知。
def audit_capacity_report_event(request: BackgroundDeliveryRequest) -> bool:
    if str(request.reason or "").strip().lower() != "audit_capacity_alert":
        return False
    wake = request.wake_signal if isinstance(request.wake_signal, dict) else {}
    metadata = wake.get("metadata") if isinstance(wake.get("metadata"), dict) else {}
    return (
        str(metadata.get("schema_version") or "") == "audit-capacity-event.v2"
        and bool(str(metadata.get("audit_id") or "").strip())
        and str(metadata.get("capacity_state") or "").strip().lower() in {"alert", "recovered"}
    )


# LLM: 审计发现和容量报告保持原交付义务，只有真实提交才能确认，不能因静默投影吞掉通知。
# 函数用途: 汇总审计类唤醒的严格交付判据，供统一确认入口使用。
def audit_owner_report_event(request: BackgroundDeliveryRequest) -> bool:
    return audit_finding_report_event(request) or audit_capacity_report_event(request)


# LLM: 冻结载荷的读取必须 fail-closed：只接受本协议 schema、reason/task_id 与唤醒完全一致、
# 且"有正文或有附件"的载荷（纯附件回复同样合法）。v1 只有正文，v2 才有整封 envelope。
# 函数用途: 读取一条唤醒上已冻结的 owner 交付载荷，不匹配返回 None。
def cached_owner_delivery(signal: WakeSignal) -> dict[str, object] | None:
    raw_metadata = getattr(signal, "metadata", None)
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
    delivery = metadata.get("owner_delivery")
    if not isinstance(delivery, dict):
        return None
    if (
        str(delivery.get("schema_version") or "")
        not in {"wake-owner-delivery.v1", "wake-owner-delivery.v2"}
        or str(delivery.get("reason") or "") != str(signal.reason or "")
        or str(delivery.get("task_id") or "") != str(signal.root_task_id or "")
    ):
        return None
    has_body = bool(str(delivery.get("content") or "").strip())
    artifacts = delivery.get("delivery_artifacts")
    has_attachments = bool(
        [item for item in artifacts if isinstance(item, dict)] if isinstance(artifacts, list) else []
    )
    if not has_body and not has_attachments:
        return None
    return dict(delivery)


# LLM: 成功回执、来源交付标志和回执编号缺一不可；审计发现还须精确覆盖本次来源，不能按正文相似去重。
# 函数用途: 核验一条工具投递事实是否满足当前后台请求。
def _delivery_satisfies_request(
    request: BackgroundDeliveryRequest,
    delivery: dict[str, object],
) -> bool:
    if (
        str(delivery.get("delivery_status") or "").strip().lower() != "sent"
        or delivery.get("source_owner_delivery") is not True
        or not str(delivery.get("receipt_id") or "").strip()
    ):
        return False
    reason = str(request.reason or "").strip().lower()
    if reason == "scheduled_job_due":
        return True
    if reason != "audit_finding" or not audit_finding_report_event(request):
        return False
    delivered_refs = tuple(
        dict.fromkeys(
            str(ref).strip()
            for ref in (
                delivery.get("evidence_refs")
                if isinstance(delivery.get("evidence_refs"), list)
                else []
            )
            if str(ref).strip()
        )
    )
    required_refs = background_delivery_evidence_refs(request)
    return (
        bool(str(delivery.get("content") or "").strip())
        and len(delivered_refs) == len(required_refs)
        and set(delivered_refs) == set(required_refs)
    )


# LLM: 原因标签只由结构化请求类型生成，供落账和展示使用，不在此修改生命周期。
# 函数用途: 标注已由消息工具完成的交付来源，方便追溯重复发送问题。
def message_tool_delivery_reason(request: BackgroundDeliveryRequest) -> str:
    if str(request.reason or "").strip().lower() == "audit_finding":
        return "audit_finding_report"
    return "scheduled_message_tool_delivery"


# LLM: 调用方只传共核后且满足原reason/refs的回执；沿原external_sent冻结出口补canonical，失败重入不再次外发。
# 函数用途: 把同槽工具正文、附件和原证据引用整封保存到原来源会话，返回实际提交事实并保留原回执metadata。
def mirror_message_tool_deliveries(delivery, request, context, deliveries, *, delivery_reason):
    content = "\n\n".join(str(item.get('content') or '') for item in deliveries)
    artifacts = tuple(dict(ref) for item in deliveries for ref in item.get('attachments', ()) if isinstance(ref, dict))
    first = deliveries[0]
    refs = tuple(dict.fromkeys(str(ref) for item in deliveries for ref in item.get('evidence_refs', ()) if str(ref).strip()))
    metadata = _background_owner_message_metadata(request, delivery_reason=delivery_reason,
        projection_status='public', delivery_artifacts=artifacts,
        evidence_refs=refs, operation_verification=None, frozen_retry=None)
    metadata['message_tool_delivery'] = {'receipt_id': first['receipt_id'], 'delivery_status': 'sent',
        'deduplicated': first.get('deduplicated') is True}
    return record_background_response(delivery, request, context, content,
        delivery_artifacts=artifacts, deliver=True, delivery_reason=delivery_reason,
        frozen_retry=FrozenOwnerDelivery(content=content, external_sent=True,
            receipt_id=first['receipt_id'], message_metadata=metadata, evidence_refs=refs))


# LLM: 原产物只提供引用/hash/size；owner_root 由当前已验证宿主另行提供，不能从序列化JSON重建权限或内容。
# 函数用途: 投影已完成产物及当前宿主归属，冷重投仍须统一服务安全重读同版本字节，不读写文件。
def _channel_attachments(
    artifacts: tuple[dict[str, object], ...],
    *, owner_root: str = '',
) -> tuple[ChannelAttachment, ...]:
    attachments: list[ChannelAttachment] = []
    for item in artifacts:
        path = str(item.get("path") or "").strip()
        if not path or item.get("ok") is not True:
            continue
        try:
            size_bytes = max(0, int(item.get("size_bytes") or 0))
        except (TypeError, ValueError):
            size_bytes = 0
        attachments.append(
            ChannelAttachment(
                artifact_id=str(item.get("artifact_id") or ""),
                path=path,
                name=str(item.get("name") or Path(path).name),
                kind=str(item.get("kind") or "file"),
                sha256=str(item.get("sha256") or ""),
                size_bytes=size_bytes,
                owner_root=owner_root,
            )
        )
    return tuple(attachments)


# LLM: 声明与可用分离是本模块的硬边界：能力探测（supports_proactive）会被 adapter 生命周期影响，
# 归属判定必须读声明（declares_channel + declared_proactive）。改这里要同步检查
# delivery/registry.py::declares_channel 与 delivery/service.py 的同名投影。
# 函数用途: 判断一条后台路线的归属类别（本地/外部/未声明）。
def background_route_ownership(channels: object, channel: str) -> str:
    key = str(channel or "").strip().lower()
    if not key or channel_supports_transcript(channels, key):
        return _ROUTE_LOCAL
    if not _channel_declares_transport(channels, key):
        return _ROUTE_UNDECLARED
    return ROUTE_EXTERNAL if _channel_declares_proactive(channels, key) else _ROUTE_UNDECLARED


# LLM: 一条路线只有在"归属外部 + 有真实目标"时才欠用户一次外发。未声明通道、
# 无 target 的本地路线都不在此列，也不得被当成 IM 通道打开。
# 函数用途: 判断当前后台路线是否必须真正外发才算完成。
def _background_delivery_obligation(*, target: str, ownership: str) -> bool:
    return bool(str(target or "").strip() and ownership == ROUTE_EXTERNAL)


# LLM: 一条答复的落账归属：transcript 路线或未声明通道由 canonical 承担交付，
# 只有"声明外发 + 有目标"的路线才把 canonical 让给外发（靠冻结重投防丢失）。
# 类用途: 承载一次后台投递的路线事实。
@dataclass(frozen=True)
class _OwnerDeliveryRoute:
    transcript_route: bool
    ownership: str
    obligation: bool
    canonical_record: bool


# LLM: 证据引用归属随工作片类型与重投状态而定：重投用冻结载荷里的原引用，
#   审计报告片用审计引用，普通后台片用通用引用。调用点统一走这里，避免各算一套。
# 函数用途: 解析一条后台投递应携带的证据引用。
def _background_evidence_refs_for(
    delivery: BackgroundDeliveryDependencies,
    request: BackgroundDeliveryRequest,
    frozen_retry: FrozenOwnerDelivery | None,
) -> tuple[str, ...]:
    if frozen_retry is not None and frozen_retry.evidence_refs:
        return frozen_retry.evidence_refs
    if audit_finding_report_event(request):
        return background_delivery_evidence_refs(request)
    return background_evidence_refs(request)


# LLM: 空转片（Goal 无进展）的正文只进权威会话转录：不欠外部投递、不参与冻结重投、
#   也不能让它被当成普通外部回复。用结构化的路线改写表达这一事实，调用点不各自推算。
# 函数用途: 把一次后台投递的路线改写为“纯转录投影”或原样返回。
def _local_only_route(
    route: _OwnerDeliveryRoute,
    local_transcript_only: bool,
) -> _OwnerDeliveryRoute:
    if not local_transcript_only:
        return route
    return replace(
        route,
        transcript_route=True,
        canonical_record=True,
        obligation=False,
    )


# LLM: 路线事实只能由"投递边界声明 + 可信 context"推出；禁止在调用点各自推算，
# 否则 canonical 归属、冻结触发和唤醒确认会各算一套。
# 函数用途: 解析一次后台投递的路线事实。
def _background_delivery_route(
    channels: object,
    delivery_context: DeliveryContext,
    *,
    route_supports_transcript: bool | None = None,
) -> _OwnerDeliveryRoute:
    transcript_route = bool(
        supports_transcript_delivery(delivery_context.channel)
        if route_supports_transcript is None
        else route_supports_transcript
    )
    ownership = background_route_ownership(channels, delivery_context.channel)
    obligation = _background_delivery_obligation(
        target=delivery_context.target,
        ownership=ownership,
    )
    return _OwnerDeliveryRoute(
        transcript_route=transcript_route,
        ownership=ownership,
        obligation=obligation,
        canonical_record=bool(transcript_route or not obligation),
    )


# LLM: 没有可落账正文时的统一结果：正文只是给上层的投影，persisted 必须为 False。
# 函数用途: 构造一条未提交的投递结果（内部抑制/空正文/终态任务）。
def _uncommitted_delivery(content: str, delivery_status: str) -> BackgroundDeliveryCommit:
    return BackgroundDeliveryCommit(
        content=str(content or ""),
        delivery_status=delivery_status,
        persisted=False,
        commit_kind=delivery_status if delivery_status == "suppressed" else "none",
    )


# LLM: 唤醒确认是结构化事实判断，不再按事件类型默认放行：只有“外发成功”或“答复已经
# 落到自己的权威记录且这条路线本来就不欠外发”才算处理完成。既没送达也没记账的唤醒必须
# 留在队列里，由冻结重投路径补发正文，绝不重新调用模型。
# 函数用途: 判断一条后台唤醒是否已经真正完成了对 owner 的交付。
def background_owner_delivery_committed(
    request: BackgroundDeliveryRequest,
    *,
    target: str,
    ownership: str,
    commit: BackgroundDeliveryCommit,
    canonical_record: bool = False,
) -> bool:
    """Acknowledge owner-facing wakes only after their real delivery commit."""

    status = str(commit.delivery_status or "").strip().lower()
    if status == "sent":
        # 外发成功但这条路线本来要靠 canonical 承担交付时，本地落账失败同样不能确认：
        # 否则"用户读过的那份记录"永远缺一条，而且没人会再补。
        return bool(commit.persisted) or not canonical_record
    if status == "local_only":
        # 空转片正文只做本地转录，不欠外部投递；转录落账成功即完成，失败留待重试。
        return bool(commit.persisted) or not canonical_record
    if status == "suppressed":
        # 投递层显式判定这是内部协议内容：交付义务归零，重投只会得到同样结论。
        # 审计类唤醒沿用原有更严格判定，避免未上报的发现被静默吞掉。
        return not audit_owner_report_event(request)
    if _background_delivery_obligation(target=target, ownership=ownership):
        # 欠 owner 一次真实外发：canonical 记录只是安全网，不能替代送达。
        # 这里必须能"欠着不确认"，即使 adapter 当前不可用。
        return False
    if commit.persisted:
        return True
    # 既没有外发义务，也没有留下任何权威记录：唤醒不能确认，否则答复会静默消失。
    return False


# LLM: 生产与测试投递服务应显式声明 transcript 能力；旧测试替身没有该
# 方法时只回退到同一份内置路由声明，绝不从模型正文或 adapter 失败推断。
# 函数用途: 读取当前投递边界对本地权威会话交付的结构化能力。
def channel_supports_transcript(channels: object, channel: str) -> bool:
    probe = getattr(channels, "supports_transcript", None)
    if callable(probe):
        return bool(probe(channel))
    return supports_transcript_delivery(channel)


# LLM: 归属用的"部署声明"探测：优先问投递服务的 declares_channel；测试替身没有该方法时
# 回退到内置的 proactive 推送通道声明表，仍然只读声明、不探测 adapter 是否在线。
# 函数用途: 判断投递边界是否声明过该外发通道。
def _channel_declares_transport(channels: object, channel: str) -> bool:
    probe = getattr(channels, "declares_channel", None)
    if callable(probe):
        return bool(probe(channel))
    return str(channel or "").strip().lower() in PROACTIVE_PUSH_CHANNELS


# LLM: 声明能力与可用性分离：declared_proactive 读注册表里的能力声明，adapter 当前是否
# 构建成功、健康与否都不改变它。
# 函数用途: 判断该通道被声明为支持 proactive 外发。
def _channel_declares_proactive(channels: object, channel: str) -> bool:
    probe = getattr(channels, "declared_proactive", None)
    if callable(probe):
        return bool(probe(channel))
    return str(channel or "").strip().lower() in PROACTIVE_PUSH_CHANNELS
