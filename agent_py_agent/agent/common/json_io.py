# LLM: JSON 文件仍共用原线程锁和 OS 锁；显式非阻塞准入只改变等待方式，不建立另一锁名或写入入口。
# 模块用途: 提供运行元数据的读取、原子写入与共享文件锁，让管理准备在资源繁忙时及时返回。

from __future__ import annotations

import json
import threading
import time
import uuid
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..runtime_errors import runtime_error_report

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows import guard.
    fcntl = None


class _PathLock:
    """可弱引用的 per-path 锁包装。

    threading.Lock 是 C 级对象不能被弱引用,故包一层暴露 __weakref__。语义:只要还有
    线程在临界区内、或在等这把锁,就持有本对象的强引用(见 _locked_json_path),弱字典不会
    回收它——互斥语义绝不破;一旦没人用了,GC 自动回收,锁表不再每见一个新文件就永久泄漏一把锁。
    """

    __slots__ = ("lock", "__weakref__")

    def __init__(self) -> None:
        self.lock = threading.Lock()


# 弱值字典:某路径的锁无任何线程持有/等待时被 GC 自动摘除 → 长跑进程锁表只随"活跃文件数"而非
# "历史见过的文件数"增长,根治无界泄漏(审计 #16,对照 _TEXT_LINES_CACHE 的 FIFO 上限同理)。
_JSON_FILE_LOCKS: weakref.WeakValueDictionary[str, _PathLock] = weakref.WeakValueDictionary()
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


def write_text_file_atomic_unlocked(path: Path, content: str) -> None:
    """Atomically replace a text file when the caller already holds ``locked_json_path``.

    Read-modify-write repositories use this companion to
    :func:`write_text_file_atomic`.  Keeping the lock acquisition outside lets
    validation and the final replace share one critical section without trying
    to re-enter the non-reentrant per-path lock.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text_unlocked(path, content)


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


# LLM: JSONL 的记录边界只有一个：物理 LF。禁止用 str.splitlines()——它会在
# U+000B/U+000C/U+001C-U+001E/U+0085(NEL)/U+2028/U+2029 处切开，而这些字符完全可以是
# 合法 JSON 字符串的内容（真实事故：子代理 transcript 里一个 U+0085 把一条记录切成两条，
# 按 LF 读 0 个错误、按 splitlines 读 19 个错误，child 被判 conversation transcript is
# unreadable 而整体 FAILED）。这里只做边界切分，不清洗字符、不吞坏行：真正的半行/截断/
# 非法 JSON 仍然交给调用方逐条报结构化错误。
# 函数用途: 按 JSONL 记录边界（物理 LF）切分文本，保留字符串内的 NEL/U+2028/U+2029 等字符。
def jsonl_lines(text: str) -> tuple[str, ...]:
    if not text:
        return ()
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    # 兼容 CRLF 写入方：只在记录末尾去掉一个 \r，绝不把记录内部的字符当边界。
    return tuple(line[:-1] if line.endswith("\r") else line for line in lines)


# LLM: 读 JSONL 文件也必须走同一条边界规则；调用方拿到的是记录列表，不是"文本行"。
# 函数用途: 读一个 JSONL 文件的记录列表（仅按 LF 切分）。
def read_jsonl_text_lines(path: Path) -> tuple[str, ...]:
    return jsonl_lines(path.read_text(encoding="utf-8"))


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
        lines = list(read_jsonl_text_lines(path))
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


def append_jsonl_capped(path: Path, record: dict[str, object], *, max_records: int) -> None:
    """有界 append:追加一条后仅保留最近 max_records 条,防 append-only 台账无界增长(审计 #16)。

    全程持同一把 per-path 锁做读-改-写,与并发 append/trim 串行不丢记录不撕行;旧文件损坏行被
    跳过(read_jsonl_objects_report 容错语义)。max_records<=0 退化为不裁剪的整文件重写。
    background_jobs 登记等"只增不回收"的观测台账走这里,长跑磁盘恒定。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with _locked_json_path(path):
        records = read_jsonl_objects_report(path).records
        records.append(record)
        if max_records > 0:
            records = records[-max_records:]
        content = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records)
        _atomic_write_text_unlocked(path, content)


def _atomic_write_text_unlocked(path: Path, content: str) -> None:
    """temp+replace 原子写文本,不自取锁(调用方已持 _locked_json_path,锁不可重入)。"""
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_text(content, encoding="utf-8")
        _replace_with_retry(tmp, path)
    finally:
        _unlink_tmp_file(tmp)


def _path_lock(path: Path) -> _PathLock:
    key = str(path.resolve())
    with _JSON_FILE_LOCKS_GUARD:
        handle = _JSON_FILE_LOCKS.get(key)
        if handle is None:
            handle = _PathLock()
            _JSON_FILE_LOCKS[key] = handle
        return handle


# LLM: 默认仍等待原双层锁；blocking=False 任一层繁忙即失败，调用方不能因此绕开原子读改写。
# 函数用途: 为原文件提供共用临界区，允许有期限的管理入口在竞争时立即返回。
@contextmanager
def locked_json_path(path: Path, *, blocking: bool = True):
    """公开的"线程锁 + fcntl.flock(LOCK_EX)"双层临界区(与 io/jsonl.py 同手法)。

    供需要把 读-改-写 整段做成原子的调用方使用(如 OptimisticLock 的 CAS):
    进入即对 path 的 per-path 线程锁 + 同名 .lock 文件的 OS 排他锁双重持有,
    退出释放。临界区内落盘请用 write_json_file_atomic_unlocked(锁已持有)。"""

    with _locked_json_path(path, blocking=blocking):
        yield


# LLM: 持有 _PathLock 强引用到释放，非阻塞失败不能释放别人的锁；文件锁失败也必须释放已取得的线程锁。
# 函数用途: 按固定顺序取得线程锁与 OS 锁，并在所有退出路径归还。
@contextmanager
def _locked_json_path(path: Path, *, blocking: bool = True):
    handle = _path_lock(path)  # 持 _PathLock 强引用直到临界区结束 → 持锁期间弱字典绝不回收它
    if not handle.lock.acquire(blocking=blocking):
        raise BlockingIOError("共享文件线程锁繁忙")
    try:
        with _locked_file_path(path, blocking=blocking):
            yield
    finally:
        handle.lock.release()


# LLM: 锁文件路径和创建方式保持原协议；非阻塞只使用同一 OS 锁的 LOCK_NB，不跳过跨进程互斥。
# 函数用途: 在原同名锁文件上获取排他权，忙碌或异常时关闭文件句柄。
@contextmanager
def _locked_file_path(path: Path, *, blocking: bool = True):
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        _flock_exclusive(handle, blocking=blocking)
        try:
            yield
        finally:
            _flock_unlock(handle)


# LLM: 不支持 OS 锁的平台沿原告警策略；支持时两种等待模式使用同一 flock，默认行为不变。
# 函数用途: 执行原排他文件锁操作，可选择立即报告锁竞争。
def _flock_exclusive(handle, *, blocking: bool = True) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
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
    lines = read_jsonl_text_lines(path)
    with _TEXT_LINES_CACHE_GUARD:
        while len(_TEXT_LINES_CACHE) >= _TEXT_LINES_CACHE_MAX:
            _TEXT_LINES_CACHE.pop(next(iter(_TEXT_LINES_CACHE)))
        _TEXT_LINES_CACHE[key] = (signature, lines)
    return lines
