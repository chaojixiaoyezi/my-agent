# LLM: 留存游标是唯一发表锚点；本模块只保存可丢弃的标记日志，读取不全、缺失或摘要不符一律当作没有进度。
# 模块用途: 为候选不可变节点回收保存可跨淘汰与重启续作的标记集合，并回收不再被游标引用的旧日志。
from __future__ import annotations

"""候选不可变节点回收检查点。

每个回收周期只追加一个私有日志，按标记顺序存放节点 SHA-256 摘要原始字节。
游标记录已提交条数与日志前缀摘要，重新加载时逐字节核对；更长的尾部来自尚未
发表的尝试，写入前截断。日志从不授予删除许可：缺失、截短或摘要不符的日志只会
让调用方从当前头重新标记，因此不会得到比真实可达集合更小的集合。
"""

import hashlib
import os
import stat
from pathlib import Path

from ..common import nofollow_fs

_WORKSPACE = ("retention", "candidate_nodes")
_SUFFIX = ".marks"


def workspace(home) -> Path:
    return Path(home.owner_data_dir).joinpath(*_WORKSPACE)


def valid_epoch(value) -> bool:
    return type(value) is str and len(value) == 32 and all(ch in "0123456789abcdef" for ch in value)


def _root(home, *, create: bool) -> Path:
    if create:
        nofollow_fs.ensure_private_dir(workspace(home))
    return nofollow_fs.resolve_existing_symlink_anchor(Path(home.owner_data_dir))


def _open(root: Path, name: str, flags: int) -> int:
    """在工作目录内不跟随链接地打开一个单链接普通日志文件。"""
    create = bool(flags & os.O_CREAT)
    if nofollow_fs._supports_dir_fd():
        parent = nofollow_fs.open_directory_beneath(root, _WORKSPACE, create=create)
        try:
            return nofollow_fs._openat_file(parent, name, flags, 0o600)
        finally:
            os.close(parent)
    target = nofollow_fs._portable_path(root, (*_WORKSPACE, name), create=create)
    try:
        info = target.lstat()
    except FileNotFoundError:
        if not create:
            raise
    else:
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise nofollow_fs.NoFollowPathError("candidate mark checkpoint is not a regular file")
    return os.open(target, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)


def encode_marks(names) -> bytes:
    """节点名为 64 位十六进制摘要加 .json，日志只存摘要原始字节。"""
    return b"".join(bytes.fromhex(name[:-len(".json")]) for name in names)


def append_marks(home, epoch: str, marked: int, payload: bytes) -> bool:
    """把本片新标记写在已提交前缀之后，更长的旧尾部来自未发表尝试，随后截断。

    返回 False 表示已提交前缀已不存在（日志被移除或截短），调用方须放弃该周期。
    """
    if not valid_epoch(epoch) or type(marked) is not int or marked < 0:
        raise ValueError("invalid candidate mark checkpoint position")
    offset = marked * hashlib.sha256().digest_size
    descriptor = _open(_root(home, create=True), epoch + _SUFFIX, os.O_RDWR | os.O_CREAT)
    try:
        if os.fstat(descriptor).st_size < offset:
            return False
        os.lseek(descriptor, offset, os.SEEK_SET)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while saving candidate mark checkpoint")
            view = view[written:]
        os.ftruncate(descriptor, offset + len(payload))
    finally:
        os.close(descriptor)
    return True


def read_marks(home, epoch: str, offset: int, size: int) -> bytes | None:
    """读取已提交前缀中的一段；日志缺失返回 None，读到的字节可能短于请求。"""
    if not valid_epoch(epoch):
        return None
    try:
        descriptor = _open(_root(home, create=False), epoch + _SUFFIX, os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
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


def retire_marks(home, keep, budget) -> bool:
    """删除不再被已提交或待提交游标引用的日志；返回预算用尽时是否仍有待删项。"""
    try:
        root = _root(home, create=False)
        names = nofollow_fs.list_names_beneath(root, _WORKSPACE)
    except FileNotFoundError:
        return False
    for name in names:
        if not name.endswith(_SUFFIX) or name[:-len(_SUFFIX)] in keep:
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


class MarkLoad:
    """按字节预算分片读回一个已提交日志；摘要核对通过前不交出任何集合。"""

    __slots__ = ("epoch", "marked", "digest", "offset", "hasher", "names")

    def __init__(self, epoch: str, marked: int, digest: str):
        self.epoch, self.marked, self.digest = epoch, marked, digest
        self.offset = 0
        self.hasher = hashlib.sha256()
        self.names = set()

    def advance(self, home, budget) -> bool | None:
        """True 表示已完整核对；False 表示本片预算暂停；None 表示缺失、截短或摘要不符。"""
        width = self.hasher.digest_size
        total = self.marked * width
        while self.offset < total:
            room = budget.max_bytes - budget.size
            size = min(total - self.offset, room - room % width)
            if size <= 0 or budget.exhausted():
                return False
            raw = read_marks(home, self.epoch, self.offset, size)
            if raw is None or len(raw) != size:
                return None
            budget.charge(size=size)
            self.hasher.update(raw)
            self.names.update(raw[start:start + width].hex() + ".json" for start in range(0, size, width))
            self.offset += size
        return True if self.hasher.hexdigest() == self.digest else None
