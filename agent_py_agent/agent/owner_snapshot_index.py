# LLM: 本地 stat 索引只用于避免重复摘要，内容损坏即丢弃；远端清单仍是快照权威。
# 模块用途: 记录保守的文件签名，并从独立暂存副本计算上传摘要。
from __future__ import annotations

import ctypes
import hashlib
import io
import json
import os
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from agent_py_agent.agent.common.json_io import write_private_json_file_atomic_unlocked
from agent_py_agent.agent.common.nofollow_fs import _snapshot_stat_fields_match
from agent_py_agent.agent.owner_snapshot_files import SnapshotParent, file_signature

# 文件时间戳粒度保护秒数：mtime 距记录时刻小于该值的条目视为可能未落定，下次必须重新计算摘要。
OWNER_SNAPSHOT_RACY_MTIME_SECONDS = 2.0


# LLM: 签名包含设备、inode、完整 mode 和 ctime；近期 mtime 的条目不提供摘要命中。
# 类用途: 保存单个相对路径的文件签名和摘要。
@dataclass(frozen=True)
class StatEntry:
    relative_path: str
    dev: int
    ino: int
    size: int
    mtime_ns: int
    ctime_ns: int
    mode: int
    sha256: str
    racy: bool


# LLM: 调用方持有 owner slot 和租约；索引不授予提交权，丢失索引只增加校验成本。
# 类用途: 读写温缓存文件签名并保守判断可复用摘要。
class OwnerStatIndex:
    # LLM: 空索引没有持久副作用，由 checkout 的清单校对填充。
    # 函数用途: 建立空的文件签名表。
    def __init__(self) -> None:
        self._entries: dict[str, StatEntry] = {}

    # LLM: 内容或读取失败一律返回空索引，不接受部分损坏的缓存作为事实。
    # 函数用途: 从已知版本的 JSON 恢复文件签名。
    @classmethod
    def load(cls, path: Path) -> OwnerStatIndex:
        index = cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
                return index
            entries = data.get("entries")
            if not isinstance(entries, dict):
                return index
            for relative, values in entries.items():
                if (not isinstance(relative, str) or not relative or not isinstance(values, list)
                        or len(values) != 8 or any(type(value) is not int for value in values[:6])
                        or not isinstance(values[6], str) or type(values[7]) is not bool):
                    return cls()
                index._entries[relative] = StatEntry(relative, *values)
        except (OSError, ValueError, UnicodeError, RecursionError):
            return cls()
        return index

    # LLM: 私有原子 writer 同步文件及父目录；调用方必须串行访问同一个温缓存。
    # 函数用途: 耐久保存版本化文件签名表。
    def save(self, path: Path) -> None:
        entries = {
            rel: [e.dev, e.ino, e.size, e.mtime_ns, e.ctime_ns, e.mode, e.sha256, e.racy]
            for rel, e in self._entries.items()
        }
        write_private_json_file_atomic_unlocked(path, {"version": 1, "entries": entries})

    # LLM: 只有六项签名完全一致且非 racy 才返回摘要，不以缓存版本推断文件内容。
    # 函数用途: 查询给定文件状态能否复用已存摘要。
    def lookup(self, relative_path: str, st: os.stat_result) -> str | None:
        entry = self._entries.get(relative_path)
        if entry is None or entry.racy:
            return None
        actual = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_mode)
        expected = (entry.dev, entry.ino, entry.size, entry.mtime_ns, entry.ctime_ns, entry.mode)
        return entry.sha256 if actual == expected else None

    # LLM: written_at 仅用于本地时间戳粒度防护，不参与数据库租约判定。
    # 函数用途: 为校验过的文件记录签名和近期写入标志。
    def record(self, relative_path: str, st: os.stat_result, sha256: str, written_at: float) -> None:
        racy = abs(st.st_mtime_ns / 1e9 - written_at) < OWNER_SNAPSHOT_RACY_MTIME_SECONDS
        self._entries[relative_path] = StatEntry(
            relative_path, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns,
            st.st_mode, sha256, racy,
        )

    # LLM: 删除索引项不删除文件，也不修改远端清单。
    # 函数用途: 忘记已不在缓存中的相对路径。
    def forget(self, relative_path: str) -> None:
        self._entries.pop(relative_path, None)

    # LLM: 返回独立路径集合，允许调用方在迭代时删除过期项。
    # 函数用途: 列出本地签名表中的路径。
    def paths(self) -> set[str]:
        return set(self._entries)


# LLM: 上传者只能使用返回的暂存副本；原文件在复制期间变化则删除副本并拒绝快照。
# 函数用途: 复制文件到唯一暂存路径并计算该副本的摘要与大小。
def stage_and_hash(source: Path, staging_dir: Path, *, owner_root: Path | None = None,
                   expected_stat=None, expected_root=None) -> tuple[OwnerStagedFile, str, int]:
    """从明确owner根的原FD暂存；二参数独立调用只以其源目录为边界，不推导其它owner。"""
    source = Path(os.path.abspath(source))
    root = Path(os.path.abspath(owner_root)) if owner_root is not None else source.parent
    relative = source.relative_to(root)
    before = expected_stat if expected_stat is not None else source.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise RuntimeError(f"owner 快照来源不是常规文件: {source}")
    staged = None
    try:
        with SnapshotParent(root, relative.parts[:-1], expected_root=expected_root) as parent:
            descriptor = parent.open_read(relative.name)
            with os.fdopen(descriptor, 'rb') as handle:
                if not _snapshot_stat_fields_match(before, os.fstat(handle.fileno()), cross_windows=os.name == 'nt'):
                    raise RuntimeError(f"owner 文件在快照时仍被写入: {source}")
                signature = file_signature(handle.fileno())
                staged, digest, size = stage_open_file(handle.fileno(), staging_dir)
                parent.verify()
                current = parent.open_read(relative.name)
                try:
                    if file_signature(current) != signature:
                        raise RuntimeError(f"owner 文件在快照时仍被写入: {source}")
                finally:
                    os.close(current)
        return staged, digest, size
    except BaseException as error:
        if staged is not None:
            try:
                staged.unlink(missing_ok=True)
            except BaseException as cleanup:
                error.add_note(f'owner stage cleanup failed: {type(cleanup).__name__}')
        raise


class _StageReader(io.RawIOBase):
    """每个读取者持原stage对象的独立游标；不重新打开名字，也不共享dup的文件位置。"""

    def __init__(self, descriptor, size):
        super().__init__()
        self._descriptor = -1
        self._position_lock = threading.RLock()
        self._size, self._position = size, 0
        self._descriptor = _stage_reader_descriptor(descriptor)

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        with self._position_lock:
            return self._position

    def seek(self, offset, whence=0):
        with self._position_lock:
            if whence not in (0, 1, 2):
                raise ValueError('invalid stage seek origin')
            position = offset + (0 if whence == 0 else self._position if whence == 1 else self._size)
            if position < 0:
                raise ValueError('negative stage seek')
            self._position = position
            return position

    def readinto(self, buffer):
        with self._position_lock:
            if self.closed:
                raise ValueError('stage reader is closed')
            count = min(len(buffer), max(0, self._size - self._position))
            if not count:
                return 0
            if os.name == 'nt':
                os.lseek(self._descriptor, self._position, os.SEEK_SET)
                content = os.read(self._descriptor, count)
            else:
                content = os.pread(self._descriptor, count, self._position)
            buffer[:len(content)] = content
            self._position += len(content)
            return len(content)

    def close(self):
        with self._position_lock:
            if not self.closed:
                if self._descriptor >= 0:
                    os.close(self._descriptor)
                    self._descriptor = -1
                super().close()


def _stage_reader_descriptor(descriptor):
    if os.name != 'nt':
        return os.dup(descriptor)
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.ReOpenFile.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD)
    kernel.ReOpenFile.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    # 新原生handle对应同一文件对象，拥有独立游标；不解析已被替换的stage文件名。
    handle = kernel.ReOpenFile(msvcrt.get_osfhandle(descriptor), 0x80000000, 0x7, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        result = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY | getattr(os, 'O_NOINHERIT', 0))
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    try:
        os.set_inheritable(result, False)
    except BaseException:
        os.close(result)
        raise
    return result


class OwnerStagedFile:
    """只持宿主原stage FD的有界租约；path仅诊断，关闭不删除任何后来的同名对象。"""

    def __init__(self, path, descriptor, size):
        self.path, self._descriptor, self._size = Path(path), descriptor, size
        self._lease_lock = threading.RLock()

    @property
    def name(self):
        return self.path.name

    @property
    def parent(self):
        return self.path.parent

    def __fspath__(self):
        return str(self.path)

    def __str__(self):
        return str(self.path)

    def stat(self):
        with self._lease_lock:
            return os.fstat(self._descriptor)

    def open_reader(self):
        with self._lease_lock:
            if self._descriptor < 0:
                raise ValueError('owner stage lease is closed')
            reader = _StageReader(self._descriptor, self._size)
            try:
                return io.BufferedReader(reader)
            except BaseException:
                reader.close()
                raise

    def read_bytes(self):
        with self.open_reader() as reader:
            return reader.read()

    def unlink(self, missing_ok=False):
        # POSIX已在创建时匿名；Windows DELETE_ON_CLOSE跟随原对象，不能unlink当前名字。
        with self._lease_lock:
            if self._descriptor >= 0:
                os.close(self._descriptor)
                self._descriptor = -1

    def __del__(self):
        descriptor = getattr(self, '_descriptor', -1)
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
            self._descriptor = -1


def _create_stage_descriptor(parent, name):
    if parent.fd is not None:
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
        descriptor = os.open(name, flags, 0o600, dir_fd=parent.fd)
        try:
            parent.remove(name, os.fstat(descriptor))
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor
    import msvcrt
    # 原父句柄约束内CREATE_NEW；只允许其它reader/rename，不允许外部writer改写stage字节。
    handle = parent.kernel.CreateFileW(str(parent.path / name), 0xC0010000, 0x5, None, 1,
        0x04000000 | 0x00200000 | 0x100, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_BINARY | getattr(os, 'O_NOINHERIT', 0))
    except BaseException:
        parent.kernel.CloseHandle(handle)
        raise
    try:
        os.set_inheritable(descriptor, False)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def descriptor_digest(descriptor: int, expected_size: int) -> tuple[str, int]:
    """在原FD上核实际字节，最多原size+1/64KiB缓冲；metadata同tick不等于内容未变。"""
    digest, size = hashlib.sha256(), 0
    position = os.lseek(descriptor, 0, os.SEEK_CUR) if os.name == 'nt' else None
    primary = None
    try:
        if position is not None:
            os.lseek(descriptor, 0, os.SEEK_SET)
        while size <= expected_size:
            count = min(65536, expected_size - size + 1)
            content = (os.read(descriptor, count) if os.name == 'nt'
                       else os.pread(descriptor, count, size))
            if not content:
                break
            digest.update(content)
            size += len(content)
        return digest.hexdigest(), size
    except BaseException as error:
        primary = error
        raise
    finally:
        if position is not None:
            try:
                os.lseek(descriptor, position, os.SEEK_SET)
            except BaseException as error:
                if primary is None:
                    raise
                primary.add_note(f'owner source cursor restore failed: {type(error).__name__}')


def stage_open_file(descriptor: int, staging_dir: Path) -> tuple[OwnerStagedFile, str, int]:
    """唯一FD复制点：内存64KiB、原size+1上界；返回原匿名FD租约，不复制第二份。"""
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
        raise RuntimeError('OWNER_SNAPSHOT_SOURCE_INVALID: owner源FD必须为普通单链接文件；上传尚未开始')
    signature = file_signature(descriptor)
    staged = Path(staging_dir) / f"{uuid4().hex}.stage"
    retained = None
    with SnapshotParent(staging_dir, create=True) as parent:
        output = _create_stage_descriptor(parent, staged.name)
        try:
            digest, size = hashlib.sha256(), 0
            with os.fdopen(output, 'w+b', closefd=False) as target:
                while size < opened.st_size:
                    chunk = os.read(descriptor, min(65536, opened.st_size - size))
                    if not chunk:
                        break
                    target.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                if (size != opened.st_size or os.read(descriptor, 1)
                        or file_signature(descriptor) != signature):
                    raise RuntimeError('OWNER_SNAPSHOT_SOURCE_CHANGED: owner源FD在复制时发生变化；上传尚未开始')
                verified_digest, verified_size = descriptor_digest(descriptor, opened.st_size)
                if (verified_digest != digest.hexdigest() or verified_size != size
                        or file_signature(descriptor) != signature):
                    raise RuntimeError('OWNER_SNAPSHOT_SOURCE_CHANGED: owner源实际字节在复制后改变；上传尚未开始')
                target.flush()
                target.seek(0)
                retained = os.dup(target.fileno())
            parent.verify()
            result = OwnerStagedFile(staged, retained, size)
            retained = None
            return result, digest.hexdigest(), size
        finally:
            os.close(output)
            if retained is not None:
                os.close(retained)
