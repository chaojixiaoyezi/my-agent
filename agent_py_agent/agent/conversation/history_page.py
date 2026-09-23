# LLM: 历史页仅倒读同一canonical JSONL，共用store_io完整LF尾界；临时游标不进入模型或Compact，不改正文。
# 模块用途: 为历史显示和精确请求原文查找提供分页读取，保持同一工作片一起返回，调用方自行按结构化身份筛选。

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .models import MessageLogEntry
from .store_io import complete_jsonl_end


# LLM: 显示分组只依据持久身份字段；正文、时间和任务名称不得成为分组依据。
# 函数用途: 前台统一按Gateway请求、child按attempt分组；过程检查点不能把同片user/final切成多个页面。
def history_group_identity(row: object) -> str:
    metadata = getattr(row, "metadata", {})
    metadata = metadata if isinstance(metadata, dict) else {}
    if metadata.get("gateway_request_id") and not metadata.get("background_delivery_reason"):
        return str(metadata["gateway_request_id"])
    if metadata.get("agent_attempt_id"):
        return str(metadata["agent_attempt_id"])
    return str(
        metadata.get("background_transcript_request_id")
        or metadata.get("conversation_request_id")
        or metadata.get("gateway_request_id")
        or metadata.get("agent_attempt_id")
        or getattr(row, "message_id", "")
    )


# LLM: 两端偏移来自同一次文件快照，后到追加不改变本页；errors 非空时调用方不得推进任一游标。
# 类用途: 携带一页按时间排列的记录、更早入口和实时续读位置。
@dataclass(frozen=True)
class ConversationHistoryPage:
    rows: tuple[MessageLogEntry, ...] = ()
    before: int = 0
    after: int = 0
    errors: tuple[dict[str, str], ...] = ()


# LLM: 分页行数是目标窗口，最早的连续工作片不能截成两半；只倒读所选窗口及一个边界行，不全扫会话。
# 函数用途: 读取一页完整工作片；忽略尚未追加完整的尾行，损坏或错误游标则明确返回失败。
def read_conversation_history_page(
    path: Path, thread_id: str, *, before: int | None = None, limit: int = 80,
) -> ConversationHistoryPage:
    try:
        if not path.exists() and before in (None, 0):
            return ConversationHistoryPage()
        with path.open("rb") as handle:
            size = handle.seek(0, 2)
            end = complete_jsonl_end(handle, size) if before is None else int(before)
            if end < 0 or end > size:
                raise ValueError("history cursor outside transcript")
            if end:
                handle.seek(end - 1)
                if handle.read(1) != b"\n":
                    raise ValueError("history cursor must be a complete row boundary")
            selected: list[MessageLogEntry] = []
            start, oldest_group = end, ""
            for offset, line in _rows_backward(handle, end):
                raw = json.loads(line.decode("utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("invalid history row")
                row = MessageLogEntry.from_dict(raw)
                if row.thread_id != thread_id or not row.message_id or not row.role:
                    raise ValueError("invalid history identity")
                group = history_group_identity(row)
                if len(selected) >= max(1, min(800, int(limit))) and group != oldest_group:
                    break
                selected.append(row)
                start, oldest_group = offset, group
        return ConversationHistoryPage(tuple(reversed(selected)), start, end)
    except (OSError, ValueError, TypeError, UnicodeError) as exc:
        return ConversationHistoryPage(errors=({
            "error_code": "CLIENT_HISTORY_LOAD_FAILED", "error_type": type(exc).__name__,
        },))


# LLM: 反向块读取保留跨块 UTF-8/长行原字节；只在完整行处解码，偏移不以字符数计算。
# 函数用途: 从给定完整行末向前逐条产生消息及其起始字节位置。
def _rows_backward(handle: BinaryIO, end: int) -> Iterator[tuple[int, bytes]]:
    position, pending = end, b""
    while position:
        step = min(position, 65536)
        position -= step
        handle.seek(position)
        block = handle.read(step) + pending
        parts = block.split(b"\n")
        right = position + len(block)
        for line in reversed(parts[1:]):
            start = right - len(line)
            if line.strip():
                yield start, line
            right = start - 1
        pending = parts[0]
    if pending.strip():
        yield 0, pending
