
from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

from ..common.value_parsing import text_value as _text
from .contract_validation_recovery import recovery_for_findings

SECRET_FIELD_NAMES = {"api_key", "authorization", "cookie", "password", "secret", "token"}
REDACTED_VALUES = {"[redacted]", "<redacted>", "***", "redacted"}
PRIVATE_HOSTS = {"localhost"}


@dataclass(frozen=True)
class OfflineSecurityBoundaryValidation:
    ok: bool
    error_codes: tuple[str, ...]
    findings: tuple[dict[str, object], ...]
    recovery: dict[str, object] | None = None


def validate_security_boundary_events(
    events: tuple[dict[str, Any], ...],
    *,
    allowed_private_hosts: tuple[str, ...] = (),
) -> OfflineSecurityBoundaryValidation:
    findings: list[dict[str, object]] = []
    _validate_path_events(events, findings)
    _validate_network_events(events, set(allowed_private_hosts), findings)
    _validate_external_text_events(events, findings)
    _validate_secret_redaction(events, findings)
    _validate_side_effect_idempotency(events, findings)
    return OfflineSecurityBoundaryValidation(
        ok=not findings,
        error_codes=tuple(dict.fromkeys(_text(item.get("code")) for item in findings)),
        findings=tuple(findings),
        recovery=recovery_for_findings("offline_security_boundary", findings),
    )


def _validate_network_events(
    events: tuple[dict[str, Any], ...],
    allowed_private_hosts: set[str],
    findings: list[dict[str, object]],
) -> None:
    for index, event in enumerate(events):
        if _event_type(event) != "network_request":
            continue
        if _scheme(event.get("url")) == "file":
            findings.append(_finding("NETWORK_FILE_URL_BLOCKED", index, event))
            continue
        host = _host(event.get("url"))
        if host and host not in allowed_private_hosts and _is_private_host(host):
            findings.append(_finding("NETWORK_PRIVATE_HOST_BLOCKED", index, event, {"host": host}))


def _validate_path_events(
    events: tuple[dict[str, Any], ...],
    findings: list[dict[str, object]],
) -> None:
    for index, event in enumerate(events):
        if _event_type(event) != "path_access":
            continue
        root = _posix_path(event.get("workspace_root"))
        resolved = _posix_path(event.get("resolved_path"))
        if root and resolved and not _is_relative_to(resolved, root):
            findings.append(_finding("PATH_SYMLINK_ESCAPE_BLOCKED", index, event, {"resolved_path": resolved}))


def _validate_external_text_events(
    events: tuple[dict[str, Any], ...],
    findings: list[dict[str, object]],
) -> None:
    for index, event in enumerate(events):
        if _event_type(event) != "external_text":
            continue
        if event.get("untrusted_instruction_detected") is True and event.get("applied_to_machine_contract") is True:
            findings.append(_finding("PROMPT_INJECTION_IGNORED_AS_DATA", index, event))


def _validate_secret_redaction(
    events: tuple[dict[str, Any], ...],
    findings: list[dict[str, object]],
) -> None:
    for index, event in enumerate(events):
        if _event_type(event) != "llm_context":
            continue
        for field_path in _secret_field_paths(event.get("payload"), prefix="payload"):
            findings.append(_finding("SECRET_REDACTION_REQUIRED", index, event, {"field_path": field_path}))


def _validate_side_effect_idempotency(
    events: tuple[dict[str, Any], ...],
    findings: list[dict[str, object]],
) -> None:
    seen: dict[str, str] = {}
    for index, event in enumerate(events):
        if event.get("side_effect") is not True:
            continue
        key = _text(event.get("idempotency_key"))
        if not key:
            findings.append(_finding("IDEMPOTENCY_KEY_REQUIRED", index, event))
            continue
        args_hash = _text(event.get("args_hash"))
        previous = seen.get(key)
        if previous is not None and previous != args_hash:
            findings.append(_finding("SIDE_EFFECT_REPLAY_BLOCKED", index, event, {"idempotency_key": key}))
        seen.setdefault(key, args_hash)


def _secret_field_paths(value: object, *, prefix: str) -> tuple[str, ...]:
    paths: list[str] = []
    stack: list[tuple[str, object]] = [(prefix, value)]
    while stack:
        current_prefix, current = stack.pop()
        if isinstance(current, dict):
            paths.extend(_dict_secret_paths(current_prefix, current, stack))
            continue
        if isinstance(current, (list, tuple)):
            stack.extend((f"{current_prefix}[{index}]", child) for index, child in enumerate(current))
    return tuple(paths)


def _dict_secret_paths(
    prefix: str,
    value: dict[object, object],
    stack: list[tuple[str, object]],
) -> list[str]:
    paths: list[str] = []
    for key, child in value.items():
        key_text = _text(key)
        path = f"{prefix}.{key_text}" if prefix else key_text
        if key_text.lower() in SECRET_FIELD_NAMES and not _value_is_redacted(child):
            paths.append(path)
        stack.append((path, child))
    return paths


def _is_private_host(host: str) -> bool:
    normalized = host.strip().lower().strip("[]")
    if normalized in PRIVATE_HOSTS:
        return True
    numeric = _numeric_ipv4_host(normalized)
    if numeric:
        normalized = numeric
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


def _host(value: object) -> str:
    parsed = urlparse(_text(value))
    return _text(parsed.hostname)


def _scheme(value: object) -> str:
    return _text(urlparse(_text(value)).scheme).lower()


def _numeric_ipv4_host(value: str) -> str:
    if not value.isdigit():
        return ""
    try:
        address = ipaddress.ip_address(int(value))
    except ValueError:
        return ""
    return str(address)


def _posix_path(value: object) -> str:
    text_value = _text(value)
    return PurePosixPath(text_value).as_posix() if text_value else ""


def _is_relative_to(path: str, root: str) -> bool:
    path_parts = PurePosixPath(path).parts
    root_parts = PurePosixPath(root).parts
    return path_parts[: len(root_parts)] == root_parts


def _value_is_redacted(value: object) -> bool:
    if value in (None, ""):
        return True
    if not isinstance(value, str):
        return False
    return value.strip().lower() in REDACTED_VALUES


def _finding(
    code: str,
    index: int,
    event: dict[str, Any],
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    return {"code": code, "index": index, "event_type": _event_type(event), **(extra or {})}


def _event_type(event: dict[str, Any]) -> str:
    return _text(event.get("type")).lower()

__all__ = ["OfflineSecurityBoundaryValidation", "validate_security_boundary_events"]
