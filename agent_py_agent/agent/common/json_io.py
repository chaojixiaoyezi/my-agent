# LLM: JSON 文件仍共用原线程锁和 OS 锁；显式非阻塞准入只改变等待方式，不建立另一锁名或写入入口。
# 模块用途: 提供运行元数据的读取、原子写入与共享文件锁，让管理准备在资源繁忙时及时返回。

from __future__ import annotations

import json
import os
import stat
import threading
import time
import uuid
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..runtime_errors import runtime_error_report
from . import cache_freshness
from .cache_freshness import cache_stat_signature
from .nofollow_fs import (
    ensure_private_dir,
    open_private_lock_beneath_tightened,
    split_existing_anchor,
)

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


# LLM: 保存显式归档根锚点和符号链接警告；同一回合的快照写入共用此结果，避免重复遍历目录链。
# 类用途: 保存私有目录链的锚点、目标、收紧状态与结构化警告，避免后续写入再次遍历目录。
@dataclass(frozen=True)
class PrivateDirectoryChainResult:
    root: Path
    directory: Path
    directories_private: bool
    warning: dict[str, object] | None = None


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


# LLM: 默认确保直接父目录存在（缺失按 0700 逐级新建、已存在一律不动，见 nofollow_fs.ensure_private_dir）；
#   若调用方已完成目录链准备或要保留符号链接目标权限，可关闭该步，临时文件仍出生即 0600；路径锁仍由调用方持有。
# 函数用途: 在既有锁内以 0600 临时文件原子替换正文，可按目录链结果跳过父目录确保步骤。
def write_private_text_file_atomic_unlocked(
    path: Path,
    content: str,
    *,
    ensure_parent_private: bool = True,
) -> None:
    if ensure_parent_private:
        ensure_private_dir(path.parent)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.chmod(tmp, 0o600)
            handle.write(content)
        _replace_with_retry(tmp, path, keep_mode=False)
    finally:
        _unlink_tmp_file(tmp)


# LLM: 仅在 memory_archive 锚点下逐级确保目录存在（缺失按 0700 逐级新建、已存在一律不动）；发现符号链接时保留目标权限并返回结构化原因码，调用方仍可私有写文件。
# 函数用途: 准备私有目录链；符号链接及其下级目录不收紧，但确保快照目录存在。
def ensure_private_directory_chain(
    root: Path,
    directory: Path,
) -> PrivateDirectoryChainResult:
    anchor = Path(os.path.abspath(root))
    target = Path(os.path.abspath(directory))
    try:
        relative = target.relative_to(anchor)
    except ValueError as exc:
        raise ValueError("private directory must be below its anchor") from exc
    if anchor.is_symlink():
        return _skip_private_directory_chain(anchor, target, anchor)
    current = anchor
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            return _skip_private_directory_chain(anchor, target, current)
        ensure_private_dir(current)
    return PrivateDirectoryChainResult(anchor, target, True)


# LLM: 符号链接可能指向搬迁后的归档；缺失下级目录按默认掩码创建，但绝不对链接或目标执行 chmod。
# 函数用途: 遇到符号链接时保留该级及后代目录的权限，返回单条结构化警告供快照写入者记录。
def _skip_private_directory_chain(
    anchor: Path,
    target: Path,
    symlink_path: Path,
) -> PrivateDirectoryChainResult:
    target.mkdir(parents=True, exist_ok=True)
    warning = {
        "reason_code": "private_directory_symlink_skipped",
        "severity": "warning",
        "path": symlink_path.relative_to(anchor).as_posix(),
        "message": "检测到目录符号链接，跳过该级及下级目录权限收紧。",
    }
    return PrivateDirectoryChainResult(anchor, target, False, warning)


# LLM: 使用与 write_text_file_atomic 相同的 per-path 线程锁 + fcntl；父目录确保可由显式参数关闭，防止链接目标被改动。
# 函数用途: 自取锁，以 0600 文件原子替换文本；仅在 ensure_parent_private 为真时确保直接父目录存在（缺失 0700 逐级新建）。
def write_private_text_file_atomic(
    path: Path,
    content: str,
    *,
    ensure_parent_private: bool = True,
) -> None:
    with _locked_json_path(path):
        write_private_text_file_atomic_unlocked(
            path,
            content,
            ensure_parent_private=ensure_parent_private,
        )


# LLM: 输出与 write_json_file_atomic 逐字节一致（indent=2、可选 sort_keys、尾换行），只是权限走私有；
#   已持锁的调用方用 unlocked 版本，避免不可重入的 per-path 锁自死锁。
# 函数用途: 以仅本人可读写的权限原子替换一个 JSON 文件（自取锁）。
def write_private_json_file_atomic(path: Path, payload: object, *, sort_keys: bool = True) -> None:
    with _locked_json_path(path):
        write_private_json_file_atomic_unlocked(path, payload, sort_keys=sort_keys)


# LLM: 与 write_json_file_atomic_unlocked 同格式；调用方须已持有 locked_json_path(path)。
# 函数用途: 持锁场景下以仅本人可读写的权限原子替换一个 JSON 文件。
def write_private_json_file_atomic_unlocked(path: Path, payload: object, *, sort_keys: bool = True) -> None:
    write_private_text_file_atomic_unlocked(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=sort_keys) + "\n",
    )


# LLM: 少数既有入口（Gateway 队列文件）原来不带尾换行；要把它们改成私有写又不改内容，
#   就需要一个不带尾换行的私有变体。缩进、sort_keys 与调用方原实现一致。
# 函数用途: 以仅本人可读写的权限原子替换一个 JSON 文件，且不加尾换行（自取锁）。
def write_private_json_file_atomic_no_newline(path: Path, payload: object, *, sort_keys: bool = True) -> None:
    with _locked_json_path(path):
        write_private_json_file_atomic_no_newline_unlocked(path, payload, sort_keys=sort_keys)


# LLM: 同 write_private_json_file_atomic_no_newline；调用方须已持有 locked_json_path(path)。
# 函数用途: 持锁场景下以仅本人可读写的权限原子替换一个 JSON 文件，不加尾换行。
def write_private_json_file_atomic_no_newline_unlocked(
    path: Path, payload: object, *, sort_keys: bool = True
) -> None:
    ensure_private_dir(path.parent)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.chmod(tmp, 0o600)
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=sort_keys))
        _replace_with_retry(tmp, path, keep_mode=False)
    finally:
        _unlink_tmp_file(tmp)


# LLM: 与 write_json_object 同格式（indent=2、可选 sort_keys、尾换行、父目录自动创建），权限走私有：
#   子代理工单模板等宿主状态文件原本走 write_json_object，改用这里后行为不变、权限收紧。
# 函数用途: 以仅本人可读写的权限写一个 JSON 对象文件（父目录自动创建，带尾换行）。
def write_private_json_object(path: Path, payload: dict[str, object], *, sort_keys: bool = True) -> None:
    write_private_text_file_atomic(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=sort_keys) + "\n",
    )


# LLM: JSONL 整文件替换与 write_jsonl_records 同格式（每行一条、ensure_ascii=False、可选 sort_keys、尾换行）。
# 函数用途: 以仅本人可读写的权限原子替换一个 JSONL 文件（自取锁）。
def write_private_jsonl_records(path: Path, records: list[dict[str, object]], *, sort_keys: bool = True) -> None:
    content = "\n".join(json.dumps(record, ensure_ascii=False, sort_keys=sort_keys) for record in records)
    write_private_text_file_atomic(path, (content + "\n") if content else "")


# LLM: 追加无法原子替换整文件；这里只保证私有：目录 0700、新文件出生 0600、已有文件先收紧再追加，
#   锁沿用 locked_json_path（与 append_jsonl_records 同一协议）。调用方负责序列化格式。
# 函数用途: 以仅本人可读写的权限向文本文件追加内容（写文件、可能 chmod 文件与目录）。
def append_private_text(path: Path, content: str) -> None:
    ensure_private_dir(path.parent)
    with _locked_json_path(path):
        _ensure_private_file(path)
        _tighten_private_file(path)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(content)


# LLM: 与 append_jsonl_records 同格式（每行一条、可选 sort_keys、批内一次写入），权限走私有 append。
# 函数用途: 以仅本人可读写的权限并发安全地追加一批 JSONL 记录（空批不操作）。
def append_private_jsonl_records(path: Path, records: list[dict[str, object]], *, sort_keys: bool = True) -> None:
    if not records:
        return
    blob = "".join(json.dumps(record, ensure_ascii=False, sort_keys=sort_keys) + "\n" for record in records)
    append_private_text(path, blob)


# LLM: 有界 append 的私有版：与 append_jsonl_capped 同一锁协议与读-改-写语义（只留最近 max_records 条、
#   坏行跳过、max_records<=0 退化为不裁剪整文件重写），权限走私有——目录 0700、新文件出生 0600、
#   已有宽权限文件在整文件重写时被收紧（write_private_text_file_atomic_unlocked 不抄回旧权限）。
#   写入格式与 append_jsonl_capped 逐字节一致（ensure_ascii=False、无 sort_keys、每行尾换行）。
# 函数用途: 以仅本人可读写的权限有界追加一条 JSONL 记录（只保留最近 max_records 条）。
def append_private_jsonl_capped(path: Path, record: dict[str, object], *, max_records: int) -> None:
    ensure_private_dir(path.parent)
    with _locked_json_path(path):
        records = read_jsonl_objects_report(path).records
        records.append(record)
        if max_records > 0:
            records = records[-max_records:]
        content = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records)
        write_private_text_file_atomic_unlocked(path, content)


# LLM: 出生即 0600（os.open 的 mode 不靠 umask 保证）；并发下目标已存在时直接返回，由收紧步骤处理权限。
# 函数用途: 确保私有追加的目标文件存在，新建时权限为 0600。
def _ensure_private_file(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    os.close(descriptor)


# LLM: 只收紧不放松：比 0600 宽（group/other 任一位）才 chmod；失败只放弃收紧，不抛给调用方。
# 函数用途: 把已存在文件的权限收紧到仅本人可读写。
def _tighten_private_file(path: Path) -> None:
    try:
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            os.chmod(path, 0o600)
    except OSError:
        return


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


# LLM: 锁文件必须与数据文件同口径私有：文件 0600、不跟随符号链接，缺失目录按 0700 新建、已存在的目录
#   一律不动（lkp 2026-10-03 修正，与上下文快照的符号链接口径一致）；原来用 Path.open("a+")+mkdir
#   会按 umask 落成 0644/0755，同机他用户能打开并 flock(LOCK_EX) 卡住宿主写入（be 2026-10-03 实测 2158 个 0644）。
#   改成经 open_private_lock_beneath 拿 fd 再 flock；只 flock、不写内容；保留 blocking=False（同一 OS 锁的 LOCK_NB）
#   与进程内线程锁层。已存在的 0644 锁在打开时无条件收紧到 0600（自愈存量），只做在我们已知的锁路径上。
# LLM: 锁文件可能被孤儿清理器删除或替换（gateway 请求锁 sidecar）：拿锁后核对 fd 与锁路径的
#   (st_dev, st_ino) 一致；不一致说明名字已换或已删——在旧 inode 上持锁不再是互斥保证（阻塞迟到者场景），
#   close 后重开重试；超上限按锁竞争失败（BlockingIOError），与既有线程锁失败语义一致。
_JSON_LOCK_IDENTITY_RETRY_COUNT = 5


# 函数用途: 在原同名锁文件上获取排他权，忙碌或异常时关闭文件句柄。
@contextmanager
def _locked_file_path(path: Path, *, blocking: bool = True):
    descriptor = _acquire_verified_lock_descriptor(path, blocking=blocking)
    try:
        yield
    finally:
        _funlock_descriptor(descriptor)
        os.close(descriptor)


# LLM: 取锁重试循环独立成函数（压平临界区的嵌套）：每轮"打开→取锁→核对身份"；身份不符说明锁文件
#   被清理器换掉，关闭后重开重试；取锁本身异常时关闭描述符再冒泡；超上限抛 BlockingIOError。
# 函数用途: 反复取锁直到 fd 与锁路径身份一致，返回已验证的锁描述符。
def _acquire_verified_lock_descriptor(path: Path, *, blocking: bool = True) -> int:
    lock_path = path.with_name(path.name + ".lock")
    for _ in range(_JSON_LOCK_IDENTITY_RETRY_COUNT):
        descriptor = _open_private_lock_descriptor(lock_path)
        try:
            _flock_descriptor(descriptor, blocking=blocking)
        except BaseException:
            os.close(descriptor)
            raise
        if _descriptor_matches_lock_path(descriptor, lock_path):
            return descriptor
        os.close(descriptor)
    raise BlockingIOError("锁文件身份反复变化，放弃取锁")


# 函数用途: 核对锁文件描述符与锁路径当前指向同一个 inode。
def _descriptor_matches_lock_path(descriptor: int, lock_path: Path) -> bool:
    try:
        fd_stat = os.fstat(descriptor)
        path_stat = os.stat(lock_path)
    except OSError:
        return False
    return (fd_stat.st_dev, fd_stat.st_ino) == (path_stat.st_dev, path_stat.st_ino)


# LLM: 调用方给的是完整锁路径；这里把绝对路径拆成“已存在的最近祖先 + 其余缺失段”（与其余锁/目录调用点共用
#   nofollow_fs.split_existing_anchor），让缺失目录仍统一经 no-follow 原语创建（0700；已存在的目录一律不动）。
# 函数用途: 打开一个私有锁文件描述符，顺带把已存在的宽权限锁收紧到 0600。
def _open_private_lock_descriptor(lock_path: Path) -> int:
    lock_path = Path(os.path.abspath(lock_path))
    anchor, missing = split_existing_anchor(lock_path.parent)
    return open_private_lock_beneath_tightened(anchor, (*missing, lock_path.name))


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


# LLM: 与 _flock_exclusive 同一 OS 锁，只是作用于裸描述符（私有锁原语给的是 fd，不是文件对象）。
# 函数用途: 在锁文件描述符上取排他锁，可选择立即报告竞争。
def _flock_descriptor(descriptor: int, *, blocking: bool = True) -> None:
    if fcntl is not None:
        fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
    else:
        from .file_lock_support import warn_file_lock_unavailable_once
        warn_file_lock_unavailable_once()


# LLM: 并发 + 非阻塞模式下阻塞取锁会把调用方的“立即返回”语义变成挂起，故只在非阻塞失败时抛原竞争错误。
# 函数用途: 释放锁文件描述符上的排他锁。
def _funlock_descriptor(descriptor: int) -> None:
    if fcntl is not None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


# LLM: 所有原子写（JSON、JSONL 账本、文本）都经这里替换；替换前先让临时文件带上目标原来的权限位，
#   否则 600 的配置/账本每写一次就按 umask 变成 644（2026-10-02 ae 在 P18 发现生产 desktop.yaml 被放宽）。
#   keep_mode=False 只给私有写（write_private_text_file_atomic_unlocked）：临时文件已是 0600，不能抄回旧的 0644。
# 函数用途: 带重试地把临时文件原子替换到目标位置，默认保留目标原有权限。副作用：替换目标文件。
def _replace_with_retry(tmp: Path, path: Path, *, keep_mode: bool = True) -> None:
    if keep_mode:
        _keep_target_mode(tmp, path)
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


# LLM: 目标不存在（新建）时不改临时文件权限，沿用调用方/umask 的默认；只抄权限位，不改属主。
# 函数用途: 把目标文件现有的权限位抄到临时文件上。副作用：chmod 临时文件。
def _keep_target_mode(tmp: Path, path: Path) -> None:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return
    os.chmod(tmp, mode)


def _unlink_tmp_file(tmp: Path) -> None:
    try:
        tmp.unlink()
    except OSError:
        pass


# LLM: 文件代次指纹守门的整文件行缓存(批4 性能小修;指纹与粗窗口判断共用 common/cache_freshness)。
#   契约:①指纹是 (dev, ino, size, mtime_ns, ctime_ns)——原子替换必然换 inode,只比 mtime+size 会在
#   同一时间片漏掉整代替换;②缓存值是 tuple[str] 不可变行,跨调用方共享零污染;③mtime 距现在不足
#   _CACHE_TRUST_AGE_SECONDS 时只返回本次读到的行、不写缓存,窗口内先读后写的 ABA 不会被缓存住;
#   ④容量上限 FIFO 逐出,防长跑进程缓存无界膨胀。高频轮询的 jsonl 台账(协作收件箱/产物注册表)读路径用它。
_TEXT_LINES_CACHE: dict[str, tuple[tuple[int, int, int, int, int], tuple[str, ...]]] = {}
_TEXT_LINES_CACHE_GUARD = threading.Lock()
# 文本行缓存最多缓存的条目数；超出按 LRU 逐出，防止大目录扫描撑爆内存。
_TEXT_LINES_CACHE_MAX_COUNT = 64


# LLM: 只在文件代次指纹未变时命中；近期写入（窗口内）读到的行不入缓存，下一次调用必然重读。
#   读取失败与 read_text 同语义上抛，不吞异常、不改返回类型；改动须同步 test_common_safe_id_and_paths。
# 函数用途: 读一个 JSONL 文件的记录列表，文件没换代时走进程内缓存；文件刚写过时不缓存本次结果。
def read_text_lines_cached(path: Path) -> tuple[str, ...]:
    stat = path.stat()
    signature = cache_stat_signature(stat)
    key = str(path)
    with _TEXT_LINES_CACHE_GUARD:
        hit = _TEXT_LINES_CACHE.get(key)
        if hit is not None and hit[0] == signature:
            return hit[1]
    lines = read_jsonl_text_lines(path)
    if not cache_freshness.cache_entry_trustworthy(stat.st_mtime_ns):
        return lines
    with _TEXT_LINES_CACHE_GUARD:
        while len(_TEXT_LINES_CACHE) >= _TEXT_LINES_CACHE_MAX_COUNT:
            _TEXT_LINES_CACHE.pop(next(iter(_TEXT_LINES_CACHE)))
        _TEXT_LINES_CACHE[key] = (signature, lines)
    return lines
