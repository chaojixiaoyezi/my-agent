# LLM: 拒绝观察仅保留目标版本约束，不能保留可恢复正文或可晋升证据；与候选身份去重保持一致。
# 模块用途: 阻止同一被拒证据通过重放刷新正式目标版本。
"""拒绝观察仅保存宿主版本约束，不保留模型正文或可晋升证据。"""
from __future__ import annotations

import hashlib
from dataclasses import replace

from .candidate_models import CandidateObservation, utc_now_iso
from .candidate_target_binding import binding_history, validate_binding_history


# LLM: 纯合并用于提交预演及锁内事务；仅保存规范身份键/版本，终态discard不进入自动晋升和合法候选身份空间。
# 函数用途: 把被拒绝观察的版本责任写入同一候选账本中的无正文终态记录。
def retain_rejected_target_bindings(rows: list, bindings: tuple | list) -> list:
    validate_binding_history(list(bindings))
    result = list(rows)
    grouped: dict[str, list] = {}
    for binding in bindings:
        grouped.setdefault(binding['key'][0], []).append(binding)
    for target, history in grouped.items():
        cid = 'memory-candidate-binding-' + hashlib.sha256(target.encode()).hexdigest()[:24]
        current = next((row for row in result if row.candidate_id == cid), None)
        if current is None:
            current = _binding_record(cid)
            result.append(current)
        if current.candidate_type != 'discard' or current.status != 'rejected' or current.content:
            raise ValueError('invalid rejected target binding record')
        merged = replace(current, target_evidence_bindings=[*binding_history(current), *history])
        result[result.index(current)] = replace(merged, target_evidence_bindings=binding_history(merged))
    return result


# LLM: 独立命名空间ID由宿主选择，不能由模型候选生成；空正文与空来源只承担CAS约束，无学习或晋升资格。
# 函数用途: 新建一条没有失败内容副本的拒绝版本墓碑。
def _binding_record(candidate_id: str):
    from .candidates import merge_candidate_observations

    draft = CandidateObservation(candidate_type='discard', content='Host rejected observation version binding',
        subject_key='curator.rejected-version-binding', scope={'scope_type': 'personal', 'scope_key': 'personal'},
        origin='model_inferred', proposed_action='none', promotion_target='none')
    _, [record] = merge_candidate_observations([], [draft])
    now = utc_now_iso()
    return replace(record, candidate_id=candidate_id, content='', content_redacted_at=now,
        observation_keys=[], occurrence_count=0, status='rejected', reviewer='evidence_gate',
        review_note='CURATOR_REJECTED_VERSION_BINDING',
        status_history=[dict(status='rejected', actor='evidence_gate', note='CURATOR_REJECTED_VERSION_BINDING', at=now)])
