
from __future__ import annotations

"""Gateway daemon metadata helpers shared by PID, lock, and status modules."""

import ctypes
import functools
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from ..common.nofollow_fs import (
    ensure_private_dir,
    open_private_lock_beneath_tightened,
    split_existing_anchor,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows import guard.
    fcntl = None

# Linux/容器的稳定主机身份来源，按顺序取第一份非空内容。
_MACHINE_ID_PATHS = (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id"))
# macOS gethostuuid 的等待上限秒数（timespec.tv_sec）；等不到按“取不到”处理，退回主机名。
_HOST_UUID_WAIT_SECONDS = 5


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_process_start_time(pid: int) -> int | str | None:
    """返回进程出生时间；只用于**同一进程的前后比较**（相等即同代），不是可换算的时间戳。

    Linux 走 /proc/<pid>/stat 第 22 字段（clock ticks，整数）；没有 /proc 的平台（macOS 等）
    走 `ps -o lstart=`，返回稳定字符串。两者都取不到时返回 None，调用方必须退化为"无法证明代际"。
    """
    if sys.platform == "win32":
        return None
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        # Field 22 in /proc/<pid>/stat is process start time (clock ticks).
        return int(stat_path.read_text().split()[21])
    except (FileNotFoundError, IndexError, PermissionError, ValueError, OSError):
        pass
    # 非 Linux：用 ps 拿同一进程稳定的出生时间字符串（不解析成时间戳，只做相等比较）。
    try:
        completed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(int(pid))],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return value or None


# LLM: 后台租约只能在“同一进程域且旧 PID 身份已死”时提前接管；machine-id 还要叠加
# Linux PID namespace，避免多个 Kubernetes Pod 共享 machine-id 后互相把不可见 PID 误判为已死。
#   一个进程里只算一次并缓存：pid 记录、信号停止请求、主循环和租约核验拿到的都是同一个值。macOS 没有 machine-id 时
#   原来退回主机名，换网络后主机名会变（step17a 切换时变成 anonymous），本进程前后身份对不上，SIGTERM 写的停止请求被主循环
#   一直忽略；现在 macOS 先用硬件 UUID，都取不到才用主机名。跨进程：有稳定来源时主机名变化不影响；只剩主机名时，
#   主机名变后新起的进程会算出另一个值，process_identity_is_live 对旧记录给 None（无法判断、按 TTL 兜底），不会误判已死。
#   只读系统事实，结果是摘要，原始 machine-id/UUID 不落盘。改动同步 test_gateway_host_identity.py。
# 函数用途: 生成当前主机/容器进程域标识，供 PID+start_time 租约身份判定复用。
def process_host_id() -> str:
    return _cached_process_host_id()


# LLM: lru_cache 只缓存本进程这一个值；稳定来源取不到才用主机名，Linux 再叠加 PID namespace。组成方式与改前相同，
#   所以 Linux 与容器上的取值不变；macOS 从主机名换成硬件 UUID，升级后上一版写的记录按“另一主机”处理（只是等 TTL）。
# 函数用途: 计算并缓存本进程的主机身份摘要。
@functools.lru_cache(maxsize=1)
def _cached_process_host_id() -> str:
    identity_parts = [_stable_host_source() or socket.gethostname().strip() or "unknown-host"]
    try:
        identity_parts.append(os.readlink("/proc/self/ns/pid"))
    except OSError:
        pass
    raw = "\n".join(identity_parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


# LLM: 稳定来源只有两种：machine-id 文件（Linux/容器），macOS 的硬件 UUID；都没有返回空串，由调用方退回主机名。
#   platform 只给测试指定平台用，产品调用不传。
# 函数用途: 取当前主机不随网络变化的身份原文。
def _stable_host_source(platform: str = sys.platform) -> str:
    for path in _MACHINE_ID_PATHS:
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    return _macos_platform_uuid() if platform == "darwin" else ""


# LLM: ctypes 版 struct timespec（两个 long），只给 gethostuuid 传等待上限；字段顺序和类型不能改。
# 类用途: gethostuuid 的等待时长参数。
class _Timespec(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]


# LLM: 用 libc 的 gethostuuid() 取硬件 UUID（与 ioreg 显示的 IOPlatformUUID 是同一个值），不起子进程：首次取身份可能
#   发生在任何代码路径里，起子进程会撞上调用方或测试对 subprocess 的替换。符号不存在或调用失败都返回空串，不抛异常。
# 函数用途: 取 macOS 硬件 UUID。
def _macos_platform_uuid() -> str:
    try:
        gethostuuid = ctypes.CDLL(None).gethostuuid
    except (OSError, AttributeError):
        return ""
    gethostuuid.argtypes = [ctypes.c_char_p, ctypes.POINTER(_Timespec)]
    gethostuuid.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(16)
    if gethostuuid(buffer, ctypes.byref(_Timespec(_HOST_UUID_WAIT_SECONDS, 0))) != 0:
        return ""
    return str(uuid.UUID(bytes=buffer.raw[:16])).upper()


# LLM: 进程身份由 host_id+pid+start_time 组成，避免只凭 PID 在复用后误认旧执行者仍存活。
# 函数用途: 为当前或指定进程构造可持久化、可核验的运行身份。
def build_process_identity(pid: int | None = None) -> dict[str, object]:
    process_id = os.getpid() if pid is None else int(pid)
    return {
        "host_id": process_host_id(),
        "pid": process_id,
        "start_time": _get_process_start_time(process_id),
    }


# LLM: 返回 None 表示跨进程域或字段不足，调用方必须按 TTL fail-safe；只有同域且能证明
# PID 已死或 start_time 不符时才返回 False，绝不能把“看不见”当成“已死”。
# 函数用途: 核验一个持久化进程身份现在是否仍代表同一个活进程。
def process_identity_is_live(identity: object) -> bool | None:
    if not isinstance(identity, dict):
        return None
    host_id = str(identity.get("host_id") or "").strip()
    if not host_id or host_id != process_host_id():
        return None
    try:
        pid = int(identity.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    from .process_control import is_pid_alive

    if not is_pid_alive(pid):
        return False
    recorded_start = identity.get("start_time")
    current_start = _get_process_start_time(pid)
    if recorded_start is not None and current_start is not None and recorded_start != current_start:
        return False
    return True


def _scope_hash(identity: str) -> str:
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def _build_pid_record() -> dict:
    identity = build_process_identity()
    return {
        **identity,
        "kind": "my-agent-gateway",
        "argv": list(sys.argv),
        "updated_at": _utc_now_iso(),
    }


def _read_json_file(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


# LLM: gateway_parts 的宿主私有 JSON 原子写（PID 记录 / 运行时状态 / 停止请求 / scoped lock 心跳刷新）。
#   临时文件按 0600 排他新建再 os.replace，替换后的正式文件就是 0600，不随 umask 变成 0644
#   （sclk2 后 9b 复核：首次抢锁 0600，同进程再抢走这里刷新就变回 0644）；缺失的上级目录按 0700 新建、
#   已存在的不动（与 pbfix 的 ensure_private_dir 同口径）。已在 _flocked_sidecar 锁内，不再套 json_io 的锁。
#   改动要同步 test_scoped_lock_private_permissions.py 的刷新用例。
# 函数用途: 原子地写一份宿主私有 JSON 记录，读取方只会看到完整的旧记录或新记录，文件权限保持 0600。
def _write_json_file(path: Path, payload: dict) -> None:
    # H8:原 path.write_text 非原子,写一半崩溃/磁盘满会留半截 JSON。daemon_metadata
    # 写的是 PID 记录 / status / scoped-lock 心跳——半截锁文件会被
    # scoped_locks._read_lock_record_report 判 lock_load_error,acquire_scoped_lock
    # 遇 load_error 直接拒绝接管,gateway 身份永久卡死。改 temp+os.replace 原子落盘:
    # 读取方要么看到旧的完整记录、要么看到新的完整记录,绝无半截。同名 sidecar 上加
    # fcntl.flock(LOCK_EX) 串行化并发写,与 io/jsonl.py、common/json_io.py 同一手法。
    ensure_private_dir(path.parent)
    content = json.dumps(payload)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    with _flocked_sidecar(path):
        try:
            _write_private_tmp(tmp, content)
            _replace_with_retry(tmp, path)
        finally:
            _unlink_quiet(tmp)


# LLM: 临时文件用 O_CREAT|O_EXCL 按 0600 新建（名字带 uuid，已存在就是异常情况，直接抛错而不是覆盖或跟随链接）；
#   只给 _write_json_file 用，调用方负责替换和清理。
# 函数用途: 新建一个只有属主可读写的临时文件并写入内容。
def _write_private_tmp(tmp: Path, content: str) -> None:
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)


# LLM: 串行化 sidecar 与其余锁同口径私有：文件 0600、缺失目录按 0700 新建、已存在的目录一律不动、
#   不跟随符号链接（锁叶子是符号链接或硬链接时抛 NoFollowPathError，不再静默跟随）。原来用
#   lock_path.open("a+") 会按 umask 落成 0644；调用方是 _write_json_file（PID 记录 / 运行时状态 /
#   scoped lock 心跳）与 daemon_control 的停止请求清理，路径都落在宿主自己的运行目录与
#   XDG_STATE_HOME/my-agent/locks，新建目录 0700 不碰用户工作区。
# 函数用途: 用私有写锁描述符串行化一个 sidecar 文件的并发读者与写者，退出时释放并关闭。
@contextmanager
def _flocked_sidecar(path: Path):
    # 串行化 sidecar 后缀【刻意】用 .wlock 而非 .lock:scoped_locks 锁目录里
    # release_all_scoped_locks 用 glob("*.lock") 扫描,若写锁 sidecar 也叫 .lock
    # 会被误当成一把"空锁记录"扫到(虽因 load_error 判定不会误删,但污染目录)。
    lock_path = path.with_name(path.name + ".wlock")
    descriptor = _open_private_wlock_descriptor(lock_path)
    try:
        _flock_descriptor(descriptor, exclusive=True)
        try:
            yield
        finally:
            _flock_descriptor(descriptor, exclusive=False)
    finally:
        os.close(descriptor)


# LLM: 写锁 sidecar 的路径由调用方给出（宿主运行目录 / XDG 状态目录），这里按“已存在的最近祖先 +
#   其余缺失段”（nofollow_fs.split_existing_anchor）交给 no-follow 原语，缺失目录逐级按 0700 创建、已存在的目录一律不动。
# 函数用途: 打开一个私有写锁描述符，顺带把已存在的宽权限 wlock 收紧到 0600。
def _open_private_wlock_descriptor(lock_path: Path) -> int:
    lock_path = Path(os.path.abspath(lock_path))
    anchor, missing = split_existing_anchor(lock_path.parent)
    return open_private_lock_beneath_tightened(anchor, (*missing, lock_path.name))


# LLM: 不支持 OS 锁的平台沿原告警策略；支持时加锁与解锁共用同一个 fd 上的 flock，阻塞语义不变。
# 函数用途: 在一个写锁描述符上加/解排他 flock。
def _flock_descriptor(descriptor: int, *, exclusive: bool) -> None:
    if fcntl is None:
        if exclusive:
            from ..common.file_lock_support import warn_file_lock_unavailable_once
            warn_file_lock_unavailable_once()
        return
    fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_UN)


def _unlink_quiet(tmp: Path) -> None:
    try:
        tmp.unlink()
    except OSError:
        pass


def _replace_with_retry(tmp: Path, path: Path) -> None:
    last_error: OSError | None = None
    for attempt in range(8):
        try:
            tmp.replace(path)
            return
        except PermissionError as exc:  # Windows: rename over open target may transiently fail.
            last_error = exc
            time.sleep(0.01 * (attempt + 1))
    if last_error is not None:
        raise last_error
