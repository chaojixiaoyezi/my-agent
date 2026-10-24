# LLM: 默认读写保持原no-follow链；显式snapshot有限废弃安全替换或零链接copyout，绝不接收0link正文，原FD/元数据/目录/清理硬门保持。
# 模块用途: 统一受管FD读写；规范Home可显式rooted创建私有目录/排他种子，零链接观察仅耗原重读预算，默认链与严格附件能力不降级。
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
    # LLM: transient_open_race 仅在 before/after 均为单链接普通文件且仅 inode 发生原子替换时置真，不凭文案判断。
    # 函数用途: 构造受管路径异常并携带结构化瞬态替换标记。
    def __init__(self, message: str = "", *, transient_open_race: bool = False) -> None:
        super().__init__(message)
        self.transient_open_race = bool(transient_open_race)


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


# LLM: 严格只读打开复用原逐段目录和普通单链接文件校验；调用方拥有返回 fd，权限仍由原业务合同检查。
# 函数用途: 安全打开需要 seek/fstat/分页读取的普通文件，平台能力不足不降级跟随路径。
def open_readonly_file_beneath(root: str | Path, relative_parts: tuple[str, ...]) -> int:
    _validate_relative_parts(relative_parts)
    parent_fd = open_directory_beneath(root, relative_parts[:-1], require_dir_fd=True)
    try:
        return _openat_file(parent_fd, relative_parts[-1], os.O_RDONLY, 0o600)
    finally:
        os.close(parent_fd)


# LLM: 默认0保持原读链；显式snapshot仅strict原parent FD有限重读安全regular替换，硬路径错误不重读，预算仍限制每次实际读取。
# 函数用途: 有界读取宿主管理的普通二进制文件，不跟随链接，也不创建目录。
def read_bytes_beneath(
    root: str | Path,
    relative_parts: tuple[str, ...],
    *,
    max_bytes: int | None = None,
    require_dir_fd: bool = False,
    snapshot_retries: int = 0,
) -> bytes | None:
    _validate_relative_parts(relative_parts)
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 0):
        raise ValueError("managed read limit must be a nonnegative integer")
    if type(snapshot_retries) is not int or not 0 <= snapshot_retries <= 2:
        raise ValueError("managed snapshot retries must be an integer from zero to two")
    if snapshot_retries:
        return _read_snapshot_beneath(root, relative_parts, max_bytes, snapshot_retries)
    if not _supports_dir_fd():
        if require_dir_fd:
            raise NoFollowPathError("strict no-follow reading is unavailable")
        return _read_bytes_portable(root, relative_parts, max_bytes)
    try:
        parent_fd = open_directory_beneath(root, relative_parts[:-1], require_dir_fd=require_dir_fd)
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


# LLM: 仅显式snapshot实测A/B安全叶替换可生成；循环按真实类型判断，不能解析文案或吞硬路径/权限错误。
# 类用途: 标记本次原目录链内可有限重读的叶身份变化，不授内容权限或写者认证。
class _SnapshotLeafChanged(NoFollowPathError):
    # LLM: reason只取本模块A/B固定分支，cleanup_failed只限制后续重读，不持久化或变成新状态账。
    # 函数用途: 保存本次叶变化及清理是否仍可靠，避免清理失败后继续读。
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason
        self.cleanup_failed = False


# LLM: 只清本次暂存FD且逐个尝试一次；显式本链primary原对象优先，不从调用方外层异常猜，清理失败不得重读/成功。
# 函数用途: 归还snapshot的目录/叶FD，不让次生关闭错误覆盖真实首因。
def _close_snapshot_fds(descriptors, primary=None):
    first = None
    for descriptor in reversed(descriptors):
        try:
            os.close(descriptor)
        except OSError as error:
            if isinstance(primary, _SnapshotLeafChanged):
                primary.cleanup_failed = True
            if first is None:
                first = error
    if primary is None and first is not None:
        raise first


# LLM: 逐prefix复用原strict目录入口；只初次最终parent FD用于叶读，其它FD只是身份观察且立即归还。
# 函数用途: 捕获本次原root和完整父链身份，初始root缺失可空，已经建立绑定后缺失硬拒。
def _snapshot_parent(root, parts):
    bindings, descriptors = [], []
    try:
        for length in range(len(parts)):
            try:
                descriptor = open_directory_beneath(root, parts[:length], require_dir_fd=True)
            except FileNotFoundError:
                if not bindings:
                    return None, ()
                raise
            descriptors.append(descriptor)
            metadata = os.fstat(descriptor)
            bindings.append((metadata.st_dev, metadata.st_ino))
    except BaseException as error:
        _close_snapshot_fds(descriptors, error)
        raise
    parent_fd = descriptors.pop()
    try:
        _close_snapshot_fds(descriptors)
    except BaseException as error:
        _close_snapshot_fds([parent_fd], error)
        raise
    return parent_fd, tuple(bindings)


# LLM: 验证FD不成为新锚；每个prefix必须仍等于本次初始身份，根/中间/parent任何缺失、链接或更换立即硬拒。
# 函数用途: 在尝试前、候选后及返回前核完整原目录链，不能只核旧parent FD仍可用。
def _verify_snapshot_parents(root, parts, bindings):
    for length, identity in enumerate(bindings):
        descriptor = open_directory_beneath(root, parts[:length], require_dir_fd=True)
        try:
            metadata = os.fstat(descriptor)
            if (metadata.st_dev, metadata.st_ino) != identity:
                raise NoFollowPathError("managed snapshot directory changed")
        except BaseException as error:
            _close_snapshot_fds([descriptor], error)
            raise
        _close_snapshot_fds([descriptor])


# LLM: 仅在current为regular0且原类型门已通过时调用；保原A/B身份拒绝、已观察元数据和准确原FD脱链证明，只抛原typed废弃，不能授权任何bytes。
# 函数用途: 将stat copyout时已脱链的整轮样本丢弃；复采原FD，不接受旁名仍附着、多版本或原位变化，fstat异常交原清理链。
def _discard_zero_link_snapshot(before, opened, current, descriptor):
    if opened.st_nlink == 0 and not _same_identity(before, opened):
        raise NoFollowPathError("managed snapshot detached identity is unknown")
    if not _same_identity(before, opened) and not _same_identity(opened, current):
        raise NoFollowPathError("managed snapshot leaf changed again")
    fields = ("st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
    if _same_identity(before, opened):
        stable_fields = fields if opened.st_nlink == 1 else fields[:-1]
        if any(getattr(before, field) != getattr(opened, field) for field in stable_fields):
            raise NoFollowPathError("managed snapshot same-inode content changed")
    if _same_identity(opened, current):
        if any(getattr(opened, field) != getattr(current, field) for field in fields[:-1]):
            raise NoFollowPathError("managed snapshot same-inode content changed")
    fresh = os.fstat(descriptor)
    if (not stat.S_ISREG(fresh.st_mode) or fresh.st_nlink != 0 or not _same_identity(opened, fresh)
            or any(getattr(opened, field) != getattr(fresh, field) for field in fields[:-1])):
        raise NoFollowPathError("managed snapshot current leaf differs")
    raise _SnapshotLeafChanged("regular_leaf_detached")


# LLM: current1保原A/B语义；current0仅经原危险门及准确原FD核验废弃整轮，附着阶段四字段漂移仍硬拒；同步snapshot回归。
# 函数用途: 核同一parent内before/FD/current，有限丢弃已脱链观察，不把零链接正文、原位改动或旁名旧FD当安全版本。
def _snapshot_leaf_identity(parent_fd, name, before, opened, descriptor):
    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (not stat.S_ISREG(current.st_mode) or current.st_nlink not in (0, 1)
            or not stat.S_ISREG(opened.st_mode) or opened.st_nlink not in (0, 1)):
        raise NoFollowPathError("managed snapshot leaf is not regular single-link")
    if current.st_nlink == 0:
        _discard_zero_link_snapshot(before, opened, current, descriptor)
    if opened.st_nlink == 0:
        if _same_identity(before, opened) and not _same_identity(before, current):
            raise _SnapshotLeafChanged("regular_leaf_detached")
        raise NoFollowPathError("managed snapshot detached identity is unknown")
    if not _same_identity(before, opened):
        if _same_identity(opened, current):
            raise _SnapshotLeafChanged("regular_leaf_replaced")
        raise NoFollowPathError("managed snapshot leaf changed again")
    fields = ("st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, field) != getattr(opened, field) for field in fields):
        raise NoFollowPathError("managed snapshot same-inode content changed")
    if not _same_identity(opened, current):
        fresh = os.fstat(descriptor)
        if (stat.S_ISREG(fresh.st_mode) and fresh.st_nlink == 0 and _same_identity(opened, fresh)
                and all(getattr(opened, field) == getattr(fresh, field) for field in fields[:-1])):
            raise _SnapshotLeafChanged("regular_leaf_detached")
        raise NoFollowPathError("managed snapshot current leaf differs")
    if any(getattr(opened, field) != getattr(current, field) for field in fields):
        raise NoFollowPathError("managed snapshot same-inode content changed")


# LLM: 初始regular0在open前丢弃，读前后0link均不授bytes；同一原FD有界读/复采及清理保持，初次缺叶可None，重读后missing硬拒。
# 函数用途: 尝试取得完整单链接版本；零链接copyout只耗既有预算，未打开时不读正文，已打开的所有出口归还原叶FD。
def _read_snapshot_leaf(parent_fd, name, max_bytes, allow_missing):
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        if allow_missing:
            return None
        raise
    if not stat.S_ISREG(before.st_mode) or before.st_nlink not in (0, 1):
        raise NoFollowPathError("managed snapshot leaf is not regular single-link")
    if before.st_nlink == 0:
        raise _SnapshotLeafChanged("regular_leaf_detached")
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(name, flags, 0o600, dir_fd=parent_fd)
    try:
        _snapshot_leaf_identity(parent_fd, name, before, os.fstat(descriptor), descriptor)
        content = _read_descriptor(descriptor, max_bytes)
        _snapshot_leaf_identity(parent_fd, name, before, os.fstat(descriptor), descriptor)
    except BaseException as error:
        _close_snapshot_fds([descriptor], error)
        raise
    _close_snapshot_fds([descriptor])
    return content


# LLM: opt-in强制原strict能力且总尝试<=3；只捕原typed的A/B或零链接废弃，硬错/清理优先，原parent FD与父链不刷新，无sleep/portable或重捕授权。
# 函数用途: 有限重读原子替换窗口，同时保原目录链、预算、None语义及首异常，失败仍交上层保守拒绝。
def _read_snapshot_beneath(root, parts, max_bytes, retries):
    parent_fd, bindings = _snapshot_parent(root, parts)
    if parent_fd is None:
        return None
    try:
        for attempt in range(retries + 1):
            _verify_snapshot_parents(root, parts, bindings)
            try:
                content = _read_snapshot_leaf(parent_fd, parts[-1], max_bytes, attempt == 0)
            except _SnapshotLeafChanged as error:
                if error.cleanup_failed:
                    raise
                _verify_snapshot_parents(root, parts, bindings)
                if attempt == retries:
                    raise
                continue
            _verify_snapshot_parents(root, parts, bindings)
            break
    except BaseException as error:
        _close_snapshot_fds([parent_fd], error)
        raise
    _close_snapshot_fds([parent_fd])
    return content


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


# LLM: 私有写/私有锁共用的锚点规则（pdp 2026-10-04，3a 裁定）：最近的已存在祖先本身是符号链接时
#   （用户工作区/项目目录常指到外置盘），允许跟随一次到它的真实目录，再从那里往下新建缺失段——
#   它已存在，权限照旧一位不动；新建段仍逐段 no-follow（段内再出现符号链接一律拒绝）。
#   目标不是目录（断链、指向文件）时保持 fail-closed，不让私有写落到不可预期的位置。
# 函数用途: 把“最近已存在的符号链接锚点”解析成真实目录；非链接原样返回，目标不可用抛 NoFollowPathError。
def resolve_existing_symlink_anchor(anchor: str | Path) -> Path:
    anchor_path = Path(anchor)
    if not anchor_path.is_symlink():
        return anchor_path
    resolved = Path(os.path.realpath(anchor_path))
    if not resolved.is_dir():
        raise NoFollowPathError("managed symlink anchor target is not a directory")
    return resolved


# LLM: 私有写/私有锁共用的“最近已存在祖先”拆分（pbfix 2026-10-04：原来 6 处各抄一遍同样的循环）：
#   从路径向上找到第一个存在（或为符号链接）的段，返回它和其余缺失段；调用方决定新建还是拒绝。
# 函数用途: 把路径拆成“已存在的最近祖先 + 其余缺失段（相对该祖先，按路径顺序）”。
def split_existing_anchor(path: str | Path) -> tuple[Path, tuple[str, ...]]:
    anchor = Path(path)
    parts: tuple[str, ...] = ()
    while not anchor.exists() and not anchor.is_symlink():
        parts = (anchor.name, *parts)
        anchor = anchor.parent
    return anchor, parts


# LLM: 私有写与私有锁统一的“确保目录存在”（原 json_io._ensure_private_dir 公开版，pbfix）：缺失段经
#   no-follow 原语逐段按 0700 新建——中间各级同样是 0700（mkdir(parents=True, mode=0o700) 只保证最后一级）；
#   已存在的目录一律不动；最近已存在祖先是有效符号链接时跟随一次，断链/指向文件保持拒绝。Python 不支持 dir_fd 的平台
#   （Windows：os.open 打不开目录）与本模块其它原语一样走 _portable_directory（逐级 lstat，拒绝链接与非目录），不再
#   打开目录描述符（CI 修复，2026-10-07：Windows 上子代理建任务目录全部 PermissionError）。
# LLM: canonical_root仅由规范Home初始化显式传入；此分支从原根逐段no-follow，不接受内部链接；默认锚点规则不变。
# 函数用途: 缺失目录按0700逐级新建，既有权限不动；显式规范根时拒绝受管子目录链接。
def ensure_private_dir(directory: str | Path, *, canonical_root: str | Path | None = None) -> None:
    if canonical_root is not None:
        root = Path(os.path.abspath(canonical_root))
        parts = Path(os.path.abspath(directory)).relative_to(root).parts
        if not _supports_dir_fd():
            _portable_directory(root, parts, create=True)
        else:
            os.close(open_directory_beneath(root, parts, create=True))
        return
    anchor, parts = split_existing_anchor(Path(os.path.abspath(directory)))
    if not parts:
        return
    root = resolve_existing_symlink_anchor(anchor)
    if not _supports_dir_fd():
        _portable_directory(root, parts, create=True)
        return
    os.close(open_directory_beneath(root, parts, create=True))


# LLM: 只供宿主规范种子创建；沿已开parent FD排他新建0600普通单链接叶，已有普通文件不打开不覆盖，危险叶拒绝。
#   portable沿原目录校验但不承诺POSIX父链竞态强度；FileExistsError绝不回退打开已有文件，不取得锁或写锁账。
# 函数用途: 返回一个刚创建的私有文件描述符，由调用方写内容并关闭；并发创建者已成功则原样抛已存在。
def create_private_file_beneath(root: str | Path, relative_parts: tuple[str, ...]) -> int:
    _validate_relative_parts(relative_parts)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if not _supports_dir_fd():
        target = _portable_path(root, relative_parts, create=True)
        try:
            metadata = target.lstat()
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise NoFollowPathError("managed seed is not a regular single-link file")
        return os.open(target, flags | getattr(os, "O_NOFOLLOW", 0), 0o600)
    parent_fd = open_directory_beneath(root, relative_parts[:-1], create=True)
    try:
        return _openat_file(parent_fd, relative_parts[-1], flags, 0o600)
    finally:
        os.close(parent_fd)


# LLM: 永久锁仅允许单链接普通文件；先排他创建，已存在再打开，避免并发非排他创建的歧义；调用方关闭 fd。
#   锁只动自己新建的东西：缺失目录经 no-follow 原语按 0700 建（mkdir 的 mode 不受 umask 放宽），锁文件 0600；
#   已存在的目录——不管是宿主其它模块建的还是 owner 维护的符号链接布局——一律不改权限。同机他用户抢锁要先能
#   打开 0600 的锁文件，目录权限不是防线；收紧已存在目录会破坏上下文快照“链接所在级和下级目录不动”的口径。
#   锚点本身是符号链接时允许跟随一次（见 resolve_existing_symlink_anchor，与私有写同一规则）。
#   exclusive=True 时走"先到先得"语义：已存在就抛 FileExistsError（不打开、不改权限），给把锁文件当互斥体的
#   调用方（gateway scoped lock）；默认 False 保持原 flock 语义（已存在则打开复用同一 inode）。
# 函数用途: 安全创建私有锁目录并打开不截断的锁文件，避免目录预检后再次按路径跟随链接。
def open_private_lock_beneath(root: str | Path, relative_parts: tuple[str, ...], *, exclusive: bool = False) -> int:
    _validate_relative_parts(relative_parts)
    root = resolve_existing_symlink_anchor(root)
    if not _supports_dir_fd():
        return _open_private_lock_portable(root, relative_parts, exclusive=exclusive)
    parent_fd = open_directory_beneath(root, relative_parts[:-1], create=True)
    try:
        return _open_lock_leaf(parent_fd, relative_parts[-1], exclusive=exclusive)
    finally:
        os.close(parent_fd)


# LLM: 排他创建永远是第一步（原子先到先得）；只有非排他模式才在 FileExistsError 后打开已存在的锁复用同一 inode。
#   排他模式不触碰已存在的文件（不打开、不改权限），把"已存在"如实抛给调用方当互斥信号。
# 函数用途: 在父目录 fd 下创建（或按模式打开）私有锁叶子，返回描述符。
def _open_lock_leaf(parent_fd: int, name: str, *, exclusive: bool) -> int:
    try:
        return _openat_file(parent_fd, name, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if exclusive:
            raise
        return _openat_file(parent_fd, name, os.O_RDWR, 0o600)


# LLM: 存量锁可能是在本修复之前按 umask 建的 0644；打开分支不重建 inode，故必须显式收紧，
#   否则那批世界可读的锁会一直留着。fchmod 只要求属主、不要求持锁，对正被别人 flock 的锁也安全。
#   只收紧不放松（已比 0600 更严就不动），chmod 失败只放弃收紧，不让拿锁失败。只动锁文件本身；
#   目录权限由 open_private_lock_beneath 负责（新建 0700、已存在的目录一律不动）。
# 函数用途: 打开一把私有锁并把已存在锁文件的权限收紧到仅本人可读写，返回可 flock 的描述符。
def open_private_lock_beneath_tightened(root: str | Path, relative_parts: tuple[str, ...]) -> int:
    descriptor = open_private_lock_beneath(root, relative_parts)
    try:
        # Windows 的 Python 3.12 没有 os.fchmod：没有就跳过自愈（该平台没有 POSIX 权限位语义），
        # 不影响其它平台把存量 0644 锁收紧到 0600。
        if hasattr(os, "fchmod") and stat.S_IMODE(os.fstat(descriptor).st_mode) & 0o077:
            os.fchmod(descriptor, 0o600)
    except OSError:
        pass
    return descriptor


# LLM: 缺少 dir_fd 时仍检查父链、锁叶子与打开后身份；无法承诺 POSIX 的目录替换竞态强度。
#   exclusive=True 时不落进"已存在就打开"分支：把 FileExistsError 原样抛给调用方当互斥信号。
# 函数用途: 在 portable 平台打开永久锁，拒绝已有链接或特殊文件，不截断原锁。
def _open_private_lock_portable(root: str | Path, parts: tuple[str, ...], *, exclusive: bool = False) -> int:
    target = _portable_path(root, parts, create=True)
    try:
        before = target.lstat()
    except FileNotFoundError:
        before = None
    if before is not None and (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1):
        raise NoFollowPathError("managed lock is not a regular file")
    if exclusive and before is not None:
        raise FileExistsError(str(target))
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


# LLM: 每段相对前一 fd 打开，调用方关闭最终 fd；严格读取模式要求实际 no-follow/dirfd 能力，不能用 portable 分支替代。
# 函数用途: 从受信根逐层打开内部目录，可选安全创建固定目录段，或要求严格只读打开能力。
def open_directory_beneath(
    root: str | Path,
    relative_parts: tuple[str, ...],
    *,
    create: bool = False,
    mode: int = 0o700,
    require_dir_fd: bool = False,
) -> int:
    _validate_relative_parts(relative_parts, allow_empty=True)
    if require_dir_fd and (not _supports_dir_fd() or not getattr(os, "O_NOFOLLOW", 0) or not getattr(os, "O_DIRECTORY", 0)):
        raise NoFollowPathError("strict no-follow opening is unavailable")
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


# LLM: 仅在 open 到 fstat 之间旧普通文件被同目录新单链接普通文件原子替换（旧 fd st_nlink==0 且当前路径为不同 inode 普通文件）时返回真。
# 函数用途: 区分 open/fstat 窗口的合法原子替换与被直接删除或硬链篡改。
def _is_transient_unlinked_open_race(
    parent_fd: int,
    name: str,
    before: os.stat_result | None,
    after: os.stat_result,
) -> bool:
    if (
        before is None
        or not stat.S_ISREG(after.st_mode)
        or int(after.st_nlink) != 0
        or not _same_identity(before, after)
    ):
        return False
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return False
    return (
        not stat.S_ISLNK(current.st_mode)
        and stat.S_ISREG(current.st_mode)
        and int(current.st_nlink) == 1
        and not _same_identity(before, current)
    )


# LLM: 只接受单链接普通文件；no-follow 与 inode 比较阻止替换，非阻塞打开避免预检后换成 FIFO 使线程卡住。
#   打开与 fstat 之间若被同目录 os.replace 原子替换，原普通文件 inode 的 st_nlink 会降为 0，标记 transient_open_race=True。
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
    if not stat.S_ISREG(after.st_mode) or int(after.st_nlink) != 1:
        os.close(descriptor)
        transient = _is_transient_unlinked_open_race(parent_fd, name, before, after)
        raise NoFollowPathError("managed file changed during open", transient_open_race=transient)
    if before is not None and not _same_identity(before, after):
        os.close(descriptor)
        raise NoFollowPathError("managed file changed during open", transient_open_race=True)
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
    "ensure_private_dir",
    "create_private_file_beneath",
    "resolve_existing_symlink_anchor",
    "split_existing_anchor",
    "open_private_lock_beneath",
    "list_names_beneath",
    "open_directory_beneath",
    "open_readonly_file_beneath",
    "read_text_beneath",
    "read_bytes_beneath",
    "regular_file_exists_beneath",
    "unlink_file_beneath",
    "write_text_atomic_beneath",
    "write_bytes_atomic_beneath",
]
