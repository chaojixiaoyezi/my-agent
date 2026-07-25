
from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from ...common.value_parsing import text_value as _text
from ...path_access_policy import PathAccessPolicy
from .command_policy import evaluate_command_policy
from .models import GateDecision, GateFinding

_PATH_KEYS = {"path", "file_path", "target_path", "output_path", "working_dir", "cwd", "directory"}
_URL_KEYS = {"url", "endpoint", "webhook_url"}
_COMMAND_KEYS = {"command", "cmd", "argv"}
_PRIVATE_HOSTS = {"localhost"}

@dataclass(frozen=True)
class PathUrlCommandFacts:
    payload: object
    workspace_root: Path
    workspace_roots: list[Path] | None = None
    path_access_mode: str = "normal"
    path_dangerous_roots: Iterable[str] = ()
    owner_scope_root: str = ""
    allowed_private_hosts: Iterable[str] = ()
    allow_shell_operators: bool = False
    allowed_commands: Iterable[str] = ()


@dataclass(frozen=True)
class PathFindingRequest:
    field: str
    raw_path: object
    roots: list[Path]
    path_policy: PathAccessPolicy
    tool_name: str = ""


def evaluate_path_url_command_gate(facts: PathUrlCommandFacts) -> GateDecision:
    data = facts.payload if isinstance(facts.payload, Mapping) else {}
    roots = _normalized_roots(facts.workspace_root, facts.workspace_roots)
    path_policy = PathAccessPolicy.from_values(
        mode=facts.path_access_mode,
        dangerous_roots=facts.path_dangerous_roots,
        owner_scope_root=facts.owner_scope_root,
    )
    findings: list[GateFinding] = []
    _collect_path_findings(data, roots, path_policy, findings)
    _collect_url_findings(data, {_normalize_host(item) for item in facts.allowed_private_hosts}, findings)
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


def _collect_url_findings(
    data: Mapping[object, object],
    allowed_private_hosts: set[str],
    findings: list[GateFinding],
) -> None:
    for key, raw_url in _matching_values(data, _URL_KEYS | {"urls"}):
        finding = _url_finding(key, raw_url, allowed_private_hosts)
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
            )
            decision = workspace_policy.check(resolved)
        if not decision.allowed:
            return GateFinding(decision.code or "PATH_ACCESS_DENIED", evidence={
                "field": request.field,
                "resolved_path": str(resolved),
                "dangerous_root": decision.dangerous_root,
            })
    if (
        request.tool_name not in {"write_file", "apply_patch"}
        and not _under_any_root(resolved, request.roots)
        and _lexically_under_any_root(candidate, request.roots)
    ):
        return GateFinding("PATH_SYMLINK_ESCAPE_BLOCKED", evidence={"field": request.field, "resolved_path": str(resolved)})
    return None


def _url_finding(field: str, raw_url: object, allowed_private_hosts: set[str]) -> GateFinding | None:
    parsed = urlparse(_text(raw_url))
    if parsed.scheme == "file":
        return GateFinding("NETWORK_FILE_URL_BLOCKED", evidence={"field": field})
    host = _normalize_host(parsed.hostname)
    if host and host not in allowed_private_hosts and _is_private_host(host):
        return GateFinding("NETWORK_PRIVATE_HOST_BLOCKED", evidence={"field": field, "host": host})
    return None


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
