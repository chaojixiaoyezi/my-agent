
from __future__ import annotations

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
from .adapter_daemon import (
    AdapterDaemonRequest,
    AdapterStopRequest,
    daemonize_adapter,
    print_daemon_adapter_status,
    stop_adapter_daemon,
)
from .common import make_agent
from .gateway_client import ensure_gateway_started
from .models import AdapterOptions


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


def _feishu_adapter_config(agent) -> dict[str, str]:
    # 凭据字段过 SecretRef 解析:值可写成 env:NAME / file:/path(密钥放源码树外,不内联进 YAML)。
    from ..agent.settings.secret_ref import resolve_secret_ref

    return {
        "feishu_app_id": resolve_secret_ref(agent.config.feishu_app_id or ""),
        "feishu_app_secret": resolve_secret_ref(agent.config.feishu_app_secret or ""),
        "feishu_verification_token": resolve_secret_ref(agent.config.feishu_verification_token or ""),
        "feishu_encrypt_key": resolve_secret_ref(getattr(agent.config, "feishu_encrypt_key", "")),
        "feishu_connection_mode": getattr(agent.config, "feishu_connection_mode", "webhook") or "webhook",
        "feishu_ws_proxy": getattr(agent.config, "feishu_ws_proxy", ""),
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
    root = logging.getLogger()
    if root.handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    logging.getLogger("agent_py_agent").setLevel(logging.INFO)


def _run_adapter_foreground(agent, options: AdapterOptions, gpaths) -> int:
    _ensure_service_logging()
    manager = ChannelManager(gateway_port=agent.config.gateway_port)
    _register_requested_adapters(manager, options.channel, agent)
    globals()["_adapter_manager"] = manager

    write_pid_record(gpaths.adapter_pid)
    _write_adapter_state(gpaths, "running", {"channel": options.channel})
    print(f"starting channel adapter: {options.channel}", file=sys.stderr)
    manager.start_all()
    print(f"started adapters: {manager.list_adapters()}", file=sys.stderr)

    _wait_for_adapter_shutdown()
    manager.stop_all()
    remove_pid_file_if_owned(gpaths.adapter_pid)
    _write_adapter_state(gpaths, "stopped", {"reason": "user request"})
    print("adapter stopped", file=sys.stderr)
    return 0


def _register_requested_adapters(manager: ChannelManager, channel: str, agent) -> None:
    if channel in ("feishu", "all"):
        _register_channel_adapter(manager, "feishu", agent)
    if channel in ("qq", "all"):
        _register_channel_adapter(manager, "qq", agent)


def _wait_for_adapter_shutdown() -> None:
    stop_event = threading.Event()

    def _sig_handler(signum, frame):
        stop_event.set()

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)
    try:
        stop_event.wait()
    except KeyboardInterrupt:
        pass


def _write_adapter_state(gpaths, state: str, extra: dict | None = None) -> None:
    from ..agent.gateway_parts.daemon_control import _get_process_start_time, _utc_now_iso

    payload = {
        "kind": "my-agent-adapter",
        "pid": os.getpid(),
        "start_time": _get_process_start_time(os.getpid()),
        "state": state,
        "updated_at": _utc_now_iso(),
    }
    if extra:
        payload.update(extra)
    state_path = gpaths.root / "adapter_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


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
