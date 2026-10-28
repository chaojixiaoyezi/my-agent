# LLM: 进程内位置索引只是原canonical文件的可丢弃投影；旧前缀逐字节校验，selector不能授予覆盖或修改缓存。
# 模块用途: 按字节预算缓存最多512份位置索引，惰性复制选择字段，并共用首地址和逐行错误。
from __future__ import annotations

import copy
import hashlib
import os
import threading
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType

from ..runtime_errors import DataCorruptionError
from .compact_guard import raise_if_compact_interrupted
from .display_checkpoint import is_display_checkpoint
from .message_replay import MessageRowAddress, MessageSnapshotRows
from .message_scan import _dedupe_tail_anchor, _validate_boundary
from .message_scan import scan_message_snapshot as scan_message_snapshot
from .models import MessageLogEntry
from .store_io import json_row, jsonl_error

MessageSelectorFactory = Callable[[Mapping[str, int]], Callable[[MessageLogEntry], bool]]

_POSITION_CACHE = OrderedDict()
_POSITION_CACHE_LOCK = threading.Lock()
# 消息物理位置缓存最多保留的行索引条数。
_POSITION_CACHE_MAX_ENTRIES = 131072
# 消息物理位置缓存可保留的累计估算字节上限。
_POSITION_CACHE_MAX_BYTES = 16 * 1024 * 1024
# 单个消息位置索引可累计保留的轻量字段字节上限，正文仍按需读取。
_LIGHT_FIELDS_MAX_BYTES = 128 * 1024
_POSITION_CACHE_MAX_FILES = 512
_UNSET = object()


# LLM: 只存原行身份/字节证据及有界非正文投影；大元数据和正文由代理按需读取，不截断字段。
# 类用途: 保存同一行的物理行号、消息身份、地址及可选轻量字段。
@dataclass(frozen=True, slots=True)
class _Position:
    line_number: int
    message_id: str
    thread_id: str
    address: MessageRowAddress
    row: MessageLogEntry | None
    is_display: bool


# LLM: prefix_digest覆盖全部空白/坏行原字节，errors含物理行号；by_id只报告目标之前错误，selection拒绝全部错误。
# 类用途: 保存已验证前缀及尾锚，不保留正文或持久状态。
@dataclass(frozen=True, slots=True)
class _PositionIndex:
    identity: tuple[int, int]
    through: int
    digest: bytes
    anchor: tuple[int, bytes]
    entries: tuple[_Position, ...]
    by_id: dict[str, _Position]
    errors: tuple[tuple[int, dict, bool], ...]
    lines: int
    size_bytes: int
    light_bytes: int


# LLM: 必须在调用方冻结的范围内分块核对；不解析JSON，不信任仅尾锚，短读视为中途改写。
# 函数用途: 计算完整前缀SHA256，可复用hasher状态继续增量扫描。
def _prefix_hasher(handle, through: int):
    digest = hashlib.sha256()
    handle.seek(0)
    remaining = through
    while remaining:
        raw = handle.read(min(65536, remaining))
        if not raw:
            raise DataCorruptionError("canonical message source changed during selection")
        digest.update(raw)
        remaining -= len(raw)
    return digest


# LLM: 选择器收到MessageLogEntry子类，所有字段保持原值；正文及超大metadata按原行hash读回，不以空串代替正文。
# 类用途: 延迟物化大字段，让完整身份表就绪后筛选无需再次解析整份历史。
class _SelectionRow(MessageLogEntry):
    # LLM: 仅绑定地址与描述符；首个字段访问才复制轻量行，predicate不读字段时零deepcopy。
    # 函数用途: 建立与原消息兼容的惰性代理，不提前复制缓存的嵌套metadata。
    def __init__(self, handle, position: _Position):
        object.__setattr__(self, "_handle", handle)
        object.__setattr__(self, "_position", position)
        object.__setattr__(self, "_loaded", None)
        object.__setattr__(self, "_light", _UNSET)

    # LLM: 首次访问复制轻量投影以隔离嵌套修改；正文仍核原行hash，缓存仅限本次predicate。
    # 函数用途: 按需提供完整字段，并保证选择器修改不会污染进程索引。
    def __getattribute__(self, name):
        if name not in MessageLogEntry.__dataclass_fields__:
            return object.__getattribute__(self, name)
        row = object.__getattribute__(self, "_light")
        if row is _UNSET:
            position = object.__getattribute__(self, "_position")
            row = copy.deepcopy(position.row) if position.row is not None else None
            object.__setattr__(self, "_light", row)
        if row is not None and name != "content":
            return getattr(row, name)
        loaded = object.__getattribute__(self, "_loaded")
        if loaded is None:
            position = object.__getattribute__(self, "_position")
            handle = object.__getattribute__(self, "_handle")
            handle.seek(position.address.offset)
            raw = handle.read(position.address.length)
            if hashlib.sha256(raw).digest() != position.address.digest:
                raise DataCorruptionError("canonical message source changed during selection")
            import json

            loaded = MessageLogEntry.from_dict(json.loads(raw))
            object.__setattr__(self, "_loaded", loaded)
        return getattr(loaded, name)

    # LLM: 延迟代理与普通MessageLogEntry按原字段值比较；比较触及正文时仍走原行hash读取，不比较代理内部状态。
    # 函数用途: 保持选择器用消息值相等判断时的原有语义。
    def __eq__(self, other):
        if not isinstance(other, MessageLogEntry):
            return NotImplemented
        return all(getattr(self, name) == getattr(other, name) for name in MessageLogEntry.__dataclass_fields__)


# LLM: 一条原行只解析一次；JSON错误与model错误分开，by_id不因别的行model错误改变旧读取合同。
# 函数用途: 把原行转为轻量位置记录，坏行记账供各入口按原语义解释。
def _position_line(path, raw, number, offset, errors):
    address = MessageRowAddress(offset, len(raw), hashlib.sha256(raw).digest())
    try:
        text = raw.decode("utf-8")
        if not text.strip():
            return None, 0
        value, error = json_row(text, context="conversation.messages.by_id", path=path, line_number=number)
        if error is not None:
            errors.append((number, error, False))
            return None, 0
        message_id = str(value.get("message_id") or "")
        try:
            row = MessageLogEntry.from_dict(value)
        except Exception as exc:
            errors.append((number, jsonl_error(exc, "conversation.messages.by_id", path=path, line_number=number), True))
            return _Position(number, message_id, str(value.get("thread_id") or ""), address, None, False), 128
        light_size = len(repr(row.metadata).encode("utf-8", "surrogatepass")) + 256 + len(message_id)
        light = replace(row, content="") if light_size <= 4096 else None
        base_size = 256 + len(message_id.encode("utf-8", "surrogatepass")) + len(row.thread_id)
        return _Position(number, message_id, row.thread_id, address, light, is_display_checkpoint(row)), base_size + (light_size if light is not None else 0)
    except UnicodeError as exc:
        errors.append((number, jsonl_error(exc, "conversation.messages.by_id", path=path, line_number=number), False))
        return None, 0


# LLM: 轻量字段总预算固定128KiB，超预算保留完整地址并懒读字段，不截断metadata或伪造空值。
# 函数用途: 限制消息字段驻留，防含native工具历史的metadata随文件体积增长。
def _bounded_position(position: _Position, size: int, light_bytes: int):
    if position.row is None:
        return position, size, light_bytes
    if light_bytes + size <= _LIGHT_FIELDS_MAX_BYTES:
        return position, size, light_bytes + size
    size = 256 + len(position.message_id.encode("utf-8", "surrogatepass")) + len(position.thread_id)
    return replace(position, row=None), size, light_bytes


# LLM: dev/ino绑定且完整旧前缀改变即重建；LRU锁内无I/O，最多512文件且仍受行数及16MiB预算约束。
# 函数用途: 单遍解析新增字节，保留轻量地址和物理行号，供选择和精确ID定位共用。
def message_position_index(path: Path, through: int, interrupt_check=None) -> _PositionIndex:
    with path.open("rb") as handle:
        stat = os.fstat(handle.fileno())
        identity = stat.st_dev, stat.st_ino
        _validate_boundary(handle, through, stat.st_size)
        with _POSITION_CACHE_LOCK:
            cached = _POSITION_CACHE.get(identity)
        digest, start = hashlib.sha256(), 0
        entries, errors, number, size, light_bytes = [], [], 0, 0, 0
        if cached is not None and cached.through <= through:
            digest = _prefix_hasher(handle, cached.through)
            if (digest.digest() == cached.digest
                    and _dedupe_tail_anchor(handle, cached.through) == cached.anchor):
                start, entries, errors = cached.through, list(cached.entries), list(cached.errors)
                number, size = cached.lines, cached.size_bytes
                light_bytes = cached.light_bytes
            else:
                digest = hashlib.sha256()
        handle.seek(start)
        while handle.tell() < through:
            raise_if_compact_interrupted(interrupt_check)
            offset = handle.tell()
            raw = handle.readline(through - offset)
            if not raw.endswith(b"\n"):
                raise DataCorruptionError("canonical transcript truncated during source scan")
            digest.update(raw)
            number += 1
            position, row_size = _position_line(path, raw, number, offset, errors)
            if position is not None:
                position, row_size, light_bytes = _bounded_position(position, row_size, light_bytes)
                entries.append(position)
                size += row_size
        by_id = {}
        for item in entries:
            by_id.setdefault(item.message_id, item)
        result = _PositionIndex(identity, through, digest.digest(), _dedupe_tail_anchor(handle, through),
                                tuple(entries), by_id, tuple(errors), number, size, light_bytes)
    with _POSITION_CACHE_LOCK:
        _POSITION_CACHE.pop(identity, None)
        if len(entries) <= _POSITION_CACHE_MAX_ENTRIES and len(errors) <= 128 and size <= _POSITION_CACHE_MAX_BYTES:
            _POSITION_CACHE[identity] = result
            while (len(_POSITION_CACHE) > _POSITION_CACHE_MAX_FILES or sum(item.size_bytes for item in _POSITION_CACHE.values())
                   > _POSITION_CACHE_MAX_BYTES):
                _POSITION_CACHE.popitem(last=False)
    return result


# LLM: 无interrupt时复用index刚核验的digest，不在factory前额外hash；所有callback后及final仍完整核原范围。
# 函数用途: 用增量索引筛选非display地址，保留损坏、范围和取消合同，避免两遍全量JSON解析。
def select_message_snapshot(
    messages, thread_id: str, *, selector_factory: MessageSelectorFactory | None = None,
    exclude_message_ids: frozenset[str] = frozenset(),
    exclude_through_message_ids: tuple[str, ...] = (), retain_limit: int = 0, interrupt_check=None,
) -> MessageSnapshotRows:
    path = messages.storage.message_path(thread_id)
    identity = _file_identity(path)
    through, errors = messages.complete_offset_report(thread_id)
    if errors:
        raise OSError("conversation transcript snapshot could not be read reliably")
    if identity != _file_identity(path):
        raise DataCorruptionError("canonical message source changed during snapshot boundary read")
    if identity is None:
        return _empty_message_snapshot(path, thread_id, selector_factory, exclude_through_message_ids, interrupt_check)
    before_callbacks = None
    if interrupt_check is not None:
        with path.open("rb") as handle:
            before_callbacks = _prefix_hasher(handle, through).digest()
    try:
        index = message_position_index(path, through, interrupt_check)
    except DataCorruptionError:
        if before_callbacks is not None:
            _verify_selection_source(path, identity, through, before_callbacks)
        raise
    if before_callbacks is not None and before_callbacks != index.digest:
        raise DataCorruptionError("canonical message source changed during selection")
    if interrupt_check is not None:
        _verify_selection_source(path, identity, through, index.digest)
    positions = _validated_positions(index, thread_id)
    if any(end not in positions for end in exclude_through_message_ids):
        raise OSError("legacy compact source boundary is missing from canonical history")
    covered_end = max((positions[end] for end in exclude_through_message_ids), default=-1)
    predicate = selector_factory(MappingProxyType(positions)) if selector_factory is not None else None
    if selector_factory is not None:
        _verify_selection_source(path, identity, through, index.digest)
    if predicate is not None and not callable(predicate):
        raise TypeError("canonical message selector must be callable")
    selected: deque[MessageRowAddress] = deque(maxlen=retain_limit if retain_limit > 0 else None)

    with path.open("rb", buffering=0) as handle:
        for item in index.entries:
            raise_if_compact_interrupted(interrupt_check)
            if item.is_display:
                continue
            allowed = predicate(_SelectionRow(handle, item)) if predicate is not None else True
            if type(allowed) is not bool:
                raise TypeError("canonical message selector must return bool")
            if allowed and positions[item.message_id] > covered_end and item.message_id not in exclude_message_ids:
                selected.append(item.address)
        raise_if_compact_interrupted(interrupt_check)
        if (identity != _file_identity(path) or index.identity != identity
                or _prefix_hasher(handle, through).digest() != index.digest):
            raise DataCorruptionError("canonical message source changed during selection")
    return MessageSnapshotRows(path, thread_id, through, identity, tuple(selected), interrupt_check)


# LLM: 不存在的canonical仍执行原factory/取消和legacy锚点合同；回调创建或替换来源不能重绑定原空快照。
# 函数用途: 选择空历史时保留回调与拒绝语义，不创建文件或缓存。
def _empty_message_snapshot(path, thread_id, selector_factory, exclude_ends, interrupt_check):
    raise_if_compact_interrupted(interrupt_check)
    if exclude_ends:
        raise OSError("legacy compact source boundary is missing from canonical history")
    predicate = selector_factory(MappingProxyType({})) if selector_factory is not None else None
    if _file_identity(path) is not None:
        raise DataCorruptionError("canonical message source changed during selection")
    if predicate is not None and not callable(predicate):
        raise TypeError("canonical message selector must be callable")
    return MessageSnapshotRows(path, thread_id, 0, None, (), interrupt_check)


# LLM: 回调可能截断/替换/删除原路径，失败统一报告选择期间来源变化；合法尾追加只复核冻结前缀。
# 函数用途: 在扫描提前失败时仍按原identity/hash识别回调造成的损坏。
def _verify_selection_source(path, identity, through, digest):
    try:
        with path.open("rb") as handle:
            changed = identity != _file_identity(path) or _prefix_hasher(handle, through).digest() != digest
    except OSError as exc:
        raise DataCorruptionError("canonical message source changed during selection") from exc
    if changed:
        raise DataCorruptionError("canonical message source changed during selection")


# LLM: 所有物理行都先验证，display也不能藏跨thread行；不把错误、空ID或重复身份当过滤结果。
# 函数用途: 构造完整非展示身份表，供范围锚点及legacy覆盖选择。
def _validated_positions(index: _PositionIndex, thread_id: str) -> dict[str, int]:
    if index.errors:
        raise DataCorruptionError("invalid canonical message row")
    positions = {}
    for item in index.entries:
        if item.thread_id != thread_id:
            raise DataCorruptionError("invalid canonical message row")
        if item.is_display:
            continue
        if not item.message_id or item.message_id in positions:
            raise DataCorruptionError("canonical message identities are missing or repeated")
        positions[item.message_id] = len(positions)
    return positions


# LLM: 只观察文件身份，不拥有偏移/数据解析；读取EOF前后比较它，阻止新文件借用旧边界。
# 函数用途: 保存canonical文件当前设备及inode，缺文件明确表示为空来源。
def _file_identity(path):
    try:
        stat = path.stat()
        return stat.st_dev, stat.st_ino
    except FileNotFoundError:
        return None
