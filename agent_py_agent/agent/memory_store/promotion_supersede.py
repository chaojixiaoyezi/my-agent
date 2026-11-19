from __future__ import annotations

"""conservative_v2：后台自动晋升时把“同主题唯一旧条目”的冲突改成绑定版本的替换。"""

# LLM: 只在 Curator 显式声明 conservative_v2 的自动晋升批次内生效；人工确认、remember 工具与默认 conservative_v1 不变。
#   判定只读结构化字段（来源、主题键、typed scope、有效期、版本、宿主核验过的消息时间），不解析正文语义。
# 模块用途: 判定并持久化安全的同主题自动替换，正式写入仍走原 replace 的版本 CAS 与崩溃恢复链。

import math
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from .candidate_models import MemoryScope
from .operations import memory_content_hash

# 新策略取值；默认 conservative_v1 不做自动替换。
AUTO_SUPERSEDE_POLICY = "conservative_v2"
# 自动替换成功时的晋升回执原因码。
AUTO_SUPERSEDED_REASON = "AUTO_SUPERSEDED_SAME_SUBJECT"

_AUTO_PROMOTION_POLICY: ContextVar[str] = ContextVar("memory_auto_promotion_policy", default="")


# LLM: 显式上下文而不是服务属性：同一晋升服务被多个入口共用，只有 Curator 批次知道当前生效的策略值。
# 函数用途: 在一段自动晋升调用内声明当前的自动晋升策略。
@contextmanager
def automatic_promotion_policy(policy: str) -> Iterator[None]:
    token = _AUTO_PROMOTION_POLICY.set(str(policy or "").strip().lower())
    try:
        yield
    finally:
        _AUTO_PROMOTION_POLICY.reset(token)


# LLM: 全部条件同时成立才替换：v2 自动批次、本候选正在自动晋升、唯一同主题冲突、已批准 user_explicit long_term add、
#   无其它冲突、双方都是永久有效、主题键与 typed scope 逐字相同、旧条目也是 user_explicit、旧版本是正整数、
#   宿主核验过的最新用户消息时间严格晚于旧条目更新时间。任一不满足返回 None，调用方照旧转 blocked_conflict。
#   绑定（replace + 目标 + 版本 + 清空冲突）先落盘再写正式库，崩溃后由 recover_committed_update 幂等补回执。
# 函数用途: 尝试把一次同主题冲突改成已持久化的版本绑定替换，返回新候选快照和旧条目身份。
def bind_same_subject_supersession(service, candidate, conflict, *, reviewer: str):
    from .candidates import CandidateStateChangedError
    from .promotion import _AUTOMATIC_PROMOTION, _promotion_target

    conflicts = tuple(conflict.conflicts_with)
    if (_AUTO_PROMOTION_POLICY.get() != AUTO_SUPERSEDE_POLICY
            or _AUTOMATIC_PROMOTION.get() != (id(service), candidate.candidate_id)
            or conflict.reason_code != "SUBJECT_SCOPE_CONFLICT_REQUIRES_REPLACE" or len(conflicts) != 1
            or candidate.status != "approved" or candidate.proposed_action != "add"
            or candidate.origin != "user_explicit" or _promotion_target(candidate) != "long_term"
            or candidate.conflicts_with or str(candidate.valid_until or "").strip()):
        return None
    try:
        current = service.long_term.read_ids([str(conflicts[0] or "").strip()])
    except ValueError:
        return None
    if len(current) != 1 or not _exact_permanent_subject(current[0], candidate):
        return None
    prior = current[0]
    if type(prior.version) is not int or prior.version < 1:
        return None
    evidence_at = _latest_verified_user_message_at(service, candidate)
    if evidence_at is None or not evidence_at > float(prior.updated_at or prior.created_at or 0.0):
        return None
    try:
        bound = service.candidates.bind_same_subject_replace(
            candidate, reviewer=reviewer, target_entry_id=prior.entry_id,
            target_entry_version=prior.version, note=AUTO_SUPERSEDED_REASON)
    except CandidateStateChangedError:
        return None
    if bound is None:
        return None
    return bound, {"entry_id": prior.entry_id, "version": prior.version,
                   "content_hash": "sha256:" + memory_content_hash(prior.content)}


# LLM: 逐字比较已保存的 subject_key 与 typed scope（不走别名等价），旧条目须为 user_explicit 且没有任何过期边界。
# 函数用途: 判断旧条目是否和候选属于同一主题、同一范围、同为永久的用户明确事实。
def _exact_permanent_subject(record, candidate) -> bool:
    attrs = record.attributes if isinstance(record.attributes, dict) else {}
    scope = MemoryScope.from_value(candidate.scope)
    return (bool(candidate.subject_key)
            and str(attrs.get("subject_key") or "") == candidate.subject_key
            and str(attrs.get("scope_type") or "") == scope.scope_type
            and str(attrs.get("scope_key") or "") == scope.scope_key
            and str(attrs.get("origin") or "") == "user_explicit"
            and not float(getattr(record, "expires_at", 0.0) or 0.0)
            and not str(attrs.get("valid_until") or "").strip())


# LLM: 时间只取宿主消息核验器返回的 created_at（核过 thread/role/hash/quote），不接受候选或模型自报时间。
# 函数用途: 返回本候选证据里最新一条已核验用户消息的时间，没有则返回 None。
def _latest_verified_user_message_at(service, candidate) -> float | None:
    times: list[float] = []
    for ref in candidate.source_message_refs:
        proof = service.message_verifier.verify(ref)
        created = proof.get("created_at") if isinstance(proof, dict) and proof.get("role") == "user" else None
        if isinstance(created, (int, float)) and not isinstance(created, bool) and math.isfinite(created):
            times.append(float(created))
    return max(times) if times else None


__all__ = [
    "AUTO_SUPERSEDED_REASON",
    "AUTO_SUPERSEDE_POLICY",
    "automatic_promotion_policy",
    "bind_same_subject_supersession",
]
