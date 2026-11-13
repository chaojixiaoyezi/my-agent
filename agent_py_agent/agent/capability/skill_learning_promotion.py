# LLM: 学习只在owner learned域发布；Verify锁外，短Commit重核原来源、归属/CAS、guard与配额，family不签复用。
# 模块用途: 连接确定性检查、精确候选身份及安全发布，不伪造用户确认或真实执行。
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace

from ..common.json_io import locked_json_path
from .skill_learning_validation import default_machine_promotion_verifier, verify_machine_candidate
from .skill_snapshot import skill_content_sha256


# LLM: 仅None取默认；复制输入并在Verify后复核hash，默认静态报告不签业务执行；最终publisher重核所有写门。
# 函数用途: 验证owner私有learned方法并发布trial，保留真实核验级别，不碰手写、内置或共享方法。
def promote_verified_skill(store, publish, verifier=None, *, commit=None):
    from . import skill_learning_host_feedback as feedback
    from .asset_promotion import business_validated
    from .skill_learning_publish import (
        CODE_LEARNING_DISABLED,
        SkillLearningGateError,
        _redacted_publish,
        _resolve_live_policy,
        _stage_and_check,
        publish_learned_skill,
        render_learned_skill,
    )

    safe, redacted = _redacted_publish(deepcopy(publish))
    if redacted:
        return None
    policy_enable, _, _, effective_guard = _resolve_live_policy(safe)
    if policy_enable is False:
        raise SkillLearningGateError(CODE_LEARNING_DISABLED, {"enable_self_learning": False})
    structural = default_machine_promotion_verifier(safe)
    if structural.get('error_code') in {'MANUAL_ONLY_TARGET', 'MACHINE_DRAFT_NAME_INVALID'}:
        raise SkillLearningGateError(structural['error_code'])
    if structural['status'] != 'passed':
        return None
    with store.state_lock():
        _stage_and_check(store, replace(safe, guard_config=effective_guard), render_learned_skill(safe.decision))
    sha = skill_content_sha256(render_learned_skill(safe.decision))
    verifier = default_machine_promotion_verifier if verifier is None else verifier
    original = getattr(verifier, 'func', verifier)
    builtin = any(original is function for function in (feedback.builtin_machine_verifier, default_machine_promotion_verifier, verify_machine_candidate))
    if not builtin:
        _commit(commit, lambda: feedback.remember_verified_candidate(store, safe, None, sha256=sha))
    receipt = verifier(safe, sha)
    if skill_content_sha256(render_learned_skill(safe.decision)) != sha:
        raise SkillLearningGateError('MACHINE_VERIFICATION_HASH_MISMATCH')
    _commit(commit, lambda: feedback.remember_verified_candidate(store, safe, receipt, sha256=sha))
    if not isinstance(receipt, dict) or receipt.get('sha256') != sha or not receipt.get('receipt_id'):
        return None
    static = builtin and receipt == structural
    if not static and not business_validated(receipt):
        return None
    proof = {'sha256': sha, 'receipt_id': receipt['receipt_id'], 'verification_kind': 'deterministic_static' if static else 'runtime_execution',
             'checks_executed': True, 'executed': not static, 'carrier': 'skill'}
    publication = replace(safe, promotion_authority='autonomous_verified', verification_receipt=proof)
    event = _commit(commit, lambda: publish_learned_skill(store, publication))
    return replace(event, carrier='skill')


# LLM: callback只同步在本次调用栈使用，不持久化；无宿主回调的直接入口仍由领域store原锁/guard/配额裁决。
# 函数用途: 在准备和验证完成后执行一个短写步骤。
def _commit(commit, operation):
    return commit(operation) if commit is not None else operation()


# LLM: 同owner准备锁不等于原C短门；Verify期间不占C，写入分别重进准确来源短门，不扩授权。
# 函数用途: 统一处理精确候选复用、机器晋升及保留提案。
def propose_or_promote_skill(store, publish, *, verifier=None, autonomous_verified=True, commit=None):
    with locked_json_path(store.directory / '.families'):
        return _propose_or_promote_locked(store, deepcopy(publish), verifier, autonomous_verified, commit)


# LLM: 精确ID/hash而非family决定复用；静态失败不签业务证明，最终发布原锁内重核磁盘所有权。
# 函数用途: 串行处理候选，保持出处、数量与原审批来源。
def _propose_or_promote_locked(store, publish, verifier, autonomous_verified, commit=None):
    from .asset_promotion import (
        asset_identity,
        promote_verified_asset,
        register_asset,
        select_asset_carrier,
    )
    from .skill_learning_proposals import list_learning_proposals, propose_learning
    from .skill_learning_publish import (
        CODE_LEARNING_DISABLED,
        CODE_LIMIT_REACHED,
        SkillLearningGateError,
        _family_record_locked,
        _resolve_live_policy,
    )
    from .skill_learning_store import (
        CODE_EVIDENCE_INCOMPLETE,
        CODE_MAINTENANCE_OBSERVE_ONLY,
        SkillLearningEvent,
        SkillLearningStoreError,
    )

    req = publish.request if isinstance(publish.request, dict) else {}
    extra = {k: req[k] for k in ('kind', 'code', 'entry_source', 'steps', 'io_contract', 'sample_inputs',
                               'entry_document', 'files', 'application_scope', 'analysis_handler') if k in req}
    if isinstance(req.get('candidate_asset'), dict):
        extra.update(req['candidate_asset'])
    if isinstance(getattr(publish.decision, 'candidate_asset', None), dict):
        extra.update(publish.decision.candidate_asset)
    if extra and select_asset_carrier({**asdict(publish.decision), **extra}) != 'skill':
        cand = {**asdict(publish.decision), **extra}
        carrier = select_asset_carrier(cand)
        promoted = promote_verified_asset(store.skills_root.parent, cand, commit=commit) if autonomous_verified else None
        if promoted is not None:
            return SkillLearningEvent('published', 'SKILL_LEARNING_AUTONOMOUS_PROMOTED', skill_name=promoted['asset_id'], carrier=carrier)
        event = _commit(commit, lambda: propose_learning(store, publish))
        _commit(commit, lambda: register_asset(store, kind=carrier, asset_id=cand['name'], asset=cand,
            source={'origin': 'S3', 'proposal_id': event.reason}, state='candidate'))
        return replace(event, carrier=carrier)
    policy_enable, policy_mode, effective_max, _ = _resolve_live_policy(publish)
    if policy_enable is False:
        raise SkillLearningGateError(CODE_LEARNING_DISABLED, {"enable_self_learning": False})
    identity = asset_identity('skill', asdict(publish.decision), store.skills_root.parent)
    with store.state_lock():
        registry = store._prepare_clean_registry_locked(store.load_registry())
        try:
            managed = store.managed_records(registry, include_archived=False)
        except SkillLearningStoreError as exc:
            if exc.code == CODE_EVIDENCE_INCOMPLETE:
                raise SkillLearningGateError(CODE_EVIDENCE_INCOMPLETE, exc.detail) from exc
            raise
        live = registry.live_count(managed_names=managed.keys())
        if (publish.decision.decision == 'create' and effective_max > 0
                and live >= effective_max and publish.decision.name not in registry.skills):
            if policy_mode == 'observe':
                raise SkillLearningGateError(
                    CODE_MAINTENANCE_OBSERVE_ONLY,
                    {"limit": effective_max, "live": live, "mode": "observe"},
                )
            # 预检只做保守验证，绝不在 preflight 淘汰现役技能；
            # 若存在实时 protection_provider，最终准入与 R-2 置换统一延迟到 publish_learned_skill 短锁内完成。
            if not (autonomous_verified and publish.protection_provider is not None):
                raise SkillLearningGateError(CODE_LIMIT_REACHED, {"limit": effective_max, "live": live})
        reusable = _family_record_locked(store, registry, publish) if publish.decision.decision == 'create' else None
    if reusable is not None:
        return SkillLearningEvent('skipped', 'SKILL_LEARNING_FAMILY_REUSED', skill_name=reusable.name)
    pending = next((row for row in list_learning_proposals(store) if row['status'] == 'pending'
                    and asset_identity('skill', row['decision'], store.skills_root.parent) == identity), None)
    event = promote_verified_skill(store, publish, verifier, commit=commit) if autonomous_verified else None
    if event is not None:
        return event
    if pending is not None:
        return SkillLearningEvent('skipped', 'SKILL_LEARNING_FAMILY_REUSED', skill_name=pending['decision']['name'])
    event = _commit(commit, lambda: propose_learning(store, publish))
    _commit(commit, lambda: register_asset(store, kind='skill', asset_id=publish.decision.name, asset=asdict(publish.decision),
        source={'origin': 'S3', 'proposal_id': event.reason}, state='candidate'))
    return event
