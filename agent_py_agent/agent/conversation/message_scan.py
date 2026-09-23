# LLM: canonical消息的只读扫描原语；复用原文件字节地址，不建索引或覆盖证明，调用方持有写入/幂等锁。
# 模块用途: 提供固定尾界、按字节限额的完整行分页和低内存幂等查找，不修改历史或线程状态。
from __future__ import annotations

import json
from pathlib import Path
from typing import BinaryIO

from ..runtime_errors import DataCorruptionError, RecoverableRuntimeError
from .display_checkpoint import is_display_checkpoint
from .models import MessageLogEntry
from .store_io import complete_jsonl_end


# LLM: 超预算不代表canonical损坏；调用方按error_type处理，不能跳行、推进游标或当作空页结束。
# 类用途: 明确区分单行超过本次读取预算与JSON损坏，原文件保持可读。
class MessagePageBudgetExceeded(RecoverableRuntimeError):
    category = "resource_limit"


# LLM: 只检查物理LF与当前描述符长度，不能证明文件未被等长改写；调用方依赖canonical append-only合同。
# 函数用途: 校验字节位置是文件内完整记录边界，不读取或解释整行正文。
def _validate_boundary(handle: BinaryIO, offset: int, size: int) -> None:
    if offset < 0 or offset > size:
        raise DataCorruptionError("message cursor exceeds canonical transcript")
    if offset:
        handle.seek(offset - 1)
        if handle.read(1) != b"\n":
            raise DataCorruptionError("message cursor is not a complete row boundary")


# LLM: 在一次打开的描述符上冻结物理大小，以64KiB块倒扫完整LF；迟到追加留给下一快照，半行不取得覆盖。
# 函数用途: 找到此次扫描可用的最后完整记录位置，内存不随巨大未完成尾行增长。
def complete_message_offset(path: Path) -> int:
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return 0
    with handle:
        return complete_jsonl_end(handle, handle.seek(0, 2))


# LLM: through只限定同canonical文件的读取范围，不授予摘要覆盖；max_bytes限制物理页与单行，错误由Store包装。
# 函数用途: 读取一页完整消息；页内放不下的下一行留待下页，第一行超限则显式拒绝而不无限空转。
def read_message_page(
    path: Path, thread_id: str, *, after: int, limit: int,
    through: int | None, max_bytes: int | None,
) -> tuple[list[MessageLogEntry], int]:
    if through is not None and (type(through) is not int or through < after):
        raise ValueError("through must be an integer at or after the cursor")
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes <= 0):
        raise ValueError("max_bytes must be a positive integer")
    entries: list[MessageLogEntry] = []
    cursor = after
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        if after == 0 and through in (None, 0):
            return [], after
        raise
    with handle:
        size = handle.seek(0, 2)
        _validate_boundary(handle, after, size)
        end = through
        if end is None:
            end = complete_jsonl_end(handle, size) if max_bytes is not None else size
        if end < after:
            raise DataCorruptionError("canonical transcript ends before the message cursor")
        if through is not None:
            _validate_boundary(handle, end, size)
        handle.seek(after)
        while cursor < end and len(entries) < max(1, min(1000, int(limit))):
            remaining = end - cursor if max_bytes is None else min(end - cursor, max_bytes - (cursor - after))
            if remaining == 0:
                break
            line = handle.readline(min(end - cursor, remaining + 1))
            if len(line) > remaining:
                if not entries:
                    raise MessagePageBudgetExceeded("canonical message row exceeds page byte budget")
                break
            if not line.endswith(b"\n"):
                if through is not None or max_bytes is not None or handle.tell() < end:
                    raise DataCorruptionError("canonical transcript truncated during page read")
                break
            try:
                raw = json.loads(line.decode("utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("message row must be a JSON object")
                entry = MessageLogEntry.from_dict(raw)
            except (ValueError, TypeError, UnicodeError) as exc:
                raise DataCorruptionError("invalid canonical message row") from exc
            if entry.thread_id != thread_id:
                raise DataCorruptionError("invalid canonical message row")
            entries.append(entry)
            cursor = handle.tell()
    return entries, cursor


# LLM: 幂等检查需扫描固定物理EOF前全部行，即使命中也不提前返回；沿原规则跳空白/display，保留首个key；未终止LF拒绝，避免追加拼坏行。
# 函数用途: 用最多一行正文及首个匹配项的内存核对旧历史，调用方仍在原append-once锁内决定是否写入。
def find_message_dedupe(path: Path, key: str) -> MessageLogEntry | None:
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return None
    existing = None
    with handle:
        end = handle.seek(0, 2)
        handle.seek(0)
        while handle.tell() < end:
            line = handle.readline(end - handle.tell())
            if not line:
                raise DataCorruptionError("canonical transcript truncated during dedupe scan")
            if not line.endswith(b"\n"):
                raise DataCorruptionError("canonical transcript has an incomplete final row")
            if not line.strip():
                continue
            entry = MessageLogEntry.from_dict(json.loads(line.decode("utf-8")))
            if (existing is None and not is_display_checkpoint(entry)
                    and str(entry.metadata.get("dedupe_key") or "").strip() == key):
                existing = entry
    return existing
