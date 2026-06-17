
from __future__ import annotations

"""可配置记录切割器 —— 把读到的字节流切成"逻辑记录"(对应 SourceProfile.splitter)。

为什么要它:日志不都是一行一条。Java 堆栈、pretty-print JSON、多行事件,按 \\n 硬切会把
一条记录撕碎,下游规则就匹配不上。让 LLM 探查后指定切割方式,daemon 照做。

统一抽象 split(text, flush_trailing) -> (records, consumed_bytes):
  - consumed_bytes 决定采集 offset 推进多少;未消费的尾巴留到下次(增量不丢的关键)。
  - flush_trailing=True(文件 mtime 静默=写完了)时,末尾未闭合的记录也算完整 flush。
    line 的"残行"和 multiline 的"未闭合记录"是同一个概念——边界未定的尾巴。
  - splitter 是无状态纯函数:跨 offset 边界的未完成记录靠"不推进 offset"留到下次,
    而非在状态里存缓冲。状态文件仍只是单个 offset,历来的不丢机制全部复用。

标杆调研结论:长期助手/通道运行时/ECC 都没有可配置日志切割,这套是自建。
"""

import re
from typing import Any, Protocol


class RecordSplitter(Protocol):
    def split(self, text: str, *, flush_trailing: bool) -> tuple[list[str], int]:
        """切出完整记录列表 + 已消费字节数。"""
        ...


def _nonempty(items: list[str]) -> list[str]:
    return [item for item in items if item.strip()]


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


class LineSplitter:
    """按行切(默认)。末尾无换行的尾段:flush_trailing 时当完整最后一行采,否则当残行留到下次。

    这就是文件源历来的按行语义(含 mtime 静默兜底——修过的 folder 末行无换行被永久丢的真 bug)。
    json_lines 复用它(每行一条),JSON 字段提取在 triage 层(profile.fields),切割行为相同。
    """

    def split(self, text: str, *, flush_trailing: bool) -> tuple[list[str], int]:
        if not text:
            return [], 0
        last_nl = text.rfind("\n")
        if last_nl == -1:
            # 整段无换行:静默→是完整最后一行采;活跃→残行不采,offset 不前进。
            if flush_trailing and text.strip():
                return _nonempty(text.splitlines()), _utf8_len(text)
            return [], 0
        trailing = text[last_nl + 1 :]
        if flush_trailing and trailing.strip():
            return _nonempty(text.splitlines()), _utf8_len(text)
        complete = text[: last_nl + 1]
        return _nonempty(complete.splitlines()), _utf8_len(complete)


class MultilineStartSplitter:
    """按"记录起始行"正则合并多行。匹配 start_pattern 的行开启新记录,后续不匹配的行
    (如堆栈帧)并入当前记录。covers Java 堆栈 / 时间戳开头的多行事件。

    consumed:到最后一条**已闭合**记录的末尾。最后一条(可能还在增长)不推进,除非 flush_trailing。
    """

    def __init__(self, start_pattern: str):
        self._pat = re.compile(start_pattern)

    def split(self, text: str, *, flush_trailing: bool) -> tuple[list[str], int]:
        if not text:
            return [], 0
        lines = text.splitlines(keepends=True)
        closed: list[str] = []  # 已闭合的完整记录(保留换行,用于精确算字节)
        current: list[str] = []  # 当前正在累积的记录
        for line in lines:
            if self._pat.search(line) and current:
                closed.append("".join(current))
                current = [line]
            else:
                current.append(line)
        consumed = sum(_utf8_len(rec) for rec in closed)
        if flush_trailing and current and "".join(current).strip():
            closed.append("".join(current))
            consumed += _utf8_len("".join(current))
        records = [rec.rstrip("\n") for rec in closed if rec.strip()]
        return records, consumed


class DelimiterSplitter:
    """按分隔符切(空行 \\n\\n / --- / 自定义)。最后一段没遇到下一个分隔符=未闭合,留到下次。"""

    def __init__(self, separator: str):
        self._sep = separator or "\n\n"

    def split(self, text: str, *, flush_trailing: bool) -> tuple[list[str], int]:
        if not text:
            return [], 0
        parts = text.split(self._sep)
        if len(parts) == 1:
            # 还没出现过分隔符:整段是一条未闭合记录。
            if flush_trailing and text.strip():
                return _nonempty([text]), _utf8_len(text)
            return [], 0
        if flush_trailing:
            closed = parts
            consumed = _utf8_len(text)
        else:
            closed = parts[:-1]  # 最后一段未闭合
            consumed = _utf8_len(self._sep.join(closed) + self._sep)
        return _nonempty([part.strip("\n") for part in closed]), consumed


# splitter 类型名 → 是否需要参数。供 profile 校验/文档。
_KNOWN_TYPES = ("line", "json_lines", "multiline_start", "delimiter")


def build_splitter(config: dict[str, Any] | None) -> RecordSplitter:
    """从 profile.splitter 配置造切割器。无配置/未知类型 → LineSplitter(向后兼容,默认行为不变)。

    config 形如 {"type": "multiline_start", "params": {"start_pattern": "^\\\\d{4}-"}}。
    """
    cfg = config or {}
    kind = str(cfg.get("type") or "line").strip()
    params = cfg.get("params") or {}
    if kind == "multiline_start":
        return _build_multiline(params)
    if kind == "delimiter":
        sep = params.get("separator")
        return DelimiterSplitter(str(sep) if sep else "\n\n")
    # line / json_lines / 未知 → 按行(json_lines 字段提取在 triage 层)。
    return LineSplitter()


def _build_multiline(params: dict[str, Any]) -> RecordSplitter:
    """造多行切割器;无 start_pattern 或坏正则 → 降级按行(不让 daemon 崩)。"""
    pattern = str(params.get("start_pattern") or "").strip()
    if not pattern:
        return LineSplitter()
    try:
        return MultilineStartSplitter(pattern)
    except re.error:
        return LineSplitter()


__all__ = [
    "DelimiterSplitter",
    "LineSplitter",
    "MultilineStartSplitter",
    "RecordSplitter",
    "build_splitter",
]
