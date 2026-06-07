
from __future__ import annotations

"""resolves typed gateway and adapter filesystem path contracts from config.

这个文件只负责"gateway 和 adapter 的文件都放在哪"。
以后命令行、后台进程、测试都从这里拿路径，不需要到处手写目录名。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core import SimpleAgent


@dataclass
class GatewayPaths:

    root: Path
    pid: Path
    adapter_pid: Path
    state: Path
    heartbeat: Path
    stop_request: Path
    log: Path
    inbox: Path
    processing: Path
    done: Path
    failed: Path
    responses: Path
    history: Path


@dataclass
class AdapterPaths:

    root: Path
    inbox: Path
    processing: Path
    done: Path
    failed: Path
    outbox: Path


def gateway_paths(agent: SimpleAgent) -> GatewayPaths:

    root = agent.root / agent.config.gateway_workspace
    return GatewayPaths(
        root=root,
        pid=root / "gateway.pid",
        adapter_pid=root / "adapter.pid",
        state=root / "gateway_state.json",
        heartbeat=root / "gateway_heartbeat.json",
        stop_request=root / "gateway_stop.request",
        log=root / "gateway.log",
        inbox=root / "requests" / "pending",
        processing=root / "requests" / "processing",
        done=root / "requests" / "done",
        failed=root / "requests" / "failed",
        responses=root / "responses",
        history=root / "gateway_requests.jsonl",
    )


def gateway_chunk_path(paths: GatewayPaths, request_id: str) -> Path:
    return paths.processing / f"{request_id}.chunks.jsonl"


def gateway_chunk_path_candidates(chunk_path: Path) -> tuple[Path, ...]:
    if chunk_path.parent.name != "processing":
        return (chunk_path,)
    request_root = chunk_path.parent.parent
    return (
        chunk_path,
        request_root / "done" / chunk_path.name,
        request_root / "failed" / chunk_path.name,
    )


def adapter_paths(agent: SimpleAgent) -> AdapterPaths:

    root = agent.root / agent.config.adapter_workspace
    return AdapterPaths(
        root=root,
        inbox=root / "inbox",
        processing=root / "processing",
        done=root / "done",
        failed=root / "failed",
        outbox=root / "outbox",
    )
