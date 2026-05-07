# LLM: CLI surface module; keep argparse/Typer wiring, stdout text, and service-call boundaries stable.
# 模块用途: 提供命令行入口或辅助函数，把用户命令转换成 agent 服务调用。

from __future__ import annotations

"""CLI entrypoints for file and channel adapters.

Small helper functions keep daemon, foreground, and file-loop flows below soft limits.
"""

import json
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..agent.adapter import ChannelManager, FeishuAdapter, QQAdapter
from ..agent.gateway import (
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


# LLM: FileAdapterLoopContext 是gateway CLI的数据契约；字段名会被调用方和测试读取。
# 类用途: 集中携带运行期上下文和共享引用，供相邻阶段稳定读取。
@dataclass(frozen=True)
class FileAdapterLoopContext:
    agent: object
    options: AdapterOptions
    gpaths: object
    apaths: AdapterPaths
    timeout: float


# LLM: cmd_adapter 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
def cmd_adapter(args) -> int:
    print("please specify adapter subcommand: file", file=sys.stderr)
    return 2


# LLM: cmd_adapter_file 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
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


# LLM: _adapter_options_from_args 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _adapter_options_from_args(args) -> AdapterOptions:
    # LLM: value 属于gateway CLI；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
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


# LLM: _resolve_adapter_paths 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 解析路径、模式或配置默认值，返回后续流程使用的稳定值。
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


# LLM: _ensure_gateway_available 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 协调 gateway 请求、进程状态、worker 或本地文件之间的流转。
def _ensure_gateway_available(options: AdapterOptions, gpaths) -> int:
    _, alive = gateway_running(gpaths)
    if alive:
        return 0
    print("gateway is not running and --no-start-gateway was specified", file=sys.stderr)
    return 2


# LLM: _process_file_adapter_loop 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
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


# LLM: _print_file_adapter_summary 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
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


# LLM: cmd_adapter_start 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
def cmd_adapter_start(args) -> int:
    agent = make_agent(args)
    options = _adapter_options_from_args(args)
    gpaths = gateway_paths(agent)
    gpaths.root.mkdir(parents=True, exist_ok=True)
    pid_file = options.pid_file if options.pid_file else gpaths.adapter_pid
    if options.daemon:
        return daemonize_adapter(AdapterDaemonRequest(agent, gpaths, pid_file, options))
    return _run_adapter_foreground(agent, options, gpaths)


# LLM: _register_channel_adapter 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
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


# LLM: _adapter_workspace_root 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _adapter_workspace_root(agent) -> Path:
    return Path(getattr(agent, "root", Path.cwd())).resolve()


# LLM: _feishu_adapter_config 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _feishu_adapter_config(agent) -> dict[str, str]:
    return {
        "feishu_app_id": agent.config.feishu_app_id or "",
        "feishu_app_secret": agent.config.feishu_app_secret or "",
        "feishu_verification_token": agent.config.feishu_verification_token or "",
        "feishu_encrypt_key": getattr(agent.config, "feishu_encrypt_key", ""),
    }


# LLM: _qq_adapter_config 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _qq_adapter_config(agent) -> dict[str, str]:
    return {
        "qq_app_id": agent.config.qq_app_id or "",
        "qq_app_secret": agent.config.qq_app_secret or "",
    }


# LLM: _run_adapter_foreground 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 执行对应流程阶段，并把成功、失败和产物写入汇总状态。
def _run_adapter_foreground(agent, options: AdapterOptions, gpaths) -> int:
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


# LLM: _register_requested_adapters 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _register_requested_adapters(manager: ChannelManager, channel: str, agent) -> None:
    if channel in ("feishu", "all"):
        _register_channel_adapter(manager, "feishu", agent)
    if channel in ("qq", "all"):
        _register_channel_adapter(manager, "qq", agent)


# LLM: _wait_for_adapter_shutdown 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _wait_for_adapter_shutdown() -> None:
    stop_event = threading.Event()

    # LLM: _sig_handler 属于gateway CLI；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    def _sig_handler(signum, frame):
        stop_event.set()

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)
    try:
        stop_event.wait()
    except KeyboardInterrupt:
        pass


# LLM: _write_adapter_state 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 把报告、摘要或状态写入磁盘，保持输出路径和 JSON 字段稳定。
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


# LLM: cmd_adapter_status 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
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


# LLM: _print_foreground_adapter_status 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
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


# LLM: cmd_adapter_stop 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
def cmd_adapter_stop(args) -> int:
    agent = make_agent(args)
    options = _adapter_options_from_args(args)
    gpaths = gateway_paths(agent)
    pid_file = options.pid_file if options.pid_file else gpaths.adapter_pid
    pid = get_running_pid(pid_file)
    if pid is not None:
        return stop_adapter_daemon(AdapterStopRequest(options, gpaths, pid_file, pid))
    return _stop_foreground_adapter_manager()


# LLM: _stop_foreground_adapter_manager 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _stop_foreground_adapter_manager() -> int:
    manager: ChannelManager | None = globals().get("_adapter_manager")
    if manager is None:
        print("no running adapter manager", file=sys.stderr)
        return 1
    manager.stop_all()
    print("all adapters stopped", file=sys.stderr)
    return 0
