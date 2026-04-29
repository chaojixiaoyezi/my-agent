from __future__ import annotations

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
