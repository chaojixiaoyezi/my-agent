# LLM: 留存游标是唯一发表锚点；本模块只保存可丢弃的分片标记日志，读取不全、缺失或摘要不符一律当作没有进度。
# 模块用途: 为候选不可变节点回收保存可跨淘汰与重启按分片续作的标记集合，并回收不再被游标引用的旧日志。
from __future__ import annotations

"""候选不可变节点回收检查点。

每个回收周期按节点摘要首字节分成 256 个私有分片日志，分片内按标记顺序存放节点 SHA-256 摘要原始字节；
另有一个按内容寻址的分片索引记录每个分片的已提交条数与分片摘要。游标只记录周期、总条数与索引摘要。
读回时先核对索引，再只读回并逐字节核对当前需要的那个分片，淘汰或重启最多丢失已读回的分片而不是
整份日志。更长的分片尾部来自尚未发表的尝试，写入前截断。日志从不授予删除许可：缺失、截短或摘要
不符的索引或分片只会让调用方从当前头重新标记，因此不会得到比真实可达集合更小的集合。
"""

import hashlib
import os
import stat
import struct
from pathlib import Path

from ..common import nofollow_fs
from ..local_storage.owner_read_index import _ResumableSha256

_WORKSPACE = ("retention", "candidate_nodes")
_SUFFIX = ".marks"
_INDEX_SUFFIX = ".index"
_WIDTH = hashlib.sha256().digest_size
_RECORD = struct.Struct(">Q32s")
SHARDS = tuple(f"{value:02x}" for value in range(256))
EMPTY_INDEX = tuple((0, hashlib.sha256().digest()) for _ in SHARDS)


def workspace(home) -> Path:
    return Path(home.owner_data_dir).joinpath(*_WORKSPACE)


def valid_epoch(value) -> bool:
    return type(value) is str and len(value) == 32 and all(ch in "0123456789abcdef" for ch in value)


def valid_digest(value) -> bool:
    return type(value) is str and len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)


def index_bytes(index) -> bytes:
    return b"".join(_RECORD.pack(count, digest) for count, digest in index)


def index_digest(index) -> str:
    return hashlib.sha256(index_bytes(index)).hexdigest()


EMPTY_INDEX_DIGEST = index_digest(EMPTY_INDEX)
_INDEX_BYTES = _RECORD.size * len(SHARDS)


def _root(home, *, create: bool) -> Path:
    if create:
        nofollow_fs.ensure_private_dir(workspace(home))
    return nofollow_fs.resolve_existing_symlink_anchor(Path(home.owner_data_dir))


def _parent(root: Path, *, create: bool) -> int | None:
    return nofollow_fs.open_directory_beneath(root, _WORKSPACE, create=create) if nofollow_fs._supports_dir_fd() else None


def _open(root: Path, parent: int | None, name: str, flags: int) -> int:
    """在工作目录内不跟随链接地打开一个单链接普通日志文件。"""
    if parent is not None:
        return nofollow_fs._openat_file(parent, name, flags, 0o600)
    target = nofollow_fs._portable_path(root, (*_WORKSPACE, name), create=bool(flags & os.O_CREAT))
    try:
        info = target.lstat()
    except FileNotFoundError:
        if not flags & os.O_CREAT:
            raise
    else:
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise nofollow_fs.NoFollowPathError("candidate mark checkpoint is not a regular file")
    return os.open(target, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write while saving candidate mark checkpoint")
        view = view[written:]


def shard_name(epoch: str, shard: str) -> str:
    return f"{epoch}.{shard}{_SUFFIX}"


def index_name(epoch: str, digest: str) -> str:
    return f"{epoch}.{digest}{_INDEX_SUFFIX}"


def encode_marks(names) -> bytes:
    """节点名为 64 位十六进制摘要加 .json，日志只存摘要原始字节。"""
    return b"".join(bytes.fromhex(name[:-len(".json")]) for name in names)


def append_marks(home, epoch: str, appends) -> set[str]:
    """把本片新标记分别写在各分片已提交前缀之后，更长的旧尾部来自未发表尝试，随后截断。

    appends 为 {分片: (已提交条数, 新标记字节)}。返回已提交前缀已不存在（日志被移除或截短）的分片，
    非空时调用方须放弃该周期。
    """
    if not valid_epoch(epoch):
        raise ValueError("invalid candidate mark checkpoint epoch")
    root = _root(home, create=True)
    parent = _parent(root, create=True)
    missing = set()
    try:
        for shard, (marked, payload) in sorted(appends.items()):
            if shard not in SHARDS or type(marked) is not int or marked < 0 or len(payload) % _WIDTH:
                raise ValueError("invalid candidate mark checkpoint position")
            offset = marked * _WIDTH
            descriptor = _open(root, parent, shard_name(epoch, shard), os.O_RDWR | os.O_CREAT)
            try:
                if os.fstat(descriptor).st_size < offset:
                    missing.add(shard)
                    continue
                os.lseek(descriptor, offset, os.SEEK_SET)
                _write_all(descriptor, payload)
                os.ftruncate(descriptor, offset + len(payload))
            finally:
                os.close(descriptor)
    finally:
        if parent is not None:
            os.close(parent)
    return missing


def write_index(home, epoch: str, index) -> str:
    """按内容寻址写出分片索引并返回其摘要；同名文件内容必然相同，覆盖写可修复上次中断的半截文件。"""
    if not valid_epoch(epoch) or len(index) != len(SHARDS):
        raise ValueError("invalid candidate mark checkpoint index")
    raw = index_bytes(index)
    digest = hashlib.sha256(raw).hexdigest()
    if digest == EMPTY_INDEX_DIGEST:
        return digest
    root = _root(home, create=True)
    parent = _parent(root, create=True)
    try:
        descriptor = _open(root, parent, index_name(epoch, digest), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        try:
            _write_all(descriptor, raw)
        finally:
            os.close(descriptor)
    finally:
        if parent is not None:
            os.close(parent)
    return digest


def read_marks(home, name: str, size: int, *, offset: int = 0) -> bytes | None:
    """Read at most size bytes from a checkpoint, returning None if it is missing."""
    try:
        root = _root(home, create=False)
        parent = _parent(root, create=False)
    except FileNotFoundError:
        return None
    try:
        try:
            descriptor = _open(root, parent, name, os.O_RDONLY)
        except FileNotFoundError:
            return None
        try:
            if offset:
                os.lseek(descriptor, offset, os.SEEK_SET)
            chunks, remaining = [], size
            while remaining:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    finally:
        if parent is not None:
            os.close(parent)


def _mark_stamp(home, name):
    try:
        root = _root(home, create=False)
        parent = _parent(root, create=False)
        try:
            descriptor = _open(root, parent, name, os.O_RDONLY)
            try:
                info = os.fstat(descriptor)
            finally:
                os.close(descriptor)
        finally:
            if parent is not None:
                os.close(parent)
    except FileNotFoundError as exc:
        raise MarkCheckpointUnavailable(name) from exc
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _verify_mark_prefix(home, name, size, digest, budget, state, consume):
    """Verify one committed prefix, saving its exact byte position and digest between slices."""
    stamp = _mark_stamp(home, name)
    if stamp[2] < size:
        raise MarkCheckpointUnavailable(name)
    if state.get("stamp") != stamp:
        state.clear()
        state["stamp"] = stamp
    offset = state.get("offset", 0)
    if type(offset) is not int or not 0 <= offset <= size:
        raise MarkCheckpointUnavailable(name)
    if state.get("verified") == digest and offset == size:
        return True
    remaining = budget.max_bytes - budget.size
    if budget.exhausted() or (offset == 0 and remaining < size <= budget.max_bytes):
        return False
    streaming = offset > 0 or size > remaining
    try:
        hasher = (_ResumableSha256.from_state(state["sha256"], offset) if offset else
                  _ResumableSha256() if streaming else hashlib.sha256())
    except (KeyError, TypeError, ValueError) as exc:
        raise MarkCheckpointUnavailable(name) from exc
    while offset < size and not budget.exhausted():
        length = min(size - offset, budget.max_bytes - budget.size)
        if streaming:
            length = min(length, _INDEX_BYTES)
        raw = read_marks(home, name, length, offset=offset) if offset else read_marks(home, name, length)
        if raw is None or len(raw) != length:
            raise MarkCheckpointUnavailable(name)
        budget.charge(size=length)
        consume(raw, offset, state)
        hasher.update(raw)
        offset += length
        state["offset"] = offset
    if _mark_stamp(home, name) != stamp:
        raise MarkCheckpointUnavailable(name)
    if offset < size:
        if offset:
            state["sha256"] = hasher.to_state()
        return False
    if hasher.hexdigest() != digest:
        raise MarkCheckpointUnavailable(name)
    state.pop("sha256", None)
    state["verified"] = digest
    return True



def _checkpoint_key(name: str) -> str | None:
    """分片日志（及旧版整份日志）按周期保留；分片索引按"周期.摘要"保留；其它文件不属于本模块。"""
    parts = name.split(".")
    if len(parts) == 2 and name.endswith(_SUFFIX) and valid_epoch(parts[0]):
        return parts[0]
    if len(parts) == 3 and valid_epoch(parts[0]):
        if name.endswith(_SUFFIX) and parts[1] in SHARDS:
            return parts[0]
        if name.endswith(_INDEX_SUFFIX) and valid_digest(parts[1]):
            return parts[0] + "." + parts[1]
    return None


def retire_marks(home, keep, budget) -> bool:
    """删除不再被已提交或待提交游标引用的分片日志与索引；返回预算用尽时是否仍有待删项。"""
    try:
        root = _root(home, create=False)
        names = nofollow_fs.list_names_beneath(root, _WORKSPACE)
    except FileNotFoundError:
        return False
    for name in names:
        key = _checkpoint_key(name)
        if key is None or key in keep:
            continue
        if budget.exhausted():
            return True
        nofollow_fs.unlink_file_beneath(root, (*_WORKSPACE, name))
        budget.charge(entries=1)
    if not keep:
        try:
            os.rmdir(workspace(home))
        except OSError:
            pass  # 非空（含非本模块文件）或已不存在时保留目录，只回收本模块日志。
    return False


class MarkCheckpointUnavailable(Exception):
    """已提交索引或分片缺失、截短或摘要不符：调用方只能放弃该周期进度。"""


class ShardedMarks:
    """一个周期标记集合的分片视图：索引核对后按需读回单个分片，未读回的分片不参与任何删除授权。"""

    __slots__ = ("epoch", "digest", "index", "shards", "hashers")

    def __init__(self, epoch: str, digest: str = EMPTY_INDEX_DIGEST, index=None):
        self.epoch, self.digest = epoch, digest
        self.index = list(EMPTY_INDEX) if digest == EMPTY_INDEX_DIGEST else (None if index is None else list(index))
        self.shards: dict[str, set[str] | frozenset[str]] = {}
        self.hashers: dict = {}

    def __contains__(self, name) -> bool:
        # 未读回分片按"可达"保守处理：只会少删，不会多删。
        names = self.shards.get(name[:2])
        return True if names is None else name in names

    def ensure(self, home, shard: str, budget) -> bool:
        """Load a full shard and its hasher for mark appends. Sweep uses resumable membership."""
        if shard in self.shards:
            return True
        started = budget.size == 0
        if self.index is None:
            if not started and budget.max_bytes - budget.size < _INDEX_BYTES:
                return False
            raw = read_marks(home, index_name(self.epoch, self.digest), _INDEX_BYTES)
            if raw is None or len(raw) != _INDEX_BYTES or hashlib.sha256(raw).hexdigest() != self.digest:
                raise MarkCheckpointUnavailable(self.epoch)
            budget.charge(size=_INDEX_BYTES)
            self.index = [_RECORD.unpack_from(raw, offset) for offset in range(0, _INDEX_BYTES, _RECORD.size)]
        count, digest = self.index[int(shard, 16)]
        size = count * _WIDTH
        if size and not started and size > budget.max_bytes - budget.size:
            return False
        hasher = hashlib.sha256()
        names = set()
        if size:
            raw = read_marks(home, shard_name(self.epoch, shard), size)
            if raw is None or len(raw) != size:
                raise MarkCheckpointUnavailable(self.epoch)
            budget.charge(size=size)
            hasher.update(raw)
            prefix = int(shard, 16)
            if hasher.digest() != digest or any(raw[start] != prefix for start in range(0, size, _WIDTH)):
                raise MarkCheckpointUnavailable(self.epoch)
            names.update(raw[start:start + _WIDTH].hex() + ".json" for start in range(0, size, _WIDTH))
        elif digest != EMPTY_INDEX[0][1]:
            raise MarkCheckpointUnavailable(self.epoch)
        self.shards[shard], self.hashers[shard] = names, hasher
        return True

    def membership(self, home, shard, names, budget, state):
        """Return exact matches for this directory page, or None until both digests are verified."""
        if shard in self.shards:
            return names.intersection(self.shards[shard])
        if self.digest == EMPTY_INDEX_DIGEST:
            return set()
        epoch, digest = self.epoch, self.digest
        prefix = int(shard, 16)
        record_offset = prefix * _RECORD.size

        def collect_index(raw, offset, checkpoint):
            start, end = max(offset, record_offset), min(offset + len(raw), record_offset + _RECORD.size)
            if start < end:
                checkpoint["record"] = checkpoint.get("record", "") + raw[start - offset:end - offset].hex()

        index = state.setdefault("index", {})
        if not _verify_mark_prefix(home, index_name(epoch, digest), _INDEX_BYTES, digest, budget, index, collect_index):
            return None
        count, checksum = _RECORD.unpack(bytes.fromhex(index["record"]))
        if not count:
            if checksum != EMPTY_INDEX[0][1]:
                raise MarkCheckpointUnavailable(epoch)
            return set()
        wanted = {bytes.fromhex(name[:-len(".json")]): name for name in names}

        def collect_matches(raw, _offset, checkpoint):
            data = bytes.fromhex(checkpoint.get("tail", "")) + raw
            end = len(data) - len(data) % _WIDTH
            checkpoint["tail"] = data[end:].hex()
            found = set(checkpoint.get("found", ()))
            for start in range(0, end, _WIDTH):
                record = data[start:start + _WIDTH]
                if record[0] != prefix:
                    raise MarkCheckpointUnavailable(epoch)
                if record in wanted:
                    found.add(wanted[record])
            checkpoint["found"] = sorted(found)

        proof = state.setdefault("shard", {})
        if not _verify_mark_prefix(home, shard_name(epoch, shard), count * _WIDTH, checksum.hex(),
                                   budget, proof, collect_matches):
            return None
        return names.intersection(proof.get("found", ()))


    def frozen(self) -> ShardedMarks:
        """证明时刻的不可变副本：沿用已读回分片的集合，其余分片按同一已提交索引按需读回。"""
        copy = ShardedMarks(self.epoch, self.digest, self.index)
        copy.shards = {shard: frozenset(names) for shard, names in self.shards.items()}
        return copy
