from __future__ import annotations

"""LLM contract: dataclasses for memory route indexes, route matches, and read receipts.

新手说明:
这里放的是长期规则路由的“票据格式”。你可以把 `MemoryRoute` 理解成一张导航卡：
它告诉程序“什么词触发我、应该去读哪个权威文件、这个规则适用于什么范围”。
"""

import time
from dataclasses import dataclass, field


@dataclass
class MemoryRoute:
    """LLM contract: describes one index entry from a memory routing index to an authority file.

    新手说明:
    这就是长期规则的路牌。比如用户说“压缩前记忆怎么落盘”，
    这张卡可以告诉系统去读 `references/memory/compression.md`，而不是让模型凭印象猜。

    字段说明:
    route_id: 路由唯一 id，方便 receipt 和日志追踪。
    topic: 这条规则的主题。
    trigger_keywords: 触发关键词，用户输入包含这些词时可能命中。
    aliases: 别名或常见说法，权重通常比普通关键词更高。
    when_to_read: 人类可读的读取条件说明。
    authority_path: 真正应该读取的权威文件路径。
    scope: 适用范围，例如 global、project、module。
    priority: 优先级，分数相同时辅助排序。
    stale_check: 过期检查说明，提醒维护者何时复核。
    last_verified_at: 最近确认有效的时间文本。
    source_path: 这条 route 来自哪个 index 文件。
    """

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
        """LLM contract: return the effective authority/rule file for this route.

        新手说明:
        新字段叫 `source_file`，旧索引里还是 `authority_path`。
        这里统一兜底，保证加载器、matcher 和 doctor 不用到处写兼容逻辑。
        """

        return (self.source_file or self.authority_path).strip()

    def trigger_terms(self) -> list[str]:
        """LLM contract: returns normalized trigger terms in priority order for matching.

        新手说明:
        把关键词和别名合成一组可搜索的词，并且去掉空值和重复项。
        后面打分时会用这组词判断用户输入是不是踩中了这条规则。

        参数说明:
        这个方法没有输入参数，只读取当前 route 的 trigger_keywords 和 aliases。

        返回说明:
        返回去重后的触发词列表，顺序保持关键词在前、别名在后。
        """

        return _dedupe([*self.trigger_keywords, *self.aliases])


@dataclass
class MemoryRouteMatch:
    """LLM contract: stores an explainable match between user text and one memory route.

    新手说明:
    这是“为什么建议读这个规则文件”的证据单。它会保存分数、
    命中的词和推荐理由，方便日志、调试和以后给用户解释。

    字段说明:
    route: 被命中的 MemoryRoute。
    score: 匹配分数，越高越相关。
    reasons: 命中理由，例如“命中别名”。
    matched_terms: 具体命中的词。
    """

    route: MemoryRoute
    score: float
    reasons: list[str] = field(default_factory=list)
    matched_terms: list[str] = field(default_factory=list)


@dataclass
class MemoryPathResolution:
    """LLM contract: separates required authority paths from optional candidate paths.

    新手说明:
    路由命中之后，不是所有文件都必须立刻读。strict 模式会把高优先级文件放进
    `required_read_paths`，soft 模式只给候选，调用方可以自己决定读不读。

    字段说明:
    mode: 路由模式，soft 或 strict。
    required_read_paths: 必须读取的权威文件路径。
    candidate_paths: 候选文件路径，相关但不强制。
    matches: 原始匹配结果，保留用于解释和审计。
    """

    mode: str
    required_read_paths: list[str] = field(default_factory=list)
    candidate_paths: list[str] = field(default_factory=list)
    matches: list[MemoryRouteMatch] = field(default_factory=list)


@dataclass
class MemoryReadReceipt:
    """LLM contract: records that a routed memory authority file was read or attempted.

    新手说明:
    这是一张“读过规则文件”的小票。以后如果模型说自己按某个规则做了，
    我们可以靠这张小票查它到底读了哪个文件、为什么读、有没有失败。

    字段说明:
    route_id: 对应 MemoryRoute 的 id。
    authority_path: 尝试读取的权威文件路径。
    status: planned/read/error 等状态。
    reasons: 为什么需要读这个文件。
    read_at: 读取时间戳。
    content_hash: 读取内容 hash，方便检查后续是否变化。
    elapsed_ms: 读取耗时毫秒。
    error: 失败原因；成功时为空。
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

        新手说明:
        如果调用方没传时间，这里就自动补一个当前时间，避免后面日志里看不出
        这次读取发生在什么时候。

        参数说明:
        这个方法没有输入参数。

        返回说明:
        返回 self，方便调用方链式使用。

        副作用说明:
        如果 read_at 为空，会修改当前对象的 read_at。
        """

        if not self.read_at:
            self.read_at = time.time()
        return self


def _dedupe(items: list[str]) -> list[str]:
    """LLM contract: deduplicates strings while preserving their original order.

    新手说明:
    同一个关键词可能同时出现在 trigger 和 alias 里，这里只保留第一次出现的值，
    这样理由和命中词不会重复刷屏。

    参数说明:
    items: 原始字符串列表。

    返回说明:
    返回去掉空白和重复后的字符串列表，保留第一次出现顺序。
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
