
# LLM: 预检消费宿主解析后的路径、命令和网络事实；私网设置须与真正连接时的 DNS/重定向安全门一致。
# 模块用途: 在工具执行前检查参数范围，不从用户正文产生授权，也不替代网络连接时的永久禁止项。
from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from ...common.value_parsing import text_value as _text
from ...path_access_policy import PathAccessPolicy
from ...tooling.write_boundary import WRITE_TOOL_NAMES
from .command_policy import evaluate_command_policy
from .models import GateDecision, GateFinding

_PATH_KEYS = {"path", "file_path", "target_path", "output_path", "working_dir", "cwd", "directory"}
_URL_KEYS = {"url", "endpoint", "webhook_url"}
_COMMAND_KEYS = {"command", "cmd", "argv"}
_PRIVATE_HOSTS = {"localhost"}

# LLM: 私网总开关由宿主传入，逐调用显式 False 可收紧部署设置；本事实对象不读取环境或增加授权。
# 类用途: 保存工具参数预检所需的同一次路径和网络设置。
@dataclass(frozen=True)
class PathUrlCommandFacts:
    payload: object
    workspace_root: Path
    workspace_roots: list[Path] | None = None
    path_access_mode: str = "normal"
    path_dangerous_roots: Iterable[str] = ()
    owner_scope_root: str = ""
    allowed_private_hosts: Iterable[str] = ()
    allow_private_resolution: bool = False
    network_enabled: bool = True
    local_file_url_fields: Iterable[str] = ()
    allow_shell_operators: bool = False
    allowed_commands: Iterable[str] = ()
    # 宿主解析出的数据根（写边界 canonical_owner_home_root 推出）；空时策略退回环境变量（H3 二审）。
    data_root: str = ""


@dataclass(frozen=True)
class PathFindingRequest:
    field: str
    raw_path: object
    roots: list[Path]
    path_policy: PathAccessPolicy
    tool_name: str = ""


# LLM: URL 预检只消费冻结网络事实；真正连接仍须经过 network_safety 的 DNS、重定向和 Gateway 门。
# 函数用途: 合并客观路径、URL 和命令拒绝，保证已开启私网的调用不会被旧默认误挡。
def evaluate_path_url_command_gate(facts: PathUrlCommandFacts) -> GateDecision:
    data = facts.payload if isinstance(facts.payload, Mapping) else {}
    roots = _normalized_roots(facts.workspace_root, facts.workspace_roots)
    path_policy = PathAccessPolicy.from_values(
        mode=facts.path_access_mode,
        dangerous_roots=facts.path_dangerous_roots,
        owner_scope_root=facts.owner_scope_root,
        agent_home_root=facts.data_root,
    )
    findings: list[GateFinding] = []
    _collect_path_findings(data, roots, path_policy, findings)
    _collect_url_findings(
        data,
        {_normalize_host(item) for item in facts.allowed_private_hosts},
        facts.allow_private_resolution,
        facts.network_enabled,
        {_text(item).strip() for item in facts.local_file_url_fields if _text(item).strip()},
        roots,
        path_policy,
        findings,
    )
    _collect_command_findings(data, facts.allow_shell_operators, facts.allowed_commands, findings)
    if findings:
        return GateDecision("path_url_command", "DENY", False, tuple(findings), "repair_tool_call", {})
    return GateDecision.allow("path_url_command", evidence={"checked_fields": _checked_field_names(data)})


def _collect_path_findings(
    data: Mapping[object, object],
    roots: list[Path],
    path_policy: PathAccessPolicy,
    findings: list[GateFinding],
) -> None:
    tool_name = _text(data.get("tool")).strip()
    for key, raw_path in _matching_values(data, _PATH_KEYS):
        finding = _path_finding(PathFindingRequest(key, raw_path, roots, path_policy, tool_name))
        if finding:
            findings.append(finding)


# LLM: 私网允许事实只影响 URL 私网预检，file URL 仍沿原精确文件权限；不做网络请求。
# 函数用途: 检查单个及批量 URL 参数并汇总客观拒绝。
def _collect_url_findings(
    data: Mapping[object, object],
    allowed_private_hosts: set[str],
    allow_private_resolution: bool,
    network_enabled: bool,
    local_file_url_fields: set[str],
    roots: list[Path],
    path_policy: PathAccessPolicy,
    findings: list[GateFinding],
) -> None:
    tool_name = _text(data.get("tool")).strip()
    for key, raw_url in _matching_values(data, _URL_KEYS | {"urls"}):
        parsed = urlparse(_text(raw_url))
        if parsed.hostname and parsed.scheme.lower() != "file" and not network_enabled:
            finding = GateFinding("NETWORK_ACCESS_DISABLED", evidence={"field": key})
        elif parsed.scheme.lower() == "file" and key in local_file_url_fields:
            finding = _local_file_url_finding(
                field=key,
                parsed=parsed,
                roots=roots,
                path_policy=path_policy,
                tool_name=tool_name,
            )
        else:
            finding = _url_finding(key, raw_url, allowed_private_hosts, allow_private_resolution)
        if finding:
            findings.append(finding)


def _collect_command_findings(
    data: Mapping[object, object],
    allow_shell_operators: bool,
    allowed_commands: Iterable[str],
    findings: list[GateFinding],
) -> None:
    for key, value in _matching_fields(data, _COMMAND_KEYS):
        findings.extend(_command_findings(str(key), value, allow_shell_operators, allowed_commands))


# LLM: Canonical filesystem mutation tools share the same symlink/write-boundary
# exception here; adding an editor must not accidentally route it as a read tool.
# 函数用途: 检查一个路径参数是否越界或穿过符号链接，并统一识别所有正式文件写工具。
def _path_finding(request: PathFindingRequest) -> GateFinding | None:
    text = _text(request.raw_path)
    if not text:
        return None
    target = Path(text)
    candidate = target if target.is_absolute() else request.roots[0] / target
    resolved = _resolve_path(candidate)
    if resolved is None:
        return GateFinding("PATH_RESOLUTION_FAILED", evidence={"field": request.field})
    decision = request.path_policy.check(resolved)
    if not decision.allowed:
        # An owner-scoped agent may receive a workspace outside its managed owner home
        # through the trusted runtime boundary (for example an explicit CLI project).
        # Mirror FileAccess.resolve_path here so the central pre-handler gate permits
        # only that exact structured root; cross-owner, credential and dangerous-root
        # decisions retain their dedicated hard denials.
        if (
            decision.code == "PATH_OWNER_SCOPE_BLOCKED"
            and _under_any_root(resolved, request.roots)
        ):
            workspace_policy = PathAccessPolicy.from_values(
                mode=request.path_policy.mode,
                dangerous_roots=request.path_policy.dangerous_roots,
                agent_home_root=request.path_policy.agent_home_root,
            )
            decision = workspace_policy.check(resolved)
        if not decision.allowed:
            return GateFinding(decision.code or "PATH_ACCESS_DENIED", evidence={
                "field": request.field,
                "resolved_path": str(resolved),
                "dangerous_root": decision.dangerous_root,
            })
    if (
        request.tool_name not in WRITE_TOOL_NAMES
        and not _under_any_root(resolved, request.roots)
        and _lexically_under_any_root(candidate, request.roots)
    ):
        return GateFinding("PATH_SYMLINK_ESCAPE_BLOCKED", evidence={"field": request.field, "resolved_path": str(resolved)})
    return None


# LLM: 宿主私网授权与逐主机授权均可通过这层预检，不能用它跳过 handler 的永久禁止地址和 Gateway 检查。
# 函数用途: 返回 URL 的文件协议或未授权私网问题，不连接目标。
def _url_finding(field: str, raw_url: object, allowed_private_hosts: set[str], allow_private_resolution: bool = False) -> GateFinding | None:
    parsed = urlparse(_text(raw_url))
    if parsed.scheme == "file":
        return GateFinding("NETWORK_FILE_URL_BLOCKED", evidence={"field": field})
    host = _normalize_host(parsed.hostname)
    if host and not allow_private_resolution and host not in allowed_private_hosts and _is_private_host(host):
        return GateFinding("NETWORK_PRIVATE_HOST_BLOCKED", evidence={"field": field, "host": host})
    return None


def _local_file_url_finding(
    *,
    field: str,
    parsed: object,
    roots: list[Path],
    path_policy: PathAccessPolicy,
    tool_name: str,
) -> GateFinding | None:
    hostname = _normalize_host(getattr(parsed, "hostname", ""))
    if hostname and hostname != "localhost":
        return GateFinding(
            "NETWORK_FILE_URL_BLOCKED",
            evidence={"field": field, "host": hostname, "reason": "non_local_file_host"},
        )
    if getattr(parsed, "query", "") or getattr(parsed, "fragment", ""):
        return GateFinding(
            "NETWORK_FILE_URL_BLOCKED",
            evidence={"field": field, "reason": "file_url_query_or_fragment"},
        )
    raw_path = unquote(str(getattr(parsed, "path", "") or ""))
    if not raw_path:
        return GateFinding(
            "PATH_RESOLUTION_FAILED",
            evidence={"field": field, "reason": "empty_file_url_path"},
        )
    return _path_finding(PathFindingRequest(field, raw_path, roots, path_policy, tool_name))


def _command_findings(field: str, value: object, allow_shell_operators: bool, allowed_commands: Iterable[str] = ()) -> list[GateFinding]:
    decision = evaluate_command_policy(value, allow_shell_operators=allow_shell_operators, allowed_commands=allowed_commands)
    return [
        GateFinding(finding.code, evidence={"field": field, **finding.evidence})
        for finding in decision.findings
        if finding.code != "COMMAND_EMPTY"
    ]


def _normalized_roots(workspace_root: Path, workspace_roots: list[Path] | None) -> list[Path]:
    raw = [workspace_root, *(workspace_roots or [])]
    roots: list[Path] = []
    for root in raw:
        resolved = Path(root).resolve(strict=False)
        if resolved not in roots:
            roots.append(resolved)
    return roots or [Path.cwd().resolve(strict=False)]


def _matching_fields(data: Mapping[object, object], names: set[str]) -> list[tuple[object, object]]:
    return [(key, value) for key, value in data.items() if str(key) in names]


def _matching_values(data: Mapping[object, object], names: set[str]) -> list[tuple[str, object]]:
    return [(str(key), item) for key, value in _matching_fields(data, names) for item in _iter_values(value)]


def _resolve_path(path: Path) -> Path | None:
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _under_any_root(path: Path, roots: list[Path]) -> bool:
    candidate = path.resolve(strict=False)
    for root in roots:
        try:
            candidate.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _lexically_under_any_root(path: Path, roots: list[Path]) -> bool:
    for root in roots:
        try:
            path.absolute().relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _is_private_host(host: str) -> bool:
    normalized = _normalize_host(host)
    if normalized in _PRIVATE_HOSTS:
        return True
    numeric = _numeric_ipv4_host(normalized)
    if numeric:
        normalized = numeric
    shorthand = _shorthand_ipv4_host(normalized)
    if shorthand:
        normalized = shorthand
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local or address.is_unspecified


def _numeric_ipv4_host(host: str) -> str:
    if not host.isdigit():
        return ""
    try:
        value = int(host)
    except ValueError:
        return ""
    if not 0 <= value <= 0xFFFFFFFF:
        return ""
    return ".".join(str((value >> shift) & 0xFF) for shift in (24, 16, 8, 0))


def _shorthand_ipv4_host(host: str) -> str:
    parts = host.split(".")
    if not 1 < len(parts) < 4 or not all(part.isdigit() for part in parts):
        return ""
    nums = [int(part) for part in parts]
    if any(num < 0 or num > 255 for num in nums):
        return ""
    if len(nums) == 2:
        nums = [nums[0], 0, 0, nums[1]]
    elif len(nums) == 3:
        nums = [nums[0], nums[1], 0, nums[2]]
    return ".".join(str(num) for num in nums)


def _checked_field_names(data: Mapping[object, object]) -> list[str]:
    keys = _PATH_KEYS | _URL_KEYS | _COMMAND_KEYS | {"urls"}
    return sorted(str(key) for key in data.keys() if str(key) in keys)


def _iter_values(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else [value]

def _normalize_host(value: object) -> str:
    return _text(value).lower().strip("[]")


__all__ = ["PathUrlCommandFacts", "evaluate_path_url_command_gate"]
