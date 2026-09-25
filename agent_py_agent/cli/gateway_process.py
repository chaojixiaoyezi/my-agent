

from __future__ import annotations

"""Public gateway command implementation.

Gateway process commands live in this module directly so CLI registration,
tests, and runtime entrypoints all target the same command surface.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..agent.auth.manager import AuthManager
from ..agent.auth.middleware import AuthMiddleware
from ..agent.gateway_parts import (
    GatewayPaths,
    gateway_paths,
    gateway_request_counts,
    gateway_running,
    is_pid_alive,
    log_gateway_event,
    recover_gateway_processing_requests,
    tail_lines,
    terminate_pid,
    wait_for_gateway_running,
    wait_for_pid_exit,
    write_json_file,
)
from ..agent.gateway_parts.daemon_control import (
    _get_process_start_time,
    _utc_now_iso,
    build_process_identity,
    get_running_pid,
    read_pid_record,
    remove_gateway_stop_request_if_owned,
    remove_pid_file_if_owned,
    stop_request_targets_process,
    write_pid_record,
    write_targeted_gateway_stop_request,
)
from ..agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
    start_http_server,
)
from ..agent.gateway_parts.io import read_json_file, read_json_file_report, write_json_file_atomic

# LLM: 就绪判据只有一份实现，位于 agent.gateway_parts.status_rendering：state/heartbeat 任一匹配 PID、
#   且记录属于本次 spawn 的那一代（not_before − 容差）、且 status=running 才算就绪。这里只导入转发，
#   禁止在本文件或任何调用方再写第二套"PID 活就算好"的判断。
from ..agent.gateway_parts.status_rendering import (
    _gateway_ready_for_pid,
    _record_is_current_generation,
    wait_for_gateway_readiness,
)
from ..agent.runtime_errors import runtime_error_report
from .common import ROOT, make_agent
from .gateway_loops import (
    _gateway_background_main_loop,
    _gateway_heartbeat_loop,
    _gateway_request_loop,
    _write_gateway_heartbeat,
)
from .gateway_service import (
    install_service,
    uninstall_service,
)
from .models import (
    GatewayRunCleanupRequest,
    GatewayRunContext,
    GatewayStartOptions,
    GatewayThreadsRequest,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _GatewayRunBuildRequest:
    agent: object
    paths: GatewayPaths
    args: argparse.Namespace


@dataclass(frozen=True)
class _GatewayServiceTermination:
    status: str
    kind: str
    reason: str
    summary: str
    exit_code: int
    details: dict[str, object] | None = None


def _gateway_start_options_from_args(args, *, workspace_root: str | Path = "") -> GatewayStartOptions:
    return GatewayStartOptions(
        config=Path(args.config),
        workspace_root=str(workspace_root or ""),
    )


def _gateway_run_context_from_args(request: _GatewayRunBuildRequest) -> GatewayRunContext:
    args = request.args
    return GatewayRunContext(
        agent=request.agent,
        paths=request.paths,
        config_path=Path(args.config),
        log_start_offset_bytes=_gateway_log_size(request.paths.log),
        process_identity=build_process_identity(),
        process_started_at=time.time(),
    )


# LLM: 当前生命周期日志起点必须在同一个 Gateway 进程内按字节记录；诊断工具只能
#   以它区分旧进程遗留噪声和本次运行新增噪声，读取失败按 0 明示降级。
# 函数用途: 记录 Gateway 进入运行阶段时日志已有大小，供后续只检查本次生命周期。
def _gateway_log_size(path: Path) -> int:
    try:
        return max(0, int(path.stat().st_size))
    except OSError:
        return 0


# LLM: This is the canonical state-file projection for one live Gateway lifecycle. Keep endpoint,
# config/model identity and log boundary here so CLI, HTTP and model tools read the same facts.
# 函数用途: 生成 Gateway 当前运行状态文件，供健康检查和运维工具权威读取。
def _build_run_state(request: GatewayThreadsRequest, pid: int, status: str = "running") -> dict:
    context = request.context
    paths = context.paths
    agent = context.agent
    return {
        "status": status,
        "pid": pid,
        "gateway_workspace": str(paths.root),
        "subagent_workspace": str(agent.subagents.workspace),
        "task_continuation": "owner_scoped_event_driven",
        "requeued_requests": request.requeued,
        "failed_processing_requests": request.failed,
        "user_inflight_limit": max(1, int(getattr(agent.config, "gateway_user_inflight_limit", 8) or 8)),
        "global_inflight_limit": max(1, int(getattr(agent.config, "gateway_global_inflight_limit", 500) or 500)),
        "config_path": str(context.config_path),
        "model_name": str(getattr(agent.config, "model_name", "") or ""),
        "http_bind_host": str(
            getattr(agent.config, "gateway_bind_host", "127.0.0.1") or "127.0.0.1"
        ),
        "http_port": request.http_port,
        "log_start_offset_bytes": max(0, int(context.log_start_offset_bytes or 0)),
        "process_identity": dict(context.process_identity or {}),
        "started_at": float(context.process_started_at or time.time()),
        # 客户端用它判断自己是否与 Gateway 同一安装；只写 sys.prefix 这一结构化事实，不写版本文案。
        "runtime_prefix": sys.prefix,
    }


# LLM: The structured event mirrors the same lifecycle identity as _build_run_state; it may add
# event-only fields but must not invent a different endpoint, model or log offset.
# 函数用途: 生成 Gateway 运行事件载荷，供审计日志记录本次生命周期的同一组事实。
def _build_run_payload(request: GatewayThreadsRequest, pid: int, extra: dict | None = None) -> dict:
    context = request.context
    agent = context.agent
    payload = {
        "status": "running",
        "pid": pid,
        "gateway_workspace": str(context.paths.root),
        "subagent_workspace": str(agent.subagents.workspace),
        "task_continuation": "owner_scoped_event_driven",
        "requeued_requests": request.requeued,
        "failed_processing_requests": request.failed,
        "user_inflight_limit": max(1, int(getattr(agent.config, "gateway_user_inflight_limit", 8) or 8)),
        "global_inflight_limit": max(1, int(getattr(agent.config, "gateway_global_inflight_limit", 500) or 500)),
        "config_path": str(context.config_path),
        "model_name": str(getattr(agent.config, "model_name", "") or ""),
        "http_bind_host": str(
            getattr(agent.config, "gateway_bind_host", "127.0.0.1") or "127.0.0.1"
        ),
        "http_port": request.http_port,
        "log_start_offset_bytes": max(0, int(context.log_start_offset_bytes or 0)),
        "process_identity": dict(context.process_identity or {}),
        "started_at": float(context.process_started_at or time.time()),
    }
    if extra:
        payload.update(extra)
    return payload


def _clear_gateway_stop_request(paths) -> None:
    try:
        paths.stop_request.unlink()
    except OSError:
        pass


# LLM: CLI start/restart writes the same target-bound stop contract as HTTP, supervisor, and
# signals. Never restore a presence-only marker that can terminate the successor process.
# 函数用途: 向指定的当前 Gateway 进程写入一份不会串到下次启动的停止请求。
def _write_gateway_stop_request(
    paths: GatewayPaths,
    *,
    reason: str,
    target_pid: int | None = None,
) -> dict[str, object] | None:
    return write_targeted_gateway_stop_request(
        paths.pid,
        paths.stop_request,
        reason=reason,
        target_pid=target_pid,
        source="gateway_cli",
    )


def _gateway_start_command(options: GatewayStartOptions) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "agent_py_agent",
        "--config",
        str(options.config.expanduser().resolve()),
        "gateway",
        "run",
    ]
    if options.workspace_root:
        command.extend(["--workspace-root", str(Path(options.workspace_root).expanduser().resolve())])
    return command


def _gateway_popen_options() -> tuple[int, bool]:
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return flags, False
    return 0, True


def _spawn_gateway_process(paths, command: list[str], *, cwd: Path):
    creationflags, start_new_session = _gateway_popen_options()
    with paths.log.open("ab") as log_file:
        return subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )


def _write_gateway_start_files(paths, *, pid: int, command: list[str]) -> None:
    paths.pid.parent.mkdir(parents=True, exist_ok=True)
    write_json_file(
        paths.pid,
        {
            "pid": pid,
            "kind": "my-agent-gateway",
            "argv": command,
            "start_time": _get_process_start_time(pid),
            "updated_at": _utc_now_iso(),
        },
    )


# LLM: cmd_gateway_start 的就绪等待复用唯一权威：子进程存活事实来自 spawn 方 poll()，代际下界是本函数
#   记录的时刻。预算、发布顺序与返回语义不变——就绪 True，子进程先死或超时 False（cmd_gateway_start
#   统一映射成退出码 2）；具体失败分型由 status_rendering 的 state/reason 提供。
# 函数用途: 在给定秒数内等待本次 spawn 的子进程真正发布 running 记录。
def _wait_for_gateway_start_ready(paths: GatewayPaths, process, *, timeout: float) -> bool:
    outcome = wait_for_gateway_readiness(
        paths,
        timeout,
        process=process,
        not_before=time.time(),
    )
    return outcome.ready


# LLM: 就绪预算必须可被调用方显式加长：默认 3 秒对冷启动（大 owner home、索引重建、排水刚结束）
# 太短，会让"进程其实正在起来"被报成启动失败（exit 2）。显式值优先于配置默认值，配置默认值
# 优先于内置 3 秒；这里只决定等多久，不改变就绪判据本身。
# 函数用途: 解析本次 gateway start/restart 的就绪等待预算秒数。
def _gateway_ready_budget_seconds(agent: object, requested: object) -> float:
    config = getattr(agent, "config", None)
    fallback = float(getattr(config, "gateway_ready_timeout_seconds", 3) or 3)
    try:
        explicit = float(requested) if requested is not None else 0.0
    except (TypeError, ValueError):
        explicit = 0.0
    return explicit if explicit > 0 else fallback


# LLM: Ordinary CLI attempts are reconciled by the long-lived Gateway before workers start. This
# preserves the process-death proof in RuntimeRepository while keeping status and TUI startup pure.
# 函数用途: Gateway 启动时收敛已证实宿主死亡的普通 run attempt，返回结构化数量和错误。
def _recover_gateway_stale_attempts(agent: object) -> dict[str, object]:
    repo = getattr(getattr(agent, "subagents", None), "runtime_db", None)
    recover = getattr(repo, "recover_stale_attempts", None)
    if not callable(recover):
        return {"run_ids": [], "count": 0, "error": None, "unidentified": []}
    try:
        run_ids = [str(item) for item in (recover() or []) if str(item)]
    except Exception as exc:  # noqa: BLE001 Gateway 仍需启动并暴露结构化恢复错误
        return {
            "run_ids": [],
            "count": 0,
            "error": runtime_error_report(exc, context="gateway.startup.stale_attempts"),
            "unidentified": [],
        }
    return {"run_ids": run_ids, "count": len(run_ids), "error": None, "unidentified": _unidentified_stale_attempts(repo)}


# LLM: 只读事实：没有 runner 身份的悬挂运行轮无法被自动判死，也不能静默留着；启动时把它们的 run_id 列出来写进状态与事件，
#   由 `my-agent runtime settle-unidentified` 这类显式结构化命令结清。读取失败按空列表，不影响启动。
# 函数用途: 列出启动时仍无法证实死活的悬挂运行轮编号。
def _unidentified_stale_attempts(repo: object) -> list[str]:
    lister = getattr(repo, "unidentified_stale_attempts", None)
    if not callable(lister):
        return []
    try:
        return [str(item.get("run_id") or item.get("agent_run_id") or "") for item in (lister() or []) if isinstance(item, dict)]
    except Exception:  # noqa: BLE001 启动路径只暴露事实，不因只读查询失败而中断
        return []


# LLM: Gateway setup is the single startup mutation boundary. It rejects an overlapping live
# generation before touching state, then recovers durable queue claims and publishes startup facts.
# 函数用途: 先阻止两个 Gateway 重叠运行，再创建目录、调和崩溃遗留并写入 starting 状态。
def _cmd_gateway_run_setup(agent, paths):
    pid = os.getpid()
    existing_pid = get_running_pid(paths.pid)
    if existing_pid and existing_pid != pid:
        raise RuntimeError(f"gateway already running pid={existing_pid}")
    for path in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses):
        path.mkdir(parents=True, exist_ok=True)
    recovery = recover_gateway_processing_requests(
        paths,
        startup=True,
        max_attempts=agent.config.gateway_request_max_attempts,
        timeout_seconds=agent.config.gateway_processing_timeout_seconds,
        agent=agent,
    )
    requeued = recovery["requeued"]
    attempt_recovery = _recover_gateway_stale_attempts(agent)
    write_pid_record(paths.pid)
    write_json_file(
        paths.state,
        {
            "status": "starting",
            "pid": pid,
            "gateway_workspace": str(paths.root),
            "subagent_workspace": str(agent.subagents.workspace),
            "requeued_requests": requeued,
            "failed_processing_requests": recovery["failed"],
            "recovered_stale_attempts": attempt_recovery["count"],
            "stale_attempt_recovery_error": attempt_recovery["error"],
            "unidentified_stale_attempts": len(attempt_recovery["unidentified"]),
            "started_at": time.time(),
        },
    )
    if attempt_recovery["count"] or attempt_recovery["error"] or attempt_recovery["unidentified"]:
        log_gateway_event(
            agent,
            "gateway_stale_attempts_reconciled",
            attempt_recovery,
        )
    print(f"[gateway-run] status=starting pid={pid}", flush=True)
    log_gateway_event(
        agent,
        "gateway_run_started",
        {
            "status": "starting",
            "pid": pid,
            "gateway_workspace": str(paths.root),
            "subagent_workspace": str(agent.subagents.workspace),
            "requeued_requests": requeued,
            "failed_processing_requests": recovery["failed"],
            "recovered_stale_attempts": attempt_recovery["count"],
            "stale_attempt_recovery_error": attempt_recovery["error"],
            "unidentified_stale_attempts": len(attempt_recovery["unidentified"]),
        },
    )
    return requeued, pid


def _build_gateway_auth_middleware(config) -> AuthMiddleware | None:
    """按 config 接线鉴权:auth_enabled 时返回强制鉴权的中间件;关闭则返回 None。

    返回 None 时,网关只允许绑回环(http_service._guard_network_exposure fail-closed),
    不会出现"绑 0.0.0.0 + 无鉴权"的未认证远程入口。每个请求均经过鉴权校验。
    """
    if not getattr(config, "auth_enabled", True):
        return None
    manager = AuthManager(admin_user_id=getattr(config, "admin_user_id", "admin"), auth_enabled=True)
    return AuthMiddleware(manager, auth_token=getattr(config, "gateway_auth_token", ""))


def _cmd_gateway_run_threads(request: GatewayThreadsRequest):
    context = request.context
    paths = context.paths
    agent = context.agent
    stop_event = threading.Event()
    heartbeat_thread = threading.Thread(
        target=_gateway_heartbeat_loop,
        args=(context, stop_event),
        daemon=True,
    )
    request_thread = threading.Thread(
        target=_gateway_request_loop,
        args=(context, paths, stop_event),
        daemon=True,
    )
    request_thread.start()
    background_thread = threading.Thread(
        target=_gateway_background_main_loop,
        args=(context, stop_event),
        daemon=True,
    )
    background_thread.start()
    http_server: GatewayHTTPServer | None = None
    http_port = request.http_port
    pid = os.getpid()
    if http_port > 0:
        http_params = GatewayHTTPServerParams(
            auth_middleware=_build_gateway_auth_middleware(agent.config),
            bind_host=getattr(agent.config, "gateway_bind_host", "127.0.0.1"),
            agent=agent,
        )
        http_server = start_http_server(http_port, paths, params=http_params)
    # 就绪信号只能在这里之后发：heartbeat 一旦写了 "running"，父进程 start
    # 就认定网关就绪。若在 HTTP bind 之前启动 heartbeat 线程，绑定失败崩溃时
    # start 会对一个已死的网关谎报成功（旧时序：heartbeat 先写 running，再 bind）。
    heartbeat_thread.start()
    write_json_file(paths.state, _build_run_state(request, pid))
    log_gateway_event(agent, "gateway_run_running", _build_run_payload(request, pid))
    print(
        "[gateway-run] "
        f"status=running pid={pid} inflight_limits=user:{max(1, int(getattr(agent.config, 'gateway_user_inflight_limit', 8) or 8))}"
        f"/global:{max(1, int(getattr(agent.config, 'gateway_global_inflight_limit', 500) or 500))} "
        f"http_port={http_port}",
        flush=True,
    )
    return stop_event, heartbeat_thread, request_thread, background_thread, http_server


# LLM: 只负责收三条循环并报告仍存活的线程名；未启动线程（ident 为 None）跳过 join，每条最多等 2 秒。
# 函数用途: Gateway 收尾时等 heartbeat/request/background 三条循环退出，返回还活着的线程名列表。
def _join_gateway_loops(request: GatewayRunCleanupRequest) -> list[str]:
    threads = {
        "heartbeat": request.heartbeat_thread,
        "request": request.request_thread,
        "background": request.background_thread,
    }
    for thread in threads.values():
        if thread.ident is None:
            # 未启动的线程（HTTP bind 失败时 heartbeat 线程还没 start）不能 join
            continue
        thread.join(timeout=2)
    return [name for name, thread in threads.items() if thread.is_alive()]


# LLM: 排空窗口之后才结清：这时仍在途的模型调用会随进程退出被切断，由唯一账本按同一原因码记 failed（用量按缺报），
#   有在途调用才写结构化事件 gateway_model_calls_interrupted（只含身份/用途/时长字段，不含正文）；账本模块出错只记
#   异常类型事件 gateway_model_call_settlement_failed，不能中断收尾。只覆盖 Gateway 进程 agent 自己的账本。
# 函数用途: Gateway 停止时给还没结清的前台/后台模型调用留下"被中断、未结算"的事实，返回条数。
def _settle_interrupted_model_calls(request: GatewayRunCleanupRequest, *, drain_complete: bool) -> int:
    agent = request.context.agent
    try:
        from ..agent.agent_core.model.call_runtime import settle_open_model_calls_for_shutdown

        interrupted = settle_open_model_calls_for_shutdown(agent)
    except Exception as exc:  # noqa: BLE001 - 停止收尾不能因账本模块出错而中断
        log_gateway_event(agent, "gateway_model_call_settlement_failed", {"error_type": type(exc).__name__})
        return 0
    if interrupted:
        log_gateway_event(
            agent,
            "gateway_model_calls_interrupted",
            {
                "status": "cleanup",
                "pid": request.pid,
                "termination_status": request.termination_status,
                "drain_complete": drain_complete,
                "count": len(interrupted),
                "calls": list(interrupted),
            },
        )
    return len(interrupted)


# LLM: 停止收尾顺序固定：先置停止事件，再取消本进程在途决策，再停 HTTP 与收三条循环，排空窗口后把仍在途的模型调用
#   记成被停机中断（只记结构化事实，不猜用量），最后清 pid/停止请求并写心跳与收尾事件；决策取消或账本结清出错只记
#   异常类型事件，不能中断后续清理。改动须同步 test_gateway_decision_shutdown_cancel.py、
#   test_gateway_model_call_shutdown_settlement.py 与 Gateway 停止相关回归。
# 函数用途: Gateway 停止时收尾三条循环、结清在途模型调用、清理 pid/停止请求并写出是否完整排空的状态。
def _cmd_gateway_run_cleanup(request: GatewayRunCleanupRequest) -> dict[str, object]:
    """Drain the three gateway loops and persist whether shutdown was complete."""
    request.stop_event.set()
    # 决策线：只取消本进程内登记的在途决策句柄，让等待中的可选增强立即回到原方案；不读写任何持久状态。
    try:
        from ..agent.conversation.decision_policy import cancel_active_decisions_for_shutdown

        cancel_active_decisions_for_shutdown()
    except Exception as exc:  # noqa: BLE001 - 停止收尾不能因可选决策模块出错而中断
        log_gateway_event(request.context.agent, "gateway_decision_cancel_failed", {"error_type": type(exc).__name__})
    if request.http_server:
        request.http_server.stop()
    alive_threads = _join_gateway_loops(request)
    drain_complete = not alive_threads
    interrupted_model_calls = _settle_interrupted_model_calls(request, drain_complete=drain_complete)
    surviving_background_sessions = _record_surviving_background_sessions(request)
    paths = request.context.paths
    try:
        remove_pid_file_if_owned(paths.pid)
    except OSError:
        pass
    remove_gateway_stop_request_if_owned(
        paths.stop_request,
        _gateway_context_process_identity(request.context),
        process_started_at=request.context.process_started_at,
    )
    _write_gateway_heartbeat(
        paths,
        request.context.agent,
        status=request.termination_status,
        pid=request.pid,
    )
    cleanup_payload = {
        "status": "cleanup",
        "pid": request.pid,
        "termination_status": request.termination_status,
        "termination_reason": request.termination_reason,
        "drain_complete": drain_complete,
        "alive_threads": alive_threads,
        "interrupted_model_calls": interrupted_model_calls,
        "surviving_background_sessions": surviving_background_sessions,
        "updated_at": time.time(),
    }
    log_gateway_event(
        request.context.agent,
        "gateway_run_cleanup",
        cleanup_payload,
    )
    print(
        "[gateway-run] "
        f"status={request.termination_status} pid={request.pid} "
        f"reason={request.termination_reason or 'unspecified'} drain_complete={str(drain_complete).lower()} "
        f"interrupted_model_calls={interrupted_model_calls} surviving_background_sessions={surviving_background_sessions}",
        flush=True,
    )
    return cleanup_payload


# LLM: 停机时受管后台进程按设计继续存活；这里只在排空后列一次仍未终态的会话并写事件 gateway_background_sessions_surviving
#   （count + 每条投影，含监听范围事实），扫描出错只记 gateway_background_sessions_scan_failed 的异常类型；不停止任何进程。
#   改事件名或字段要同步 test_gateway_background_sessions_shutdown.py。
# 函数用途: Gateway 停止时告诉用户还有哪些后台进程会继续跑，返回条数。
def _record_surviving_background_sessions(request: GatewayRunCleanupRequest) -> int:
    agent = request.context.agent
    try:
        from ..agent.gateway_parts.background_sessions import surviving_background_sessions

        rows = surviving_background_sessions(agent)
    except Exception as exc:  # noqa: BLE001 - 停止收尾不能因后台会话目录出错而中断
        log_gateway_event(agent, "gateway_background_sessions_scan_failed", {"error_type": type(exc).__name__})
        return 0
    if rows:
        log_gateway_event(
            agent,
            "gateway_background_sessions_surviving",
            {
                "status": "cleanup",
                "pid": request.pid,
                "termination_status": request.termination_status,
                "count": len(rows),
                "sessions": list(rows),
            },
        )
    return len(rows)


# LLM: 停机 state 已由 termination 记录写好；这里只把停机后仍存活的后台会话数并进 state.json 供 my-agent status 投影，
#   不改 status/termination 字段，不写会话明细（明细在 gateway_background_sessions_surviving 事件里）。
# 函数用途: 把"停机后还有 N 条后台进程在跑"写进 Gateway 状态文件。
def _record_surviving_background_sessions_state(paths: GatewayPaths, cleanup: dict[str, object]) -> None:
    prior = read_json_file(paths.state)
    payload = {
        **prior,
        "surviving_background_sessions": int(cleanup.get("surviving_background_sessions") or 0),
        "updated_at": time.time(),
    }
    write_json_file(paths.state, payload)


# LLM: The context identity is captured before startup mutations. Tests/legacy embedders that did
# not populate it fall back to this process, but production never derives identity from a PID file
# that a successor may already have replaced.
# 函数用途: 取得当前 Gateway 生命周期自身的进程指纹，供停止等待、分类和清理复用。
def _gateway_context_process_identity(context: GatewayRunContext) -> dict[str, object]:
    identity = dict(context.process_identity or {})
    return identity or build_process_identity()


# LLM: Service wait accepts only a stop marker matching this exact process generation. Stale or
# malformed files remain observable on disk but cannot terminate a freshly started Gateway.
# 函数用途: 等待真正发给当前 Gateway 的停止请求，忽略上一代遗留文件。
def _run_gateway_service_loop(context: GatewayRunContext) -> object:
    """Keep the Gateway alive without running a global model-driven planner.

    Active user turns run in the request pool and durable continuations run in
    the owner-scoped background loop.  A process-wide dispatch watch has no
    owner authority and can repeatedly spend model calls on stale local/main
    records, so the service thread only waits for the explicit stop record.
    """
    process_identity = _gateway_context_process_identity(context)
    while True:
        if context.paths.stop_request.exists():
            payload = read_json_file(context.paths.stop_request)
            if stop_request_targets_process(
                payload,
                process_identity,
                process_started_at=context.process_started_at,
            ):
                return {"summary": "stop requested", "stop_request": payload}
        time.sleep(0.25)


# LLM: SIGTERM/SIGINT 不得再表现成“Python 正常消失”；handler 只写一份小型结构化停止请求，
# 让现有 watch stop_file 路径负责退出和 drain。它不猜信号发送者，也不在 handler 里跑重诊断。
# 函数用途: 安装网关信号处理器并返回原 handler，调用方在退出后必须恢复以免污染测试/嵌入运行。
def _install_gateway_signal_handlers(paths: GatewayPaths) -> dict[int, object]:
    previous: dict[int, object] = {}

    def handler(signum: int, _frame: object) -> None:
        _record_gateway_signal_stop_request(paths, signum)

    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, handler)
    return previous


# LLM: 恢复动作只接受本函数安装前捕获的 handler 表，不从磁盘或模型内容决定信号行为。
# 函数用途: 在网关主循环结束后还原宿主进程原有 SIGTERM/SIGINT 处理方式。
def _restore_gateway_signal_handlers(previous: dict[int, object]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


# LLM: 快照保留 signal、父进程和 systemd 环境事实；只有属于本代进程的已有停止请求
# 才追加 observed，上一代遗留文件必须被本次 signal 的精确目标记录替换。
# 函数用途: 原子写入绑定当前进程代次、可供等待和事后审计共同读取的信号停止请求。
def _record_gateway_signal_stop_request(paths: GatewayPaths, signum: int) -> dict[str, object]:
    observed_at = time.time()
    existing = read_json_file(paths.stop_request) if paths.stop_request.exists() else {}
    process_identity = build_process_identity()
    signal_name = signal.Signals(signum).name if signum in signal.Signals.__members__.values() else str(signum)
    snapshot: dict[str, object] = {
        "number": int(signum),
        "name": signal_name,
        "observed_at": observed_at,
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "parent_cmdline": _linux_process_cmdline(os.getppid()),
        "systemd": {
            key.lower(): str(os.environ.get(key) or "")
            for key in ("INVOCATION_ID", "JOURNAL_STREAM", "NOTIFY_SOCKET")
            if os.environ.get(key)
        },
        "preexisting_stop_request": bool(existing),
    }
    if existing and stop_request_targets_process(existing, process_identity):
        payload = {
            **existing,
            "target_process": process_identity,
            "signal_observed": snapshot,
        }
    else:
        payload = {
            "requested_at": observed_at,
            "reason": f"received {signal_name}",
            "source": "signal",
            "planned": False,
            "target_process": process_identity,
            "signal": snapshot,
        }
    write_json_file_atomic(paths.stop_request, payload)
    return payload


def _linux_process_cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()[:4096]
    except OSError:
        return ""
    return " ".join(part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part)


def _classify_gateway_service_return(
    context: GatewayRunContext,
    report: object,
) -> _GatewayServiceTermination:
    """A service loop may return cleanly only after an explicit stop record."""
    if isinstance(report, dict):
        summary = str(report.get("summary") or "")
    else:
        summary = str(getattr(report, "summary", "") or "")
    stop_payload: dict[str, object] | None = None
    if isinstance(report, dict) and isinstance(report.get("stop_request"), dict):
        stop_payload = dict(report["stop_request"])
    elif context.paths.stop_request.exists():
        stop_payload = read_json_file(context.paths.stop_request)
    if stop_payload is not None and stop_request_targets_process(
        stop_payload,
        _gateway_context_process_identity(context),
        process_started_at=context.process_started_at,
    ):
        reason = str(stop_payload.get("reason") or "stop requested")
        if str(stop_payload.get("source") or "") == "signal":
            return _GatewayServiceTermination(
                "stopped",
                "signal_shutdown",
                reason,
                summary,
                0,
                {"signal": dict(stop_payload.get("signal") or {})},
            )
        details = {"signal_observed": dict(stop_payload.get("signal_observed") or {})} if stop_payload.get("signal_observed") else None
        return _GatewayServiceTermination("stopped", "planned_stop", reason, summary, 0, details)
    return _GatewayServiceTermination(
        "failed",
        "unexpected_service_loop_return",
        "gateway service loop returned without a stop request",
        summary,
        2,
    )


def _record_gateway_service_termination(
    paths: GatewayPaths,
    agent: object,
    pid: int,
    termination: _GatewayServiceTermination,
) -> None:
    payload = {
        "status": termination.status,
        "pid": pid,
        "stopped_at": time.time(),
        "termination_kind": termination.kind,
        "termination_reason": termination.reason,
        "summary": termination.summary,
        "exit_code": termination.exit_code,
    }
    if termination.details:
        payload.update(termination.details)
    if termination.exit_code:
        payload["error_code"] = "GATEWAY_SERVICE_LOOP_UNEXPECTED_RETURN"
    write_json_file(paths.state, payload)
    event = "gateway_run_failed" if termination.exit_code else "gateway_run_stopped"
    log_gateway_event(agent, event, payload)


def _record_gateway_drain_failure(
    paths: GatewayPaths,
    agent: object,
    pid: int,
    cleanup: dict[str, object],
) -> None:
    prior = read_json_file(paths.state)
    payload = {
        **prior,
        "status": "failed",
        "pid": pid,
        "error_code": "GATEWAY_DRAIN_INCOMPLETE",
        "termination_kind": "drain_incomplete",
        "termination_reason": "gateway worker threads did not stop within the drain deadline",
        "alive_threads": list(cleanup.get("alive_threads") or []),
        "updated_at": time.time(),
        "exit_code": 2,
    }
    write_json_file(paths.state, payload)
    log_gateway_event(agent, "gateway_run_failed", payload)


def _record_gateway_run_interrupted(paths, agent, pid: int) -> None:
    payload = {"status": "interrupted", "pid": pid, "stopped_at": time.time()}
    write_json_file(paths.state, payload)
    log_gateway_event(agent, "gateway_run_interrupted", payload)


def _record_gateway_run_failed(paths, agent, pid: int, exc: Exception) -> None:
    payload = {"status": "failed", "pid": pid, "error": str(exc), "updated_at": time.time()}
    write_json_file(paths.state, payload)
    log_gateway_event(agent, "gateway_run_failed", payload)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_gateway_install(args) -> int:
    success = install_service(force=args.force)
    return 0 if success else 1


def cmd_gateway_uninstall(args) -> int:
    success = uninstall_service()
    return 0 if success else 1


def cmd_gateway_start(args) -> int:
    agent = make_agent(args)
    paths = gateway_paths(agent)
    paths.root.mkdir(parents=True, exist_ok=True)
    pid = get_running_pid(paths.pid)
    if pid and is_pid_alive(pid) and not args.force:
        record = read_pid_record(paths.pid)
        print(f"gateway 已在运行 pid={pid}")
        print(f"status: {paths.state}")
        if record:
            print(f"start_time: {record.get('start_time')}")
        return 0
    if pid and is_pid_alive(pid) and args.force:
        _write_gateway_stop_request(
            paths,
            reason="force restart before start",
            target_pid=pid,
        )
        if not wait_for_pid_exit(pid, agent.config.gateway_stop_timeout):
            terminate_pid(pid)
            wait_for_pid_exit(pid, 5)
    _clear_gateway_stop_request(paths)
    command = _gateway_start_command(_gateway_start_options_from_args(args, workspace_root=agent.root))
    process = _spawn_gateway_process(paths, command, cwd=ROOT.parent)
    _write_gateway_start_files(paths, pid=process.pid, command=command)
    ready = _wait_for_gateway_start_ready(
        paths,
        process,
        timeout=_gateway_ready_budget_seconds(agent, getattr(args, "ready_timeout", None)),
    )
    print(f"gateway starting pid={process.pid}")
    print(f"state: {paths.state}")
    print(f"log: {paths.log}")
    if not ready:
        # 失败必须分型：超时（可能仍在启动）、子进程已退出、服务自身发布 failed 对用户呈现不同 reason，
        # 但退出码语义保持不变，仍是 2。
        outcome = wait_for_gateway_readiness(paths, 0.0, process=process)
        print("gateway started but did not become ready before timeout", file=sys.stderr)
        print(
            f"  readiness={outcome.state} reason={outcome.reason} pid={process.pid}",
            file=sys.stderr,
        )
        return 2
    return 0


def cmd_gateway_run(args) -> int:
    agent = make_agent(args)
    paths = gateway_paths(agent)
    paths.root.mkdir(parents=True, exist_ok=True)
    run_context = _gateway_run_context_from_args(_GatewayRunBuildRequest(agent, paths, args))
    requeued, pid = _cmd_gateway_run_setup(agent, paths)
    http_port = getattr(agent.config, "gateway_port", 0) or 0
    stop_event, heartbeat_thread, request_thread, background_thread, http_server = _cmd_gateway_run_threads(
        GatewayThreadsRequest(context=run_context, requeued=requeued, failed=0, http_port=http_port)
    )

    previous_signal_handlers = _install_gateway_signal_handlers(paths)
    exit_code = 0
    termination_status = "failed"
    termination_reason = "gateway run ended before termination was classified"
    cleanup_report: dict[str, object] = {}
    try:
        report = _run_gateway_service_loop(run_context)
        termination = _classify_gateway_service_return(run_context, report)
        termination_status = termination.status
        termination_reason = termination.reason
        exit_code = termination.exit_code
        _record_gateway_service_termination(paths, agent, pid, termination)
        if exit_code:
            print(termination.reason, file=sys.stderr)
    except KeyboardInterrupt:
        _record_gateway_run_interrupted(paths, agent, pid)
        termination_status = "interrupted"
        termination_reason = "keyboard interrupt"
        exit_code = 130
    except Exception as exc:
        _record_gateway_run_failed(paths, agent, pid, exc)
        termination_status = "failed"
        termination_reason = f"{type(exc).__name__}: {exc}"
        print(str(exc), file=sys.stderr)
        exit_code = 2
    finally:
        try:
            cleanup_report = _cmd_gateway_run_cleanup(
                GatewayRunCleanupRequest(
                    context=run_context,
                    pid=pid,
                    stop_event=stop_event,
                    heartbeat_thread=heartbeat_thread,
                    request_thread=request_thread,
                    background_thread=background_thread,
                    http_server=http_server,
                    termination_status=termination_status,
                    termination_reason=termination_reason,
                )
            )
        finally:
            _restore_gateway_signal_handlers(previous_signal_handlers)
    if not bool(cleanup_report.get("drain_complete")):
        _record_gateway_drain_failure(paths, agent, pid, cleanup_report)
        exit_code = 2
    if cleanup_report.get("surviving_background_sessions"):
        _record_surviving_background_sessions_state(paths, cleanup_report)
    return exit_code


def cmd_gateway_status(args) -> int:
    agent = make_agent(args)
    paths = gateway_paths(agent)
    pid_record = read_pid_record(paths.pid)
    running = is_pid_alive(pid_record["pid"]) if pid_record else False
    state_report = read_json_file_report(paths.state, context="gateway.cli.status.state.read")
    state = state_report.payload
    http_port = getattr(agent.config, "gateway_port", 0)

    status = "running" if running else "stopped"
    state_status = _gateway_state_status_for_display(running, state)
    print(f"gateway status={status} pid={pid_record.get('pid') if pid_record else 'none'} state={state_status}")
    print(f"  workspace={paths.root}")
    if state_report.load_error:
        print("  state_load_error=" + json.dumps(state_report.load_error, ensure_ascii=False, sort_keys=True))
    if http_port > 0:
        print(f"  http_port={http_port}")

    if running:
        inbox_count = len(list(paths.inbox.glob("*.json"))) if paths.inbox.exists() else 0
        processing_count = len(list(paths.processing.glob("*.json"))) if paths.processing.exists() else 0
        print(f"  inbox={inbox_count} processing={processing_count}")
        print(f"  pid_file={paths.pid}")
        print(f"  state_file={paths.state}")

    if state:
        print(f"  status_detail={json.dumps(_gateway_state_detail_for_display(running, state), ensure_ascii=False)}")

    heartbeat_report = read_json_file_report(paths.heartbeat, context="gateway.cli.status.heartbeat.read")
    heartbeat = heartbeat_report.payload
    if heartbeat:
        print(f"  last_heartbeat={heartbeat.get('updated_at', 'none')}")
        print(f"  last_status={_gateway_state_status_for_display(running, heartbeat)}")
    if heartbeat_report.load_error:
        print("  heartbeat_load_error=" + json.dumps(heartbeat_report.load_error, ensure_ascii=False, sort_keys=True))
    return 0


def _gateway_state_status_for_display(running: bool, state: dict) -> str:
    status = str(state.get("status") or "unknown")
    if not running and status == "running":
        return "stale-running"
    return status


def _gateway_state_detail_for_display(running: bool, state: dict) -> dict:
    detail = dict(state)
    if not running and detail.get("status") == "running":
        detail["stale_state"] = True
        detail["stale_state_reason"] = "pid_not_alive"
    return detail


def cmd_gateway_stop(args) -> int:
    agent = make_agent(args)
    paths = gateway_paths(agent)
    pid = get_running_pid(paths.pid)
    if not pid:
        remove_pid_file_if_owned(paths.pid)
        print("gateway 未在运行")
        return 0

    stop_payload = write_targeted_gateway_stop_request(
        paths.pid,
        paths.stop_request,
        reason=args.reason or "user stop",
        target_pid=pid,
        source="gateway_cli",
    )
    if stop_payload is None:
        print(f"gateway stop target unavailable pid={pid}", file=sys.stderr)
        return 2
    log_gateway_event(
        agent,
        "gateway_stop_requested",
        {"status": "stop_requested", "pid": pid, "reason": args.reason or "user stop", "updated_at": time.time()},
    )
    timeout = args.timeout if args.timeout is not None else agent.config.gateway_stop_timeout
    if wait_for_pid_exit(pid, timeout):
        log_gateway_event(
            agent,
            "gateway_stopped",
            {"status": "stopped", "pid": pid, "updated_at": time.time()},
        )
        print(f"gateway stopped pid={pid}")
        return 0
    if args.kill and _force_kill_gateway(agent, paths, pid):
        return 0
    print(f"gateway stop requested but still running pid={pid}", file=sys.stderr)
    return 2


def _force_kill_gateway(agent, paths: GatewayPaths, pid: int) -> bool:
    terminate_pid(pid)
    if not wait_for_pid_exit(pid, 5):
        return False
    write_json_file(paths.state, {"status": "killed", "pid": pid, "stopped_at": time.time()})
    log_gateway_event(
        agent,
        "gateway_killed",
        {"status": "killed", "pid": pid, "updated_at": time.time()},
    )
    try:
        paths.pid.unlink()
    except OSError:
        pass
    print(f"gateway killed pid={pid}")
    return True


# LLM: restart 是"排水 + 冷启动"两段，预算必须端到端可见：--ready-timeout 显式优先；
# 没给就用 --timeout（用户把 --timeout 理解成"整个重启最多等多久"），都没给才落配置默认值。
# 禁止让 start 段永远只等内置 3 秒——那会把"仍在启动"误报成启动失败。
# 函数用途: 重启 gateway，并把就绪等待预算透传给 start 段。
def cmd_gateway_restart(args) -> int:
    stop_args = argparse.Namespace(
        config=args.config,
        timeout=args.timeout,
        kill=True,
        reason="gateway restart",
    )
    stop_code = cmd_gateway_stop(stop_args)
    if stop_code not in {0}:
        return stop_code
    explicit_ready = getattr(args, "ready_timeout", None)
    start_args = argparse.Namespace(
        config=args.config,
        force=True,
        ready_timeout=explicit_ready if explicit_ready is not None else args.timeout,
    )
    return cmd_gateway_start(start_args)


def cmd_gateway_logs(args) -> int:
    agent = make_agent(args)
    paths = gateway_paths(agent)
    if not paths.log.exists():
        print("没有日志文件")
        return 1
    lines = _current_gateway_log_lines(paths.log, args.lines or 50)
    for line in lines:
        print(line)
    return 0


def _current_gateway_log_lines(path: Path, line_count: int) -> list[str]:
    lines = tail_lines(path, 0)
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].startswith("[gateway-run] status=starting"):
            lines = lines[index:]
            break
    if line_count <= 0:
        return lines
    return lines[-line_count:]


__all__ = [
    "cmd_gateway_install",
    "cmd_gateway_start",
    "cmd_gateway_status",
    "cmd_gateway_stop",
    "cmd_gateway_restart",
    "cmd_gateway_logs",
    "cmd_gateway_uninstall",
    "cmd_gateway_run",
    "gateway_running",
    "get_running_pid",
    "gateway_paths",
    "make_agent",
    "read_json_file",
    "read_pid_record",
    "remove_pid_file_if_owned",
    "terminate_pid",
    "wait_for_gateway_running",
    "gateway_request_counts",
    "install_service",
    "uninstall_service",
    "_gateway_heartbeat_loop",
    "_gateway_request_loop",
    "_gateway_background_main_loop",
    "_write_gateway_heartbeat",
    "_gateway_ready_for_pid",
    "_record_is_current_generation",
    "wait_for_gateway_readiness",
]
