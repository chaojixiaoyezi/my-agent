# LLM: 会话历史种子的只读来源：只引用同次冻结的原始行（canonical 地址视图或调用方已有的内存行）和宿主原有的单行投影规则；
# 不建第二历史库、不写索引、不在重放时重新读取权限或范围。只在原 native/text 准备边界解析成具体值，下游合同不变。
# 模块用途: 让三宿主把已裁决的完整历史延后到真正发送前才物化，避免准备阶段同时持有两份完整正文。
from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any

from .message_replay import MessageSnapshotRows, filtered_message_rows, message_rows_iterator
from .native_history import provider_history_messages_from_rows


# LLM: 每次迭代与随机访问都现读现投影，不缓存结果；切片保持同一规则，长度来自已冻结的行数。
# current_epoch 是冻结时刻，传给单行投影，使终态工具折叠的热尾/冷折叠选择不随解析时间变化。
# 类用途: 把原始行序列包装成"已投影行"的只读序列，供原生历史两遍回放按需读取。
@dataclass(frozen=True, eq=False)
class ProjectedHistoryRows(Sequence[Any]):
    rows: Sequence[Any]
    project_row: Callable[..., Any]
    current_epoch: float | None = None

    # LLM: 不读文件，行数只来自冻结选择。
    # 函数用途: 返回已选中行的数量。
    def __len__(self) -> int:
        return len(self.rows)

    # LLM: 单行读取沿原序列的完整校验；切片只复制地址或引用，不物化正文。
    # 函数用途: 按位置返回一条投影行，或返回同规则的子序列。
    def __getitem__(self, key):
        if isinstance(key, slice):
            return replace(self, rows=self.rows[key])
        return self.project_row(self.rows[key], current_epoch=self.current_epoch)

    # LLM: 原序列的读取器在提前退出或异常时必须关闭；每行投影后立即交出，不累积。
    # 函数用途: 顺序产生投影行，供文本种子和原生回放共用。
    def __iter__(self):
        with message_rows_iterator(self.rows) as rows:
            for row in rows:
                yield self.project_row(row, current_epoch=self.current_epoch)


# LLM: rows 是已按宿主原规则裁决过范围的冻结选择；project_row 必须是宿主原有的模块级单行投影函数。
# 与具体 messages/canonical_messages 互斥，由 ConversationHistorySeed 校验；不可序列化成持久状态。
# 相等比较按值：同一规则、同一冻结时刻且逐行内容相等才相等；不同时刻冻结的来源即使行相同也不相等，
# 要比较内容应比较两个边界的解析结果。地址视图比较会重读行，只适合测试与校验，不能放进热路径。
# 类用途: 承载一次准备确定的历史来源与投影规则，供发送边界重放。
@dataclass(frozen=True)
class ConversationHistorySource:
    rows: Sequence[Any]
    project_row: Callable[..., Any]
    # LLM: 冻结时刻；投影依赖时间的部分（终态工具折叠）一律按此时刻计算，解析多少次结果都相同。
    projected_at: float = 0.0
    # LLM: 宿主原 messages 规则是否保留正文为空的行（后台原实现保留，Gateway/child 过滤）；只影响文本解析。
    keep_empty_text: bool = False

    # LLM: 每次调用都返回新的只读投影视图，不缓存。
    # 函数用途: 取得按原规则、按冻结时刻投影后的历史行序列。
    def projected_rows(self) -> ProjectedHistoryRows:
        return ProjectedHistoryRows(self.rows, self.project_row, self.projected_at)


# LLM: select 只能是宿主原有的单行选择规则；地址视图去掉准备期的取消回调，重放取消由运行时自己的边界负责。
# 冻结时刻取调用时，与原具体种子在准备时投影一致。
# 函数用途: 在准备阶段冻结选择结果与投影时刻，只保留地址或原内存行引用，不物化正文。
def freeze_history_source(
    rows: Sequence[Any],
    *,
    project_row: Callable[..., Any],
    select: Callable[[Any], bool] | None = None,
    keep_empty_text: bool = False,
) -> ConversationHistorySource:
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        raise ValueError("complete history projection requires explicit source rows")
    selected = filtered_message_rows(rows, select) if select is not None else rows
    if isinstance(selected, MessageSnapshotRows):
        selected = replace(selected, interrupt_check=None)
    elif not isinstance(selected, tuple):
        selected = tuple(selected)
    return ConversationHistorySource(selected, project_row, time.time(), keep_empty_text)


# LLM: 与原具体种子的 messages 字段同规则：只保留 user/assistant 投影行，按宿主原规则决定是否保留空正文，保持原顺序。
# 函数用途: 在文本准备边界把来源解析成 (role, content) 序列。
def history_source_text_messages(source: ConversationHistorySource) -> tuple[tuple[str, str], ...]:
    messages: list[tuple[str, str]] = []
    for row in source.projected_rows():
        role = str(getattr(row, "role", "") or "").strip().lower()
        content = str(getattr(row, "content", "") or "")
        if role in {"user", "assistant"} and (content or source.keep_empty_text):
            messages.append((role, content))
    return tuple(messages)


# LLM: 与原具体种子的 canonical_messages 同规则：沿唯一 native_history 回放（信封合并、匿名信封、display 过滤），
# 输出已由 native_history 隔离，调用方无需再深拷贝。
# 函数用途: 在原生准备边界把来源解析成 provider 历史消息。
def history_source_provider_messages(source: ConversationHistorySource) -> tuple[dict[str, Any], ...]:
    return provider_history_messages_from_rows(source.projected_rows())


# LLM: 原生准备边界的唯一解析入口：来源按原 native 规则重放（输出已由 native_history 隔离），
# 具体种子保持原深拷贝隔离；两种形式对同一历史必须逐项相等，返回调用方独占的列表。
# 函数用途: 取得种子对应的 provider 历史消息，不含摘要前缀。
def seed_provider_history_messages(seed: object) -> list[dict[str, Any]]:
    source = getattr(seed, "source", None)
    if source is not None:
        return [item for item in history_source_provider_messages(source) if isinstance(item, dict)]
    return [deepcopy(item) for item in tuple(getattr(seed, "canonical_messages", ()) or ()) if isinstance(item, dict)]


# LLM: 文本准备边界的唯一解析入口：来源按原规则解析，具体种子原样返回；两种形式对同一历史逐项相等。
# 函数用途: 取得种子对应的 (role, content) 历史序列。
def seed_text_messages(seed: object) -> tuple[tuple[str, str], ...]:
    source = getattr(seed, "source", None)
    if source is not None:
        return history_source_text_messages(source)
    return tuple(getattr(seed, "messages", ()) or ())


__all__ = [
    "ConversationHistorySource",
    "ProjectedHistoryRows",
    "freeze_history_source",
    "history_source_provider_messages",
    "history_source_text_messages",
    "seed_provider_history_messages",
    "seed_text_messages",
]
