
from __future__ import annotations

from typing import Any

from .models import AgentCapability, CollaborationRequest
from .store_common import strings

_GENERIC_GENERATED_NAME_BASES = {
    "agent",
    "worker",
    "runner",
    "subagent",
    "child",
    "responder",
    "source",
}


def agent_identity_aliases(values: object = ()) -> set[str]:
    """Preserve exact IDs while tolerating manager-appended numeric suffixes."""
    aliases: set[str] = set()
    for value in _identity_values(values):
        text = str(value or "").strip()
        if not text:
            continue
        _add_identity_alias(aliases, text)
        _add_generated_base_alias(aliases, text)
    return aliases


def _identity_values(values: object) -> tuple[object, ...]:
    if isinstance(values, (list, tuple, set)):
        return tuple(values)
    return (values,)


def capability_identity_aliases(capability: AgentCapability) -> set[str]:
    """Bridge registered run IDs and model-visible names without role matching."""
    metadata = capability.metadata if isinstance(capability.metadata, dict) else {}
    return agent_identity_aliases((capability.agent_id, metadata.get("agent_name"), metadata.get("run_id")))


def request_update_targets(*, explicit_targets: object, metadata: dict[str, Any]) -> tuple[str, ...]:
    """Read reroute targets from explicit params or metadata."""
    targets = strings(explicit_targets)
    if targets:
        return targets
    for key in ("target_agent_ids", "rerouted_to", "reroute_target_agent_ids"):
        targets = strings(_listish(metadata.get(key)))
        if targets:
            return targets
    return ()


def candidate_target_agent_ids(request: CollaborationRequest) -> list[str]:
    """Expose only structured alternate routing candidates."""
    candidates: list[str] = []
    for key in ("candidate_target_agent_ids", "alternate_sources_available", "alternate_target_agent_ids"):
        _append_candidates(candidates, request, key)
    return candidates


def _append_candidates(candidates: list[str], request: CollaborationRequest, key: str) -> None:
    for item in strings(_listish(request.metadata.get(key))):
        if item not in candidates and item not in request.target_agent_ids:
            candidates.append(item)


def _listish(value: object) -> object:
    return value if isinstance(value, (list, tuple)) else [value]


def _add_generated_base_alias(aliases: set[str], text: str) -> None:
    if "-" not in text:
        return
    base, suffix = text.rsplit("-", 1)
    if base and suffix.isdigit() and _specific_generated_name_base(base):
        _add_identity_alias(aliases, base)


def _add_identity_alias(aliases: set[str], value: str) -> None:
    aliases.add(value)
    aliases.add(value.lower())


def _specific_generated_name_base(base: str) -> bool:
    text = str(base or "").strip()
    return bool(text and text.lower() not in _GENERIC_GENERATED_NAME_BASES)
