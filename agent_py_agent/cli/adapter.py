from __future__ import annotations

import threading

"""LLM: implements file-adapter CLI commands that bridge external inbox/outbox JSON with gateway ask.

给人看的解释：
外部工具可以把消息 JSON 放进 inbox，这个命令会把它转成 gateway 请求，再把回复写到 outbox。
这里是聊天工具/TUI 和 my-agent gateway 的文件协议适配层。
"""

import json
import sys
import time
from pathlib import Path

from ..agent.gateway import AdapterPaths, adapter_paths, gateway_paths, gateway_running, process_file_adapter_once
from .common import make_agent
from .gateway_client import ensure_gateway_started
from ..agent.adapter import ChannelManager, FeishuAdapter, QQAdapter


def cmd_adapter(args) -> int:
    """adapter 命令族入口。"""

    print("请指定 adapter 子命令：file。", file=sys.stderr)
    return 2



def cmd_adapter_file(args) -> int:
    """文件协议 adapter：inbox JSON -> gateway -> outbox JSON。"""

    agent = make_agent(args)
    gpaths = gateway_paths(agent)
    apaths = adapter_paths(agent)
    if args.root:
        root = Path(args.root).expanduser()
        apaths = AdapterPaths(
            root=root,
            inbox=root / "inbox",
            processing=root / "processing",
            done=root / "done",
            failed=root / "failed",
            outbox=root / "outbox",
        )
    if args.inbox:
        apaths.inbox = Path(args.inbox).expanduser()
    if args.outbox:
        apaths.outbox = Path(args.outbox).expanduser()

    if not args.no_start_gateway:
        code = ensure_gateway_started(args)
        if code:
            return code
    else:
        _, alive = gateway_running(gpaths)
        if not alive:
            print("gateway 未在运行，且指定了 --no-start-gateway。", file=sys.stderr)
            return 2

    timeout = args.timeout if args.timeout is not None else agent.config.gateway_request_timeout
    total = 0
    while True:
        processed = process_file_adapter_once(
            agent,
            gateway_paths_obj=gpaths,
            adapter_paths_obj=apaths,
            timeout=timeout,
            limit=args.limit,
        )
        total += processed
        if args.once or not args.watch:
            break
        time.sleep(max(0.2, args.poll_interval))
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
    return 0


# ---------------------------------------------------------------------------
# 通道适配器子命令
# ---------------------------------------------------------------------------


def cmd_adapter_start(args) -> int:
    """启动指定通道的适配器（feishu / qq / all）。"""
    import sys
    from pathlib import Path

    agent = make_agent(args)

    manager = ChannelManager(gateway_port=agent.config.gateway_port)

    # 注册所需适配器
    if args.channel in ("feishu", "all"):
        feishu_cfg = {
            "feishu_app_id": agent.config.feishu_app_id or "",
            "feishu_app_secret": agent.config.feishu_app_secret or "",
            "feishu_verification_token": agent.config.feishu_verification_token or "",
            "feishu_encrypt_key": getattr(agent.config, "feishu_encrypt_key", ""),
        }
        feishu = FeishuAdapter(
            config=feishu_cfg,
            callback_port=agent.config.feishu_callback_port or 8421,
            workspace_root=Path(agent.config.workspace_root or ".").resolve()
            if agent.config.workspace_root
            else Path.cwd(),
        )
        feishu.on_message(lambda msg: manager.route_message(msg))
        manager.register_adapter(feishu)

    if args.channel in ("qq", "all"):
        qq_cfg = {
            "qq_app_id": agent.config.qq_app_id or "",
            "qq_app_secret": agent.config.qq_app_secret or "",
        }
        qq = QQAdapter(
            config=qq_cfg,
            workspace_root=Path(agent.config.workspace_root or ".").resolve()
            if agent.config.workspace_root
            else Path.cwd(),
        )
        qq.on_message(lambda msg: manager.route_message(msg))
        manager.register_adapter(qq)

    # 把 manager 存到全局（后续 stop/status 需要用到）
    _global_manager = manager
    globals()["_adapter_manager"] = _global_manager

    print(f"启动通道适配器: {args.channel}", file=sys.stderr)
    manager.start_all()
    print(f"已启动: {manager.list_adapters()}", file=sys.stderr)

    # 前台保持运行，Ctrl+C 退出
    try:
        import signal
        stop_event = threading.Event()

        def _sig_handler(signum, frame):
            stop_event.set()

        signal.signal(signal.SIGINT, _sig_handler)
        signal.signal(signal.SIGTERM, _sig_handler)
        stop_event.wait()
    except KeyboardInterrupt:
        pass

    manager.stop_all()
    print("适配器已停止。", file=sys.stderr)
    return 0


def cmd_adapter_status(args) -> int:
    """查看已注册适配器的运行状态。"""
    import sys

    manager: ChannelManager | None = globals().get("_adapter_manager")
    if manager is None:
        print("没有正在运行的适配器管理器。", file=sys.stderr)
        return 1
    for name in manager.list_adapters():
        adapter = manager.get_adapter(name)
        status = "running" if adapter and adapter.running else "stopped"
        print(f"  {name}: {status}")
    return 0


def cmd_adapter_stop(args) -> int:
    """停止所有已启动的通道适配器。"""
    import sys

    manager: ChannelManager | None = globals().get("_adapter_manager")
    if manager is None:
        print("没有正在运行的适配器管理器。", file=sys.stderr)
        return 1
    manager.stop_all()
    print("所有适配器已停止。", file=sys.stderr)
    return 0
