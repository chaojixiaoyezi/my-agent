
from __future__ import annotations

import shlex
from collections.abc import Iterable

from .model_capabilities import capability_request_suppresses_duplicate


def find_equivalent_capability_request(requests: Iterable[object], candidate: object):
    target = capability_request_signature(candidate)
    for request in requests or []:
        if not capability_request_suppresses_duplicate(getattr(request, "status", "")):
            continue
        if capability_request_signature(request) == target:
            return request
    return None


def capability_request_signature(value: object) -> tuple:
    return (
        _text_attr(value, "capability_type", default="generic").lower(),
        _text_attr(value, "needed_capability").lower(),
        _normalized_list_attr(value, "requested_tools"),
        _normalized_list_attr(value, "requested_skills"),
        _normalized_list_attr(value, "requested_mcp_tools"),
        _normalized_command_attr(value, "requested_commands"),
        _normalized_list_attr(value, "cwd_scope"),
        _normalized_list_attr(value, "path_scope"),
        _normalized_list_attr(value, "network_scope"),
        tuple(sorted(_object_dict_attr(value, "output_budget").items())),
        _text_attr(value, "risk_level").lower(),
    )


def _normalized_command_attr(value: object, name: str) -> tuple[str, ...]:
    commands = [_command_name(item).lower() for item in _list_attr(value, name)]
    return tuple(sorted(item for item in dict.fromkeys(commands) if item))


def _command_name(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parts = shlex.split(text)
    except ValueError:
        parts = text.split()
    return str(parts[0]).strip() if parts else ""


def _normalized_list_attr(value: object, name: str) -> tuple[str, ...]:
    items = [str(item).strip() for item in _list_attr(value, name)]
    return tuple(sorted(item for item in dict.fromkeys(items) if item))


def _list_attr(value: object, name: str) -> list:
    raw = getattr(value, name, None)
    if raw is None:
        return []
    return list(raw) if isinstance(raw, (list, tuple, set)) else [raw]


def _text_attr(value: object, name: str, *, default: str = "") -> str:
    raw = getattr(value, name, default)
    if raw is None:
        return default
    return str(raw or default).strip()


def _object_dict_attr(value: object, name: str) -> dict[str, object]:
    raw = getattr(value, name, None)
    if not isinstance(raw, dict):
        return {}
    return {str(key): item for key, item in raw.items()}
