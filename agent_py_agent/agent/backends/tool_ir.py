# LLM: 原生消息保存调用、时间顺序及用户输入内部身份；身份不外发，也不成为权限的第二事实源。
# 模块用途: 定义主/子代理共用的原生历史记录类型，供后端翻译、Compact 和缓存续接使用。
from __future__ import annotations

"""Provider-neutral history containers for canonical tool calls and results.

这套 IR 是「provider 无关」的内部表达，用来无损描述一段工具循环历史：

    第 N 轮：assistant 文本 + 若干 ToolCall
    随后：与这些调用一一对应的 ToolResult
    期间：用户在同一运行 turn 里追加的 UserTurn
    压缩后：一条 CompactionSummary 替换已回收的旧工具往返

``ToolCall`` and ``ToolResult`` are deliberately imported from
``tooling.runtime_contracts``.  This module owns only chronology containers;
it must never define a second call/result authority.
"""

from dataclasses import dataclass, field
from typing import Any

from ..tooling.runtime_contracts import ToolCall, ToolResult


# LLM: 媒体保留已验证的内容引用，input_ids沿原插话包用于Compact释放；只向provider投影正文与媒体。
# 类用途: 保存原顺序的真实用户输入、附件及内部身份，不能由文本猜附件或输入消费。
@dataclass(frozen=True)
class UserTurn:
    """同一运行 turn 期间追加的一条真实用户输入。

    这不是运行时策略或系统提示。它用于保存 会话运行时 steer：用户在模型或工具
    正在工作时补充的输入必须留在原有工具往返历史中的准确时间位置，并在后续采样
    继续作为 ``role=user`` 可见，不能只在下一次请求临时出现一次。``input_ids``
    只用于内部按插话身份追踪和回收，供应商只看到文字和媒体。
    """

    text: str
    input_ids: tuple[str, ...] = ()
    media: tuple[dict[str, Any], ...] = ()


# LLM: 摘要仍非权威；source由原构造点赋值，候选只能按结构化来源替换，不解析正文，provider不发送该字段。
# 类用途: 保存模型可见的摘要与内部来源，供完整恢复保留媒体、用户插话和其它IR片段。
@dataclass(frozen=True)
class CompactionSummary:
    """当前运行 turn 的非权威压缩摘要。

    这不是第二份会话、任务状态或用户输入。它只是在同一份 native IR 历史里替换已经
    回收的旧工具往返，作用等同 会话运行时 history 里的 compaction item：后续模型轮持续
    可见，精确事实仍以 raw archive、operation ledger、artifact 和真实文件为准。
    """

    text: str
    source: str = ""


# LLM: RuntimeFactsTurn is a chronological provider-visible input item. Unlike UserTurn it is
# host-produced and cannot authorize work. source is an open host field, never parsed from text;
# Compact must retain the latest surviving snapshot for each source, including the legacy empty source.
# 类用途: 按来源保存某次调用前的运行事实；来源只用于变化比较和摘要回收，不外发或赋权。
@dataclass(frozen=True)
class RuntimeFactsTurn:
    text: str
    source: str = ""


# LLM: AssistantTurn 是原生工具历史的单一 assistant 事实；content_blocks 只保存后端白名单清洗后的有序块，不能直接作为用户正文。
# 类用途: 保存一轮模型的可见文字、工具调用，以及下一轮厂商协议要求回放的内部内容块。
@dataclass(frozen=True)
class AssistantTurn:
    """一轮 assistant 输出：可见文本 + 本轮发起的全部工具调用。

    一轮里文本与 tool_calls 至少有一个非空。Anthropic 协议下，文本与 tool_use 同处
    一条 assistant 消息，所以这里把它们绑在同一个容器里，方便适配器一次产出。
    """

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    content_blocks: list[dict[str, Any]] = field(default_factory=list)


__all__ = [
    "AssistantTurn",
    "CompactionSummary",
    "RuntimeFactsTurn",
    "ToolCall",
    "ToolResult",
    "UserTurn",
]
