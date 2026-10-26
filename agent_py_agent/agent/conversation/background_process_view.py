# LLM: 只读缓存已校验静态记录及坏文件诊断；两级有界、粗时间窗与60秒全读兜底，不缓存存活/耗时/日志或消费唤醒。
# 模块用途: 给TUI与插件提供不写盘的进程观察，隔离无关坏文件并实时计算当前会话动态状态。
from __future__ import annotations

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
_TAIL_BYTES = 64 * 1024
_DIRECTORY_CACHE_MAX_ENTRIES = 64
_FILE_CACHE_MAX_ENTRIES = 2048
_CACHE_RECHECK_SECONDS = 60.0
_UNSCOPED_CORRUPT_STALE_SECONDS = 60.0
_SCOPE_FIELDS = ("access_scope", "execution_scope", "completion_target", "metadata", "scope")


# LLM: 成功保存校验快照，失败仅保存原归属标量；verified_at沿文件复核传递，目录换代不能续租旧文件。
# 类用途: 保存静态记录、无正文错误与实际复核时间，不保存动态探活或会话过滤结论。
@dataclass(frozen=True)
class _SessionSnapshot:
    session_id: str
    payload: dict | None = None
    error: str = ""
    mtime_ns: int = 0
    cacheable: bool = False
    verified_at: float = 0.0


# LLM: verified_at固定在本轮读取开始，命中不延长TTL；缓存与返回对象必须深拷贝隔离。
# 类用途: 保存一次目录或单文件复核的快照与单调时钟时间。
@dataclass(frozen=True)
class _ViewCacheEntry:
    snapshots: tuple[_SessionSnapshot, ...]
    verified_at: float


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


# LLM: 仅缓存静态快照；线程过滤先于动态探活。坏文件逐轮按原归属/年龄分类，目录错误及当前线程错误不吞。
# 函数用途: 返回当前范围的实时进程概要与分范围诊断，缺失目录不创建、不消费通知。
def list_background_processes(owner_home: Path, *, thread_id: str | None = None, active_only: bool = False) -> dict[str, object]:
    home = Path(owner_home).expanduser().absolute()
    items, errors, unrelated_errors = [], [], []
    now = time.time()
    for root, snapshot in _owner_session_snapshots(home):
        if snapshot.error:
            destination = unrelated_errors if _unrelated_error(snapshot, home, thread_id, now) else errors
            destination.append({"session_id": snapshot.session_id, "error": snapshot.error})
            continue
        try:
            payload = snapshot.payload
            if thread_id and _record_thread_id(payload) != thread_id:
                continue
            items.append(_summary(root, payload).to_dict())
        except (OSError, TypeError, ValueError) as exc:
            errors.append({"session_id": snapshot.session_id, "error": type(exc).__name__})
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


# LLM: 发表前深拷贝；超深合法旧记录只放弃优化，不能让缓存递归错误中断原观察。锁内替换逐出且不改验证时间。
# 函数用途: 保存有界快照并返回是否成功入缓存，缓存不能处理的对象仍走实时只读观察。
def _store_view_cache(cache, key, snapshots, verified_at, limit):
    try:
        entry = _ViewCacheEntry(deepcopy(snapshots), verified_at)
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
    descriptor = open_directory_beneath(root, ())
    try:
        metadata = os.fstat(descriptor)
        return metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns
    finally:
        os.close(descriptor)


# LLM: 目录错误与同名坏文件诊断不得去重；仅有效记录按原目录优先级保留首项，坏副本不能遮住有效行。
# 函数用途: 遍历本owner的快照，保留全部只读错误并去重成功展示项。
def _owner_session_snapshots(home: Path):
    seen = set()
    for root in _session_directories(home):
        try:
            snapshots = _directory_snapshots(root, home)
        except (OSError, TypeError, ValueError) as exc:
            snapshots = (_SessionSnapshot(str(root), error=type(exc).__name__),)
        for snapshot in snapshots:
            if not snapshot.error and snapshot.session_id in seen:
                continue
            if not snapshot.error:
                seen.add(snapshot.session_id)
            yield root, snapshot


# LLM: 目录命中须前后同代、读取前可信且未满60秒；验证时刻取最老文件，换代不续租，到期穿透文件缓存。
# 函数用途: 按目录身份复用静态快照，变化时仅重读必要文件，仍保证原文件复核期限。
def _directory_snapshots(root: Path, home: Path) -> tuple[_SessionSnapshot, ...]:
    try:
        before = _directory_stat_key(root)
    except FileNotFoundError:
        return ()
    now = time.monotonic()
    trusted = cache_entry_trustworthy(before[2])
    key = (str(home), str(root), *before)
    entry = _lookup_view_cache(_DIRECTORY_CACHE, key)
    expired = entry is not None and not 0 <= now - entry.verified_at < _CACHE_RECHECK_SECONDS
    if trusted and entry is not None and not expired:
        snapshots = deepcopy(entry.snapshots)
        if _directory_stat_key(root) == before:
            return snapshots
    with os.scandir(root) as entries:
        names = sorted(item.name for item in entries if item.name.startswith("bg-") and item.name.endswith(".json"))
    snapshots = tuple(_file_snapshot(root, name[:-5], home, force_read=expired) for name in names)
    after = _directory_stat_key(root)
    if trusted and after == before and all(item.cacheable for item in snapshots):
        verified_at = min((item.verified_at for item in snapshots), default=now)
        _store_view_cache(_DIRECTORY_CACHE, key, snapshots, verified_at, _DIRECTORY_CACHE_MAX_ENTRIES)
    return snapshots


# LLM: 类型及多链接门与原reader一致；mtime/size之外加ctime防同inode原位改写，键同时绑定home防跨owner复用。
# 函数用途: 取普通单链接元数据文件的安全代次指纹。
def _file_stat_key(path: Path) -> tuple[int, ...]:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise NoFollowPathError("process record is not a regular single-link file")
    return metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns, metadata.st_size, metadata.st_ctime_ns


# LLM: 只复用读取前已可信且前后同代的单文件；错误年龄取读后代次，缺失则保持未知，防新坏文件继承旧mtime被误隔离。
# 函数用途: 安全读取、校验并缓存静态元数据及无正文诊断，目录变化时只复用未变文件。
def _file_snapshot(root: Path, session_id: str, home: Path, *, force_read: bool = False) -> _SessionSnapshot:
    path, now = root / f"{session_id}.json", time.monotonic()
    payload, before, trusted = None, None, False
    try:
        before = _file_stat_key(path)
        trusted = cache_entry_trustworthy(before[2])
        key = (str(home), str(root), session_id, *before)
        entry = _lookup_view_cache(_FILE_CACHE, key)
        if trusted and not force_read and entry is not None and 0 <= now - entry.verified_at < _CACHE_RECHECK_SECONDS:
            snapshot = deepcopy(entry.snapshots[0])
            if _file_stat_key(path) == before:
                return snapshot
        raw = _read_session_payload(root, session_id)
        payload = raw if isinstance(raw, dict) else None
        payload = _validate_session_payload(raw, session_id, home)
        snapshot = _SessionSnapshot(session_id, payload, mtime_ns=before[2], verified_at=now)
    except (OSError, TypeError, ValueError, RecursionError) as exc:
        snapshot = _SessionSnapshot(session_id, _error_scope_payload(payload), type(exc).__name__,
                                    before[2] if before else 0, verified_at=now)
    try:
        after = _file_stat_key(path)
    except OSError:
        after = None
    if snapshot.error:
        snapshot = replace(snapshot, mtime_ns=after[2] if after else 0)
    if trusted and after == before:
        snapshot = replace(snapshot, cacheable=True)
    if snapshot.cacheable and not _store_view_cache(_FILE_CACHE, key, (snapshot,), now, _FILE_CACHE_MAX_ENTRIES):
        snapshot = replace(snapshot, cacheable=False)
    return snapshot


# LLM: 坏JSON仅保留诊断用归属标量，不复制任意深嵌套坏正文；缺少可信标量仍按无归属年龄门处理。
# 函数用途: 从校验失败的原字典提取有界深度的线程和owner诊断信息。
def _error_scope_payload(payload: object) -> dict | None:
    if not isinstance(payload, dict):
        return None
    result = {}
    for name in ("", *_SCOPE_FIELDS):
        scope = payload if not name else payload.get(name)
        if not isinstance(scope, dict):
            continue
        fields = {key: scope[key] for key in ("thread_id", "conversation_id", "owner_home")
                  if isinstance(scope.get(key), (str, int, float, bool))}
        if name:
            result[name] = fields
        else:
            result.update(fields)
    return result


# LLM: 归属取原JSON中所有可用字典，不因schema/其它scope损坏丢掉明确线程或owner信息；不从正文推断。
# 函数用途: 提取顶层与五类scope供坏记录诊断使用。
def _raw_record_scopes(payload: object) -> tuple[dict, ...]:
    if not isinstance(payload, dict):
        return ()
    return (payload, *(payload[key] for key in _SCOPE_FIELDS if isinstance(payload.get(key), dict)))


# LLM: 全owner错误照常显示；明确外owner隔离，含当前线程的冲突声明仍报错，无归属仅在严格超过60秒才隔离。
# 函数用途: 每次观察重新判断坏文件是否与当前会话无关。
def _unrelated_error(snapshot: _SessionSnapshot, home: Path, thread_id: str | None, now: float) -> bool:
    if not thread_id:
        return False
    scopes = _raw_record_scopes(snapshot.payload)
    owners = (scope.get("owner_home") for scope in scopes)
    if any(isinstance(owner, str) and owner and Path(owner).absolute() != home for owner in owners):
        return True
    threads = {str(scope[key]) for scope in scopes for key in ("thread_id", "conversation_id") if scope.get(key)}
    if threads:
        return thread_id not in threads
    return bool(snapshot.mtime_ns and now - snapshot.mtime_ns / 1_000_000_000 > _UNSCOPED_CORRUPT_STALE_SECONDS)


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


# LLM: 静态快照共用原schema/身份/线程校验；顶层与嵌套owner同等核验，坏记录不得因缓存变成可信正文。
# 函数用途: 验证新读取或待缓存的进程元数据，保持详情与列表解释一致。
def _validate_session_payload(payload: object, selected: str, home: Path) -> dict:
    if not isinstance(payload, dict) or payload.get("session_id") != selected:
        raise ValueError("process session identity mismatch")
    if payload.get("schema"):
        payload = validate_process_record(payload)
    scopes = [payload.get(key) or {} for key in _SCOPE_FIELDS]
    if any(not isinstance(scope, dict) for scope in scopes):
        raise ValueError("invalid process display scope")
    owners = [scope["owner_home"] for scope in (payload, *scopes) if scope.get("owner_home")]
    if any(not isinstance(owner, str) or Path(owner).absolute() != home for owner in owners):
        raise ValueError("process owner mismatch")
    _record_thread_id(payload)
    return payload


# LLM: 输入已经过scope映射类型校验；所有原支持的线程位置共用唯一解释，冲突继续拒绝而不猜归属。
# 函数用途: 为记录验证、过滤和概要提取同一个线程身份；无绑定返回空串。
def _record_thread_id(payload: dict) -> str:
    scopes = [payload, *(payload.get(key) or {} for key in ("access_scope", "execution_scope", "completion_target", "metadata", "scope"))]
    threads = {str(scope[key]) for scope in scopes for key in ("thread_id", "conversation_id") if scope.get(key)}
    if len(threads) > 1:
        raise ValueError("process thread mismatch")
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
            offset = max(0, size - _TAIL_BYTES)
            stream.seek(offset)
            content = stream.read(_TAIL_BYTES)
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
        row["log_tail"] = "\n".join(str(line) for line in tail[-80:])[-_TAIL_BYTES:].splitlines() if isinstance(tail, (list, tuple)) else []
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
