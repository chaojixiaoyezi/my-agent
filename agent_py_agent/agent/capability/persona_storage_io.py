# LLM: C目录/版本锁退出只尝试原每个FD close一次；词法native/physical primary不被secondary OSError盖掉，False和ordinary优先原合同。
# 模块用途: 沿原当前门读写人格私有文件，准确回收FD而不发行清理权。
"""Strict no-follow Persona version I/O. Callers own the owner/target transaction."""
import json
import os
import secrets
import sys
from contextlib import contextmanager
from pathlib import Path

from ..backends.bounded_call import BoundedCallExitUnconfirmedError
from ..common.global_write_commit import GlobalWriteCommitUnavailable, execute_global_write
from ..common.global_write_guard import GlobalWriteInWorkerError
from ..common.nofollow_fs import _openat_directory, _openat_file, open_directory_beneath
from ..common.owner_write_errors import (
    LocalControlCoordinatorUnavailable,
    OwnerManagementIngressUnavailable,
)
from ..common.request_execution_ports import (
    RequestExecutionContextUnavailable,
    RequestExecutionOutcomeUncertainError,
)
from ..common.tool_write_context import ToolWriteContextUnavailable

# 原当前source/common核验及其实际物理外层；只透传类型，不按码名推断权限。
_PERSONA_NATIVE_ERRORS = (GlobalWriteCommitUnavailable, GlobalWriteInWorkerError,
    LocalControlCoordinatorUnavailable, OwnerManagementIngressUnavailable,
    RequestExecutionContextUnavailable, RequestExecutionOutcomeUncertainError,
    ToolWriteContextUnavailable, BoundedCallExitUnconfirmedError)


# LLM: C仅准确目录/锁FD/本地写叶进入原execute_global_write，等待/编码/scan在外；native primary保留，必要FD关闭不授清理权。
# 函数用途: 安全读写与收尾人格私有文件，只按显式bool选择原当前提交门。
def persona_io(operation, *, coordinator_io=False):
    if coordinator_io:
        return execute_global_write(operation, require_scope=True)
    return operation()


# LLM: 原父目录FD只关闭一次；C词法native已存在时保同对象/cause/context，secondary close不授改盘或重试。
# 函数用途: 固定严格父目录并归还原FD，C退出不丢真正来源异常。
@contextmanager
def parent_directory(path, *, create=False, coordinator_io=False):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('Persona storage path must be absolute')
    descriptor = persona_io(lambda: open_directory_beneath(path.anchor, path.parts[1:-1], create=create, require_dir_fd=True), coordinator_io=coordinator_io and create)
    try:
        yield descriptor
    finally:
        if coordinator_io:
            _close_storage_descriptor(descriptor, sys.exception())
        else:
            os.close(descriptor)


# LLM: 显式C沿原父目录上下文透传同一IO bool以保退出primary；False不添kwargs，原nofollow/文件操作与close次数保持。
# 函数用途: 复用原本地文件叶，父目录close不能掩盖C真正来源异常。
def open_file(path, flags, *, coordinator_io=False):
    with parent_directory(path, **({} if not coordinator_io else {"coordinator_io": True})) as parent:
        return persona_io(lambda: _openat_file(parent, path.name, flags, 0o600), coordinator_io=coordinator_io and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)))


# LLM: 显式C沿原父目录上下文透传同一IO bool以保退出primary；False不添kwargs，原nofollow/文件操作与close次数保持。
# 函数用途: 复用原本地文件叶，父目录close不能掩盖C真正来源异常。
def transfer(source, destination, *, move=False, coordinator_io=False):
    with parent_directory(source, **({} if not coordinator_io else {"coordinator_io": True})) as src, parent_directory(destination, **({} if not coordinator_io else {"coordinator_io": True})) as dst:
        if move:
            persona_io(lambda: os.rename(source.name, destination.name, src_dir_fd=src, dst_dir_fd=dst), coordinator_io=coordinator_io)
        else:
            persona_io(lambda: os.link(source.name, destination.name, src_dir_fd=src, dst_dir_fd=dst, follow_symlinks=False), coordinator_io=coordinator_io)


# LLM: 显式C沿原父目录上下文透传同一IO bool以保退出primary；False不添kwargs，原nofollow/文件操作与close次数保持。
# 函数用途: 复用原本地文件叶，父目录close不能掩盖C真正来源异常。
def remove_private(path, *, directory=False, coordinator_io=False):
    with parent_directory(path, **({} if not coordinator_io else {"coordinator_io": True})) as parent:
        operation = os.rmdir if directory else os.unlink
        persona_io(lambda: operation(path.name, dir_fd=parent), coordinator_io=coordinator_io)


# LLM: C仅准确目录/锁FD/本地写叶进入原execute_global_write，等待/编码/scan在外；native primary保留，必要FD关闭不授清理权。
# 函数用途: 安全读写与收尾人格私有文件，只按显式bool选择原当前提交门。
def make_private_directory(parent, prefix, *, coordinator_io=False):
    with parent_directory(parent / '_anchor', create=True, **({} if not coordinator_io else {'coordinator_io': True})) as descriptor:
        name = prefix + secrets.token_hex(16)
        persona_io(lambda: os.mkdir(name, mode=0o700, dir_fd=descriptor), coordinator_io=coordinator_io)
        return parent / name


# LLM: C仅准确目录/锁FD/本地写叶进入原execute_global_write，等待/编码/scan在外；native primary保留，必要FD关闭不授清理权。
# 函数用途: 安全读写与收尾人格私有文件，只按显式bool选择原当前提交门。
@contextmanager
def temporary_directory(*, prefix, dir, ignore_cleanup_errors=False, coordinator_io=False):
    with parent_directory(Path(dir) / '_anchor', create=True, **({} if not coordinator_io else {'coordinator_io': True})) as parent:
        name = prefix + secrets.token_hex(16)
        persona_io(lambda: os.mkdir(name, mode=0o700, dir_fd=parent), coordinator_io=coordinator_io)
        descriptor = _openat_directory(parent, name)
        try:
            yield str(Path(dir) / name)
        finally:
            _cleanup_temporary_directory(parent, name, descriptor, ignore_cleanup_errors, **({} if not coordinator_io else {'coordinator_io': True, 'primary': sys.exception()}))


# LLM: C仅准确目录/锁FD/本地写叶进入原execute_global_write，等待/编码/scan在外；native primary保留，必要FD关闭不授清理权。
# 函数用途: 安全读写与收尾人格私有文件，只按显式bool选择原当前提交门。
def _cleanup_temporary_directory(parent, name, descriptor, ignore_cleanup_errors, *, coordinator_io=False, primary=None):
    try:
        if coordinator_io and isinstance(primary, _PERSONA_NATIVE_ERRORS):
            return
        for leaf in os.listdir(descriptor):
            persona_io(lambda: os.unlink(leaf, dir_fd=descriptor), coordinator_io=coordinator_io)
        persona_io(lambda: os.rmdir(name, dir_fd=parent), coordinator_io=coordinator_io)
    except OSError as error:
        if coordinator_io and isinstance(error, _PERSONA_NATIVE_ERRORS):
            raise
        if not ignore_cleanup_errors:
            raise
    finally:
        if coordinator_io:
            active = sys.exception() or primary
            try:
                os.close(descriptor)
            except OSError:
                if not isinstance(active, _PERSONA_NATIVE_ERRORS):
                    raise
        else:
            os.close(descriptor)


# LLM: 原版本锁FD只关闭一次；C词法native已存在时保同对象/cause/context，secondary close不授改盘或重试。
# 函数用途: 原flock等待在门外，结束时关锁FD且保C真正primary。
@contextmanager
def versions_lock(path, *, coordinator_io=False):
    try:
        import fcntl
    except ImportError as error:
        raise OSError('PERSONA_STRICT_LOCK_UNAVAILABLE') from error
    with parent_directory(path, create=True, **({} if not coordinator_io else {'coordinator_io': True})) as parent:
        name = path.name + '.lock'
        descriptor = persona_io(lambda: _openat_file(parent, name, os.O_RDWR | os.O_CREAT, 0o600), coordinator_io=coordinator_io)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            _verify_versions_lock(parent, name, descriptor)
            yield
        finally:
            if coordinator_io:
                _close_storage_descriptor(descriptor, sys.exception())
            else:
                os.close(descriptor)


# LLM: 已持内核锁后再核验公开锁名；等待期间被换身时拒绝把旧FD当新路径的互斥权。
# 函数用途: 确认版本锁仍是同一个文件。
def _verify_versions_lock(parent, name, descriptor):
    actual, opened = os.stat(name, dir_fd=parent, follow_symlinks=False), os.fstat(descriptor)
    if (actual.st_dev, actual.st_ino) != (opened.st_dev, opened.st_ino):
        raise OSError('PERSONA_VERSION_LOCK_CHANGED')


# LLM: C仅准确目录/锁FD/本地写叶进入原execute_global_write，等待/编码/scan在外；native primary保留，必要FD关闭不授清理权。
# 函数用途: 安全读写与收尾人格私有文件，只按显式bool选择原当前提交门。
def append_version_records(path, records):
    body = encode_version_records(records)
    append_encoded_version_records(path, body)


# LLM: C仅准确目录/锁FD/本地写叶进入原execute_global_write，等待/编码/scan在外；native primary保留，必要FD关闭不授清理权。
# 函数用途: 安全读写与收尾人格私有文件，只按显式bool选择原当前提交门。
def encode_version_records(records):
    return ''.join(json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n' for row in records).encode()


# LLM: C仅准确目录/锁FD/本地写叶进入原execute_global_write，等待/编码/scan在外；native primary保留，必要FD关闭不授清理权。
# 函数用途: 安全读写与收尾人格私有文件，只按显式bool选择原当前提交门。
def append_encoded_version_records(path, body):
    descriptor = open_file(path, os.O_WRONLY | os.O_APPEND)
    with os.fdopen(descriptor, 'ab') as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())


# LLM: 只供本模块原parent/versions两个finally，调用方传本词法primary；同一原os.close算法只尝试一次，无注册器/状态/写入或补偿。
# 函数用途: 归还准确原FD，C已有实际native时不让secondary OSError抢走它；普通错误仍抛原close错。
def _close_storage_descriptor(descriptor, primary):
    try:
        os.close(descriptor)
    except OSError:
        if not isinstance(primary, _PERSONA_NATIVE_ERRORS):
            raise
