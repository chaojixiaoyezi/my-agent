# LLM: Gateway 原文件队列的读写和只读统计入口；不得由展示计数派生准入权或任务终态。
# 模块用途: 保持请求文件格式、原子写入和轻量目录统计，不执行业务逻辑。
from __future__ import annotations

"""provides deterministic JSON-file queue IO helpers for gateway protocol files.

这个文件只管 gateway 文件队列的基础读写。
比如写请求文件、读响应文件、数队列里有多少 JSON、追加历史流水。
这里不执行业务，只保证文件格式和移动规则稳定。
"""

import errno
import hashlib
import json
import os
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..common.json_io import (
    append_private_jsonl_records,
    jsonl_lines,
    write_private_json_file_atomic_no_newline,
    write_private_json_file_atomic_no_newline_unlocked,
    write_private_text_file_atomic,
)
from ..common.logical_reference_ids import GATEWAY_REQUEST_ID_PREFIX
from ..common.nofollow_fs import (
    ensure_private_dir,
    open_private_lock_beneath_tightened,
    split_existing_anchor,
)
from ..runtime_errors import DataCorruptionError, runtime_error_report
from .paths import GatewayPaths

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows import guard.
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX import guard.
    msvcrt = None

_JSON_FILE_LOCKS: dict[str, threading.Lock] = {}
_JSON_FILE_LOCKS_GUARD = threading.Lock()
_GATEWAY_HISTORY_CACHE_LOCK = threading.Lock()
_GATEWAY_HISTORY_CACHE: dict[
    str,
    tuple[tuple[int, int], tuple[str, ...], dict[str, _GatewayHistoryIndexEntry]],
] = {}
GATEWAY_REQUEST_FINGERPRINT_SCHEMA = "gateway_request_fingerprint.v1"
_GATEWAY_REQUEST_FINGERPRINT_KEYS = (
    "kind",
    "goal",
    "prompt",
    "source",
    "user_id",
    "channel",
    "conversation",
    "metadata",
    "system_task",
    "inject",
    "prompt_files",
    "input_media",
    "save",
    "include_prompt",
    "resume_context",
    "client_capabilities",
)


# LLM: The process-local history index is only a performance cache. Canonical terminal archives
# remain authority, and file mtime/size invalidates the cache after any other process writes.
# 类用途: 缓存某个请求 ID 在历史投影中的完整正文、出现次数和冲突情况。
@dataclass(frozen=True)
class _GatewayHistoryIndexEntry:
    payload: dict
    count: int = 1
    conflict: bool = False


@dataclass(frozen=True)
class GatewayJsonReadReport:
    payload: dict
    load_error: dict | None = None
    # LLM: 取锁失败（身份核对重试耗尽、取锁被打断）是暂时竞争，不是数据损坏：单独置位，供调用方
    #   按“可重试的锁失败”处理；不读它、只看 load_error 的调用方行为保持原样。
    lock_busy: bool = False


# LLM: The request fingerprint covers authenticated owner, conversation, prompt, task, and all
# execution-affecting options including immutable input_media refs, excluding mutable lease/status/projection fields. The filename
# request id is supplied by the caller and overrides any untrusted payload alias.
# 函数用途: 计算 Gateway 请求不可变身份与执行内容的稳定指纹。
def gateway_request_fingerprint(payload: dict, request_id: str) -> str:
    canonical_id = str(request_id or "").strip()
    immutable: dict[str, object] = {"id": canonical_id}
    if "request_id" in payload:
        immutable["request_id"] = canonical_id
    for key in _GATEWAY_REQUEST_FINGERPRINT_KEYS:
        if key in payload:
            immutable[key] = payload.get(key)
    encoded = json.dumps(
        immutable,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# LLM: Persisted fingerprints are immutable authority. Missing legacy values may be explicitly
# materialized by the caller, but a present value/schema must match a fresh canonical recomputation.
# 函数用途: 校验已保存的 Gateway 请求指纹，并返回当前正确值。
def validated_gateway_request_fingerprint(payload: dict, request_id: str) -> str:
    expected = gateway_request_fingerprint(payload, request_id)
    stored = str(payload.get("request_fingerprint") or "").strip()
    schema = str(payload.get("request_fingerprint_schema") or "").strip()
    if stored and (
        schema != GATEWAY_REQUEST_FINGERPRINT_SCHEMA
        or stored != expected
    ):
        raise DataCorruptionError(
            "gateway request fingerprint conflicts with immutable request content"
        )
    if schema and schema != GATEWAY_REQUEST_FINGERPRINT_SCHEMA:
        raise DataCorruptionError("gateway request fingerprint schema is invalid")
    return expected


def _path_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _JSON_FILE_LOCKS_GUARD:
        lock = _JSON_FILE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _JSON_FILE_LOCKS[key] = lock
        return lock


def write_json_file(path: Path, payload: dict) -> None:

    # 目录缺失时逐级按 0700 新建（pbfix 2026-10-04：统一走 nofollow_fs.ensure_private_dir；已存在的目录一律不动）。
    ensure_private_dir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


# LLM: 队列文件（请求/响应/状态）都是宿主自己的运行数据，一律按私有权限落盘：目录 0700、文件出生 0600、
#   已有宽权限文件下次写入即收紧。读写方（TUI、适配器、派活工具、后台服务）都是同一个系统用户，
#   收紧到 0600 不影响它们；内容格式与原来逐字节一致（indent=2、sort_keys、无尾换行）。
# 函数用途: 原子替换一个 Gateway 队列 JSON 文件（仅本人可读写）。
def write_json_file_atomic(path: Path, payload: dict) -> None:
    write_private_json_file_atomic_no_newline(path, payload, sort_keys=True)


# 函数用途: 尽力删除临时文件；删除失败不影响已经完成或即将重试的替换。
def _unlink_tmp_file(tmp: Path) -> None:
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
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.01 * (attempt + 1))
    if last_error is not None:
        raise last_error


def read_json_file(path: Path) -> dict:
    return read_json_file_report(path).payload


def read_json_file_report(path: Path, *, context: str = "gateway.json.read") -> GatewayJsonReadReport:
    if not path.exists():
        return GatewayJsonReadReport({})

    try:
        with _locked_json_path(path):
            payload = json.loads(path.read_text(encoding="utf-8"))
    except (BlockingIOError, InterruptedError) as exc:
        # LLM: BlockingIOError/InterruptedError 都是 OSError 子类，必须在下面的 catch-all 之前单独
        #   识别：锁忙（含身份核对重试耗尽）要保持“可重试的锁失败”语义，调用方按锁失败处理，
        #   不能当成数据损坏；load_error 仍带上诊断，其余只认 load_error 的调用方行为不变。
        return GatewayJsonReadReport({}, _gateway_json_load_error(path, exc, context), lock_busy=True)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return GatewayJsonReadReport({}, _gateway_json_load_error(path, exc, context))
    if isinstance(payload, dict):
        return GatewayJsonReadReport(payload)
    return GatewayJsonReadReport(
        {},
        _gateway_json_load_error(
            path,
            ValueError(f"gateway JSON root is {type(payload).__name__}, expected object"),
            context,
        ),
    )


def _gateway_json_load_error(path: Path, exc: BaseException, context: str) -> dict:
    report = runtime_error_report(exc, context=context)
    report["path"] = str(path)
    return report


# LLM: require_existing closes move/update races for queue records while preserving the
# create-or-update behavior used by conversation and collaboration state files.
#   读-改-写整段在同一把锁内完成，落盘走私有原子写（目录 0700、文件 0600、存量宽权限下次写入收紧）。
# 函数用途：在单个文件锁内读改写 JSON，并可要求目标必须仍然存在（仅本人可读写）。
# 函数用途: 在 per-path 锁内读改写一个 JSON 文件，落盘走私有原语（0600/0700）。
def update_json_file_atomic(
    path: Path,
    updater: Callable[[dict], dict],
    *,
    require_existing: bool = False,
) -> dict:
    with _locked_json_path(path):
        if require_existing and not path.is_file():
            raise FileNotFoundError(path)
        current = _read_json_dict_unlocked(path)
        updated = _updated_json_dict(updater, current)
        write_private_json_file_atomic_no_newline_unlocked(path, updated, sort_keys=True)
        return updated


def _read_json_dict_unlocked(path: Path) -> dict:
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return current if isinstance(current, dict) else {}


def _updated_json_dict(updater: Callable[[dict], dict], current: dict) -> dict:
    updated = updater(dict(current))
    if not isinstance(updated, dict):
        raise TypeError("update_json_file_atomic updater must return dict")
    return updated


@contextmanager
def _locked_json_path(path: Path):
    lock = _path_lock(path)
    with lock:
        with _locked_file_path(path):
            yield


@contextmanager
def locked_file_transition(path: Path):
    """Serialize a logical transition spanning more than one durable file.

    The JSON helpers make one file atomic.  Conversation steer/stop/complete must
    order a task-link transition and its guidance ledger together, so they share a
    dedicated lock path through this small public boundary.
    """
    with _locked_json_path(path):
        yield


# LLM: Read-only status paths may probe an active transaction without queueing behind provider IO.
# A False result grants no authority to mutate; callers must return the last atomic receipt.
# 函数用途: 尝试领取逻辑文件事务锁，锁正被使用时立即返回 False 而不是阻塞。
@contextmanager
def try_locked_file_transition(path: Path):
    lock = _path_lock(path)
    if not lock.acquire(blocking=False):
        yield False
        return
    handle = None
    os_lock_acquired = False
    try:
        handle = _open_lock_handle(path)
        os_lock_acquired = _try_flock_exclusive(handle)
        if not os_lock_acquired:
            yield False
            return
        # LLM: 锁文件可能刚被孤儿清理器换掉：身份不符说明拿到的不是当前锁文件的锁，探测不能获得授权
        #   （fail-closed）；按"没拿到"返回，让调用方保留原事实。
        if not _lock_handle_matches_path(handle, path.with_name(path.name + ".lock")):
            yield False
            return
        yield True
    finally:
        if handle is not None:
            try:
                if os_lock_acquired:
                    _flock_unlock(handle)
            finally:
                handle.close()
        lock.release()


# LLM: Every processing/inbox/terminal transition and active-turn ingress for one request must
# share this lock. The digest keeps untrusted request ids out of filenames without losing identity.
# 函数用途: 取得并持有某个 Gateway 回合唯一的跨进程状态转换锁。
@contextmanager
def gateway_turn_transition(paths: GatewayPaths, request_id: str):
    turn_id = str(request_id or "").strip()
    if not turn_id:
        raise ValueError("gateway turn transition requires request_id")
    digest = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()
    transition_path = paths.root / "turn_transitions" / f"{digest}.transition"
    with locked_file_transition(transition_path):
        yield


# LLM: Non-blocking probes use the exact same hashed T-lock path as blocking turn transitions;
# failure to acquire can only preserve/replay an existing fact, never authorize a state change.
# 函数用途: 非阻塞尝试取得某个 Gateway 回合的跨进程状态转换锁。
@contextmanager
def try_gateway_turn_transition(paths: GatewayPaths, request_id: str):
    turn_id = str(request_id or "").strip()
    if not turn_id:
        raise ValueError("gateway turn transition requires request_id")
    digest = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()
    transition_path = paths.root / "turn_transitions" / f"{digest}.transition"
    with try_locked_file_transition(transition_path) as acquired:
        yield acquired


# LLM: 锁文件可能被孤儿清理器删除或替换（gateway 请求锁 sidecar）：拿到 flock 后核对 fd 与锁路径的
#   (st_dev, st_ino) 一致；不一致说明名字已换或已删——在旧 inode 上持锁不再是互斥保证（阻塞迟到者场景），
#   close 后重开重试。超上限按锁竞争失败（BlockingIOError），与既有线程锁失败语义一致。
_LOCK_IDENTITY_RETRY_COUNT = 5


# LLM: 锁与数据文件同口径私有（0600/0700）；缺失目录经 no-follow 原语按 0700 建、已存在一律不动（lkp/pdp）；保留 blocking=False 非阻塞语义。
# 函数用途: 在 <path>.lock 上取排他锁，包裹调用方的临界区。
@contextmanager
def _locked_file_path(path: Path):
    handle = _acquire_verified_lock(path)
    try:
        yield
    finally:
        _flock_unlock(handle)
        handle.close()


# LLM: 取锁重试循环独立成函数（压平临界区的嵌套）：每轮"打开→阻塞取锁→核对身份"；身份不符说明锁文件
#   被清理器换掉，关闭后重开重试；取锁本身异常时关闭句柄再冒泡，不泄漏 fd；超上限抛 BlockingIOError。
# 函数用途: 反复取锁直到 fd 与锁路径身份一致，返回已验证的锁句柄。
def _acquire_verified_lock(path: Path):
    lock_path = path.with_name(path.name + ".lock")
    for _ in range(_LOCK_IDENTITY_RETRY_COUNT):
        handle = _open_lock_handle(path)
        try:
            _flock_exclusive(handle)
        except BaseException:
            handle.close()
            raise
        if _lock_handle_matches_path(handle, lock_path):
            return handle
        handle.close()
    raise BlockingIOError("锁文件身份反复变化，放弃取锁")


# LLM: 身份核对只比较 (st_dev, st_ino)，不看 mtime/权限；路径缺失或不可 stat 一律视为不匹配（fail-closed）。
# 函数用途: 核对锁句柄与锁路径当前指向同一个 inode。
def _lock_handle_matches_path(handle, lock_path: Path) -> bool:
    try:
        fd_stat = os.fstat(handle.fileno())
        path_stat = os.stat(lock_path)
    except OSError:
        return False
    return (fd_stat.st_dev, fd_stat.st_ino) == (path_stat.st_dev, path_stat.st_ino)


# LLM: 锁文件与数据文件同口径私有（文件 0600、父目录 0700、不跟随符号链接）：原来 lock_path.open("a+")+mkdir
#   会按 umask 落成 0644/0755，同机他用户可打开并 flock 卡住宿主写入（ds3 统一锁写法时漏掉的第四处）。
#   经 open_private_lock_beneath_tightened 拿 fd（已存在宽权限锁打开时收紧到 0600），再包回文本句柄；
#   flock / Windows byte-lock / close 的既有用法不变。
# 函数用途: 打开一个私有锁文件句柄，顺带把已存在的宽权限锁收紧到 0600。
def _open_lock_handle(path: Path):
    lock_path = path.with_name(path.name + ".lock")
    descriptor = _open_private_lock_descriptor(lock_path)
    return os.fdopen(descriptor, "a+", encoding="utf-8")


# LLM: 调用方给的是完整锁路径；这里把绝对路径拆成“已存在的最近祖先 + 其余缺失段”（与其余锁调用点共用
#   nofollow_fs.split_existing_anchor），让缺失目录仍统一经 no-follow 原语按 0700 创建。
# 函数用途: 打开一个私有锁文件描述符。
def _open_private_lock_descriptor(lock_path: Path) -> int:
    lock_path = Path(os.path.abspath(lock_path))
    anchor, missing = split_existing_anchor(lock_path.parent)
    return open_private_lock_beneath_tightened(anchor, (*missing, lock_path.name))


# LLM: POSIX flock and Windows byte-range locking implement the same blocking cross-process
# transition. A platform without either primitive fails closed because process-local locking cannot
# uphold the Gateway exactly-once contract.
# 函数用途: 跨平台独占当前 lock 文件，直到同路径的另一个进程释放事务。
def _flock_exclusive(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return
    if msvcrt is not None:
        _ensure_windows_lock_byte(handle)
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                return
            except OSError as exc:
                if not _is_file_lock_contention(exc):
                    raise
                time.sleep(0.05)
    raise RuntimeError("cross-process file locking is unavailable on this platform")


# LLM: A non-blocking probe distinguishes live ownership from an abandoned executing receipt.
# Unexpected POSIX lock errors still raise; only contention returns False.
# 函数用途: 尝试取得 OS 文件锁并返回是否成功，不等待另一个进程释放。
def _try_flock_exclusive(handle) -> bool:
    if fcntl is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                return False
            raise
        return True
    if msvcrt is not None:
        _ensure_windows_lock_byte(handle)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if _is_file_lock_contention(exc):
                return False
            raise
        return True
    raise RuntimeError("cross-process file locking is unavailable on this platform")


# LLM: Unlock mirrors the primitive selected by acquisition and always targets byte zero on
# Windows; callers invoke it from finally so exceptions cannot leak ownership.
# 函数用途: 释放当前 lock 文件的跨进程独占锁。
def _flock_unlock(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    if msvcrt is not None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


# LLM: msvcrt locks a byte range rather than an inode. Each lock file therefore owns one durable
# sentinel byte, created before the first acquisition and never used as business data.
# 函数用途: 确保 Windows byte-range lock 有固定的第 0 字节可锁。
def _ensure_windows_lock_byte(handle) -> None:
    handle.seek(0, 2)
    if handle.tell() == 0:
        handle.write("\0")
        handle.flush()
    handle.seek(0)


# LLM: Only explicit lock-contention errors mean another process owns the transaction. Invalid
# handles, disk faults, and unsupported operations must propagate instead of masquerading as busy.
# 函数用途: 判断一次跨平台文件锁失败是否只是“锁正被其他进程占用”。
def _is_file_lock_contention(exc: OSError) -> bool:
    contention_errnos = {errno.EACCES, errno.EAGAIN}
    deadlock_errno = getattr(errno, "EDEADLK", None)
    if deadlock_errno is not None:
        contention_errnos.add(deadlock_errno)
    if exc.errno in contention_errnos:
        return True
    return getattr(exc, "winerror", None) in {33}


# 孤儿锁 sidecar 的最小年龄：24 小时宽限，避开“文件刚删、别的进程还握着旧句柄读写”的窗口，又让清理每天都能收掉积压；纯维护动作，不加配置开关。
_ORPHAN_LOCK_MIN_AGE_SECONDS = 24 * 3600


# LLM: 请求队列的读写都会给数据文件建 <name>.lock sidecar（_open_lock_handle/_locked_file_path），
#   文件归档/删除后 sidecar 没有任何删除路径，会长期累积（真机 3000+ 空锁文件）。清理只删同时满足三条的：
#   对应数据文件已不存在、mtime 早于宽限（见上）、且能非阻塞拿到 flock（拿不到=有人正持锁）。
#   数据文件还在、宽限内、拿不到锁的一律不动；单文件失败只跳过，绝不把异常抛给维护巡。
# 函数用途: 清扫一个目录里的孤儿锁 sidecar，返回删掉的数量。
def cleanup_orphan_lock_files(directory: Path, *, now: float | None = None) -> int:
    if not directory.is_dir():
        return 0
    current = time.time() if now is None else now
    try:
        candidates = sorted(directory.glob("*.lock"))
    except OSError:
        return 0
    return sum(1 for lock_path in candidates if _cleanup_one_orphan_lock(lock_path, current=current))


# LLM: 三条判据（孤儿、超宽限、非活锁）逐条早返回，单文件失败只跳过；拆出来同时压平整轮清扫的嵌套。
# 函数用途: 判断并尝试删除单个孤儿锁文件，真的删掉返回 True。
def _cleanup_one_orphan_lock(lock_path: Path, *, current: float) -> bool:
    if len(lock_path.name) <= len(".lock"):
        return False
    try:
        modified_at = lock_path.stat().st_mtime
    except OSError:
        return False
    if current - modified_at < _ORPHAN_LOCK_MIN_AGE_SECONDS:
        return False
    if lock_path.with_name(lock_path.name[: -len(".lock")]).exists():
        return False
    try:
        return _remove_orphan_lock_file(lock_path)
    except Exception:  # noqa: BLE001 单个坏锁文件不能中断整轮清扫
        return False


# LLM: 必须在锁文件本体上试锁：_open_lock_handle 是"数据文件路径"入口，会再加一层 .lock，
#   用它探测会永远拿得到 <name>.lock.lock，活锁保护形同虚设（obsfix34b 实测：真实持有者持锁时照样被删）。
#   拿不到锁说明有活进程正在用这把锁，直接放行不动文件。拿到后在持锁状态下 unlink 再 close：
#   POSIX 下删名后新来者只能创建新 inode，不会出现"旧 inode 持有者与新 inode 持有者同时进临界区"。
#   Windows 上打开着的文件删不掉：unlink 失败就放弃本次删除（宁可留孤儿，不回退成先 close 再删，
#   那会重新引入竞态窗口）。
# 函数用途: 非阻塞试锁并在持锁状态下删掉一个孤儿锁文件，真的删掉返回 True。
def _remove_orphan_lock_file(lock_path: Path) -> bool:
    handle = os.fdopen(_open_private_lock_descriptor(lock_path), "a+", encoding="utf-8")
    try:
        if not _try_flock_exclusive(handle):
            return False
        try:
            lock_path.unlink()
        except FileNotFoundError:
            return False
        except OSError:
            return False
        return True
    finally:
        handle.close()


# 流式 chunk 文件单次读取上限：整体 f.read() 无上限会 MemoryError，按上限分块读、剩余部分下一拍继续
# （CLI 与 TUI 两个网关客户端共用这一份）。
STREAM_CHUNK_READ_MAX_BYTES = 8 * 1024 * 1024


# LLM: Streaming JSONL readers may observe a writer between payload bytes and its trailing newline.
# Only newline-terminated rows advance the byte cursor; trailing partial UTF-8 stays for the next poll.
# 函数用途: 从字节游标读取一批完整 UTF-8 行，保留尚未写完的最后一行而不误报或丢失。
def read_complete_utf8_rows(
    path: Path,
    offset: int,
    *,
    max_bytes: int,
) -> tuple[tuple[str, ...], int, bool]:
    start = max(0, int(offset or 0))
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        if start > size:
            start = 0
        handle.seek(start)
        data = handle.read(max(1, int(max_bytes)))
    boundary = data.rfind(b"\n")
    if boundary < 0:
        return (), start, False
    complete = data[: boundary + 1]
    rows: list[str] = []
    decode_error = False
    for raw in complete.split(b"\n")[:-1]:
        try:
            rows.append(raw.decode("utf-8"))
        except UnicodeDecodeError:
            decode_error = True
            rows.append(raw.decode("utf-8", "replace"))
    return tuple(rows), start + len(complete), decode_error


def read_pid(path: Path) -> int:

    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def tail_lines(path: Path, line_count: int) -> list[str]:

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    if line_count <= 0:
        return lines
    return lines[-line_count:]


# LLM: 前缀与工具层守卫共用 common.logical_reference_ids 的同一常量（禁止另写一份字面量）；
#   守卫按"前缀 + 冒号 + call 编号"形态拒绝把逻辑引用当写路径。
# 函数用途: 生成一次 Gateway 请求号（gwreq-<时间戳>-<随机 hex>）。
def new_gateway_request_id() -> str:

    return f"{GATEWAY_REQUEST_ID_PREFIX}{int(time.time())}-{uuid.uuid4().hex}"


def gateway_response_path(paths: GatewayPaths, request_id: str) -> Path:

    return paths.responses / f"{request_id}.json"


# LLM: 这些目录的 JSON 读/写都走 _locked_file_path（<name>.lock sidecar），文件归档/删除后 sidecar 会累积；
#   锁清理巡只扫这一份清单（队列四个目录 + 终态副本 + 响应 + 每个请求都会建锁的 turn_transitions），
#   新增别的锁目录要同步加进来。清理只删孤儿（见 cleanup_orphan_lock_files），活锁与在用文件不动。
# 函数用途: 列出会积累锁 sidecar 的 Gateway 目录，供周期清理。
def gateway_lock_sidecar_dirs(paths: GatewayPaths) -> tuple[Path, ...]:
    return (
        paths.inbox,
        paths.processing,
        paths.done,
        paths.failed,
        paths.terminal,
        paths.responses,
        paths.root / "turn_transitions",
    )


# LLM: 队列计数只投影目录事实，不读取请求或参与准入；scandir 复用目录类型，避免逐文件 stat 抢占状态线程。
# 函数用途: 轻量统计等待和执行数量，历史归档仅在调用方明确需要时纳入。
def gateway_request_counts(paths: GatewayPaths, *, include_archives: bool = True) -> dict[str, int]:

    # LLM: 保持只计 JSON 普通文件的原语义；有界迭代，不把队列正文或所有 Path 常驻内存。
    # 函数用途: 一次目录遍历计数，避免高并发时重复释放和争抢 GIL。
    def count_json(path: Path) -> int:
        try:
            with os.scandir(path) as entries:
                return sum(1 for item in entries if item.name.endswith(".json") and item.is_file())
        except OSError:
            return 0

    counts = {
        "pending": count_json(paths.inbox),
        "processing": count_json(paths.processing),
    }
    if include_archives:
        counts.update(
            {
                "done": count_json(paths.done),
                "failed": count_json(paths.failed),
                "responses": count_json(paths.responses),
            }
        )
    return counts


def gateway_queue_ages(paths: GatewayPaths) -> dict[str, float]:
    """结构化队列年龄观测：最老 pending 等待秒数与最老 processing lease 年龄。

    只用文件 mtime（结构化事实），不读文件内容；供 heartbeat / status 展示，
    不参与任何调度或恢复决策。"""
    now = time.time()

    def oldest_age(path: Path) -> float:
        try:
            mtimes = [item.stat().st_mtime for item in path.glob("*.json") if item.is_file()]
        except OSError:
            return 0.0
        return round(now - min(mtimes), 3) if mtimes else 0.0

    return {
        "oldest_pending_age_seconds": oldest_age(paths.inbox),
        "oldest_processing_age_seconds": oldest_age(paths.processing),
    }


# LLM: 请求队列文件也是宿主数据；tmp 与目标都按私有权限落盘（目录 0700、文件出生 0600、
#   已有宽权限文件下次入队即收紧）。读取方（TUI、适配器、派活工具、后台服务）都是同一个系统用户。
# 函数用途: 把一个 Gateway 请求写进 inbox 队列并返回目标路径（仅本人可读写）。
def write_gateway_request(paths: GatewayPaths, payload: dict) -> Path:
    request_id = str(payload["id"])
    target = paths.inbox / f"{request_id}.json"
    write_private_json_file_atomic_no_newline(target, payload, sort_keys=True)
    from ..observability.concurrency_metrics import gateway_request_enqueued

    gateway_request_enqueued()  # §6-A 进队计数(与 claimed 对比:排队饿死 vs 认领后卡首轮一眼可分)
    return target


# LLM: A stable client message id maps to one request id across HTTP response loss. The exact-turn
# lock protects create-vs-claim/archive races; existing payload identity must match before replay.
# 函数用途: 按稳定请求 ID 只入队一次，断线重试返回同一请求而不会复活或复制任务。
def write_gateway_request_once(paths: GatewayPaths, payload: dict) -> tuple[Path, bool]:
    request_id = str(payload.get("id") or "").strip()
    if not request_id:
        raise ValueError("gateway request id is required")
    expected_digest = _gateway_client_input_digest(payload)
    if not expected_digest:
        raise ValueError("gateway idempotent request requires client_input_digest")
    metadata = payload.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    from .input_delivery_service import (
        gateway_input_transition,
        load_or_prepare_gateway_input_locked,
        queue_gateway_input_locked,
    )

    with gateway_input_transition(paths, request_id):
        receipt, _prepared = load_or_prepare_gateway_input_locked(
            paths,
            request_id=request_id,
            client_input_digest=expected_digest,
            client_message_id=str(metadata.get("message_id") or "").strip(),
            guidance_dedupe_key="",
            prepared_request=payload,
        )
        receipt, created = queue_gateway_input_locked(paths, receipt)
    for folder in (paths.inbox, paths.processing, paths.done, paths.failed):
        candidate = folder / f"{request_id}.json"
        if candidate.is_file():
            return candidate, created
    return gateway_response_path(paths, receipt.request_id), created


# LLM: The digest is a structured ingress fact written by the HTTP/client builder. Absence is
# represented explicitly so legacy random-id requests can never compare equal to idempotent input.
# 函数用途: 读取一次幂等 Gateway 请求的稳定输入指纹。
def _gateway_client_input_digest(payload: dict) -> str:
    metadata = payload.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    return str(metadata.get("client_input_digest") or "").strip()


def append_gateway_history(paths: GatewayPaths, payload: dict) -> None:
    append_gateway_history_once(paths, payload)


# LLM: History is a rebuildable projection keyed by canonical request id. One global transition
# lock plus an mtime-invalidated in-process index makes normal appends O(1) after one file scan;
# conflicting/duplicate rows are atomically replaced by the canonical payload.
#   落盘走私有追加/原子重写（0600/0700），存量宽权限历史下次写入即收紧；内容逐字节不变。
# 函数用途: 按请求 ID 最多保留一条准确历史记录，并返回本次是否实际写入或修复。
def append_gateway_history_once(paths: GatewayPaths, payload: dict) -> bool:
    request_id = str(payload.get("id") or payload.get("request_id") or "").strip()
    if not request_id:
        raise ValueError("gateway history projection requires request_id")
    transition = paths.root / "history_transitions" / "history.transition"
    with locked_file_transition(transition):
        lines, index = _gateway_history_snapshot(paths.history)
        existing = index.get(request_id)
        if (
            existing is not None
            and not existing.conflict
            and existing.count == 1
            and existing.payload == payload
        ):
            return False
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if existing is None:
            append_private_jsonl_records(paths.history, [payload], sort_keys=True)
            retained = [*lines, serialized]
        else:
            retained: list[str] = []
            for line in lines:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    retained.append(line)
                    continue
                row_id = (
                    str(row.get("id") or row.get("request_id") or "").strip()
                    if isinstance(row, dict)
                    else ""
                )
                if row_id != request_id:
                    retained.append(line)
            retained.append(serialized)
            write_private_text_file_atomic(paths.history, "\n".join(retained) + "\n")
        index[request_id] = _GatewayHistoryIndexEntry(dict(payload))
        _store_gateway_history_cache(paths.history, retained, index)
    return True


# LLM: Snapshot parsing preserves raw lines for atomic conflict repair while the index stores only
# valid object rows. Cache validity is a filesystem stamp, never a delivery or completion fact.
# 函数用途: 读取 Gateway 历史一次并构建按请求 ID 查询的进程内索引。
def _gateway_history_snapshot(
    path: Path,
) -> tuple[list[str], dict[str, _GatewayHistoryIndexEntry]]:
    stamp = _gateway_history_stamp(path)
    key = str(path.resolve())
    with _GATEWAY_HISTORY_CACHE_LOCK:
        cached = _GATEWAY_HISTORY_CACHE.get(key)
    if cached is not None and cached[0] == stamp:
        return list(cached[1]), dict(cached[2])
    # JSONL 记录边界只能是物理 LF：splitlines() 会在 U+0085/U+2028/U+2029 等合法正文字符处切开记录。
    lines = jsonl_lines(path.read_text(encoding="utf-8")) if path.exists() else []
    index: dict[str, _GatewayHistoryIndexEntry] = {}
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        request_id = str(row.get("id") or row.get("request_id") or "").strip()
        if not request_id:
            continue
        existing = index.get(request_id)
        if existing is None:
            index[request_id] = _GatewayHistoryIndexEntry(dict(row))
        else:
            index[request_id] = _GatewayHistoryIndexEntry(
                existing.payload,
                count=existing.count + 1,
                conflict=existing.conflict or existing.payload != row,
            )
    with _GATEWAY_HISTORY_CACHE_LOCK:
        _GATEWAY_HISTORY_CACHE[key] = (stamp, tuple(lines), dict(index))
    return lines, index


# LLM: mtime and size detect writes from other Gateway processes without making this cache a
# cross-process authority.
# 函数用途: 返回历史文件当前缓存校验戳，不存在时用零值。
def _gateway_history_stamp(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
    except OSError:
        return 0, 0
    return int(stat.st_mtime_ns), int(stat.st_size)


# LLM: Caller has persisted these exact lines under the global history transition lock, so the
# refreshed stamp and copied values are safe as the next O(1) lookup snapshot.
# 函数用途: 在写入历史后同步刷新对应进程内索引缓存。
def _store_gateway_history_cache(
    path: Path,
    lines: list[str],
    index: dict[str, _GatewayHistoryIndexEntry],
) -> None:
    with _GATEWAY_HISTORY_CACHE_LOCK:
        _GATEWAY_HISTORY_CACHE[str(path.resolve())] = (
            _gateway_history_stamp(path),
            tuple(lines),
            dict(index),
        )
