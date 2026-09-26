# LLM: Gateway 安全重启的唯一状态源：重启请求文件、进程内排空阶段、排空完成标记与冷却/防循环记录都在这里。
# 两段排空：先停领新请求、等处理中的回合结束（上限 turn_wait）；再关闭 restart_gate、等执行中的副作用工具归零（上限 drain_timeout，
# 0 不限）。第二段超时就取消重启、恢复服务，不强杀。只认结构化字段（目标进程号、请求编号、requester 身份），不读正文。
# 进程外的接班启动由 cli/gateway_process 负责。改动时同步 test_gateway_restart_service.py 与 docs/design/GATEWAY_SAFE_RESTART.md。
# 模块用途: 让代理、/restart 和终端都能安全地安排 Gateway 重启：先排空再换进程，重启后由新进程续跑并通知发起方。
from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from ..concurrency.restart_gate import (
    close_tool_gate,
    executing_tool_count,
    open_tool_gate,
    wait_until_no_executing_tools,
)
from .io import (
    read_json_file_report,
    update_json_file_atomic,
    write_json_file_atomic,
)
from .paths import GatewayPaths

HOSTING_GATEWAY_PID_ENV = "MY_AGENT_HOSTING_GATEWAY_PID"
REQUEST_SCHEMA = "gateway_restart_request.v1"
COMPLETED_SCHEMA = "gateway_restart_completed.v1"
STATE_SCHEMA = "gateway_restart_state.v1"
LOOP_GUARD_WINDOW_SECONDS = 600.0
LOOP_GUARD_LIMIT = 3
MARKER_MAX_AGE_SECONDS = 600.0
CONTINUATION_EVENT_TYPE = "gateway_restart_completed"
CANCELLED_EVENT_TYPE = "gateway_restart_cancelled"
_POLL_SECONDS = 0.25

_phase_lock = threading.Lock()
_phase: dict[str, object] = {"phase": "idle", "request_id": "", "since": 0.0}


# LLM: 托管身份只来自 Gateway 服务进程启动时写进自身环境的进程号（cli/gateway_host_guard 负责写入）；缺失或坏值返回 0。
# 函数用途: 返回托管当前进程的 Gateway 进程号，工具据此确认自己运行在 Gateway 里、能安排安全重启。
def hosting_gateway_pid() -> int:
    try:
        return max(0, int(os.environ.get(HOSTING_GATEWAY_PID_ENV, "") or 0))
    except ValueError:
        return 0


# LLM: 路径只由已确认的 Gateway 根目录派生，不接受调用方传入的任意路径。
# 函数用途: 返回待执行重启请求文件的位置。
def restart_request_path(paths: GatewayPaths) -> Path:
    return Path(paths.root) / "gateway_restart.request"


# LLM: 标记由旧进程在排空成功后写入，新进程启动时消费一次。
# 函数用途: 返回“排空完成、等待接班”标记文件的位置。
def restart_completed_path(paths: GatewayPaths) -> Path:
    return Path(paths.root) / "gateway_restart.completed"


# LLM: 只存冷却与防循环需要的结构化时间点，不存原因正文以外的内容。
# 函数用途: 返回重启冷却与近期记录文件的位置。
def restart_state_path(paths: GatewayPaths) -> Path:
    return Path(paths.root) / "gateway_restart_state.json"


# LLM: 进程内只读快照；draining 表示已停领新请求，调用方据此暂停派发与后台 tick。
# 函数用途: 判断本进程是否处在安全重启排空中。
def restart_draining() -> bool:
    with _phase_lock:
        return _phase["phase"] in {"turn_wait", "tool_drain", "exiting"}


# LLM: 只读快照，供状态投影与测试；返回副本。
# 函数用途: 返回当前排空阶段、请求编号和开始时间。
def restart_phase() -> dict[str, object]:
    with _phase_lock:
        return dict(_phase)


def _set_phase(phase: str, request_id: str = "") -> None:
    with _phase_lock:
        _phase.update(phase=phase, request_id=request_id, since=time.time() if phase != "idle" else 0.0)


# LLM: 测试与异常收尾专用：把阶段和工具关口都恢复到空闲，不动任何文件。
# 函数用途: 重置进程内排空状态并重新打开工具关口。
def reset_restart_drain_state() -> None:
    _set_phase("idle")
    open_tool_gate()


# LLM: 写入在请求文件锁内完成；同一目标进程已有待执行请求时只追加发起方并返回 coalesced，冷却与防循环按结构化时间判断。
# requester 只接受调用方已核验的结构化身份字段；reason 仅供展示，截断到 200 字。
# 函数用途: 提交一次安全重启请求，返回 scheduled / coalesced / cooldown / loop_guard 之一及请求内容；会写请求文件。
def submit_restart_request(
    paths: GatewayPaths,
    *,
    target_pid: int,
    requester: dict[str, object],
    reason: str,
    cooldown_seconds: float,
    now: float | None = None,
) -> dict[str, object]:
    current = time.time() if now is None else float(now)
    requester = {str(k): v for k, v in dict(requester or {}).items() if v not in (None, "")}
    refusal = _cooldown_or_loop_refusal(paths, requester, current, cooldown_seconds)
    if refusal is not None:
        return refusal
    outcome: dict[str, object] = {}

    def updater(existing: dict) -> dict:
        if existing.get("schema") == REQUEST_SCHEMA and existing.get("status") == "pending" \
                and int(existing.get("target_pid") or 0) == int(target_pid):
            extra = list(existing.get("additional_requesters") or [])
            if requester and requester != existing.get("requester") and requester not in extra:
                extra.append(requester)
            outcome.update(status="coalesced")
            return {**existing, "additional_requesters": extra}
        outcome.update(status="scheduled")
        return {
            "schema": REQUEST_SCHEMA,
            "request_id": f"gwrestart-{int(current)}-{uuid.uuid4().hex[:12]}",
            "status": "pending",
            "target_pid": int(target_pid),
            "reason": str(reason or "").strip()[:200],
            "requested_at": current,
            "requester": requester,
            "additional_requesters": [],
        }

    payload = update_json_file_atomic(restart_request_path(paths), updater)
    if outcome.get("status") == "scheduled":
        _record_recent_request(paths, requester, current)
    return {"status": outcome.get("status", "scheduled"), "request": payload}


def _cooldown_or_loop_refusal(
    paths: GatewayPaths,
    requester: dict[str, object],
    now: float,
    cooldown_seconds: float,
) -> dict[str, object] | None:
    state = read_json_file_report(restart_state_path(paths)).payload or {}
    last_done = float(state.get("last_drained_at") or 0.0)
    remaining = float(cooldown_seconds or 0.0) - (now - last_done)
    if last_done and remaining > 0:
        return {"status": "cooldown", "retry_after_seconds": round(remaining, 1)}
    thread_id = str(requester.get("thread_id") or "")
    if not thread_id:
        return None
    recent = [
        item for item in state.get("recent_requests") or []
        if isinstance(item, dict) and str(item.get("thread_id") or "") == thread_id
        and now - float(item.get("requested_at") or 0.0) <= LOOP_GUARD_WINDOW_SECONDS
    ]
    if len(recent) >= LOOP_GUARD_LIMIT:
        return {"status": "loop_guard", "recent_count": len(recent)}
    return None


def _record_recent_request(paths: GatewayPaths, requester: dict[str, object], now: float) -> None:
    def updater(state: dict) -> dict:
        recent = [
            item for item in state.get("recent_requests") or []
            if isinstance(item, dict) and now - float(item.get("requested_at") or 0.0) <= LOOP_GUARD_WINDOW_SECONDS
        ]
        recent.append({"thread_id": str(requester.get("thread_id") or ""), "requested_at": now})
        return {**state, "schema": STATE_SCHEMA, "recent_requests": recent[-20:]}

    update_json_file_atomic(restart_state_path(paths), updater)


# LLM: 只返回目标是本进程的待执行请求；其他进程号或坏文件一律视为无请求。
# 函数用途: 读取指向本 Gateway 进程的待执行重启请求。
def pending_restart_request(paths: GatewayPaths, *, pid: int) -> dict[str, object] | None:
    payload = read_json_file_report(restart_request_path(paths)).payload or {}
    if payload.get("schema") != REQUEST_SCHEMA or payload.get("status") != "pending":
        return None
    if int(payload.get("target_pid") or 0) != int(pid):
        return None
    return payload


# LLM: 在服务主线程同步运行；active_turns 由调用方给出本进程正在执行的请求数（Gateway 用 admission 计数，不数
# processing 目录，避免遗留文件拖满等待）。第一段期间派发与后台 tick 暂停，在跑的回合照常完成；第二段关闭工具关口。
# 成功时保持 exiting 与关口关闭，直到进程退出；第二段超时则打开关口、回到 idle 并返回取消原因，不强杀。
# 函数用途: 执行两段排空并返回结果；会改变进程内排空阶段与工具关口，并通过 on_progress 回报阶段。
def run_restart_drain(
    request: dict[str, object],
    *,
    active_turns: Callable[[], int],
    turn_wait_seconds: float,
    drain_timeout_seconds: float,
    stop_requested: Callable[[], bool] = lambda: False,
    on_progress: Callable[[dict[str, object]], None] | None = None,
) -> dict[str, object]:
    request_id = str(request.get("request_id") or "")
    _set_phase("turn_wait", request_id)
    report = on_progress or (lambda _facts: None)
    deadline = time.monotonic() + max(0.0, float(turn_wait_seconds or 0.0))
    turns = int(active_turns())
    report({"phase": "turn_wait", "active_turns": turns})
    while turns > 0 and time.monotonic() < deadline and not stop_requested():
        time.sleep(_POLL_SECONDS)
        turns = int(active_turns())
    _set_phase("tool_drain", request_id)
    close_tool_gate()
    report({"phase": "tool_drain", "active_turns": turns, "executing_tools": executing_tool_count()})
    timeout = float(drain_timeout_seconds or 0.0)
    drained = wait_until_no_executing_tools(timeout if timeout > 0 else None)
    if not drained:
        executing = executing_tool_count()
        reset_restart_drain_state()
        return {"ok": False, "reason": "drain_timeout", "executing": executing, "request_id": request_id}
    _set_phase("exiting", request_id)
    return {"ok": True, "request_id": request_id, "active_turns_at_exit": turns}


# LLM: 旧进程排空成功后调用：把请求原子改写为完成标记并删除请求文件，同时记下冷却起点；不启动任何进程。
# 函数用途: 写入“排空完成、等待接班”标记，供新进程启动时续跑与通知发起方。
def mark_restart_drained(
    paths: GatewayPaths,
    request: dict[str, object],
    *,
    old_pid: int,
    old_process_started_at: float,
    active_turns_at_exit: int,
) -> dict[str, object]:
    now = time.time()
    marker = {
        **{k: v for k, v in request.items() if k != "status"},
        "schema": COMPLETED_SCHEMA,
        "old_pid": int(old_pid),
        "old_process_started_at": float(old_process_started_at or 0.0),
        "drained_at": now,
        "active_turns_at_exit": int(active_turns_at_exit),
    }
    write_json_file_atomic(restart_completed_path(paths), marker)
    try:
        restart_request_path(paths).unlink()
    except OSError:
        pass
    update_json_file_atomic(
        restart_state_path(paths),
        lambda state: {**state, "schema": STATE_SCHEMA, "last_drained_at": now,
                       "last_request_id": str(request.get("request_id") or "")},
    )
    return marker


# LLM: 取消只清掉请求文件并记录原因；进程继续正常服务，发起方下次可重新安排。
# 函数用途: 排空超时时撤销这次重启请求，并留下结构化的取消记录。
def cancel_restart_request(paths: GatewayPaths, request: dict[str, object], result: dict[str, object]) -> None:
    try:
        restart_request_path(paths).unlink()
    except OSError:
        pass
    update_json_file_atomic(
        restart_state_path(paths),
        lambda state: {**state, "schema": STATE_SCHEMA, "last_cancelled": {
            "request_id": str(request.get("request_id") or ""),
            "reason": str(result.get("reason") or ""),
            "executing": int(result.get("executing") or 0),
            "cancelled_at": time.time(),
        }},
    )


# LLM: 新进程启动时、启动恢复之前调用一次；只在旧进程号已不存活且标记未超过 MARKER_MAX_AGE_SECONDS 时返回标记
# （证明是刚排空完的计划内重启），读后删除标记文件；过期标记同样删除但返回 None，按普通启动处理。
# 函数用途: 读取并消费上一次安全重启的完成标记，供启动恢复去掉等待延迟并发起续跑通知。
def consume_restart_marker(
    paths: GatewayPaths,
    *,
    old_pid_alive: Callable[[int], bool],
    now: float | None = None,
) -> dict[str, object] | None:
    path = restart_completed_path(paths)
    marker = read_json_file_report(path).payload or {}
    if marker.get("schema") != COMPLETED_SCHEMA:
        return None
    old_pid = int(marker.get("old_pid") or 0)
    if old_pid <= 0 or old_pid_alive(old_pid):
        return None
    try:
        path.unlink()
    except OSError:
        pass
    current = time.time() if now is None else float(now)
    if current - float(marker.get("drained_at") or 0.0) > MARKER_MAX_AGE_SECONDS:
        return None
    return marker


# LLM: 由接班进程在启动恢复后调用；观测文字明确“重启已完成、不要再次重启”，防止续跑回合再触发重启。
# 函数用途: 重启完成后通知发起会话继续原来的工作；返回写入条数与跳过/失败原因（只含异常类型）。
def append_restart_continuations(marker: dict[str, object], *, home_root: str | Path) -> dict[str, object]:
    request_id = str(marker.get("request_id") or "")
    downtime = max(0.0, time.time() - float(marker.get("drained_at") or time.time()))
    summary = (
        f"Gateway 安全重启已完成（请求 {request_id}，旧进程 {int(marker.get('old_pid') or 0)} 已退出，"
        f"新进程 {os.getpid()} 已接班，停顿约 {downtime:.0f} 秒）。重启前未完成的回合会自动续跑。"
        "不要再次调用 restart_gateway，也不要为了验证而重启；如果重启前在排查问题，请直接继续排查并向用户汇报结果。"
    )
    return notify_restart_requesters(marker, home_root=home_root, event_type=CONTINUATION_EVENT_TYPE, summary=summary)


# LLM: 由旧进程在排空超时取消重启后调用；发起方的工具回执写的是“已安排”，这里补上结构化的取消事实。
# 函数用途: 告诉发起会话这次重启已取消、原因和仍在执行的工具数，服务照常运行。
def append_restart_cancellation(
    request: dict[str, object], result: dict[str, object], *, home_root: str | Path,
) -> dict[str, object]:
    summary = (
        f"Gateway 安全重启已取消（请求 {request.get('request_id') or ''}，原因 {result.get('reason') or ''}，"
        f"仍在执行的副作用工具 {int(result.get('executing') or 0)} 个）。Gateway 没有重启、照常服务。"
        "不要立刻重复安排；先告诉用户哪些工作还在跑，由用户决定等它结束后再重启还是放弃。"
    )
    return notify_restart_requesters(request, home_root=home_root, event_type=CANCELLED_EVENT_TYPE, summary=summary)


# LLM: 只给记录了会话存储根与会话编号的发起方各写一条观测+唤醒（按事件类型+请求编号去重）；存储根必须位于 my-agent
# 数据根之内且已存在，否则跳过，不创建目录。唤醒由后台调度按原 owner/thread 权限处理。
# 函数用途: 把重启完成/取消事实写进发起会话的持久唤醒；返回写入条数与跳过原因（只含异常类型）。
def notify_restart_requesters(
    record: dict[str, object], *, home_root: str | Path, event_type: str, summary: str,
) -> dict[str, object]:
    from ..conversation.store import ConversationStore

    root = Path(home_root).expanduser().resolve(strict=False)
    request_id = str(record.get("request_id") or "")
    written, skipped = 0, []
    for requester in [record.get("requester"), *list(record.get("additional_requesters") or [])]:
        if not isinstance(requester, dict):
            continue
        thread_id = str(requester.get("thread_id") or "")
        raw_root = str(requester.get("conversation_store_root") or "")
        if not thread_id or not raw_root:
            continue
        store_root = Path(raw_root).expanduser().resolve(strict=False)
        if root not in store_root.parents or not store_root.is_dir():
            skipped.append({"thread_id": thread_id, "reason": "store_root_outside_home"})
            continue
        shared = {"thread_id": thread_id, "urgency": "normal",
                  "metadata": {"restart_request_id": request_id, "old_pid": record.get("old_pid") or record.get("target_pid")}}
        try:
            ConversationStore(store_root, initialize=False).wakes.append_observation(
                {**shared, "event_type": event_type, "requires_main_agent": True, "summary": summary},
                {**shared, "reason": event_type, "summary": summary, "dedupe_key": f"{event_type}:{request_id}"},
            )
            written += 1
        except Exception as exc:  # noqa: BLE001 - 单个会话通知失败不能阻止 Gateway 启动或恢复服务
            skipped.append({"thread_id": thread_id, "reason": type(exc).__name__})
    return {"written": written, "skipped": skipped}


__all__ = [
    "CANCELLED_EVENT_TYPE",
    "COMPLETED_SCHEMA",
    "CONTINUATION_EVENT_TYPE",
    "HOSTING_GATEWAY_PID_ENV",
    "REQUEST_SCHEMA",
    "append_restart_cancellation",
    "append_restart_continuations",
    "cancel_restart_request",
    "consume_restart_marker",
    "hosting_gateway_pid",
    "mark_restart_drained",
    "notify_restart_requesters",
    "pending_restart_request",
    "reset_restart_drain_state",
    "restart_completed_path",
    "restart_draining",
    "restart_phase",
    "restart_request_path",
    "restart_state_path",
    "run_restart_drain",
    "submit_restart_request",
]
