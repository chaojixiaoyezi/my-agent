# LLM: C仅存coordinator_io策略；原锁/reload/身份/编码门外，准确FD出生及固定快照raw末写逐次消费原scope。
# LLM: 向量身份和正式记录删除分别守原合同；损坏库不能被当作空库报告删除成功，保留唯一原子存储。
# 模块用途: 保存并检索正式向量，为精确删除提供可确认的清理结果。
"""单文件向量投影，保留跨进程锁、快照代次和崩溃残尾处理。

v3 先写 UTF-8 JSON 头和逐行 ID/正文/元数据表，再写连续的小端 float32 向量块与同序范数块。
追加记录仍位于同一文件末尾。普通批量删除用墓碑，隐私清理请求立即压实对应原始数据。
v1/v2 只读时不迁移，下一次压实才换成 v3。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import sys
import threading
import uuid
from array import array
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from heapq import heappush, heapreplace
from io import DEFAULT_BUFFER_SIZE
from itertools import islice
from pathlib import Path

from agent_py_agent.agent.common.json_io import (
    _replace_with_retry,
    _sync_parent_directory,
    _unlink_tmp_file,
    ensure_private_dir,
    locked_json_path,
)

from ..common.global_write_commit import execute_global_write

# 快照文件 schema 版本；不是这个版本的 JSON 对象按 P14 之前的旧格式（整份就是 id→条目、没有身份）读入。
VECTOR_STORE_SCHEMA_VERSION = "my-agent.memory-vectors.v2"
# 快照之后追加记录的 schema 版本；每条记录带所属快照的 generation，换代后的旧记录不会被套到新快照上。
VECTOR_STORE_APPEND_SCHEMA_VERSION = "my-agent.memory-vectors.v2.append"
# 快照小于这个字节数时整份重写本来就便宜，继续保持单个 JSON 文档（旧版本照常读）；达到后新增 id 才走追加。
_VECTOR_APPEND_MIN_BASE_BYTES = 1024 * 1024
# 追加段累计将超过快照字节数的这个百分比时整份压实一次，限定冷启动多读的字节和压实摊销。
_VECTOR_APPEND_MAX_TAIL_PERCENT = 25


# LLM: 向量库的结构化失败统一带 reason 码（登记在 error_taxonomy），调用方按码记健康状态，不解析异常文本。
# 类用途: 向量库失败的基类，携带原因码，例如重建未完成 VECTOR_REBUILD_INCOMPLETE。
class VectorStoreError(Exception):
    # LLM: reason 是机器判断的唯一依据，消息文本只给人看。
    # 函数用途: 记下原因码。
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# LLM: 向量空间不可信不是普通异常：调用方要按 reason 降级（语义召回退回关键词），不能吞成"没结果"。
# 类用途: 携带空间失效原因码：VECTOR_META_MISSING / VECTOR_IDENTITY_MISMATCH / VECTOR_DIMENSION_MISMATCH /
#   VECTOR_STORE_UNREADABLE。
class VectorIdentityError(VectorStoreError):
    pass


def _validated_vector(vector: object) -> array | None:
    if not isinstance(vector, (list, tuple)):
        return None
    try:
        values = array("f", (float(value) for value in vector))
        return values if values and all(map(math.isfinite, values)) else None
    except (TypeError, ValueError, OverflowError):
        return None


@dataclass(slots=True)
class _Row:
    offset: int
    length: int
    text: object
    metadata: object
    invalid: object = None


class _Packed:
    def __init__(self):
        self.values = array("f")
        self.rows: dict[str, _Row] = {}
        self.tombstones: set[str] = set()
        self.readers = 0

    def put(self, key, item):
        vector = item.get("vector") if isinstance(item, dict) else None
        values = vector if isinstance(vector, array) else _validated_vector(vector)
        start = len(self.values)
        if values is None:
            self.rows[key] = _Row(start, 0, "", None, item)
        else:
            self.values.extend(values)
            self.rows[key] = _Row(start, len(values), item.get("text", ""), item.get("metadata"))
        self.tombstones.discard(key)

    def copy_row(self, key, row, source):
        start = len(self.values)
        self.values.extend(source[row.offset:row.offset + row.length])
        self.rows[key] = _Row(start, row.length, row.text, row.metadata, row.invalid)

    def apply(self, added, removed):
        for key in removed:
            self.rows.pop(key, None)
        self.tombstones.update(removed)
        for key, item in added.items():
            self.put(key, item)


@dataclass(slots=True)
class _Rank:
    score: float
    id: str

    def __lt__(self, other):
        return self.score < other.score or (self.score == other.score and self.id > other.id)


@dataclass(frozen=True)
class VectorHit:
    id: str
    score: float
    text: str
    metadata: dict[str, object]


# LLM: 指纹 = (inode, 大小, 纳秒 mtime)；原子替换每次都换新文件，读者靠它发现别的实例或进程已经换代。
# 函数用途: 从 stat 结果取快照文件指纹。
def _file_stamp(st: os.stat_result) -> tuple[int, int, int]:
    return st.st_ino, st.st_size, st.st_mtime_ns


# LLM: 身份按 {str: str} 整体相等比较；值统一转字符串，避免 JSON 往返改类型造成假失配。空值表示不管理身份。
# 函数用途: 规范调用方或快照头里的身份字典，空值返回 None。
def _normalized_identity(identity: object) -> dict[str, str] | None:
    if not isinstance(identity, dict) or not identity:
        return None
    return {str(key): str(value) for key, value in identity.items()}


# LLM: 只认 v2 快照头；其它合法 JSON 对象按旧格式读入（没有头 = 没有身份）。坏 JSON、非对象、items 不是字典都算
#   读不了，不猜内容。返回 (快照头或 None, 条目, 是否可读)。接受 bytes 或 memoryview，切片解码不复制整段字节。
# 函数用途: 把快照文件字节解析成头、条目和可读标记。
def _parse_snapshot(raw: bytes | memoryview) -> tuple[dict[str, object] | None, dict[str, dict[str, object]], bool]:
    try:
        data = json.loads(str(raw, "utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, {}, False
    if not isinstance(data, dict):
        return None, {}, False
    if data.get("schema_version") != VECTOR_STORE_SCHEMA_VERSION:
        return None, data, True
    items = data.get("items")
    if not isinstance(items, dict):
        return None, {}, False
    header = {key: data.get(key) for key in ("schema_version", "identity", "dim", "generation", "written_at")}
    return header, items, True


_V3_PREFIX = b'{"v":3,'
_TOMBSTONE_SCHEMA = "my-agent.memory-vectors.v3.tombstone"


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _space_id(identity, dim):
    raw = json.dumps([identity, dim], sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def _read_float_block(handle, count):
    values = array("f", [0.0]) * count
    with memoryview(values).cast("B") as dest:
        if handle.readinto(dest) != len(dest):
            raise ValueError("incomplete vector block")
    if sys.byteorder != "little":
        values.byteswap()
    return values


def _float_bytes(values):
    if sys.byteorder != "little":
        values = values[:]
        values.byteswap()
    return values.tobytes()


def _read_v3(handle, size):
    header = json.loads(handle.readline(size + 1))
    count = header.get("count")
    dim = header.get("dim")
    generation = header.get("generation")
    if (type(count) is not int or not 0 <= count <= size or header.get("v") != 3
            or (dim is not None and (type(dim) is not int or dim < 1))
            or not isinstance(generation, str) or not generation
            or header.get("space_id") != _space_id(header.get("identity"), dim)):
        raise ValueError("invalid vector snapshot header")
    state = _Packed()
    offset = 0
    for _ in range(count):
        row = json.loads(handle.readline(max(0, size - handle.tell()) + 1))
        if not isinstance(row, list) or len(row) not in (4, 5):
            raise ValueError("invalid vector row")
        key, length, text, metadata = row[:4]
        if (not isinstance(key, str) or key in state.rows
                or type(length) is not int or length < 0 or (len(row) == 5 and length)):
            raise ValueError("invalid vector row extent")
        state.rows[key] = _Row(offset, length, text, metadata, row[4] if len(row) == 5 else None)
        offset += length
    if (offset + count) * state.values.itemsize > size - handle.tell():
        raise ValueError("vector snapshot extent exceeds file")
    state.values = _read_float_block(handle, offset)
    norms = _read_float_block(handle, count)
    if not all(map(math.isfinite, state.values)) or not all(value >= 0 for value in norms):
        raise ValueError("invalid packed vector values")
    end = handle.tell()
    tail = _tail_records(generation, handle.read(size - end), 0)
    if tail is None:
        raise ValueError("invalid vector tail")
    _apply_tail(state, tail[1])
    return header, state, (end, end + tail[0])


def _snapshot_bytes(header, state):
    # 调用方准备的是压实状态，行表和 float32 块按同一顺序写出。
    norms = array("f")
    entries = bytearray()
    for key, row in state.rows.items():
        line = [key, row.length, row.text, row.metadata]
        if not row.length:
            line.append(row.invalid)
        entries.extend(_json_bytes(line))
        entries.extend(b"\n")
        end = row.offset + row.length
        norms.append(math.sqrt(sum(state.values[i] ** 2 for i in range(row.offset, end))))
    return b"".join((_json_bytes(header), b"\n", entries, _float_bytes(state.values), _float_bytes(norms)))


# 同一句柄取得的代次是追加记录的范围，一条记录完整验收后才可改活跃行。
def _tail_records(generation, data, start):
    records = []
    consumed = position = start
    while position < len(data):
        if data[position] != ord("\n"):
            return None
        stop = data.find(b"\n", position + 1)
        last = stop < 0
        stop = len(data) if last else stop
        body = data[position + 1:stop]
        if body.strip():
            try:
                record = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                if last:
                    break
                return None
            if (not isinstance(record, dict) or not isinstance(generation, str)
                    or record.get("generation") != generation):
                return None
            schema = record.get("schema_version")
            if schema == VECTOR_STORE_APPEND_SCHEMA_VERSION and isinstance(record.get("items"), dict):
                records.append((record["items"], ()))
            elif schema == _TOMBSTONE_SCHEMA and isinstance(record.get("removed"), list) and all(
                isinstance(key, str) for key in record["removed"]
            ):
                records.append(({}, record["removed"]))
            else:
                return None
        consumed = position = stop
    return consumed, records


def _apply_tail(state, records):
    for added, removed in records:
        state.apply(added, removed)


def _parse_store(raw):
    # 旧格式照常解析，提交前只把合法向量放入本实例的连续块。
    newline = raw.find(b"\n")
    if newline >= 0:
        header, items, _ = _parse_snapshot(memoryview(raw)[:newline])
        if header is not None:
            parsed = _tail_records(header.get("generation"), raw, newline)
            if parsed is None:
                return None, _Packed(), False, None
            state = _Packed()
            state.apply(items, ())
            _apply_tail(state, parsed[1])
            return header, state, True, (newline, parsed[0])
    header, items, readable = _parse_snapshot(raw)
    tail = (len(raw), len(raw)) if header is not None and newline < 0 else None
    state = _Packed()
    state.apply(items, ())
    return header, state, readable, tail


def _write_private_snapshot_unlocked(path, payload, *, replace_once=False):
    """沿原私有临时文件和原子替换合同写字节，调用方持原路径锁。"""
    ensure_private_dir(path.parent)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.chmod(temporary, 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if replace_once:
            _replace_with_retry(temporary, path, keep_mode=False, replace_once=True)
        else:
            _replace_with_retry(temporary, path, keep_mode=False)
        _sync_parent_directory(path)
    finally:
        _unlink_tmp_file(temporary)


# LLM: 调用方持 self._lock 与 locked_json_path；不跟随符号链接，打开后先核对文件仍是刷新时那一代（指纹一致），
#   有崩溃残尾先截回已提交末尾，再写一条完整记录并 fsync。写入失败尽力截回原末尾再抛出，调用方不发布实例状态。
# 函数用途: 在已提交末尾追加一条记录，返回追加后的文件指纹。
def _append_record_unlocked(path: Path, expected: tuple[int, int, int] | None, end: int, payload: bytes) -> tuple[int, int, int]:
    descriptor = os.open(path, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    try:
        info = os.fstat(descriptor)
        current = _file_stamp(info)
        if expected is None or current != expected or current[1] < end or info.st_nlink != 1:
            raise OSError("vector snapshot changed before append")
        try:
            if current[1] > end:
                os.ftruncate(descriptor, end)
            os.lseek(descriptor, end, os.SEEK_SET)
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short write while appending vector record")
                view = view[written:]
            os.fsync(descriptor)
        except BaseException:
            try:
                os.ftruncate(descriptor, end)
            except OSError:
                pass
            raise
        return _file_stamp(os.fstat(descriptor))
    finally:
        os.close(descriptor)


# LLM: 维度以实际向量为准（P14 第 5 条）：同一批写入必须等长且非空，否则抛 VECTOR_DIMENSION_MISMATCH；空批返回 None。
# 函数用途: 校验一批 (id, 向量, 正文, 元数据) 的向量维度一致并返回该维度。
def _rows_dim(rows: list[tuple[str, list[float], str, dict[str, object]]]) -> int | None:
    lengths = {len(vector) for _id, vector, _text, _metadata in rows}
    if not lengths:
        return None
    if 0 in lengths or len(lengths) > 1:
        raise VectorIdentityError("VECTOR_DIMENSION_MISMATCH")
    return lengths.pop()


# LLM: 条目形状固定为 {vector, text, metadata}，读侧 search 与 jsonl 二次核验都按这三个键取值；改形状要同步两边。
# 插入前拒绝非有限值，向量按快照精度打包。
# 函数用途: 校验并复制一行向量数据，拒绝坏向量。
def _item(vector: list[float], text: str, metadata: dict[str, object] | None) -> dict[str, object]:
    validated = _validated_vector(vector)
    if validated is None:
        raise VectorIdentityError("VECTOR_DIMENSION_MISMATCH")
    return {"vector": validated, "text": text, "metadata": dict(metadata or {})}


class VectorStore:
    """绑定空间身份的派生向量投影，保留原读取、写入和检索接口。"""

    # LLM: 构造只读文件、不写盘；身份在实例生命周期内固定。
    # 函数用途: 绑定快照路径与当前空间身份，并加载一次现有快照。
    # LLM: coordinator_io默认False仅纯策略字段，原读取/身份不变，不在构造保存活来源。
    # 函数用途: 绑定原向量快照和IO策略，启动只读现有文件。
    def __init__(self, path: str | Path, identity: dict[str, object] | None = None, *, coordinator_io: bool = False) -> None:
        self.coordinator_io = coordinator_io
        self._path = Path(path).expanduser()
        self._lock = threading.RLock()  # 同实例的读写在进程内串行；跨实例、跨进程的写由 locked_json_path 串行
        self._identity = _normalized_identity(identity)
        self._header: dict[str, object] | None = None
        self._packed = _Packed()
        self._readable = True
        self._stamp: tuple[int, int, int] | None = None
        self._tail: tuple[int, int] | None = None  # (快照字节数, 已提交末尾)
        self._reload()

    # LLM: 默认原locked_json_path调用形状；C只交准确FD出生，锁等待与所有向量裁决仍在Root外。
    # 函数用途: 取得本快照原跨进程锁，不保存或复用请求来源。
    def _locked_snapshot(self):
        if self.coordinator_io:
            return locked_json_path(self._path, birth_commit=_coordinator_vector_write)
        return locked_json_path(self._path)

    @property
    def _items(self):
        return self._packed.rows

    def _editable_rows(self):
        # 活跃搜索固定行表，通过原实例锁分块读数组。追加只扩展数组末尾，旧行偏移保持不变。
        current = self._packed
        if current.readers:
            new = _Packed()
            new.values = current.values
            new.rows = current.rows.copy()
            new.tombstones = current.tombstones.copy()
            self._packed = new
        return self._packed

    def _reload(self) -> None:
        try:
            with self._path.open("rb") as handle:
                stamp = _file_stamp(os.fstat(handle.fileno()))
                if self._read_appended(handle, stamp):
                    return
                handle.seek(0)
                prefix = handle.read(len(_V3_PREFIX))
                handle.seek(0)
                if prefix == _V3_PREFIX:
                    header, state, tail = _read_v3(handle, stamp[1])
                    self._header, self._packed, self._readable, self._tail = header, state, True, tail
                else:
                    self._header, self._packed, self._readable, self._tail = _parse_store(handle.read(stamp[1]))
        except FileNotFoundError:
            self._header, self._packed, self._readable, self._stamp, self._tail = None, _Packed(), True, None, None
            return
        except (OSError, ValueError, OverflowError, TypeError):
            self._header, self._packed, self._readable, self._stamp, self._tail = None, _Packed(), False, None, None
            return
        self._stamp = stamp

    def _read_appended(self, handle, stamp: tuple[int, int, int]) -> bool:
        tail, known = self._tail, self._stamp
        if (tail is None or known is None or not self._readable or self._header is None
                or stamp[0] != known[0] or stamp[1] <= tail[1]):
            return False
        handle.seek(tail[1])
        parsed = _tail_records(self._header.get("generation"), handle.read(stamp[1] - tail[1]), 0)
        if parsed is None or not parsed[1]:
            return False
        _apply_tail(self._editable_rows(), parsed[1])
        self._tail, self._stamp = (tail[0], tail[1] + parsed[0]), stamp
        return True

    # LLM: 每次读写前先比文件指纹，别的实例或进程换代后立即重载；没变就用缓存（只花一次 stat）。调用方须持 self._lock。
    # 函数用途: 需要时重载快照，保证接下来看到的是最新一代。
    def _refresh(self) -> None:
        try:
            stamp: tuple[int, int, int] | None = _file_stamp(self._path.stat())
        except OSError:
            stamp = None
        if stamp != self._stamp or (not self._readable and self._stamp is None):
            self._reload()

    # LLM: 维度只认快照头里写下的整数（来自实际向量）；布尔值等非整数一律当没有，交给 META_MISSING 裁决。
    # 函数用途: 返回快照头记录的实际维度；没有头或值不是整数返回 None。
    def _stored_dim(self) -> int | None:
        dim = (self._header or {}).get("dim")
        return dim if isinstance(dim, int) and not isinstance(dim, bool) else None

    # LLM: 空间裁决只看结构化事实：读不了 → VECTOR_STORE_UNREADABLE；空库可被接管；有向量但头里没身份或没维度 →
    #   VECTOR_META_MISSING；身份不同 → VECTOR_IDENTITY_MISMATCH。不管理身份时恒可用。调用方须已刷新快照。
    # 函数用途: 返回当前快照对本实例身份的裁决原因码，空串表示可用。
    def _identity_reason(self) -> str:
        if self._identity is None:
            return ""
        if not self._readable:
            return "VECTOR_STORE_UNREADABLE"
        if not self._items:
            return ""
        stored = _normalized_identity((self._header or {}).get("identity"))
        if stored is None or self._stored_dim() is None:
            return "VECTOR_META_MISSING"
        return "" if stored == self._identity else "VECTOR_IDENTITY_MISMATCH"

    # LLM: 增量写入的裁决：快照身份可用、新向量与快照维度一致（空库按这批向量定维度）；不管理身份时不检查。
    # 函数用途: 校验本次写入能否进入当前快照，返回写入后快照的维度。
    def _writable_dim(self, rows: list[tuple[str, list[float], str, dict[str, object]]]) -> int | None:
        if self._identity is None:
            return None
        reason = self._identity_reason()
        if reason:
            raise VectorIdentityError(reason)
        dim = _rows_dim(rows)
        if self._items and dim != self._stored_dim():
            raise VectorIdentityError("VECTOR_DIMENSION_MISMATCH")
        return dim

    def _compact(self, added, removed):
        packed = _Packed()
        for key, row in self._items.items():
            if key in removed:
                continue
            if key in added:
                packed.put(key, added[key])
            else:
                packed.copy_row(key, row, self._packed.values)
        for key, item in added.items():
            if key not in self._items:
                packed.put(key, item)
        return packed

    def _write_snapshot(self, packed, identity, dim) -> None:
        if identity is None:
            dimensions = {row.length for row in packed.rows.values() if row.length}
            dim = dimensions.pop() if len(dimensions) == 1 else None
        elif not packed.rows:
            dim = None
        header = {
            "v": 3, "dim": dim, "count": len(packed.rows), "space_id": _space_id(identity, dim),
            "identity": identity, "generation": uuid.uuid4().hex,
            "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        raw = _snapshot_bytes(header, packed)
        if self.coordinator_io:
            _coordinator_vector_write(lambda: _write_private_snapshot_unlocked(self._path, raw, replace_once=True))
        else:
            _write_private_snapshot_unlocked(self._path, raw)
        self._header, self._packed, self._readable = header, packed, True
        self._stamp = _file_stamp(self._path.stat())
        self._tail = (self._stamp[1], self._stamp[1])

    # LLM: 只读汇总，与身份裁决用同一次刷新；读不了时条目数为 None（未知），不用 0 冒充。
    # 函数用途: 返回条目数和快照头，供状态预览使用。
    def summary(self) -> dict[str, object]:
        with self._lock:
            self._refresh()
            return {
                "vector_count": len(self._items) if self._readable else None,
                "meta": dict(self._header) if self._header else None,
            }

    # LLM: 对外的身份状态投影；读路径（语义召回前置检查、状态预览）使用，不写盘。
    # 函数用途: 返回 (是否可用, 原因码)；不管理身份时恒可用。
    def identity_status(self) -> tuple[bool, str]:
        with self._lock:
            self._refresh()
            reason = self._identity_reason()
        return not reason, reason

    # LLM: 先刷新再计数，别的实例或进程换代后数字立即跟上；读不了时为 0，需要区分"未知"的地方用 summary()。
    # 函数用途: 返回当前快照的条目数。
    def __len__(self) -> int:
        with self._lock:
            self._refresh()
            return len(self._items)

    # LLM: 单条写入与批量写入共用同一条裁决和落盘路径；空 id 直接拒绝。
    # 函数用途: 写入或覆盖一条向量。
    def upsert(self, id: str, vector: list[float], *, text: str = "", metadata: dict[str, object] | None = None) -> None:
        if not id:
            raise ValueError("vector id required")
        self.upsert_many([(id, vector, text, dict(metadata or {}))])

    # LLM: 增量写在跨进程锁内"刷新 → 裁决 → 写回"，裁决失败抛 VectorIdentityError 且不落盘。不管理身份的写入
    #   会把快照头身份置空（它无法为新向量担保），之后带身份的读者会得到 VECTOR_META_MISSING。空 id 的行跳过。
    # 函数用途: 批量写入或覆盖向量，只落盘一次。
    # LLM: C仅锁FD出生进原scope，等待/reload/身份/维度/合并仍Root外，原行过滤和一次写保持。
    # LLM: 全是新 id 且快照头不变时只追加一条记录（_append_payload 定条件）；其余情况仍整份写回并压实追加段。
    # 函数用途: 批量写原向量，保唯一快照与原跨实例一致性。
    def upsert_many(self, rows: list[tuple[str, list[float], str, dict[str, object]]]) -> None:
        """批量 upsert(只 flush 一次,省 IO)。rows: [(id, vector, text, metadata), ...]。"""
        rows = [row for row in rows if row[0]]
        if not rows:
            return
        with self._lock, self._locked_snapshot():
            self._refresh()
            dim = self._writable_dim(rows)
            added = {id: _item(vector, text, metadata) for id, vector, text, metadata in rows}
            payload = self._append_payload(added, dim)
            if payload is not None:
                self._append_items(added, (), payload)
            else:
                self._write_snapshot(self._compact(added, ()), self._identity, dim)

    def _append_payload(self, added, dim, *, removed=()) -> bytes | None:
        tail, header = self._tail, self._header or {}
        generation = header.get("generation")
        if (tail is None or not self._readable or not isinstance(generation, str) or not generation
                or tail[0] < _VECTOR_APPEND_MIN_BASE_BYTES):
            return None
        if added:
            if (_normalized_identity(header.get("identity")) != self._identity
                    or any(key in self._items or key in self._packed.tombstones for key in added)):
                return None
            stored_dim = self._stored_dim()
            if self._identity is not None and stored_dim != dim:
                return None
            if self._identity is None and stored_dim is not None and any(
                len(item["vector"]) != stored_dim for item in added.values()
            ):
                return None
        try:
            info = os.lstat(self._path)
        except OSError:
            return None
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or _file_stamp(info) != self._stamp
                or (os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077)):
            return None
        record = {"generation": generation}
        if removed:
            record.update(schema_version=_TOMBSTONE_SCHEMA, removed=sorted(removed))
        else:
            record.update(schema_version=VECTOR_STORE_APPEND_SCHEMA_VERSION, items={
                key: {**item, "vector": list(item["vector"])} for key, item in added.items()
            })
        payload = b"\n" + _json_bytes(record)
        if (tail[1] - tail[0]) + len(payload) > tail[0] * _VECTOR_APPEND_MAX_TAIL_PERCENT // 100:
            return None
        return payload

    def _append_items(self, added, removed, payload: bytes) -> None:
        base_size, end = self._tail
        expected = self._stamp

        def append():
            return _append_record_unlocked(self._path, expected, end, payload)

        stamp = _coordinator_vector_write(append) if self.coordinator_io else append()
        self._editable_rows().apply(added, removed)
        self._tail, self._stamp = (base_size, end + len(payload)), stamp

    # LLM: 重建专用：用完整新快照整体替换旧库，身份取本实例、维度取这批向量，不看旧快照——这正是管理员修复失配的
    #   入口。与增量写共用同一把跨进程锁；维度不一致抛 VECTOR_DIMENSION_MISMATCH，旧库原样保留。
    # 函数用途: 原子替换整个向量库，返回写入条数。
    # LLM: C只接准确FD出生与原末writer，完整维度/内容准备Root外，原重建身份算法不变。
    # 函数用途: 替换原向量快照，不另造恢复库或跨请求来源。
    def replace_all(self, rows: list[tuple[str, list[float], str, dict[str, object]]]) -> int:
        rows = [row for row in rows if row[0]]
        dim = _rows_dim(rows) if self._identity is not None else None
        packed = _Packed()
        for key, vector, text, metadata in rows:
            packed.put(key, _item(vector, text, metadata))
        # 兼容同批重复 ID，快照只序列化活跃行。
        if sum(row.length for row in packed.rows.values()) != len(packed.values):
            compacted = _Packed()
            for key, row in packed.rows.items():
                compacted.copy_row(key, row, packed.values)
            packed = compacted
        with self._lock, self._locked_snapshot():
            self._write_snapshot(packed, self._identity, dim)
        return len(packed.rows)

    def remove(self, id: str) -> bool:
        """单条删除保留立即落盘清理的原语义。"""
        return id in self.remove_many([id], purge=True)

    def remove_many(self, ids: Iterable[str], *, purge: bool = False) -> set[str]:
        """批量移除活跃行。隐私清理传 purge=True，立即压实原行及相关墓碑。"""
        wanted = set(ids)
        with self._lock, self._locked_snapshot():
            self._refresh()
            if not self._readable:
                raise VectorIdentityError("VECTOR_STORE_UNREADABLE")
            removed = {key for key in wanted if key in self._items}
            if not removed and (not purge or wanted.isdisjoint(self._packed.tombstones)):
                return set()
            dim = self._stored_dim()
            payload = None if purge else self._append_payload({}, dim, removed=removed)
            if payload is not None:
                self._append_items({}, removed, payload)
            else:
                identity = _normalized_identity((self._header or {}).get("identity"))
                self._write_snapshot(self._compact({}, removed), identity, dim)
            return removed

    def search(self, query_vector: list[float], *, top_k: int = 5, min_score: float = 0.0) -> list[VectorHit]:
        """按原点积排序，只保留 top_k 个候选，以 ID 处理同分。"""
        qlen = len(query_vector)
        with self._lock:
            self._refresh()
            reason = self._identity_reason()
            if not reason and self._identity is not None and self._items and qlen != self._stored_dim():
                reason = "VECTOR_DIMENSION_MISMATCH"
            if reason:
                raise VectorIdentityError(reason)
            if not qlen or top_k <= 0:
                return []
            packed = self._packed
            packed.readers += 1
        try:
            heap: list[_Rank] = []
            rows = iter(packed.rows.items())
            axes = range(qlen)
            block_size = max(1, DEFAULT_BUFFER_SIZE // (packed.values.itemsize * qlen))
            while block := list(islice(rows, block_size)):
                with self._lock:
                    values = packed.values
                    for key, row in block:
                        if row.length != qlen:
                            continue
                        score = sum(query_vector[i] * values[row.offset + i] for i in axes)
                        if score < min_score:
                            continue
                        rank = _Rank(score, key)
                        if len(heap) < top_k:
                            heappush(heap, rank)
                        elif heap[0] < rank:
                            heapreplace(heap, rank)
            hits = []
            for rank in sorted(heap, key=lambda rank: (-rank.score, rank.id)):
                row = packed.rows[rank.id]
                metadata = dict(row.metadata) if isinstance(row.metadata, dict) else {}
                hits.append(VectorHit(rank.id, rank.score, str(row.text), metadata))
            return hits
        finally:
            with self._lock:
                packed.readers -= 1

    def stored_vectors(self, ids: Iterable[str]) -> dict[str, tuple[list[float], str]]:
        wanted = set(ids)
        with self._lock:
            self._refresh()
            if self._identity is None or self._identity_reason():
                return {}
            values = self._packed.values
            return {
                key: (list(values[row.offset:row.offset + row.length]), str(row.text))
                for key in wanted if (row := self._items.get(key)) is not None and row.length
            }

    # LLM: 先刷新再列 id；保留期与硬删除核对靠它确认向量是否真被清掉，只读。
    # 函数用途: 返回当前快照的全部向量 id。
    def ids(self) -> list[str]:
        with self._lock:
            self._refresh()
            return list(self._items.keys())


# LLM: 只消费当前global scope，缺源沿原GlobalWriteCommitUnavailable，不缓存来源或把向量算法放入末门。
# 函数用途: 为本地向量快照的准确出生和raw末写调用原同步写门。
def _coordinator_vector_write(operation):
    return execute_global_write(operation, require_scope=True)
