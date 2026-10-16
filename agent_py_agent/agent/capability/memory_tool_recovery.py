# LLM: 只从已提交的删除墓碑恢复清理；不能借恢复路径重新授权正式写，普通操作仍交唯一晋升服务。
# 模块用途: 为记忆工具接续删除遗留清理并投影准确提交事实。
"""Remember真实入口的已提交删除恢复适配，不观察新候选或重新授权正式写入。"""
from types import SimpleNamespace

from ..memory_store.promotion_remove_recovery import (
    CommittedRemoval,
    prepare_committed_remove,
    recover_prepared_remove,
)
from ..tooling.models import ToolHandlerOutcome


# LLM: 仅在active目标确实不存在时读取墓碑；沿用原scope/subject校验，整批准备阶段零副作用。
# 函数用途: 把可恢复的删除转换为宿主冻结清理计划，其它错误原样返回。
def prepare_removal_retry(agent, operation, error, *, index):
    from .memory_tool import _validated_domain_fields

    if str(operation.get('action') or '').strip().lower() != 'remove' or error.error_code != 'MEMORY_ENTRY_NOT_FOUND':
        return error
    plan = prepare_committed_remove(getattr(agent, 'memory_promotion', None), str(operation.get('entry_id') or '').strip())
    if plan is None:
        return error
    candidate = plan.candidate
    target = SimpleNamespace(kind={'long_term_fact': 'fact', 'event': 'event', 'project': 'project'}.get(
        candidate.candidate_type, 'fact'), attributes=dict(candidate.scope, subject_key=candidate.subject_key))
    domain = _validated_domain_fields(agent, operation, target, index=index)
    return domain if isinstance(domain, ToolHandlerOutcome) else plan


# LLM: 新观察仍一次原子observe_qualified；恢复计划不得进入观察链重新获得证据或复活正文。
# 函数用途: 保持批次原顺序，把正常候选结果和只读恢复计划合并交回。
def observe_prepared(candidates, promotion, prepared):
    from ..memory_store.evidence_admission import observe_qualified

    drafts = [item for item in prepared if not isinstance(item, CommittedRemoval)]
    observed = iter(observe_qualified(candidates, getattr(promotion, 'tool_verifier', None), drafts) if drafts else ())
    return [item if isinstance(item, CommittedRemoval) else next(observed) for item in prepared]


# LLM: 恢复只能清理已提交墓碑；正常候选权限仍以持久化promotion_mode为准，不由batch形态授予。
# 函数用途: 为Remember的每条普通或恢复操作返回有scope的结构化结果。
def promote_prepared(promotion, item):
    from .memory_tool import _promotion_result_with_scope

    if isinstance(item, dict):
        return item
    if isinstance(item, CommittedRemoval):
        return _promotion_result_with_scope(recover_prepared_remove(promotion, item).to_dict(), item.candidate)
    if str(getattr(item, 'promotion_mode', '') or '').strip().lower() == 'auto_eligible':
        result = promotion.promote(item.candidate_id, automatic=True)
        return _promotion_result_with_scope(result.to_dict(), item)
    return _promotion_result_with_scope({
        'candidate_id': item.candidate_id, 'promoted': False, 'status': item.status,
        'reason_code': 'REVIEW_REQUIRED', 'promotion_ref': '',
    }, item)


# LLM: 仅依据逐条结构化原因码生成说明；清理历史墓碑不是新增正式写入，混合批次不得概括为全部成功。
# 函数用途: 删除收尾不能被描述为“已经记着”，未完成擦除也不能描述为正式记忆没变。
def removal_result_hint(results):
    pending = [row for row in results if row.get('reason_code') == 'REMOVE_CLEANUP_PENDING']
    if pending:
        changed = any(row.get('formal_memory_changed') is True for row in pending)
        timing = '本次正式删除已提交' if changed else '此前正式删除已提交'
        return timing + '，但隐私清理尚未完成；不要宣称完整成功。重试同一删除以接续清理，其它操作看逐条results。'
    recovered = [row for row in results if row.get('reason_code') == 'REMOVE_CLEANUP_RECOVERED']
    if recovered:
        return '已完成此前正式删除的遗留隐私清理，没有重新写入被删事实；混合批次的其它操作以逐条results为准。'
    return ''


# LLM: 带时区ISO/区间与正式epoch约束全批共用，普通操作和墓碑恢复都在候选写入之前拒绝坏参数。
# 函数用途: 前置核验完整时间合同，避免批次部分提交后才发现无效过期时间。
def validate_operation_times(operation, *, index):
    from ..memory_store.promotion import _expiry_epoch
    from .memory_tool import _memory_error

    try:
        start, end = [_operation_time(operation, key) for key in ('valid_from', 'valid_until')]
        if start and end and end <= start:
            raise ValueError('valid_until must be later than valid_from')
        if end is not None:
            _expiry_epoch(end.isoformat())
    except (ValueError, TypeError, OverflowError, OSError) as exc:
        return _memory_error(f'第 {index} 个操作的时间无效: {exc}', 'TOOL_INVALID_ARGUMENTS', not_started=True)
    return None


# LLM: 不补猜时区；仅对工具传入值做纯解析，历史候选归一化政策不变。
# 函数用途: 读取单个可选带时区ISO时间，无效值抛给整批参数检查。
def _operation_time(operation, key):
    from datetime import datetime

    text = str(operation.get(key) or '').strip()
    value = datetime.fromisoformat(text.replace('Z', '+00:00')) if text else None
    if value is not None and value.utcoffset() is None:
        raise ValueError(key + ' must include timezone')
    return value
