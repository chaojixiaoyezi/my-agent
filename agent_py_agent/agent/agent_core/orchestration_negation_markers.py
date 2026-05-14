# LLM: Negation marker helpers keep natural-language guard checks from treating preserved bans as asks.
# 模块用途: 给派工约束检查复用“否定词前缀”判断，避免“不要写注释”被误读成“写注释”。

_NEGATION_PREFIXES = (
    "不要",
    "不得",
    "不能",
    "不准",
    "禁止",
    "避免",
    "别",
    "no ",
    "without ",
    "do not ",
    "don't ",
    "must not ",
)


# LLM: contains_unnegated_marker returns true only when a marker is not guarded by a nearby negation.
# 函数用途: 判断目标里是否真正“要求”某事；marker 前有不要/禁止/no/without 等否定词时不算冲突。
def contains_unnegated_marker(text: str, markers: tuple[str, ...]) -> bool:
    lowered = str(text or "").lower()
    return any(_has_unnegated_occurrence(lowered, marker) for marker in markers)


# LLM: _has_unnegated_occurrence scans repeated marker occurrences without deep nesting.
# 函数用途: 同一句话里 marker 可能出现多次，只要有一次不是否定语境，就算正向要求。
def _has_unnegated_occurrence(text: str, marker: str) -> bool:
    start = text.find(marker)
    while start >= 0:
        if not marker_has_negation_prefix(text, start):
            return True
        start = text.find(marker, start + len(marker))
    return False


# LLM: marker_has_negation_prefix recognizes compact Chinese/English negation near a marker.
# 函数用途: 检查 marker 前一小段是否出现“不要/禁止/without”等否定词。
def marker_has_negation_prefix(text: str, start: int) -> bool:
    prefix = text[max(0, start - 18) : start]
    return any(negation in prefix for negation in _NEGATION_PREFIXES)
