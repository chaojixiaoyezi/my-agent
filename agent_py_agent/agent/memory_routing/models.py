from __future__ import annotations

"""LLM contract: dataclasses for memory route indexes, route matches, and read receipts.

这里放的是长期规则路由的“票据格式”。你可以把 `MemoryRoute` 理解成一张导航卡：
它告诉程序“什么词触发我、应该去读哪个权威文件、这个规则适用于什么范围”。
"""

import time
from dataclasses import dataclass, field


@dataclass
class MemoryRoute:
    """LLM contract: describes one index entry from a memory routing index to an authority file.

    大白话：这就是长期规则的路牌。比如用户说“压缩前记忆怎么落盘”，
    这张卡可以告诉系统去读 `references/memory/compression.md`，而不是让模型凭印象猜。
    """

    route_id: str
    topic: str
    trigger_keywords: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    when_to_read: str = ""
    authority_path: str = ""
    scope: str = "global"
    priority: int = 0
    stale_check: str = ""
    last_verified_at: str = ""
    source_path: str = ""

    def trigger_terms(self) -> list[str]:
        """LLM contract: returns normalized trigger terms in priority order for matching.

        大白话：把关键词和别名合成一组可搜索的词，并且去掉空值和重复项。
        后面打分时会用这组词判断用户输入是不是踩中了这条规则。
        """

        return _dedupe([*self.trigger_keywords, *self.aliases])


@dataclass
class MemoryRouteMatch:
    """LLM contract: stores an explainable match between user text and one memory route.

    大白话：这是“为什么建议读这个规则文件”的证据单。它会保存分数、
    命中的词和推荐理由，方便日志、调试和以后给用户解释。
    """

    route: MemoryRoute
    score: float
    reasons: list[str] = field(default_factory=list)
    matched_terms: list[str] = field(default_factory=list)


@dataclass
class MemoryPathResolution:
    """LLM contract: separates required authority paths from optional candidate paths.

    大白话：路由命中之后，不是所有文件都必须立刻读。strict 模式会把高优先级文件放进
    `required_read_paths`，soft 模式只给候选，调用方可以自己决定读不读。
    """

    mode: str
    required_read_paths: list[str] = field(default_factory=list)
    candidate_paths: list[str] = field(default_factory=list)
    matches: list[MemoryRouteMatch] = field(default_factory=list)


@dataclass
class MemoryReadReceipt:
    """LLM contract: records that a routed memory authority file was read or attempted.

    大白话：这是一张“读过规则文件”的小票。以后如果模型说自己按某个规则做了，
    我们可以靠这张小票查它到底读了哪个文件、为什么读、有没有失败。
    """

    route_id: str
    authority_path: str
    status: str
    reasons: list[str] = field(default_factory=list)
    read_at: float = 0.0
    content_hash: str = ""
    elapsed_ms: float = 0.0
    error: str = ""

    def mark_now(self) -> "MemoryReadReceipt":
        """LLM contract: fills `read_at` with the current timestamp when it is empty.

        大白话：如果调用方没传时间，这里就自动补一个当前时间，避免后面日志里看不出
        这次读取发生在什么时候。
        """

        if not self.read_at:
            self.read_at = time.time()
        return self


def _dedupe(items: list[str]) -> list[str]:
    """LLM contract: deduplicates strings while preserving their original order.

    大白话：同一个关键词可能同时出现在 trigger 和 alias 里，这里只保留第一次出现的值，
    这样理由和命中词不会重复刷屏。
    """

    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        cleaned = item.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result
