# LLM: 普通文件各读取方式共用此索引；索引是可丢弃投影，版本变化必须重建，不是文件写入权限。
# 模块用途: 用有界内存统计文本并定位字符页，避免把大文件全部读入或每翻一页从头扫描。
from __future__ import annotations

import codecs
import hmac
import json
import re
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .cancellation import raise_if_cancelled
from .encoding_detect import decode_bytes

# 文本编码探测与分页读取的单次读取字节块大小；块太大首屏慢、太小探测不准。
_CHUNK_BYTES = 64 * 1024
# 页索引最多记录的分页点数；防止超长文件产生无界索引，超限时退化为顺序读取。
_MAX_POINTS = 1024
# 同进程校验自己生成的解码位置，防止把客户端整数作为内部解码状态直接 seek。
_CHECKPOINT_KEY = secrets.token_bytes()


# LLM: fingerprint 绑定 inode、修改时间和长度；仅用于本次读取一致性，不承诺跨进程 CAS 写锁。
# 函数用途: 识别文件替换或更新，防止重复使用旧分页坐标。
def text_file_fingerprint(path: Path) -> tuple[int, ...]:
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


# LLM: BOM 和严格 UTF-8 优先；样本末尾可能截断代码点，必须使用增量解码判定。
# 函数用途: 在有限样本内选文本编码，后续流式读取仍严格校验，绝不用替换字符掩盖错误。
def _stream_encoding(path: Path) -> str:
    with path.open("rb") as handle:
        sample = handle.read(_CHUNK_BYTES)
    for marker, codec in ((codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),
                          (codecs.BOM_UTF8, "utf-8-sig"), (codecs.BOM_UTF16_LE, "utf-16"),
                          (codecs.BOM_UTF16_BE, "utf-16")):
        if sample.startswith(marker):
            return codec
    try:
        codecs.getincrementaldecoder("utf-8")().decode(sample, final=False)
        return "utf-8"
    except UnicodeDecodeError:
        return decode_bytes(sample)[1]


# LLM: checkpoint 保存 TextIO seek cookie 而非把字符偏移当字节；CRLF/CR 在所有读取模式都统一为 LF。
# 类用途: 保存少量文本定位点和总数，每次读完关闭文件，不累积打开句柄。
@dataclass(frozen=True)
class TextFileIndex:
    path: Path
    fingerprint: tuple[int, ...]
    encoding: str
    total_chars: int
    total_lines: int
    checkpoints: tuple[tuple[int, int], ...]
    cursors: dict[int, int] = field(default_factory=dict, compare=False)
    cursor_lock: object = field(default_factory=threading.Lock, compare=False)
    line_checkpoints: tuple[tuple[int, int, int, bool], ...] = field(default=(), kw_only=True)
    line_cursors: dict[int, tuple[int, int, int]] = field(default_factory=dict, compare=False, kw_only=True)

    # LLM: 调用者已验证路径权限；打开后和读后都要核对版本，变化时拒绝混合新旧内容。
    # 函数用途: 打开统一换行、严格解码的流；使用方用 with 关闭。
    def open(self):
        self.check_current()
        return self.path.open("r", encoding=self.encoding, newline=None)

    # LLM: 版本冲突不能作为空页成功返回。
    # 函数用途: 检测索引生成之后文件是否变化。
    def check_current(self) -> None:
        if text_file_fingerprint(self.path) != self.fingerprint:
            raise OSError("文件在读取期间发生变化，请重新读取最新版本")

    # LLM: 从最近检查点定位；内存仅保留请求页，扫描尾距离有界于检查点间隔。
    # 函数用途: 读取一页文字，不重新扫描文件前面的所有字符。
    def read(self, offset: int, limit: int) -> str:
        with self.open() as handle:
            self._seek_character(handle, offset)
            result = handle.read(limit)
            cookie = handle.tell()
        self.check_current()
        with self.cursor_lock:
            while len(self.cursors) >= 32:
                self.cursors.pop(next(iter(self.cursors)))
            self.cursors[offset + len(result)] = cookie
        return result


    def _seek_character(self, handle, offset: int) -> None:
        with self.cursor_lock:
            points = (*self.checkpoints, *self.cursors.items())
        position, cookie = max((point for point in points if point[0] <= offset), default=(0, 0))
        handle.seek(cookie)
        while position < offset:
            raise_if_cancelled()
            part = handle.read(min(_CHUNK_BYTES, offset - position))
            if not part:
                break
            position += len(part)

    def seek_line(self, handle, line: int) -> int:
        with self.cursor_lock:
            recent = tuple((n, chars, cookie, True) for n, (chars, cookie, _) in self.line_cursors.items())
        number, chars, cookie, _ = max(
            (point for point in (*self.line_checkpoints, *recent)
             if point[0] < line or (point[0] == line and point[3])),
            default=(1, 0, 0, True),
            key=lambda point: point[1],
        )
        handle.seek(cookie)
        while number < line:
            raise_if_cancelled()
            part = handle.readline(_CHUNK_BYTES)
            if not part:
                break
            chars += len(part)
            number += int(part.endswith("\n"))
        return chars

    def line_byte_offset(self, line: int, cookie: int) -> int:
        if cookie <= self.fingerprint[2]:
            return cookie
        with self.cursor_lock:
            previous = tuple((n, raw) for n, (_, _, raw) in self.line_cursors.items() if n <= line)
        indexed = ((n, position) for n, _, position, at_start in self.line_checkpoints
                   if at_start and n <= line and position <= self.fingerprint[2])
        number, raw = max((*previous, *indexed), default=(1, 0))
        if number == line:
            return raw
        return _raw_line_boundary(self.path, self.encoding, number, raw, line)

    def checkpoint(
        self, *, line: int, chars: int, cookie: int, lookbehind: int,
    ) -> dict[str, object]:
        byte_offset = self.line_byte_offset(line, cookie)
        preceding = max(0, chars - lookbehind)
        preceding_cookie = cookie
        if preceding < chars:
            with self.open() as handle:
                self._seek_character(handle, preceding)
                preceding_cookie = handle.tell()
        state = [chars, cookie, self.total_chars, self.total_lines, preceding, preceding_cookie]
        checkpoint = {
            "v": 1, "file_id": list(self.fingerprint[:4]), "byte_offset": byte_offset,
            "line": line, "encoding": self.encoding, "decode_state": state,
        }
        checkpoint["seal"] = _checkpoint_seal(self.path, self.fingerprint, checkpoint)
        self.remember_line(line, chars, cookie, byte_offset)
        return checkpoint

    def remember_line(self, line: int, chars: int, cookie: int, byte_offset: int) -> None:
        with self.cursor_lock:
            while len(self.line_cursors) >= 32:
                self.line_cursors.pop(next(iter(self.line_cursors)))
            self.line_cursors[line] = (chars, cookie, byte_offset)


def _checkpoint_seal(path: Path, fingerprint: tuple[int, ...], checkpoint: dict) -> str:
    facts = (str(path), fingerprint, checkpoint["v"], checkpoint["line"], checkpoint["byte_offset"],
             checkpoint["encoding"], checkpoint["decode_state"])
    data = json.dumps(facts, separators=(",", ":")).encode("ascii")
    return hmac.digest(_CHECKPOINT_KEY, data, "sha256").hex()


def text_file_index_from_checkpoint(path: Path, checkpoint: dict, *, lookbehind: int) -> TextFileIndex | None:
    """从本进程生成的行锚点恢复，跨进程时由原索引按实际源验证位置。"""
    state, receipt = checkpoint.get("decode_state"), checkpoint.get("seal")
    if (
        not isinstance(state, list) or len(state) != 6
        or any(type(value) is not int or value < 0 or value.bit_length() > 192 for value in state)
        or not isinstance(receipt, str) or len(receipt) != 64 or not receipt.isascii()
    ):
        return None
    fingerprint = text_file_fingerprint(path)
    if not hmac.compare_digest(receipt, _checkpoint_seal(path, fingerprint, checkpoint)):
        return None
    chars, cookie, total_chars, total_lines, preceding, preceding_cookie = state
    if preceding > max(0, chars - lookbehind):
        return None
    line = checkpoint["line"]
    return TextFileIndex(
        path, fingerprint, checkpoint["encoding"], total_chars, total_lines,
        ((preceding, preceding_cookie), (chars, cookie)),
        line_checkpoints=((line, chars, cookie, True),),
        line_cursors={line: (chars, cookie, checkpoint["byte_offset"])},
    )


def _raw_line_boundary(path: Path, encoding: str, number: int, offset: int, line: int) -> int:
    # 孤立 CR 的 tell() 包含换行解码状态。用原始分隔符定位实际字节，不解析解释器内部 cookie。
    with path.open("rb") as handle:
        if encoding in ("utf-16", "utf-32"):
            marker = codecs.BOM_UTF16_BE if encoding == "utf-16" else codecs.BOM_UTF32_BE
            encoding += "-be" if handle.read(len(marker)) == marker else "-le"
        if encoding == "utf-8-sig":
            encoding = "utf-8"
        cr, lf = "\r".encode(encoding), "\n".encode(encoding)
        width = len(cr)
        pattern = re.compile(b"(?=(" + b"|".join(re.escape(part) for part in (cr + lf, cr, lf)) + b"))")
        handle.seek(offset)
        pending = b""
        while True:
            raise_if_cancelled()
            chunk = handle.read(_CHUNK_BYTES)
            data = pending + chunk
            boundary = max(0, len(data) - len(cr + lf) + 1) if chunk else len(data)
            consumed = 0
            for found in pattern.finditer(data):
                start = found.start()
                if start >= boundary:
                    break
                if start < consumed or (offset + start) % width:
                    continue
                consumed = start + len(found[1])
                number += 1
                if number == line:
                    return offset + consumed
            if not chunk:
                raise OSError("无法核对解码检查点的字节位置，请重新读取源文件。")
            advance = max(boundary, consumed)
            pending = data[advance:]
            offset += advance


# LLM: 每个工具最多保留四份最多 1024 点的版本索引；扫描不保留完整文本或单个超长行。
# 函数用途: 为字符页和行读取取得共同格式与总数，文件变化自动替换旧索引。
def text_file_index(tool, path: Path) -> TextFileIndex:
    fingerprint = text_file_fingerprint(path)
    key = (str(path), fingerprint)
    cache = getattr(tool, "_text_file_indexes", None)
    if cache is None:
        cache = {}
        tool._text_file_indexes = cache
    if key in cache:
        return cache[key]
    encoding = _stream_encoding(path)
    stride = max(_CHUNK_BYTES, (fingerprint[2] + _MAX_POINTS - 2) // (_MAX_POINTS - 1))
    points, line_points = [(0, 0)], [(1, 0, 0, True)]
    total, line_breaks, last, next_point = 0, 0, "", stride
    with path.open("r", encoding=encoding, newline=None) as handle:
        while part := handle.read(_CHUNK_BYTES):
            raise_if_cancelled()
            total += len(part)
            line_breaks += part.count("\n")
            last = part[-1]
            if total >= next_point and len(points) < _MAX_POINTS:
                cookie = handle.tell()
                points.append((total, cookie))
                line_points.append((line_breaks + 1, total, cookie, last == "\n"))
                next_point += stride
    index = TextFileIndex(path, fingerprint, encoding, total,
                          line_breaks + int(bool(last) and last != "\n"), tuple(points),
                          line_checkpoints=tuple(line_points))
    index.check_current()
    while len(cache) >= 4:
        cache.pop(next(iter(cache)))
    cache[key] = index
    return index
