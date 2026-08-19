
from __future__ import annotations

"""provides deterministic JSON-file queue IO helpers for gateway protocol files.

这个文件只管 gateway 文件队列的基础读写。
比如写请求文件、读响应文件、数队列里有多少 JSON、追加历史流水。
这里不执行业务，只保证文件格式和移动规则稳定。
"""

import errno
import hashlib
import json
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..common.json_io import write_text_file_atomic
from ..io import append_jsonl
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


# LLM: The request fingerprint covers authenticated owner, conversation, prompt, task, and all
# execution-affecting options while excluding mutable lease/status/projection fields. The filename
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

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def write_json_file_atomic(path: Path, payload: dict) -> None:

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    with _locked_json_path(path):
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            _replace_with_retry(tmp, path)
        finally:
            _unlink_tmp_file(tmp)


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
# 函数用途：在单个文件锁内读改写 JSON，并可要求目标必须仍然存在。
def update_json_file_atomic(
    path: Path,
    updater: Callable[[dict], dict],
    *,
    require_existing: bool = False,
) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    with _locked_json_path(path):
        try:
            if require_existing and not path.is_file():
                raise FileNotFoundError(path)
            current = _read_json_dict_unlocked(path)
            updated = _updated_json_dict(updater, current)
            tmp.write_text(json.dumps(updated, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            _replace_with_retry(tmp, path)
            return updated
        finally:
            _unlink_tmp_file(tmp)


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


@contextmanager
def _locked_file_path(path: Path):
    with _open_lock_handle(path) as handle:
        _flock_exclusive(handle)
        try:
            yield
        finally:
            _flock_unlock(handle)


def _open_lock_handle(path: Path):
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    return lock_path.open("a+", encoding="utf-8")


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


def new_gateway_request_id() -> str:

    return f"gwreq-{int(time.time())}-{uuid.uuid4().hex}"


def gateway_response_path(paths: GatewayPaths, request_id: str) -> Path:

    return paths.responses / f"{request_id}.json"


def gateway_request_counts(paths: GatewayPaths, *, include_archives: bool = True) -> dict[str, int]:

    def count_json(path: Path) -> int:
        try:
            return len([item for item in path.glob("*.json") if item.is_file()])
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


def write_gateway_request(paths: GatewayPaths, payload: dict) -> Path:

    request_id = str(payload["id"])
    paths.inbox.mkdir(parents=True, exist_ok=True)
    target = paths.inbox / f"{request_id}.json"
    tmp = paths.inbox / f".{request_id}.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(target)
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
            append_jsonl(paths.history, payload, sort_keys=True)
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
            write_text_file_atomic(paths.history, "\n".join(retained) + "\n")
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
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
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
