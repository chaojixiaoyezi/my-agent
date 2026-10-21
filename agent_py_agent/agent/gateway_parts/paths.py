# LLM: Gateway与supervisor各有独立规范PID/状态定位，属性只派生路径不授角色或创建文件；同步CLI/监护读取。
# 模块用途: 集中定义Gateway、监护者及adapter的文件位置，禁止共享角色状态或另猜home。

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


# LLM: 新角色位置用只读属性派生，保原构造字段兼容；任何路径不等于执行身份。
# 类用途: 保存同一Gateway根的规范文件地址，监护状态独立于业务状态。
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

    # LLM: 只定位原独立supervisor.pid，不得作为Gateway本体或ready来源。
    # 函数用途: 返回监护者自己的进程记录位置。
    @property
    def supervisor_pid(self) -> Path:
        return self.root / "supervisor.pid"

    # LLM: 监护生命周期不得覆盖gateway_state.json，CLI仅只读该投影。
    # 函数用途: 返回监护者自己的状态位置。
    @property
    def supervisor_state(self) -> Path:
        return self.root / "supervisor_state.json"

    # LLM: 维护锁仅从同一Gateway根派生，不创建文件或更改进程权限。
    # 函数用途: 返回维护期间暂停自动恢复的结构化锁地址。
    @property
    def maintenance_lock(self) -> Path:
        return self.root / "maintenance_lock.json"

    # LLM: 服务安装回执独立于Gateway和supervisor的运行状态，不代表已启动。
    # 函数用途: 返回OS看门狗服务安装记录地址。
    @property
    def supervisor_service(self) -> Path:
        return self.root / "supervisor_service.json"

    # LLM: Terminal request authority is derived from the Gateway root so existing constructors
    # remain compatible; done/failed directories are only outcome projections.
    # 函数用途: 返回按请求 ID 唯一保存完整最终答复的终态目录。
    @property
    def terminal(self) -> Path:
        return self.root / "requests" / "terminal"


# LLM: 纯路径容器不创建文件；适配器队列不与Gateway请求目录混用。
# 类用途: 保存适配器文件协议的各目录位置。
@dataclass
class AdapterPaths:

    root: Path
    inbox: Path
    processing: Path
    done: Path
    failed: Path
    outbox: Path


# LLM: 路径来自已解析Agent及其配置，不另选home或初始化业务。
# 函数用途: 取得当前Agent的Gateway规范路径。
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


# LLM: 仅拼接processing投影地址，不验证或授予请求权限。
# 函数用途: 返回正在处理请求的进度文件位置。
def gateway_chunk_path(paths: GatewayPaths, request_id: str) -> Path:
    return paths.processing / f"{request_id}.chunks.jsonl"


# LLM: 仅枚举同队列归档候选；存在性和终态仍由调用方核验。
# 函数用途: 请求移动后同时查找原进度和完成/失败目录。
def gateway_chunk_path_candidates(chunk_path: Path) -> tuple[Path, ...]:
    if chunk_path.parent.name != "processing":
        return (chunk_path,)
    request_root = chunk_path.parent.parent
    return (
        chunk_path,
        request_root / "done" / chunk_path.name,
        request_root / "failed" / chunk_path.name,
    )


# LLM: 根目录只沿同Agent配置派生；不创建目录或切换执行身份。
# 函数用途: 取得适配器收发文件目录合同。
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
