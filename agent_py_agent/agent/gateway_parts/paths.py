from __future__ import annotations

"""LLM: resolves typed gateway and adapter filesystem path contracts from config.

给人看的解释：
这个文件只负责“gateway 和 adapter 的文件都放在哪”。
以后命令行、后台进程、测试都从这里拿路径，不需要到处手写目录名。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core import SimpleAgent


@dataclass
class GatewayPaths:
    """LLM contract: all filesystem endpoints used by the gateway queue.

    Human version:
    这里集中保存 gateway 会读写的所有文件夹和文件。比如 `inbox` 是待处理请求，
    `processing` 是正在处理的请求，`responses` 是结果。CLI 不需要自己拼路径，
    只要拿到这组对象就知道 gateway 的“现场”在哪里。
    """

    root: Path
    pid: Path
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
    """LLM contract: file-adapter inbox/outbox directory set.

    Human version:
    文件适配器是给外部聊天工具/TUI 用的。外部程序把消息 JSON 放进 `inbox`，
    my-agent 处理后把回复 JSON 放到 `outbox`。中间的 `processing/done/failed`
    让人可以直接看目录判断消息走到哪一步。
    """

    root: Path
    inbox: Path
    processing: Path
    done: Path
    failed: Path
    outbox: Path


def gateway_paths(agent: SimpleAgent) -> GatewayPaths:
    """LLM contract: resolve gateway control and queue paths from agent config.

    Human version:
    根据配置算出 gateway 的工作目录。测试环境可以把它指到临时目录，真实运行时
    默认落到 `agent_py_agent/data/gateway`，这样不会把路径写死在命令逻辑里。
    """

    root = agent.root / agent.config.gateway_workspace
    return GatewayPaths(
        root=root,
        pid=root / "gateway.pid",
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
    """LLM: resolve the streaming chunk file for a processing request.

    Human version:
    流式输出时，daemon 把每个 chunk 追加到这个文件。CLI 轮询读取后显示给用户。
    请求完成后由 daemon 删除。
    """
    return paths.processing / f"{request_id}.chunks.jsonl"


def adapter_paths(agent: SimpleAgent) -> AdapterPaths:
    """LLM contract: resolve file-adapter paths from agent config.

    Human version:
    和 `gateway_paths()` 类似，只是这里服务外部消息适配器。以后如果 adapter
    从文件协议换成别的协议，CLI 入口也不需要知道太多底层细节。
    """

    root = agent.root / agent.config.adapter_workspace
    return AdapterPaths(
        root=root,
        inbox=root / "inbox",
        processing=root / "processing",
        done=root / "done",
        failed=root / "failed",
        outbox=root / "outbox",
    )
