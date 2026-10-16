# LLM: 版本IO保持严格no-follow，调用方持owner/目标事务；路径和内容都不能自证来源许可。
# 模块用途: 安全读写人格版本与快照的底层文件。
"""Strict no-follow Persona version I/O. Callers own the owner/target transaction."""
import json
import os
import secrets
from contextlib import contextmanager
from pathlib import Path

from ..common.nofollow_fs import _openat_directory, _openat_file, open_directory_beneath


# LLM: 路径只接受仓库生成的绝对词法路径；每段nofollow，所有最终操作仍经已打开父FD。
# 函数用途: 固定版本文件所在目录，阻止中途符号链接把读写带到owner外。
@contextmanager
def parent_directory(path, *, create=False):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('Persona storage path must be absolute')
    descriptor = open_directory_beneath(path.anchor, path.parts[1:-1], create=create, require_dir_fd=True)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


# LLM: 返回FD归调用者关闭，拒绝所有父链接及叶子多链接；创建是排他模式。
# 函数用途: 安全打开私有版本文件而不再次普通open路径。
def open_file(path, flags):
    with parent_directory(path) as parent:
        return _openat_file(parent, path.name, flags, 0o600)


# LLM: 两端分别固定目录FD；link本身排他，rename仅用于调用方拥有的唯一私有空目的名。
# 函数用途: 在不跟随父目录替换的前提下移动或排他发布版本文件。
def transfer(source, destination, *, move=False):
    with parent_directory(source) as src, parent_directory(destination) as dst:
        if move:
            os.rename(source.name, destination.name, src_dir_fd=src, dst_dir_fd=dst)
        else:
            os.link(source.name, destination.name, src_dir_fd=src, dst_dir_fd=dst, follow_symlinks=False)


# LLM: 只供本事务私有暂存名清理；公开冲突对象不能调用此函数。
# 函数用途: 通过固定父FD清除私有文件或已知空目录，不跟随父链接。
def remove_private(path, *, directory=False):
    with parent_directory(path) as parent:
        operation = os.rmdir if directory else os.unlink
        operation(path.name, dir_fd=parent)


# LLM: 随机目录在已打开父目录下排他创建；可保留为恢复材料，不使用路径形式tempfile创建。
# 函数用途: 为账本和快照创建0700私有暂存或恢复目录。
def make_private_directory(parent, prefix):
    with parent_directory(parent / '_anchor', create=True) as descriptor:
        name = prefix + secrets.token_hex(16)
        os.mkdir(name, mode=0o700, dir_fd=descriptor)
        return parent / name


# LLM: 清理只消费创建时固定的目录FD；不递归删除，不清除不认识的子目录。
# 函数用途: 暂存结束后归还已知文件，父路径被替换也不触碰新目录。
@contextmanager
def temporary_directory(*, prefix, dir, ignore_cleanup_errors=False):
    with parent_directory(Path(dir) / '_anchor', create=True) as parent:
        name = prefix + secrets.token_hex(16)
        os.mkdir(name, mode=0o700, dir_fd=parent)
        descriptor = _openat_directory(parent, name)
        try:
            yield str(Path(dir) / name)
        finally:
            _cleanup_temporary_directory(parent, name, descriptor, ignore_cleanup_errors)


# LLM: 只消费创建时固定的目录FD；即使清理失败也关闭描述符，错误策略由原调用方传入。
# 函数用途: 清除私有暂存文件并归还目录描述符。
def _cleanup_temporary_directory(parent, name, descriptor, ignore_cleanup_errors):
    try:
        for leaf in os.listdir(descriptor):
            os.unlink(leaf, dir_fd=descriptor)
        os.rmdir(name, dir_fd=parent)
    except OSError:
        if not ignore_cleanup_errors:
            raise
    finally:
        os.close(descriptor)


# LLM: 与旧versions.jsonl.lock共用OS锁；无fcntl在任何落盘前明确拒绝，不阻断模块导入。
# 函数用途: 对版本读、选版和发布使用严格共同锁，不能静默降级。
@contextmanager
def versions_lock(path):
    try:
        import fcntl
    except ImportError as error:
        raise OSError('PERSONA_STRICT_LOCK_UNAVAILABLE') from error
    with parent_directory(path, create=True) as parent:
        name = path.name + '.lock'
        descriptor = _openat_file(parent, name, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            _verify_versions_lock(parent, name, descriptor)
            yield
        finally:
            os.close(descriptor)


# LLM: 已持内核锁后再核验公开锁名；等待期间被换身时拒绝把旧FD当新路径的互斥权。
# 函数用途: 确认版本锁仍是同一个文件。
def _verify_versions_lock(parent, name, descriptor):
    actual, opened = os.stat(name, dir_fd=parent, follow_symlinks=False), os.fstat(descriptor)
    if (actual.st_dev, actual.st_ino) != (opened.st_dev, opened.st_ino):
        raise OSError('PERSONA_VERSION_LOCK_CHANGED')


# LLM: 仅供唯一私有暂存账本；格式保持旧append私有JSONL一致，所有落盘均用nofollow父FD。
# 函数用途: 写完整批次并刷盘，不在校验后又走普通路径追加。
def append_version_records(path, records):
    body = ''.join(json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n' for row in records).encode()
    descriptor = open_file(path, os.O_WRONLY | os.O_APPEND)
    with os.fdopen(descriptor, 'ab') as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
