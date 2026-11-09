# LLM: 每对会话（发送 thread, 接收 thread）每小时消息条数的结构化计数。只按两个 thread id 分桶与时间窗口
#   计数，绝不解析消息正文、任务正文或模型输出；窗口是固定长度的小时桶，跨窗口自动归零。
#   调用方分两类：模型发起的发送/派发先 count 再按配置上限决定是否拒绝；宿主自动回报只 record 计入配额，
#   不被拒绝（回报不是模型请求，拒绝只会丢信息，但必须占用配额，避免回报绕过限额）。
# 模块用途: 为会话间消息与派活的每对会话限额提供可单测、可核对的持久计数。
from __future__ import annotations

import json
import math
import os
import stat
import time
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

from ..common.nofollow_fs import open_directory_beneath
from ..gateway_parts.io import read_json_file_report, update_json_file_atomic
from ..user_space.directory_paging import directory_page
from .store_io import safe_file_stem

# 窗口长度（秒）：一小时，与配置键 session_pair_hourly_limit 的语义一致。
WINDOW_SECONDS = 3600.0
# 一片维护的总目录项预算；宽限沿原小时桶长度，不加常驻定时器。
SESSION_PAIR_RETENTION_MAX_FILES = 128


@dataclass(frozen=True)
class SessionPairRetentionRequest:
    home: object
    now: float
    cursor: dict = field(default_factory=dict)
    max_files: int = SESSION_PAIR_RETENTION_MAX_FILES
    max_bytes: int = 8 * 1024 * 1024
    deadline: float | None = None


def cleanup_session_pair_rates(request: SessionPairRetentionRequest) -> dict:
    """只消费原维护的owner及预算；游标保留在同一maintenance.json，不另建账本。"""
    if (type(request.max_files) is not int or request.max_files < 1
            or type(request.max_bytes) is not int or request.max_bytes < 1
            or not math.isfinite(request.now)):
        raise ValueError("invalid_session_pair_retention_budget")
    root = Path(request.home.owner_home_dir)
    sessions = Path(request.home.owner_sessions_dir).relative_to(root).parts + ("session_pair_rate",)
    workspaces = Path(request.home.owner_workspace_dir).relative_to(root).parts + ("runtime", "workspaces")
    position = deepcopy(request.cursor)
    if position.get("schema_version", 1) != 1:
        raise ValueError("invalid_session_pair_retention_cursor")
    # 已完成位置只代表上一轮走到末尾；下个原维护周期须重扫后来过期的活桶。
    if position.get("complete") is True:
        position = {}
    position.update(schema_version=1)
    result = {"cursor": position, "complete": False, "items": 0, "bytes": 0, "deleted": 0,
              "locks_deleted": 0, "preserved_invalid": 0, "busy": 0}
    limit = min(request.max_files, SESSION_PAIR_RETENTION_MAX_FILES)
    while result["items"] < limit and (request.deadline is None or time.monotonic() < request.deadline):
        phase = position.get("phase", "sessions")
        if phase not in {"sessions", "workspaces"}:
            raise ValueError("invalid_session_pair_retention_phase")
        workspace = position.get("workspace")
        if phase == "workspaces" and workspace is None:
            names, scan, complete = directory_page(root, workspaces, position.get("workspace_scan", {}), 1)
            position["workspace_scan"] = scan
            result["items"] += max(1, scan.pop("last_scanned", len(names)))
            if not names:
                result["complete"] = complete
                break
            workspace = names[0]
            _require_pair_entry_name(workspace)
            position["workspace"] = workspace
        parts = sessions if phase == "sessions" else workspaces + (workspace, "conversations", "session_pair_rate")
        complete = _clean_pair_directory(root, parts, position, result, limit, request)
        if not complete:
            break
        position.pop("pair_scan", None)
        if phase == "sessions":
            position["phase"] = "workspaces"
        else:
            position.pop("workspace", None)
    position["complete"] = result["complete"]
    position["last_slice"] = {key: value for key, value in result.items() if key not in {"cursor", "complete"}}
    return result


def _require_pair_entry_name(name: str) -> None:
    if type(name) is not str or name in {"", ".", ".."} or "/" in name or "\\" in name or "\x00" in name:
        raise ValueError("invalid_session_pair_entry")



def _clean_pair_directory(root, parts, position, result, limit, request):
    from ..gateway_parts.io import _cleanup_orphan_lock_at

    remaining = limit - result["items"]
    if remaining <= 0:
        return False
    saved = position.get("pair_scan", {})
    retry = saved.get("retry", [])
    if not isinstance(retry, list) or len(retry) > SESSION_PAIR_RETENTION_MAX_FILES:
        raise ValueError("invalid_session_pair_retry_cursor")
    if retry:
        names, scan, complete = retry[:remaining], {**saved, "retry": []}, saved.get("scan_complete", False)
        tail = retry[remaining:]
    else:
        names, scan, complete = directory_page(root, parts, saved, remaining)
        tail = []
    result["items"] += max(1, scan.pop("last_scanned", len(names)))
    try:
        descriptor = open_directory_beneath(root, parts, require_dir_fd=True)
    except (FileNotFoundError, NotADirectoryError):
        position["pair_scan"] = {}
        return True
    sync_pending = False
    primary = None
    try:
        for name in names:
            _require_pair_entry_name(name)
            if request.deadline is not None and time.monotonic() >= request.deadline:
                tail.append(name)
                continue
            if name.endswith(".json.lock"):
                sync_pending = True
                result["locks_deleted"] += int(_cleanup_orphan_lock_at(descriptor, name, current=request.now))
            elif name.endswith(".json"):
                sync_pending = True
                outcome, size = _remove_expired_pair(descriptor, root.joinpath(*parts, name), request.now,
                                                     request.max_bytes - result["bytes"])
                result["bytes"] += size
                result["deleted"] += int(outcome == "deleted")
                result["busy"] += int(outcome == "busy")
                result["preserved_invalid"] += int(outcome == "invalid")
                if outcome in {"busy", "budget", "changed"}:
                    tail.append(name)
                if outcome == "deleted":
                    result["locks_deleted"] += int(_cleanup_orphan_lock_at(descriptor, name + ".lock", current=request.now))
        scan.update(retry=tail, scan_complete=complete)
        position["pair_scan"] = scan
        return complete and not tail
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            # 同一固定目录的一片删除共同持久化；回执发表前必须成功，不为每个历史桶串行刷盘。
            if sync_pending:
                os.fsync(descriptor)
        except BaseException as cleanup:
            if primary is None:
                raise
            primary.session_pair_sync_error = cleanup
        finally:
            os.close(descriptor)


def _pair_file_version(info) -> tuple:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode, info.st_nlink)


def _remove_expired_pair(directory_fd, path, now, remaining_bytes):
    from ..gateway_parts.io import try_existing_file_transition_at

    raw = b""
    try:
        with try_existing_file_transition_at(directory_fd, path) as acquired:
            if not acquired:
                return "busy", 0
            info = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 65536:
                return "invalid", 0
            if info.st_size > remaining_bytes:
                return "budget", 0
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0), dir_fd=directory_fd)
            try:
                handle = os.fdopen(descriptor, "rb")
            except BaseException:
                os.close(descriptor)
                raise
            with handle:
                opened = os.fstat(handle.fileno())
                if _pair_file_version(opened) != _pair_file_version(info):
                    return "changed", 0
                raw = handle.read(65537)
                data = json.loads(raw)
                current = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
                if (_pair_file_version(os.fstat(handle.fileno())) != _pair_file_version(opened)
                        or _pair_file_version(current) != _pair_file_version(opened)):
                    return "changed", len(raw)
            if (type(data) is not dict or data.get("schema_version") != "session_pair_rate.v1"
                    or type(data.get("schema_generation", 1)) is not int or data.get("schema_generation", 1) != 1
                    or type(data.get("count")) is not int or data["count"] < 1
                    or type(data.get("window_start")) not in (int, float)
                    or not math.isfinite(data["window_start"]) or data["window_start"] < 0
                    or _window_start(data["window_start"]) != data["window_start"]):
                return "invalid", len(raw)
            if data["window_start"] + WINDOW_SECONDS * 2 > now:
                return "active", len(raw)
            os.unlink(path.name, dir_fd=directory_fd)
            return "deleted", len(raw)
    except FileNotFoundError:
        return "missing", 0
    except (json.JSONDecodeError, UnicodeDecodeError):
        return "invalid", len(raw)


# LLM: 冻结值对象；只承载两个 thread id 与窗口起点，不做 IO、不判断上限。
# 类用途: 描述一个会话对在某个时间窗口里的计数。
@dataclass(frozen=True)
class PairWindow:
    sender_thread_id: str
    target_thread_id: str
    window_start: float
    count: int


# LLM: 只接收 storage；不读配置、不发请求、不持有会话对象。上限由调用方按配置传入。
# 类用途: 记录与读取每对会话在当前小时窗口内的消息条数。
class SessionPairRateLimiter:
    # LLM: 计数文件放在 canonical storage 根下，与其它会话领域共用同一持久位置。
    # 函数用途: 绑定 canonical 存储目录。
    def __init__(self, storage: object) -> None:
        self.storage = storage

    # LLM: 只读计数，不写入；窗口已过期按 0 返回。纯查询，供调用方在真正写入前判断。
    # 函数用途: 返回这一对会话在当前窗口内已记录的条数。
    def count(self, sender_thread_id: str, target_thread_id: str, *, now: float | None = None) -> int:
        window = self._load(sender_thread_id, target_thread_id, now)
        return window.count if window is not None else 0

    # LLM: 先读窗口再原子 +1：跨窗口时把计数重置为 1，窗口内累加。读坏计数文件按新窗口处理，
    #   不让坏文件永久卡住这一对会话（计数是防滥用节流，不是权威业务账本）。
    # 函数用途: 记一条消息并返回记录后的窗口内条数。
    def record(self, sender_thread_id: str, target_thread_id: str, *, now: float | None = None) -> int:
        current = time.time() if now is None else float(now)
        start = _window_start(current)
        path = self._path(sender_thread_id, target_thread_id)
        if path is None:
            return 0

        # LLM: 锁内重读再累加，避免两个并发写入方互相覆盖计数。
        # 函数用途: 在原子更新内把窗口计数加一。
        def updater(data: dict[str, object]) -> dict[str, object]:
            previous = _count_of(data, start)
            return {"schema_version": "session_pair_rate.v1", "window_start": start, "count": previous + 1}

        updated = update_json_file_atomic(path, updater)
        return int(updated.get("count") or 0)

    # LLM: 超限判定只按结构化计数与调用方传入的上限；limit<=0 表示不限制（与配置语义一致）。
    # 函数用途: 判断这一对会话是否已经用满本窗口配额。
    def over_limit(
        self, sender_thread_id: str, target_thread_id: str, limit: int, *, now: float | None = None
    ) -> bool:
        if int(limit or 0) <= 0:
            return False
        return self.count(sender_thread_id, target_thread_id, now=now) >= int(limit)

    # LLM: 目录不存在时按"没有计数"处理；读坏或字段异常同样按新窗口，不把读坏当超限。
    # 函数用途: 读取这一对会话在当前窗口内的计数，跨窗口或缺失时返回 None。
    def _load(self, sender_thread_id: str, target_thread_id: str, now: float | None) -> PairWindow | None:
        path = self._path(sender_thread_id, target_thread_id)
        if path is None or not path.exists():
            return None
        report = read_json_file_report(path, context="conversation.session_pair_rate.read")
        if report.load_error is not None or not report.payload:
            return None
        start = _window_start(time.time() if now is None else float(now))
        if float(report.payload.get("window_start") or 0.0) != start:
            return None
        return PairWindow(
            sender_thread_id=str(sender_thread_id or ""),
            target_thread_id=str(target_thread_id or ""),
            window_start=start,
            count=int(report.payload.get("count") or 0),
        )

    # LLM: 两个 thread id 都必须非空才能分桶；空值返回 None，调用方按"不计数"处理而不是编造一个桶。
    # 函数用途: 返回这一对会话的计数文件路径。
    def _path(self, sender_thread_id: str, target_thread_id: str) -> Path | None:
        sender = str(sender_thread_id or "").strip()
        target = str(target_thread_id or "").strip()
        if not sender or not target:
            return None
        root = Path(self.storage.session_pair_rate_dir)
        return root / f"{safe_file_stem(sender)}.{safe_file_stem(target)}.json"


# LLM: 窗口起点是时间戳向下取整到固定长度，只由当前时间决定，不读正文也不看历史。
# 函数用途: 返回给定时刻所属小时窗口的起点。
def _window_start(now: float) -> float:
    return float(int(now // WINDOW_SECONDS) * WINDOW_SECONDS)


# LLM: 计数文件里的 window_start 与当前窗口一致时沿用旧值，否则归零重数。
# 函数用途: 从计数文件内容里取出当前窗口的有效计数。
def _count_of(data: dict[str, object], window_start: float) -> int:
    if float(data.get("window_start") or 0.0) != window_start:
        return 0
    return int(data.get("count") or 0)


# LLM: 计数是节流不是权威账本，所以 store 上没有这个组件时按"不限制/不计数"处理，绝不因为缺组件而拒绝业务。
# 函数用途: 取当前会话存储上的每对会话计数器；没有该组件时返回 None。
def pair_rate_limiter(store: object) -> SessionPairRateLimiter | None:
    limiter = getattr(store, "session_pair_rate", None)
    return limiter if isinstance(limiter, SessionPairRateLimiter) else None


# LLM: 判定"这一对会话是否已经用满本小时配额"。limit<=0 或没有计数器时一律放行（0 = 不限制）。
#   只读计数，不写入；调用方在真正投递前调用，避免被拒的消息占用配额。
# 函数用途: 判断一次新的会话间消息是否超出每对会话限额。
def pair_limit_reached(
    store: object, sender_thread_id: str, target_thread_id: str, limit: int
) -> bool:
    limiter = pair_rate_limiter(store)
    if limiter is None:
        return False
    return limiter.over_limit(sender_thread_id, target_thread_id, limit)


# LLM: 真正投递成功（或幂等重放）后记一条。回报消息也走这里计入配额，但调用方不因超限拒绝回报——
#   回报是宿主发出的结构化结果，丢掉它只会让发送方永远看不到任务结束。
# 函数用途: 记一条会话间消息到这一对会话的当前小时计数。
def record_pair_message(store: object, sender_thread_id: str, target_thread_id: str) -> int:
    limiter = pair_rate_limiter(store)
    if limiter is None:
        return 0
    try:
        return limiter.record(sender_thread_id, target_thread_id)
    except (OSError, ValueError, TypeError):
        # 计数写失败不影响已经投递成功的消息；节流是附加能力。
        return 0


__all__ = [
    "PairWindow",
    "SessionPairRateLimiter",
    "WINDOW_SECONDS",
    "pair_limit_reached",
    "pair_rate_limiter",
    "record_pair_message",
]
