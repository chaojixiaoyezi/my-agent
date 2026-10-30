# LLM: 读口冻结canonical布局；写口仅在原消息锁及获胜线程CAS后冷转，清单不授予摘要覆盖权。
# 模块用途: 提供冷热消息扫描、完整行分页、幂等查找及耐久分卷转换和prepared恢复。
from __future__ import annotations

import hashlib
import json
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from ..io.cursor_cache import note_seen
from ..runtime_errors import DataCorruptionError, RecoverableRuntimeError
from .display_checkpoint import is_display_checkpoint
from .models import MessageLogEntry
from .store_io import complete_jsonl_end

TRANSCRIPT_ROTATE_BYTES = 1024 * 1024
TRANSCRIPT_ROTATE_ROWS = 128
_TRANSCRIPT_SEGMENT_BYTES = 1024 * 1024

# 消息去重索引最多缓存的线程文件数量。
_MAX_DEDUPE_INDEX_FILES = 64
# 单个消息文件去重索引最多保留的幂等键数量。
_MAX_DEDUPE_KEYS_PER_FILE = 16384


# LLM: recent保存首条非display键的精确地址，历史Bloom阳性需回查；64KiB尾锚及完整SHA256防中段改写后增长。
# 类用途: 用有界混合键表及原字节证据复用已验证JSON，不持有正文，旧字节仍分块校验。
@dataclass
class _TranscriptDedupeIndex:
    dev: int
    ino: int
    scanned_offset: int
    tail_anchor_offset: int
    tail_anchor_digest: bytes
    key_offsets: dict[str, tuple[int, int]]
    prefix_digest: bytes
    mtime_ns: int
    ctime_ns: int
    historical_seen: bytes


_DEDUPE_INDEX_CACHE: OrderedDict[str, _TranscriptDedupeIndex] = OrderedDict()
_DEDUPE_FILE_LOCKS: OrderedDict[str, threading.Lock] = OrderedDict()
_DEDUPE_INDEX_LOCK = threading.Lock()


# LLM: 全局锁仅保护有界LRU映射；单文件扫描持路径锁，不让慢盘或大文件阻塞其他owner的去重查询。
# 函数用途: 获取或创建有界的单路径扫描锁，防止跨路径去重查询互相串行阻塞。
def _dedupe_file_lock(cache_key: str) -> threading.Lock:
    with _DEDUPE_INDEX_LOCK:
        lock = _DEDUPE_FILE_LOCKS.get(cache_key)
        if lock is not None:
            _DEDUPE_FILE_LOCKS.move_to_end(cache_key)
            return lock
        lock = threading.Lock()
        _DEDUPE_FILE_LOCKS[cache_key] = lock
        while len(_DEDUPE_FILE_LOCKS) > _MAX_DEDUPE_INDEX_FILES * 2:
            _DEDUPE_FILE_LOCKS.popitem(last=False)
        return lock


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


# LLM: canonical分页和来源扫描共用严格UTF8解析；数值溢出及错误身份归数据损坏，仅来源扫描显式跳Unicode空白。
# 函数用途: 将一条完整原字节消息解析为本线程记录，不修改正文、元数据或持久状态。
def _message_from_line(line: bytes, thread_id: str, *, skip_blank: bool = False) -> MessageLogEntry | None:
    try:
        text = line.decode("utf-8")
        if skip_blank and not text.strip():
            return None
        raw = json.loads(text)
        if not isinstance(raw, dict):
            raise ValueError("message row must be a JSON object")
        entry = MessageLogEntry.from_dict(raw)
    except (ValueError, TypeError, UnicodeError, OverflowError) as exc:
        raise DataCorruptionError("invalid canonical message row") from exc
    if entry.thread_id != thread_id:
        raise DataCorruptionError("invalid canonical message row")
    return entry


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
        handle = TranscriptReader(path)
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
            entry = _message_from_line(line, thread_id)
            entries.append(entry)
            cursor = handle.tell()
    return entries, cursor


# LLM: 不包装UTF8、JSON及model异常，Unicode空白沿原语义跳过；未终止LF在解码之前拒绝。
# 函数用途: 按去重扫描原合同解析一条完整原始消息行。
def _dedupe_message_from_line(line: bytes) -> MessageLogEntry | None:
    if not line:
        raise DataCorruptionError("canonical transcript truncated during dedupe scan")
    if not line.endswith(b"\n"):
        raise DataCorruptionError("canonical transcript has an incomplete final row")
    text = line.decode("utf-8")
    return MessageLogEntry.from_dict(json.loads(text)) if text.strip() else None


# LLM: 只读取水位前最多64KiB；该锚是快速否定条件，完整前缀摘要仍是改写检测权威。
# 函数用途: 计算已验证前缀的64KiB尾块锚，供后续增量扫描核对。
def _dedupe_tail_anchor(handle: BinaryIO, offset: int) -> tuple[int, bytes]:
    start = max(0, offset - 65536)
    handle.seek(start)
    return start, hashlib.blake2b(handle.read(offset - start), digest_size=16).digest()


# LLM: 局部尾锚不能证明整个前缀；元数据或锚异常重扫，其他命中按64KiB块校验完整旧字节但不解析旧JSON。
#   传入空hasher，成功后其状态包含旧前缀；失败时调用方必须丢弃，不能拿部分摘要发布新水位。
# 函数用途: 核对缓存对应文件身份和完整旧前缀，防止等长中段改写加新尾绕过失效。
def _dedupe_index_matches(handle: BinaryIO, index: _TranscriptDedupeIndex, status: os.stat_result, end: int,
                          hasher) -> bool:
    if (index.dev, index.ino) != (status.st_dev, status.st_ino) or not 0 < index.scanned_offset <= end:
        return False
    if end == index.scanned_offset and (index.mtime_ns, index.ctime_ns) != (status.st_mtime_ns, status.st_ctime_ns):
        return False
    if _dedupe_tail_anchor(handle, index.scanned_offset) != (index.tail_anchor_offset, index.tail_anchor_digest):
        return False
    handle.seek(0)
    remaining = index.scanned_offset
    while remaining:
        block = handle.read(min(65536, remaining))
        if not block:
            return False
        hasher.update(block)
        remaining -= len(block)
    return hasher.digest() == index.prefix_digest


# LLM: 旧地址命中必须读回完整行并核对真实key及display角色；不匹配或解析失败只触发从0重扫，不吞最终损坏异常。
# 函数用途: 从已索引字节地址恢复首个匹配消息，不能直接信任缓存中的旧行身份。
def _indexed_dedupe_message(handle: BinaryIO, key: str, address: tuple[int, int]) -> MessageLogEntry | None:
    start, length = address
    handle.seek(start)
    line = handle.read(length)
    if len(line) != length:
        return None
    try:
        entry = _dedupe_message_from_line(line)
    except (DataCorruptionError, ValueError, TypeError, AttributeError, OverflowError):
        return None
    if entry is not None and not is_display_checkpoint(entry) and str(entry.metadata.get("dedupe_key") or "").strip() == key:
        return entry
    return None


# LLM: 与cursor_cache.note_seen使用同一三探针算法；只读位图，不复制整份历史过滤器或修改覆盖事实。
# 函数用途: 查询一个历史键是否可能存在，阳性必须回磁盘核实。
def _dedupe_seen(seen: bytes | bytearray, key: str) -> bool:
    digest = hashlib.blake2b(key.encode("utf-8", "surrogatepass"), digest_size=12).digest()
    indexes = (int.from_bytes(digest[i:i + 4], "little") % (len(seen) * 8) for i in (0, 4, 8))
    return all(seen[index // 8] & (1 << (index % 8)) for index in indexes)


# LLM: 历史阳性只按原字节地址验证，不依据Bloom直接判重；完整消费给定前缀防首匹配掩盖后续坏行，退出恢复扫描位置。
# 函数用途: 找到历史键的首地址，供Bloom阳性验证及重新进入recent使用。
def _historical_dedupe_address(handle: BinaryIO, key: str, end: int) -> tuple[int, int] | None:
    resume, found = handle.tell(), None
    try:
        handle.seek(0)
        while handle.tell() < end:
            start = handle.tell()
            raw = handle.readline(end - start)
            row = _dedupe_message_from_line(raw)
            if (found is None and row is not None and not is_display_checkpoint(row)
                    and str(row.metadata.get("dedupe_key") or "").strip() == key):
                found = start, len(raw)
        return found
    finally:
        handle.seek(resume)


# LLM: 传入地址已被证明是首出现；旧键重新活跃时可回recent，淘汰进入Bloom，过滤器不删除位。
# 函数用途: 保持最近16384个精确键及首地址，超过阈值不销毁缓存。
def _promote_dedupe_key(offsets: OrderedDict, seen: bytearray, key: str, address: tuple[int, int]) -> None:
    offsets[key] = address
    offsets.move_to_end(key)
    if len(offsets) > _MAX_DEDUPE_KEYS_PER_FILE:
        old, _address = offsets.popitem(last=False)
        note_seen(seen, old)


# LLM: 历史Bloom阳性先核首地址再入recent；假阳性新键也正常进入，不能把当前重复行覆盖旧首地址。
# 函数用途: 扫描一条键后更新真实recent集合及历史位图。
def _remember_dedupe_key(handle, offsets: OrderedDict, seen: bytearray, key: str, address: tuple[int, int]) -> None:
    if not key:
        return
    if key in offsets:
        offsets.move_to_end(key)
        return
    if _dedupe_seen(seen, key):
        address = _historical_dedupe_address(handle, key, address[0]) or address
    _promote_dedupe_key(offsets, seen, key, address)


# LLM: 固定EOF前所有新行必须校验，命中也不提前返回；空白入摘要，Bloom只保存被淘汰键，不限制扫描覆盖。
# 函数用途: 单遍增量校验新尾、更新摘要与混合键表，保留首匹配。
def _scan_dedupe_tail(handle: BinaryIO, end: int, key: str, key_offsets: OrderedDict,
                      existing: MessageLogEntry | None, hasher, seen: bytearray) -> MessageLogEntry | None:
    while handle.tell() < end:
        start = handle.tell()
        line = handle.readline(end - start)
        entry = _dedupe_message_from_line(line)
        hasher.update(line)
        if entry is None or is_display_checkpoint(entry):
            continue
        row_key = str(entry.metadata.get("dedupe_key") or "").strip()
        _remember_dedupe_key(handle, key_offsets, seen, row_key, (start, len(line)))
        if existing is None and row_key == key:
            existing = entry
    return existing


# LLM: 原append-once锁仍是跨进程幂等权威；全局锁仅保护有界LRU字典，单路径锁保护本文件增量扫描，不阻塞其他文件。
#   缓存只省略已验证前缀；先恢复其首匹配再扫描新尾，避免迟到同键覆盖首条；空key保持原全扫行为。
# 函数用途: 校验水位、尾锚及前缀字节后增量解析新尾，Bloom命中历史时按原文件核实首匹配。
def find_message_dedupe(path: Path, key: str) -> MessageLogEntry | None:
    cache_key = str(path)
    with _dedupe_file_lock(cache_key):
        cold = cold_message_lookup(path, f'dedupe:{key}') if key else None
        hot = _find_message_dedupe_locked(path, cache_key, key)
        return cold[0] if cold is not None else hot


# LLM: 调用方已持路径锁；全局LRU锁不覆盖I/O，失败直接在当前锁内冷扫，不递归获取路径锁。
# 函数用途: 复核完整前缀后复用recent/Bloom索引，历史可能命中才额外扫描旧键。
def _find_message_dedupe_locked(path: Path, cache_key: str, key: str) -> MessageLogEntry | None:
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        with _DEDUPE_INDEX_LOCK:
            _DEDUPE_INDEX_CACHE.pop(cache_key, None)
        return None
    with handle:
        with _DEDUPE_INDEX_LOCK:
            index = _DEDUPE_INDEX_CACHE.pop(cache_key, None)
        status = os.fstat(handle.fileno())
        end = handle.seek(0, 2)
        cursor, key_offsets, existing = 0, OrderedDict(), None
        seen = bytearray(65536)
        hasher = hashlib.sha256()
        if key and index is not None and _dedupe_index_matches(handle, index, status, end, hasher):
            cursor, key_offsets = index.scanned_offset, OrderedDict(index.key_offsets)
            seen = bytearray(index.historical_seen)
            existing, valid = _cached_dedupe_match(handle, key, key_offsets, seen, cursor)
            if not valid:
                cursor, key_offsets, seen = 0, OrderedDict(), bytearray(65536)
                hasher = hashlib.sha256()
        else:
            hasher = hashlib.sha256()
        handle.seek(cursor)
        existing = _scan_dedupe_tail(handle, end, key, key_offsets, existing, hasher, seen)
        if end:
            anchor_offset, anchor_digest = _dedupe_tail_anchor(handle, end)
            new_index = _TranscriptDedupeIndex(
                dev=status.st_dev, ino=status.st_ino, scanned_offset=end,
                tail_anchor_offset=anchor_offset, tail_anchor_digest=anchor_digest, key_offsets=key_offsets,
                prefix_digest=hasher.digest(),
                mtime_ns=status.st_mtime_ns, ctime_ns=status.st_ctime_ns,
                historical_seen=bytes(seen),
            )
            with _DEDUPE_INDEX_LOCK:
                _DEDUPE_INDEX_CACHE[cache_key] = new_index
                while len(_DEDUPE_INDEX_CACHE) > _MAX_DEDUPE_INDEX_FILES:
                    _DEDUPE_INDEX_CACHE.popitem(last=False)
        return existing


# LLM: 完整旧前缀已复核才调用；recent或Bloom阳性均需原行读回，失配由调用方同锁冷扫，不递归获取锁。
# 函数用途: 核实首匹配并让已确认历史键回温，单独返回缓存是否仍有效。
def _cached_dedupe_match(handle, key, offsets, seen, cursor):
    address = offsets.get(key)
    if address is None and _dedupe_seen(seen, key):
        address = _historical_dedupe_address(handle, key, cursor)
    if address is None:
        return None, True
    existing = _indexed_dedupe_message(handle, key, address)
    if existing is None:
        return None, False
    _promote_dedupe_key(offsets, seen, key, address)
    return existing, True


# LLM: 固定LF尾界内严格解析，空白仍计hash；可选地址visitor只生成临时引用，原inode及取消在读取边界检查。
# 函数用途: 不保存全量正文地扫描canonical消息及原行地址，坏行、截断或换文件拒绝整个来源。
def scan_message_snapshot(path: Path, thread_id: str, through: int, visitor, *, address_visitor=None,
                          expected_identity=None, interrupt_check=None) -> str:
    import hashlib

    from .compact_guard import raise_if_compact_interrupted

    raise_if_compact_interrupted(interrupt_check)
    digest = hashlib.sha256()
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        if through == 0:
            return digest.hexdigest()
        raise
    with handle:
        stat = os.fstat(handle.fileno())
        if expected_identity is not None and expected_identity != (stat.st_dev, stat.st_ino):
            raise DataCorruptionError("canonical message source changed during selection")
        _validate_boundary(handle, through, handle.seek(0, 2))
        handle.seek(0)
        while handle.tell() < through:
            raise_if_compact_interrupted(interrupt_check)
            offset = handle.tell()
            line = handle.readline(through - handle.tell())
            if not line.endswith(b"\n"):
                raise DataCorruptionError("canonical transcript truncated during source scan")
            digest.update(line)
            entry = _message_from_line(line, thread_id, skip_blank=True)
            if entry is not None:
                visitor(entry)
                if address_visitor is not None:
                    address_visitor(entry, offset, len(line), hashlib.sha256(line).digest())
            del entry, line
        raise_if_compact_interrupted(interrupt_check)
    return digest.hexdigest()


# LLM: 消息清单只描述物理冷热分段，不赋予Compact覆盖；pending按热文件inode选择旧/新视图，读口不写修复。
# 函数用途: 恢复固定大小的消息布局，拒绝不完整或跨线程的清单。
def transcript_manifest(path: Path):
    from ..common.nofollow_fs import read_bytes_beneath

    raw = read_bytes_beneath(path.parent, (path.with_suffix('.manifest.json').name,), max_bytes=65536)
    if raw is None:
        return None
    try:
        state = json.loads(raw)
        if state.get('phase') == 'prepared':
            stamp = path.stat()
            identity = [stamp.st_dev, stamp.st_ino]
            if identity == state['old_identity']:
                state = state['before']
            elif identity == state['new_identity']:
                state = state['after']
            else:
                raise ValueError('pending transcript identity mismatches')
        if state is None:
            return None
        if (type(state) is not dict or state.get('schema') != 'conversation_transcript_segments.v1'
                or type(state.get('thread_id')) is not str or not state['thread_id']
                or Path(state['thread_id']).name != state['thread_id'] or state['thread_id'] != path.stem
                or any(type(state.get(name)) is not int or not 0 <= state[name] < 2 ** 63
                       for name in ('hot_base', 'segment_count', 'cold_message_count'))):
            raise ValueError('invalid transcript manifest')
        return state
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise DataCorruptionError('canonical transcript segment manifest is invalid') from exc


# LLM: writer必须已持原消息锁；回滚只撤销未rename的布局计划，冷孤儿不成为历史；已rename则完成新清单。
# 函数用途: 在后续追加前收口崩溃留下的冷热文件转换，防止重放阶段覆盖迟到追加。
def recover_transcript_transition(path: Path):
    from ..common.nofollow_fs import read_bytes_beneath, write_text_atomic_beneath

    name = path.with_suffix('.manifest.json').name
    raw = read_bytes_beneath(path.parent, (name,), max_bytes=65536)
    if raw is None:
        return
    state = json.loads(raw)
    if state.get('phase') != 'prepared':
        return
    selected = transcript_manifest(path)
    if selected is None:
        # 空旧清单用显式零状态恢复，避免删除事务记录引入额外失败窗。
        selected = {'schema': 'conversation_transcript_segments.v1', 'thread_id': state['after']['thread_id'],
                    'hot_base': 0, 'segment_count': 0, 'cold_message_count': 0,
                    'index': None, 'chain_sha256': hashlib.sha256(b'').hexdigest()}
    write_text_atomic_beneath(path.parent, (name,), json.dumps(selected, sort_keys=True) + '\n')


# LLM: 句柄冻结同次清单、完整尾界及热FD；冷热物理位置对外统一为从会话开始的虚拟字节坐标。
# 类用途: 为显式历史/分页/来源查询提供可seek的完整会话，不让模型热选源打开冷段。
class TranscriptReader:
    # LLM: 默认冻结完整LF尾界；全量读取可显式选择物理EOF以校验半行，冷数据仍延迟读取且不创建索引或锁。
    # 函数用途: 冻结当前冷热布局及两种尾界，后续轮转不重绑定这个读取快照。
    def __init__(self, path: Path, *, complete_only=True):
        from .store_io import ImmutableJsonIndex

        self.path = path
        for _attempt in range(4):
            self.manifest = transcript_manifest(path)
            self.hot = path.open('rb')
            try:
                current = transcript_manifest(path)
                stamp = os.fstat(self.hot.fileno())
                expected = self.manifest.get('hot_identity') if self.manifest else None
                if current == self.manifest and (expected is None or expected == [stamp.st_dev, stamp.st_ino]):
                    break
            except BaseException:
                self.hot.close()
                raise
            self.hot.close()
        else:
            raise DataCorruptionError('canonical transcript layout changed during snapshot open')
        self.base = self.manifest['hot_base'] if self.manifest else 0
        physical_end = self.hot.seek(0, 2)
        self.complete_end = self.base + complete_jsonl_end(self.hot, physical_end)
        self.end = self.complete_end if complete_only else self.base + physical_end
        self.position, self.cached = 0, None
        self.index = ImmutableJsonIndex(path.parent, ('archive', self.manifest['thread_id'], 'index')) if self.manifest else None

    # LLM: 只管理本次句柄，不改变持久布局。
    # 函数用途: 进入只读快照上下文。
    def __enter__(self):
        return self

    # LLM: 失败和提前退出都关闭原热FD，冷块仅在内存中持有。
    # 函数用途: 释放本次快照资源。
    def __exit__(self, *_args):
        self.hot.close()
        self.cached = None

    # LLM: 坐标始终是虚拟字节，不使用字符数或物理热文件偏移。
    # 函数用途: 返回当前逻辑读取位置。
    def tell(self):
        return self.position

    # LLM: seek允许EOF，不能越过冻结尾界；不读取或校验目标行正文。
    # 函数用途: 在完整冷热消息空间内定位。
    def seek(self, offset, whence=0):
        position = offset if whence == 0 else (self.position if whence == 1 else self.end) + offset
        if whence not in (0, 1, 2) or not 0 <= position <= self.end:
            raise DataCorruptionError('message cursor exceeds canonical transcript')
        self.position = position
        return position

    # LLM: 元数据来自清单封印的不可变根；二分仅服务显式冷查询，不进入热选源。
    # 函数用途: 按虚拟偏移查找包含该字节的冷段。
    def _segment(self):
        if self.cached is not None and self.cached[0]['start'] <= self.position < self.cached[0]['end']:
            return self.cached
        from ..common.nofollow_fs import read_bytes_beneath

        low, high = 1, self.manifest['segment_count']
        while low <= high:
            sequence = (low + high) // 2
            descriptor = self.index.get(self.manifest['index'], f'segment:{sequence}')
            if not isinstance(descriptor, dict):
                raise DataCorruptionError('canonical transcript segment is missing')
            if self.position < descriptor['start']:
                high = sequence - 1
            elif self.position >= descriptor['end']:
                low = sequence + 1
            else:
                size = descriptor['end'] - descriptor['start']
                if not 0 < size <= 64 * 1024 * 1024:
                    raise DataCorruptionError('canonical transcript segment exceeds byte budget')
                raw = read_bytes_beneath(self.path.parent, ('archive', self.manifest['thread_id'], f'seg-{sequence:020d}.jsonl'), max_bytes=size)
                if raw is None or len(raw) != size or hashlib.sha256(raw).hexdigest() != descriptor['sha256']:
                    raise DataCorruptionError('canonical transcript segment digest or length mismatches')
                self.cached = descriptor, raw
                return self.cached
        raise DataCorruptionError('message cursor is outside canonical segments')

    # LLM: 只在冻结尾界内逐段读取；每个冷段先核完整SHA，热段沿保留的同一FD读取，短读不能当EOF。
    # 函数用途: 从虚拟会话中读取明确数量原始字节。
    def read(self, size=-1):
        remaining = self.end - self.position if size < 0 else min(size, self.end - self.position)
        parts = []
        while remaining:
            if self.position >= self.base:
                wanted = remaining
                self.hot.seek(self.position - self.base)
                raw = self.hot.read(wanted)
            else:
                descriptor, block = self._segment()
                wanted = min(remaining, descriptor['end'] - self.position)
                begin = self.position - descriptor['start']
                raw = block[begin:begin + wanted]
            if len(raw) != wanted:
                raise DataCorruptionError('canonical transcript truncated during read')
            parts.append(raw)
            self.position += wanted
            remaining -= wanted
        return b''.join(parts)

    # LLM: 冷段只在完整LF边界切分，跨段仍保持同一逻辑记录；字节预算由原分页调用方裁决。
    # 函数用途: 读取一条真实消息行而不是按Unicode分隔符拆分正文。
    def readline(self, size=-1):
        if self.position == self.end or size == 0:
            return b''
        if self.position >= self.base:
            self.hot.seek(self.position - self.base)
            amount = self.end - self.position if size < 0 else min(size, self.end - self.position)
            raw = self.hot.readline(amount)
        else:
            descriptor, block = self._segment()
            begin = self.position - descriptor['start']
            end = block.find(b'\n', begin) + 1
            if not end:
                raise DataCorruptionError('canonical transcript segment ends inside a row')
            if size >= 0:
                end = min(end, begin + size)
            raw = block[begin:end]
        if not raw:
            raise DataCorruptionError('canonical transcript truncated during line read')
        self.position += len(raw)
        return raw


# LLM: 冷索引仅指向本线程不可变冷段原行；必须按实际字节hash读回，不把缓存地址当证据。
# 函数用途: 为精确ID及幂等键恢复冷消息与原虚拟位置。
def cold_message_lookup(path: Path, key: str, *, reader=None):
    if reader is None:
        if not path.exists() and transcript_manifest(path) is None:
            return None
        with TranscriptReader(path) as frozen:
            return cold_message_lookup(path, key, reader=frozen)
    manifest = reader.manifest
    if manifest is None:
        return None
    address = reader.index.get(manifest['index'], key)
    if address is None:
        return None
    reader.seek(address['offset'])
    raw = reader.read(address['length'])
    if hashlib.sha256(raw).hexdigest() != address['sha256']:
        raise DataCorruptionError('canonical cold message digest mismatches')
    entry = _message_from_line(raw, manifest['thread_id'])
    actual = f'id:{entry.message_id}' if key.startswith('id:') else f'dedupe:{entry.metadata.get("dedupe_key", "")}'
    if actual != key:
        raise DataCorruptionError('canonical cold message identity mismatches')
    return entry, address


# LLM: 仅原线程CAS成功后、持消息锁调用；检查点四元边界、thread scope及精确覆盖都成立才可物理冷转。
# 对非终态、活动索引或未知任务保守pin；终态任务仍须被本次thread摘要精确覆盖，原字节保存在冷段。
# 函数用途: 将已覆盖且无活跃任务pin的前缀冷存，保留边界本行及全部迟到尾部。
def rotate_committed_transcript(storage, thread):
    from ..common.nofollow_fs import write_bytes_atomic_beneath, write_text_atomic_beneath
    from .compact_checkpoint import _cold_checkpoint
    from .compact_scope import THREAD_COMPACT_SCOPE, CompactScope
    from .compact_summary_view import _IndexedMessageCoverage
    from .message_selection import _validated_positions, message_position_index
    from .store_io import ImmutableJsonIndex

    path = storage.message_path(thread.thread_id)
    if not path.exists() or not thread.compact_checkpoint_id:
        return
    compact_root = storage.root.parent / 'compact'
    row = _cold_checkpoint(compact_root, thread.thread_id, thread.compact_checkpoint_id)
    if (row is None or row.get('schema') != 'conversation_compact_checkpoint.v3'
            or CompactScope.from_dict(row['scope']) != THREAD_COMPACT_SCOPE
            or row.get('source_kind') not in {'transcript', 'transcript_and_tool_archive'}
            or row['generation'] != thread.compact_generation
            or row['source_end_message_id'] != thread.compacted_through_message_id
            or row['source_end_byte_offset'] != thread.compacted_through_byte_offset
            or row['summary_sha256'] != hashlib.sha256(thread.summary.encode('utf-8')).hexdigest()
            or 'carried_view' not in row):
        return
    recover_transcript_transition(path)
    before = transcript_manifest(path)
    base = before['hot_base'] if before else 0
    through = complete_message_offset(path)
    positions = message_position_index(path, through)
    _validated_positions(positions, thread.thread_id)
    boundary = positions.by_id.get(thread.compacted_through_message_id)
    if boundary is None or base + boundary.address.offset + boundary.address.length != thread.compacted_through_byte_offset:
        return
    cut = boundary.address.offset
    count = sum(position.address.offset < cut for position in positions.entries)
    if not cut or (cut < TRANSCRIPT_ROTATE_BYTES and count < TRANSCRIPT_ROTATE_ROWS):
        return
    coverage_index = ImmutableJsonIndex(compact_root, ('evidence', thread.thread_id, 'indexes'))
    meta = row['carried_view']
    coverage = _IndexedMessageCoverage(meta['recent_messages'], coverage_index, meta['messages'], meta['message_count'])
    task_pins = {}
    with path.open('rb') as handle:
        while handle.tell() < cut:
            raw = handle.readline(cut - handle.tell())
            entry = _message_from_line(raw, thread.thread_id, skip_blank=True)
            if entry is None:
                continue
            if _entry_has_task_pin(storage, thread, entry, task_pins):
                return
            if entry.role in {'user', 'assistant'} and entry.message_id not in coverage:
                from .models import is_audit_background_transcript_entry

                if not is_audit_background_transcript_entry(entry):
                    return
    after = dict(before) if before else {
        'schema': 'conversation_transcript_segments.v1', 'thread_id': thread.thread_id,
        'hot_base': 0, 'segment_count': 0, 'cold_message_count': 0,
        'index': None, 'chain_sha256': hashlib.sha256(b'').hexdigest(),
    }
    index = ImmutableJsonIndex(path.parent, ('archive', thread.thread_id, 'index'))
    with path.open('rb') as handle:
        start, lines, length = base, [], 0
        while handle.tell() < cut:
            raw = handle.readline(cut - handle.tell())
            if len(raw) > 64 * 1024 * 1024:
                raise MessagePageBudgetExceeded('canonical message row exceeds cold segment budget')
            if lines and length + len(raw) > _TRANSCRIPT_SEGMENT_BYTES:
                _save_transcript_segment(path, after, index, start, b''.join(lines))
                start += length
                lines, length = [], 0
            lines.append(raw)
            length += len(raw)
        if lines:
            _save_transcript_segment(path, after, index, start, b''.join(lines))
        handle.seek(cut)
        tail = handle.read()
    after.update({'hot_base': base + cut, 'checkpoint_id': thread.compact_checkpoint_id,
                  'generation': thread.compact_generation, 'boundary_message_id': thread.compacted_through_message_id,
                  'summary_sha256': row['summary_sha256']})
    stage = f'.{path.name}.rotation-{after["segment_count"]}.stage'
    write_bytes_atomic_beneath(path.parent, (stage,), tail)
    old_stat, new_stat = path.stat(), path.with_name(stage).stat()
    after['hot_identity'] = [new_stat.st_dev, new_stat.st_ino]
    pending = {'phase': 'prepared', 'before': before, 'after': after,
               'old_identity': [old_stat.st_dev, old_stat.st_ino], 'new_identity': [new_stat.st_dev, new_stat.st_ino]}
    write_text_atomic_beneath(path.parent, (path.with_suffix('.manifest.json').name,), json.dumps(pending, sort_keys=True) + '\n')
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.replace(stage, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        os.close(directory)
    write_text_atomic_beneath(path.parent, (path.with_suffix('.manifest.json').name,), json.dumps(after, sort_keys=True) + '\n')


# LLM: 原TaskLink及当前线程活动索引是任务生命周期事实；缺失/损坏/错线程均保守保留，不靠消息文字猜终态。
# 函数用途: 在一次持锁冷转中按准确任务身份缓存pin判断，终态已覆盖历史不永久阻挡分卷。
def _entry_has_task_pin(storage, thread, entry, pins):
    from .models import THREAD_TASK_LINK_INACTIVE_STATUSES
    from .store_tasks import _read_task_link

    for name in ('task_id', 'conversation_task_id', 'background_task_id'):
        task_id = str(entry.metadata.get(name) or '')
        if not task_id:
            continue
        if task_id not in pins:
            link, error = _read_task_link(storage.task_path(task_id), task_id)
            pins[task_id] = (task_id in thread.active_task_ids or error is not None or link is None
                             or link.thread_id != thread.thread_id
                             or str(link.status).strip().lower() not in THREAD_TASK_LINK_INACTIVE_STATUSES)
        if pins[task_id]:
            return True
    return False


# LLM: 原字节先耐久保存，再更新不可变地址根；首ID/首幂等键不能被后来相同身份覆盖，display也保存原样。
# 函数用途: 保存单个完整冷段及精确来源/幂等索引，不发布消息清单。
def _save_transcript_segment(path, manifest, index, start, raw):
    from ..common.nofollow_fs import write_bytes_atomic_beneath

    sequence = manifest['segment_count'] + 1
    thread_id = manifest['thread_id']
    write_bytes_atomic_beneath(path.parent, ('archive', thread_id, f'seg-{sequence:020d}.jsonl'), raw)
    root, offset = manifest['index'], start
    import io

    for line in io.BytesIO(raw):
        entry = _message_from_line(line, thread_id, skip_blank=True)
        manifest['chain_sha256'] = hashlib.sha256(bytes.fromhex(manifest['chain_sha256']) + line).hexdigest()
        if entry is not None:
            manifest['cold_message_count'] += 1
            address = {'offset': offset, 'length': len(line), 'sha256': hashlib.sha256(line).hexdigest()}
            keys = [f'id:{entry.message_id}']
            dedupe = str(entry.metadata.get('dedupe_key') or '').strip()
            if dedupe and not is_display_checkpoint(entry):
                keys.append(f'dedupe:{dedupe}')
            for key in keys:
                if index.get(root, key) is None:
                    root = index.put(root, key, address)
        offset += len(line)
    descriptor = {'start': start, 'end': start + len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
    manifest['index'] = index.put(root, f'segment:{sequence}', descriptor)
    manifest['segment_count'] = sequence
