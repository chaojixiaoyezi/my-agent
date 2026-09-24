# LLM: 消息追加、幂等提交和游标读取；只显式调用线程校验与活动更新，保留原 JSONL 和 display 边界；保持 canonical 文件、锁和错误报告合同。
# 模块用途: 消息追加、幂等提交和游标读取；只显式调用线程校验与活动更新，保留原 JSONL 和 display 边界。
from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from itertools import takewhile
from pathlib import Path
from typing import Any, TypeVar

from ..gateway_parts.io import (
    locked_file_transition,
)
from ..io.jsonl import append_jsonl
from ..runtime_errors import DataCorruptionError, runtime_error_report
from .display_checkpoint import (
    DISPLAY_CHECKPOINT_ROLE,
    display_checkpoint_event,
    is_display_checkpoint,
)
from .message_scan import complete_message_offset, find_message_dedupe, read_message_page
from .models import (
    ConversationThread,
    MessageLogEntry,
    new_id,
)
from .store_io import (
    iter_jsonl_tail_lines,
    json_row,
    jsonl_error,
    now,
    read_jsonl_tail_report,
)
from .store_layout import ConversationStorage

_Projected = TypeVar("_Projected")
_READ_CONTEXT = "conversation.messages.read"
_PARSE_CONTEXT = "conversation.messages.parse"


# LLM: 一个尾部窗口的扫描状态：只含投影值、错误和两个计数，不含正文；计数与 recent_report 的停止条件逐项对应。
# 类用途: 在逐行投影和翻倍重读之间传递窗口事实。
@dataclass
class _TailWindow:
    path: Path
    limit: int
    project: Callable[[MessageLogEntry], Any]
    projected: list[Any] = field(default_factory=list)
    json_errors: list[dict[str, Any]] = field(default_factory=list)
    parse_errors: list[tuple[int, dict[str, Any]]] = field(default_factory=list)
    rows: int = 0
    selected: int = 0

    # LLM: 与原实现错误同序：先 JSONL 坏行（文件顺序），后解析失败（row_index 为窗口内正向序号）。
    # 函数用途: 把倒序收集的错误换成原 recent_report 的顺序与行号。
    def errors(self) -> list[dict[str, Any]]:
        for reverse_index, report in self.parse_errors:
            report["row_index"] = self.rows - reverse_index + 1
        return [*reversed(self.json_errors), *(report for _index, report in reversed(self.parse_errors))]


# LLM: 与 recent_report 的单窗逐行处理相同：坏行记错不计行；解析失败记错计行；display 不计选中；只投影最新 limit 条。
# 函数用途: 处理倒读到的一行并更新窗口状态。
def _project_tail_line(window: _TailWindow, line: str) -> None:
    row, error = json_row(line, context=_READ_CONTEXT, path=window.path, line_number=0)
    if error is not None:
        window.json_errors.append(error)
        return
    window.rows += 1
    try:
        entry = MessageLogEntry.from_dict(row)
    except Exception as exc:
        window.parse_errors.append((window.rows, runtime_error_report(exc, context=_PARSE_CONTEXT)))
        return
    if is_display_checkpoint(entry):
        return
    window.selected += 1
    if window.selected <= window.limit:
        window.projected.append(window.project(entry))


# LLM: 与 recent_report 的单个窗口等价；读取错误时丢弃已投影结果，与原实现"读失败即空行加一条错误"一致；project 必须是纯投影。
# 函数用途: 倒序扫描一个尾部窗口，返回投影值（新到旧）、错误与两个计数，不驻留正文。
def _project_tail_window(path: Path, *, read_limit: int, limit: int, project: Callable[[MessageLogEntry], Any]) -> tuple[list[Any], list[dict[str, Any]], int, int]:
    window = _TailWindow(path, limit, project)
    try:
        for line in iter_jsonl_tail_lines(path, limit=read_limit):
            _project_tail_line(window, line)
    except OSError as exc:
        return [], [jsonl_error(exc, _READ_CONTEXT, path=path)], 0, 0
    return window.projected, window.errors(), window.rows, window.selected


# LLM: 与 read_jsonl_report 同源：文本模式通用换行、严格 UTF-8（解码错误照原样抛出）、跳过空白行、行号含空白行。
# 函数用途: 正向逐行产出（行号, 去掉换行的文本），供全量扫描两遍共用，不物化文件。
def _forward_lines(handle) -> Iterator[tuple[int, str]]:
    for line_number, line in enumerate(handle, start=1):
        text = line[:-1] if line.endswith("\n") else line
        text = text[:-1] if text.endswith("\r") else text
        if text.strip():
            yield line_number, text


# LLM: 全量校验的逐行状态；错误顺序与 recent_report(limit=0) 相同：先 JSONL 坏行（行序），后解析失败（row_index 正向）。
# 类用途: 第一遍只累积错误、行数和末行行号，不保留条目。
@dataclass
class _Validation:
    path: Path
    json_errors: list[dict[str, Any]] = field(default_factory=list)
    parse_errors: list[dict[str, Any]] = field(default_factory=list)
    rows: int = 0
    last_line: int = 0

    # 函数用途: 校验一行并更新状态。
    def check(self, line_number: int, text: str) -> None:
        self.last_line = line_number
        row, error = json_row(text, context=_READ_CONTEXT, path=self.path, line_number=line_number)
        if error is not None:
            self.json_errors.append(error)
            return
        self.rows += 1
        try:
            MessageLogEntry.from_dict(row)
        except Exception as exc:
            report = runtime_error_report(exc, context=_PARSE_CONTEXT)
            report["row_index"] = self.rows
            self.parse_errors.append(report)


# LLM: 第一遍全量校验，只保留错误与末行行号；文件身份供第二遍核对仍是同一个 canonical 文件。
# 函数用途: 返回（错误, 末行行号, 文件身份）。
def _validate_all(path: Path) -> tuple[list[dict[str, Any]], int, tuple[int, int]]:
    validation = _Validation(path)
    with path.open("r", encoding="utf-8") as handle:
        identity = _file_identity(handle)
        for line_number, text in _forward_lines(handle):
            validation.check(line_number, text)
    return [*validation.json_errors, *validation.parse_errors], validation.last_line, identity


# LLM: 第二遍只在同一文件上读到第一遍的末行，之后追加的行留给下次；已校验过的行不应失败，改写则按异常上抛。
# 函数用途: 正向产出已校验范围内的条目（含 display，由调用方过滤）。
def _validated_entries(path: Path, identity: tuple[int, int], last_line: int) -> Iterator[MessageLogEntry]:
    with path.open("r", encoding="utf-8") as handle:
        if _file_identity(handle) != identity:
            raise OSError("canonical transcript was replaced between validation and visit")
        for _line_number, text in takewhile(lambda item: item[0] <= last_line, _forward_lines(handle)):
            yield MessageLogEntry.from_dict(json.loads(text))


# 函数用途: 读取打开文件的设备号与 inode，用于确认两遍扫描读的是同一个 canonical 文件。
def _file_identity(handle) -> tuple[int, int]:
    info = os.fstat(handle.fileno())
    return info.st_dev, info.st_ino


# LLM: store_messages 的持久化合同：把 JSONL 对象逐条解析为消息，同时保留行号对应的解析错误；修改须同步本领域调用方与存储回归。
# 函数用途: 把 JSONL 对象逐条解析为消息，同时保留行号对应的解析错误。
def _message_entries(
    rows: list[dict[str, Any]],
) -> tuple[list[MessageLogEntry], list[dict[str, Any]]]:
    entries: list[MessageLogEntry] = []
    errors: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        try:
            entries.append(MessageLogEntry.from_dict(row))
        except Exception as exc:
            report = runtime_error_report(exc, context="conversation.messages.parse")
            report["row_index"] = index
            errors.append(report)
    return entries, errors


# LLM: Exact Memory evidence lookup must stop at the matching row while preserving JSONL corruption errors.
# 函数用途: 流式扫描单个 transcript 文件并返回目标消息，避免加载完整会话或在公开方法中堆叠嵌套分支。
def _message_entry_by_id_in_path(
    path: Path,
    target: str,
) -> tuple[MessageLogEntry | None, list[dict[str, Any]]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row, error = json_row(
                line,
                context="conversation.messages.by_id",
                path=path,
                line_number=line_number,
            )
            if error is not None:
                return None, [error]
            if row is None or str(row.get("message_id") or "") != target:
                continue
            try:
                return MessageLogEntry.from_dict(row), []
            except Exception as exc:
                return None, [
                    jsonl_error(
                        exc,
                        "conversation.messages.by_id",
                        path=path,
                        line_number=line_number,
                    )
                ]
    return None, []


# LLM: 消息追加、幂等提交和游标读取；只显式调用线程校验与活动更新，保留原 JSONL 和 display 边界；修改须核对直接调用方与原子存储测试。
# 类用途: 消息追加、幂等提交和游标读取；只显式调用线程校验与活动更新，保留原 JSONL 和 display 边界。
class MessageStore:
    # LLM: 只注入线程存在校验和原子更新；不能反向访问整棵 ConversationStore 或创建额外账本。
    # 函数用途: 绑定消息目录及写后活动更新能力，初始化不追加消息或改线程状态。
    def __init__(
        self,
        storage: ConversationStorage,
        *,
        require_thread: Callable[[str], ConversationThread],
        update_thread_atomic: Callable[
            [str, Callable[[ConversationThread], ConversationThread]], ConversationThread
        ],
    ) -> None:
        self.storage = storage
        self._require_thread = require_thread
        self._update_thread_atomic = update_thread_atomic

    # LLM: 历史页只从当前 store 的 canonical 路径倒读；不修改消息、Compact、模型上下文或实时消费位置。
    # 函数用途: 提供独立向前游标，让客户端按需补更早记录，而不是固定截断或全量载入会话。
    def history_page_report(self, thread_id: str, *, before: int | None = None, limit: int = 80):
        from .history_page import read_conversation_history_page

        return read_conversation_history_page(
            self.storage.message_path(thread_id), thread_id, before=before, limit=limit
        )

    # LLM: Transcript append is append-only; its activity projection must merge into the latest
    # thread after the ledger write rather than writing the earlier loaded thread snapshot.
    # 函数用途: 追加一条对话记录，并只刷新会话活动时间，避免迟到消息把新任务目录改回旧目录。
    def append(self, request: dict) -> MessageLogEntry:
        thread_id = str(request.get("thread_id") or "")
        thread = self._require_thread(thread_id)
        entry = MessageLogEntry(
            message_id=new_id("msg"),
            thread_id=thread.thread_id,
            role=str(request.get("role") or ""),
            content=str(request.get("content") or ""),
            channel=str(request.get("channel") or "internal"),
            channel_message_id=str(request.get("channel_message_id") or ""),
            created_at=now(request.get("now")),
            metadata=request.get("metadata") or {},
        )
        append_jsonl(self.storage.message_path(thread_id), entry.to_dict(), sort_keys=True)
        self._update_thread_atomic(
            thread.thread_id,
            lambda latest: replace(
                latest,
                updated_at=max(latest.updated_at, entry.created_at),
            ),
        )
        return entry

    # LLM: typed display只能进入本thread canonical账本；不更新updated_at、不索引Memory、不触及执行或Compact状态。
    # 函数用途: 逐块保存已公开过程；失败向上报告，不能把这类记录当助手回复或用来凑最近对话条数。
    def append_display_checkpoint(self, thread_id: str, metadata: dict) -> MessageLogEntry:
        thread = self._require_thread(thread_id)
        entry = MessageLogEntry(
            message_id=new_id("msg"),
            thread_id=thread.thread_id,
            role=DISPLAY_CHECKPOINT_ROLE,
            content="",
            created_at=now(None),
            metadata=metadata,
        )
        if display_checkpoint_event(entry) is None:
            raise ValueError("invalid canonical display checkpoint")
        append_jsonl(self.storage.message_path(thread.thread_id), entry.to_dict(), sort_keys=True)
        return entry

    # LLM: 原幂等锁内逐行核对完整历史、dedupe_key与内容；命中不跳过后续坏行，内存随最大行而非历史总量增长。
    # 函数用途: 在原幂等锁内核对 dedupe_key 与输入内容，只首次追加消息并刷新活动时间。
    def append_once(self, request: dict, *, dedupe_key: str) -> MessageLogEntry:
        """Append one transcript event exactly once across retries and process restarts."""
        thread_id = str(request.get("thread_id") or "").strip()
        key = str(dedupe_key or "").strip()
        if not thread_id or not key:
            raise ValueError("thread_id and dedupe_key are required")
        path = self.storage.message_path(thread_id)
        transition = path.with_name(f".{path.name}.append-once")
        with locked_file_transition(transition):
            try:
                existing = find_message_dedupe(path, key)
            except Exception as exc:
                raise DataCorruptionError(f"conversation transcript is unreadable: {thread_id}") from exc
            if existing is not None:
                if existing.role != str(request.get("role") or "") or existing.content != str(
                    request.get("content") or ""
                ):
                    raise DataCorruptionError(
                        f"conversation message dedupe key reused with different input: {key}"
                    )
                return existing
            metadata = request.get("metadata")
            request = {
                **request,
                "metadata": {
                    **(metadata if isinstance(metadata, dict) else {}),
                    "dedupe_key": key,
                },
            }
            return self.append(request)

    # LLM: store_messages 的持久化合同：读取最近真实对话，需要坏行报告时使用 recent_report，展示块不占消息数；修改须同步本领域调用方与存储回归。
    # 函数用途: 读取最近真实对话，需要坏行报告时使用 recent_report，展示块不占消息数。
    def recent(self, thread_id: str, *, limit: int = 20) -> list[MessageLogEntry]:
        entries, _errors = self.recent_report(thread_id, limit=limit)
        return entries

    # LLM: 模型/记忆读取排除display；逐步扩大尾读窗口，不能让大量过程块挤掉真正用户/助手消息。
    # 函数用途: 读取最近对话及损坏报告；完整显示分页使用独立原文件游标，不复用模型消息条数。
    def recent_report(
        self,
        thread_id: str,
        *,
        limit: int = 20,
    ) -> tuple[list[MessageLogEntry], list[dict[str, Any]]]:
        message_path = self.storage.message_path(thread_id)
        if not message_path.exists():
            return [], []
        read_limit = 0 if limit <= 0 else max(limit * 2, limit + 8)
        while True:
            report = read_jsonl_tail_report(
                message_path, context="conversation.messages.read", limit=read_limit
            )
            entries, parse_errors = _message_entries(report.rows)
            selected = [entry for entry in entries if not is_display_checkpoint(entry)]
            errors = [*report.load_errors, *parse_errors]
            if errors or limit <= 0 or len(selected) >= limit or len(report.rows) < read_limit:
                return (selected if limit <= 0 else selected[-limit:]), errors
            read_limit *= 2

    # LLM: 与 recent_report(limit) 同窗同错：同样的起始窗口与翻倍、同样的坏行与 row_index、同样只选最新 limit 条非 display；
    # 只把选中条目交给纯投影 project，返回投影值（新到旧）与错误，不驻留正文。修改须同步 recent_report 等价测试。
    # 函数用途: 给只要元数据的调用方（近期产物、追加与补写去重）读取最近消息，避免整块物化尾部正文。
    def recent_projection_report(
        self,
        thread_id: str,
        *,
        limit: int,
        project: Callable[[MessageLogEntry], _Projected],
    ) -> tuple[list[_Projected], list[dict[str, Any]]]:
        if limit <= 0:
            raise ValueError("recent projection needs a positive limit")
        path = self.storage.message_path(thread_id)
        if not path.exists():
            return [], []
        read_limit = max(limit * 2, limit + 8)
        while True:
            projected, errors, rows, selected = _project_tail_window(
                path, read_limit=read_limit, limit=limit, project=project,
            )
            if errors or selected >= limit or rows < read_limit:
                return projected, errors
            read_limit *= 2

    # LLM: 与 recent_report(limit=0) 同源同错；先完整校验一遍，有任何错误就一条都不交给 visitor（原实现先读全量再判断）。
    # 第二遍核对仍是同一文件，只读到第一遍的最后一行，之后追加的行留给下次；visitor 按正向顺序只收非 display 条目。
    # 函数用途: 顺序访问全部消息而不驻留全量正文，供搜索索引逐条投影；返回错误列表，非空时未访问任何条目。
    def visit_all_report(self, thread_id: str, visitor: Callable[[MessageLogEntry], object]) -> list[dict[str, Any]]:
        path = self.storage.message_path(thread_id)
        if not path.exists():
            return []
        try:
            errors, last_line, identity = _validate_all(path)
        except OSError as exc:
            return [jsonl_error(exc, _READ_CONTEXT, path=path)]
        if errors:
            return errors
        for entry in _validated_entries(path, identity, last_line):
            if not is_display_checkpoint(entry):
                visitor(entry)
        return []

    # LLM: 固定完整LF尾界供多页共享；只读、不产生Compact覆盖证明，半行和后续追加留待下次快照。
    # 函数用途: 获取原消息文件的完整行结束位置，失败时返回结构化错误，不把坏读当成空历史。
    def complete_offset_report(self, thread_id: str) -> tuple[int, list[dict[str, Any]]]:
        path = self.storage.message_path(thread_id)
        try:
            return complete_message_offset(path), []
        except Exception as exc:
            return 0, [jsonl_error(exc, "conversation.messages.snapshot", path=path)]

    # LLM: 同thread完整行游标可带固定through与页字节预算；错误保留原游标，超大单行不能假装空页或损坏。
    # 函数用途: 顺序读取canonical消息；旧显示调用保持原行为，有界宿主显式冻结尾界和页大小。
    def page_after_offset_report(
        self,
        thread_id: str,
        *,
        after: int = 0,
        limit: int = 100,
        through: int | None = None,
        max_bytes: int | None = None,
    ) -> tuple[list[MessageLogEntry], int, list[dict[str, Any]]]:
        path = self.storage.message_path(thread_id)
        cursor = max(0, int(after))
        try:
            entries, cursor = read_message_page(
                path, thread_id, after=cursor, limit=limit, through=through, max_bytes=max_bytes,
            )
            return entries, cursor, []
        except Exception as exc:
            return [], max(0, int(after)), [jsonl_error(exc, "conversation.messages.page", path=path)]

    # LLM: Memory Curator从精确message_id后读取对话；display行既不进模型也不占批量条数，物理读取仍顺序推进。
    # 函数用途: 跳过展示记录后收集有界真实消息；不改变调用方已消费的消息编号或正式记忆。
    def after_report(
        self,
        thread_id: str,
        *,
        after_message_id: str = "",
        limit: int = 100,
    ) -> tuple[list[MessageLogEntry], list[dict[str, Any]]]:
        bounded_limit = max(1, min(1_000, int(limit or 100)))
        path = self.storage.message_path(thread_id)
        if not path.exists():
            return [], []
        try:
            offset = (
                self.byte_offset_after(thread_id, after_message_id)
                if str(after_message_id or "").strip()
                else 0
            )
            rows: list[dict[str, Any]] = []
            errors: list[dict[str, Any]] = []
            with path.open("rb") as handle:
                handle.seek(offset)
                while len(rows) < bounded_limit and (line := handle.readline()):
                    if not line.strip():
                        continue
                    try:
                        text = line.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        errors.append(jsonl_error(exc, "conversation.messages.after", path=path))
                        break
                    row, error = json_row(
                        text,
                        context="conversation.messages.after",
                        path=path,
                        line_number=0,
                    )
                    if error is not None:
                        errors.append(error)
                        break
                    if row is not None and not is_display_checkpoint(row):
                        rows.append(row)
            entries, parse_errors = _message_entries(rows)
            return entries, [*errors, *parse_errors]
        except Exception as exc:
            return [], [jsonl_error(exc, "conversation.messages.after", path=path)]

    # LLM: Persona/Memory evidence verification must resolve one exact message_id without loading a whole transcript.
    # 函数用途: 流式查找一条 ConversationStore 消息并返回结构化读取错误。
    def by_id_report(
        self,
        thread_id: str,
        message_id: str,
    ) -> tuple[MessageLogEntry | None, list[dict[str, Any]]]:
        path = self.storage.message_path(thread_id)
        target = str(message_id or "").strip()
        if not path.exists() or not target:
            return None, []
        try:
            return _message_entry_by_id_in_path(path, target)
        except Exception as exc:
            return None, [jsonl_error(exc, "conversation.messages.by_id", path=path)]

    # LLM: Compact输入只含真实会话消息；display行仍留在canonical文件，压缩游标必须锚定真实消息而非展示块。
    # 函数用途: 读取未压缩的对话尾部，防止过程显示增加压缩次数或成为摘要/缓存输入。
    def after_compact_report(
        self,
        thread: ConversationThread,
    ) -> tuple[list[MessageLogEntry], list[dict[str, Any]]]:
        """Read only the append-only tail after a validated compact byte cursor."""
        offset = max(0, int(thread.compacted_through_byte_offset or 0))
        if offset <= 0:
            return self.recent_report(thread.thread_id, limit=0)
        path = self.storage.message_path(thread.thread_id)
        try:
            size = path.stat().st_size
            if offset > size:
                raise DataCorruptionError(
                    f"conversation compact byte cursor {offset} exceeds transcript size {size}"
                )
            with path.open("rb") as handle:
                handle.seek(offset)
                data = handle.read()
        except Exception as exc:
            return [], [jsonl_error(exc, "conversation.messages.after_compact", path=path)]
        rows: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for line in data.decode("utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            row, error = json_row(
                line,
                context="conversation.messages.after_compact",
                path=path,
                line_number=0,
            )
            if error is not None:
                errors.append(error)
            elif row is not None:
                rows.append(row)
        entries, parse_errors = _message_entries(rows)
        return [entry for entry in entries if not is_display_checkpoint(entry)], [
            *errors,
            *parse_errors,
        ]

    # LLM: store_messages 的持久化合同：扫描精确消息之后的字节位置，供 Compact 和增量读取沿原文件续读；修改须同步本领域调用方与存储回归。
    # 函数用途: 扫描精确消息之后的字节位置，供 Compact 和增量读取沿原文件续读。
    def byte_offset_after(self, thread_id: str, message_id: str) -> int:
        """Return the byte position immediately after a message in the raw ledger."""
        path = self.storage.message_path(thread_id)
        try:
            with path.open("rb") as handle:
                while line := handle.readline():
                    try:
                        row = json.loads(line.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeError) as exc:
                        raise DataCorruptionError(
                            f"conversation transcript contains an unreadable row: {path}"
                        ) from exc
                    if isinstance(row, dict) and str(row.get("message_id") or "") == message_id:
                        return handle.tell()
        except OSError as exc:
            raise DataCorruptionError(f"cannot read conversation transcript: {path}") from exc
        raise DataCorruptionError(
            f"conversation compact message cursor is missing from transcript: {message_id}"
        )
