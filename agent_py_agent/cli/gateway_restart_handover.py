# LLM: Gateway 安全重启的进程级接线：服务主循环里执行排空、收尾后交给接班进程、接班进程等旧进程退出再启动恢复。
# 请求/排空/标记的状态源在 agent/gateway_parts/restart_service；这里只做进程与服务管理器相关的动作，不读对话正文。
# systemd 托管（cgroup 属于本服务单元）或 launchd 托管（XPC_SERVICE_NAME 等于本服务标签）时返回退出码 75 交给管理器拉起，
# 否则自己拉起带 --after-pid 的接班进程。改动须同步 test_gateway_safe_restart.py 与 docs/design/GATEWAY_SAFE_RESTART.md。
#   内部C取消固定caller必须原controller→lease，先核source再取消写，与C publisher同序；不把unsupported变成滚动重启。
# 模块用途: 让 Gateway 安全换进程并通知发起方；内部多进程继续原拒绝和取消，元数据发表保持原锁序。
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from ..agent.gateway_parts import is_pid_alive, log_gateway_event, wait_for_pid_exit
from ..agent.gateway_parts.daemon_control import (
    GATEWAY_SERVICE_RESTART_EXIT_CODE,
    get_running_pid,
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
    reset_restart_drain_state,
    run_restart_drain,
    submit_restart_request,
)
from .gateway_host_guard import refuse_stopping_hosting_gateway
from .gateway_service import _get_launchd_label, get_service_name
from .models import GatewayRunContext

# 重启交接等待前任进程退出最多 60 秒，超时按异常处理
PREDECESSOR_EXIT_WAIT_SECONDS = 60.0
# 内部多进程滚动重启尚未实现，拒绝后保留当前服务。
MULTIPROCESS_RESTART_ERROR_CODE = "GATEWAY_MULTIPROCESS_RESTART_UNSUPPORTED"


# LLM: 只在服务主循环里调用；有指向本进程的请求才排空。停止请求优先于重启：排空期间收到停止请求就撤销重启，
# 交回主循环按停止处理。排空超时取消时给发起会话写一条取消通知。成功时返回带 restart 标记的结束报告，其余返回 None 继续服务。
# 函数用途: 汇总前后台回合后两段排空；按重启策略记录强制超时，停止请求优先并复位工具门。
# LLM: 内部正count只能沿实际原C取消请求并继续服务；未实现B4滚动重启，不得进入Count0排空或接班。
def drain_for_requested_restart(context: GatewayRunContext, process_identity: dict[str, object]) -> dict | None:
    paths = context.paths
    request = pending_restart_request(paths, pid=os.getpid())
    if request is None:
        return None
    if getattr(context, "coordinator_worker_count", 0) > 0:
        _cancel_internal_restart(context, request)
        return None
    agent = context.agent
    config = agent.config
    request_id = str(request.get("request_id") or "")
    background_shutdown = getattr(context, "background_shutdown", None)

    # LLM: 仅合计前台准入与后台纯内存片计数，不把blockers/数据库/指标投影当生命周期权威。
    # 函数用途: 让后台发起重启的当前回合也能在开门阶段完成工具收尾。
    def active_turns() -> int:
        foreground = int(admission.snapshot().get("total") or 0)
        background = (int(background_shutdown.active_slice_count())
                      if background_shutdown is not None and hasattr(background_shutdown, "active_slice_count") else 0)
        return foreground + background

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
        active_turns=active_turns,
        turn_wait_seconds=float(config.gateway_restart_turn_wait_seconds),
        drain_timeout_seconds=float(config.gateway_restart_drain_timeout_seconds),
        force_on_drain_timeout=(str(request.get("operation") or "restart") == "restart"
                                and bool(getattr(config, "gateway_restart_force_on_drain_timeout", True))),
        stop_requested=stop_requested,
        on_progress=progress,
    )
    if result.get("ok") and stop_requested():
        reset_restart_drain_state()
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
    return _mark_drained_restart(context, request, result)


# LLM: 只复用原完成标记写者；强制超时事实同样传给持久标记和事件，不启动进程，不清理未退工具。
# 函数用途: 保存排空交接结果并生成主循环的重启结束报告。
def _mark_drained_restart(context, request: dict, result: dict) -> dict:
    marker = mark_restart_drained(
        context.paths, request, old_pid=os.getpid(),
        old_process_started_at=float(context.process_started_at or 0.0),
        active_turns_at_exit=int(result.get("active_turns_at_exit") or 0),
        forced_after_drain_timeout=bool(result.get("forced_after_drain_timeout")),
        executing_tools_at_exit=int(result.get("executing_tools_at_exit") or 0),
        executing_tools=result.get("executing_tools"),
    )
    log_gateway_event(context.agent, "gateway_restart_drained", {
        "request_id": str(request.get("request_id") or ""), "active_turns_at_exit": marker.get("active_turns_at_exit"),
        **({"forced_after_drain_timeout": True,
            "executing_tools_at_exit": int(marker.get("executing_tools_at_exit") or 0),
            **({"executing_tools": list(marker.get("executing_tools"))} if marker.get("executing_tools") else {})}
           if marker.get("forced_after_drain_timeout") else {}),
    })
    return {"summary": "safe restart drained", "restart": marker}


# LLM: 仅实际同代C按原controller→lease保护取消→state读回→publisher，首次写前核同source；不从业务隔离/closing重签或拒失败元数据。
# 函数用途: 对尚未实现的内部多进程热重启明确拒绝，让原服务继续工作。
def _cancel_internal_restart(context, request: dict) -> None:
    from ..agent.gateway_parts.control_plane import GatewayControlPlane
    from ..agent.gateway_parts.multiprocess_coordinator import CoordinatorUnavailable
    from ..agent.gateway_parts.multiprocess_takeover_runtime import CoordinatorTakeover
    from .gateway_coordinated_run import (
        coordinated_state_fields,
        coordinated_state_path,
        publish_coordinated_state,
    )

    control = context.control_plane
    original = getattr(control, "_coordinator_role_lease", None)
    controller = control.controller() if type(control) is GatewayControlPlane else None
    if (type(controller) is not CoordinatorTakeover or original is None
            or original.controller is not controller or controller._lease is not original):
        raise CoordinatorUnavailable("COORDINATOR_CONTEXT_REQUIRED")
    facts = {"ok": False, "error_code": MULTIPROCESS_RESTART_ERROR_CODE,
             "reason": "internal_multiprocess_unsupported", "request_id": request.get("request_id", "")}
    with controller._mutex, original._lifecycle_gate:
        if (context.control_plane is not control or control.controller() is not controller
                or original.controller is not controller or controller._lease is not original
                or controller.paths is not control.paths or controller.count != control.worker_count
                or controller.c_run_id != control.c_run_id):
            raise CoordinatorUnavailable("COORDINATOR_CONTEXT_REQUIRED")
        coordinated_state_fields(context)
        coordinated_state_path(context)
        cancel_restart_request(context.paths, request, facts)
        state = read_json_file(coordinated_state_path(context))
        publish_coordinated_state(context, {**state, "restart_drain": {**facts, "phase": "cancelled"}})
    if context.agent is not None:
        log_gateway_event(context.agent, "gateway_restart_cancelled", facts)
    print(json.dumps(facts, sort_keys=True), file=sys.stderr)


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
# LLM: 内部正count在管理器探测/spawn前拒绝，避免原W/工具未收口时启动无锁接班；Count0原体保持。
def hand_over_to_successor(context: GatewayRunContext, *, pid: int, command: list[str], spawn, write_start_files) -> int:
    if getattr(context, "coordinator_worker_count", 0) > 0:
        from .gateway_coordinated_run import coordinated_refusal

        return coordinated_refusal(MULTIPROCESS_RESTART_ERROR_CODE)
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


# LLM: 终端 `my-agent gateway restart` 的默认路径：Gateway 在跑时写一份 kind=cli 的安全重启请求，按状态文件里的结构化事实
# 等新进程号 running（或本请求被取消）；Gateway 没在跑返回 None，由调用方走原来的启动路径。托管自己的工具进程仍被拒绝，
# 否则它自己就是“执行中的工具”，第二段会一直等到超时。改动须同步 test_gateway_safe_restart.py 与 CLI_REFERENCE.md。
# 函数用途: 从终端安排安全重启并等待结果；返回退出码，Gateway 未运行时返回 None。
def safe_restart_from_cli(agent, paths) -> int | None:
    pid = get_running_pid(paths.pid)
    if not pid or not is_pid_alive(pid):
        return None
    if refuse_stopping_hosting_gateway(pid):
        return 2
    config = agent.config
    result = submit_restart_request(
        paths, target_pid=pid, requester={"kind": "cli"}, reason="终端执行 my-agent gateway restart",
        cooldown_seconds=float(getattr(config, "gateway_restart_cooldown_seconds", 30) or 0),
    )
    if result.get("status") in {"cooldown", "loop_guard"}:
        retry = result.get("retry_after_seconds")
        hint = f"刚完成过一次安全重启，请 {retry} 秒后再试" if retry is not None else "短时间内重启次数过多"
        print(f"{hint}；确需立即重启可加 --force 先停后起。", file=sys.stderr)
        return 2
    request_id = str((result.get("request") or {}).get("request_id") or "")
    print(f"已安排安全重启 request_id={request_id}：等在跑的回合和执行中的工具结束后换新进程……", flush=True)
    drain = float(getattr(config, "gateway_restart_drain_timeout_seconds", 600) or 3600)
    budget = float(getattr(config, "gateway_restart_turn_wait_seconds", 300) or 0) + drain + 120.0
    return _wait_for_cli_restart(paths, pid, request_id, budget)


def _wait_for_cli_restart(paths, old_pid: int, request_id: str, budget: float) -> int:
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        state = read_json_file(paths.state)
        drain = state.get("restart_drain") if isinstance(state.get("restart_drain"), dict) else {}
        if drain.get("request_id") == request_id and drain.get("phase") == "cancelled":
            print(f"安全重启已取消（{drain.get('reason')}），Gateway 照常运行；确需立即重启可加 --force。", file=sys.stderr)
            return 2
        new_pid = get_running_pid(paths.pid)
        if new_pid and new_pid != old_pid and state.get("status") == "running" and int(state.get("pid") or 0) == new_pid:
            print(f"gateway 已安全重启 pid {old_pid} → {new_pid}")
            return 0
        time.sleep(0.5)
    print("等待安全重启超时：Gateway 可能仍在排空，可用 my-agent gateway status 查看。", file=sys.stderr)
    return 2


__all__ = [
    "PREDECESSOR_EXIT_WAIT_SECONDS",
    "announce_restart_completed",
    "drain_for_requested_restart",
    "hand_over_to_successor",
    "safe_restart_from_cli",
    "service_manager_restarts_gateway",
    "take_restart_marker",
    "wait_for_predecessor_exit",
]


# LLM: 停止使用原 restart 请求、两段排空和完成标记，仅终态 operation 区分不拉起旧版本；晚到旧请求不能换目标进程。
# 函数用途: 为离线更新排空并停止准确 Gateway，超时保留原服务、不强杀工具；新包成功后原入口恢复。
def safe_stop_for_update(agent,paths,pid):
    result=submit_restart_request(paths,target_pid=pid,requester={'kind':'cli'},reason='离线更新排空',
        cooldown_seconds=0,operation='stop_for_update')
    if result.get('status') not in {'scheduled','coalesced'}:
        print('当前有其它重启/排空请求，请等待完成后更新。',file=sys.stderr)
        return 2
    request_id=result['request']['request_id']
    budget=float(agent.config.gateway_restart_turn_wait_seconds)+float(agent.config.gateway_restart_drain_timeout_seconds or 3600)+120
    deadline=time.monotonic()+budget
    print('更新正在等待原回合和工具排空，不会强杀执行中的工具。',flush=True)
    while time.monotonic()<deadline:
        if not is_pid_alive(pid):
            return 0
        state=read_json_file(paths.state)
        phase=state.get('restart_drain') or {}
        if phase.get('request_id')==request_id and phase.get('phase')=='cancelled':
            print('排空取消，原实例保持运行，未更新。',file=sys.stderr)
            return 2
        time.sleep(.25)
    print('等待排空超时，未切换版本。',file=sys.stderr)
    return 2
