# LLM: 仅为同次冻结rows准备provider回放位置；不缓存native/正文、不持久化索引、不授予Compact覆盖，实际读取仍逐行校验hash。
# 模块用途: 让同次摘要多遍计量复用轻量信封位置，避免每遍重新解析全历史来选最后信封。
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from .compact_message_source import CompactMessageSource
from .display_checkpoint import is_display_checkpoint
from .message_replay import MessageSnapshotRows, message_rows_iterator
from .native_history import (
    _anonymous_row_key,
    _has_native_envelope,
    _legacy_row_message,
    _row_turn_identity,
    canonical_native_messages_from_metadata,
    iter_provider_history_messages_from_rows,
)


# LLM: rows是原地址序列的有序投影；envelopes仅存序号，所有公开消息每遍从原行重新隔离复制，早退关闭FD。
# 类用途: 重放已准备的原生消息次序，不保留任何消息正文。
@dataclass(frozen=True)
class NativeHistoryReplay:
    rows: Sequence
    envelopes: frozenset[int]

    # LLM: 原生与legacy仍共用原转换器；重放校验原文件内容，调用方投影不可污染下次读取。
    # 函数用途: 顺序生成独立provider消息，取消与提前关闭沿原rows合同。
    def __iter__(self):
        with message_rows_iterator(self.rows) as rows:
            for index, row in enumerate(rows):
                yield from _replay_row_messages(row, index in self.envelopes)


# LLM: 信封标记仅来自hash验证快照；输出仍由唯一normalizer隔离复制，legacy空值保持不发。
# 函数用途: 将单个冻结位置转换成公开消息，不让主循环叠加条件层数。
def _replay_row_messages(row, native):
    if native:
        return canonical_native_messages_from_metadata(getattr(row, 'metadata', None))
    legacy = _legacy_row_message(row)
    return (legacy,) if legacy is not None else ()


# LLM: 仅hash验证磁盘快照可缓存位置；普通可变行沿原迭代器每遍重建选择，不能拿旧信封标记解释变化后的正文。
# 函数用途: 冻结同次来源的原生回放位置，后续迭代无需重扫信封索引。
def prepare_native_history_replay(rows):
    selected = rows if isinstance(rows, Sequence) else tuple(rows)
    if not isinstance(selected, MessageSnapshotRows):
        return CompactMessageSource(lambda: iter_provider_history_messages_from_rows(selected))
    with message_rows_iterator(selected) as iterator:
        facts = [fact for index, row in enumerate(iterator) if (fact := _row_replay_fact(row, index)) is not None]
    envelopes = {key: index for key, index, native in facts if native}
    return _selected_replay(selected, facts, envelopes)


# LLM: 行对象离开首遍就丢弃；只保留结构化身份和位置，不保存任何正文副本。
# 函数用途: 为非展示行生成轻量回放事实。
def _row_replay_fact(row, index):
    if is_display_checkpoint(row):
        return None
    identity = _row_turn_identity(row)
    key = ('turn', identity) if identity else _anonymous_row_key(row, index)
    return key, index, _has_native_envelope(row)


# LLM: identified回合仅在首次位置发最后信封，匿名重复依旧逐条发最后信封；只能选择原地址，不重新排序native块。
# 函数用途: 将轻量位置计划变成可重复读取的同源地址投影。
def _selected_replay(selected, facts, envelopes):
    emitted, positions, native_positions = set(), [], set()
    for key, index, _ in facts:
        if key[0] == 'turn' and key in emitted:
            continue
        native = key in envelopes
        if native:
            native_positions.add(len(positions))
        if native and key[0] == 'turn':
            emitted.add(key)
        positions.append(envelopes.get(key, index))
    rows = replace(selected, addresses=tuple(selected.addresses[index] for index in positions))
    return NativeHistoryReplay(rows, frozenset(native_positions))
