from __future__ import annotations

# LLM: Capability scoring module; keep routing tokenization and score weights deterministic.
# 模块用途: 为 skill/tool/MCP capability card 提供轻量关键词检索评分。
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .router import CapabilityCard


# LLM: score_card belongs to capability routing; keep score weights explainable and deterministic.
# 函数用途: 用可解释的关键词规则给能力卡打分。
def score_card(query: str, card: CapabilityCard) -> tuple[float, list[str]]:
    tokens = tokenize(query)
    if not tokens:
        return 0.0, []
    haystacks = {
        "name": card.name.lower(),
        "kind": card.kind.lower(),
        "description": card.description.lower(),
        "capabilities": " ".join(card.capabilities).lower(),
        "keywords": " ".join(card.keywords).lower(),
        "when_to_use": " ".join(card.when_to_use).lower(),
        "not_when_to_use": " ".join(card.not_when_to_use).lower(),
    }
    score = 0.0
    reasons: list[str] = []
    for token in tokens:
        token_score = 0.0
        if token in haystacks["name"]:
            token_score += 6.0
            reasons.append(f"命中名称'{token}'")
        if token in haystacks["capabilities"]:
            token_score += 5.0
            reasons.append(f"命中能力'{token}'")
        if token in haystacks["keywords"]:
            token_score += 4.0
            reasons.append(f"命中关键词'{token}'")
        if token in haystacks["kind"]:
            token_score += 2.0
            reasons.append(f"命中类型'{token}'")
        if token in haystacks["description"] or token in haystacks["when_to_use"]:
            token_score += 1.5
            reasons.append(f"命中描述'{token}'")
        score += token_score
    return score, _dedupe(reasons)


# LLM: tokenize keeps Chinese and ASCII capability search behavior stable.
# 函数用途: 把查询切成适合粗检索的 token。
def tokenize(text: str) -> list[str]:
    lowered = text.lower()
    tokens = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", lowered)
    expanded: list[str] = []
    for token in tokens:
        expanded.append(token)
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            expanded.extend(_chinese_ngrams(token))
    return _dedupe(expanded)


# LLM: _chinese_ngrams expands Chinese query tokens for coarse matching.
# 函数用途: 提取中文字符的 n-gram，提升中文短语召回。
def _chinese_ngrams(token: str) -> list[str]:
    ngrams: list[str] = []
    for size in (2, 3, 4):
        for idx in range(0, max(len(token) - size + 1, 0)):
            ngrams.append(token[idx : idx + size])
    return ngrams


# LLM: _dedupe preserves scoring reason order while removing repeated tokens.
# 函数用途: 保持顺序去重，避免重复关键词放大结果。
def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result
