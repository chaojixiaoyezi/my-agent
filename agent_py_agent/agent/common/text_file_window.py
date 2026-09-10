# LLM: 普通文件各读取方式共用此索引；索引是可丢弃投影，版本变化必须重建，不是文件写入权限。
# 模块用途: 用有界内存统计文本并定位字符页，避免把大文件全部读入或每翻一页从头扫描。
from __future__ import annotations

import codecs
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .encoding_detect import decode_bytes

_CHUNK = 64 * 1024
_MAX_POINTS = 1024


# LLM: fingerprint 绑定 inode、修改时间和长度；仅用于本次读取一致性，不承诺跨进程 CAS 写锁。
# 函数用途: 识别文件替换或更新，防止重复使用旧分页坐标。
def text_file_fingerprint(path: Path) -> tuple[int, ...]:
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


# LLM: BOM 和严格 UTF-8 优先；样本末尾可能截断代码点，必须使用增量解码判定。
# 函数用途: 在有限样本内选文本编码，后续流式读取仍严格校验，绝不用替换字符掩盖错误。
def _stream_encoding(path: Path) -> str:
    with path.open("rb") as handle:
        sample = handle.read(_CHUNK)
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
        with self.cursor_lock:
            points = (*self.checkpoints, *self.cursors.items())
        position, cookie = max((point for point in points if point[0] <= offset), default=(0, 0))
        with self.open() as handle:
            handle.seek(cookie)
            while position < offset:
                part = handle.read(min(_CHUNK, offset - position))
                if not part:
                    break
                position += len(part)
            result = handle.read(limit)
            cookie = handle.tell()
        self.check_current()
        with self.cursor_lock:
            while len(self.cursors) >= 32:
                self.cursors.pop(next(iter(self.cursors)))
            self.cursors[offset + len(result)] = cookie
        return result


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
    stride = max(_CHUNK, (fingerprint[2] + _MAX_POINTS - 2) // (_MAX_POINTS - 1))
    points, total, line_breaks, last, next_point = [(0, 0)], 0, 0, "", stride
    with path.open("r", encoding=encoding, newline=None) as handle:
        while part := handle.read(_CHUNK):
            total += len(part)
            line_breaks += part.count("\n")
            last = part[-1]
            if total >= next_point and len(points) < _MAX_POINTS:
                points.append((total, handle.tell()))
                next_point += stride
    index = TextFileIndex(path, fingerprint, encoding, total,
                          line_breaks + int(bool(last) and last != "\n"), tuple(points))
    index.check_current()
    while len(cache) >= 4:
        cache.pop(next(iter(cache)))
    cache[key] = index
    return index
