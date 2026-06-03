
from __future__ import annotations

_ROOT_ROLE_MARKERS = ("coordinator", "lead", "root")


def is_self_authorized_root_task(task: object) -> bool:
    parent_id = str(getattr(task, "parent_id", "") or "").strip()
    if parent_id:
        return False
    attrs = getattr(task, "attributes", {}) or {}
    if isinstance(attrs, dict) and bool(attrs.get("self_authorized_root")):
        return True
    role = str(getattr(task, "role", "") or "").strip().lower()
    return any(marker in role for marker in _ROOT_ROLE_MARKERS)
