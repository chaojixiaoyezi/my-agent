# LLM: 受管后台进程监听范围的唯一判定：loopback 只允许 127.0.0.0/8、::1 的监听，lan 不限制。观测只看宿主机内核/lsof 的真实
#   socket 表（Linux 走 process_network_status 的 /proc 解析，其它 POSIX 用 lsof，都按 child 进程树 PID 归属），不解析命令正文。
#   观测不到（unsupported/lsof 缺失）返回空表并给出 observation 码，调用方不能据此宣布"未监听"。host 用它做启动后持续核对。
# 模块用途: 给后台进程 host 提供"这棵进程树现在监听在哪些地址"的事实，以及是否越出了声明的监听范围。
from __future__ import annotations

import ipaddress
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

LISTEN_SCOPES = ("loopback", "lan")
DEFAULT_LISTEN_SCOPE = "loopback"
LISTEN_SCOPE_VIOLATION = "listen_scope_violation"
_LSOF_TIMEOUT_SECONDS = 2.0


# 函数用途: 规范 listen_scope 取值；空按默认 loopback，未知值抛 ValueError。
def normalize_listen_scope(value: object) -> str:
    text = str(value or DEFAULT_LISTEN_SCOPE).strip().lower()
    if text not in LISTEN_SCOPES:
        raise ValueError("managed background listen scope invalid")
    return text


# LLM: 返回 (bindings, observation)。bindings 每行 {host, port, scope(loopback|non_loopback), listener_pids}；observation 取
#   observed / observed_no_listener / unsupported_on_host / lsof_unavailable / lsof_failed。Linux 复用 /proc 解析（同一集合），
#   其它 POSIX 用 lsof -iTCP -sTCP:LISTEN 按 child 进程树过滤；Windows 返回 unsupported_on_host。
# 函数用途: 观测 child 进程树当前的 TCP 监听地址。
def observe_tree_listeners(child_pid: int) -> tuple[list[dict[str, Any]], str]:
    if os.name != "posix":
        return [], "unsupported_on_host"
    if Path("/proc/net/tcp").exists():
        return _observe_via_proc(child_pid)
    return _observe_via_lsof(child_pid)


# 函数用途: 挑出越出 loopback 范围的监听行；scope 为 lan 时永远为空。
def scope_violations(listen_scope: str, bindings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if normalize_listen_scope(listen_scope) == "lan":
        return []
    return [row for row in bindings if row.get("scope") == "non_loopback"]


def _observe_via_proc(child_pid: int) -> tuple[list[dict[str, Any]], str]:
    import socket

    from .process_network_status import (
        _proc_tcp_listeners,
        _process_tree_pids,
        _socket_inode_owners,
    )

    owners = _socket_inode_owners(_process_tree_pids(child_pid, 0))
    if not owners:
        return [], "observed_no_listener"
    rows: list[dict[str, Any]] = []
    for path, family in ((Path("/proc/net/tcp"), socket.AF_INET), (Path("/proc/net/tcp6"), socket.AF_INET6)):
        rows.extend(_proc_tcp_listeners(path, family=family, owned_inodes=owners, requested_port=0))
    return rows, "observed" if rows else "observed_no_listener"


# LLM: lsof 只按 -p 精确 PID 集合过滤，输出用 -F 字段格式解析（p=pid, n=地址）；`*` 或非回环地址记 non_loopback。
# 函数用途: 在没有 /proc 的 POSIX（macOS）上观测监听。
def _observe_via_lsof(child_pid: int) -> tuple[list[dict[str, Any]], str]:
    lsof = shutil.which("lsof")
    if not lsof:
        return [], "lsof_unavailable"
    pids = _descendant_pids_posix(child_pid)
    if not pids:
        return [], "observed_no_listener"
    try:
        completed = subprocess.run(
            [lsof, "-nP", "-a", "-iTCP", "-sTCP:LISTEN", "-p", ",".join(str(pid) for pid in sorted(pids)), "-Fpn"],
            capture_output=True, text=True, timeout=_LSOF_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return [], "lsof_failed"
    rows: list[dict[str, Any]] = []
    current_pid = 0
    for line in completed.stdout.splitlines():
        if line.startswith("p"):
            current_pid = int(line[1:] or 0)
        elif line.startswith("n") and current_pid:
            parsed = _parse_lsof_address(line[1:])
            if parsed is not None:
                rows.append({**parsed, "listener_pids": [current_pid]})
    return rows, "observed" if rows else "observed_no_listener"


# 函数用途: 把 lsof 的 `host:port` / `[v6]:port` / `*:port` 转成 host/port/scope 行。
def _parse_lsof_address(text: str) -> dict[str, Any] | None:
    host, sep, port_text = text.rpartition(":")
    if not sep or not port_text.isdigit():
        return None
    host = host.strip("[]")
    if host in {"*", ""}:
        return {"host": "*", "port": int(port_text), "scope": "non_loopback"}
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    return {"host": host, "port": int(port_text), "scope": "loopback" if loopback else "non_loopback"}


# 函数用途: 用 ps 列出 child 及其后代 PID（macOS 没有 /proc children 文件）。
def _descendant_pids_posix(root_pid: int) -> set[int]:
    if root_pid <= 0:
        return set()
    try:
        completed = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True, timeout=_LSOF_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError):
        return {root_pid}
    children: dict[int, list[int]] = {}
    alive: set[int] = set()
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            continue
        pid, ppid = int(parts[0]), int(parts[1])
        alive.add(pid)
        children.setdefault(ppid, []).append(pid)
    if root_pid not in alive:
        return set()
    pending, seen = [root_pid], set()
    while pending and len(seen) < 4096:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        pending.extend(children.get(pid, []))
    return seen


__all__ = [
    "DEFAULT_LISTEN_SCOPE", "LISTEN_SCOPES", "LISTEN_SCOPE_VIOLATION",
    "normalize_listen_scope", "observe_tree_listeners", "scope_violations",
]
