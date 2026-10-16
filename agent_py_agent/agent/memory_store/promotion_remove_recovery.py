# LLM: 只凭正式删除墓碑接续清理，不重放正式写入；原记录缺失或IO错误不能假判清理成功。
# 模块用途: 在重启和故障后收拾已删除记忆的候选与派生残留。
"""只凭正式删除墓碑修复候选和派生层收尾，不重放任何正式写入。"""
from dataclasses import dataclass

from .candidate_models import MemoryCandidate, effective_promotion_target


# LLM: 仅宿主从正式墓碑和同owner候选构造，模型不能提供该类型或候选快照。
# 类用途: 在全批验证阶段携带一份已提交删除的待清理责任。
@dataclass(frozen=True)
class CommittedRemoval:
    candidate: MemoryCandidate
    record: object


# LLM: 只认精确entry/version/source三元组；候选脱敏状态不撤销已提交墓碑的清理责任，读取无副作用。
# 函数用途: 让真实Remember入口在active条目消失后仍能找到旧删除责任。
def prepare_committed_remove(service, entry_id: str) -> CommittedRemoval | None:
    if service is None or not entry_id:
        return None
    record = service.long_term.committed_removal_record(entry_id)
    if record is None or not record.source.startswith('candidate:'):
        return None
    try:
        candidate = service.candidates.get(record.source.removeprefix('candidate:'))
    except KeyError:
        return None
    if (not _removal_candidate(candidate) or candidate.target_entry_id != entry_id
            or candidate.target_entry_version != record.version - 1):
        return None
    return CommittedRemoval(candidate, record)


# LLM: 仅做删除形状筛选，资格由调用方核对精确正式墓碑；不以候选终态取消旧责任或授予新写入。
# 函数用途: 判断候选是否可能持有正式删除清理责任。
def _removal_candidate(candidate) -> bool:
    return (candidate.proposed_action == 'remove'
        and effective_promotion_target(candidate.promotion_target, candidate.origin, candidate.candidate_type) == 'long_term')


# LLM: 已持候选提交锁；历史关联只认正式promotion_ref或精确target，不按正文/主题猜条目。
# 函数用途: 从旧格式墓碑对应的候选历史补回遗失的删除hash计划。
def _legacy_removal_hashes(service, candidate) -> set[str]:
    from .promotion import _long_term_entry_id

    entry_id = candidate.target_entry_id
    ref = 'memory/long_term/memory.jsonl#' + entry_id
    rows = service.candidates._load_unlocked()
    return {row.content_hash for row in rows if row.content_hash and (
        (row.proposed_action == 'add' and _long_term_entry_id(row.candidate_id) == entry_id)
        or row.promotion_ref in {ref, ref + ':removed'} or (
            row.target_entry_id == entry_id and effective_promotion_target(
                row.promotion_target, row.origin, row.candidate_type) == 'long_term'
            and (row.target_entry_version is None or row.target_entry_version <= candidate.target_entry_version)))}


# LLM: quota→候选CAS→精确当前墓碑；候选和派生清理失败皆报告旧责任，恢复不授予新写入或依赖晋升开关。
# 函数用途: 恢复删除的全部隐私清理责任，连已标记但遗留索引的旧记录也补清。
def recover_committed_remove(service, candidate, reviewer) -> str:
    if not _removal_candidate(candidate):
        return ''
    enforcer = service.long_term.quota_enforcer or service.persona.quota_enforcer
    with enforcer.admission() as admission, service.candidates.locked_snapshot(candidate):
        hashes = service.long_term.committed_removal_hashes(candidate.target_entry_id,
            candidate.target_entry_version, f'candidate:{candidate.candidate_id}')
        if hashes is None:
            return ''
        from ._jsonl_indexing import removal_cleanup_phase

        with removal_cleanup_phase(formal_memory_changed=False):
            _purge_removal_history(service, candidate, admission, hashes)
            return _finish_removal_receipt(service, candidate, admission, reviewer)


# LLM: 仅由已持admission、候选CAS锁并核对精确墓碑的恢复入口调用；失败由外层归为旧清理责任。
# 函数用途: 清掉删除后的派生索引与关联候选正文。
def _purge_removal_history(service, candidate, admission, hashes) -> None:
    service.long_term.purge_removed_indexes(candidate.target_entry_id)
    history = set(hashes) or _legacy_removal_hashes(service, candidate)
    service.candidates.redact_content_admitted(admission, {
        'content_hashes': history, 'exclude_candidate_ids': {candidate.candidate_id},
    })


# LLM: 调用方持候选CAS及quota锁，已完成隐私清理；不生成新正式记录，仅补齐旧删除回执。
# 函数用途: 未脱敏的删除候选补记终态，已脱敏的保留原终态。
def _finish_removal_receipt(service, candidate, admission, reviewer) -> str:
    ref = 'memory/long_term/memory.jsonl#' + candidate.target_entry_id + ':removed'
    if candidate.content_redacted_at and not candidate.content:
        return ref
    service.candidates.mark_promoted_admitted(candidate.candidate_id, admission, (reviewer, ref, ''))
    return ref


# LLM: 只消费准备阶段的冻结候选；恢复失配绝不能退回普通promote而产生新的正式写入。
# 函数用途: 返回真实入口清理结果，失败仍保留原候选和墓碑责任。
def recover_prepared_remove(service, plan: CommittedRemoval):
    from ._jsonl_indexing import RemovalCleanupError
    from .candidates import CandidateStateChangedError
    from .promotion import MemoryPromotionResult

    candidate = plan.candidate
    try:
        ref = recover_committed_remove(service, candidate, 'remember-remove-recovery')
    except CandidateStateChangedError as exc:
        return MemoryPromotionResult(candidate.candidate_id, False, exc.status, 'CANDIDATE_CHANGED_AFTER_VERIFICATION')
    except RemovalCleanupError:
        return MemoryPromotionResult(candidate.candidate_id, False, candidate.status, 'REMOVE_CLEANUP_PENDING')
    code = 'REMOVE_CLEANUP_RECOVERED' if ref else 'REMOVE_RECEIPT_CHANGED'
    return MemoryPromotionResult(candidate.candidate_id, bool(ref), 'promoted' if ref else candidate.status, code, ref)


# LLM: promote仅把已读候选交给恢复；清理报错沿用原快照，不重读故障账本、不产生正式新提交。
# 函数用途: 将历史删除恢复映射成独立回执，无匹配墓碑时继续正常晋升。
def recover_promotion_result(service, candidate, reviewer):
    from ._jsonl_indexing import RemovalCleanupError
    from .promotion import MemoryPromotionResult

    try:
        ref = recover_committed_remove(service, candidate, reviewer)
    except RemovalCleanupError as exc:
        return MemoryPromotionResult(candidate.candidate_id, False, candidate.status, 'REMOVE_CLEANUP_PENDING',
                                     formal_memory_changed=exc.formal_memory_changed)
    if ref:
        return MemoryPromotionResult(candidate.candidate_id, True, 'promoted', 'REMOVE_CLEANUP_RECOVERED', ref)
    return None
