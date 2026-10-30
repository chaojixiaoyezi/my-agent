# LLM: 只凭正式删除墓碑接续清理，不重放业务写入；quota→policy→candidate→formal锁序贯穿恢复。
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


# LLM: quota→policy→候选CAS→formal；已有admission透传全部清理，恢复不授予新业务删除。
# 函数用途: 恢复删除的全部隐私清理责任，连已标记但遗留索引的旧记录也补清。
def recover_committed_remove(service, candidate, reviewer) -> str:
    from .jsonl import _managed_policy_scope

    if not _removal_candidate(candidate):
        return ''
    enforcer = service.long_term.quota_enforcer or service.persona.quota_enforcer
    with (enforcer.admission() as admission, _managed_policy_scope(service.persona.owner_home),
          service.candidates.locked_snapshot(candidate)):
        hashes = service.long_term.committed_removal_hashes(candidate.target_entry_id,
            candidate.target_entry_version, f'candidate:{candidate.candidate_id}')
        if hashes is None:
            return ''
        from ._jsonl_indexing import removal_cleanup_phase

        with removal_cleanup_phase(formal_memory_changed=False):
            _purge_removal_history(service, candidate, admission, hashes)
            return _finish_removal_receipt(service, candidate, admission, reviewer)


# LLM: 仅由已持quota/policy/候选CAS且核对精确墓碑的恢复入口调用；准入传至派生清理，不能再取quota锁。
# 函数用途: 清掉删除后的派生索引与关联候选正文。
def _purge_removal_history(service, candidate, admission, hashes) -> None:
    service.long_term.purge_removed_indexes(candidate.target_entry_id, _admission=admission)
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


# LLM: 仅恢复approved精确replace/merge；quota→policy→candidate→formal锁内严格读正式/墓碑全账，坏源拒绝补回执。
# 函数用途: 正式更新成功但候选回执中断时，完整核对来源后补记；有坏行就保留approved，不冒认恢复成功。
def recover_committed_update(service, candidate, reviewer):
    from ..common.json_io import locked_json_path
    from .jsonl import _strict_memory_events
    from .promotion import (
        MemoryPromotionResult,
        _coordinator_policy_birth,
        _learning_exclusion_block,
        _learning_exclusion_scope,
    )

    if (candidate.status != "approved" or candidate.proposed_action not in {"replace", "merge"}
            or effective_promotion_target(candidate.promotion_target, candidate.origin, candidate.candidate_type) != "long_term"
            or type(candidate.target_entry_version) is not int or candidate.target_entry_version < 1):
        return None
    # 服务依赖必须仍在同一owner根；候选本身还会由原locked_snapshot做完整CAS。
    owner = service.persona.owner_home.resolve()
    try:
        service.candidates.path.resolve().relative_to(owner)
        service.long_term.path.resolve().relative_to(owner)
    except ValueError:
        return None
    enforcer = service.long_term.quota_enforcer or service.persona.quota_enforcer
    options = {"birth_commit": _coordinator_policy_birth} if service.coordinator_io else {}
    with enforcer.admission() as admission, _learning_exclusion_scope(service), service.candidates.locked_snapshot(candidate):
        with locked_json_path(service.long_term.path, **options):
            events = _strict_memory_events(service.long_term.path, require_source=True)
            latest = next((row for row in reversed(events) if row.entry_id == candidate.target_entry_id and row.action != "touch"), None)
            if not _same_committed_update(latest, candidate):
                return None
            blocked = _learning_exclusion_block(service, candidate)
            if blocked:
                return MemoryPromotionResult(candidate.candidate_id, False, candidate.status, blocked)
            ref = "memory/long_term/memory.jsonl#" + candidate.target_entry_id
            service.candidates.mark_promoted_admitted(candidate.candidate_id, admission, (reviewer, ref, "update_commit_recovered"))
    return MemoryPromotionResult(candidate.candidate_id, True, "promoted", "UPDATE_COMMIT_RECOVERED", ref)


# LLM: 比较最新非touch事件的全部写入身份；merge在唯一正式库中按replace存储，旧source/更高版本或任一元数据漂移均拒绝。
# 函数用途: 严格确认目标确由本候选、同一前序版本和规范化内容提交，不用同文相似性代替归属。
def _same_committed_update(record, candidate) -> bool:
    from .operations import memory_content_hash
    from .promotion import _expiry_epoch, _long_term_attributes, _long_term_kind

    return (record is not None and record.entry_id == candidate.target_entry_id
            and record.source == f"candidate:{candidate.candidate_id}"
            and record.version == candidate.target_entry_version + 1 and record.action == "replace"
            and memory_content_hash(record.content) == memory_content_hash(candidate.content)
            and record.kind == _long_term_kind(candidate.candidate_type)
            and record.tags == [candidate.candidate_type, candidate.origin]
            and record.attributes == _long_term_attributes(candidate)
            and record.expires_at == _expiry_epoch(candidate.valid_until))
