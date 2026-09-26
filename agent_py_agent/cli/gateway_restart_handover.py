# LLM: Gateway 安全重启的进程级接线：服务主循环里执行排空、收尾后交给接班进程、接班进程等旧进程退出再启动恢复。
# 请求/排空/标记的状态源在 agent/gateway_parts/restart_service；这里只做进程与服务管理器相关的动作，不读对话正文。
# systemd 托管（cgroup 属于本服务单元）或 launchd 托管（XPC_SERVICE_NAME 等于本服务标签）时返回退出码 75 交给管理器拉起，
# 否则自己拉起带 --after-pid 的接班进程。改动须同步 test_gateway_safe_restart.py 与 docs/design/GATEWAY_SAFE_RESTART.md。
# 模块用途: 让 Gateway 在不丢回合、不撕裂工具的前提下自己换进程，并在重启后通知发起方继续工作。
from __future__ import annotations

import os
from pathlib import Path

from ..agent.gateway_parts import is_pid_alive, log_gateway_event, wait_for_pid_exit
from ..agent.gateway_parts.daemon_control import (
    GATEWAY_SERVICE_RESTART_EXIT_CODE,
    stop_request_targets_process,
)
from ..agent.gateway_parts.io import read_json_file, update_json_file_atomic
from ..agent.gateway_parts.request_worker import admission
from ..agent.gateway_parts.restart_service import (
    append_restart_cancellation,
    append_restart_continuations,
    cancel_restart_request,
    consume_restart_marker,
    mark_restart_drained,
    pending_restart_request,
    run_restart_drain,
)
from .gateway_service import _get_launchd_label, get_service_name
from .models import GatewayRunContext

PREDECESSOR_EXIT_WAIT_SECONDS = 60.0


# LLM: 只在服务主循环里调用；有指向本进程的请求才排空。停止请求优先于重启：排空期间收到停止请求就撤销重启，
# 交回主循环按停止处理。排空超时取消时给发起会话写一条取消通知。成功时返回带 restart 标记的结束报告，其余返回 None 继续服务。
# 函数用途: 发现安全重启请求时执行两段排空，并把阶段写进 Gateway 状态文件供 /status 查看。
def drain_for_requested_restart(context: GatewayRunContext, process_identity: dict[str, object]) -> dict | None:
    paths = context.paths
    request = pending_restart_request(paths, pid=os.getpid())
    if request is None:
        return None
    agent = context.agent
    config = agent.config
    request_id = str(request.get("request_id") or "")

    def stop_requested() -> bool:
        return paths.stop_request.exists() and stop_request_targets_process(
            read_json_file(paths.stop_request), process_identity, process_started_at=context.process_started_at,
        )

    def progress(facts: dict[str, object]) -> None:
        _publish_drain_state(paths, {**facts, "request_id": request_id})
        log_gateway_event(agent, "gateway_restart_draining", {**facts, "request_id": request_id})

    log_gateway_event(agent, "gateway_restart_requested", {
        "request_id": request_id, "reason": request.get("reason"),
        "requester_kind": (request.get("requester") or {}).get("kind"),
    })
    result = run_restart_drain(
        request,
        active_turns=lambda: int(admission.snapshot().get("total") or 0),
        turn_wait_seconds=float(config.gateway_restart_turn_wait_seconds),
        drain_timeout_seconds=float(config.gateway_restart_drain_timeout_seconds),
        stop_requested=stop_requested,
        on_progress=progress,
    )
    if result.get("ok") and stop_requested():
        result = {"ok": False, "reason": "stop_requested", "request_id": request_id}
    if not result.get("ok"):
        cancel_restart_request(paths, request, result)
        _publish_drain_state(paths, {"phase": "cancelled", "reason": result.get("reason"), "request_id": request_id})
        notified = {"written": 0, "skipped": []}
        home_root = getattr(getattr(agent, "home_paths", None), "root", None)
        if result.get("reason") != "stop_requested" and home_root:
            notified = append_restart_cancellation(request, result, home_root=home_root)
        log_gateway_event(agent, "gateway_restart_cancelled", {**result, "requesters_notified": notified.get("written", 0)})
        return None
    marker = mark_restart_drained(
        paths, request, old_pid=os.getpid(),
        old_process_started_at=float(context.process_started_at or 0.0),
        active_turns_at_exit=int(result.get("active_turns_at_exit") or 0),
    )
    log_gateway_event(agent, "gateway_restart_drained", {
        "request_id": request_id, "active_turns_at_exit": marker.get("active_turns_at_exit"),
    })
    return {"summary": "safe restart drained", "restart": marker}


def _publish_drain_state(paths, facts: dict[str, object]) -> None:
    try:
        update_json_file_atomic(paths.state, lambda state: {**state, "restart_drain": dict(facts)})
    except OSError:
        pass


# LLM: 服务管理器事实只认结构化环境：本进程 cgroup 属于本服务 systemd 单元，或 launchd 给本服务标签设置的 XPC_SERVICE_NAME。
# systemd 下自己拉起的接班进程会随旧进程所在 cgroup 被清理，所以托管时必须交给管理器。
# 函数用途: 判断本 Gateway 是否由 systemd/launchd 托管、应当用退出码 75 让管理器重启。
def service_manager_restarts_gateway() -> bool:
    if os.environ.get("XPC_SERVICE_NAME", "") == _get_launchd_label():
        return True
    try:
        cgroup = Path("/proc/self/cgroup").read_text(encoding="utf-8")
    except OSError:
        return False
    return f"/{get_service_name()}.service" in cgroup


# LLM: 只在计划内安全重启完成收尾之后调用；spawn 与 start_files 由调用方传入，保证与 gateway start 同一套启动命令和 pid 记录。
# 函数用途: 托管时返回 75 交给服务管理器；否则拉起带 --after-pid 的接班进程并写入它的 pid 记录，返回旧进程退出码。
def hand_over_to_successor(context: GatewayRunContext, *, pid: int, command: list[str], spawn, write_start_files) -> int:
    agent = context.agent
    if service_manager_restarts_gateway():
        log_gateway_event(agent, "gateway_restart_handover", {"old_pid": pid, "mode": "service_manager"})
        return GATEWAY_SERVICE_RESTART_EXIT_CODE
    successor_command = [*command, "--after-pid", str(pid)]
    try:
        process = spawn(successor_command)
    except OSError as exc:
        log_gateway_event(agent, "gateway_restart_handover_failed", {"old_pid": pid, "error_type": type(exc).__name__})
        return 2
    write_start_files(process.pid, successor_command)
    log_gateway_event(agent, "gateway_restart_handover", {"old_pid": pid, "new_pid": process.pid, "mode": "successor"})
    return 0


# LLM: 接班进程在创建 Agent 之前调用；旧进程收尾有界（循环 join 与 HTTP 停止），等不到也继续启动，由常规恢复兜底。
# 函数用途: 等旧 Gateway 进程真正退出，保证启动恢复把它留下的回合当作已死租约立刻重排。
def wait_for_predecessor_exit(after_pid: int) -> bool:
    if after_pid <= 0 or after_pid == os.getpid():
        return True
    return bool(wait_for_pid_exit(after_pid, PREDECESSOR_EXIT_WAIT_SECONDS))


# LLM: 在启动恢复之前调用一次；返回的标记决定恢复是否按“计划内重启”处理（不加等待延迟、续跑优先）。
# 函数用途: 消费上一次安全重启的完成标记；没有或过期时返回 None。
def take_restart_marker(paths) -> dict[str, object] | None:
    return consume_restart_marker(paths, old_pid_alive=is_pid_alive)


# LLM: 在启动恢复之后调用；续跑通知写进发起会话的持久唤醒，由后台调度按原 owner/thread 权限处理。
# 函数用途: 记录重启完成事件并通知发起会话“重启已完成、不要再次重启”。
def announce_restart_completed(agent, marker: dict[str, object], *, requeued: int) -> None:
    home_root = getattr(getattr(agent, "home_paths", None), "root", None)
    continuations = append_restart_continuations(marker, home_root=home_root) if home_root else {"written": 0, "skipped": []}
    log_gateway_event(agent, "gateway_restart_completed", {
        "request_id": marker.get("request_id"),
        "old_pid": marker.get("old_pid"),
        "new_pid": os.getpid(),
        "requeued_requests": requeued,
        "continuations_written": continuations.get("written", 0),
        "continuations_skipped": continuations.get("skipped", []),
    })


__all__ = [
    "PREDECESSOR_EXIT_WAIT_SECONDS",
    "announce_restart_completed",
    "drain_for_requested_restart",
    "hand_over_to_successor",
    "service_manager_restarts_gateway",
    "take_restart_marker",
    "wait_for_predecessor_exit",
]
