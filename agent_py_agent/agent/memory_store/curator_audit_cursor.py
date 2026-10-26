# LLM: 仅替换Curator审计只读扫描；共享粗时间窗口内不信任或保存分片hint，历史错误及AUDIT_CURSOR_MISSING不变，不改状态schema。
# 模块用途: 为稳定分片提供有界定位缓存；近期写入、冷启动和失效均走原扫描，防止同tick改写被旧EOF或前缀吞掉。
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from ..common.cache_freshness import cache_entry_trustworthy
from ..io.cursor_cache import CursorCache, FileStamp, cursor_key, file_stamp, note_seen

# 128个近期审计游标兼顾多owner和80条批次，淘汰仅重扫；不缓存事件正文或历史异常。
_AUDIT_CURSOR_CACHE_MAX_COUNT = 128
# 最近512份旧分片保明细，更早分片折叠为前缀摘要；复用时仍逐份重新stat核验。
_AUDIT_CURSOR_PREFIX_MAX_COUNT = 512
# 128KiB有界首ID过滤器使约十万事件仍能保守提升新游标；误报仅变慢，极端128份为16MiB。
_AUDIT_CURSOR_FILTER_BYTES = 128 * 1024
_AUDIT_CURSORS = CursorCache(_AUDIT_CURSOR_CACHE_MAX_COUNT)


# LLM: 一次读取只使用显式owner目录、原cursor及原两项预算，不解析正文决定归属。
# 类用途: 保存审计读取的四个原输入。
@dataclass(frozen=True)
class AuditRead:
    root: Path
    target: str
    limit: int
    max_chars: int


# LLM: prefix只含已确认干净的较早分片指纹；seen保守挡住重复event_id首匹配，不含事件内容。
# 类用途: 保存某审计事件所在文件、锚行及其之前的只读定位事实。
@dataclass(frozen=True)
class AuditCursor:
    name: str
    stamp: FileStamp
    start: int
    end: int
    line: int
    prefix: tuple[tuple[str, FileStamp], ...]
    seen: bytes
    prefix_digest: str = ""
    prefix_last: str = ""
    prefix_count: int = 0


# LLM: 这些对象只在本批驻留，绝不进入缓存或Curator持久状态。
# 类用途: 保存一条已解析行及原字节/物理行位置。
@dataclass(frozen=True)
class AuditRow:
    payload: dict
    start: int
    end: int
    line: int


# LLM: 整个当前文件的错误必须先收齐，预算不能吞掉较晚坏行；UTF-8/IO整读失败丢弃全部有效行。
# 类用途: 返回一个分片的原顺序记录、错误和可缓存性。
@dataclass
class AuditSlice:
    rows: list[AuditRow] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    stamp: FileStamp | None = None
    fatal: bool = False
    path: Path | None = None
    stopped: bool = False


# LLM: 局部状态只控制当前选取前缀；所有持久游标仍由原Curator提交链推进。
# 类用途: 保存扫描中的预算、错误、定位提示和首ID过滤器。
@dataclass
class AuditWindow:
    request: AuditRead
    project: Callable
    found: bool
    seen: bytearray
    selected: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    pending: list = field(default_factory=list)
    prefix: list = field(default_factory=list)
    used_chars: int = 0
    cacheable: bool = True
    prefix_digest: str = ""
    prefix_last: str = ""
    prefix_count: int = 0


# LLM: 缓存锚文件后半发生整读失败时，原路径不会找到该锚；内部信号只要求同算法从头重扫。
# 类用途: 让校验失败退回完整扫描，不把缓存hint当作损坏诊断来源。
class _AuditRescan(Exception):
    pass


# LLM: 压缩前缀仍逐份stat重算累积摘要；近期文件、删除、替换或改写均退回原冷扫描。
# 函数用途: 校验全部旧分片及锚行，不因历史超过512份永久放弃缓存。
def _reuse_cursor(request: AuditRead, paths: list[Path]) -> AuditCursor | None:
    cached = _AUDIT_CURSORS.get(cursor_key(request.root, request.target))
    if cached is None:
        return None
    index = next((i for i, path in enumerate(paths) if path.name == cached.name), -1)
    if index < 0 or index != cached.prefix_count + len(cached.prefix):
        return None
    try:
        prefix_paths = paths[:cached.prefix_count]
        digest = ""
        for path in prefix_paths:
            stamp = file_stamp(path.stat())
            if not cache_entry_trustworthy(stamp.mtime_ns):
                return None
            digest = _prefix_digest(digest, path.name, stamp)
        if (digest != cached.prefix_digest
                or (prefix_paths[-1].name if prefix_paths else "") != cached.prefix_last):
            return None
        paths_before = paths[cached.prefix_count:index]
        if tuple(path.name for path in paths_before) != tuple(name for name, _ in cached.prefix):
            return None
        if any(not cache_entry_trustworthy(stamp.mtime_ns) or file_stamp(path.stat()) != stamp
               for path, (_, stamp) in zip(paths_before, cached.prefix)):
            return None
        return _validate_anchor(paths[index], request.target, cached)
    except (OSError, ValueError, UnicodeError):
        return None


# LLM: 近期stamp即使五字段相同也不能复用；窗口外仍核身份/截断/同尺寸变更，非EOF锚须读回精确ID和边界。
# 函数用途: 复核稳定审计文件的锚行；fresh文件返回冷扫描，不把缓存hint当存在证明。
def _validate_anchor(path: Path, target: str, cached: AuditCursor) -> AuditCursor | None:
    stamp = file_stamp(path.stat())
    if (not cache_entry_trustworthy(stamp.mtime_ns)
            or not cached.stamp.same_file(stamp) or stamp.size < cached.stamp.size):
        return None
    if stamp.size == cached.stamp.size and stamp != cached.stamp:
        return None
    if stamp == cached.stamp and cached.end == stamp.size:
        return cached
    with path.open("rb") as handle:
        handle.seek(cached.start)
        _, raw, end = next(_raw_lines(handle), (0, b"", 0))
        row = json.loads(raw.decode("utf-8"))
        matches = isinstance(row, dict) and str(row.get("event_id") or "").strip() == target
        if matches and end == cached.end and file_stamp(os.fstat(handle.fileno())) == stamp:
            return replace(cached, stamp=stamp)
    return None


# LLM: 错误投影保持原AUDIT_READ_FAILED的字段和行号，不暴露坏行正文。
# 函数用途: 为审计分片生成原稳定损坏报告。
def _read_error(path: Path, exc: BaseException, line: int) -> dict:
    return {"context": "memory_curator.audit_read", "error_code": "AUDIT_READ_FAILED",
            "error_type": type(exc).__name__, "path": str(path), "line": line}


# LLM: 二进制LF逐行严格解码与原整读等价；当前锚文件整读失败要求重定位，不能凭缓存认定cursor存在。
# 函数用途: 读取一份分片并保留全部原诊断，可直接交给有界窗口而不累积整文件。
def _read_slice(path: Path, cursor: AuditCursor | None, window: AuditWindow | None = None) -> AuditSlice:
    if not path.is_file():
        return AuditSlice()
    try:
        with path.open("rb") as handle:
            return _read_open(handle, path, cursor, window)
    except (OSError, UnicodeDecodeError) as exc:
        return AuditSlice(errors=[_read_error(path, exc, 0)], fatal=True)


# LLM: 预算达到后不再构造AuditRow或业务投影，仍流式校验全分片，保错误顺序、物理行号及FD核验。
# 函数用途: 有界构造审计输入并收齐尾部损坏，读取中改写则不缓存。
def _read_open(handle, path: Path, cursor: AuditCursor | None, window: AuditWindow | None = None) -> AuditSlice:
    stamp = file_stamp(os.fstat(handle.fileno()))
    if cursor is not None and cursor.stamp != stamp:
        raise _AuditRescan
    handle.seek(cursor.end if cursor else 0)
    result = AuditSlice(stamp=stamp, path=path)
    missing_errors = []
    line_number = cursor.line if cursor else 0
    for start, raw, end in _raw_lines(handle):
        line_number += 1
        payload = _parse_row(result, missing_errors, raw.decode("utf-8"), line_number)
        if payload is None or result.stopped:
            continue
        row = AuditRow(payload, start, end, line_number)
        if window is None:
            result.rows.append(row)
        else:
            result.stopped = _consume_row(window, row, (path.name, stamp))
    result.errors.extend(missing_errors)
    if file_stamp(os.fstat(handle.fileno())) != stamp:
        result.stamp = None
    return result


# LLM: 原read_text先做通用换行归一，再按LF切分；bytes.splitlines仅识别CR/LF，不会切开合法JSON内的NEL等Unicode字符。
# 函数用途: 流式返回原换行口径的行与精确原始字节边界，兼容CR、CRLF和LF。
def _raw_lines(handle):
    while raw := handle.readline():
        start = handle.tell() - len(raw)
        for line in raw.splitlines(keepends=True):
            end = start + len(line)
            yield start, line, end
            start = end


# LLM: 保持旧顺序：所有JSON/非对象错误在前，缺event_id错误在后；空白LF/CRLF/NEL语义不变。
# 函数用途: 校验一个物理行并返回临时JSON对象，预算外不再累积AuditRow或正式业务输入。
def _parse_row(result: AuditSlice, missing: list, text: str, line: int) -> dict | None:
    if not text.strip():
        return
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        result.errors.append(_read_error(result.path, exc, line))
        return
    if not isinstance(payload, dict):
        result.errors.append(_read_error(result.path, ValueError("JSONL row is not an object"), line))
        return
    if not str(payload.get("event_id") or "").strip():
        missing.append(_read_error(result.path, ValueError("audit row lacks event_id"), 0))
        return
    return payload


# LLM: 锚只来自原顺序首次发现或有界过滤器可证明的新ID；正文不进入pending/cache。
# 函数用途: 暂存精确物理行边界及有界历史前缀，批次结束统一冻结过滤器。
def _queue_cursor(window: AuditWindow, row: AuditRow, scope: tuple[str, FileStamp]) -> None:
    if not window.cacheable:
        return
    target = str(row.payload.get("event_id") or "").strip()
    if len(target) <= 256:
        name, stamp = scope
        position = AuditCursor(name, stamp, row.start, row.end, row.line, tuple(window.prefix), b"",
                               window.prefix_digest, window.prefix_last, window.prefix_count)
        window.pending.append((target, position))


# LLM: 预算逐项与原_collect_audit相同；只在确认首匹配后处理事件，缺游标不能从头重放。
# 函数用途: 消费一条审计行并决定是否到达原批量或字符上限。
def _consume_row(window: AuditWindow, row: AuditRow, scope: tuple[str, FileStamp]) -> bool:
    target = str(row.payload.get("event_id") or "").strip()
    if not window.found:
        note_seen(window.seen, target)
        window.found = target == window.request.target
        if window.found:
            _queue_cursor(window, row, scope)
        return False
    item = window.project(row.payload)
    chars = len(json.dumps(item.to_model(), ensure_ascii=False))
    if window.selected and (len(window.selected) >= window.request.limit or window.used_chars + chars > window.request.max_chars):
        return True
    if not note_seen(window.seen, target):
        _queue_cursor(window, row, scope)
    window.selected.append(item)
    window.used_chars += chars
    return len(window.selected) >= window.request.limit


# LLM: 局部选择只在全分片读成功后发布；UTF-8/IO失败丢弃本分片选择，Bloom多置位只能降低命中率。
# 函数用途: 限额构造对象但完整检测当前分片损坏，稳定干净位置才可以进入缓存。
def _consume_file(window: AuditWindow, path: Path, cursor: AuditCursor | None) -> bool:
    empty = cursor and cursor.end == cursor.stamp.size
    local = replace(window, selected=list(window.selected), pending=list(window.pending))
    result = AuditSlice(stamp=cursor.stamp) if empty else _read_slice(path, cursor, local)
    if cursor is not None and result.fatal:
        raise _AuditRescan
    window.errors.extend(result.errors)
    window.cacheable = (window.cacheable and not result.errors and result.stamp is not None
                        and cache_entry_trustworthy(result.stamp.mtime_ns))
    if not result.fatal:
        window.found, window.selected, window.pending = local.found, local.selected, local.pending
        window.used_chars = local.used_chars
    if result.stopped:
        return True
    _append_prefix(window, path.name, result.stamp)
    return False


# LLM: 旧前缀仅压缩内存表达，不跳过后续stat复核；摘要绑定名字和五字段stamp，删除或变更均失效。
# 函数用途: 保留最近512份明细，更早干净分片滚动进入固定长度摘要。
def _append_prefix(window: AuditWindow, name: str, stamp: FileStamp | None) -> None:
    if stamp is None:
        window.cacheable = False
        return
    if len(window.prefix) >= _AUDIT_CURSOR_PREFIX_MAX_COUNT:
        old_name, old_stamp = window.prefix.pop(0)
        window.prefix_digest = _prefix_digest(window.prefix_digest, old_name, old_stamp)
        window.prefix_last = old_name
        window.prefix_count += 1
    window.prefix.append((name, stamp))


# LLM: 固定编码只含文件名和stat，不含审计正文；同一序列可从当前目录重新计算核验。
# 函数用途: 将一份已消费分片折叠到累积前缀摘要。
def _prefix_digest(previous: str, name: str, stamp: FileStamp) -> str:
    material = json.dumps([previous, name, asdict(stamp)], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# LLM: 同一批的位置共享不可变seen；坏分片/缺游标/过大前缀不缓存，仅退回同算法重扫。
# 函数用途: 保存本批干净首位置并返回原缺游标诊断。
def _finish_window(window: AuditWindow):
    if window.request.target and not window.found:
        window.errors.append({"context": "memory_curator.audit_cursor", "error_code": "AUDIT_CURSOR_MISSING",
                              "event_id": window.request.target})
    if window.cacheable and not window.errors:
        seen = bytes(window.seen)
        for target, position in window.pending:
            _AUDIT_CURSORS.put(cursor_key(window.request.root, target), replace(position, seen=seen))
    return window.selected, window.errors


# LLM: 缓存只改变起点，物理文件/行顺序、投影、错误及预算始终由这一个算法负责。
# 函数用途: 从完整目录头或已复核锚行之后扫描原审计批次。
def _scan_window(window: AuditWindow, paths: list[Path], cursor: AuditCursor | None):
    start = next((i for i, path in enumerate(paths) if cursor and path.name == cursor.name), 0)
    for path in paths[start:]:
        current_cursor = cursor if cursor and path.name == cursor.name else None
        if _consume_file(window, path, current_cursor):
            break
    return _finish_window(window)


# LLM: 每次尝试新建局部状态，回退不能继承上次部分选择或错误；只复制有界元数据。
# 函数用途: 为冷扫描或已验证游标创建独立窗口，继承有界明细与累积摘要。
def _new_window(request: AuditRead, project: Callable, cursor: AuditCursor | None) -> AuditWindow:
    seen = bytearray(cursor.seen if cursor else bytes(_AUDIT_CURSOR_FILTER_BYTES))
    window = AuditWindow(request, project, bool(cursor) or not request.target, seen)
    window.prefix = list(cursor.prefix) if cursor else []
    if cursor:
        window.prefix_digest, window.prefix_last, window.prefix_count = cursor.prefix_digest, cursor.prefix_last, cursor.prefix_count
    return window


# LLM: 不建磁盘状态，不迁移旧cursor；IO/UTF-8使缓存锚不可见时同算法冷扫保留原缺游标错误。
# 函数用途: 收集精确audit事件之后的原有界输入。
def collect_audit_window(request: AuditRead, project: Callable):
    if not request.root.exists():
        return [], []
    request = replace(request, target=str(request.target or "").strip())
    paths = sorted(request.root.glob("*.jsonl"))
    cursor = _reuse_cursor(request, paths) if request.target else None
    try:
        return _scan_window(_new_window(request, project, cursor), paths, cursor)
    except _AuditRescan:
        return _scan_window(_new_window(request, project, None), paths, None)
