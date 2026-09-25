# LLM: Gateway 停机收尾的只读投影：列出 owner 后台会话权威目录里仍未终态的受管进程。受管后台进程跨 Gateway 存活是设计行为
#   （只有显式 /stop 或 background_process stop 按身份回收），这里不停止、不改记录、不猜归属，每条只带
#   session_id/status/started_at/uptime_seconds/command(≤200)/lan_reachability/listener_observation。目录与 ProcessSessionTool
#   同源：registry.workspace_root + registry.owner_scope_root → process_session_store_root。列表会顺带按 PID 出生身份刷新已死
#   实例（registry 既有行为）。gateway_parts 不得 import cli/agent_core/subagents，调用方是 cli/gateway_process 的停机收尾。
# 模块用途: 给 Gateway 停机事件和 my-agent status 提供"停机后还有哪些后台进程在跑"的结构化事实。
from __future__ import annotations

from typing import Any

from ..tooling.process_network_status import managed_process_network_status
from ..tooling.process_registry import process_registry
from ..tooling.process_session_records import PROCESS_TERMINAL_STATUSES
from ..tooling.process_session_store import process_session_store_root

_SUMMARY_FIELDS = ("session_id", "status", "started_at", "uptime_seconds", "command")


# LLM: 没有工具注册表或工作区根（测试替身、未装工具的宿主）返回空列表；权威目录不存在也返回空，不创建目录。
#   监听事实来自 process_network_status（Linux /proc；其它平台 listener_observation=unsupported_on_host），不做外部探针。
# 函数用途: 列出 Gateway 停机后仍会继续运行的后台进程会话。
def surviving_background_sessions(agent: object) -> list[dict[str, Any]]:
    registry = getattr(agent, "tools", None)
    workspace_root = getattr(registry, "workspace_root", None)
    if registry is None or not workspace_root:
        return []
    store_root = process_session_store_root(workspace_root, getattr(registry, "owner_scope_root", "") or "")
    if not store_root.is_dir():
        return []
    summaries, _load_errors = process_registry.list_report(None, store_root)
    rows: list[dict[str, Any]] = []
    for summary in summaries:
        if str(summary.get("status") or "") in PROCESS_TERMINAL_STATUSES:
            continue
        row = {key: summary.get(key) for key in _SUMMARY_FIELDS}
        row.update(_listener_facts(str(summary.get("session_id") or ""), store_root))
        rows.append(row)
    return rows


# 函数用途: 取一条会话的监听范围事实；记录读不到时保持 unknown，不伪造"未监听"。
def _listener_facts(session_id: str, store_root: object) -> dict[str, Any]:
    record = process_registry.get(session_id, None, store_root) if session_id else None
    if record is None:
        return {"lan_reachability": "unknown", "listener_observation": "record_unavailable"}
    network = managed_process_network_status(record)
    return {
        "lan_reachability": str(network.get("lan_reachability") or "unknown"),
        "listener_observation": str(network.get("listener_observation") or "unknown"),
    }


__all__ = ["surviving_background_sessions"]
