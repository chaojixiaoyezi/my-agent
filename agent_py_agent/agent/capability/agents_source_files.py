# LLM: 来源控制件共用严格dir_fd；可选回调仅包文件创建提交，锁等待不占提交回调，不新增授权账。
# 模块用途: 固定每段目录/最终文件身份，从同一FD读写，并安全提交小型owner配置。
import json
import os
import secrets
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path

from ..common.nofollow_fs import (
    NoFollowPathError,
    _openat_directory,
    _openat_file,
    open_directory_beneath,
)
from ._agents_contract_fields import AgentsContractError

# 本地来源原字节最大2MiB；读取同FD时多取一字节判断超限，不静默裁剪正文。
AGENTS_SOURCE_MAX_BYTES = 2 * 1024 * 1024
# 来源配置和last-good控制JSON最多512KiB，拒绝损坏记录造成无界解析。
AGENTS_CONTROL_MAX_BYTES = 512 * 1024


# LLM: 所有身份均取实际FD，哈希不含正文；只比较inode与设备，不将mtime变动当配置变化。
# 函数用途: 给整个目录链和最终文件形成固定身份摘要。
def _identity_digest(records: list[tuple[int, int]]) -> str:
    return sha256(json.dumps(records, separators=(",", ":")).encode()).hexdigest()


# LLM: 从根逐段nofollow打开；create排他创建，expected_parent绑定预先冻结父FD，核验后仍以该同一目录FD开叶子。
# 函数用途: 把绝对来源路径变成已核验FD及整条身份链，调用方不再普通open。
@contextmanager
def source_descriptor(path: Path, *, writable: bool = False, create: bool = False, expected_parent: int | None = None):
    parent = descriptor = None
    try:
        parent = open_directory_beneath(path.anchor, (), require_dir_fd=True)
        value = os.fstat(parent)
        chain = [(value.st_dev, value.st_ino)]
        for part in path.parts[1:-1]:
            child = _openat_directory(parent, part)
            os.close(parent)
            parent = child
            value = os.fstat(parent)
            chain.append((value.st_dev, value.st_ino))
        _verify_parent_descriptor(parent, expected_parent)
        descriptor = _openat_file(parent, path.name, _source_open_flags(writable, create), 0o600)
        value = os.fstat(descriptor)
        chain.append((value.st_dev, value.st_ino))
    except FileNotFoundError:
        _close_source_descriptors(descriptor, parent)
        raise
    except (OSError, NoFollowPathError, NotImplementedError) as error:
        _close_source_descriptors(descriptor, parent)
        raise AgentsContractError("AGENTS_SOURCE_UNSAFE_PATH", "locator") from error
    # 只有逐级打开失败才算路径不安全；调用方 with 块里的错误（如正文被外部改写）原样上抛，
    # 不能被改写成 UNSAFE_PATH 掩盖真实结构化错误码。
    try:
        yield descriptor, _identity_digest(chain)
    finally:
        _close_source_descriptors(descriptor, parent)


# LLM: 只关闭本次打开的两个FD；None表示该级尚未打开。
# 函数用途: 让打开失败和正常退出两条路径共用同一套关闭逻辑。
def _close_source_descriptors(descriptor, parent) -> None:
    if descriptor is not None:
        os.close(descriptor)
    if parent is not None:
        os.close(parent)


# LLM: 比较两个仍打开的目录FD，不按路径重开，不把字节相同当身份相同。
# 函数用途: 核对即将使用的父目录仍是事务冻结的目录。
def _verify_parent_descriptor(parent: int, expected_parent: int | None) -> None:
    if expected_parent is None:
        return
    expected, actual = os.fstat(expected_parent), os.fstat(parent)
    if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
        raise AgentsContractError("AGENTS_SOURCE_IDENTITY_CHANGED", "locator")


# LLM: 创建只允许可写排他方式；调用方仍须用nofollow原语消费flags。
# 函数用途: 集中表达来源文件打开模式，不混入目录遍历流程。
def _source_open_flags(writable: bool, create: bool) -> int:
    flags = (os.O_RDWR if writable else os.O_RDONLY) | getattr(os, "O_NONBLOCK", 0)
    if not create:
        return flags
    if not writable:
        raise ValueError("source creation requires writable descriptor")
    return flags | os.O_CREAT | os.O_EXCL


# LLM: fstat预检只是优化，实际读取仍以limit+1累计限制；没有无界read或换行规范化。
# 函数用途: 从已核验FD读取有限原始字节。
def read_descriptor(descriptor: int, limit: int) -> bytes:
    if os.fstat(descriptor).st_size > limit:
        raise AgentsContractError("AGENTS_SOURCE_TOO_LARGE", "content")
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks = []
    count = 0
    while count <= limit:
        chunk = os.read(descriptor, min(64 * 1024, limit + 1 - count))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        count += len(chunk)
    raise AgentsContractError("AGENTS_SOURCE_TOO_LARGE", "content")


# LLM: 配置记录的身份来自受信首次读取；配置来源每次读都核整条链，不能同字节换inode绕过。
# 函数用途: 读取本地来源或控制件，拒绝父目录/叶子的替换。
def read_source_bytes(path: Path, *, limit=AGENTS_SOURCE_MAX_BYTES, expected_identity=""):
    with source_descriptor(path) as (descriptor, identity):
        if expected_identity and identity != expected_identity:
            raise AgentsContractError("AGENTS_SOURCE_IDENTITY_CHANGED", "locator")
        return read_descriptor(descriptor, limit), identity


# LLM: 只从受控owner根创建persona；根身份及各段均严格核验，不因目录不存在走普通mkdir路径。
# 函数用途: 安全创建配置目录并返回可供后续at操作的父FD。
def open_control_directory(home: Path, *, create=False) -> int:
    descriptor = open_directory_beneath(home.anchor, home.parts[1:], require_dir_fd=True)
    try:
        if create:
            _create_persona_directory(descriptor)
        child = _openat_directory(descriptor, "persona")
        return child
    except (OSError, NoFollowPathError) as error:
        raise AgentsContractError("AGENTS_SOURCE_UNSAFE_PATH", "configuration") from error
    finally:
        os.close(descriptor)


# LLM: 只在已验证owner目录FD下创建persona；目录已存在仍由后续nofollow原语验证。
# 函数用途: 不使用普通路径创建来源控制目录。
def _create_persona_directory(descriptor: int) -> None:
    try:
        os.mkdir("persona", mode=0o700, dir_fd=descriptor)
    except FileExistsError:
        pass


# LLM: 序列化完成/有界后才落盘；临时文件及replace均在同一已核验目录FD，原件链接必须被拒。
# 函数用途: 原子提交owner小型配置或last-good，不产生逐轮多份缓存。
def write_control_json(home: Path, name: str, payload: dict) -> None:
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    if len(body) > AGENTS_CONTROL_MAX_BYTES:
        raise AgentsContractError("AGENTS_SOURCE_TOO_LARGE", "configuration")
    parent = open_control_directory(home, create=True)
    temporary = ".agents-" + secrets.token_hex(12)
    descriptor = None
    try:
        try:
            existing = _openat_file(parent, name, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0), 0o600)
        except FileNotFoundError:
            pass
        else:
            os.close(existing)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        _write_all(descriptor, body)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    except (OSError, NoFollowPathError) as error:
        raise AgentsContractError("AGENTS_SOURCE_UNSAFE_PATH", "configuration") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


# LLM: 保原owner EX→配置flock顺序；管理仅锁FD出生经原callback，不把等待/业务粗包C门，默认读者仍零控制件。
# 函数用途: 让配置和正文提交对所有读入口互斥，保留原锁文件安全核验。
@contextmanager
def source_configuration_lock(home: Path, *, file_commit=None):
    from .agents_source_read_lock import source_read_boundary
    with source_read_boundary(home, writing=True), _source_configuration_file_lock(home, file_commit=file_commit):
        yield


# LLM: 已持原owner EX，管理callback只执行有界目录/FD出生；flock在短门外，原nofollow与异常关闭不变。
# 函数用途: 串行配置CAS写入，不把线程重入当成跨进程锁。
@contextmanager
def _source_configuration_file_lock(home: Path, *, file_commit=None):
    parent = descriptor = None
    try:
        import fcntl
        # LLM: 只开本次原owner控制目录及锁FD，不等flock；失败关闭自己已经打开的FD。
        # 函数用途: 在管理原短门内完成锁文件出生。
        def open_lock():
            parent = open_control_directory(home, create=True)
            try:
                return parent, _openat_file(parent, "agents_source.lock", os.O_RDWR | os.O_CREAT, 0o600)
            except BaseException:
                os.close(parent)
                raise
        parent, descriptor = file_commit(open_lock, owner_home=home) if file_commit is not None else open_lock()
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    except (ImportError, OSError, NoFollowPathError) as error:
        raise AgentsContractError("AGENTS_SOURCE_UNSAFE_PATH", "configuration") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent is not None:
            os.close(parent)


# LLM: 已核验FD写前比对前像；失败只补偿精确已写状态，外部同inode编辑不得被回滚覆盖。
# 函数用途: 提交来源正文并有条件恢复，无法确认或恢复时由调用方保留版本恢复材料。
def replace_descriptor_text(descriptor: int, body: bytes, previous: bytes) -> None:
    if read_descriptor(descriptor, AGENTS_SOURCE_MAX_BYTES) != previous:
        raise OSError("AGENTS_SOURCE_CONTENT_MISMATCH")
    expected = previous
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            _write_all(descriptor, body)
        except _PartialWriteError as error:
            expected = body[:error.written] + previous[error.written:]
            raise
        expected = body + previous[len(body):]
        os.ftruncate(descriptor, len(body))
        expected = body
        os.fsync(descriptor)
    except OSError:
        _restore_descriptor_if_owned(descriptor, previous, expected)
        raise


# LLM: 实际字节与本次已知部分写完全相同才恢复；原字节仍在时不再次写，冲突只报错。
# 函数用途: 把失败补偿限制到自身写出的版本，不静默盖掉外部编辑。
def _restore_descriptor_if_owned(descriptor, previous, expected):
    actual = read_descriptor(descriptor, AGENTS_SOURCE_MAX_BYTES)
    if actual == previous:
        return
    if actual != expected:
        raise OSError("AGENTS_SOURCE_WRITE_RECOVERY_CONFLICT")
    os.lseek(descriptor, 0, os.SEEK_SET)
    _write_all(descriptor, previous)
    os.ftruncate(descriptor, len(previous))
    os.fsync(descriptor)


# LLM: 只记录内核已经返回成功的字节数；异常文本不携带来源正文。
# 类用途: 让部分写入错误仍能精确判断哪些字节由本事务写出。
class _PartialWriteError(OSError):
    # LLM: written由写循环给出，不能由路径当前内容倒推出创建或覆盖权。
    # 函数用途: 保存故障前实际写入进度。
    def __init__(self, written):
        super().__init__("source write incomplete")
        self.written = written


# LLM: os.write可以短写；零写必须报错，失败携带已成功写入量，不把不完整正文当成功。
# 函数用途: 将有限字节完整写入当前FD，并保留可核对的部分写进度。
def _write_all(descriptor: int, body: bytes) -> None:
    offset = 0
    try:
        while offset < len(body):
            offset += _write_once(descriptor, body[offset:])
    except OSError as error:
        raise _PartialWriteError(offset) from error


# LLM: 返回值只来自本次内核write；零进展仍为I/O失败，不伪造写入量。
# 函数用途: 完成一次可短写的正文写入并拒绝零字节进展。
def _write_once(descriptor, body):
    written = os.write(descriptor, body)
    if written <= 0:
        raise OSError("source write made no progress")
    return written
