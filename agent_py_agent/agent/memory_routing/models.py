# LLM: Memory routing module; keep context selection and read-receipt records stable.
# 模块用途: 根据任务上下文选择可注入记忆，并记录读取路径。

from __future__ import annotations

"""LLM contract: dataclasses for memory route indexes, route matches, and read receipts.

新手说明:
这里放的是长期规则路由的'票据格式'。你可以把 `MemoryRoute` 理解成一张导航卡：
它告诉程序'什么词触发我、应该去读哪个权威文件、这个规则适用于什么范围'。
"""

import time
from dataclasses import dataclass, field


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 MemoryRoute 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 MemoryRoute 的字段集合，在模块边界间传递结构化状态和结果。
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

    # LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 authority_file 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 authority file 在当前模块中的核心转换或协调步骤，衔接 memory routing 读取项目规则、路径和上下文片段来决定注入范围。
    def authority_file(self) -> str:

        return (self.source_file or self.authority_path).strip()

    # LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 trigger_terms 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 trigger terms 在当前模块中的核心转换或协调步骤，衔接 memory routing 读取项目规则、路径和上下文片段来决定注入范围。
    def trigger_terms(self) -> list[str]:

        return _dedupe([*self.trigger_keywords, *self.aliases])


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 MemoryRouteMatch 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 MemoryRouteMatch 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass
class MemoryRouteMatch:

    route: MemoryRoute
    score: float
    reasons: list[str] = field(default_factory=list)
    matched_terms: list[str] = field(default_factory=list)


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 MemoryPathResolution 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 MemoryPathResolution 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass
class MemoryPathResolution:

    mode: str
    required_read_paths: list[str] = field(default_factory=list)
    candidate_paths: list[str] = field(default_factory=list)
    matches: list[MemoryRouteMatch] = field(default_factory=list)


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 MemoryReadReceipt 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 MemoryReadReceipt 的字段集合，在模块边界间传递结构化状态和结果。
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

    # LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 mark_now 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 写入或登记 mark now 相关记录，集中处理目标路径、格式化和状态更新。
    def mark_now(self) -> MemoryReadReceipt:

        if not self.read_at:
            self.read_at = time.time()
        return self


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 _dedupe 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 dedupe 涉及的字段，让后续匹配和存储使用同一形态。
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
