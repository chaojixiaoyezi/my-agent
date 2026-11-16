# LLM: /progress 的行号读取层：有界稀疏索引 + 增量扫描 + EOF 快路径，把"每次轮询整读事件流"
#   降成"增量读取+固定大小边界核验"。索引只存位置/hash、不缓存正文、丢了可从文件重建，不是
#   第二事实源；行号协议与公开事件白名单保持原样（旧 IM 客户端不需要改），投影/过滤仍留在
#   http_handlers。与旧实现的两处行为差异：未写完的半行不再被消费（修正，见 _read_one_line）；
#   超过块上限的合法长行跨块拼完整后照常返回（MC4-1001 返工），只有超过硬上限的行才丢弃内容
#   并合成结构化超限行（_oversize_marker，投影层转成公开超限事件）。
# 模块用途: 按原始行号增量读取 Gateway chunk 事件流，供 /progress 接口使用。
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from ..common import cache_capacity

logger = logging.getLogger(__name__)

# 缓存文件数上限：IM 并发轮询的会话量级是几十；超出按 LRU 淘汰最旧（索引可重建）。
_MAX_INDEXED_FILES = 64
# 锚点间隔行数：读 200 行时最多回退 63 行重新扫描（进度行 KB 级，最多几十 KB）。
_ANCHOR_STRIDE_LINE_COUNT = 64
# 每文件锚点上限：覆盖约 6.5 万行；到达上限后停增锚点，读更远的行退化为从最后锚点扫描（正确但慢）。
_MAX_FILE_ANCHOR_COUNT = 1024
# 单行读取块上限（字节）：正常进度行是 KB 级；超过即按超长行处理（跨块拼完整，内存按单行上限算）。
_READ_CHUNK_BYTES = 1_048_576
# 单行硬上限（字节，64 MiB）：只有超过它的行才丢弃内容并合成结构化超限事件。旧实现完全没有
# 上限（异常文件可以把网关内存撑爆）；正常事件最大来自工具边界的完整 commentary，远小于此值。
_MAX_LINE_BYTES = 67_108_864


# LLM: 索引是纯缓存：identity/observed_size 用来判断"文件是否还是它"，anchors 只存稀疏的
#   (行号, 字节偏移)；complete_lines/scanned_bytes 描述"已确认到哪"。任何不一致都丢弃重建。
# 类用途: 保存一个 chunk 文件的增量读取索引（只存位置，不缓存正文）。
@dataclass
class _FileIndex:
    identity: tuple[object, ...]
    observed_size: int
    observed_version: tuple[int, int]
    scanned_bytes: int
    complete_lines: int
    boundary_digest: bytes | None = None
    anchors: list[tuple[int, int]] = field(default_factory=lambda: [(0, 0)])
    eof_reached: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)


_INDEX_LOCK = threading.Lock()
# LLM: 索引表按访问顺序维护（_evict_indexes 淘汰队首 = 最久未用）；键是路径字符串。
_INDEXES: OrderedDict[str, _FileIndex] = OrderedDict()


# LLM: 文件身份是"截断/替换识别"的核心：POSIX 用 (st_dev, st_ino)，同长度替换也换 inode；
#   Windows 没有可靠 inode，按任务合同退化为创建时间（追加不改创建时间，替换会变）。
# 函数用途: 取文件身份元组（POSIX 用设备号+inode；Windows 用创建时间）。
def _identity(st: os.stat_result) -> tuple[object, ...]:
    if os.name == "nt":
        return (st.st_ctime_ns,)
    return (st.st_dev, st.st_ino)


# LLM: 此入口只维护进度文件的可重建位置索引，不授予读权；修改需同步替换、半行及游标回归。
# 函数用途: 判断索引身份与当前文件描述符是否一致（不一致说明文件被替换，索引作废重建）。
def _matches(index: _FileIndex, st: os.stat_result) -> bool:
    return index.identity == _identity(st)


# LLM: 此入口只维护进度文件的可重建位置索引，不授予读权；修改需同步替换、半行及游标回归。
# 函数用途: 从当前文件描述符读一段行字节（readline 语义：到 LF、到块上限或到 EOF）。
#   所有正文读取都经过这里，测试用它统计"一次读取到底读了多少字节"。
def _read_line_bytes(f: BinaryIO, limit: int) -> bytes:
    return f.readline(limit)


# LLM: 此入口只维护进度文件的可重建位置索引，不授予读权；修改需同步替换、半行及游标回归。
# 函数用途: 把一行原始字节转成记录文本（去 LF、兼容 CRLF）；非法 UTF-8 返回 None（按坏行跳过）。
def _decode_line(raw: bytes | bytearray) -> str | None:
    body = raw[:-1]
    if body.endswith(b"\r"):
        body = body[:-1]
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return None


# LLM: 此入口只维护进度文件的可重建位置索引，不授予读权；修改需同步替换、半行及游标回归。
# 函数用途: 解码一行并把可用的行文本并入结果（非法 UTF-8 行按坏行跳过，行号照常推进）。
def _collect_decoded(line: bytes | bytearray, collected: list[str]) -> None:
    text = _decode_line(line)
    if text is not None:
        collected.append(text)

# LLM: 读一行的统一入口：完整行（含超过块上限的合法长行——跨块拼完整，内存按单行上限算）直接
#   返回；只有超过硬上限的行才丢弃内容并返回总字节数（上层合成结构化超限事件，行号照常推进）；
#   EOF 或未写完的半行不消费——这是对现状的修正（现状会把没写完的末行算作一行）。
# 函数用途: 读一条记录，返回 (完整行字节, 0) 或 (None, 超限行总字节数) 或 (None, 0)（未完成）。
def _read_one_line(f: BinaryIO) -> tuple[bytes | bytearray | None, int]:
    start = f.tell()
    first = _read_line_bytes(f, _READ_CHUNK_BYTES)
    if not first:
        return None, 0
    if first.endswith(b"\n"):
        return first, 0
    if len(first) < _READ_CHUNK_BYTES:
        f.seek(start)
        return None, 0
    return _read_long_line(f, start, bytearray(first))


# LLM: 此入口只维护进度文件的可重建位置索引，不授予读权；修改需同步替换、半行及游标回归。
# 函数用途: 跨块拼一条超长行：到 LF 返回完整内容；超过硬上限改走丢弃路径；EOF 时退回行首不消费。
def _read_long_line(f: BinaryIO, start: int, buffer: bytearray) -> tuple[bytes | bytearray | None, int]:
    while True:
        more = _read_line_bytes(f, _READ_CHUNK_BYTES)
        if not more:
            f.seek(start)
            return None, 0
        buffer.extend(more)
        if more.endswith(b"\n"):
            return _finish_long_line(buffer)
        if len(buffer) > _MAX_LINE_BYTES:
            return _discard_long_line(f, len(buffer))


# LLM: 此入口只维护进度文件的可重建位置索引，不授予读权；修改需同步替换、半行及游标回归。
# 函数用途: 超长行到行尾后的收口：未超硬上限返回完整内容，超过则丢弃内容并报总字节数。
def _finish_long_line(buffer: bytearray) -> tuple[bytes | bytearray | None, int]:
    if len(buffer) > _MAX_LINE_BYTES:
        return None, len(buffer)
    return buffer, 0


# LLM: 此入口只维护进度文件的可重建位置索引，不授予读权；修改需同步替换、半行及游标回归。
# 函数用途: 超限行继续读到行尾（内容丢弃），返回该行总字节数；EOF 时返回 0（该行不消费）。
def _discard_long_line(f: BinaryIO, total: int) -> tuple[None, int]:
    while True:
        more = _read_line_bytes(f, _READ_CHUNK_BYTES)
        if not more:
            return None, 0
        total += len(more)
        if more.endswith(b"\n"):
            return None, total


# LLM: 此入口只维护进度文件的可重建位置索引，不授予读权；修改需同步替换、半行及游标回归。
# 函数用途: 行号跨过锚点间隔时记录 (行号, 字节偏移)，供"从最近锚点定位"使用；到上限后停增。
def _maybe_add_anchor(index: _FileIndex, line_no: int, offset: int) -> None:
    if line_no % _ANCHOR_STRIDE_LINE_COUNT:
        return
    if len(index.anchors) >= _MAX_FILE_ANCHOR_COUNT:
        return
    index.anchors.append((line_no, offset))


# LLM: 此入口只维护进度文件的可重建位置索引，不授予读权；修改需同步替换、半行及游标回归。
# 函数用途: 找不超过目标行号的最近锚点，返回 (锚点行号, 字节偏移)。
def _anchor_for(index: _FileIndex, since: int) -> tuple[int, int]:
    best_line, best_offset = 0, 0
    for line_no, offset in index.anchors:
        if line_no > since:
            break
        best_line, best_offset = line_no, offset
    return best_line, best_offset


# LLM: 此入口只维护进度文件的可重建位置索引，不授予读权；修改需同步替换、半行及游标回归。
# 函数用途: 超限行的结构化占位行（投影层据此给客户端"该事件过大、已省略"提示；不是真实 chunk）。
def _oversize_marker(line_no: int, bytes_total: int) -> str:
    return json.dumps(
        {"kind": "progress_oversize", "line": line_no, "bytes": bytes_total},
        ensure_ascii=False,
    )


# LLM: 此入口只维护进度文件的可重建位置索引，不授予读权；修改需同步替换、半行及游标回归。
# 函数用途: 把扫描到的一行转成要收集的文本（超限行合成占位行；窗口外或坏行返回 None）。
def _line_text(line: bytes | bytearray | None, over_limit: int, line_no: int, window: tuple[int, int]) -> str | None:
    if over_limit:
        logger.warning(
            "progress chunk line %d exceeds %d bytes; emitting oversize marker",
            line_no,
            _MAX_LINE_BYTES,
        )
    since, limit = window
    if not (since <= line_no < since + limit):
        return None
    if over_limit:
        return _oversize_marker(line_no, over_limit)
    if line is None:
        return None
    return _decode_line(line)


# LLM: 增量扫描从"已确认位置"向前扫描至窗口上限（since + limit）或 EOF：逐行找 LF、更新行数、按间隔记锚点；
#   落在请求窗口内的新行顺带收集（一遍读完，追加场景不重读旧字节）。未写完的半行不推进
#   （scanned_bytes 停在它前面），等补齐后再读；因窗口上限提前停下时只记录实际扫描字节前缀。
# 函数用途: 从索引已扫描位置增量扫到窗口末尾或 EOF，返回 (新行列表, 是否到达 EOF)。
def _scan_new_bytes(f: BinaryIO, index: _FileIndex, since: int, limit: int) -> tuple[list[str], bool]:
    f.seek(index.scanned_bytes)
    line_no = index.complete_lines
    scanned = index.scanned_bytes
    collected: list[str] = []
    target = since + limit
    window = (since, limit)
    reached_eof = False
    while line_no < target:
        line, over_limit = _read_one_line(f)
        if line is None and not over_limit:
            reached_eof = True
            break
        text = _line_text(line, over_limit, line_no, window)
        if text is not None:
            collected.append(text)
        line_no += 1
        scanned = f.tell()
        _maybe_add_anchor(index, line_no, scanned)
    index.complete_lines = line_no
    index.scanned_bytes = scanned
    return collected, reached_eof


# LLM: 此入口只维护进度文件的可重建位置索引，不授予读权；修改需同步替换、半行及游标回归。
# 函数用途: 从最近锚点读到窗口末尾，返回 since 起最多 limit 条旧区行文本（超限行合成占位行）。
def _read_old_range(f: BinaryIO, index: _FileIndex, since: int, limit: int) -> list[str]:
    end = min(index.complete_lines, since + limit)
    if since >= end:
        return []
    anchor_line, offset = _anchor_for(index, since)
    f.seek(offset)
    line_no = anchor_line
    out: list[str] = []
    while line_no < end:
        line, over_limit = _read_one_line(f)
        if line is None and not over_limit:
            break
        if line_no >= since:
            _collect_old_line(line, over_limit, line_no, out)
        line_no += 1
    return out


# LLM: 此入口只维护进度文件的可重建位置索引，不授予读权；修改需同步替换、半行及游标回归。
# 函数用途: 把旧区一行并入结果（超限行合成占位行；坏行跳过）。
def _collect_old_line(line: bytes | bytearray | None, over_limit: int, line_no: int, out: list[str]) -> None:
    if over_limit:
        out.append(_oversize_marker(line_no, over_limit))
    elif line is not None:
        _collect_decoded(line, out)


# 函数用途: 索引失效时就地重置状态，保持持有同一 index.lock 的并发调用方锁身份不变。
def _reset_index(index: _FileIndex, st: os.stat_result) -> None:
    index.identity = _identity(st)
    index.observed_size = 0
    index.observed_version = (st.st_mtime_ns, st.st_ctime_ns)
    index.scanned_bytes = 0
    index.complete_lines = 0
    index.boundary_digest = None
    index.anchors = [(0, 0)]
    index.eof_reached = False


# LLM: _INDEX_LOCK 仅保护 _INDEXES 字典查找/插入/LRU 淘汰，不跨文件 I/O，也不读取可被流锁改写的字段；
#   身份/截短/版本校验全部在持有 index.lock 后就地判定，彻底消除无锁读写撕裂与锁序反转。
# 函数用途: 获取或创建该路径的索引槽位（超出并发容量时按 LRU 淘汰最旧条目）。
def _lookup_index(path: Path, st: os.stat_result) -> _FileIndex:
    key = str(path)
    index = _INDEXES.get(key)
    if index is None:
        index = _FileIndex(
            identity=_identity(st),
            observed_size=0,
            observed_version=(st.st_mtime_ns, st.st_ctime_ns),
            scanned_bytes=0,
            complete_lines=0,
        )
        _INDEXES[key] = index
        _evict_indexes()
    _INDEXES.move_to_end(key)
    return index


# LLM: 此入口只维护进度文件的可重建位置索引，不授予读权；修改需同步替换、半行及游标回归。
# 函数用途: 超出全局在途并发容量（最低保持 _MAX_INDEXED_FILES）时淘汰最旧索引。
def _evict_indexes() -> None:
    capacity = cache_capacity.global_inflight(_MAX_INDEXED_FILES)
    while len(_INDEXES) > capacity:
        _INDEXES.popitem(last=False)


# LLM: 读取流程 = 打开同一个 fd → fstat → 取可用索引（身份/截短检查，必要时重建）→ 快路径
#   （文件没变且已到末尾：不读正文直接返回）→ 读旧区 → 增量扫描新字节。索引与正文都从这
#   一个 fd 出发，避免"先查路径、后开另一个文件"的替换窗口；锁只保护索引状态与读取过程，
#   单次读是毫秒级（首次建索引允许全扫一次）。拆小函数只为嵌套深度达标，流程不变。
# 函数用途: 打开进度文件并取一次 stat（失败返回 None，调用方按空结果处理）。
def _open_progress_file(path: Path) -> tuple[BinaryIO, os.stat_result] | None:
    try:
        f = path.open("rb")
    except OSError:
        return None
    try:
        st = os.fstat(f.fileno())
    except OSError:
        f.close()
        return None
    return f, st
# LLM: 此入口只维护进度文件的可重建位置索引，不授予读权；修改需同步替换、半行及游标回归。


# LLM: 原chunk协议是追加或原子替换；有界首尾摘要用于发现copytruncate后快速长回的边界变化，
#   只存hash/位置，不保存正文，不承诺任意外部中段并发改写的事务一致性。空轮询不调用此读口。
# 函数用途: 从同一fd读取已扫描前缀的首尾各最多64字节，核验旧行位置是否仍可复用。
def _boundary_fingerprint(handle: BinaryIO, end: int) -> bytes:
    size = min(64, end)
    handle.seek(0)
    head = handle.read(size)
    handle.seek(end - size)
    tail = handle.read(size)
    return hashlib.sha256(head + tail).digest()


# 函数用途: 确认底层文件是否已被替换、截短、同长重写或边界改写，是则需重置索引。
def _index_invalidated(f: BinaryIO, index: _FileIndex, st: os.stat_result) -> bool:
    if not _matches(index, st):
        return True
    if st.st_size < index.observed_size or st.st_size < index.scanned_bytes:
        return True
    if st.st_size == index.observed_size and (st.st_mtime_ns, st.st_ctime_ns) != index.observed_version:
        return True
    return (
        st.st_size > index.observed_size
        and index.boundary_digest is not None
        and _boundary_fingerprint(f, index.scanned_bytes) != index.boundary_digest
    )


# 函数用途: 文件确已扫描至当前末尾或已覆盖目标窗口时跳过扫描；否则增量扫新字节并只记录实际前缀大小。
def _scan_if_changed(
    f: BinaryIO, index: _FileIndex, st: os.stat_result, window: tuple[int, int]
) -> list[str]:
    if st.st_size == index.observed_size and (index.eof_reached or index.scanned_bytes == st.st_size):
        return []
    since, limit = window
    if index.complete_lines >= since + limit:
        return []
    new_lines, reached_eof = _scan_new_bytes(f, index, since, limit)
    index.eof_reached = reached_eof
    index.observed_size = st.st_size if reached_eof else index.scanned_bytes
    index.observed_version = (st.st_mtime_ns, st.st_ctime_ns)
    index.boundary_digest = _boundary_fingerprint(f, index.scanned_bytes) if index.scanned_bytes else None
    return new_lines


# LLM: _INDEX_LOCK 仅用于查表；每条流的边界核验与文件扫描一律在 index.lock 内串行，绝不借用
#   json_io.path_thread_lock（写入侧在追加时持有该锁），保证不同流读写互不阻塞。
# 函数用途: 流锁内完成一次读取（快路径 → 旧区 → 增量扫描），返回 (行文本 tuple, next 行号)。
def _read_with_index(
    f: BinaryIO, path: Path, st: os.stat_result, window: tuple[int, int]
) -> tuple[tuple[str, ...], int]:
    since, limit = window
    with _INDEX_LOCK:
        index = _lookup_index(path, st)
    with index.lock:
        try:
            if _index_invalidated(f, index, st):
                _reset_index(index, st)
            if (
                st.st_size == index.observed_size
                and (index.eof_reached or index.scanned_bytes == st.st_size)
                and since >= index.complete_lines
            ):
                return (), min(index.complete_lines, since + limit)
            old_lines = _read_old_range(f, index, since, limit)
            new_lines = _scan_if_changed(f, index, st, window)
        except OSError:
            with _INDEX_LOCK:
                if _INDEXES.get(str(path)) is index:
                    _INDEXES.pop(str(path), None)
            return (), since
        return tuple(old_lines + new_lines), min(index.complete_lines, since + limit)



# LLM: 此入口只维护进度文件的可重建位置索引，不授予读权；修改需同步替换、半行及游标回归。
# 函数用途: 按行号读取进度流切片，返回 (行文本 tuple, next 行号)；读不了时返回 ((), since)。
def read_progress_lines(
    path: Path, since: int, *, limit: int = 200
) -> tuple[tuple[str, ...], int]:
    opened = _open_progress_file(path)
    if opened is None:
        return (), since
    f, st = opened
    with f:
        return _read_with_index(f, path, st, (since, limit))
