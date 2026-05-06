from __future__ import annotations

"""LLM contract: dataclasses for memory route indexes, route matches, and read receipts.

新手说明:
这里放的是长期规则路由的'票据格式'。你可以把 `MemoryRoute` 理解成一张导航卡：
它告诉程序'什么词触发我、应该去读哪个权威文件、这个规则适用于什么范围'。
"""

import time
from dataclasses import dataclass, field


@dataclass
class MemoryRoute:

    route_id: str
    topic: str
    trigger_keywords: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    when_to_read: str = ""
    authority_path: str = ""
    inject_mode: str = "on_hit"
    scope: str = "global"
    priority: int = 0
    stale_check: str = ""
    last_verified_at: str = ""
    source_file: str = ""
    source_path: str = ""

    def authority_file(self) -> str:

        return (self.source_file or self.authority_path).strip()

    def trigger_terms(self) -> list[str]:

        return _dedupe([*self.trigger_keywords, *self.aliases])


@dataclass
class MemoryRouteMatch:

    route: MemoryRoute
    score: float
    reasons: list[str] = field(default_factory=list)
    matched_terms: list[str] = field(default_factory=list)


@dataclass
class MemoryPathResolution:

    mode: str
    required_read_paths: list[str] = field(default_factory=list)
    candidate_paths: list[str] = field(default_factory=list)
    matches: list[MemoryRouteMatch] = field(default_factory=list)


@dataclass
class MemoryReadReceipt:

    route_id: str
    authority_path: str
    status: str
    reasons: list[str] = field(default_factory=list)
    read_at: float = 0.0
    content_hash: str = ""
    elapsed_ms: float = 0.0
    error: str = ""

    def mark_now(self) -> MemoryReadReceipt:

        if not self.read_at:
            self.read_at = time.time()
        return self


def _dedupe(items: list[str]) -> list[str]:

    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        cleaned = item.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result
