
from __future__ import annotations

from .role_templates import role_template_snapshot_for_task


def is_self_authorized_root_task(task: object) -> bool:
    parent_id = str(getattr(task, "parent_id", "") or "").strip()
    if parent_id:
        return False
    attrs = getattr(task, "attributes", {}) or {}
    if isinstance(attrs, dict) and bool(attrs.get("self_authorized_root")):
        return True
    return bool(role_template_snapshot_for_task(task).get("can_spawn_children"))
