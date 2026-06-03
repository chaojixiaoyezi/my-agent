
from __future__ import annotations

"""Small markdown section renderers shared by runner rendering."""


def render_granted_card_lines(card: dict[str, object]) -> list[str]:
    lines = [
        f"- [{card.get('kind', 'unknown')}] {card.get('name', 'unknown')} "
        f"risk={card.get('risk_level', 'unknown')} source={card.get('source', 'unknown')}"
    ]
    if card.get("description"):
        lines.append(f"  - description: {card['description']}")
    if card.get("path"):
        lines.append(f"  - path: {card['path']}")
    if card.get("reasons"):
        lines.append(f"  - reasons: {card['reasons']}")
    return lines


def render_evidence_item_lines(item: dict[str, object]) -> list[str]:
    status = "OK" if item.get("ok") else "FAIL"
    lines = [f"- [{status}] {item.get('kind', 'unknown')}: {item.get('summary', '')}"]
    if item.get("command"):
        lines.append(f"  - command: `{item['command']}`")
    if item.get("path"):
        lines.append(f"  - path: {item['path']}")
    if item.get("url"):
        lines.append(f"  - url: {item['url']}")
    return lines
