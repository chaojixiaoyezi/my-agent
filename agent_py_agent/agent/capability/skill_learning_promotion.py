# LLM: 生产晋升复用原publisher/registry/锁；静态核验与真实执行证明分开，开关关闭仍只保留候选。
# 模块用途: 连接默认机器核验、结构载体选择、同族复用与安全发布。
from __future__ import annotations

from dataclasses import asdict, replace

from ..common.json_io import locked_json_path
from .skill_learning_validation import default_machine_promotion_verifier, verify_machine_candidate
from .skill_snapshot import skill_content_sha256


# LLM: 仅None取默认；callback按函数身份分类，支持假值/不可哈希callable；静态不签业务证明，原guard/CAS仍重核。
# 函数用途: 将机器检查合格的方法放入trial，记录真实核验级别及载体，绝不伪造用户确认。
def promote_verified_skill(store, publish, verifier=None):
    from . import skill_learning_host_feedback as feedback
    from .asset_promotion import business_validated, select_asset_carrier
    from .skill_learning_publish import (
        SkillLearningGateError,
        _redacted_publish,
        _stage_and_check,
        publish_learned_skill,
        render_learned_skill,
    )

    safe, redacted = _redacted_publish(publish)
    if redacted:
        return None
    structural = default_machine_promotion_verifier(safe)
    if structural.get('error_code') in {'MANUAL_ONLY_TARGET', 'MACHINE_DRAFT_NAME_INVALID'}:
        raise SkillLearningGateError(structural['error_code'])
    if structural['status'] != 'passed':
        return None
    with store.state_lock():
        _stage_and_check(store, safe, render_learned_skill(safe.decision))
    sha = skill_content_sha256(render_learned_skill(safe.decision))
    verifier = default_machine_promotion_verifier if verifier is None else verifier
    original = getattr(verifier, 'func', verifier)
    builtin = any(original is function for function in (feedback.builtin_machine_verifier, default_machine_promotion_verifier, verify_machine_candidate))
    if not builtin:
        feedback.remember_verified_candidate(store, safe, None, sha256=sha)
    receipt = verifier(safe, sha)
    feedback.remember_verified_candidate(store, safe, receipt, sha256=sha)
    if not isinstance(receipt, dict) or receipt.get('sha256') != sha or not receipt.get('receipt_id'):
        return None
    static = builtin and receipt == structural
    if not static and not business_validated(receipt):
        return None
    carrier = select_asset_carrier(asdict(safe.decision))
    proof = {'sha256':sha, 'receipt_id':receipt['receipt_id']}
    if static:
        proof.update(verification_kind='deterministic_static', checks_executed=True, executed=False, carrier=carrier)
    event = publish_learned_skill(store, replace(safe, promotion_authority='autonomous_verified', verification_receipt=proof))
    return replace(event, carrier=carrier)


# LLM: 显式开关关闭不进入核验；模型不能传callback，原family锁只串行同族读改写，不扩Owner/工具权限。
# 函数用途: 生产统一入口，依次尝试同族复用、默认机器晋升、保留待审候选。
def propose_or_promote_skill(store, publish, *, verifier=None, autonomous_verified=True):
    with locked_json_path(store.directory / '.families'):
        return _propose_or_promote_locked(store, publish, verifier, autonomous_verified)


# LLM: 只在原family锁内，正式复用仍核磁盘所有权；发布继续经原publisher，静态失败不得冒充发布成功。
# 函数用途: 串行处理同族候选及晋升，保持原限额和出处记录。
def _propose_or_promote_locked(store, publish, verifier, autonomous_verified):
    from .asset_promotion import (
        promote_verified_asset,
        register_asset,
        select_asset_carrier,
        task_family_fingerprint,
    )
    from .skill_learning_proposals import list_learning_proposals, propose_learning
    from .skill_learning_publish import (
        CODE_LIMIT_REACHED,
        SkillLearningGateError,
        _family_record_locked,
    )
    from .skill_learning_store import SkillLearningEvent

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
        promoted = promote_verified_asset(store.skills_root.parent, cand) if autonomous_verified else None
        if promoted is not None:
            return SkillLearningEvent('published', 'SKILL_LEARNING_AUTONOMOUS_PROMOTED', skill_name=promoted['asset_id'], carrier=carrier)
        event = propose_learning(store, publish)
        register_asset(store, kind=carrier, asset_id=cand['name'], asset=cand,
                       source={'origin': 'S3', 'proposal_id': event.reason}, state='candidate')
        return replace(event, carrier=carrier)
    family = task_family_fingerprint('skill', asdict(publish.decision))
    with store.state_lock():
        registry = store.load_registry()
        if (publish.decision.decision == 'create' and publish.max_skills > 0
                and len(registry.skills) >= publish.max_skills and publish.decision.name not in registry.skills):
            raise SkillLearningGateError(CODE_LIMIT_REACHED)
        reusable = _family_record_locked(store, registry, publish) if publish.decision.decision == 'create' else None
    if reusable is not None:
        return SkillLearningEvent('skipped', 'SKILL_LEARNING_FAMILY_REUSED', skill_name=reusable.name)
    pending = next((row for row in list_learning_proposals(store) if row['status']=='pending'
                    and task_family_fingerprint('skill', row['decision']) == family), None)
    event = promote_verified_skill(store, publish, verifier) if autonomous_verified else None
    if event is not None:
        return event
    if pending is not None:
        return SkillLearningEvent('skipped', 'SKILL_LEARNING_FAMILY_REUSED', skill_name=pending['decision']['name'])
    event = propose_learning(store, publish)
    register_asset(store, kind='skill', asset_id=publish.decision.name, asset=asdict(publish.decision),
                   source={'origin':'S3','proposal_id':event.reason}, state='candidate')
    return event
