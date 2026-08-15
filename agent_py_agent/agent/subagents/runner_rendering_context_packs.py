
from __future__ import annotations


def render_context_packs_section(context) -> list[str]:
    lines = ["", "## Context Packs", ""]
    if context.context_packs:
        for item in context.context_packs:
            name = item.get("name") or item.get("id") or item.get("kind") or "pack"
            lines.append(f"- {name}")
            lines.extend(_render_context_pack_item_lines(item))
    else:
        lines.append("- none")
    return lines


def _render_context_pack_item_lines(item: dict[str, object]) -> list[str]:
    lines = []
    rendered = {"kind", "summary", "path", "ref", "role", "contract"}
    for key in ["kind", "summary", "path", "ref", "role"]:
        if item.get(key):
            lines.append(f"  - {key}: {item[key]}")
    for key in sorted(item):
        if key in rendered:
            continue
        lines.extend(_render_context_pack_extra_lines(key, item.get(key)))
    lines.extend(_render_context_pack_contract_lines(item.get("contract")))
    return lines


def _render_context_pack_extra_lines(key: str, value: object) -> list[str]:
    if value in (None, "", [], {}):
        return []
    label = str(key or "").strip()
    if not label:
        return []
    if isinstance(value, (list, tuple)):
        return _render_context_pack_list(label, value)
    if isinstance(value, dict):
        keys = ", ".join(str(item) for item in list(value.keys())[:8])
        return [f"  - {label}: object keys={keys}"] if keys else []
    text = " ".join(str(value).split())[:300]
    return [f"  - {label}: {text}"] if text else []


def _render_context_pack_list(label: str, value: list | tuple) -> list[str]:
    limit = _context_pack_list_limit(label)
    items = [" ".join(str(item or "").split()) for item in value[:limit]]
    items = [item[:240] for item in items if item]
    if not items:
        return []
    lines = [f"  - {label}:"]
    lines.extend(f"    - {item}" for item in items)
    if len(value) > len(items):
        lines.append(f"    - ... omitted {len(value) - len(items)} items")
    return lines


def _context_pack_list_limit(label: str) -> int:
    lowered = str(label or "").strip().lower()
    if lowered in {"roster", "siblings", "sibling_roster", "peer_run_ids"}:
        return 30
    return 6


def _render_context_pack_contract_lines(value: object) -> list[str]:
    if not isinstance(value, dict):
        return []
    lines = []
    for key in ["schema", "kind"]:
        if value.get(key):
            lines.append(f"  - contract.{key}: {value[key]}")
    lines.extend(_contract_action_lines(value))
    return lines


def _contract_action_lines(value: dict[str, object]) -> list[str]:
    lines: list[str] = []
    actions = _bounded_contract_list(value.get("same_run_required_actions"), limit=6)
    if actions:
        lines.append(f"  - contract.same_run_required_actions: {', '.join(actions)}")
    targets = _bounded_contract_list(value.get("target_artifact_refs"), limit=6)
    if targets:
        lines.append("- contract.target_artifact_refs:")
        lines.extend(f"    - {item}" for item in targets)
    return lines


def _bounded_contract_list(value: object, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    items = [" ".join(str(item or "").split()) for item in value[:limit]]
    return [item for item in items if item]
