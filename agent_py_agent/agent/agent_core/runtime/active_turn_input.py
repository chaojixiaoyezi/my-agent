from __future__ import annotations

"""Structured current-turn user input carried across compact continuations.

``/btw`` is a real user turn delivered while one durable task is already running.
The model-visible text lives in the provider-neutral ``UserTurn`` IR.  This small
packet is the continuation carrier: it keeps the same user turn available when
compact creates a fresh tool loop without parsing prompt text or replaying an
already-delivered guidance ledger entry.
"""

from collections.abc import Iterable
from typing import Any

_SCHEMA_VERSION = "active-turn-user-input.v1"


def packet_from_guidance(entries: Iterable[Any], text: str) -> dict[str, object] | None:
    content = str(text or "")
    input_ids = [
        value
        for entry in entries
        if (value := str(getattr(entry, "guidance_id", "") or "").strip())
    ]
    if not content.strip() or not input_ids:
        return None
    return {
        "schema_version": _SCHEMA_VERSION,
        "input_ids": input_ids,
        "text": content,
    }


def append_active_turn_user_input(params: object, packet: dict[str, object] | None) -> bool:
    if packet is None:
        return False
    target = getattr(params, "active_turn_user_inputs", None)
    if not isinstance(target, list):
        return False
    merged = merge_active_turn_user_inputs(target, [packet])
    if len(merged) == len(target):
        return False
    target[:] = merged
    return True


def merge_active_turn_user_inputs(
    existing: Iterable[dict[str, object]] | None,
    incoming: Iterable[dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    merged: list[dict[str, object]] = []
    seen: set[tuple[tuple[str, ...], str]] = set()
    for collection in (existing, incoming):
        for raw in collection or []:
            packet = _normalized_packet(raw)
            if packet is None:
                continue
            key = (tuple(packet["input_ids"]), str(packet["text"]))
            if key in seen:
                continue
            seen.add(key)
            merged.append(packet)
    return merged


def active_turn_user_input_texts(
    packets: Iterable[dict[str, object]] | None,
) -> list[str]:
    return [
        str(packet["text"])
        for raw in packets or []
        if (packet := _normalized_packet(raw)) is not None
    ]


def _normalized_packet(raw: object) -> dict[str, object] | None:
    if not isinstance(raw, dict):
        return None
    if str(raw.get("schema_version") or "") != _SCHEMA_VERSION:
        return None
    text = str(raw.get("text") or "")
    raw_ids = raw.get("input_ids")
    if not text.strip() or not isinstance(raw_ids, list):
        return None
    input_ids: list[str] = []
    seen_ids: set[str] = set()
    for raw_id in raw_ids:
        input_id = str(raw_id or "").strip()
        if input_id and input_id not in seen_ids:
            input_ids.append(input_id)
            seen_ids.add(input_id)
    if not input_ids:
        return None
    return {
        "schema_version": _SCHEMA_VERSION,
        "input_ids": input_ids,
        "text": text,
    }


__all__ = [
    "active_turn_user_input_texts",
    "append_active_turn_user_input",
    "merge_active_turn_user_inputs",
    "packet_from_guidance",
]
