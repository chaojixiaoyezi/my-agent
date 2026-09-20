
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

    # LLM: Terminal request authority is derived from the Gateway root so existing constructors
    # remain compatible; done/failed directories are only outcome projections.
    # 函数用途: 返回按请求 ID 唯一保存完整最终答复的终态目录。
    @property
    def terminal(self) -> Path:
        return self.root / "requests" / "terminal"


@dataclass
class AdapterPaths:

    root: Path
    inbox: Path
    processing: Path
    done: Path
    failed: Path
    outbox: Path


def gateway_paths(agent: SimpleAgent) -> GatewayPaths:
    return gateway_paths_from_root(agent.root / agent.config.gateway_workspace)


# LLM: Owner-scoped agents may execute a request claimed from the base Gateway. Callers holding an
# authoritative queue path must rebuild paths from that queue root instead of the agent home.
# 函数用途: 根据已经确认的 Gateway 根目录构造完整路径合同。
def gateway_paths_from_root(root: Path) -> GatewayPaths:
    root = Path(root)
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


# LLM: 路径跟随已认领的权威队列记录，不能按执行 Agent 的 owner root 重新派生；同步恢复/轮询测试。
# 函数用途: 为正常执行和恢复取得同一 chunk 文件地址，使多用户客户端能读到真实过程。
def claimed_request_chunk_path(request_path: Path, request_id: str) -> Path:
    """Keep live chunks beside the authoritative claimed queue record.

    The request worker claims records in the base Gateway queue even when the
    actual run uses an owner-scoped agent.  HTTP progress polling and final
    archival both follow that claimed record, so deriving this path from the
    owner agent root would make per-user progress invisible to the adapter.
    """
    return request_path.with_name(f"{request_id}.chunks.jsonl")


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
