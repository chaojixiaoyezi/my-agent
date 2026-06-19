"""Small JSON IO helpers for runtime metadata files."""

from __future__ import annotations

import json
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..runtime_errors import runtime_error_report

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows import guard.
    fcntl = None

_JSON_FILE_LOCKS: dict[str, threading.Lock] = {}
_JSON_FILE_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class JsonObjectReadReport:
    payload: dict[str, Any]
    load_error: dict[str, object] | None = None


@dataclass(frozen=True)
class JsonlObjectsReadReport:
    records: list[dict[str, Any]]
    load_errors: list[dict[str, object]]


def read_json_object(path: Path, *, parse_nested_string: bool = False) -> dict[str, Any]:
    """Read a JSON object, returning an empty dict for missing or malformed files."""

    return read_json_object_report(path, parse_nested_string=parse_nested_string).payload


def read_json_object_report(
    path: Path,
    *,
    parse_nested_string: bool = False,
    context: str = "json_io.read_json_object",
) -> JsonObjectReadReport:
    """Read a JSON object and preserve a model-visible error for malformed files."""

    if not path.exists():
        return JsonObjectReadReport({})
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if parse_nested_string and isinstance(payload, str):
            payload = json.loads(payload)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return JsonObjectReadReport({}, _json_object_load_error(path, exc, context))
    if isinstance(payload, dict):
        return JsonObjectReadReport(payload)
    return JsonObjectReadReport(
        {},
        _json_object_load_error(path, ValueError(f"JSON root is {type(payload).__name__}, expected object"), context),
    )


def _json_object_load_error(path: Path, exc: BaseException, context: str) -> dict[str, object]:
    report = runtime_error_report(exc, context=context)
    report["path"] = str(path)
    return report


def write_json_object(path: Path, payload: dict[str, object], *, sort_keys: bool = True) -> None:
    """Write a small JSON object with parent creation and a trailing newline."""

    write_json_file(path, payload, sort_keys=sort_keys)


def write_json_file(path: Path, payload: object, *, sort_keys: bool = True) -> None:
    """Write JSON payloads with parent creation and a trailing newline."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=sort_keys) + "\n",
        encoding="utf-8",
    )


def write_text_file_atomic(path: Path, content: str) -> None:
    """原子写任意文本:temp+replace,与 JSON 原子写同一把 per-path 锁。
    LocalStore blob 等"半写即损坏"的内容写入统一走这里(体检实锤:records
    的 write_text 非原子,崩溃可留半截内容文件)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    with _locked_json_path(path):
        try:
            tmp.write_text(content, encoding="utf-8")
            _replace_with_retry(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)


def write_json_file_atomic(path: Path, payload: object, *, sort_keys: bool = True) -> None:
    """Write JSON payloads via temp-file replace under a per-path lock."""

    with _locked_json_path(path):
        write_json_file_atomic_unlocked(path, payload, sort_keys=sort_keys)


def write_json_file_atomic_unlocked(path: Path, payload: object, *, sort_keys: bool = True) -> None:
    """temp+replace 原子写,但【不】自己取 per-path 锁。

    用途:调用方已经通过 locked_json_path(path) 持有同一把锁,需要在一个更大的
    读-改-写临界区里复用原子落盘(例如 OptimisticLock 的 CAS)。threading.Lock
    不可重入,所以临界区内严禁再调 write_json_file_atomic(会自死锁),改调本函数。
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=sort_keys) + "\n",
            encoding="utf-8",
        )
        _replace_with_retry(tmp, path)
    finally:
        _unlink_tmp_file(tmp)


def read_jsonl_objects(path: Path) -> list[dict[str, Any]]:
    """Read JSONL objects, skipping blank or malformed rows."""

    return read_jsonl_objects_report(path).records


def read_jsonl_objects_report(path: Path, *, context: str = "json_io.read_jsonl_objects") -> JsonlObjectsReadReport:
    """Read JSONL objects and preserve recoverable diagnostics for bad rows."""

    if not path.exists():
        return JsonlObjectsReadReport([], [])
    records: list[dict[str, Any]] = []
    load_errors: list[dict[str, object]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return JsonlObjectsReadReport([], [_jsonl_load_error(path, exc, context, line_no=0)])
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            load_errors.append(_jsonl_load_error(path, exc, context, line_no=line_no))
            continue
        if isinstance(payload, dict):
            records.append(payload)
            continue
        load_errors.append(
            _jsonl_load_error(
                path,
                ValueError(f"JSONL row is {type(payload).__name__}, expected object"),
                context,
                line_no=line_no,
            )
        )
    return JsonlObjectsReadReport(records, load_errors)


def _jsonl_load_error(path: Path, exc: BaseException, context: str, *, line_no: int) -> dict[str, object]:
    report = runtime_error_report(exc, context=context)
    report["path"] = str(path)
    if line_no:
        report["line"] = line_no
    return report


def write_jsonl_records(path: Path, records: list[dict[str, object]], *, sort_keys: bool = True) -> None:
    """Write a JSONL file, replacing existing content."""

    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(json.dumps(record, ensure_ascii=False, sort_keys=sort_keys) for record in records)
    path.write_text((content + "\n") if content else "", encoding="utf-8")


def append_jsonl_records(path: Path, records: list[dict[str, object]], *, sort_keys: bool = True) -> None:
    """并发安全地 append 一批 JSONL 记录(空批不操作)。

    并发加固(C2/C3,修 H5 审计裸写/H8 非原子):整批先拼成一个 blob、再在 per-path 线程锁 +
    fcntl 排他锁内一次写入——多进程/多线程同时 append 同一审计/记录文件时不再撕行/交错。
    """
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=sort_keys) + "\n" for record in records
    )
    with locked_json_path(path):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(blob)


def _path_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _JSON_FILE_LOCKS_GUARD:
        lock = _JSON_FILE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _JSON_FILE_LOCKS[key] = lock
        return lock


@contextmanager
def locked_json_path(path: Path):
    """公开的"线程锁 + fcntl.flock(LOCK_EX)"双层临界区(与 io/jsonl.py 同手法)。

    供需要把 读-改-写 整段做成原子的调用方使用(如 OptimisticLock 的 CAS):
    进入即对 path 的 per-path 线程锁 + 同名 .lock 文件的 OS 排他锁双重持有,
    退出释放。临界区内落盘请用 write_json_file_atomic_unlocked(锁已持有)。"""

    with _locked_json_path(path):
        yield


@contextmanager
def _locked_json_path(path: Path):
    lock = _path_lock(path)
    with lock:
        with _locked_file_path(path):
            yield


@contextmanager
def _locked_file_path(path: Path):
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        _flock_exclusive(handle)
        try:
            yield
        finally:
            _flock_unlock(handle)


def _flock_exclusive(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    else:
        from .file_lock_support import warn_file_lock_unavailable_once
        warn_file_lock_unavailable_once()


def _flock_unlock(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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


def _unlink_tmp_file(tmp: Path) -> None:
    try:
        tmp.unlink()
    except OSError:
        pass


# LLM: mtime+size 守门的整文件行缓存(批4 性能小修,gateway inbox mtime 门
#   同一手法)。契约:①签名 (st_mtime_ns, st_size) 任一变化即重读——本仓写
#   路径全是 append/原子 replace,size 必变,双保险;②缓存值是 tuple[str]
#   不可变行,跨调用方共享零污染;③FileNotFoundError/OSError 与 read_text
#   同语义上抛,调用方既有异常处理形态不变;④容量上限 FIFO 逐出,防长跑进程
#   缓存无界膨胀。高频轮询的 jsonl 台账(协作收件箱/产物注册表)读路径用它。
# 函数用途: 反复读同一个没变过的台账文件时,直接给上次的解析行,不再碰磁盘。
_TEXT_LINES_CACHE: dict[str, tuple[tuple[int, int], tuple[str, ...]]] = {}
_TEXT_LINES_CACHE_GUARD = threading.Lock()
_TEXT_LINES_CACHE_MAX = 64


def read_text_lines_cached(path: Path) -> tuple[str, ...]:
    stat = path.stat()
    signature = (stat.st_mtime_ns, stat.st_size)
    key = str(path)
    with _TEXT_LINES_CACHE_GUARD:
        hit = _TEXT_LINES_CACHE.get(key)
        if hit is not None and hit[0] == signature:
            return hit[1]
    lines = tuple(path.read_text(encoding="utf-8").splitlines())
    with _TEXT_LINES_CACHE_GUARD:
        while len(_TEXT_LINES_CACHE) >= _TEXT_LINES_CACHE_MAX:
            _TEXT_LINES_CACHE.pop(next(iter(_TEXT_LINES_CACHE)))
        _TEXT_LINES_CACHE[key] = (signature, lines)
    return lines
