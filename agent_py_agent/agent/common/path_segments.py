"""Path segment normalization helpers shared by owner and memory modules."""

from __future__ import annotations


def safe_path_segment(value: object, *, default: str = "unknown", replacement: str = "-") -> str:
    """Turn an external id into one literal directory name.

    The helper is intentionally open-world: it does not validate business ids,
    it only prevents slash/dot shaped values from becoming path structure.
    """

    text = str(value or "").strip()
    result = "".join(char if char.isalnum() or char in {"_", "-", "."} else replacement for char in text)
    return result.strip(".-_/") or default
