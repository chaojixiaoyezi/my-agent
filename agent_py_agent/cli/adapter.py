# LLM: 适配器生命周期复用原 Agent 数据根，启动通道前核对 G3 凭据；开关开且凭据不可读时拒绝启动，开关关时记一次降级 warning 后继续。
# 秘密只留宿主请求头，不能传给 daemon、日志或状态。
# 模块用途: 运行文件和 IM 适配器，接线实际短窗口与引用附件开关；凭据不可用时按 G2b 降级或拒绝无凭据客户端。
from __future__ import annotations

# LLM: 通道守护入口沿原 owner/runtime 路径和注册表生命周期运行；状态心跳须保留 requested_channel 供离线更新精确恢复。
# 模块用途: 启动、停止和查询 IM 通道进程，周期保存服务状态并处理退出清理。

"""CLI entrypoints for file and channel adapters.

Small helper functions keep daemon, foreground, and file-loop flows below soft limits.
"""

import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..agent.adapter import ChannelManager, FeishuAdapter, QQAdapter
from ..agent.common.log_redaction import redact_sensitive_text
from ..agent.gateway_parts import (
    AdapterPaths,
    adapter_paths,
    gateway_paths,
    gateway_running,
    process_file_adapter_once,
)
from ..agent.gateway_parts.daemon_control import (
    get_running_pid,
    remove_pid_file_if_owned,
    write_pid_record,
)
from ..agent.gateway_parts.io import write_json_file_atomic
from .adapter_daemon import (
    AdapterDaemonRequest,
    AdapterStopRequest,
    adapter_stop_path,
    daemonize_adapter,
    print_daemon_adapter_status,
    stop_adapter_daemon,
)
from .common import make_agent
from .gateway_client import ensure_gateway_started
from .models import AdapterOptions

# 适配器常驻进程每隔多久把自己的状态写进 adapter_state.json（channel_health 按 30 秒判陈旧）。
ADAPTER_STATE_WRITE_INTERVAL_SECONDS = 5.0
# 停止请求独立短轮询，不把原五秒状态心跳提高到每秒多次写盘。
ADAPTER_STOP_POLL_INTERVAL_SECONDS = 0.2
# 状态写入失败的进程内记录：只记内存，不做 IO；下一次写成功时一并写进状态文件，让恢复过程可见。
_ADAPTER_STATE_WRITE_HEALTH: dict[str, object] = {"failures": 0, "last_error": {}}
_adapter_logger = logging.getLogger("agent_py_agent.adapter")


@dataclass(frozen=True)
class FileAdapterLoopContext:
    agent: object
    options: AdapterOptions
    gpaths: object
    apaths: AdapterPaths
    timeout: float


def cmd_adapter(args) -> int:
    print("please specify adapter subcommand: file", file=sys.stderr)
    return 2


def cmd_adapter_file(args) -> int:
    agent = make_agent(args)
    options = _adapter_options_from_args(args)
    gpaths = gateway_paths(agent)
    apaths = _resolve_adapter_paths(agent, options)
    gateway_code = ensure_gateway_started(args) if not options.no_start_gateway else _ensure_gateway_available(options, gpaths)
    if gateway_code:
        return gateway_code

    timeout = options.timeout if options.timeout is not None else agent.config.gateway_request_timeout
    total = _process_file_adapter_loop(FileAdapterLoopContext(agent, options, gpaths, apaths, timeout))
    _print_file_adapter_summary(total, apaths, gpaths)
    return 0


def _adapter_options_from_args(args) -> AdapterOptions:
    def value(name: str, default=None):
        return getattr(args, "__dict__", {}).get(name, default)

    root = value("root")
    inbox = value("inbox")
    outbox = value("outbox")
    pid_file = value("pid_file")
    timeout = value("timeout")
    return AdapterOptions(
        root=Path(root).expanduser() if root else None,
        inbox=Path(inbox).expanduser() if inbox else None,
        outbox=Path(outbox).expanduser() if outbox else None,
        timeout=timeout,
        limit=value("limit", 20),
        once=value("once", False),
        watch=value("watch", False),
        poll_interval=value("poll_interval", 1.0),
        no_start_gateway=value("no_start_gateway", False),
        channel=value("channel", "all"),
        pid_file=Path(pid_file).expanduser() if pid_file else None,
        daemon=value("daemon", False),
        stop_timeout=10.0 if timeout is None else timeout,
    )


def _resolve_adapter_paths(agent, options: AdapterOptions) -> AdapterPaths:
    apaths = adapter_paths(agent)
    if options.root:
        root = options.root
        apaths = AdapterPaths(
            root=root,
            inbox=root / "inbox",
            processing=root / "processing",
            done=root / "done",
            failed=root / "failed",
            outbox=root / "outbox",
        )
    if options.inbox:
        apaths.inbox = options.inbox
    if options.outbox:
        apaths.outbox = options.outbox
    return apaths


def _ensure_gateway_available(options: AdapterOptions, gpaths) -> int:
    _, alive = gateway_running(gpaths)
    if alive:
        return 0
    print("gateway is not running and --no-start-gateway was specified", file=sys.stderr)
    return 2


def _process_file_adapter_loop(context: FileAdapterLoopContext) -> int:
    total = 0
    while True:
        total += process_file_adapter_once(
            context.agent,
            gateway_paths_obj=context.gpaths,
            adapter_paths_obj=context.apaths,
            timeout=context.timeout,
            limit=context.options.limit,
        )
        if context.options.once or not context.options.watch:
            return total
        time.sleep(max(0.2, context.options.poll_interval))


def _print_file_adapter_summary(total: int, apaths: AdapterPaths, gpaths) -> None:
    print(
        json.dumps(
            {
                "processed": total,
                "adapter_root": str(apaths.root),
                "inbox": str(apaths.inbox),
                "outbox": str(apaths.outbox),
                "gateway_workspace": str(gpaths.root),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


def cmd_adapter_start(args) -> int:
    agent = make_agent(args)
    options = _adapter_options_from_args(args)
    gpaths = gateway_paths(agent)
    gpaths.root.mkdir(parents=True, exist_ok=True)
    pid_file = options.pid_file if options.pid_file else gpaths.adapter_pid
    if options.daemon:
        return daemonize_adapter(AdapterDaemonRequest(agent, gpaths, pid_file, options))
    return _run_adapter_foreground(agent, options, gpaths)


def _register_channel_adapter(manager: ChannelManager, channel: str, agent) -> None:
    workspace_root = _adapter_workspace_root(agent)
    if channel == "feishu":
        adapter = FeishuAdapter(
            config=_feishu_adapter_config(agent),
            callback_port=agent.config.feishu_callback_port or 8421,
            workspace_root=workspace_root,
        )
    elif channel == "qq":
        adapter = QQAdapter(
            config=_qq_adapter_config(agent),
            workspace_root=workspace_root,
        )
    else:
        return
    adapter.on_message(lambda msg: manager.route_message(msg))
    manager.register_adapter(adapter)


def _adapter_workspace_root(agent) -> Path:
    return Path(getattr(agent, "root", Path.cwd())).resolve()


# LLM: 宿主配置同源接入原 FeishuAdapter；引用读取只加既有 durable prepare 能力，不创建独立队列或配置副本。
# 函数用途: 解析宿主凭据、回调端口和入站开关；卡片与普通消息同源，秘密只传运输、不写记录。
def _feishu_adapter_config(agent) -> dict[str, object]:
    # 凭据字段过 SecretRef 解析:值可写成 env:NAME / file:/path(密钥放源码树外,不内联进 YAML)。
    # 闲置锁 fallback 直接复用运行时常量，避免 CLI adapter 另写一份小时数后再次漂移。
    from ..agent.session_lock import DEFAULT_IDLE_SECONDS
    from ..agent.settings.secret_ref import resolve_secret_ref

    return {
        "feishu_app_id": resolve_secret_ref(agent.config.feishu_app_id or ""),
        "feishu_app_secret": resolve_secret_ref(agent.config.feishu_app_secret or ""),
        "feishu_verification_token": resolve_secret_ref(agent.config.feishu_verification_token or ""),
        "feishu_encrypt_key": resolve_secret_ref(getattr(agent.config, "feishu_encrypt_key", "")),
        "feishu_connection_mode": (
            getattr(agent.config, "feishu_connection_mode", "long_connection") or "long_connection"
        ),
        "feishu_ws_proxy": getattr(agent.config, "feishu_ws_proxy", ""),
        # WS/webhook 共用群聊触发配置，只认机器人直接 mention，@全员不派发。
        "feishu_group_require_bot_mention": getattr(agent.config, "feishu_group_require_bot_mention", True),
        "feishu_quoted_media_enabled": getattr(agent.config, "feishu_quoted_media_enabled", True),
        "feishu_quoted_text_enabled": getattr(agent.config, "feishu_quoted_text_enabled", True),
        # 个人私聊会话锁默认开启；显式 false 才关闭，群聊不锁。
        "feishu_session_lock_enabled": getattr(agent.config, "feishu_session_lock_enabled", True),
        "feishu_personal_idle_lock_seconds": getattr(
            agent.config,
            "feishu_personal_idle_lock_seconds",
            DEFAULT_IDLE_SECONDS,
        ),
        # my_agent_home 根:卡片按钮回调据此读待确认记录 + 定位 owner 的 SOUL.md(与网关侧
        # update_persona 写入用的 home_paths.root 同一根,跨进程一致)。
        "my_agent_home": str(getattr(getattr(agent, "home_paths", None), "root", "") or ""),
        # 控制卡回调与普通消息使用同一宿主端口和配置凭据，不能回落到默认8420。
        "gateway_port": getattr(agent.config, "gateway_port", 8420),
        "gateway_auth_token": getattr(agent.config, "gateway_auth_token", ""),
    }


def _qq_adapter_config(agent) -> dict[str, str]:
    return {
        "qq_app_id": agent.config.qq_app_id or "",
        "qq_app_secret": agent.config.qq_app_secret or "",
    }


def _ensure_service_logging() -> None:
    """适配器是常驻服务进程,需要一个 stderr handler 让 INFO 级生命周期日志(适配器启动、连接建立、
    重连、心跳)可见。否则 agent_py_agent logger 无 handler,落到 WARNING-only 的 last-resort,
    通道的 INFO 全静默丢失——QQ 当初"不在线"时日志里一行没有,排查无从下手。
    已有 handler(测试/被嵌入调用)则不重复配置,避免重复打印。"""
    from ..agent.common.log_redaction import RedactingFormatter, install_log_redaction

    install_log_redaction()
    root = logging.getLogger()
    if root.handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    logging.getLogger("agent_py_agent").setLevel(logging.INFO)


# LLM: 先核对 Gateway 客户端凭据并清理遗留停止标记，再起通道/PID/状态；配置 token 优先；开关开且凭据失败时不启动无凭据客户端。
# 函数用途: 在原适配器生命周期入口绑定宿主凭据并运行通道服务，不把秘密传给子进程或状态。
def _run_adapter_foreground(agent, options: AdapterOptions, gpaths) -> int:
    _ensure_service_logging()
    manager = ChannelManager(
        gateway_port=agent.config.gateway_port,
        delivery_state_dir=Path(gpaths.root) / "channel-delivery",
        stream_preview_max_chars=agent.config.im_stream_preview_max_chars,
    )
    from ..agent.adapter.manager import configure_gateway_client

    configure_gateway_client(manager, agent)
    _register_requested_adapters(manager, options.channel, agent)
    globals()["_adapter_manager"] = manager

    adapter_stop_path(gpaths.root).unlink(missing_ok=True)
    write_pid_record(gpaths.adapter_pid)
    _write_adapter_state_guarded(gpaths, "starting", {"requested_channel": options.channel})
    print(f"starting channel adapter: {options.channel}", file=sys.stderr)
    # LLM: 2026-09-28 生产事故：磁盘写满时周期状态写入抛 OSError，没人接，适配器进程整个退出，状态文件却还写着
    #   running，飞书从此不通。现在任何异常逃出启动/等待阶段都先收尾（停适配器、删 pid 文件、状态写成 failed）再上抛，
    #   进程退出与否由异常类型决定，但结构化痕迹一定留下。
    try:
        manager.start_all()
        _write_adapter_state_guarded(
            gpaths,
            "running",
            {"requested_channel": options.channel, "channels": manager.runtime_channel_statuses()},
        )
        print(f"started adapters: {manager.list_adapters()}", file=sys.stderr)
        _wait_for_adapter_shutdown(manager, gpaths, requested_channel=options.channel)
    except BaseException as exc:
        _finish_adapter_process(manager, gpaths, "failed", {"reason": "unhandled_error", "error": _error_record(exc)})
        raise
    _finish_adapter_process(manager, gpaths, "stopped", {"reason": "user request"})
    print("adapter stopped", file=sys.stderr)
    return 0


# LLM: 退出收尾顺序固定：先停适配器，再删自己的 pid 文件（不占磁盘空间，磁盘写满也能成），最后把状态写成非 running；
#   状态写不进去时 pid 文件已经没了，channel_health/ /status 按"进程不在"如实判定，另外再记一条 ERROR 日志留痕。
# 函数用途: 适配器进程退出（正常停止或异常）时的统一收尾。
def _finish_adapter_process(manager: ChannelManager, gpaths, state: str, extra: dict) -> None:
    try:
        manager.stop_all()
    except Exception as exc:  # noqa: BLE001 - 收尾阶段任何一步失败都不能挡住后面的痕迹
        _adapter_logger.error("adapter stop_all failed during %s: %s: %s", state, type(exc).__name__, exc)
    remove_pid_file_if_owned(gpaths.adapter_pid)
    if not _write_adapter_state_guarded(gpaths, state, extra):
        _adapter_logger.error("adapter_state_write_failed_at_exit state=%s reason=%s", state, extra.get("reason", ""))


# LLM: 记录会进 adapter_state.json 并被 /status、channel_health 读出，消息先过 redact_sensitive_text 再截断；不存异常对象。
# 函数用途: 把异常压成可写进状态文件的小记录（类型、脱敏并截断的消息）。
def _error_record(exc: BaseException) -> dict[str, str]:
    return {"type": type(exc).__name__, "message": redact_sensitive_text(str(exc))[:500]}


def _register_requested_adapters(manager: ChannelManager, channel: str, agent) -> None:
    if channel in ("feishu", "all"):
        _register_channel_adapter(manager, "feishu", agent)
    if channel in ("qq", "all"):
        _register_channel_adapter(manager, "qq", agent)


# LLM: 停止文件存在且未带异代PID时才视为当前进程的停止请求；若文件声明了旧代PID则立即清理并忽略，防止跨代误杀。
# 函数用途: 判断停止请求文件是否面向当前适配器进程。
def _stop_request_matches_current_process(stop_path: Path) -> bool:
    if not stop_path.exists():
        return False
    try:
        raw = json.loads(stop_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return stop_path.exists()
    if isinstance(raw, dict) and raw.get("pid") is not None:
        try:
            target_pid = int(raw["pid"])
        except (ValueError, TypeError):
            target_pid = 0
        if target_pid > 0 and target_pid != os.getpid():
            try:
                stop_path.unlink(missing_ok=True)
            except OSError:
                pass
            return False
    return True


# LLM: 停止文件沿daemon写者同一gpaths定位并核对目标PID，短Event等待并finally清理；本循环异常优先，单独清理失败仍上抛；原心跳守卫和requested_channel不变。
# 函数用途: 等待停止请求或信号后交调用方优雅收尾，不让短轮询增加原状态文件写入频率。
def _wait_for_adapter_shutdown(manager: ChannelManager, gpaths, stop_event: threading.Event | None = None, *, requested_channel: str | None = None) -> None:
    stop_event = stop_event if stop_event is not None else threading.Event()
    stop_path = adapter_stop_path(gpaths.root)
    next_state_write = time.monotonic() + ADAPTER_STATE_WRITE_INTERVAL_SECONDS

    # LLM: 信号仅设置原等待事件，实际manager停止及PID收尾仍归前台调用栈。
    # 函数用途: 让SIGINT和SIGTERM中断当前等待片。
    def _sig_handler(signum, frame):
        stop_event.set()

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)
    failed = False
    try:
        while not stop_event.is_set() and not _stop_request_matches_current_process(stop_path):
            timeout = min(ADAPTER_STOP_POLL_INTERVAL_SECONDS, max(0.0, next_state_write - time.monotonic()))
            if stop_event.wait(timeout) or _stop_request_matches_current_process(stop_path):
                break
            if time.monotonic() < next_state_write:
                continue
            extra = {"channels": manager.runtime_channel_statuses()}
            if requested_channel is not None:
                extra["requested_channel"] = requested_channel
            _write_adapter_state_guarded(gpaths, "running", extra)
            next_state_write = time.monotonic() + ADAPTER_STATE_WRITE_INTERVAL_SECONDS
    except KeyboardInterrupt:
        pass
    except BaseException:
        failed = True
        raise
    finally:
        try:
            stop_path.unlink(missing_ok=True)
        except OSError as exc:
            if not failed:
                raise
            _adapter_logger.warning("adapter_stop_request_cleanup_failed error=%s", type(exc).__name__)


# LLM: 状态文件是原子写（temp+replace）：写失败时旧内容保持完整，不会留下半截 JSON 让读者报"状态不可读"。
#   载荷带上进程内记录的写失败次数与最近一次错误，恢复写入后读者能看出中间断过。会抛 OSError，调用方按需守卫。
# 函数用途: 把适配器进程的生命周期状态原子写进 adapter_state.json。
def _write_adapter_state(gpaths, state: str, extra: dict | None = None) -> None:
    from ..agent.gateway_parts.daemon_control import _get_process_start_time, _utc_now_iso

    payload = {
        "kind": "my-agent-adapter",
        "pid": os.getpid(),
        "start_time": _get_process_start_time(os.getpid()),
        "state": state,
        "updated_at": _utc_now_iso(),
        "state_write_failures": int(_ADAPTER_STATE_WRITE_HEALTH["failures"]),
        "last_state_write_error": dict(_ADAPTER_STATE_WRITE_HEALTH["last_error"]),
    }
    if extra:
        payload.update(extra)
    write_json_file_atomic(gpaths.root / "adapter_state.json", payload)


# LLM: 唯一允许在常驻循环里调用的状态写入口：OSError（磁盘满、只读、权限）只记内存账并打一条 WARNING，返回 False；
#   其它异常照常上抛（那是程序错误，不能静默）。
# 函数用途: 守卫版状态写入，失败不杀进程，下一轮再试。
def _write_adapter_state_guarded(gpaths, state: str, extra: dict | None = None) -> bool:
    try:
        _write_adapter_state(gpaths, state, extra)
    except OSError as exc:
        _ADAPTER_STATE_WRITE_HEALTH["failures"] = int(_ADAPTER_STATE_WRITE_HEALTH["failures"]) + 1
        _ADAPTER_STATE_WRITE_HEALTH["last_error"] = {**_error_record(exc), "state": state}
        _adapter_logger.warning(
            "adapter_state_write_failed state=%s failures=%s error=%s: %s",
            state, _ADAPTER_STATE_WRITE_HEALTH["failures"], type(exc).__name__, exc,
        )
        return False
    return True


def cmd_adapter_status(args) -> int:
    agent = make_agent(args)
    options = _adapter_options_from_args(args)
    gpaths = gateway_paths(agent)
    pid_file = options.pid_file if options.pid_file else gpaths.adapter_pid
    pid = get_running_pid(pid_file)
    if pid is not None:
        print_daemon_adapter_status(pid, pid_file, gpaths)
        return 0
    return _print_foreground_adapter_status()


def _print_foreground_adapter_status() -> int:
    manager: ChannelManager | None = globals().get("_adapter_manager")
    if manager is None:
        print("no running adapter manager", file=sys.stderr)
        return 1
    for name in manager.list_adapters():
        adapter = manager.get_adapter(name)
        status = "running" if adapter and adapter.running else "stopped"
        print(f"  {name}: {status}")
    return 0


def cmd_adapter_stop(args) -> int:
    agent = make_agent(args)
    options = _adapter_options_from_args(args)
    gpaths = gateway_paths(agent)
    pid_file = options.pid_file if options.pid_file else gpaths.adapter_pid
    pid = get_running_pid(pid_file)
    if pid is not None:
        return stop_adapter_daemon(AdapterStopRequest(options, gpaths, pid_file, pid))
    return _stop_foreground_adapter_manager()


def _stop_foreground_adapter_manager() -> int:
    manager: ChannelManager | None = globals().get("_adapter_manager")
    if manager is None:
        print("no running adapter manager", file=sys.stderr)
        return 1
    manager.stop_all()
    print("all adapters stopped", file=sys.stderr)
    return 0
