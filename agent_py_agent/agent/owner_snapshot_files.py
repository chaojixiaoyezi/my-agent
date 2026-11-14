"""owner快照的目录锚点；下载/删除只作用于本次打开的原目录，不追随工作区换链。"""
from __future__ import annotations

import ctypes
import hashlib
import ntpath
import os
import stat
import sys
import tempfile
import uuid
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path, PurePosixPath, PureWindowsPath

from .common.nofollow_fs import (
    NoFollowPathError,
    _openat_directory,
    _openat_file,
    file_descriptor_change_ns,
    open_directory_beneath,
)
from .common.nofollow_tree import _remove_directory_at


def _identity(info):
    return info.st_dev, info.st_ino


def validate_component(name, *, windows=None):
    windows = os.name == 'nt' if windows is None else windows
    if not isinstance(name, str) or not name or name in ('.', '..') or '/' in name or '\x00' in name:
        raise NoFollowPathError('owner snapshot relative component is invalid')
    reserved = getattr(ntpath, 'isreserved', None)
    is_reserved = (reserved(name) if reserved is not None else PureWindowsPath(name).is_reserved()) if windows else False
    if windows and (any(ord(char) < 32 or char in '<>:"\\|?*' for char in name)
                    or name.endswith(('.', ' ')) or is_reserved):
        raise NoFollowPathError('owner snapshot Windows component is not an ordinary file name')
    return name


def validate_relative(value, *, windows=None):
    windows = os.name == 'nt' if windows is None else windows
    if not isinstance(value, str) or not value or '\x00' in value:
        raise NoFollowPathError('owner snapshot relative path is invalid')
    pure = PurePosixPath(value)
    if pure.is_absolute() or not pure.parts or any(part in ('', '.', '..') for part in pure.parts):
        raise NoFollowPathError('owner snapshot relative path escapes its owner')
    if windows and (PureWindowsPath(value).drive or PureWindowsPath(value).root):
        raise NoFollowPathError('owner snapshot Windows path has a drive or root')
    return tuple(validate_component(part, windows=windows) for part in pure.parts)


def file_signature(descriptor):
    info = os.fstat(descriptor)
    changed = file_descriptor_change_ns(descriptor, info)
    if changed is None:
        raise NoFollowPathError('owner snapshot file generation is unavailable')
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            changed, info.st_mode, info.st_nlink)


def _parts(path):
    path = Path(os.path.abspath(path))
    return Path(path.anchor), path.parts[1:]


def lexical_path(path):
    """不resolve用户可变层；macOS仅折叠root持有且目标精确匹配的系统目录别名。"""
    result = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    if sys.platform == 'darwin' and len(result.parts) > 1 and result.parts[1] in ('tmp', 'var', 'etc'):
        alias = Path('/') / result.parts[1]
        before = alias.lstat()
        if stat.S_ISLNK(before.st_mode) and before.st_uid == 0:
            target = Path(os.path.normpath(os.path.join('/', os.readlink(alias))))
            after = alias.lstat()
            if target == Path('/private') / result.parts[1] and _identity(before) == _identity(after):
                result = target.joinpath(*result.parts[2:])
    return result


@lru_cache(maxsize=1)
def _windows_api():
    from ctypes import wintypes

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    class Info(ctypes.Structure):
        _fields_ = [('attributes', wintypes.DWORD), ('created', wintypes.FILETIME),
            ('accessed', wintypes.FILETIME), ('written', wintypes.FILETIME),
            ('volume', wintypes.DWORD), ('size_high', wintypes.DWORD), ('size_low', wintypes.DWORD),
            ('links', wintypes.DWORD), ('index_high', wintypes.DWORD), ('index_low', wintypes.DWORD)]
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileInformationByHandle.argtypes = (wintypes.HANDLE, ctypes.POINTER(Info))
    kernel.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel, Info


class SnapshotParent:
    """POSIX用原nofollow dirfd；Windows用拒绝reparse/write/delete共享的原生目录句柄。"""

    def __init__(self, root, parts=(), *, create=False, expected_root=None):
        self.root, self.parts, self.create = lexical_path(root), tuple(parts), create
        self.expected_root = expected_root
        for part in self.parts:
            validate_component(part)
        self.path = self.root.joinpath(*self.parts)
        self.fd = self.root_fd = None
        self.handles = []

    def __enter__(self):
        try:
            if os.name == 'nt':
                self._enter_windows()
            else:
                anchor, parts = _parts(self.root)
                self.root_fd = open_directory_beneath(anchor, parts,
                    create=self.create and self.expected_root is None, require_dir_fd=True)
                self.root_identity = _identity(os.fstat(self.root_fd))
                if self.expected_root is not None and self.root_identity != self.expected_root:
                    raise NoFollowPathError('owner snapshot root binding changed')
                self.fd = os.dup(self.root_fd)
                for part in self.parts:
                    if self.create:
                        try:
                            os.mkdir(part, mode=0o700, dir_fd=self.fd)
                        except FileExistsError:
                            pass
                    child = _openat_directory(self.fd, part)
                    os.close(self.fd)
                    self.fd = child
                self.identity = _identity(os.fstat(self.fd))
            self.verify()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _windows_open(self, path):
        handle = self.kernel.CreateFileW(str(path), 0x80, 0x1, None, 3, 0x02000000 | 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            value = self.Info()
            if not self.kernel.GetFileInformationByHandle(handle, ctypes.byref(value)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not value.attributes & 0x10 or value.attributes & 0x400:
                raise NoFollowPathError('owner snapshot directory is a reparse point or not a directory')
            return handle, (value.volume, value.index_high, value.index_low)
        except BaseException:
            self.kernel.CloseHandle(handle)
            raise

    def _enter_windows(self):
        self.kernel, self.Info = _windows_api()
        anchor, root_parts = _parts(self.root)
        current = anchor
        for part in (None, *root_parts, *self.parts):
            if part is not None:
                current = current / part
                if self.create:
                    try:
                        current.mkdir(mode=0o700)
                    except FileExistsError:
                        pass
            handle, identity = self._windows_open(current)
            self.handles.append((current, handle, identity))
            if current == self.root:
                self.root_identity = identity
                if self.expected_root is not None and identity != self.expected_root:
                    raise NoFollowPathError('owner snapshot root binding changed')
        self.identity = self.handles[-1][2]

    def verify(self):
        if os.name == 'nt':
            for path, _handle, expected in self.handles:
                current, identity = self._windows_open(path)
                self.kernel.CloseHandle(current)
                if identity != expected:
                    raise NoFollowPathError('owner snapshot directory binding changed')
            return
        anchor, parts = _parts(self.root)
        root = open_directory_beneath(anchor, parts, require_dir_fd=True)
        child = None
        try:
            if _identity(os.fstat(root)) != self.root_identity:
                raise NoFollowPathError('owner snapshot root binding changed')
            child = os.dup(root)
            for part in self.parts:
                following = _openat_directory(child, part)
                os.close(child)
                child = following
            if _identity(os.fstat(child)) != self.identity:
                raise NoFollowPathError('owner snapshot directory binding changed')
        finally:
            if child is not None:
                os.close(child)
            os.close(root)

    def __exit__(self, exc_type=None, exc=None, tb=None):
        for descriptor in (self.fd, self.root_fd):
            if descriptor is not None:
                os.close(descriptor)
        self.fd = self.root_fd = None
        for _path, handle, _identity_value in reversed(self.handles):
            self.kernel.CloseHandle(handle)
        self.handles.clear()

    def names(self):
        self.verify()
        result = os.listdir(self.fd if self.fd is not None else self.path)
        self.verify()
        return result

    def info(self, name):
        validate_component(name)
        return (os.stat(name, dir_fd=self.fd, follow_symlinks=False) if self.fd is not None
                else (self.path / name).lstat())

    def open_read(self, name):
        validate_component(name)
        self.verify()
        if self.fd is not None:
            return _openat_file(self.fd, name, os.O_RDONLY, 0o600)
        import msvcrt

        handle = self.kernel.CreateFileW(str(self.path / name), 0x80000000, 0x3, None, 3, 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            value = self.Info()
            if not self.kernel.GetFileInformationByHandle(handle, ctypes.byref(value)):
                raise ctypes.WinError(ctypes.get_last_error())
            if value.attributes & (0x10 | 0x400) or value.links != 1:
                raise NoFollowPathError('owner snapshot source is not regular single-link')
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            self.kernel.CloseHandle(handle)
            raise
        return descriptor

    def entry_signature(self, name):
        info = self.info(name)
        if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            descriptor = self.open_read(name)
            try:
                if _identity(info) != _identity(os.fstat(descriptor)):
                    raise NoFollowPathError('owner snapshot leaf changed during open')
                return file_signature(descriptor)
            finally:
                os.close(descriptor)
        return tuple(getattr(info, field) for field in
            ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_mode', 'st_nlink'))

    def remove(self, name, expected=None, *, recursive=True):
        validate_component(name)
        self.verify()
        try:
            current = self.info(name)
        except FileNotFoundError:
            return
        if expected is not None and _identity(current) != _identity(expected):
            raise NoFollowPathError('owner snapshot cleanup candidate changed')
        reparse = bool(getattr(current, 'st_file_attributes', 0) & 0x400)
        if reparse and stat.S_ISDIR(current.st_mode):
            os.rmdir(self.path / name)
        elif stat.S_ISDIR(current.st_mode) and not stat.S_ISLNK(current.st_mode):
            if self.fd is not None:
                if recursive:
                    _remove_directory_at(self.fd, name)
                else:
                    os.rmdir(name, dir_fd=self.fd)
            else:
                if recursive:
                    with SnapshotParent(self.root, (*self.parts, name), expected_root=self.root_identity) as child:
                        for item in child.names():
                            child.remove(item, child.info(item))
                os.rmdir(self.path / name)
        elif self.fd is not None:
            os.unlink(name, dir_fd=self.fd)
        else:
            os.unlink(self.path / name)
        self.verify()

    def publish(self, name, source, row):
        """只复制已打开的私有下载FD；同一父锚点内创建/校验/rename，不重新打开目标路径。"""
        validate_component(name)
        self.verify()
        temporary = '.' + name + '.' + uuid.uuid4().hex + '.restore'
        descriptor = None
        created = None
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
            descriptor = (os.open(temporary, flags, 0o600, dir_fd=self.fd) if self.fd is not None
                          else os.open(self.path / temporary, flags, 0o600))
            created = _identity(os.fstat(descriptor))
            source.seek(0)
            digest, size = hashlib.sha256(), 0
            with os.fdopen(descriptor, 'wb') as output:
                descriptor = None
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    if size > row.size_bytes:
                        raise RuntimeError(f'owner object 校验失败: {row.relative_path}')
                    digest.update(chunk)
                    output.write(chunk)
                if size != row.size_bytes or digest.hexdigest() != row.content_sha256:
                    raise RuntimeError(f'owner object 校验失败: {row.relative_path}')
                if hasattr(os, 'fchmod'):
                    os.fchmod(output.fileno(), row.file_mode & 0o777)
                elif os.name == 'nt':
                    _windows_fd_mode(output.fileno(), row.file_mode)
                output.flush()
                os.fsync(output.fileno())
            self.verify()
            if _identity(self.info(temporary)) != created:
                raise NoFollowPathError('owner snapshot temporary file changed')
            if self.fd is not None:
                os.rename(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
                os.fsync(self.fd)
            else:
                # 全父链原生句柄仍持有且拒绝rename/delete/reparse，不是便携Path检查后裸replace。
                os.replace(self.path / temporary, self.path / name)
            self.verify()
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                if created is not None and _identity(self.info(temporary)) == created:
                    if self.fd is not None:
                        os.unlink(temporary, dir_fd=self.fd)
                    else:
                        os.unlink(self.path / temporary)
            except FileNotFoundError:
                pass


def _windows_fd_mode(descriptor, mode):
    import msvcrt
    from ctypes import wintypes

    class BasicInfo(ctypes.Structure):
        _fields_ = [(name, ctypes.c_int64) for name in ('creation', 'access', 'write', 'change')] + [
            ('attributes', wintypes.DWORD)]
    kernel = _windows_api()[0]
    update = kernel.SetFileInformationByHandle
    update.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    update.restype = wintypes.BOOL
    info = BasicInfo(attributes=0x80 if mode & 0o222 else 0x1)
    if not update(msvcrt.get_osfhandle(descriptor), 0, ctypes.byref(info), ctypes.sizeof(info)):
        raise ctypes.WinError(ctypes.get_last_error())


class DownloadedObjects:
    """整次恢复共用一个0700控制区；文件验证后立即关FD，不按清单数量占句柄。"""

    def __init__(self, client, bucket):
        self.client, self.bucket = client, bucket

    def __enter__(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='my-agent-owner-download-')
        try:
            self.root = Path(self.temporary.name).resolve()
            self.private = SnapshotParent(self.root).__enter__()
        except BaseException:
            self.temporary.cleanup()
            raise
        return self

    def __exit__(self, exc_type=None, exc=None, tb=None):
        try:
            self.private.__exit__(exc_type, exc, tb)
        finally:
            self.temporary.cleanup()

    def fetch(self, row):
        name = uuid.uuid4().hex + '.object'
        self.client.download_file(self.bucket, row.object_key, str(self.root / name))
        with self.open(name, row):
            pass
        return name

    @contextmanager
    def open(self, name, row):
        self.private.verify()
        private = self.private
        try:
            descriptor = private.open_read(name)
            with os.fdopen(descriptor, 'rb') as source:
                before = os.fstat(source.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size != row.size_bytes:
                    raise RuntimeError(f'owner object 校验失败: {row.relative_path}')
                signature = file_signature(source.fileno())
                digest = hashlib.file_digest(source, 'sha256').hexdigest()
                if digest != row.content_sha256 or file_signature(source.fileno()) != signature:
                    raise RuntimeError(f'owner object 校验失败: {row.relative_path}')
                private.verify()
                yield source
        finally:
            private.verify()


@contextmanager
def downloaded_object(client, bucket, row):
    """原下载API只拿工作区之外私有路径；坏正文不会触碰owner现有文件。"""
    with DownloadedObjects(client, bucket) as batch:
        name = batch.fetch(row)
        with batch.open(name, row) as source:
            yield source


def tree_snapshot(parent):
    """在同一owner锚点上记录清理前树，不跟随链接；目录变化不能被误认成可删的旧树。"""
    result, pending = {}, [()]
    while pending:
        parts = pending.pop()
        parent.verify()
        with SnapshotParent(parent.root, (*parent.parts, *parts), expected_root=parent.root_identity) as current:
            for name in current.names():
                info = current.info(name)
                key = (*parts, name)
                result[key] = current.entry_signature(name)
                if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) and not (
                        getattr(info, 'st_file_attributes', 0) & 0x400):
                    pending.append(key)
    parent.verify()
    return result
