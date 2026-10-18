# LLM: 显式记忆服务只传递已装配的三个真实对象并承接原remember算法；不构造、复制、缓存或关闭仓库，不提供Agent形状或C许可。
# 模块用途: 让原Agent把用户明确保存交给同一Candidate与Promotion链，保持正式记忆身份、费用、锁和顺序。
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .candidates import CandidateService
    from .jsonl import JsonlMemory
    from .promotion import MemoryPromotionService


# LLM: 三字段必须引用调用方原CandidateService/MemoryPromotionService/JsonlMemory同一实例；束不拥有生命周期、不解析home/config/backend，不授写权限。
# 类用途: 将显式保存所需的真实服务一起传参，避免为调用业务而创建Agent替身。
@dataclass(frozen=True)
class ExplicitRememberServices:
    candidates: CandidateService
    promotion: MemoryPromotionService
    long_term: JsonlMemory


# LLM: 原kind/内容/digest/observation身份和observe→review→promote→formal查询必须保持；调用会写原候选/正式源并可能沿原索引发embedding，异常不包装。
# 函数用途: 将用户明确保存的事实、事件或项目按原审核晋升链落盘，返回原正式记录对象。
def remember_explicit(services: ExplicitRememberServices, content: str, *, kind: str = "fact"):
    from .candidate_models import CandidateObservation, MemoryScope

    normalized_kind = str(kind or "fact").strip().lower()
    type_map = {
        "fact": "long_term_fact",
        "event": "event",
        "project": "project",
    }
    if normalized_kind not in type_map:
        raise ValueError(
            "remember kind 只允许 fact/event/project；人格偏好请使用 update_persona"
        )
    text = str(content or "").strip()
    if not text:
        raise ValueError("remember content 不能为空")
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:20]
    candidate = services.candidates.observe(
        CandidateObservation(
            candidate_type=type_map[normalized_kind],
            content=text,
            subject_key=f"manual.{normalized_kind}.{digest}",
            scope=MemoryScope("personal", "personal", "本地用户明确保存", ""),
            origin="reviewed",
            confidence=1.0,
            proposed_action="add",
            promotion_target="long_term",
            observation_id=f"manual-remember:{digest}",
        )
    )
    if candidate.status == "pending_review":
        candidate = services.promotion.review(
            candidate.candidate_id,
            approved=True,
            reviewer="local-user-explicit",
            note="本地 remember 命令显式确认。",
        )
    result = services.promotion.promote(
        candidate.candidate_id,
        reviewer="local-user-explicit",
        confirmed=True,
    )
    if not result.promoted:
        raise RuntimeError(f"remember promotion failed: {result.reason_code}")
    entry_id = result.promotion_ref.rsplit("#", 1)[-1]
    for record in services.long_term.all():
        if record.entry_id == entry_id:
            return record
    raise RuntimeError("remember promotion succeeded without a formal memory record")
