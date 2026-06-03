from __future__ import annotations

"""Subagent runtime config scope identity helpers."""

from typing import Any


def apply_config_overlay_ref(task: Any, params: Any, *, parent_task: Any | None) -> None:
    attrs = params.attributes if isinstance(params.attributes, dict) else {}
    explicit = str(attrs.get("config_overlay_ref") or attrs.get("runtime_config_overlay_ref") or "").strip()
    inherited = inherited_config_overlay_ref(params, parent_task=parent_task)
    overlay_ref = explicit or inherited
    if overlay_ref:
        task.runtime_identity.config_overlay_ref = overlay_ref
    scope = str(attrs.get("config_scope") or attrs.get("runtime_config_scope") or "").strip()
    if scope:
        task.runtime_identity.config_scope = scope
    elif overlay_ref:
        task.runtime_identity.config_scope = "run"


def inherited_config_overlay_ref(params: Any, *, parent_task: Any | None) -> str:
    parent_id = str(params.parent_id or "").strip()
    if not parent_id or parent_task is None:
        return ""
    attrs = params.attributes if isinstance(params.attributes, dict) else {}
    if attrs.get("inherit_config_overlay_ref") is False:
        return ""
    identity = getattr(parent_task, "runtime_identity", None)
    return str(attrs.get("parent_config_overlay_ref") or getattr(identity, "config_overlay_ref", "") or "").strip()


def runtime_config_scope(task: Any) -> dict[str, object]:
    identity = task.runtime_identity
    return {
        "schema_version": "runtime_config_scope.v1",
        "scope": identity.config_scope or "run_override",
        "overlay_ref": identity.config_overlay_ref,
        "promotion_policy": identity.config_promotion_policy,
        "loaded_as": "run_layer" if identity.config_overlay_ref else "base_config",
    }


__all__ = ["apply_config_overlay_ref", "runtime_config_scope"]
