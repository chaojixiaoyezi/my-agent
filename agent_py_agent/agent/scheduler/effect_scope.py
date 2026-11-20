# LLM: 宿主副作用入口只消费原task/子代理root事实；普通任务不写scheduler，不从模型参数或文本推断定时身份。
# 模块用途: 在原模型与后台投递调用边界复用事件副作用lease，不改变原执行器和回执权威。
from __future__ import annotations

import hashlib
import json
import logging
from contextlib import contextmanager
from dataclasses import asdict


# LLM: 只有开始CAS明确未获准才抛此类型；运输已开始后的中断不能冒充not_started。
# 类用途: 让模型和工具出口保留同一结构化拒绝码。
class SchedulerEffectRejected(InterruptedError):
    # LLM: code只能由宿主选择，不含平台响应、任务正文或凭据。
    # 函数用途: 保存准确未开始错误码，不改变原运行或回执。
    def __init__(self, code):
        self.error_code = code
        super().__init__(code)


# LLM: 子代理根只取原SubAgentTask.root_id，当前runner上下文不允许以模型自报参数冒充其它run。
# 函数用途: 找到当前任务所属的原scheduler根，普通前台任务快速返回空。
def _scheduled_root(agent, task_id):
    value = str(task_id or '')
    from ..runtime_context import current_subagent_run_id

    child = current_subagent_run_id(agent)
    if not child:
        return value if value.startswith('srun_') else ''
    seen = set()
    while child and child not in seen:
        if child.startswith('srun_'):
            return child
        seen.add(child)
        try:
            task = agent.subagents.load(child)
        except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
            raise SchedulerEffectRejected('SCHEDULER_EFFECT_SOURCE_UNAVAILABLE') from exc
        if task is None:
            raise SchedulerEffectRejected('SCHEDULER_EFFECT_SOURCE_UNAVAILABLE')
        root = str(getattr(task, 'root_id', '') or '')
        child = root if root.startswith('srun_') else str(getattr(task, 'parent_id', '') or '')
    if value.startswith('srun_'):
        raise SchedulerEffectRejected('SCHEDULER_EFFECT_SOURCE_UNAVAILABLE')
    return ''


# LLM: lease只围住原调用生命周期而不持store锁；拒绝必须发生在backend/transport调用之前，finally不覆盖原异常。
# 函数用途: 为准确事件根取得开始资格，调用结束后同nonce释放；普通定时任务保持零写。
@contextmanager
def scheduler_effect_scope(agent, task_id, operation_key, *, kind, allow_terminal=False, replay_key=None):
    root = _scheduled_root(agent, task_id)
    repository = getattr(agent, 'scheduler_repository', None)
    if not root:
        yield
        return
    if repository is None:
        raise SchedulerEffectRejected('SCHEDULER_EFFECT_SOURCE_UNAVAILABLE')
    try:
        candidate = repository.effect_run(root)
    except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
        raise SchedulerEffectRejected('SCHEDULER_EFFECT_SOURCE_UNAVAILABLE') from exc
    if candidate is None:
        raise SchedulerEffectRejected('SCHEDULER_EFFECT_SOURCE_UNAVAILABLE')
    if candidate.get('event_source') is None and replay_key is None:
        yield
        return
    try:
        if replay_key is not None and candidate.get('claim_id'):
            lease = repository.begin_event_effect(
                candidate, replay_key, kind=kind, allow_terminal=allow_terminal, capture_receipt=True,
            )
        else:
            lease = repository.begin_event_effect(candidate, operation_key, kind=kind, allow_terminal=allow_terminal)
    except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
        raise SchedulerEffectRejected('SCHEDULER_EVENT_EFFECT_RETIRED') from exc
    try:
        yield lease if replay_key is not None else None
    finally:
        try:
            if replay_key is not None and lease is not None:
                repository.finish_event_effect(lease, receipt=lease.get('receipt'),
                                               reexecutable=lease.get('reexecutable'))
            else:
                repository.finish_event_effect(lease)
        except (OSError, RuntimeError, TypeError, ValueError, KeyError):
            logging.getLogger(__name__).warning('scheduler_event_effect_cleanup_pending')


def run_scheduler_delivery(agent, context, envelope, send, *, allow_terminal=False, request_key=None):
    """同一定时运行链按冻结投递键复用已发回执。"""
    from ..conversation.channels import ComponentDeliveryReceipt, DeliveryReceipt
    from ..delivery.service import ComponentDeliveryReceipt as HostDeliveryReceipt

    # 网关请求和 wake ID 会换代。操作键仍绑定实际收件方、回复范围和附件字节。
    actor_task = "" if str(context.task_id or "").startswith("srun_") else context.task_id
    payload = (actor_task, context.channel, context.target, context.mode, context.conversation_id, context.reply_to,
               context.reply_policy, context.provider_thread_id, context.root_id, context.parent_id,
               envelope.content, envelope.format,
               [(item.name, item.sha256 or item.artifact_id) for item in envelope.attachments], envelope.evidence_refs)
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    key = f"delivery-payload:{digest}"
    if request_key is not None:
        request_digest = hashlib.sha256(json.dumps([actor_task, request_key]).encode()).hexdigest()
        key = f"script-delivery:{request_digest}"
    with scheduler_effect_scope(agent, context.task_id, context.idempotency_key, kind='delivery',
                                allow_terminal=allow_terminal, replay_key=key) as lease:
        if lease is not None and 'replayed_receipt' in lease:
            fields = dict(lease['replayed_receipt'])
            if fields.pop('payload_digest', None) != digest:
                raise SchedulerEffectRejected('SCRIPT_DELIVERY_REQUEST_INVALID')
            receipt_type = HostDeliveryReceipt if fields.pop('host_delivery', False) else DeliveryReceipt
            components = fields.get('component_receipts', ())
            fields['component_receipts'] = (tuple(components) if receipt_type is HostDeliveryReceipt
                                           else tuple(ComponentDeliveryReceipt(**row) for row in components))
            for name in ('attachment_ids', 'evidence_refs'):
                fields[name] = tuple(fields.get(name, ()))
            return receipt_type(content=envelope.content, **fields)
        receipt = send()
        if lease is not None:
            if isinstance(receipt, DeliveryReceipt) and receipt.delivery_status == 'sent':
                lease['receipt'] = asdict(receipt)
                lease['receipt'].pop('content')
                lease['receipt']['payload_digest'] = digest
                if isinstance(receipt, HostDeliveryReceipt):
                    lease['receipt']['host_delivery'] = True
            else:
                lease['reexecutable'] = getattr(receipt, 'effect_outcome', '') == 'not_started'
        return receipt
