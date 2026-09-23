# LLM: 本模块只在原MessageStore固定尾界内选择已验证canonical消息；不创建索引、覆盖账本或权限，selector只可决定保留。
# 模块用途: 先收集消息身份与锚点位置，再流式筛掉范围外及已覆盖正文，避免先载入全部历史再裁剪。
from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from types import MappingProxyType

from ..runtime_errors import DataCorruptionError
from .display_checkpoint import is_display_checkpoint
from .message_scan import scan_message_snapshot
from .models import MessageLogEntry

MessageSelectorFactory = Callable[[Mapping[str, int]], Callable[[MessageLogEntry], bool]]


# LLM: 完整身份索引仍O消息数，正文只保留选中结果；retain_limit只用于明确展示窗口，Compact必须传0避免截断来源。
# 函数用途: 两次读同一完整LF范围并比较原字节hash，保持锚点缺失/范围外坏行事实，迟到追加留给下一次读取。
def select_message_snapshot(
    messages, thread_id: str, *, selector_factory: MessageSelectorFactory | None = None,
    exclude_message_ids: frozenset[str] = frozenset(),
    exclude_through_message_ids: tuple[str, ...] = (), retain_limit: int = 0,
) -> tuple[MessageLogEntry, ...]:
    through, errors = messages.complete_offset_report(thread_id)
    if errors:
        raise OSError("conversation transcript snapshot could not be read reliably")
    path = messages.storage.message_path(thread_id)
    positions: dict[str, int] = {}

    # LLM: 只保留非display消息身份；重复/空ID拒绝，覆盖或权限筛选不能掩盖坏canonical行。
    # 函数用途: 第一遍冻结完整来源的顺序和锚点位置，不保存正文。
    def inspect(row: MessageLogEntry) -> None:
        if is_display_checkpoint(row):
            return
        if not row.message_id or row.message_id in positions:
            raise DataCorruptionError("canonical message identities are missing or repeated")
        positions[row.message_id] = len(positions)

    original_hash = scan_message_snapshot(path, thread_id, through, inspect)
    if any(end not in positions for end in exclude_through_message_ids):
        raise OSError("legacy compact source boundary is missing from canonical history")
    covered_end = max((positions[end] for end in exclude_through_message_ids), default=-1)
    predicate = selector_factory(MappingProxyType(positions)) if selector_factory is not None else None
    if predicate is not None and not callable(predicate):
        raise TypeError("canonical message selector must be callable")
    selected: deque[MessageLogEntry] = deque(maxlen=retain_limit if retain_limit > 0 else None)

    # LLM: predicate不能换行/重排/增加来源，排除覆盖后只保留原解析对象；非bool选择属于调用错误。
    # 函数用途: 第二遍按同一位置事实筛正文，展示窗口可只保留末尾，但Compact默认保留全部合法来源。
    def collect(row: MessageLogEntry) -> None:
        if is_display_checkpoint(row):
            return
        allowed = predicate(row) if predicate is not None else True
        if type(allowed) is not bool:
            raise TypeError("canonical message selector must return bool")
        if allowed and positions.get(row.message_id, -1) > covered_end and row.message_id not in exclude_message_ids:
            selected.append(row)

    if scan_message_snapshot(path, thread_id, through, collect) != original_hash:
        raise DataCorruptionError("canonical message source changed during selection")
    return tuple(selected)
