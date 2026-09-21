"""LLM: 宿主文件读写固定受信根；文本和二进制共用逐段 no-follow 原语，不能用它代替业务权限。

模块用途: 当模型或 shell 能修改工作区目录时，宿主通过受信根逐段拒绝符号链接地读写内部账本，
避免普通 Path.open/mkdir 跟随被替换的父目录越出工作区。
"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path


# LLM: All structural failures use one typed OS error so callers can fail closed without parsing text.
# 类用途: 表示受管相对路径包含符号链接、非目录组件或打开期间发生身份变化。
class NoFollowPathError(OSError):
    pass


# LLM: Append is anchored at an already trusted root; relative parts are fixed application data,
# not model input. Parent creation and the final file open both use dir_fd and O_NOFOLLOW.
# 函数用途: 在可变工作区内安全创建父目录并向宿主管理的文本账本追加一段内容。
def append_text_beneath(
    root: str | Path,
    relative_parts: tuple[str, ...],
    text: str,
    *,
    directory_mode: int = 0o700,
    file_mode: int = 0o600,
) -> None:
    _validate_relative_parts(relative_parts)
    if not _supports_dir_fd():
        _append_text_portable(root, relative_parts, text, directory_mode, file_mode)
        return
    parent_fd = open_directory_beneath(
        root,
        relative_parts[:-1],
        create=True,
        mode=directory_mode,
    )
    descriptor = -1
    try:
        descriptor = _openat_file(
            parent_fd,
            relative_parts[-1],
            os.O_WRONLY | os.O_APPEND | os.O_CREAT,
            file_mode,
        )
        payload = text.encode("utf-8")
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while appending managed file")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)


# LLM: 文本复用同一二进制 no-follow 链；缺失返回 None，路径违规与 UTF-8 损坏显式失败，不缓存未验证内容。
# 函数用途: 从受信根安全读取一个宿主管理的 UTF-8 文本文件。
def read_text_beneath(
    root: str | Path,
    relative_parts: tuple[str, ...],
) -> str | None:
    content = read_bytes_beneath(root, relative_parts)
    return content.decode("utf-8") if content is not None else None


# LLM: 与文本读取共用 no-follow 链；严格来源读取可要求 dirfd，能力不足不能降级 portable；预算限制实际读取而非仅相信 stat。
# 函数用途: 有界读取宿主管理的普通二进制文件，不跟随链接，也不创建目录。
def read_bytes_beneath(
    root: str | Path,
    relative_parts: tuple[str, ...],
    *,
    max_bytes: int | None = None,
    require_dir_fd: bool = False,
) -> bytes | None:
    _validate_relative_parts(relative_parts)
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 0):
        raise ValueError("managed read limit must be a nonnegative integer")
    if not _supports_dir_fd():
        if require_dir_fd:
            raise NoFollowPathError("strict no-follow reading is unavailable")
        return _read_bytes_portable(root, relative_parts, max_bytes)
    try:
        parent_fd = open_directory_beneath(root, relative_parts[:-1])
    except FileNotFoundError:
        return None
    descriptor = -1
    try:
        try:
            descriptor = _openat_file(parent_fd, relative_parts[-1], os.O_RDONLY, 0o600)
        except FileNotFoundError:
            return None
        return _read_descriptor(descriptor, max_bytes)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)


# LLM: Validation probes may ask only whether a bounded regular file exists. Symlinks, hardlinks,
# directories, traversal and unreadable components all return False without exposing host details.
# 函数用途: 安全判断受信根内一条相对路径是否是单链接普通文件。
def regular_file_exists_beneath(
    root: str | Path,
    relative_parts: tuple[str, ...],
) -> bool:
    try:
        _validate_relative_parts(relative_parts)
        if not _supports_dir_fd():
            target = _portable_path(root, relative_parts, create=False)
            if target.is_symlink() or not target.is_file():
                return False
            return int(target.stat().st_nlink) == 1
        parent_fd = open_directory_beneath(root, relative_parts[:-1])
        descriptor = -1
        try:
            descriptor = _openat_file(parent_fd, relative_parts[-1], os.O_RDONLY, 0o600)
            return True
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(parent_fd)
    except (FileNotFoundError, NoFollowPathError, OSError, ValueError):
        return False


# LLM: 文本状态文件与二进制共用同目录原子替换；替换后异常不证明未提交，调用方须按自己的提交标识读回。
# 函数用途: 在受信根内原子写入一个 UTF-8 状态文件，并把文件和父目录都刷盘。
def write_text_atomic_beneath(
    root: str | Path,
    relative_parts: tuple[str, ...],
    text: str,
    *,
    directory_mode: int = 0o700,
    file_mode: int = 0o600,
) -> None:
    write_bytes_atomic_beneath(
        root,
        relative_parts,
        text.encode("utf-8"),
        directory_mode=directory_mode,
        file_mode=file_mode,
    )


# LLM: 候选文件先以私有权限完整写入并刷盘，再同目录替换；本函数不拥有领域锁或跨文件事务。
# 函数用途: 安全原子保存宿主二进制文件；POSIX 刷父目录，portable 分支不承诺相同的目录耐久性。
def write_bytes_atomic_beneath(
    root: str | Path,
    relative_parts: tuple[str, ...],
    payload: bytes,
    *,
    directory_mode: int = 0o700,
    file_mode: int = 0o600,
) -> None:
    _validate_relative_parts(relative_parts)
    if not _supports_dir_fd():
        _write_bytes_portable(root, relative_parts, payload, file_mode)
        return
    parent_fd = open_directory_beneath(
        root,
        relative_parts[:-1],
        create=True,
        mode=directory_mode,
    )
    temporary_name = f".{relative_parts[-1]}.tmp-{secrets.token_hex(16)}"
    descriptor = -1
    try:
        descriptor = _openat_file(
            parent_fd,
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            file_mode,
        )
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while replacing managed file")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        _reject_unsafe_existing_leaf(parent_fd, relative_parts[-1])
        os.rename(
            temporary_name,
            relative_parts[-1],
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)


# LLM: 永久锁仅允许单链接普通文件；先排他创建，已存在再打开，避免并发非排他创建的歧义；调用方关闭 fd。
# 函数用途: 安全创建私有锁目录并打开不截断的锁文件，避免目录预检后再次按路径跟随链接。
def open_private_lock_beneath(root: str | Path, relative_parts: tuple[str, ...]) -> int:
    _validate_relative_parts(relative_parts)
    if not _supports_dir_fd():
        return _open_private_lock_portable(root, relative_parts)
    parent_fd = open_directory_beneath(root, relative_parts[:-1], create=True)
    try:
        os.fchmod(parent_fd, 0o700)
        try:
            return _openat_file(
                parent_fd, relative_parts[-1], os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600
            )
        except FileExistsError:
            return _openat_file(parent_fd, relative_parts[-1], os.O_RDWR, 0o600)
    finally:
        os.close(parent_fd)


# LLM: 缺少 dir_fd 时仍检查父链、锁叶子与打开后身份；无法承诺 POSIX 的目录替换竞态强度。
# 函数用途: 在 portable 平台打开永久锁，拒绝已有链接或特殊文件，不截断原锁。
def _open_private_lock_portable(root: str | Path, parts: tuple[str, ...]) -> int:
    target = _portable_path(root, parts, create=True)
    try:
        before = target.lstat()
    except FileNotFoundError:
        before = None
    if before is not None and (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1):
        raise NoFollowPathError("managed lock is not a regular file")
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        descriptor = os.open(target, flags, 0o600)
    try:
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
            or (before is not None and not _same_identity(before, after))
        ):
            raise NoFollowPathError("managed lock changed during open")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


# LLM: Managed cleanup unlinks regular leaves only and never follows an attacker-controlled link.
# 函数用途: 从受信根安全删除一个普通状态文件；文件不存在时视为已经清理。
def unlink_file_beneath(root: str | Path, relative_parts: tuple[str, ...]) -> None:
    _validate_relative_parts(relative_parts)
    if not _supports_dir_fd():
        target = _portable_path(root, relative_parts, create=False)
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise NoFollowPathError("managed file is not regular")
        target.unlink(missing_ok=True)
        return
    try:
        parent_fd = open_directory_beneath(root, relative_parts[:-1])
    except FileNotFoundError:
        return
    try:
        try:
            metadata = os.stat(
                relative_parts[-1],
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise NoFollowPathError("managed file is not regular")
        os.unlink(relative_parts[-1], dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


# LLM: Recovery discovers operation directories through an already verified directory descriptor;
# returned names are data only and every later read validates them again as one path component.
# 函数用途: 安全列出受信根内一个目录的直接子项名称，目录不存在时返回空列表。
def list_names_beneath(root: str | Path, relative_parts: tuple[str, ...]) -> list[str]:
    _validate_relative_parts(relative_parts, allow_empty=True)
    if not _supports_dir_fd():
        base = _portable_directory(root, relative_parts, create=False)
        return sorted(path.name for path in base.iterdir())
    try:
        descriptor = open_directory_beneath(root, relative_parts)
    except FileNotFoundError:
        return []
    try:
        with os.scandir(descriptor) as entries:
            return sorted(entry.name for entry in entries)
    finally:
        os.close(descriptor)


# LLM: Directory traversal owns and returns only the final descriptor. Every component is opened
# relative to the prior fd; callers must close the returned descriptor.
# 函数用途: 从受信根逐层打开内部目录，可选安全创建缺失的固定目录段。
def open_directory_beneath(
    root: str | Path,
    relative_parts: tuple[str, ...],
    *,
    create: bool = False,
    mode: int = 0o700,
) -> int:
    _validate_relative_parts(relative_parts, allow_empty=True)
    descriptor = _open_verified_root(Path(root).expanduser())
    try:
        for part in relative_parts:
            if create:
                try:
                    os.mkdir(part, mode=mode, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = _openat_directory(descriptor, part)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


# LLM: Exact relative components are code-owned protocol values. Reject separators and traversal
# before they reach any dir_fd syscall.
# 函数用途: 校验受管相对路径只由普通单层目录名组成。
def _validate_relative_parts(parts: tuple[str, ...], *, allow_empty: bool = False) -> None:
    if not parts and not allow_empty:
        raise ValueError("managed relative path is empty")
    if any(not part or part in {".", ".."} or Path(part).name != part for part in parts):
        raise ValueError("managed relative path contains an invalid component")


# LLM: Root identity is checked across lstat/open so replacing the trusted root during entry
# cannot redirect the descriptor.
# 函数用途: 安全打开受信根目录并验证打开前后仍是同一目录。
def _open_verified_root(root: Path) -> int:
    root = Path(os.path.abspath(os.path.normpath(str(root.expanduser()))))
    before = root.lstat()
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise NoFollowPathError("managed root is not a regular directory")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(root, flags)
    if not _same_identity(before, os.fstat(descriptor)):
        os.close(descriptor)
        raise NoFollowPathError("managed root changed during open")
    return descriptor


# LLM: Intermediate directories can never be symlinks, even if their target would remain under root.
# 函数用途: 在已验证父目录中打开一个普通子目录。
def _openat_directory(parent_fd: int, name: str) -> int:
    before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise NoFollowPathError("managed path contains a non-directory or symlink component")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    if not _same_identity(before, os.fstat(descriptor)):
        os.close(descriptor)
        raise NoFollowPathError("managed directory changed during open")
    return descriptor


# LLM: 只接受单链接普通文件；no-follow 与 inode 比较阻止替换，非阻塞打开避免预检后换成 FIFO 使线程卡住。
# 函数用途: 在已验证父目录中打开并复核最终文件，调用方负责关闭描述符。
def _openat_file(parent_fd: int, name: str, flags: int, mode: int) -> int:
    before: os.stat_result | None
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        before = None
    if before is not None and (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or int(before.st_nlink) != 1
    ):
        raise NoFollowPathError("managed file is not a regular file")
    descriptor = os.open(
        name,
        flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        mode,
        dir_fd=parent_fd,
    )
    after = os.fstat(descriptor)
    if (
        not stat.S_ISREG(after.st_mode)
        or int(after.st_nlink) != 1
        or (before is not None and not _same_identity(before, after))
    ):
        os.close(descriptor)
        raise NoFollowPathError("managed file changed during open")
    return descriptor


# LLM: Atomic replacement may overwrite only an absent or regular destination leaf. Symlinks are
# rejected as tamper evidence even though POSIX rename itself would not follow them.
# 函数用途: 原子替换前确认目标名称不是链接或目录。
def _reject_unsafe_existing_leaf(parent_fd: int, name: str) -> None:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise NoFollowPathError("managed destination is not a regular file")


# LLM: The fallback is used only where Python lacks dir_fd. It rejects every existing parent link,
# verifies final containment, and keeps the same regular-file requirement.
# 函数用途: 在不支持 dir_fd 的平台上保守地解析受管文件路径。
def _portable_path(root: str | Path, parts: tuple[str, ...], *, create: bool) -> Path:
    base = Path(os.path.abspath(os.path.normpath(str(Path(root).expanduser()))))
    _verify_portable_root(base)
    current = _portable_directory(base, parts[:-1], create=create)
    target = current / parts[-1]
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise NoFollowPathError("managed path escapes root") from exc
    return target


# LLM: 只容忍并发创建的 FileExistsError，随后重新 lstat；不 resolve 链接，也不承诺 POSIX 描述符的竞态强度。
# 函数用途: 在缺少 dir_fd 的平台逐层检查或创建普通目录，正常创建竞争不误报失败。
def _portable_directory(
    root: str | Path,
    parts: tuple[str, ...],
    *,
    create: bool,
) -> Path:
    base = Path(os.path.abspath(os.path.normpath(str(Path(root).expanduser()))))
    _verify_portable_root(base)
    current = base
    for part in parts:
        current = current / part
        if create:
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise NoFollowPathError("managed path contains an unsafe parent")
    return current


# LLM: The portable root check intentionally uses lstat on the lexical path rather than resolve.
# 函数用途: 确认受信根本身仍是普通目录而不是替换后的链接。
def _verify_portable_root(base: Path) -> None:
    metadata = base.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise NoFollowPathError("managed root is not a regular directory")


# LLM: Portable append mirrors descriptor semantics as closely as the platform permits.
# 函数用途: 在缺少 dir_fd 的平台安全追加 UTF-8 文本。
def _append_text_portable(
    root: str | Path,
    parts: tuple[str, ...],
    text: str,
    directory_mode: int,
    file_mode: int,
) -> None:
    _ = directory_mode
    target = _portable_path(root, parts, create=True)
    if target.is_symlink() or (
        target.exists() and (not target.is_file() or int(target.stat().st_nlink) != 1)
    ):
        raise NoFollowPathError("managed file is not regular")
    descriptor = os.open(
        target,
        os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        file_mode,
    )
    try:
        payload = text.encode("utf-8")
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


# LLM: Portable 读取保留缺失语义和实际字节预算，不把链接当作合法宿主文件。
# 函数用途: 在缺少 dir_fd 的平台读取有界二进制内容。
def _read_bytes_portable(
    root: str | Path, parts: tuple[str, ...], max_bytes: int | None
) -> bytes | None:
    try:
        target = _portable_path(root, parts, create=False)
    except FileNotFoundError:
        return None
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode) or int(metadata.st_nlink) != 1:
        raise NoFollowPathError("managed file is not regular")
    descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        return _read_descriptor(descriptor, max_bytes)
    finally:
        os.close(descriptor)


# LLM: 以剩余预算加一字节检测超限，不能因为文件大小在打开后改变而无界分配。
# 函数用途: 读取已验证的描述符，供 POSIX/portable 和文本/二进制共用。
def _read_descriptor(descriptor: int, max_bytes: int | None) -> bytes:
    chunks = []
    count = 0
    while True:
        size = 1024 * 1024 if max_bytes is None else min(1024 * 1024, max_bytes - count + 1)
        chunk = os.read(descriptor, size)
        if not chunk:
            return b"".join(chunks)
        count += len(chunk)
        if max_bytes is not None and count > max_bytes:
            raise ValueError("managed file exceeds read limit")
        chunks.append(chunk)


# LLM: 临时文件创建即为指定私有权限；原子替换前拒绝链接，异常可能发生在替换后，不能推断未提交。
# 函数用途: 在没有 dir_fd 的平台保存完整二进制内容并清理暂存文件。
def _write_bytes_portable(
    root: str | Path, parts: tuple[str, ...], payload: bytes, file_mode: int
) -> None:
    target = _portable_path(root, parts, create=True)
    temporary = target.with_name(f".{target.name}.tmp-{secrets.token_hex(16)}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, file_mode)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise NoFollowPathError("managed destination is not a regular file")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


# LLM: Feature detection is runtime-based because supported dir_fd sets differ by platform build.
# 函数用途: 判断当前 Python/操作系统是否支持安全逐段目录调用。
def _supports_dir_fd() -> bool:
    return all(
        function in os.supports_dir_fd
        for function in (os.open, os.stat, os.mkdir, os.rename, os.unlink)
    )


# LLM: Device and inode form the stable local identity for lstat/fstat comparisons.
# 函数用途: 比较两个 stat 结果是否指向同一文件系统对象。
def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


__all__ = [
    "NoFollowPathError",
    "append_text_beneath",
    "open_private_lock_beneath",
    "list_names_beneath",
    "open_directory_beneath",
    "read_text_beneath",
    "read_bytes_beneath",
    "regular_file_exists_beneath",
    "unlink_file_beneath",
    "write_text_atomic_beneath",
    "write_bytes_atomic_beneath",
]
