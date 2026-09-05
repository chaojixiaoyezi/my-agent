from __future__ import annotations

"""Structured listener and host-firewall observations for managed processes."""

# LLM: This module observes only host-owned process/socket/firewall facts for one already
# authorized managed session. Failed observations remain unknown, never approval or stopped state.
# It never changes firewall rules or declares remote reachability; keep process_session tests in sync.
# 模块用途: 查询受管后台服务监听与防火墙事实；查询失败保留未知，不能据此宣布局域网可达。

import ipaddress
import os
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any

NETWORK_STATUS_SCHEMA = "managed_process_network_status.v1"
_LISTEN_STATE = "0A"
_FIREWALL_TIMEOUT_SECONDS = 1.5
_FIREWALL_NOT_RUNNING = 252  # firewall-cmd 的公开退出码，不从错误文案猜运行状态。


# LLM: Remote reachability always needs an independent observer. Local listener and firewall
# observations are additive evidence only and cannot be promoted into a successful LAN claim.
# 函数用途: 为一条已授权后台会话生成结构化网络状态，不做任何配置修改。
def managed_process_network_status(
    record: object,
    *,
    requested_port: int = 0,
) -> dict[str, Any]:
    port = max(0, min(65_535, int(requested_port or 0)))
    process_status = str(getattr(record, "status", "") or "unknown")
    bindings, observation = _managed_listener_bindings(record, requested_port=port)
    non_loopback = [row for row in bindings if row["scope"] == "non_loopback"]
    if process_status != "running":
        reachability = "process_not_running"
    elif not bindings:
        reachability = "not_listening"
    elif not non_loopback:
        reachability = "loopback_only"
    else:
        reachability = "unverified_external_probe_required"
    return {
        "schema": NETWORK_STATUS_SCHEMA,
        "session_id": str(getattr(record, "session_id", "") or ""),
        "process_status": process_status,
        "requested_port": port,
        "listener_observation": observation,
        "listener_bindings": bindings,
        "listener_pid_evidence": {
            "source": "host_kernel_process_tree",
            "authority": "observed",
            "sandbox_visibility": "may_be_hidden",
        },
        "host_firewall": _host_firewall_observation(non_loopback),
        "lan_reachability": reachability,
        "external_probe_required": reachability == "unverified_external_probe_required",
        "evidence_boundary": (
            "listener_pids 来自宿主机内核对受管进程树的观测；run_command 沙箱可能看不到这些 PID，"
            "沙箱内 ps/lsof 无结果不能推翻该监听事实。"
            "监听 0.0.0.0、本机端口存在或本机请求成功都不等于局域网可达；"
            "只有另一台目标机器的真实连接结果才能把 lan_reachability 改成已验证。"
        ),
    }


# LLM: Linux /proc ownership is joined by socket inode across the exact managed process tree.
# Unsupported platforms return an explicit observation state instead of guessing from command text.
# 函数用途: 找出当前受管进程及其后代真正持有的 TCP 监听地址。
def _managed_listener_bindings(
    record: object,
    *,
    requested_port: int,
) -> tuple[list[dict[str, object]], str]:
    if os.name != "posix" or not Path("/proc/net/tcp").exists():
        return [], "unsupported_on_host"
    pids = _process_tree_pids(
        int(getattr(record, "pid", 0) or 0),
        int(getattr(record, "child_pid", 0) or 0),
    )
    inode_owners = _socket_inode_owners(pids)
    if not inode_owners:
        return [], "observed_no_owned_listener"
    rows: list[dict[str, object]] = []
    for path, family in ((Path("/proc/net/tcp"), socket.AF_INET), (Path("/proc/net/tcp6"), socket.AF_INET6)):
        rows.extend(
            _proc_tcp_listeners(
                path,
                family=family,
                owned_inodes=inode_owners,
                requested_port=requested_port,
            )
        )
    rows.sort(key=lambda row: (int(row["port"]), str(row["host"])))
    return rows, "observed" if rows else "observed_no_matching_listener"


# LLM: Descendant discovery is bounded to live /proc identities and never follows a PID outside
# the exact child relation. Missing/racing process files simply reduce observability.
# 函数用途: 收集后台 host、沙箱启动器与真实服务进程的 PID 集合。
def _process_tree_pids(host_pid: int, child_pid: int) -> set[int]:
    pending = [pid for pid in (host_pid, child_pid) if pid > 0]
    seen: set[int] = set()
    while pending and len(seen) < 4096:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        children_path = Path(f"/proc/{pid}/task/{pid}/children")
        try:
            raw = children_path.read_text(encoding="utf-8")
        except OSError:
            continue
        for value in raw.split():
            try:
                child = int(value)
            except ValueError:
                continue
            if child > 0 and child not in seen:
                pending.append(child)
    return seen


# LLM: Socket inode links are kernel facts. Preserve their owning PIDs so a
# listener can name the actual resource holder instead of a launcher ancestor.
# Permission/race failures are ignored per fd and never become guessed ownership.
# 函数用途: 从受管进程树的 fd 链接收集 socket inode 及其真实持有进程号。
def _socket_inode_owners(pids: set[int]) -> dict[str, set[int]]:
    owners: dict[str, set[int]] = {}
    for pid in pids:
        fd_root = Path(f"/proc/{pid}/fd")
        try:
            entries = tuple(fd_root.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                target = os.readlink(entry)
            except OSError:
                continue
            if target.startswith("socket:[") and target.endswith("]"):
                owners.setdefault(target[8:-1], set()).add(pid)
    return owners


# LLM: /proc parsing accepts only LISTEN rows whose inode belongs to the managed tree. Malformed
# kernel rows are skipped and no port/address is inferred from the launch command.
# 函数用途: 解析一个 TCP 表并返回属于受管进程的监听地址。
def _proc_tcp_listeners(
    path: Path,
    *,
    family: socket.AddressFamily,
    owned_inodes: dict[str, set[int]],
    requested_port: int,
) -> list[dict[str, object]]:
    try:
        lines = path.read_text(encoding="ascii").splitlines()[1:]
    except OSError:
        return []
    bindings: list[dict[str, object]] = []
    for line in lines:
        fields = line.split()
        if len(fields) < 10 or fields[3] != _LISTEN_STATE or fields[9] not in owned_inodes:
            continue
        try:
            address_hex, port_hex = fields[1].split(":", 1)
            port = int(port_hex, 16)
            host = _proc_address(address_hex, family)
        except (OSError, ValueError):
            continue
        if requested_port and port != requested_port:
            continue
        bindings.append(
            {
                "host": host,
                "port": port,
                "scope": "loopback" if ipaddress.ip_address(host).is_loopback else "non_loopback",
                "listener_pids": sorted(owned_inodes[fields[9]]),
            }
        )
    return bindings


# LLM: Linux exposes IPv4 words little-endian and IPv6 as four little-endian 32-bit words.
# Conversion is deterministic and rejects unknown address widths.
# 函数用途: 把 /proc 十六进制地址转换成人能读懂的 IP。
def _proc_address(value: str, family: socket.AddressFamily) -> str:
    raw = bytes.fromhex(value)
    if family == socket.AF_INET:
        if len(raw) != 4:
            raise ValueError("invalid IPv4 proc address")
        return socket.inet_ntop(family, raw[::-1])
    if len(raw) != 16:
        raise ValueError("invalid IPv6 proc address")
    reordered = b"".join(raw[index : index + 4][::-1] for index in range(0, 16, 4))
    return socket.inet_ntop(family, reordered)


# LLM: Fixed read-only firewalld calls preserve exit-code evidence per zone/port. Only NOT_RUNNING
# proves daemon absence; partial/failed observations cannot imply all ports allowed. No explicit
# rule proves neither blocking nor external reachability. Sync process_session contracts and tests.
# 函数用途: 逐区域、端口查询显式放行；超时或拒绝显示未知，不能把一个端口的结果推广到全部端口。
def _host_firewall_observation(
    bindings: list[dict[str, object]],
) -> dict[str, object]:
    ports = sorted({int(row["port"]) for row in bindings})
    binary = shutil.which("firewall-cmd")
    if not ports:
        return {"status": "not_applicable", "ports": []}
    if not binary:
        return {"status": "not_detected", "ports": ports}
    state = _firewall_command(binary, ("--state",))
    if state is not None and state.returncode == _FIREWALL_NOT_RUNNING:
        return {"status": "not_running", "ports": ports}
    if state is None or state.returncode != 0:
        return {"status": "unknown", "ports": ports, "reason": "state_query_failed"}
    active = _firewall_command(binary, ("--get-active-zones",))
    if active is None or active.returncode != 0:
        return {"status": "unknown", "ports": ports, "reason": "zone_query_failed"}
    zones = [line.strip() for line in active.stdout.splitlines() if line and not line[:1].isspace()]
    if not zones:
        return {"status": "unknown", "ports": ports, "reason": "no_active_zones"}
    allowed: list[dict[str, object]] = []
    observations: list[dict[str, object]] = []
    for zone in zones:
        for port in ports:
            answer = _firewall_command(
                binary,
                (f"--zone={zone}", f"--query-port={port}/tcp"),
            )
            rule = {"zone": zone, "port": port, "protocol": "tcp"}
            code = answer.returncode if answer is not None else None
            status = "unknown"
            if code == 0:
                status = "explicitly_allowed"
                allowed.append(rule)
            elif code == 1:
                status = "not_explicitly_allowed"
            observations.append({**rule, "status": status, "query_return_code": code})
    if any(row["status"] == "unknown" for row in observations):
        status = "unknown"
    elif len(allowed) == len(observations):
        status = "explicitly_allowed"
    else:
        status = "partially_allowed" if allowed else "not_explicitly_allowed"
    return {
        "status": status,
        "ports": ports,
        "active_zones": zones,
        "explicit_rules": allowed,
        "port_observations": observations,
        "evidence_boundary": (
            "只查询活动 firewalld 区域的显式端口规则；未覆盖 service/rich rules、其他 nftables 规则或上游设备。"
            "未显式放行不等于已阻断，部分放行不等于全部放行，查询失败不等于防火墙已关闭。"
        ),
    }


# LLM: This helper runs only fixed read-only firewalld arguments assembled from integer ports
# and discovered zone names. Preserve native exit codes (query false=1, NOT_RUNNING=252);
# timeout/OS failure is None, not an empty successful observation. Never mutate rules.
# 函数用途: 有界执行只读 firewall-cmd 查询，保留退出码；无法执行时返回未知而不是伪造停用状态。
def _firewall_command(
    binary: str, arguments: tuple[str, ...],
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            [binary, *arguments],
            capture_output=True,
            text=True,
            timeout=_FIREWALL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


__all__ = ["NETWORK_STATUS_SCHEMA", "managed_process_network_status"]
