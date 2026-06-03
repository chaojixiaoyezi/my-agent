from __future__ import annotations

"""Lightweight text similarity for self-learning draft dedupe."""

import re
from difflib import SequenceMatcher

_CONTAINMENT_RATIO_FOR_MATCH = 0.55
_LONG_CONTEXT_CHARS = 80
_LONG_CONTEXT_RATIO_CAP = 0.44
_CJK_STOP_CHARS = frozenset("的一是在和与及把让先后再时要做该个了有为到对中上")


def _normalize_learning_text(text: str) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "").strip().lower())
    normalized = re.sub(r"[^\w\u4e00-\u9fff ]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _word_shingles(words: list[str], size: int) -> set[str]:
    if len(words) < size:
        return set()
    return {" ".join(words[index : index + size]) for index in range(len(words) - size + 1)}


def _ngrams(value: str, size: int, *, prefix: str) -> set[str]:
    if not value:
        return set()
    if len(value) < size:
        return {f"{prefix}:{value}"}
    return {f"{prefix}{size}:{value[index : index + size]}" for index in range(len(value) - size + 1)}


def _compact_learning_chars(normalized: str) -> str:
    compact = normalized.replace(" ", "")
    return "".join(char for char in compact if char not in _CJK_STOP_CHARS)


def _char_learning_tokens(compact: str) -> set[str]:
    if not compact:
        return set()
    if len(compact) == 1:
        return {f"c:{compact}"}
    size = 2 if len(compact) <= _LONG_CONTEXT_CHARS else 3
    return _ngrams(compact, size, prefix="c")


def _learning_tokens(text: str) -> set[str]:
    normalized = _normalize_learning_text(text)
    words = [item for item in normalized.split(" ") if item]
    tokens = {f"w:{item}" for item in words}
    tokens.update(f"s3:{item}" for item in _word_shingles(words, 3))
    compact = _compact_learning_chars(normalized)
    tokens.update(_char_learning_tokens(compact))
    tokens.update(_ngrams(compact, 1, prefix="u"))
    return tokens


def _overlap_ratio(left_tokens: set[str], right_tokens: set[str], *, denominator: str) -> float:
    if not left_tokens or not right_tokens:
        return 0.0
    overlap = len(left_tokens & right_tokens)
    if denominator == "union":
        return overlap / max(1, len(left_tokens | right_tokens))
    if denominator == "min":
        return overlap / max(1, min(len(left_tokens), len(right_tokens)))
    return 2 * overlap / max(1, len(left_tokens) + len(right_tokens))


def _lexical_similarity(left: str, right: str) -> float:
    left_norm = _normalize_learning_text(left)
    right_norm = _normalize_learning_text(right)
    left_compact = _compact_learning_chars(left_norm)
    right_compact = _compact_learning_chars(right_norm)
    left_tokens = _learning_tokens(left_norm)
    right_tokens = _learning_tokens(right_norm)
    token_jaccard = _overlap_ratio(left_tokens, right_tokens, denominator="union")
    token_dice = _overlap_ratio(left_tokens, right_tokens, denominator="dice")
    unigram_coverage = _overlap_ratio(
        _ngrams(left_compact, 1, prefix="u"),
        _ngrams(right_compact, 1, prefix="u"),
        denominator="min",
    )
    bigram_coverage = _overlap_ratio(
        _ngrams(left_compact, 2, prefix="b"),
        _ngrams(right_compact, 2, prefix="b"),
        denominator="min",
    )
    sequence_ratio = SequenceMatcher(None, left_compact, right_compact).ratio()
    cjk_anchor = min(0.82, unigram_coverage * 0.62 + bigram_coverage * 0.52)
    return max(token_jaccard, token_dice * 0.84, sequence_ratio * 0.86, cjk_anchor)


def _length_skew_cap(left: str, right: str, score: float) -> float:
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    if not shorter or len(longer) < _LONG_CONTEXT_CHARS:
        return score
    ratio = len(shorter) / max(1, len(longer))
    if ratio >= _CONTAINMENT_RATIO_FOR_MATCH:
        return score
    return min(score, _LONG_CONTEXT_RATIO_CAP)


def _learning_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return round(_length_skew_cap(left, right, _lexical_similarity(left, right)), 4)


__all__ = ["_learning_similarity", "_learning_tokens", "_normalize_learning_text"]
