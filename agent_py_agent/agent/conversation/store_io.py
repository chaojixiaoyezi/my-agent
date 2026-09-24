# LLM: 会话存储通用 IO 原语，不依赖 Store 或执行器；保留记录边界、错误、路径及归档文件移动顺序。
# 模块用途: 为各领域共用账本读取、路径、时间和显式文件归档，避免领域对象反向依赖组装入口。
from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any, BinaryIO

from ..common.json_io import jsonl_lines, read_jsonl_text_lines
from ..runtime_errors import DataCorruptionError, runtime_error_report


# LLM: 会话 JSONL 读取同时返回成功行和损坏事实；调用方不能把 load_errors 当成空账本。
# 类用途: 将账本数据与带路径、行号的读取错误一起交给领域存储。
@dataclass(frozen=True)
class JsonlReadReport:
    rows: list[dict[str, Any]]
    load_errors: list[dict[str, Any]]


# LLM: 仅按物理 LF 读取 canonical JSONL；不修复、不丢弃损坏事实，修改须联测用量与 transcript。
# 函数用途: 读取完整会话账本，返回可解析记录及逐行错误，不写文件。
def read_jsonl_report(path: Path, *, context: str) -> JsonlReadReport:
    # 记录边界只能是物理 LF：splitlines() 会在 U+0085/U+2028/U+2029 等合法 JSON 字符串字符处
    # 切开一条完整记录，制造假 JSON 错误并把可读账本判成损坏（真实事故见 jsonl_lines 注释）。
    try:
        lines = read_jsonl_text_lines(path)
    except OSError as exc:
        return JsonlReadReport([], [jsonl_error(exc, context, path=path)])
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        row, error = json_row(line, context=context, path=path, line_number=line_number)
        if error is not None:
            errors.append(error)
            continue
        if row is not None:
            rows.append(row)
    return JsonlReadReport(rows, errors)


_TAIL_BLOCK_BYTES = 64 * 1024


# LLM: 尾读只按字节定位边界；上层负责舍弃不完整首行并解释 JSON，不改变文件。
# 函数用途: 分块从文件末尾读取足够行，避免分页时加载整个账本。
def _read_tail_bytes(path: Path, limit: int) -> tuple[bytes, int]:
    """按 64KB 块从文件尾部倒读，直到覆盖 limit+1 个换行或到达文件头。"""
    with path.open("rb") as handle:
        handle.seek(0, 2)
        pos = handle.tell()
        block = _TAIL_BLOCK_BYTES
        data = b""
        while pos > 0 and data.count(b"\n") <= limit:
            step = min(block, pos)
            pos -= step
            handle.seek(pos)
            data = handle.read(step) + data
    return data, pos


# LLM: 与 read_jsonl_tail_report 同窗：同样按 64KiB 块从文件尾对齐倒读，同样在"已读换行数超过 limit 或到文件头"时停，
# 窗口首个可能不完整的行同样丢弃（到文件头时保留）；只按 LF 字节切行，逐行 UTF-8 replace 解码并去掉一个行尾 CR，空白行不计。
# 修改须与 read_jsonl_tail_report 的等价测试同步；read_jsonl_tail_report 本身不经此实现，child/后台路径不受影响。
# 函数用途: 从新到旧逐行产出尾部最多 limit 条非空记录，内存只占一个块加一行，不物化整个尾部；凑够即停止读块。
def iter_jsonl_tail_lines(path: Path, *, limit: int) -> Iterator[str]:
    if limit <= 0:
        raise ValueError("tail line iteration needs a positive limit")
    texts = (_tail_line_text(raw) for raw in _tail_raw_lines(path, limit))
    return islice((text for text in texts if text.strip()), limit)


# LLM: 窗口规则的唯一实现：读块条件与原 _read_tail_bytes 相同；关窗时首段只在到达文件头时才是完整行。
# 函数用途: 从新到旧产出窗口内的完整原始行（含空行），由上层过滤与截断。
def _tail_raw_lines(path: Path, limit: int) -> Iterator[bytes]:
    with path.open("rb") as handle:
        position = handle.seek(0, 2)
        newlines = 0
        carry: deque[bytes] = deque()
        while position > 0 and newlines <= limit:
            step = min(_TAIL_BLOCK_BYTES, position)
            position -= step
            handle.seek(position)
            block = handle.read(step)
            newlines += block.count(b"\n")
            yield from _split_tail_block(block, carry)
        if position == 0 and carry:
            yield b"".join(carry)


# LLM: carry 按文件顺序保存较新块里尚未遇到行首的片段；块内第一条（最新）完整行接上它，之后清空，块首残段成为新的 carry。
# 函数用途: 从新到旧切出一个块里的完整行，跨块的行在找到其行首时一次拼接。
def _split_tail_block(block: bytes, carry: deque[bytes]) -> Iterator[bytes]:
    end = len(block)
    index = block.rfind(b"\n", 0, end)
    while index >= 0:
        yield block[index + 1:end] + b"".join(carry)
        carry.clear()
        end = index
        index = block.rfind(b"\n", 0, end)
    carry.appendleft(block[:end])


# LLM: 与整块解码后按 LF 切分等价：LF 不会出现在 UTF-8 多字节序列中，坏字节同样以 replace 处理；只去一个行尾 CR。
# 函数用途: 把倒读得到的一行原始字节变成记录文本。
def _tail_line_text(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    return text[:-1] if text.endswith("\r") else text


# LLM: 在调用方已打开的canonical描述符上以固定物理大小倒扫LF；64KiB空间、短读拒绝，不识别业务行或覆盖。
# 函数用途: 共用历史倒读与前向快照的完整尾界，半行不推进游标，追加留给下一次读取。
def complete_jsonl_end(handle: BinaryIO, size: int) -> int:
    position = size
    while position:
        step = min(position, 65536)
        position -= step
        handle.seek(position)
        block = handle.read(step)
        if len(block) != step:
            raise OSError("canonical transcript truncated during snapshot")
        index = block.rfind(b"\n")
        if index >= 0:
            return position + index + 1
    return 0


# LLM: 有界读取保持与全量读取相同的坏行和 Unicode 规则；limit 非正时读取全量，不修改账本。
# 函数用途: 给 transcript 和消息窗口提供末尾记录及读取错误。
def read_jsonl_tail_report(path: Path, *, context: str, limit: int) -> JsonlReadReport:
    """从文件尾部按块倒读最后 limit 条记录，避免大账本全量加载。

    跨块边界可能截断的首行会被丢弃（它属于更早的记录）；limit<=0 退回全量读取。"""
    if limit <= 0:
        return read_jsonl_report(path, context=context)
    try:
        data, pos = _read_tail_bytes(path, limit)
    except OSError as exc:
        return JsonlReadReport([], [jsonl_error(exc, context, path=path)])
    # 尾部倒读同样只按 LF 切记录；跨块截断的首行仍按下面的既有规则丢弃。
    lines = list(jsonl_lines(data.decode("utf-8", errors="replace")))
    if pos > 0 and lines:
        lines = lines[1:]
    selected = [line for line in lines if line.strip()][-limit:]
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for line in selected:
        row, error = json_row(line, context=context, path=path, line_number=0)
        if error is not None:
            errors.append(error)
            continue
        if row is not None:
            rows.append(row)
    return JsonlReadReport(rows, errors)


# LLM: 显式时间原样归一，否则读取墙钟；不作为 Goal 单调计时或运行身份的替代。
# 函数用途: 为会话持久记录生成时间戳，允许调用方提供确定性时间。
def now(value: float | None = None) -> float:
    return float(time.time() if value is None else value)


# LLM: 保持既有会话文件名映射，不能随组件拆分改变持久路径；调用方仍须验证身份。
# 函数用途: 将显式标识转换成原账本采用的文件名片段。
def safe_file_stem(value: str) -> str:
    text = str(value or "").strip()
    return "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in text) or "unknown"


# LLM: 单行必须是 JSON 对象；语法错误与非对象都返回结构化错误，不把它们当作空记录。
# 函数用途: 解析一行会话账本并保留出错路径与行号。
def json_row(
    line: str,
    *,
    context: str,
    path: Path,
    line_number: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        row = json.loads(line)
    except json.JSONDecodeError as exc:
        return None, jsonl_error(exc, context, path=path, line_number=line_number)
    if not isinstance(row, dict):
        return None, jsonl_error(
            ValueError(f"JSONL row is {type(row).__name__}, expected object"),
            context,
            path=path,
            line_number=line_number,
        )
    return row, None


# LLM: 复用运行时错误分类并附加 canonical 路径和可用行号；不读取或修改原文件。
# 函数用途: 给领域存储生成可追踪的 JSONL 损坏报告。
def jsonl_error(
    exc: BaseException,
    context: str,
    *,
    path: Path,
    line_number: int | None = None,
) -> dict[str, Any]:
    report = runtime_error_report(exc, context=context)
    report["path"] = str(path)
    if line_number is not None:
        report["line_number"] = line_number
    return report


LEDGER_ARCHIVE_DIR = ".ledger_archive"


# LLM: 归档操作保持原文件及锁的移动顺序，重复清理幂等；是否可归档由调用领域判断。
# 函数用途: 将已确认可归档的单个账本文件及锁移入归档目录，返回是否成功。
def archive_ledger_file(src: Path, archive_dir: Path) -> bool:
    """把单个账本文件(及其 .lock)移入归档目录;文件已被并发清走则视为成功(幂等)。"""
    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
        src.rename(archive_dir / src.name)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    lock = Path(f"{src}.lock")
    try:
        if lock.exists():
            lock.rename(archive_dir / lock.name)
    except OSError:
        pass
    return True


# LLM: 原 JSON 对象读取与坏账报告统一入口，缺失文件仍为空，损坏不能被当作不存在。
# 函数用途: 只读线程或目标 JSON，返回原上下文和路径错误，不初始化目录。
def read_json_object_report(
    path: Path, *, context: str
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if not path.exists():
        return {}, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise DataCorruptionError(f"{path.name} is {type(payload).__name__}, expected object")
        return payload, None
    except Exception as exc:
        report = runtime_error_report(exc, context=context)
        report["path"] = str(path)
        return {}, report


# LLM: 沿用唤醒和插话索引清理的尽力删除语义，只忽略 OSError；不用于判断业务完成或成功投递。
# 函数用途: 删除已经交接的队列文件或过期索引，保留调用方原先的失败处理边界。
def unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
