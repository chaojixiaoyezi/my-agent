# LLM: 只读投影受管进程记录和有界日志；禁止调用会恢复事务或写回状态的 Store/Registry 查询。
# 模块用途: 给 TUI 与插件提供不消费唤醒、不干扰进程生命周期的观察数据。
from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from ..common.nofollow_fs import open_readonly_file_beneath, read_bytes_beneath
from ..tooling.process_registry import process_registry
from ..tooling.process_session_records import validate_process_record, validate_session_id
from ..tooling.process_session_store import process_session_store_root
from .process_events import _managed_instance_still_live

_TERMINAL = frozenset({"exited", "failed", "killed", "completed", "not_started"})
# 只读后台进程投影最多读取的日志尾部字节数。
_TAIL_BYTES = 64 * 1024


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


# LLM: 缺失目录不得创建；指定线程严格只含同线程记录，未指定线程才返回全owner记录，过滤先于存活探针，不回收或落盘。
# 函数用途: 按可选当前会话返回本owner的只读后台进程列表与该范围的计数。
def list_background_processes(owner_home: Path, *, thread_id: str | None = None, active_only: bool = False) -> dict[str, object]:
    home = Path(owner_home).expanduser().absolute()
    items, errors, seen = [], [], set()
    for root in _session_directories(home):
        for path in sorted(root.glob("bg-*.json")):
            if path.stem in seen:
                continue
            seen.add(path.stem)
            try:
                payload = _read_session(root, path.stem, home)
                if thread_id and _record_thread_id(payload) != thread_id:
                    continue
                item = _summary(root, payload)
                items.append(item.to_dict())
            except (OSError, TypeError, ValueError) as exc:
                errors.append({"session_id": path.stem, "error": type(exc).__name__})
    active = [item for item in items if item["alive"] or item["wake_pending"]]
    return {"items": active if active_only else items, "active_count": len(active),
            "total": len(items), "errors": errors, "projection_ok": not errors}


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


# LLM: 不调用 Store.load/list（可能恢复 redo）；逐段 nofollow 有界读原 JSON，显式 owner/thread 冲突拒绝。
# 函数用途: 读取会话元数据，旧文档目录的无 schema 展示记录也必须校验身份与归属。
def _read_session(root: Path, session_id: str, home: Path) -> dict:
    selected = validate_session_id(session_id)
    raw = read_bytes_beneath(root, (f"{selected}.json",), max_bytes=1024 * 1024)
    if raw is None:
        raise FileNotFoundError(selected)
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get("session_id") != selected:
        raise ValueError("process session identity mismatch")
    if payload.get("schema"):
        payload = validate_process_record(payload)
    scopes = [payload.get(key) or {} for key in ("access_scope", "execution_scope", "completion_target", "metadata", "scope")]
    if any(not isinstance(scope, dict) for scope in scopes):
        raise ValueError("invalid process display scope")
    owners = [scope["owner_home"] for scope in scopes if scope.get("owner_home")]
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


# LLM: 终态不探测PID；线程身份共用原reader的完整解释，非终态核原实例，不推断成功或消费通知。
# 函数用途: 投影一致的线程元数据、当前存活与刚退出待通知事实，保留unknown。
def _summary(root: Path, payload: dict) -> BackgroundProcessSummary:
    status = str(payload.get("status") or "unknown")
    sid = str(payload["session_id"])
    pid = payload.get("pid") or payload.get("launcher_pid") or 0
    if type(pid) is not int or pid < 0:
        raise ValueError("invalid process pid")
    alive = status not in _TERMINAL and status != "unknown" and _managed_instance_still_live(sid, root, payload)
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
