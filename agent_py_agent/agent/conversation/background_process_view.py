# LLM: 静态缓存按规范目录单槽、逐文件stat重验、4096条硬上限；坏记录仅凭严格一致归属隔离，不凭年龄忽略错误。
# 模块用途: 给TUI与插件提供严格归属校验的只读进程观察，有界重试目录换代，保留真实错误分类与实时状态。
from __future__ import annotations

import errno
import json
import math
import os
import stat
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path

from ..common.cache_freshness import cache_entry_trustworthy
from ..common.nofollow_fs import (
    NoFollowPathError,
    _portable_directory,
    _supports_dir_fd,
    open_directory_beneath,
    open_readonly_file_beneath,
    read_bytes_beneath,
)
from ..tooling.process_registry import process_registry
from ..tooling.process_session_records import validate_process_record, validate_session_id
from ..tooling.process_session_store import process_session_store_root
from .process_events import _managed_instance_still_live

_TERMINAL = frozenset({"exited", "failed", "killed", "completed", "not_started"})
# 只读后台进程投影最多读取的日志尾部字节数。
_PROCESS_LOG_TAIL_BYTES = 64 * 1024
# 后台进程只读视图最多缓存的规范目录条数，每个目录只占一槽。
_PROCESS_VIEW_DIR_CACHE_MAX_ENTRIES = 64
# 后台进程只读视图最多缓存的单文件快照条数。
_FILE_CACHE_MAX_ENTRIES = 2048
# 单个后台记录目录允许观察的记录数量上限，超限失败关闭。
_MAX_DIRECTORY_RECORDS_COUNT = 4096
# 后台静态快照必须重新核验的最长秒数，热命中不延长有效期。
_CACHE_RECHECK_SECONDS = 60.0
# 显式换代或扫描边界瞬态异常总共最多尝试四次，每次重试前退避5毫秒。
_MAX_SCAN_ATTEMPTS = 4
_DIR_SCAN_RETRY_BACKOFF_SECONDS = 0.005
_TRANSIENT_SCAN_ERRNOS = frozenset({errno.EINTR, errno.EAGAIN, errno.ENOENT, getattr(errno, "ESTALE", errno.ENOENT), errno.ESRCH})
_PERMISSION_SCAN_ERRNOS = frozenset({errno.EACCES, errno.EPERM})
_SCOPE_FIELDS = ("access_scope", "execution_scope", "activation_scope", "completion_target", "metadata", "scope")
_ATTRIBUTION_FIELDS = ("owner_id", "thread_id", "conversation_id", "owner_home", "task_id", "request_id")


# LLM: 仅owner错配/冲突/非法owner声明使用此ValueError子类；稳定双字段供列表投影和详情异常识别，不按文案猜权限。
# 类用途: 保留真实reader的owner拒绝语义，同时兼容原有ValueError捕获链。
class _OwnerMismatchError(ValueError):
    error_code = "OWNER_MISMATCH"
    reason_code = "OWNER_MISMATCH"


# LLM: 保留文件名身份及实际stat代次；失败仅存归属字符串/非法标记和errno，不保存正文或猜测瞬态性质。
# 类用途: 保存静态记录、无正文错误与实际复核时间，不保存动态探活或会话过滤结论。
@dataclass(frozen=True)
class _SessionSnapshot:
    session_id: str
    payload: dict | None = None
    error: str = ""
    mtime_ns: int = 0
    cacheable: bool = False
    verified_at: float = 0.0
    stat_key: tuple[int, ...] = ()
    error_code: str = ""
    error_errno: int | None = None


# LLM: cached_at沿最老文件复核时间传递，不因目录换代续租；目录代次放值内，键只绑定规范目录身份。
# 类用途: 保存一次目录或单文件复核的快照与单调时钟时间。
@dataclass(frozen=True)
class _ViewCacheEntry:
    snapshots: tuple[_SessionSnapshot, ...]
    cached_at: float
    dir_stat_key: tuple[int, ...] = ()


_DIRECTORY_CACHE: OrderedDict[tuple, _ViewCacheEntry] = OrderedDict()
_FILE_CACHE: OrderedDict[tuple, _ViewCacheEntry] = OrderedDict()
_VIEW_CACHE_LOCK = threading.Lock()


# LLM: 这些字段仅作展示，不授予进程输入、停止或完成通知消费权限。
# 类用途: 保存一项后台任务的公开概要。
@dataclass(frozen=True)
class BackgroundProcessSummary:
    session_id: str
    command: str = ""
    status: str = "unknown"
    pid: int = 0
    cwd: str = ""
    thread_id: str = ""
    task_id: str = ""
    run_id: str = ""
    started_at: float | str = 0.0
    elapsed_seconds: float | None = None
    exit_code: int | None = None
    log_path: str = ""
    alive: bool = False
    wake_pending: bool = False

    # LLM: 返回独立标量字典，不暴露可写的原始记录。
    # 函数用途: 供 JSON 接口和导航器读取概要字段。
    def to_dict(self) -> dict[str, object]:
        return asdict(self)


# LLM: 日志只是观察数据，不解释为控制指令或业务状态。
# 类用途: 在概要外附带有界输出尾部。
@dataclass(frozen=True)
class BackgroundProcessDetail(BackgroundProcessSummary):
    log_tail: tuple[str, ...] = ()


# LLM: owner拒绝在静态和动态概要两条出口保留双字段；严格归属隔离不变，普通ValueError不升级，不泄漏异常正文。
# 函数用途: 返回本范围进程及结构化诊断，让真实owner错配能触发消费端清帧。
def list_background_processes(owner_home: Path, *, thread_id: str | None = None, active_only: bool = False) -> dict[str, object]:
    home = Path(owner_home).expanduser().absolute()
    items, errors, unrelated_errors = [], [], []
    now = time.time()
    for root, snapshot in _owner_session_snapshots(home):
        if snapshot.error:
            destination = unrelated_errors if _unrelated_error(snapshot, home, thread_id, now) else errors
            diagnostic = {"session_id": snapshot.session_id, "error": snapshot.error}
            if snapshot.error_code:
                diagnostic["error_code"] = snapshot.error_code
                if snapshot.error_code == "OWNER_MISMATCH":
                    diagnostic["reason_code"] = snapshot.error_code
            if snapshot.error_errno is not None:
                diagnostic["errno"] = snapshot.error_errno
            destination.append(diagnostic)
            continue
        try:
            payload = snapshot.payload
            if thread_id and _record_thread_id(payload) != thread_id:
                continue
            items.append(_summary(root, payload).to_dict())
        except (OSError, TypeError, ValueError) as exc:
            diagnostic = {"session_id": snapshot.session_id, "error": type(exc).__name__}
            code = exc.error_code if isinstance(exc, _OwnerMismatchError) else ""
            if code:
                diagnostic.update(error_code=code, reason_code=code)
            if isinstance(exc, OSError) and exc.errno is not None:
                diagnostic["errno"] = exc.errno
            failed = replace(snapshot, error=type(exc).__name__, error_code=code)
            destination = unrelated_errors if _unrelated_error(failed, home, thread_id, now) else errors
            destination.append(diagnostic)
    active = [item for item in items if item["alive"] or item["wake_pending"]]
    return {"items": active if active_only else items, "active_count": len(active),
            "total": len(items), "errors": errors, "projection_ok": not errors,
            "unrelated_errors": unrelated_errors, "unscoped_error_count": len(unrelated_errors)}


# LLM: 只清本模块易失缓存，不读取Store、不恢复redo、不改盘；测试或宿主可明确失效两级快照。
# 函数用途: 在线程锁内清空后台只读观察缓存。
def _clear_background_process_view_cache() -> None:
    with _VIEW_CACHE_LOCK:
        _DIRECTORY_CACHE.clear()
        _FILE_CACHE.clear()


# LLM: 查找与LRU提级同锁，返回仅限模块内部使用；IO和深拷贝不占全局锁，调用方不得修改缓存对象。
# 函数用途: 读取某代缓存项但不刷新其复核时间。
def _lookup_view_cache(cache, key):
    with _VIEW_CACHE_LOCK:
        entry = cache.get(key)
        if entry is not None:
            cache.move_to_end(key)
        return entry


# LLM: 发表前深拷贝，目录指纹保存在值内；锁内替换同一目录最新代并逐出，超深记录只放弃优化不改观察。
# 函数用途: 保存有界快照并返回是否成功入缓存，缓存不能处理的对象仍走实时只读观察。
def _store_view_cache(cache, key, snapshots, verified_at, limit, *, dir_stat_key=()):
    try:
        entry = _ViewCacheEntry(deepcopy(snapshots), verified_at, dir_stat_key)
    except RecursionError:
        return False
    with _VIEW_CACHE_LOCK:
        cache[key] = entry
        cache.move_to_end(key)
        while len(cache) > limit:
            cache.popitem(last=False)
    return True


# LLM: 原nofollow入口只读打开真实根，权限/链接/类型错误不伪装成缺失；FD用完立即关闭。
# 函数用途: 读取目录dev/ino/mtime指纹，同时确认它仍可安全打开。
def _directory_stat_key(root: Path) -> tuple[int, int, int]:
    if not _supports_dir_fd():
        metadata = _portable_directory(root, (), create=False).lstat()
        return metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns
    descriptor = open_directory_beneath(root, ())
    try:
        metadata = os.fstat(descriptor)
        return metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns
    finally:
        os.close(descriptor)


# LLM: 目录错误与同名坏文件不得去重；仅有效记录按原优先级保留首项，错误保留真实errno，不将未知错误解释为瞬态。
# 函数用途: 遍历本owner的快照，保留全部只读错误并去重成功展示项。
def _owner_session_snapshots(home: Path):
    seen = set()
    for root in _session_directories(home):
        try:
            snapshots = _directory_snapshots(root, home)
        except (OSError, TypeError, ValueError) as exc:
            snapshots = (_SessionSnapshot(str(root), error=type(exc).__name__,
                error_errno=exc.errno if isinstance(exc, OSError) else None),)
        for snapshot in snapshots:
            if not snapshot.error and snapshot.session_id in seen:
                continue
            if not snapshot.error:
                seen.add(snapshot.session_id)
            yield root, snapshot


# LLM: 最多收集4096个候选名；第4097个即失败关闭，避免扫描时先构造无界名字列表或超大快照。
# 函数用途: 有界列出后台记录名，超限返回None，不解析文件或写盘。
def _directory_record_names(root: Path):
    names = []
    with os.scandir(root) as entries:
        for item in entries:
            if not (item.name.startswith("bg-") and item.name.endswith(".json")):
                continue
            if len(names) == _MAX_DIRECTORY_RECORDS_COUNT:
                return None
            names.append(item.name)
    return sorted(names)


# LLM: 只复用stat代次未变且仍可信的单项，所有改变/拒绝交原reader校验；返回对象已由目录调用方深拷贝隔离。
# 函数用途: 热目录逐文件查元数据，只重新解析实际变化的文件。
def _revalidate_cached_file(root, home, snapshot):
    try:
        current = _file_stat_key(root / f"{snapshot.session_id}.json")
    except OSError:
        current = None
    if current == snapshot.stat_key and current is not None and cache_entry_trustworthy(current[2]):
        return snapshot
    return _file_snapshot(root, snapshot.session_id, home)


# LLM: 目录级错误不得归属隔离；锁内移除失效投影，异常仅保留类型/errno，不保存正文或写磁盘。
# 函数用途: 丢弃此目录旧缓存并报告超限、换代或明确权限拒绝。
def _directory_projection_error(root, key, code, *, failure: OSError | None = None):
    with _VIEW_CACHE_LOCK:
        _DIRECTORY_CACHE.pop(key, None)
    return (_SessionSnapshot(str(root), error=type(failure).__name__ if failure is not None else code,
                             error_code=code, error_errno=failure.errno if failure is not None else None),)


# LLM: 换代及准确瞬态异常共用四次完整尝试；权限优先立即失败，未知错误透传，不拿旧帧冒充稳定扫描。
# 函数用途: 在扫描边界竞争时最多退避重试三次，耗尽统一报告目录读取期间换代。
def _directory_snapshots(root: Path, home: Path) -> tuple[_SessionSnapshot, ...]:
    for attempt in range(_MAX_SCAN_ATTEMPTS):
        try:
            snapshots = _directory_snapshot_attempt(root, home, allow_initial_missing=attempt == 0)
        except OSError as exc:
            denied = isinstance(exc, PermissionError) or exc.errno in _PERMISSION_SCAN_ERRNOS
            if not denied and not (isinstance(exc, (FileNotFoundError, ProcessLookupError)) or exc.errno in _TRANSIENT_SCAN_ERRNOS):
                raise
            try:
                key = (str(home.resolve()), str(root.resolve()))
            except OSError:
                # 规范化本身也被拒绝时保留最初错误，不让额外文件系统访问掩盖它。
                key = (str(home), str(root))
            code = "PERMISSION_DENIED" if denied else "DIRECTORY_CHANGED_DURING_READ"
            snapshots = _directory_projection_error(root, key, code, failure=exc)
        if not any(item.error_code == "DIRECTORY_CHANGED_DURING_READ" for item in snapshots):
            return snapshots
        if attempt + 1 < _MAX_SCAN_ATTEMPTS:
            time.sleep(_DIR_SCAN_RETRY_BACKOFF_SECONDS)
    return snapshots


# LLM: 单槽、stat/nofollow、上限和cached_at不变；只有整轮首次可把缺目录当空态，重试缺失仍须上抛竞争。
# 函数用途: 执行一次安全扫描，区分初次不存在与读后消失，不改变缓存复核期限。
def _directory_snapshot_attempt(root: Path, home: Path, *, allow_initial_missing: bool = True) -> tuple[_SessionSnapshot, ...]:
    try:
        before = _directory_stat_key(root)
    except FileNotFoundError:
        if not allow_initial_missing:
            raise
        return ()
    now = time.monotonic()
    trusted = cache_entry_trustworthy(before[2])
    key = (str(home.resolve()), str(root.resolve()))
    entry = _lookup_view_cache(_DIRECTORY_CACHE, key)
    expired = entry is not None and not 0 <= now - entry.cached_at < _CACHE_RECHECK_SECONDS
    if trusted and entry is not None and entry.dir_stat_key == before and not expired:
        snapshots = tuple(_revalidate_cached_file(root, home, snapshot) for snapshot in deepcopy(entry.snapshots))
        if _directory_stat_key(root) != before:
            snapshots = None
    else:
        snapshots = None
    observed_at = entry.cached_at if snapshots is not None else now
    if snapshots is None:
        names = _directory_record_names(root)
        if names is None:
            return _directory_projection_error(root, key, "DIRECTORY_RECORD_LIMIT_EXCEEDED")
        snapshots = tuple(_file_snapshot(root, name[:-5], home, force_read=expired) for name in names)
    after = _directory_stat_key(root)
    if after != before:
        oversized = _directory_record_names(root) is None
        code = "DIRECTORY_RECORD_LIMIT_EXCEEDED" if oversized else "DIRECTORY_CHANGED_DURING_READ"
        return _directory_projection_error(root, key, code)
    if trusted and all(item.cacheable for item in snapshots):
        verified_at = min(observed_at, min((item.verified_at for item in snapshots), default=observed_at))
        _store_view_cache(_DIRECTORY_CACHE, key, snapshots, verified_at, _PROCESS_VIEW_DIR_CACHE_MAX_ENTRIES, dir_stat_key=before)
    return snapshots


# LLM: 类型及多链接门与原reader一致；mtime/size之外加ctime防同inode原位改写，键同时绑定home防跨owner复用。
# 函数用途: 取普通单链接元数据文件的安全代次指纹。
def _file_stat_key(path: Path) -> tuple[int, ...]:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise NoFollowPathError("process record is not a regular single-link file")
    return metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns, metadata.st_size, metadata.st_ctime_ns


# LLM: 仅复用同代可信文件；owner专用异常保留结构化code及原归属，普通错误不误标权限，不缓存动态探活。
# 函数用途: 读取真实记录并保留owner拒绝分类，让缓存和实时列表具有相同错误语义。
def _file_snapshot(root: Path, session_id: str, home: Path, *, force_read: bool = False) -> _SessionSnapshot:
    path, now = root / f"{session_id}.json", time.monotonic()
    payload, before, trusted = None, None, False
    try:
        before = _file_stat_key(path)
        trusted = cache_entry_trustworthy(before[2])
        key = (str(home), str(root), session_id, *before)
        entry = _lookup_view_cache(_FILE_CACHE, key)
        if trusted and not force_read and entry is not None and 0 <= now - entry.cached_at < _CACHE_RECHECK_SECONDS:
            snapshot = deepcopy(entry.snapshots[0])
            if _file_stat_key(path) == before:
                return snapshot
        raw = _read_session_payload(root, session_id)
        payload = raw if isinstance(raw, dict) else None
        payload = _validate_session_payload(raw, session_id, home)
        snapshot = _SessionSnapshot(session_id, payload, mtime_ns=before[2], verified_at=now)
    except (OSError, TypeError, ValueError, RecursionError) as exc:
        snapshot = _SessionSnapshot(session_id, _error_scope_payload(payload), type(exc).__name__,
                                    before[2] if before else 0, verified_at=now,
                                    error_code=exc.error_code if isinstance(exc, _OwnerMismatchError) else "",
                                    error_errno=exc.errno if isinstance(exc, OSError) else None)
    try:
        after = _file_stat_key(path)
    except OSError:
        after = None
    if snapshot.error:
        snapshot = replace(snapshot, mtime_ns=after[2] if after else 0)
    if trusted and after == before:
        snapshot = replace(snapshot, cacheable=True, stat_key=before)
    if snapshot.cacheable and not _store_view_cache(_FILE_CACHE, key, (snapshot,), now, _FILE_CACHE_MAX_ENTRIES):
        snapshot = replace(snapshot, cacheable=False)
    return snapshot


# LLM: 坏JSON仅保留归属字符串；非字符串保留None反证，坏scope也保留标记；不复制任意嵌套正文。
# 函数用途: 从校验失败的原字典提取有界深度的线程和owner诊断信息。
def _error_scope_payload(payload: object) -> dict | None:
    if not isinstance(payload, dict):
        return None
    result = {}
    for name in ("", *_SCOPE_FIELDS):
        if name and name not in payload:
            continue
        scope = payload if not name else payload.get(name)
        if not isinstance(scope, dict):
            result[name] = None
            continue
        fields = {key: scope[key] if isinstance(scope[key], str) else None
                  for key in _ATTRIBUTION_FIELDS if key in scope}
        if name:
            result[name] = fields
        else:
            result.update(fields)
    return result


# LLM: 正常校验与错误诊断共用原JSON字典位置；只枚举字典，不在此转换归属类型或丢弃原字符串差异。
# 函数用途: 提取顶层与含activation的六类scope，供严格验证和只读线程提取使用。
def _raw_record_scopes(payload: object) -> tuple[dict, ...]:
    if not isinstance(payload, dict):
        return ()
    return (payload, *(payload[key] for key in _SCOPE_FIELDS if isinstance(payload.get(key), dict)))


# LLM: 空观察线程也可隔离严格确认的非空其他线程；坏记录的空串/坏类型/owner或激活归属冲突仍不得洗白。
# 函数用途: 每次观察重新判断坏文件是否与当前会话无关。
def _unrelated_error(snapshot: _SessionSnapshot, home: Path, thread_id: str | None, now: float) -> bool:
    if thread_id is not None and not isinstance(thread_id, str) or not isinstance(snapshot.payload, dict):
        return False
    if any(key in snapshot.payload and not isinstance(snapshot.payload[key], dict) for key in _SCOPE_FIELDS):
        return False
    scopes = _raw_record_scopes(snapshot.payload)
    claims = {key: set() for key in _ATTRIBUTION_FIELDS}
    for scope in scopes:
        for key in _ATTRIBUTION_FIELDS:
            if key not in scope:
                continue
            value = scope[key]
            if not isinstance(value, str) or not value.strip():
                return False
            claims[key].add(value)
    threads = claims["thread_id"] | claims["conversation_id"]
    try:
        owners = {str(Path(value).expanduser().resolve()) for value in claims["owner_home"]}
    except (OSError, ValueError, RuntimeError):
        return False
    if len(threads) > 1 or len(owners) > 1 or any(len(claims[key]) > 1 for key in ("owner_id", "owner_home", "task_id", "request_id")):
        return False
    if thread_id and thread_id.strip() in {value.strip() for value in threads}:
        return False
    return bool(threads or owners and str(home.resolve()) not in owners)


# LLM: 先校验session与owner；显式指定thread_id时严格要求同线程匹配并在存活探针/日志读取前拒绝跨线程或无归属记录。
# 函数用途: 在可选当前会话范围内只读查看后台进程，日志仍限制路径和字节数。
def inspect_background_process(owner_home: Path, session_id: str, *, thread_id: str | None = None, tail_lines: int = 80) -> dict[str, object]:
    selected = validate_session_id(session_id)
    home = Path(owner_home).expanduser().absolute()
    for root in _session_directories(home):
        try:
            payload = _read_session(root, selected, home)
        except FileNotFoundError:
            continue
        if thread_id and _record_thread_id(payload) != thread_id:
            raise KeyError(selected)
        summary = _summary(root, payload)
        detail = summary.to_dict()
        detail["log_tail"] = _log_tail(home, root, selected, payload, tail_lines)
        return detail
    return {"session_id": selected, "status": "not_found", "log_tail": [], "error_code": "PROCESS_SESSION_NOT_FOUND"}


# LLM: 固定 owner 哈希权威目录优先；附加目录只取原 registry 已登记且位于本 owner 下的路径，不搜索其它 owner。
# 函数用途: 列出原受管目录及文档约定目录，不创建目录或新 registry。
def _session_directories(home: Path) -> tuple[Path, ...]:
    roots = [process_session_store_root(home, home), home / "workspace/runtime/processes/sessions",
             home / ".background_jobs/process_sessions"]
    with process_registry._lock:
        registered = {Path(record.store_root) for record in process_registry._processes.values()}
    roots.extend(sorted(root for root in registered if root.is_relative_to(home)))
    return tuple(dict.fromkeys(roots))


# LLM: 详情始终沿同一只读解析/校验链，不调用Store.load/transaction或复用动态日志；身份验证在内容返回前完成。
# 函数用途: 为详情读取并验证一条当前进程元数据。
def _read_session(root: Path, session_id: str, home: Path) -> dict:
    selected = validate_session_id(session_id)
    return _validate_session_payload(_read_session_payload(root, selected), selected, home)


# LLM: JSON只读有界1MiB，原nofollow链禁止链接/危险类型，不恢复事务或创建目录；坏JSON沿原异常报告。
# 函数用途: 读取一条原始JSON，保留校验失败前可用的归属信息。
def _read_session_payload(root: Path, session_id: str) -> object:
    selected = validate_session_id(session_id)
    raw = read_bytes_beneath(root, (f"{selected}.json",), max_bytes=1024 * 1024)
    if raw is None:
        raise FileNotFoundError(selected)
    return json.loads(raw)


# LLM: 原接受/拒绝范围不变；先完成可读取owner声明的校验，普通scope/属性错误延后，不能遮盖明确owner拒绝。
# 函数用途: 精确区分owner归属拒绝与其它元数据错误，供列表和详情共享。
def _validate_session_attribution(payload: dict, *, home: Path | None = None) -> None:
    invalid_scope = any(payload.get(key) is not None and not isinstance(payload[key], dict) for key in _SCOPE_FIELDS)
    invalid_attribution = False
    for name in ("access_scope", "execution_scope", "activation_scope"):
        scope = payload.get(name)
        if isinstance(scope, dict) and (
            scope.get("owner_home") == "" or name == "activation_scope" and scope.get("owner_id") == ""
        ):
            raise _OwnerMismatchError("invalid_session_attribution")
    claims = {key: set() for key in _ATTRIBUTION_FIELDS}
    for scope in _raw_record_scopes(payload):
        for key in _ATTRIBUTION_FIELDS:
            value = scope.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                if key in {"owner_id", "owner_home"}:
                    raise _OwnerMismatchError("invalid_session_attribution")
                invalid_attribution = True
                continue
            if value == "":
                continue
            if not value.strip():
                if key in {"owner_id", "owner_home"}:
                    raise _OwnerMismatchError("invalid_session_attribution")
                invalid_attribution = True
                continue
            claims[key].add(value)
    threads = claims["thread_id"] | claims["conversation_id"]
    if any(len(claims[key]) > 1 for key in ("owner_id", "owner_home")):
        raise _OwnerMismatchError("conflicting_session_attribution")
    if home is not None:
        _validate_session_owner_home(payload, home)
    if invalid_scope:
        raise ValueError("invalid_session_scope")
    if invalid_attribution:
        raise ValueError("invalid_session_attribution")
    if len(threads) > 1 or any(len(claims[key]) > 1 for key in ("task_id", "request_id")):
        raise ValueError("conflicting_session_attribution")


# LLM: schema前后均校验归属和home；明确跨owner不能被坏schema降级，普通元数据错误不升级，列表/详情同链。
# 函数用途: 验证真实或待缓存元数据，并将跨owner错配显式传给上层。
def _validate_session_payload(payload: object, selected: str, home: Path) -> dict:
    if not isinstance(payload, dict) or payload.get("session_id") != selected:
        raise ValueError("process session identity mismatch")
    _validate_session_attribution(payload, home=home)
    if payload.get("schema"):
        payload = validate_process_record(payload)
        _validate_session_attribution(payload, home=home)
    return payload


# LLM: 仅检查已验证owner字段的原home声明，原边界不放宽；普通错误尚可待报，不能遮盖已明确的跨owner拒绝。
# 函数用途: 将真实owner_home错配稳定标成OWNER_MISMATCH，不读其它owner文件。
def _validate_session_owner_home(payload: dict, home: Path) -> None:
    owners = [scope["owner_home"] for scope in _raw_record_scopes(payload) if scope.get("owner_home")]
    if any(not isinstance(owner, str) or Path(owner).absolute() != home for owner in owners):
        raise _OwnerMismatchError("process owner mismatch")


# LLM: 列表/详情输入已严格验证；直接调用也只取非空str，永不将非字符串转换为线程身份，冲突不猜归属。
# 函数用途: 为会话过滤和概要提取原始字符串线程身份，无有效绑定返回空串。
def _record_thread_id(payload: dict) -> str:
    threads = {scope[key] for scope in _raw_record_scopes(payload) for key in ("thread_id", "conversation_id")
               if isinstance(scope.get(key), str) and scope[key].strip()}
    if len(threads) > 1:
        raise ValueError("conflicting_session_attribution")
    return next(iter(threads), "")


# LLM: 非有限或畸形时间保持不可用，不让旧文案或当前时刻冒充原开始。
# 函数用途: 解析数值或 ISO 时间供耗时展示。
def _timestamp(value: object) -> float:
    try:
        number = float(value) if isinstance(value, (int, float)) else datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        return number if math.isfinite(number) and number >= 0 else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


# LLM: 终态不探测PID；包括unknown的非终态每轮核原实例，线程身份共用reader，不因探活成功改写业务状态或消费通知。
# 函数用途: 为静态快照实时计算存活与耗时，保留原unknown及待唤醒事实。
def _summary(root: Path, payload: dict) -> BackgroundProcessSummary:
    status = str(payload.get("status") or "unknown")
    sid = str(payload["session_id"])
    pid = payload.get("pid") or payload.get("launcher_pid") or 0
    if type(pid) is not int or pid < 0:
        raise ValueError("invalid process pid")
    alive = status not in _TERMINAL and _managed_instance_still_live(sid, root, payload)
    if status not in _TERMINAL and not alive:
        status = "unknown"
    target = payload.get("completion_target") or {}
    execution = payload.get("execution_scope") or {}
    start = _timestamp(payload.get("started_at"))
    end = _timestamp(payload.get("finished_at") or payload.get("completed_at"))
    elapsed = max(0.0, (end or time.time()) - start) if start and (end or status not in _TERMINAL) else None
    wake_pending = status == "exited" and bool(target) and not payload.get("completion_notice_id") and not payload.get("stop_requested")
    return BackgroundProcessSummary(session_id=sid, command=str(payload.get("command") or ""), status=status,
        pid=pid, cwd=str(payload.get("cwd") or ""), thread_id=_record_thread_id(payload),
        task_id=str(execution.get("root_task_id") or target.get("task_id") or payload.get("task_id") or ""),
        run_id=str(execution.get("run_id") or target.get("run_id") or payload.get("run_id") or ""),
        started_at=payload.get("started_at") or 0.0, elapsed_seconds=elapsed,
        exit_code=payload.get("exit_code") if type(payload.get("exit_code")) is int else None,
        log_path=str(payload.get("output_path") or payload.get("log_path") or payload.get("output_file") or ""),
        alive=bool(alive), wake_pending=bool(wake_pending))


# LLM: 日志只允许 owner 下或本 owner 的权威 session 目录下；nofollow 拒链接/设备/FIFO，最多从文件尾读 64KiB。
# 函数用途: 返回有限行的 UTF-8 输出尾部；不打开任意外部路径，不解释终端控制码。
def _log_tail(home: Path, root: Path, session_id: str, payload: dict, tail_lines: int) -> list[str]:
    count = max(0, min(1000, int(tail_lines)))
    if not count:
        return []
    declared = payload.get("output_path") or payload.get("log_path") or payload.get("output_file")
    candidates = [Path(str(declared))] if declared else [root / f"{session_id}.log", root / f"{session_id}.out"]
    for path in candidates:
        path = path if path.is_absolute() else root / path
        anchor = home if path.is_relative_to(home) else root if path.is_relative_to(root) else None
        if anchor is None:
            raise ValueError("process log outside owner boundary")
        try:
            fd = open_readonly_file_beneath(anchor, path.relative_to(anchor).parts)
        except FileNotFoundError:
            continue
        with os.fdopen(fd, "rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            offset = max(0, size - _PROCESS_LOG_TAIL_BYTES)
            stream.seek(offset)
            content = stream.read(_PROCESS_LOG_TAIL_BYTES)
        if offset:
            # 窗口从一条长行中部开始时舍掉残片；不能把残片冒充完整日志行。
            content = content.partition(b"\n")[2]
        return content.decode("utf-8", errors="replace").splitlines()[-count:]
    return []


# LLM: 只接受有准确 session_id 的有界标量展示行，不持有原 metadata 或控制句柄；最多显示 128 项。
# 函数用途: 为导航器清洗进程快照，日志总长保持每项 64KiB 内。
def background_watch_rows(value: object) -> tuple[dict, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    rows, seen = [], set()
    fields = tuple(BackgroundProcessSummary.__dataclass_fields__)
    for raw in value[:128]:
        if not isinstance(raw, Mapping):
            continue
        try:
            sid = validate_session_id(raw.get("session_id"))
        except ValueError:
            continue
        if sid in seen:
            continue
        row = {key: raw[key] for key in fields if key in raw and isinstance(raw[key], (str, int, float, bool, type(None)))}
        tail = raw.get("log_tail")
        row["log_tail"] = "\n".join(str(line) for line in tail[-80:])[-_PROCESS_LOG_TAIL_BYTES:].splitlines() if isinstance(tail, (list, tuple)) else []
        rows.append(row)
        seen.add(sid)
    return tuple(rows)


# LLM: 纯展示，运行数来自范围内alive/wake_pending；不宣称跨会话全量可见，文字不驱动生命周期。
# 函数用途: 生成当前可见范围的后台标题、任务概要与选中项日志。
def background_watch_text(rows: tuple[dict, ...], selected: str, *, active_count: int | None = None) -> tuple[str, ...]:
    active = active_count if active_count is not None else sum(row.get("alive") is True or row.get("wake_pending") is True for row in rows)
    lines = [f"⚡ 后台驻留监控（后台进程 {active} 个运行中） · Ctrl+G / Esc 返回", "当前可见范围 · 只读观察 · 结束或异常时由宿主原通知链唤醒主代理"]
    for row in rows:
        sid = str(row["session_id"])
        chosen = selected == f"bg:{sid}"
        status = "进程存活运行中" if row.get("alive") else "刚退出待唤醒" if row.get("wake_pending") else str(row.get("status") or "unknown")
        elapsed = f"{int(_timestamp(row.get('elapsed_seconds')))}s" if row.get("elapsed_seconds") is not None else "耗时未知"
        lines.append(f"{'›' if chosen else ' '} {sid} · PID {row.get('pid', 0)} · {status} · {elapsed}")
        lines.append(str(row.get("command") or ""))
        if chosen:
            lines.extend(str(line) for line in row.get("log_tail", []))
    return tuple(lines)
